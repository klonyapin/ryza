"""jobs.daily のテスト(T-013)。

日次サイクルのステップ実行順・エンドツーエンド完走・失敗許容・冪等(同日再実行で二重投稿しない)・
Kill Switch ゲートを、ローカル DB + ``DryRunProvider``(実 API を呼ばない)で検証する。
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime
from decimal import Decimal

from ryza.bot import COLOR_FLASH, COLOR_NORMAL, killswitch
from ryza.fm.config import BenConfig
from ryza.fm.theses import quarantine_thesis, record_thesis
from ryza.ingest.jquants import JQuantsAuthError
from ryza.jobs import daily
from ryza.jobs.daily import make_default_ingest, run_daily, run_ingest_sources
from ryza.risk.daily import CLOSE_FAILED_NOTE


def _seed(insert_enriched_doc):
    """マクロ/ミクロ/センチメント + 朝刊候補になる素材を数件仕込む。"""
    insert_enriched_doc(
        source_type="news", source_name="日銀", title="金融政策決定会合",
        category="news_monetary_policy", tier="high", score=0.9,
    )
    insert_enriched_doc(
        source_type="filing", source_name="TDnet", title="通期業績上方修正",
        category="filing_earnings", tier="high", score=0.85, instrument_ids=[101],
    )


def _run(conn, run, config, make_daily_llms, **kwargs):
    research, press, provider = make_daily_llms()
    result = run_daily(
        conn, run, research_llm=research, press_llm=press,
        config=config, dry_run=True, **kwargs,
    )
    return result, provider


# ── エンドツーエンド完走 ───────────────────────────────────────────────────────
def test_daily_end_to_end(conn, run, llm_config, make_daily_llms, insert_enriched_doc):
    _seed(insert_enriched_doc)
    result, _ = _run(conn, run, llm_config, make_daily_llms)

    # ステップ実行順(取込→前処理→分析→FM→執行/締め→curated→リスク→朝刊→サマリ)。
    # fm 段は分析の後・執行の前(FM 提案 → ゲート → 執行 — T-017)。
    # risk 段は会計締め(execution 段)の直後(00 §9・設計リード裁定 2026-08-03)。
    # curated 段は risk の分類ステップの直前(2026-08-04 の universe=0 事象の是正)。
    assert [s.name for s in result.stages] == [
        "ingest", "preprocess", "analysis", "fm.jim", "fm.ben", "execution",
        "curated", "risk", "morning", "ops_summary",
    ]
    assert result.ok
    assert all(s.ok for s in result.stages)
    # 執行段(T-016): 注文が無い日は no-op だが、締めは走り NAV を記帳する。
    execution = result.stage("execution")
    assert execution.detail["filled"] == 0
    assert execution.detail["nav_status"] == "confirmed"
    with conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM risk.nav_daily WHERE book_id = 'DEMO_FUND'"
        )
        assert cur.fetchone()[0] == 1
    # 分析が research_reports を生成している。
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM docs.research_reports WHERE run_id = %s", (run.run_id,))
        assert cur.fetchone()[0] >= 1
    # 朝刊が投稿された。
    assert result.posted and result.morning_outbox_id is not None
    # 実行サマリが #運営 へ投入された。
    assert result.ops_outbox_id is not None
    with conn.cursor() as cur:
        cur.execute("SELECT channel FROM press.outbox WHERE id = %s", (result.ops_outbox_id,))
        assert cur.fetchone()[0] == "ops"


# ── 執行段: ゲート通過注文が daily で約定・記帳される(T-016 配線)──────────────
def test_daily_execution_fills_passed_order(
    conn, run, llm_config, make_daily_llms, insert_enriched_doc
):
    from datetime import time
    from decimal import Decimal
    from zoneinfo import ZoneInfo

    from ryza.gate.compliance import OrderProposal
    from ryza.gate.orders import gate_and_record

    _seed(insert_enriched_doc)
    jst = ZoneInfo("Asia/Tokyo")
    today = datetime.now(UTC).astimezone(jst).date()
    with conn.cursor() as cur:  # ゲートの前提状態(取引状態・リスク状態・当日バー)
        cur.execute(
            """
            INSERT INTO ops.trading_state (state, updated_by) VALUES ('normal', 'test')
            ON CONFLICT (singleton) DO UPDATE SET state = 'normal', updated_by = 'test'
            """
        )
        cur.execute(
            """
            INSERT INTO risk.limits_state
                (book_id, dd_soft, dd_hard, vol_exceeded, es_exceeded, as_of)
            VALUES ('DEMO_FUND', false, false, false, false, now())
            ON CONFLICT (book_id) DO UPDATE SET as_of = now()
            """
        )
        cur.execute(
            """
            INSERT INTO market.bars
                (instrument_id, ts, timeframe, close, volume, source, as_of, run_id)
            VALUES (1, %s, '1d', 1000, 1000000, 'test', now(), %s)
            """,
            (datetime.combine(today, time(0, 0), tzinfo=jst), run.run_id),
        )
    proposal = OrderProposal(
        book_id="DEMO_FUND", fm="ben", instrument_id=1, side="buy",
        qty=Decimal(100), order_type="market", ref_price=Decimal(1000),
        product="listed_equity_cash", asset_class="equity_jp",
        universe_tags=("jp_equity_cash",), is_single_name=True, unit_size=Decimal(100),
    )
    order_id, _, verdict = gate_and_record(
        conn, proposal, nav=Decimal(10_000_000), cash=Decimal(9_000_000),
        run_id=run.run_id,
    )
    assert verdict.verdict == "pass", verdict.reasons

    result, _ = _run(conn, run, llm_config, make_daily_llms)
    execution = result.stage("execution")
    assert execution.ok, execution.error
    assert execution.detail["filled"] == 1 and execution.detail["errors"] == 0
    with conn.cursor() as cur:
        cur.execute("SELECT status FROM trading.orders WHERE id = %s", (order_id,))
        assert cur.fetchone()[0] == "filled"
        cur.execute(
            "SELECT status FROM risk.nav_daily WHERE book_id = 'DEMO_FUND' AND nav_date = %s",
            (today,),
        )
        assert cur.fetchone()[0] == "confirmed"


# ── リスク段(T-015)の配線: risk ステージが走り ops へレポートが届く ─────────────
def test_daily_risk_stage_reports_to_ops(
    conn, run, llm_config, make_daily_llms, insert_enriched_doc
):
    _seed(insert_enriched_doc)
    result, _ = _run(conn, run, llm_config, make_daily_llms)
    risk_stage = result.stage("risk")
    assert risk_stage is not None and risk_stage.ok
    assert "DEMO_FUND" in risk_stage.detail
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT count(*) FROM press.outbox
            WHERE channel = 'ops' AND embed_json->>'title' LIKE 'リスクレポート%'
            """
        )
        assert cur.fetchone()[0] >= 1


