"""照合(reconciliation)とブレイク管理。

ブローカーの snapshot(ポジション・評価額)と帳簿を突合し、ledger.reconciliations に記録する。
不一致は status='break_open' で登録し、通知フック(on_break コールバック)を呼ぶ。
通知の実装は後続タスク。ここではコールバック interface のみ。

重要(設計書 §9): デモ口座の仮想現金残高は帳簿と一致しない設計のため、**現金総額は照合対象外**。
照合対象はポジション(数量)と評価額のみ。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date as _date
from decimal import Decimal
from typing import Any

import psycopg

from ryza.ledger import _util

# 評価額の許容誤差(丸め対策)
_VALUATION_TOL = Decimal("0.01")

# 不一致時に呼ばれる通知フックの型: (break_info: dict) -> None
BreakCallback = Callable[[dict], None]


@dataclass
class ReconResult:
    """照合結果。all_matched が True なら NAV を confirmed にしてよい。"""

    book_id: str
    recon_date: _date
    all_matched: bool
    rows: list[dict[str, Any]] = field(default_factory=list)
    breaks: list[dict[str, Any]] = field(default_factory=list)


def reconcile(
    conn: psycopg.Connection,
    *,
    book_id: str,
    date: _date,
    broker_snapshot: dict[str, Any],
    run_id: int,
    broker: str = "sim",
    on_break: BreakCallback | None = None,
) -> ReconResult:
    """帳簿とブローカー snapshot を突合し reconciliations に記録して ReconResult を返す。

    broker_snapshot 形式(現金総額は含めない):
        {
          "positions":  {instrument_id: qty, ...},       # ポジション数量
          "valuation":  {instrument_id: market_value, ...}  # 評価額(任意)
        }
    突合項目:
      - position:<instrument> … 保有数量(帳簿の約定再生 vs snapshot)
      - valuation … 有価証券評価額の総額(帳簿の securities 帳簿価額 vs snapshot 合計)
    """
    positions = {int(k): _util.to_decimal(v) for k, v in
                 broker_snapshot.get("positions", {}).items()}
    valuation = {int(k): _util.to_decimal(v) for k, v in
                 broker_snapshot.get("valuation", {}).items()}

    evidence_id = _util.create_evidence(
        conn,
        kind="broker_statement",
        payload={"broker": broker, "as_of": date.isoformat(), "snapshot": broker_snapshot},
        source=broker,
    )

    rows: list[dict[str, Any]] = []
    breaks: list[dict[str, Any]] = []

    # ── ポジション(数量)──
    instruments = set(positions) | set(_util.held_instruments(conn, book_id))
    for iid in sorted(instruments):
        ours_qty, _cost = _util.replay_position(conn, book_id, iid)
        theirs_qty = positions.get(iid, Decimal(0))
        matched = ours_qty == theirs_qty
        rows.append(
            {"item": f"position:{iid}", "ours": ours_qty, "theirs": theirs_qty,
             "status": "matched" if matched else "break_open"}
        )
        if not matched:
            breaks.append(rows[-1])

    # ── 評価額(総額)──
    if valuation:
        ours_val = sum(
            (_util.securities_book_value(conn, book_id, iid, as_of=date)
             for iid in _util.held_instruments(conn, book_id)),
            Decimal(0),
        )
        theirs_val = sum(valuation.values(), Decimal(0))
        matched = abs(ours_val - theirs_val) <= _VALUATION_TOL
        rows.append(
            {"item": "valuation", "ours": ours_val, "theirs": theirs_val,
             "status": "matched" if matched else "break_open"}
        )
        if not matched:
            breaks.append(rows[-1])

    # ── reconciliations へ記録 ──
    with conn.cursor() as cur:
        for r in rows:
            cur.execute(
                """
                INSERT INTO ledger.reconciliations
                    (book_id, recon_date, broker, item, ours, theirs, status, evidence_id)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (book_id, date, broker, r["item"], r["ours"], r["theirs"], r["status"],
                 evidence_id),
            )

    # ── ブレイク通知フック ──
    if on_break is not None:
        for b in breaks:
            on_break({"book_id": book_id, "recon_date": date, "broker": broker, **b})

    return ReconResult(
        book_id=book_id,
        recon_date=date,
        all_matched=not breaks,
        rows=rows,
        breaks=breaks,
    )
