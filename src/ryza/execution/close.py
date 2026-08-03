"""締め処理(T-016): 執行照合 → MTM/NAV(ledger 既存 API)→ ``risk.nav_daily``。

00 §9 の順序「… 会計記帳 → 照合 → NAV 確定」の実装:

1. ``reconcile_executions`` — ``trading.executions`` と ledger 仕訳(broker_fill)の
   件数・金額(約定代金・手数料)突合。A-2(照合状況とブレイク滞留)の基盤。
   ブレイクは ``on_break`` コールバックで通知する(``ledger.recon`` と同じ流儀 —
   通知の実装は呼び出し側)
2. ``ledger.closing.run_daily_close`` — MTM(``post_mark_to_market``)・NAV 算出・
   ``ledger.nav_snapshots`` 更新・ポジション照合。broker_snapshot は
   ``trading.positions`` から合成する(= デモブローカーの「残高証明」。ledger の
   約定再生と執行系ポジションの独立クロスチェックになる)
3. ``risk.nav_daily`` へ book_id×date×nav を upsert。status は執行照合(1)と
   ポジション照合(2)の両方が一致したときのみ confirmed

**NAV 二表の役割分担(T-015 統合時の設計リード裁定 2026-08-03)**:
``ledger.nav_snapshots`` が NAV の正(ledger が所有・T-015 の ``risk/daily.py`` は
これを読む)。``risk.nav_daily`` は同じ NAV に**執行照合の結果を重ねた risk 用
ビュー**(status=confirmed の条件が nav_snapshots より厳しい)であり、正を二重化
するものではない。値は常に run_daily_close の同一計算から書かれるため一致する。

ledger への書込は既存 API(``run_daily_close`` → ``post_mark_to_market``)経由のみ。
照合のための ledger テーブル読み取りは SQL 直読(読み取り専用 — 突合の公開 API が
無いため。書込は一切しない)。
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date as _date
from decimal import Decimal
from typing import Any

import psycopg
from psycopg.types.json import Json

from ryza.execution.demo import latest_close
from ryza.ledger import closing

# 金額突合の許容誤差(丸め対策 — ledger.recon._VALUATION_TOL と同水準)。
_AMOUNT_TOL = Decimal("0.01")

# 不一致時に呼ばれる通知フックの型(ledger.recon.BreakCallback と同形)。
BreakCallback = Callable[[dict], None]


def reconcile_executions(
    conn: psycopg.Connection,
    *,
    book_id: str,
    date: _date,
    on_break: BreakCallback | None = None,
) -> dict[str, Any]:
    """当日(JST)の executions と ledger 仕訳(broker_fill)を件数・金額で突合する。

    ledger 側の約定代金は仕訳の現金行から復元する(買い: |現金貸借差| − 手数料、
    売り: |現金貸借差| + 手数料)— 証憑 payload の再解釈に依存しない、勘定の恒等式
    ベースの突合。戻り値: ``{matched, executions: {...}, ledger: {...}, breaks: [...]}``
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT count(*), COALESCE(sum(e.qty * e.price), 0), COALESCE(sum(e.fee), 0)
            FROM trading.executions e
            JOIN trading.orders o ON o.id = e.order_id
            WHERE o.book_id = %s
              AND (e.executed_at AT TIME ZONE 'Asia/Tokyo')::date = %s
            """,
            (book_id, date),
        )
        exec_count, exec_gross, exec_fee = cur.fetchone()
        exec_gross, exec_fee = Decimal(exec_gross), Decimal(exec_fee)

        # ledger 側: 当日 entry_date の broker_fill 仕訳(source=trading.executions、
        # 逆仕訳・逆仕訳済みは除外)ごとに現金行と手数料行を集計する。
        cur.execute(
            """
            SELECT je.entry_id,
                   COALESCE(sum(jl.debit - jl.credit)
                            FILTER (WHERE jl.account_id = 'cash'), 0)       AS cash_delta,
                   COALESCE(sum(jl.debit)
                            FILTER (WHERE jl.account_id = 'commission'), 0) AS fee_debit
            FROM ledger.journal_entries je
            JOIN ledger.evidence ev ON ev.evidence_id = je.evidence_id
            JOIN ledger.journal_lines jl ON jl.entry_id = je.entry_id
            WHERE je.book_id = %s AND je.entry_date = %s
              AND ev.kind = 'broker_fill' AND ev.source = 'trading.executions'
              AND je.reversal_of IS NULL
              AND NOT EXISTS (SELECT 1 FROM ledger.journal_entries r
                              WHERE r.reversal_of = je.entry_id)
            GROUP BY je.entry_id
            """,
            (book_id, date),
        )
        rows = cur.fetchall()

    ledger_count = len(rows)
    ledger_gross = Decimal(0)
    ledger_fee = Decimal(0)
    for _entry_id, cash_delta, fee_debit in rows:
        cash_delta, fee_debit = Decimal(cash_delta), Decimal(fee_debit)
        ledger_fee += fee_debit
        if cash_delta < 0:  # 買い: cash 貸方 = 代金 + 手数料
            ledger_gross += -cash_delta - fee_debit
        else:  # 売り: cash 借方 = 代金 − 手数料
            ledger_gross += cash_delta + fee_debit

    breaks: list[dict[str, Any]] = []
    checks = (
        ("exec_count", Decimal(exec_count), Decimal(ledger_count), Decimal(0)),
        ("exec_gross", exec_gross, ledger_gross, _AMOUNT_TOL),
        ("exec_fee", exec_fee, ledger_fee, _AMOUNT_TOL),
    )
    for item, ours, theirs, tol in checks:
        if abs(ours - theirs) > tol:
            breaks.append(
                {"item": item, "ours": ours, "theirs": theirs, "status": "break_open"}
            )
    if on_break is not None:
        for b in breaks:
            on_break({"book_id": book_id, "recon_date": date, "broker": "demo", **b})

    return {
        "matched": not breaks,
        "executions": {"count": exec_count, "gross": exec_gross, "fee": exec_fee},
        "ledger": {"count": ledger_count, "gross": ledger_gross, "fee": ledger_fee},
        "breaks": breaks,
    }


def _net_positions(conn: psycopg.Connection, book_id: str) -> dict[int, Decimal]:
    """trading.positions の銘柄別ネット数量(fm 横断合算・ゼロ除外)。"""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT instrument_id, sum(qty) FROM trading.positions
            WHERE book_id = %s GROUP BY instrument_id HAVING sum(qty) <> 0
            """,
            (book_id,),
        )
        return {int(r[0]): Decimal(r[1]) for r in cur.fetchall()}