# ── 締め段の失敗を risk 段へ伝える(独立審査 再々審査 起草者の留意点 (a))────────
def test_daily_close_failure_is_surfaced_in_risk_report(
    conn, run, llm_config, make_daily_llms, insert_enriched_doc, monkeypatch
):
    """締めが落ちた日、リスク日次は未再締めの系列を**黙って**測らない。

    execution 段は savepoint で囲まれているので、締めが例外を投げた日は当日の
    スナップショットも再締めも残らない。それでも risk 段は前日までの系列で測れて
    しまうため、レポート先頭の警告と urgent で「測定の as_of がずれている」ことを
    必ず読ませる。
    """
    _seed(insert_enriched_doc)

    def _boom(*_args, **_kwargs):
        raise RuntimeError("close boom")

    monkeypatch.setattr(daily, "run_demo_close", _boom)
    result, _ = _run(conn, run, llm_config, make_daily_llms)

    execution = result.stage("execution")
    assert not execution.ok and "close boom" in (execution.error or "")
    risk_stage = result.stage("risk")
    assert risk_stage.ok  # 後続段は走る(失敗許容)
    assert risk_stage.detail["DEMO_FUND"]["close_ok"] is False

    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT embed_json, urgent FROM press.outbox
            WHERE channel = 'ops' AND embed_json->>'title' LIKE 'リスクレポート%'
            ORDER BY id DESC LIMIT 1
            """
        )
        embed, urgent = cur.fetchone()
    assert urgent is True
    assert embed["description"].startswith(f"【要確認】{CLOSE_FAILED_NOTE}")
    assert embed["color"] == COLOR_FLASH


def test_daily_close_success_leaves_risk_report_unchanged(
    conn, run, llm_config, make_daily_llms, insert_enriched_doc
):
    """締めが成功した日は従来どおり(締め警告を出さない — 毎日赤にしない)。"""
    _seed(insert_enriched_doc)
    result, _ = _run(conn, run, llm_config, make_daily_llms)
    assert result.stage("execution").ok
    assert result.stage("risk").detail["DEMO_FUND"]["close_ok"] is True
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT embed_json FROM press.outbox
            WHERE channel = 'ops' AND embed_json->>'title' LIKE 'リスクレポート%'
            ORDER BY id DESC LIMIT 1
            """
        )
        embed = cur.fetchone()[0]
    assert CLOSE_FAILED_NOTE not in embed["description"]
    assert not [f for f in embed["fields"] if f["name"] == "本日の締め"]


