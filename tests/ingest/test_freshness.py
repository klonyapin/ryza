"""鮮度 SLA 検査テスト。SLA 超過の発火・鮮度内の不発火・#運営 への投入。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from ryza.ingest import base, freshness
from ryza.ingest.freshness import FreshnessSLA


def _sla_doc(source_name, minutes=30):
    return FreshnessSLA(
        label=f"{source_name} test", kind="documents", key=source_name,
        max_age=timedelta(minutes=minutes),
    )


def test_fresh_document_no_breach(conn, run, store):
    now = datetime(2026, 8, 3, 12, 0, tzinfo=UTC)
    base.upsert_document(
        conn, run, store,
        source_type="filing", source_name="FRESH_SRC",
        title="t", body="b", as_of=now - timedelta(minutes=5),
        raw_payload=b"x", evidence_kind="test",
    )
    breaches = freshness.check_freshness(
        conn, slas=[_sla_doc("FRESH_SRC")], now=now
    )
    assert breaches == []


def test_stale_document_breach_fires(conn, run, store):
    now = datetime(2026, 8, 3, 12, 0, tzinfo=UTC)
    base.upsert_document(
        conn, run, store,
        source_type="filing", source_name="STALE_SRC",
        title="t", body="b", as_of=now - timedelta(hours=2),
        raw_payload=b"x", evidence_kind="test",
    )
    breaches = freshness.check_freshness(
        conn, slas=[_sla_doc("STALE_SRC", minutes=30)], now=now
    )
    assert len(breaches) == 1
    assert breaches[0].reason == "stale"


def test_no_data_is_breach(conn):
    now = datetime(2026, 8, 3, 12, 0, tzinfo=UTC)
    breaches = freshness.check_freshness(
        conn, slas=[_sla_doc("NEVER_INGESTED")], now=now
    )
    assert len(breaches) == 1
    assert breaches[0].reason == "no_data"
    assert breaches[0].last_as_of is None


def test_run_check_enqueues_to_ops_outbox(conn, run, store):
    now = datetime(2026, 8, 3, 12, 0, tzinfo=UTC)
    base.upsert_document(
        conn, run, store,
        source_type="filing", source_name="STALE2",
        title="t", body="b", as_of=now - timedelta(hours=5),
        raw_payload=b"x", evidence_kind="test",
    )
    result = freshness.run_check(
        conn, run, slas=[_sla_doc("STALE2", minutes=30)], now=now
    )
    assert result["breaches"] == 1
    assert result["enqueued"] == 1
    # press.outbox（#運営 = channel 'ops'）へ未送で投入されている。
    with conn.cursor() as cur:
        cur.execute(
            "SELECT channel, urgent, run_id, sent_at FROM press.outbox "
            "WHERE run_id=%s ORDER BY id DESC LIMIT 1",
            (run.run_id,),
        )
        channel, urgent, run_id, sent_at = cur.fetchone()
    assert channel == "ops"
    assert urgent is True
    assert run_id == run.run_id
    assert sent_at is None


def test_bars_and_indicators_freshness_kinds(conn, run, store):
    now = datetime(2026, 8, 3, 12, 0, tzinfo=UTC)
    # bars 系（J-Quants）を新鮮に入れる → bars SLA は不発火。
    instrument_id = base.resolve_instrument(conn, "1111.T")
    base.write_bar(
        conn, run, instrument_id=instrument_id, ts=now, timeframe="1d",
        open=1, high=1, low=1, close=1, volume=1, source="jquants",
        as_of=now - timedelta(minutes=1),
    )
    slas = [
        FreshnessSLA("jq", "bars", "jquants", timedelta(hours=1)),
        FreshnessSLA("fred", "indicators", "FRED:%", timedelta(hours=1)),
    ]
    breaches = freshness.check_freshness(conn, slas=slas, now=now)
    # bars は新鮮 → 不発火。indicators は未取込 → no_data で発火。
    reasons = {b.sla.kind: b.reason for b in breaches}
    assert "bars" not in reasons
    assert reasons.get("indicators") == "no_data"
