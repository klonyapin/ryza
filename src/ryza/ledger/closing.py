"""日次締め(設計書 §5 のシーケンス)。

run_daily_close:
  1. 未記帳の約定を検出して記帳(冪等: 記帳済み fill はスキップ)
  2. 全ポジションを終値で評価替え(price_snapshot を evidence 化)
  3. アクルーアル(当面は手数料のみ。金利は TODO)
  4. NAV 算出 → nav_snapshots に provisional で保存
  5. recon の照合結果が全件 matched なら confirmed に更新、不一致なら provisional のまま
"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import date as _date
from decimal import Decimal
from typing import Any

import psycopg

from ryza.ledger import _util, posting, recon, statements

# 帳簿 -> trade.order_intents.track の対応
_BOOK_TRACK = {"DEMO_FUND": "demo", "LIVE_FUND": "live"}

# price_source は callable(instrument_id)->price、または dict{instrument_id: price}
PriceSource = Callable[[int], Any] | dict[int, Any]


def _price_of(price_source: PriceSource, instrument_id: int) -> Any:
    if callable(price_source):
        return price_source(instrument_id)
    return price_source[instrument_id]


def _recorded_fill_ids(conn: psycopg.Connection, book_id: str) -> set[int]:
    """既に記帳済みの trade fill_id の集合(broker_fill 証憑の payload から抽出)。"""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT e.payload_ref
            FROM ledger.journal_entries je
            JOIN ledger.evidence e ON e.evidence_id = je.evidence_id
            WHERE je.book_id = %s AND e.kind = 'broker_fill'
            """,
            (book_id,),
        )
        recorded: set[int] = set()
        for (text,) in cur.fetchall():
            try:
                fid = json.loads(text).get("fill_id")
            except (ValueError, TypeError):
                continue
            if fid is not None:
                recorded.add(int(fid))
    return recorded


def _record_unrecorded_fills(
    conn: psycopg.Connection, book_id: str, date: _date, run_id: int
) -> list[int]:
    """trade.fills のうち未記帳のものを検出して記帳する。冪等。記帳した entry_id を返す。

    fill -> order -> intent の連鎖で track(=帳簿)と instrument/side を解決する。
    OPS 帳簿や、track 対応の無い帳簿では何もしない。
    """
    track = _BOOK_TRACK.get(book_id)
    if track is None:
        return []

    recorded = _recorded_fill_ids(conn, book_id)
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT f.fill_id, oi.instrument_id, oi.side, f.qty, f.price, f.fee,
                   f.filled_at::date
            FROM trade.fills f
            JOIN trade.orders o ON o.order_id = f.order_id
            JOIN trade.order_intents oi ON oi.intent_id = o.intent_id
            WHERE oi.track = %s
            ORDER BY f.fill_id
            """,
            (track,),
        )
        pending = cur.fetchall()

    entry_ids: list[int] = []
    for fill_id, instrument_id, side, qty, price, fee, filled_date in pending:
        if fill_id in recorded:
            continue
        norm_side = "buy" if side in ("buy", "long") else "sell"
        entry_ids.append(
            posting.post_fill(
                conn,
                book_id=book_id,
                instrument_id=instrument_id,
                side=norm_side,
                qty=qty,
                price=price,
                fee=fee or 0,
                entry_date=filled_date or date,
                run_id=run_id,
                fill_id=fill_id,
                source="trade.fills",
                posted_by="ledger.closing",
            )
        )
    return entry_ids


def run_daily_close(
    conn: psycopg.Connection,
    *,
    book_id: str,
    date: _date,
    price_source: PriceSource,
    run_id: int,
    broker_snapshot: dict[str, Any] | None = None,
    broker: str = "sim",
    on_break: recon.BreakCallback | None = None,
) -> dict[str, Any]:
    """日次締めを実行し、要約 dict を返す。

    戻り値: {nav, status, marked, fills_recorded, recon}
    """
    bt = _util.book_type(conn, book_id)

    # 1. 未記帳の約定を検出して記帳(冪等)
    fills_recorded = _record_unrecorded_fills(conn, book_id, date, run_id)

    # 2. 全ポジションを終値で評価替え(ファンド帳簿のみ)
    marked: list[int] = []
    positions_detail: dict[str, Any] = {}
    if bt == "fund":
        for iid in _util.held_instruments(conn, book_id):
            qty, _cost = _util.replay_position(conn, book_id, iid)
            if qty == 0:
                continue
            price = _util.to_decimal(_price_of(price_source, iid))
            entry_id = posting.post_mark_to_market(
                conn,
                book_id=book_id,
                instrument_id=iid,
                price=price,
                entry_date=date,
                run_id=run_id,
                posted_by="ledger.closing",
            )
            if entry_id is not None:
                marked.append(entry_id)
            positions_detail[str(iid)] = {
                "qty": str(qty),
                "price": str(price),
                "market_value": str(qty * price),
            }

    # 3. アクルーアル: 当面は手数料のみ(約定時に計上済み)。
    #    TODO: 金利(信用取引の支払利息 interest_expense / 貸株料など)の日次アクルーアル。

    # 4. NAV 算出(= 資産 − 負債)→ nav_snapshots に provisional で保存
    totals = statements.book_totals(conn, book_id, date)
    nav = totals["nav"]
    detail = {
        "assets": str(totals["assets"]),
        "liabilities": str(totals["liabilities"]),
        "net_income": str(totals["net_income"]),
        "positions": positions_detail,
        "priced_at": date.isoformat(),
    }
    _upsert_nav(conn, book_id, date, nav, "provisional", detail)

    # 5. ブローカー照合。全件 matched なら confirmed に更新。
    recon_result = None
    status = "provisional"
    if broker_snapshot is not None:
        recon_result = recon.reconcile(
            conn,
            book_id=book_id,
            date=date,
            broker_snapshot=broker_snapshot,
            run_id=run_id,
            broker=broker,
            on_break=on_break,
        )
        if recon_result.all_matched:
            _upsert_nav(conn, book_id, date, nav, "confirmed", detail)
            status = "confirmed"

    return {
        "nav": nav,
        "status": status,
        "marked": marked,
        "fills_recorded": fills_recorded,
        "recon": recon_result,
    }


def _upsert_nav(
    conn: psycopg.Connection,
    book_id: str,
    snap_date: _date,
    nav: Decimal,
    status: str,
    detail: dict[str, Any],
) -> None:
    """nav_snapshots を upsert する(同日再締めは上書き。provisional→confirmed の更新に対応)。"""
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO ledger.nav_snapshots (book_id, snap_date, nav, status, detail)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (book_id, snap_date)
            DO UPDATE SET nav = EXCLUDED.nav, status = EXCLUDED.status, detail = EXCLUDED.detail
            """,
            (book_id, snap_date, nav, status, json.dumps(detail)),
        )