# ── 冪等(同日再実行で二重投稿しない)──────────────────────────────────────────
def test_daily_idempotent_no_double_post(
    conn, run, llm_config, make_daily_llms, insert_enriched_doc
):
    _seed(insert_enriched_doc)
    r1, _ = _run(conn, run, llm_config, make_daily_llms)
    assert r1.posted

    r2, _ = _run(conn, run, llm_config, make_daily_llms)
    assert not r2.posted
    assert r2.stage("morning").detail.get("skipped") == "already_posted"

    # press.outbox に朝刊 embed は 1 本だけ。
    with conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM press.outbox WHERE embed_json->>'title' = %s",
            (daily.MORNING_TITLE,),
        )
        assert cur.fetchone()[0] == 1


# ── Kill Switch: 投稿はスキップ・分析は走る ──────────────────────────────────────
def test_daily_kill_switch_skips_posting(
    conn, run, llm_config, make_daily_llms, insert_enriched_doc
):
    _seed(insert_enriched_doc)
    killswitch.engage(conn, "1", ["1"], reason="test")

    result, _ = _run(conn, run, llm_config, make_daily_llms)
    assert result.kill_switch
    assert not result.posted
    assert result.stage("morning").detail.get("skipped") == "kill_switch"
    # 分析は走る。
    assert result.stage("analysis").ok
    # 執行段: 新規執行はスキップ、締め(内部会計・NAV)は走る(T-016)。
    execution = result.stage("execution")
    assert execution.ok
    assert execution.detail.get("orders") == "skipped(kill_switch)"
    assert execution.detail.get("nav_status") == "confirmed"
    # 朝刊 embed は投入されない。
    with conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM press.outbox WHERE embed_json->>'title' = %s",
            (daily.MORNING_TITLE,),
        )
        assert cur.fetchone()[0] == 0


