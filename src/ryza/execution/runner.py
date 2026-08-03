"""執行ループ(T-016): status=passed の注文を Broker に流し、約定 → 記帳 → 状態遷移。

**1 注文 = 1 トランザクション**(``conn.transaction()`` のネスト = savepoint)。仕訳の
失敗はその注文の executions 記帳・状態遷移ごと巻き戻り(原子性 — 受け入れ基準)、注文は
passed に留まって次回実行で再試行される。1 注文の失敗は他の注文に波及しない。

記帳経路(不変原則・指示書の遵守):

- ``trading.executions`` への記帳は T-014 の ``record_execution`` 経由のみ
  (追記オンリー+累積数量突合の管轄。直接 INSERT しない)
- ledger への記帳は既存 API ``posting.post_fill`` のみ(約定 = 証券/現金の振替 +
  手数料費用)。証憑は post_fill が kind='broker_fill'・source='trading.executions'・
  payload.fill_id=execution_id で登録するため、仕訳 → 証憑 → execution 行が辿れる
  (evidence_id は execution 行を証憑として登録 — 指示書3)
- デモ執行 MVP は現物 buy/sell のみ。short/cover は ledger の ``post_fill`` が未対応
  (信用・空売りの会計勘定 short_positions/borrowings の記帳 API が無い)ため、
  ブローカーへ出さず rejected に落とす(理由付き — 将来タスクで解除)
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

import psycopg

from ryza.execution.broker import EXPIRED, FILLED, REJECTED, Broker, BrokerOrder, BrokerResult
from ryza.gate.orders import advance_order_status, record_execution
from ryza.ledger import posting

_JST = ZoneInfo("Asia/Tokyo")

# ledger(post_fill)が記帳できる side。short/cover は会計未対応のため執行しない。
_LEDGER_SIDES = frozenset({"buy", "sell"})


def _passed_orders(conn: psycopg.Connection, book_id: str) -> list[dict[str, Any]]:
    """status=passed の注文(+ゲート判定スナップショットの asset_class)を id 順で返す。"""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT o.id, o.fm, o.instrument_id, o.side, o.qty, o.order_type,
                   o.limit_price, o.ref_price, g.order_ref ->> 'asset_class'
            FROM trading.orders o
            JOIN compliance.gate_log g ON g.id = o.gate_log_id
            WHERE o.book_id = %s AND o.status = 'passed'
            ORDER BY o.id
            """,
            (book_id,),
        )
        rows = cur.fetchall()
    return [
        {
            "order_id": r[0],
            "fm": r[1],
            "instrument_id": r[2],
            "side": r[3],
            "qty": Decimal(r[4]),
            "order_type": r[5],
            "limit_price": None if r[6] is None else Decimal(r[6]),
            "ref_price": None if r[7] is None else Decimal(r[7]),
            "asset_class": r[8],
        }
        for r in rows
    ]


def _execute_one(
    conn: psycopg.Connection,
    row: dict[str, Any],
    *,
    book_id: str,
    broker: Broker,
    run_id: int,
    entry_date: date | None,
) -> dict[str, Any]:
    """注文 1 件の執行 → 記帳 → 状態遷移(呼び出し側がトランザクションで囲む)。"""
    order_id = row["order_id"]
    advance_order_status(conn, order_id, "submitted")

    if row["side"] not in _LEDGER_SIDES:
        # ブローカーへ出す前に落とす: 約定してしまうと会計に記帳できず原子性違反になる。
        advance_order_status(conn, order_id, "rejected")
        return {
            "order_id": order_id,
            "status": "rejected",
            "reason": f"デモ執行は現物 buy/sell のみ(side={row['side']} は会計未対応)",
        }

    result: BrokerResult = broker.submit(
        BrokerOrder(
            order_id=order_id,
            book_id=book_id,
            fm=row["fm"],
            instrument_id=row["instrument_id"],
            side=row["side"],
            qty=row["qty"],
            order_type=row["order_type"],
            limit_price=row["limit_price"],
            ref_price=row["ref_price"],
            asset_class=row["asset_class"],
        )
    )

    if result.status == FILLED:
        if result.qty is None or result.price is None or result.executed_at is None:
            raise ValueError(f"BrokerResult(filled) の必須項目欠落(注文 {order_id})")
        fee = Decimal(0) if result.fee is None else result.fee  # 0.00 は falsy — or で潰さない
        execution_id = record_execution(
            conn,
            order_id=order_id,
            qty=result.qty,
            price=result.price,
            fee=fee,
            executed_at=result.executed_at,
            venue=result.venue,
            broker_ref=result.broker_ref,
            run_id=run_id,
        )
        entry_id = posting.post_fill(
            conn,
            book_id=book_id,
            instrument_id=row["instrument_id"],
            side=row["side"],
            qty=result.qty,
            price=result.price,
            fee=fee,
            entry_date=entry_date or result.executed_at.astimezone(_JST).date(),
            run_id=run_id,
            fill_id=execution_id,
            source="trading.executions",
            posted_by="execution.runner",
        )
        advance_order_status(conn, order_id, "filled")
        return {
            "order_id": order_id,
            "status": "filled",
            "execution_id": execution_id,
            "entry_id": entry_id,
            "qty": str(result.qty),
            "price": str(result.price),
            "fee": str(fee),
        }

    if result.status == REJECTED:
        advance_order_status(conn, order_id, "rejected")
        return {"order_id": order_id, "status": "rejected", "reason": result.reason}

    if result.status == EXPIRED:
        advance_order_status(conn, order_id, "cancelled")
        return {"order_id": order_id, "status": "expired", "reason": result.reason}

    raise ValueError(f"未知の BrokerResult.status: {result.status!r}(注文 {order_id})")


def run_pending(
    conn: psycopg.Connection,
    *,
    book_id: str,
    broker: Broker,
    run_id: int,
    entry_date: date | None = None,
) -> dict[str, Any]:
    """帳簿の passed 注文を順に執行する。戻り値は要約 dict。

    - ``entry_date``: ledger 仕訳の記帳日。省略時は約定時刻の JST 日付
    - 1 注文の例外(仕訳失敗等)は savepoint で巻き戻して ``errors`` に記録し、
      後続の注文は続行する(注文は passed のまま → 次回再試行)
    """
    summary: dict[str, Any] = {
        "processed": 0, "filled": 0, "rejected": 0, "expired": 0,
        "errors": [], "orders": [],
    }
    for row in _passed_orders(conn, book_id):
        summary["processed"] += 1
        try:
            with conn.transaction():  # 1 注文 = 1 トランザクション(savepoint)
                outcome = _execute_one(
                    conn, row, book_id=book_id, broker=broker,
                    run_id=run_id, entry_date=entry_date,
                )
        except Exception as exc:  # noqa: BLE001 - 1 注文の失敗は握って次へ(失敗許容)
            summary["errors"].append(
                {"order_id": row["order_id"], "error": f"{type(exc).__name__}: {exc}"}
            )
            continue
        summary[outcome["status"]] += 1
        summary["orders"].append(outcome)
    return summary


__all__ = ["run_pending"]
