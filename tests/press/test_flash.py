"""速報エンジンの E2E テスト(モック LLM)。トリガ → outbox urgent + predictions + 的中判定。"""

from __future__ import annotations

from datetime import UTC, datetime

from ryza.press import flash
from ryza.press.config import PressConfig
from ryza.press.flash import (
    FlashTrigger,
    collect_triggers,
    plan_emissions,
    prediction_hit_rate,
    process_triggers,
    verify_due_predictions,
)


def _trigger(
    key: str, *, magnitude: float, refs: list[int], summary: str = "市場観の急変"
) -> FlashTrigger:
    return FlashTrigger(key=key, source="market_view", summary=summary,
                        refs=refs, magnitude=magnitude)


def _urgent_outbox(conn, run_id):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, urgent, embed_json FROM press.outbox WHERE run_id = %s AND urgent = true",
            (run_id,),
        )
        return cur.fetchall()


# ── plan_emissions(純関数)────────────────────────────────────────────────────
def test_plan_emissions_within_limit():
    plan = plan_emissions(2, recent_hour=0, recent_day=0, per_hour=3, per_day=12)
    assert plan.individual == 2 and plan.digest == 0
    assert plan.total_posts == 2


def test_plan_emissions_overflow_to_digest():
    plan = plan_emissions(5, recent_hour=0, recent_day=0, per_hour=3, per_day=12)
    assert plan.individual == 3 and plan.digest == 2
    assert plan.total_posts == 4  # 3 個別 + 1 まとめ速報


def test_plan_emissions_hour_limit_already_hit():
    plan = plan_emissions(3, recent_hour=3, recent_day=5, per_hour=3, per_day=12)
    assert plan.individual == 0 and plan.digest == 3  # 全部まとめ速報へ


# ── E2E ───────────────────────────────────────────────────────────────────────
def test_flash_fact_e2e(conn, run, make_press_llm, insert_enriched_doc):
    doc = insert_enriched_doc(title="急落")
    llm, _ = make_press_llm()
    result = process_triggers(conn, run, llm, [_trigger("mv:1", magnitude=0.8, refs=[doc])])

    published = [o for o in result.outcomes if o.published]
    assert len(published) == 1
    out = published[0]
    assert out.outbox_id is not None
    # 緊急フラグ付きで outbox に投入(赤 embed)。
    rows = _urgent_outbox(conn, run.run_id)
    assert len(rows) == 1
    assert rows[0][2]["color"] == 0xC24E3A
    # flash レポートが保存されている。
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM docs.research_reports "
                    "WHERE run_id = %s AND report_type = 'flash'", (run.run_id,))
        assert cur.fetchone()[0] == 1


def test_flash_below_threshold_records_only(conn, run, make_press_llm, insert_enriched_doc):
    doc = insert_enriched_doc(title="小変動")
    llm, _ = make_press_llm()
    # magnitude 0.3 → 報道価値 30 < 60(閾値)→ 記録のみ。
    result = process_triggers(conn, run, llm, [_trigger("mv:1", magnitude=0.3, refs=[doc])])

    assert all(not o.published for o in result.outcomes)
    assert _urgent_outbox(conn, run.run_id) == []
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM docs.research_reports "
                    "WHERE run_id = %s AND report_type = 'flash_skipped'", (run.run_id,))
        assert cur.fetchone()[0] == 1


def test_flash_prediction_registers_and_verifies(conn, run, make_press_llm, insert_enriched_doc):
    doc = insert_enriched_doc(title="予兆")
    llm, _ = make_press_llm()
    # summary に「予兆」を含めると一次判定が prediction 種別を返す。
    result = process_triggers(
        conn, run, llm, [_trigger("mv:1", magnitude=0.9, refs=[doc], summary="予兆の急変")]
    )
    out = [o for o in result.outcomes if o.published][0]
    assert out.is_prediction
    assert out.prediction_id is not None

    # press.predictions に確度・検証期限つきで登録されている。
    with conn.cursor() as cur:
        cur.execute("SELECT claim, confidence, outcome, verify_by FROM press.predictions "
                    "WHERE id = %s", (out.prediction_id,))
        claim, confidence, outcome, _verify_by = cur.fetchone()
    assert outcome == "pending"
    assert float(confidence) == 0.6

    # 期限到来時の的中判定(resolver は決定論)。
    res = verify_due_predictions(
        conn, run, resolver=lambda claim, conf, meta: "hit",
        as_of=datetime(2026, 8, 11, tzinfo=UTC),
    )
    assert res.verified == 1
    assert res.by_outcome == {"hit": 1}
    rate = prediction_hit_rate(conn)
    assert rate["hit"] == 1 and rate["hit_rate"] == 1.0


def test_flash_rate_limit_consolidates(conn, run, make_press_llm, insert_enriched_doc):
    doc = insert_enriched_doc(title="連発")
    llm, _ = make_press_llm()
    triggers = [_trigger(f"mv:{i}", magnitude=0.8, refs=[doc]) for i in range(5)]
    result = process_triggers(conn, run, llm, triggers)  # per_hour=3(既定)

    # 3 本は個別、超過 2 本は 1 本のまとめ速報に統合 → urgent 行は 4。
    assert result.digest_outbox_id is not None
    rows = _urgent_outbox(conn, run.run_id)
    assert len(rows) == 4
    assert sum(1 for o in result.outcomes if o.published) == 5
    # まとめ速報 embed のタイトル。
    digest_embed = [r[2] for r in rows if r[0] == result.digest_outbox_id][0]
    assert "まとめ速報" in digest_embed["title"]


# ── collect_triggers ───────────────────────────────────────────────────────────
def test_collect_triggers_excludes_processed(conn, run, make_press_llm, insert_flash_trigger,
                                              insert_enriched_doc):
    doc = insert_enriched_doc(title="X")
    t1 = insert_flash_trigger(magnitude=0.8, refs=[doc])
    insert_flash_trigger(magnitude=0.7, refs=[doc])
    since = datetime(2020, 1, 1, tzinfo=UTC)

    triggers = collect_triggers(conn, since=since)
    assert len(triggers) == 2
    keys = {t.key for t in triggers}
    assert f"mv:{t1}" in keys

    # 1 本を速報化済みにする(flash レポートに trigger_key を残す)と次回は除外される。
    llm, _ = make_press_llm()
    process_triggers(conn, run, llm, [t for t in triggers if t.key == f"mv:{t1}"])
    remaining = collect_triggers(conn, since=since)
    assert f"mv:{t1}" not in {t.key for t in remaining}
    assert len(remaining) == 1


def test_run_flash_convenience(conn, run, make_press_llm, insert_flash_trigger,
                               insert_enriched_doc):
    doc = insert_enriched_doc(title="Y")
    insert_flash_trigger(magnitude=0.9, refs=[doc])
    llm, _ = make_press_llm()
    cfg = PressConfig.load()
    result = flash.run_flash(conn, run, llm, cfg=cfg, since=datetime(2020, 1, 1, tzinfo=UTC))
    assert any(o.published for o in result.outcomes)