# ── FM 段(T-017)の配線: Jim の提案がゲートを通り同日に約定する ─────────────────
def _seed_jim_universe(conn, run) -> int:
    """Jim のユニバース銘柄(curated 分類)+末日にゴールデンクロスする日足を仕込む。"""
    from datetime import time, timedelta
    from zoneinfo import ZoneInfo

    from ryza.risk.classify import Classification, upsert_classification

    jst = ZoneInfo("Asia/Tokyo")
    today: date = datetime.now(UTC).astimezone(jst).date()
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO ops.trading_state (state, updated_by) VALUES ('normal', 'test')
            ON CONFLICT (singleton) DO UPDATE SET state = 'normal', updated_by = 'test'
            """
        )
        cur.execute(
            """
            INSERT INTO risk.limits_state
                (book_id, dd_soft, dd_hard, vol_exceeded, es_exceeded, as_of)
            VALUES ('DEMO_FUND', false, false, false, false, now())
            ON CONFLICT (book_id) DO UPDATE SET as_of = now()
            """
        )
        cur.execute(
            """
            INSERT INTO market.instruments (symbol, asset_class, venue, currency, valid_from)
            VALUES ('1301.T', 'equity', 'TSE', 'JPY', now() - interval '90 days')
            RETURNING instrument_id
            """
        )
        instrument_id = cur.fetchone()[0]
        closes = [1000] * 60 + [1600]
        for i, close in enumerate(closes):
            day = today - timedelta(days=len(closes) - i)
            ts = datetime.combine(day, time(0, 0), tzinfo=jst)
            cur.execute(
                """
                INSERT INTO market.bars
                    (instrument_id, ts, timeframe, close, volume, source, as_of, run_id)
                VALUES (%s, %s, '1d', %s, %s, 'test', %s, %s)
                """,
                (instrument_id, ts, close, 500_000 if i == len(closes) - 1 else 100_000,
                 ts, run.run_id),
            )
        # FM は ledger.nav_snapshots から NAV を読む(会計締めより前に走るため前日値)。
        cur.execute(
            """
            INSERT INTO ledger.nav_snapshots (book_id, snap_date, nav, status, detail)
            VALUES ('DEMO_FUND', %s, 10000000, 'confirmed', '{}'::jsonb)
            ON CONFLICT (book_id, snap_date) DO UPDATE SET nav = EXCLUDED.nav
            """,
            (today - timedelta(days=1),),
        )
    upsert_classification(
        conn,
        instrument_id,
        Classification(
            universe_tags=("liquid_equity",), instrument_flags=(),
            is_single_name=True, product="listed_equity_cash", unit_size=Decimal(100),
            asset_class="equity_jp",
        ),
        run_id=run.run_id,
        source="curated",
        as_of=datetime.now(UTC) - timedelta(days=1),
    )
    return instrument_id


def test_daily_fm_stage_proposes_and_executes(
    conn, run, llm_config, make_daily_llms, insert_enriched_doc
):
    """fm 段が Jim の提案をゲートへ通し、同じ日次の execution 段が約定させる。"""
    _seed(insert_enriched_doc)
    instrument_id = _seed_jim_universe(conn, run)

    result, _ = _run(conn, run, llm_config, make_daily_llms)

    jim = result.stage("fm.jim")
    assert jim is not None and jim.ok, jim.error
    assert jim.detail["passed"] == 1 and jim.detail["blocked"] == 0
    # Ben は LLM 未注入(run_daily に fm_llm を渡していない)のためスキップ。
    assert "skipped" in result.stage("fm.ben").detail
    # 同日の執行段が約定させ、注文には論拠(thesis_id)が紐づいている。
    assert result.stage("execution").detail["filled"] == 1
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT o.status, o.fm, t.direction, t.rule_id
            FROM trading.orders o JOIN trading.fm_theses t ON t.thesis_id = o.thesis_id
            WHERE o.instrument_id = %s
            """,
            (instrument_id,),
        )
        assert cur.fetchone() == ("filled", "jim", "buy", "jim.sma_cross.v1")


def test_daily_fm_stage_skipped_on_kill_switch(
    conn, run, llm_config, make_daily_llms, insert_enriched_doc
):
    """Kill Switch 中は提案自体を作らない(通らないと分かっている案を作らない)。"""
    _seed(insert_enriched_doc)
    _seed_jim_universe(conn, run)
    killswitch.engage(conn, "1", ["1"], reason="test")

    result, _ = _run(conn, run, llm_config, make_daily_llms)
    assert result.stage("fm.jim").detail == {"skipped": "kill_switch"}
    assert result.stage("fm.ben").detail == {"skipped": "kill_switch"}
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM trading.fm_theses")
        assert cur.fetchone()[0] == 0


