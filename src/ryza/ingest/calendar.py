"""ingest.calendar — 経済カレンダー取込。

``market.calendar_events``（0008 新設・設計 §6）へ書き込む。初期は次の 2 系統:

1. **静的定義**（日銀・FRB の政策会合、主要指標の定例発表）: ``STATIC_EVENTS`` に宣言的に
   持つ。日付はスケジュール確定分を投入する（当面は年次で追補）。
2. **決算予定**: J-Quants の決算発表予定（``/v1/fins/announcement``）から
   ``event_type='earnings'`` として取り込み、対象銘柄を ``instrument_id`` に解決する。

冪等は ``market.calendar_events`` の ``UNIQUE NULLS NOT DISTINCT`` に委ねる。

実行: ``python -m ryza.ingest.calendar``
"""

from __future__ import annotations

import argparse
from datetime import UTC, datetime
from typing import Any

import psycopg

from ryza.db.conn import connect
from ryza.ingest import base
from ryza.ingest.jquants import _normalize_symbol
from ryza.provenance import Run
from ryza.provenance import run as run_ctx

# ── 静的イベント（政策会合・主要指標の定例）─────────────────────────────────────
# scheduled_at は ISO8601（UTC）。当面は確定スケジュールを追補していく運用。
# importance: 3=最重要（政策決定）/ 2=重要指標 / 1=その他。
STATIC_EVENTS: list[dict[str, Any]] = [
    {
        "event_type": "policy",
        "title": "日銀 金融政策決定会合",
        "scheduled_at": "2026-09-18T03:00:00+00:00",
        "importance": 3,
        "meta": {"authority": "BOJ", "country": "JP"},
    },
    {
        "event_type": "policy",
        "title": "FOMC 政策金利発表",
        "scheduled_at": "2026-09-16T18:00:00+00:00",
        "importance": 3,
        "meta": {"authority": "FRB", "country": "US"},
    },
    {
        "event_type": "indicator",
        "title": "米 雇用統計（非農業部門雇用者数）",
        "scheduled_at": "2026-09-04T12:30:00+00:00",
        "importance": 2,
        "meta": {"country": "US", "series": "PAYEMS"},
    },
    {
        "event_type": "indicator",
        "title": "米 CPI",
        "scheduled_at": "2026-09-10T12:30:00+00:00",
        "importance": 2,
        "meta": {"country": "US", "series": "CPIAUCSL"},
    },
    {
        "event_type": "indicator",
        "title": "日本 全国 CPI",
        "scheduled_at": "2026-09-18T23:30:00+00:00",
        "importance": 2,
        "meta": {"country": "JP", "series": "JP_CPI"},
    },
]


def ingest_static(
    conn: psycopg.Connection,
    run: Run,
    *,
    events: list[dict[str, Any]] | None = None,
    as_of: datetime | None = None,
) -> dict[str, int]:
    """静的イベント定義を ``market.calendar_events`` に取り込む（冪等）。"""
    as_of = as_of or datetime.now(UTC)
    events = events if events is not None else STATIC_EVENTS
    written = 0
    for ev in events:
        scheduled_at = ev["scheduled_at"]
        if isinstance(scheduled_at, str):
            scheduled_at = datetime.fromisoformat(scheduled_at)
        event_id = base.write_calendar_event(
            conn, run,
            event_type=ev["event_type"], title=ev["title"],
            scheduled_at=scheduled_at, instrument_id=ev.get("instrument_id"),
            importance=ev.get("importance", 1), meta=ev.get("meta"), as_of=as_of,
        )
        if event_id is not None:
            written += 1
    return {"written": written, "total": len(events)}


def ingest_earnings(
    conn: psycopg.Connection,
    run: Run,
    announcements: list[dict[str, Any]],
    *,
    as_of: datetime | None = None,
) -> dict[str, int]:
    """J-Quants 決算発表予定を ``event_type='earnings'`` として取り込む（冪等）。

    各予定は対象銘柄を ``instrument_id`` に解決する（未登録は SCD2 自動登録）。
    ``announcements`` は J-Quants ``/v1/fins/announcement`` の ``announcement`` 配列相当。
    """
    as_of = as_of or datetime.now(UTC)
    written = 0
    for ann in announcements:
        symbol = _normalize_symbol(ann.get("Code", ""))
        instrument_id = base.resolve_instrument(
            conn, symbol, asset_class="equity", venue="TSE",
            currency="JPY", as_of=as_of,
        )
        ann_date = ann.get("Date", "")
        try:
            scheduled_at = datetime.fromisoformat(ann_date).replace(tzinfo=UTC)
        except ValueError:
            continue
        name = ann.get("CompanyName", symbol)
        event_id = base.write_calendar_event(
            conn, run,
            event_type="earnings", title=f"{name} 決算発表",
            scheduled_at=scheduled_at, instrument_id=instrument_id,
            importance=2,
            meta={"symbol": symbol, "fiscal_year": ann.get("FiscalYear")},
            as_of=as_of,
        )
        if event_id is not None:
            written += 1
    return {"written": written, "total": len(announcements)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="経済カレンダー取込（静的定義）")
    parser.parse_args(argv)

    conn = connect(autocommit=True)
    try:
        with run_ctx("ingest.calendar", {"kind": "static"}, conn=conn) as r:
            result = ingest_static(conn, r)
        print(f"calendar static: {result}")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
