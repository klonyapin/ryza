"""jobs.daily のテスト(T-013)。

日次サイクルのステップ実行順・エンドツーエンド完走・失敗許容・冪等(同日再実行で二重投稿しない)・
Kill Switch ゲートを、ローカル DB + ``DryRunProvider``(実 API を呼ばない)で検証する。
"""

from __future__ import annotations

from datetime import UTC, datetime

from ryza.bot import killswitch
from ryza.ingest.jquants import JQuantsAuthError
from ryza.jobs import daily
from ryza.jobs.daily import make_default_ingest, run_daily, run_ingest_sources


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

    # ステップ実行順(取込→前処理→分析→執行/締め→朝刊→サマリ)。
    assert [s.name for s in result.stages] == [
        "ingest", "preprocess", "analysis", "execution", "morning", "ops_summary"
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