def test_ben_failure_does_not_roll_back_jim(
    conn, run, llm_config, make_daily_llms, insert_enriched_doc, monkeypatch
):
    """Ben(LLM・週次)の例外で Jim(決定論・日次)の提案・注文が巻き戻らない。

    独立役員審査 T-017 C-5 の是正(fm 段を FM ごとの savepoint に分割)を固定する。
    Ben を当日実行にするため実行曜日を当日に差し替え、run_ben を例外に置き換える。
    """
    _seed(insert_enriched_doc)
    instrument_id = _seed_jim_universe(conn, run)
    weekday = datetime.now(UTC).astimezone(daily.JST).isoweekday()
    base_cfg = BenConfig.load()
    monkeypatch.setattr(
        daily.BenConfig, "load",
        classmethod(lambda cls: replace(base_cfg, weekday=weekday)),
    )

    def _boom(*_args, **_kwargs):
        raise RuntimeError("ben boom")

    monkeypatch.setattr(daily, "run_ben", _boom)

    result, _ = _run(conn, run, llm_config, make_daily_llms, fm_llm=object())

    ben = result.stage("fm.ben")
    assert ben is not None and not ben.ok and "ben boom" in (ben.error or "")
    # Jim の段は成功したまま残り、同日の執行段が約定させる(巻き戻っていない)。
    jim = result.stage("fm.jim")
    assert jim.ok and jim.detail["passed"] == 1
    assert result.stage("execution").detail["filled"] == 1
    with conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM trading.fm_theses WHERE fm = 'jim' AND instrument_id = %s",
            (instrument_id,),
        )
        assert cur.fetchone()[0] == 1


# ── 検疫の可視化(独立役員審査 T-017 C-10)────────────────────────────────────
def _quarantine_theses(conn, run, doc_id, count: int) -> list[int]:
    """ben の提案を count 件記録し、全て検疫する。"""
    ids = []
    for i in range(count):
        thesis_id = record_thesis(
            conn, fm="ben", book_id="DEMO_FUND", instrument_id=900 + i,
            direction="buy", thesis_md="論拠。",
            evidence_refs=[{"kind": "document", "doc_id": doc_id}],
            invalidation_md="崩れたら降りる。", producer="test.ben",
            as_of=datetime.now(UTC), run_id=run.run_id, model="test-mid",
        )
        quarantine_thesis(conn, thesis_id, reason="注入", quarantined_by="test")
        ids.append(thesis_id)
    return ids


def test_ops_summary_always_reports_quarantine_counts(
    conn, run, llm_config, make_daily_llms, insert_enriched_doc
):
    """検疫件数(当日増分/累計)は増分ゼロでも実行サマリに必ず出す。"""
    _seed(insert_enriched_doc)
    result, _ = _run(conn, run, llm_config, make_daily_llms)
    assert result.stage("ops_summary").detail["quarantine_today"] == 0
    with conn.cursor() as cur:
        cur.execute(
            "SELECT embed_json FROM press.outbox WHERE id = %s",
            (result.ops_outbox_id,),
        )
        embed = cur.fetchone()[0]
    names = [f["name"] for f in embed["fields"]]
    assert "検疫(FM 提案)" in names


def test_mass_quarantine_raises_alert(
    conn, run, llm_config, make_daily_llms, insert_enriched_doc
):
    """1日の検疫が閾値以上なら #運営 へ別 embed で警告する(silent な抹消を作らない)。"""
    doc_id = insert_enriched_doc()
    _quarantine_theses(conn, run, doc_id, daily._QUARANTINE_MASS_COUNT)
    result, _ = _run(conn, run, llm_config, make_daily_llms)

    detail = result.stage("ops_summary").detail
    assert detail["quarantine_today"] == daily._QUARANTINE_MASS_COUNT
    assert "quarantine_alert_outbox_id" in detail
    with conn.cursor() as cur:
        cur.execute(
            "SELECT embed_json FROM press.outbox WHERE id = %s",
            (detail["quarantine_alert_outbox_id"],),
        )
        embed = cur.fetchone()[0]
    assert "大量検疫" in embed["title"]
    assert "解除できない" in embed["description"]