def _make_price_source(
    conn: psycopg.Connection, date: _date, cache: dict[int, Decimal]
) -> Callable[[int], Decimal]:
    """終値の遅延取得(``ledger.closing.PriceSource`` の callable 形)。

    ledger 側の保有銘柄が trading.positions と食い違っていても、ここで初めて
    参照される — 終値が無ければ明確な例外で締めを失敗させる(評価不能を黙って
    スキップしない)。
    """

    def _price(instrument_id: int) -> Decimal:
        iid = int(instrument_id)
        if iid not in cache:
            close = latest_close(conn, iid, date)
            if close is None:
                raise ValueError(
                    f"締め不能: 銘柄 {iid} の日足終値が無い(~{date.isoformat()})"
                )
            cache[iid] = close
        return cache[iid]

    return _price


def run_demo_close(
    conn: psycopg.Connection,
    *,
    book_id: str,
    date: _date,
    run_id: int,
    on_break: BreakCallback | None = None,
) -> dict[str, Any]:
    """日次締め: 執行照合 → MTM/NAV/ポジション照合(ledger)→ risk.nav_daily。

    戻り値: ``{nav, status, exec_recon, ledger}``。コミットは呼び出し側
    (ledger.posting と同じ流儀)。
    """
    exec_recon = reconcile_executions(conn, book_id=book_id, date=date, on_break=on_break)

    positions = _net_positions(conn, book_id)
    prices: dict[int, Decimal] = {}
    price_source = _make_price_source(conn, date, prices)
    for iid in positions:
        price_source(iid)  # 保有銘柄の終値を先に確定(snapshot の評価額に使う)

    snapshot = {
        "positions": {iid: qty for iid, qty in positions.items()},
        "valuation": {iid: positions[iid] * prices[iid] for iid in positions},
    }
    ledger_summary = closing.run_daily_close(
        conn,
        book_id=book_id,
        date=date,
        price_source=price_source,
        run_id=run_id,
        broker_snapshot=snapshot,
        broker="demo",
        on_break=on_break,
    )

    recon = ledger_summary["recon"]
    positions_matched = recon is not None and recon.all_matched
    status = "confirmed" if (exec_recon["matched"] and positions_matched) else "provisional"
    nav = ledger_summary["nav"]

    detail = {
        "assets_priced_at": date.isoformat(),
        "positions": {str(i): str(q) for i, q in positions.items()},
        "prices": {str(i): str(p) for i, p in prices.items()},
        "exec_recon": {
            "matched": exec_recon["matched"],
            "executions": {k: str(v) for k, v in exec_recon["executions"].items()},
            "ledger": {k: str(v) for k, v in exec_recon["ledger"].items()},
        },
        "position_recon_matched": positions_matched,
        "ledger_status": ledger_summary["status"],
    }
    _upsert_nav_daily(conn, book_id, date, nav, status, detail, run_id)

    return {"nav": nav, "status": status, "exec_recon": exec_recon, "ledger": ledger_summary}


def _upsert_nav_daily(
    conn: psycopg.Connection,
    book_id: str,
    nav_date: _date,
    nav: Decimal,
    status: str,
    detail: dict[str, Any],
    run_id: int,
) -> None:
    """risk.nav_daily を upsert する(同日再締めは上書き — nav_snapshots と同じ流儀)。"""
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO risk.nav_daily (book_id, nav_date, nav, status, detail, run_id)
            VALUES (%s, %s, %s, %s, %s, %s)
            ON CONFLICT (book_id, nav_date) DO UPDATE
            SET nav = EXCLUDED.nav, status = EXCLUDED.status,
                detail = EXCLUDED.detail, run_id = EXCLUDED.run_id, updated_at = now()
            """,
            (book_id, nav_date, nav, status, Json(detail), run_id),
        )


__all__ = ["BreakCallback", "reconcile_executions", "run_demo_close"]
