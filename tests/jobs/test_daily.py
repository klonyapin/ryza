"""jobs.daily のテスト(T-013)。

日次サイクルのステップ実行順・エンドツーエンド完走・失敗許容・冪等(同日再実行で二重投稿しない)・
Kill Switch ゲートを、ローカル DB + ``DryRunProvider``(実 API を呼ばない)で検証する。
"""

from __future__ import annotations

from datetime import UTC, datetime

from ryza.bot import killswitch
from ryza.jobs import daily
from ryza.jobs.daily import run_daily


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

    # ステップ実行順(取込→前処理→分析→朝刊→サマリ)。
    assert [s.name for s in result.stages] == [
        "ingest", "preprocess", "analysis", "morning", "ops_summary"
    ]
    assert result.ok
    assert all(s.ok for s in result.stages)
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