def test_mass_quarantine_thresholds():
    """増分ゼロは警告しない。件数閾値・比率閾値のどちらでも発火する(決定論)。"""
    assert not daily._is_mass_quarantine({"today": 0, "total": 50, "theses_total": 60})
    assert daily._is_mass_quarantine(
        {"today": daily._QUARANTINE_MASS_COUNT, "total": 5, "theses_total": 1000}
    )
    assert daily._is_mass_quarantine({"today": 1, "total": 10, "theses_total": 100})
    assert not daily._is_mass_quarantine({"today": 1, "total": 1, "theses_total": 100})


# ── 確定 NAV の書き換え通知(独立審査 再-7)──────────────────────────────────
def _restatement(day: date, age: int, *, missing: bool = False) -> dict:
    return {
        "date": day, "nav_before": Decimal(10_000_000), "nav_after": Decimal(15_000_000),
        "status": "confirmed", "restated": True, "late_entries": True,
        "age_business_days": age, "nav_daily_missing": missing,
    }


def test_restatement_embed_is_urgent_only_for_old_days():
    """しきい値より古い日の書き換えだけを urgent 色・タイトルにする(決定論ルール)。"""
    as_of = datetime(2026, 8, 4, 10, 0, tzinfo=UTC)
    threshold = daily.RESTATEMENT_URGENT_BUSINESS_DAYS

    recent = daily._build_restatement_embed(
        [_restatement(date(2026, 8, 3), threshold)], as_of=as_of
    )
    assert recent["color"] == COLOR_NORMAL and "🚨" not in recent["title"]

    old = daily._build_restatement_embed(
        [_restatement(date(2026, 7, 20), threshold + 1)], as_of=as_of
    )
    assert old["color"] == COLOR_FLASH and "🚨" in old["title"]
    assert f"{threshold} 営業日より古い" in old["description"]


def test_restatement_embed_surfaces_unsynced_nav_daily():
    """nav_daily が追随できなかった日は embed 本文で名指しする(黙って乖離させない)。"""
    embed = daily._build_restatement_embed(
        [_restatement(date(2026, 8, 3), 1, missing=True)],
        as_of=datetime(2026, 8, 4, 10, 0, tzinfo=UTC),
    )
    assert "risk 側は未追随" in embed["fields"][0]["value"]


def test_residue_embed_names_the_instruments():
    """説明不能な残渣は専用 embed で名指しする(実行サマリに埋もれさせない — 新-15)。"""
    embed = daily._build_residue_embed(
        {"1001": {"book_value": "-1000000", "replay_cost": "0", "qty": "0",
                  "reason": "zero_qty_residue"}},
        book_id="DEMO_FUND", day="2026-08-03",
        as_of=datetime(2026, 8, 4, 10, 0, tzinfo=UTC),
    )
    assert embed["color"] == COLOR_FLASH
    assert "1 件" in embed["description"]
    # 0034 以降の残渣は原価恒等式の破れなので、両辺(原価勘定と再生原価)を並べて出す。
    assert embed["fields"][0] == {
        "name": "銘柄 1001",
        "value": "原価勘定 -1000000 / 再生原価 0(zero_qty_residue)",
        "inline": True,
    }


# ── 失敗許容: 一段が落ちても後段は走る ──────────────────────────────────────────
def test_daily_stage_failure_tolerance(conn, run, llm_config, make_daily_llms, insert_enriched_doc):
    _seed(insert_enriched_doc)

    def _boom(_conn, _run, _as_of):
        raise RuntimeError("ingest boom")

    result, _ = _run(conn, run, llm_config, make_daily_llms, ingest=_boom)

    ingest = result.stage("ingest")
    assert not ingest.ok and "ingest boom" in (ingest.error or "")
    # 後段(前処理・分析・朝刊・サマリ)は走り、朝刊は投稿される。
    assert result.stage("preprocess").ok
    assert result.stage("analysis").ok
    assert result.posted
    assert result.stage("ops_summary").ok
    assert not result.ok  # 全体としては失敗(ingest 段が落ちた)


