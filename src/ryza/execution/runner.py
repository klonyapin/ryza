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
  ブローカーへ出さず rejected に落とす。**方針(設計リード裁定 2026-08-03)**:
  T-017 第一陣(Ben/Jim)はロングオンリーで運用し、short 生成は ledger の信用記帳
  API 実装後に解禁する。解禁時はこの執行側ガードの解除に先立ち、ゲート/マンデート側
  の事前遮断(short 未対応 FM の注文を gate で block)も実装すること — 執行段の
  rejected は最後の防衛線であり、通常経路で到達させない
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

import psycopg

from ryza.bot import COLOR_FLASH, DISCLAIMER
from ryza.bot.outbox import enqueue
from ryza.execution.broker import EXPIRED, FILLED, REJECTED, Broker, BrokerOrder, BrokerResult
from ryza.gate.orders import (
    TurnoverBreach,
    advance_order_status,
    record_execution,
    turnover_breach_after_execution,
)
from ryza.ledger import posting

_JST = ZoneInfo("Asia/Tokyo")

# ledger(post_fill)が記帳できる side。short/cover は会計未対応のため執行しない。
_LEDGER_SIDES = frozenset({"buy", "sell"})


def _turnover_breach_embed(breach: TurnoverBreach) -> dict[str, Any]:
    """G-7 上限跨ぎ(F-12)通知の embed。#運営 へ urgent で1通。

    NAV が取れなかった fail-closed 経路は理由を明示し、上限は「判定不能」と書く
    (数値を偽装しない — 監査再現性)。
    """
    if breach.nav is None:
        limit_text = (
            f"判定不能({breach.nav_missing_reason or 'gate_log から NAV を取得できない'})"
        )
        title = f"⚠ G-7 事後監視: NAV 判定不能({breach.book_id} {breach.trade_date})"
    else:
        limit_text = f"¥{breach.limit:,.0f}(NAV ¥{breach.nav:,.0f} × 30%)"
        title = (
            f"⚠ G-7 上限跨ぎ検知({breach.book_id} {breach.trade_date}"
            f" — 約定 {breach.execution_id})"
        )
    fields: list[dict[str, Any]] = [
        {
            "name": "約定ベース当日累計",
            "value": (
                f"before ¥{breach.before:,.0f} → after ¥{breach.after:,.0f}"
                f"(注文 {breach.order_id} / instrument {breach.instrument_id})"
            ),
            "inline": False,
        },
        {"name": "上限", "value": limit_text, "inline": False},
    ]
    if breach.nav_source_gate_log_id is not None:
        fields.append(
            {
                "name": "NAV 出所",
                "value": f"compliance.gate_log #{breach.nav_source_gate_log_id}",
                "inline": True,
            }
        )
    return {
        "title": title,
        "description": (
            "約定ベースの当日売買代金が G-7 上限を跨いだ(F-12 事後監視)。"
            "以後の注文は現行 G-7 が塞ぐため事後遮断は行わない。"
        ),
        "color": COLOR_FLASH,
        "fields": fields,
        "footer": {"text": DISCLAIMER},
    }


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
        # 解禁は ledger の信用記帳 API 実装後(モジュール docstring の方針参照)。
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
        # F-12: 約定ベース累計が G-7 上限を跨いだ瞬間を検知し、urgent 通知する。
        # 事後遮断はしない(既に約定済み)— 以後の注文は現行 G-7 が自動的に塞ぐ。
        # 同一トランザクション内で enqueue することで、約定の巻き戻し(トランザクション
        # 失敗)と通知の存在を一致させる(通知だけ残ることを防ぐ)。
        breach = turnover_breach_after_execution(conn, execution_id)
        breach_notified: int | None = None
        if breach is not None:
            breach_notified = enqueue(
                conn,
                "ops",
                _turnover_breach_embed(breach),
                run_id,
                urgent=True,
            )
        outcome: dict[str, Any] = {
            "order_id": order_id,
            "status": "filled",
            "execution_id": execution_id,
            "entry_id": entry_id,
            "qty": str(result.qty),
            "price": str(result.price),
            "fee": str(fee),
        }
        if breach_notified is not None:
            outcome["turnover_breach_outbox_id"] = breach_notified
        return outcome

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
