"""経済カレンダー取込テスト。静的定義・決算予定・冪等。"""

from __future__ import annotations

from datetime import UTC, datetime

from ryza.ingest import calendar


def test_ingest_static_idempotent(conn, run):
    events = [
        {"event_type": "policy", "title": "日銀 会合",
         "scheduled_at": "2026-09-18T03:00:00+00:00", "importance": 3,
         "meta": {"authority": "BOJ"}},
        {"event_type": "indicator", "title": "米 CPI",
         "scheduled_at": "2026-09-10T12:30:00+00:00", "importance": 2},
    ]
    r1 = calendar.ingest_static(conn, run, events=events)
    r2 = calendar.ingest_static(conn, run, events=events)
    assert r1["written"] == 2
    assert r2["written"] == 0  # UNIQUE NULLS NOT DISTINCT で冪等
    with conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM market.calendar_events WHERE event_type='policy'"
        )
        assert cur.fetchone()[0] == 1


def test_ingest_static_writes_run_and_asof(conn, run):
    as_of = datetime(2026, 8, 3, tzinfo=UTC)
    calendar.ingest_static(
        conn, run,
        events=[{"event_type": "policy", "title": "T", "scheduled_at":
                 "2026-09-18T03:00:00+00:00"}],
        as_of=as_of,
    )
    with conn.cursor() as cur:
        cur.execute(
            "SELECT run_id, as_of FROM market.calendar_events WHERE title='T'"
        )
        row = cur.fetchone()
    assert row[0] == run.run_id
    assert row[1] == as_of


def test_ingest_earnings_resolves_instrument(conn, run):
    announcements = [
        {"Code": "72030", "Date": "2026-08-10", "CompanyName": "トヨタ自動車",
         "FiscalYear": "2027-03"},
    ]
    r1 = calendar.ingest_earnings(conn, run, announcements)
    assert r1["written"] == 1
    with conn.cursor() as cur:
        cur.execute(
            "SELECT ce.instrument_id, i.symbol "
            "FROM market.calendar_events ce "
            "JOIN market.instruments i ON i.instrument_id = ce.instrument_id "
            "WHERE ce.event_type='earnings'"
        )
        row = cur.fetchone()
    assert row[1] == "7203.T"
    # 冪等。
    r2 = calendar.ingest_earnings(conn, run, announcements)
    assert r2["written"] == 0


def test_static_events_catalog_nonempty():
    assert len(calendar.STATIC_EVENTS) >= 3
    assert any(e["event_type"] == "policy" for e in calendar.STATIC_EVENTS)