# ── 取込フックが呼ばれる ────────────────────────────────────────────────────────
def test_daily_ingest_hook_invoked(conn, run, llm_config, make_daily_llms, insert_enriched_doc):
    _seed(insert_enriched_doc)
    seen: list[str] = []

    def _ingest(_conn, _run, as_of: datetime) -> dict:
        seen.append("called")
        assert as_of.tzinfo is not None
        return {"fetched": 3}

    result, _ = _run(conn, run, llm_config, make_daily_llms, ingest=_ingest)
    assert seen == ["called"]
    assert result.stage("ingest").ok
    assert result.stage("ingest").detail == {"fetched": 3}


def test_daily_default_as_of_is_utc(conn, run, llm_config, make_daily_llms, insert_enriched_doc):
    _seed(insert_enriched_doc)
    result, _ = _run(conn, run, llm_config, make_daily_llms)
    assert result.as_of.tzinfo is not None
    # 既定 as_of は now(UTC) 付近。
    assert abs((datetime.now(UTC) - result.as_of).total_seconds()) < 30


# ── 実取込の本配線(default_ingest)──────────────────────────────────────────────
def _mock_sources(calls: list[str]):
    """順次実行・部分失敗・資格情報未設定を再現するモックソース列。"""
    def ok(name):
        def _fn(as_of):
            calls.append(name)
            return {"fetched": 1}
        return _fn

    def boom(as_of):
        calls.append("tdnet")
        raise RuntimeError("network down")

    def noauth(as_of):
        calls.append("fred")
        raise JQuantsAuthError("no key")  # auth_errors に含まれる型

    return [("jquants", ok("jquants")), ("tdnet", boom), ("fred", noauth)]


def test_run_ingest_sources_order_and_partial_failure():
    calls: list[str] = []
    summary = run_ingest_sources(datetime.now(UTC), sources=_mock_sources(calls))
    # 全ソースが順に呼ばれる(部分失敗でも後続を止めない)。
    assert calls == ["jquants", "tdnet", "fred"]
    assert summary["ok"] == 1 and summary["failed"] == 1 and summary["skipped"] == 1
    assert summary["sources"]["jquants"]["status"] == "ok"
    assert summary["sources"]["tdnet"]["status"] == "failed"
    assert "network down" in summary["sources"]["tdnet"]["error"]
    # 資格情報未設定は失敗でなく skipped。
    assert summary["sources"]["fred"]["status"] == "skipped"
    assert "資格情報" in summary["sources"]["fred"]["reason"]


def test_run_ingest_sources_dry_run_skips_network():
    calls: list[str] = []
    summary = run_ingest_sources(
        datetime.now(UTC), dry_run=True, sources=_mock_sources(calls)
    )
    # dry-run は 1 度もコーラブルを呼ばない。
    assert calls == []
    assert summary["skipped"] == 3 and summary["ok"] == 0 and summary["failed"] == 0
    assert all(v["status"] == "skipped" for v in summary["sources"].values())


def test_default_ingest_sources_are_wired():
    # T-009 6 ソース + T-012 3 ソースが名前付きで登録されている(呼び出しはしない)。
    from ryza.jobs.daily import _default_ingest_sources

    names = [n for n, _ in _default_ingest_sources()]
    assert names == [
        "jquants", "tdnet", "edinet", "news_rss", "fred", "calendar",
        "edgar", "estat", "intl_banks",
    ]


def test_daily_with_default_ingest_stage(
    conn, run, llm_config, make_daily_llms, insert_enriched_doc
):
    _seed(insert_enriched_doc)
    calls: list[str] = []

    def _src(as_of):
        calls.append("jquants")
        return {"n": 1}

    ingest = make_default_ingest(sources=[("jquants", _src)])
    research, press, _ = make_daily_llms()
    result = run_daily(
        conn, run, research_llm=research, press_llm=press,
        config=llm_config, dry_run=True, ingest=ingest,
    )
    # 注入した実取込フックが取込段で走り、サマリに載る。
    assert calls == ["jquants"]
    ingest_stage = result.stage("ingest")
    assert ingest_stage.ok
    assert ingest_stage.detail["sources"]["jquants"]["status"] == "ok"
    assert result.posted  # 後段は通常どおり
