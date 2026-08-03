"""orders — ゲートの付帯アプリ層(T-014。保護領域 — 定款第5条)。

- ``gate_and_record``: ゲート評価 → ``compliance.gate_log`` 記帳 → ``trading.orders``
  行を passed/blocked で作成、を1トランザクションで行う**唯一の入口**。これ以外に
  orders 行を作る公開 API は存在しない(直接 INSERT は監査 A-3 が突合で検知)
- ``advance_order_status``: 注文状態遷移の強制(不正遷移は例外)
- ``record_execution`` / ``apply_execution``: 約定の記録とポジション反映(冪等)

すべての関数は psycopg 接続 ``conn`` を第1引数に取り、呼び出し側がコミットを制御する
(ledger.posting と同じ流儀)。
"""

from __future__ import annotations

from dataclasses import asdict
from datetime import UTC, date, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

import psycopg
from psycopg.types.json import Json

from ryza.gate.compliance import (
    GateResult,
    LimitsState,
    OrderProposal,
    PortfolioState,
    PositionState,
    evaluate,
    mandates_hash,
)
from ryza.ips import IPSConfig, Mandate, load_and_validate

# 当日売買代金(G-7)の「当日」は JST 日付で区切る(基準通貨 JPY・IPS §1)。
_JST = ZoneInfo("Asia/Tokyo")

# pg_advisory_xact_lock のクラス ID(独立役員審査 2026-08-03 条件2)。
# 帳簿単位でゲート判定〜記帳を直列化し、状態読み出しと記帳の間の TOCTOU を封鎖する。
_GATE_LOCK_CLASS = 4014

# 状態遷移表(不正遷移は例外)。blocked / filled / cancelled / rejected は端状態。
_TRANSITIONS: dict[str, frozenset[str]] = {
    "proposed": frozenset({"passed", "blocked"}),
    "passed": frozenset({"submitted"}),
    "submitted": frozenset({"filled", "cancelled", "rejected"}),
    "blocked": frozenset(),
    "filled": frozenset(),
    "cancelled": frozenset(),
    "rejected": frozenset(),
}


class OrderStatusError(ValueError):
    """不正な注文状態遷移。"""


def advance_order_status(conn: psycopg.Connection, order_id: int, new_status: str) -> str:
    """注文の状態を遷移させる。遷移表(``_TRANSITIONS``)に無い遷移は例外。

    返り値は遷移前の状態。行ロック(FOR UPDATE)で並行遷移を直列化する。
    """
    if new_status not in _TRANSITIONS:
        raise OrderStatusError(f"未知の状態: {new_status!r}")
    with conn.cursor() as cur:
        cur.execute("SELECT status FROM trading.orders WHERE id = %s FOR UPDATE", (order_id,))
        row = cur.fetchone()
        if row is None:
            raise OrderStatusError(f"注文 {order_id} が存在しない")
        current = row[0]
        if new_status not in _TRANSITIONS[current]:
            raise OrderStatusError(
                f"不正な状態遷移: {current} → {new_status}(注文 {order_id})"
            )
        cur.execute(
            "UPDATE trading.orders SET status = %s WHERE id = %s", (new_status, order_id)
        )
    return current


# ── 現在状態の読み出し(gate_and_record 用)──────────────────────────────────
def _load_trading_state(conn: psycopg.Connection) -> str | None:
    """``ops.trading_state`` の現在値。行が無ければ None(→ G-0 が「未初期化」で block)。

    bot 側 ``killswitch.get_state`` は表示用途の互換として欠落=normal を返すが、
    ゲートは fail-closed — 状態が測定できないことを normal と主張しない。
    """
    with conn.cursor() as cur:
        cur.execute("SELECT state FROM ops.trading_state")
        row = cur.fetchone()
        return row[0] if row else None


def _load_positions(conn: psycopg.Connection, book_id: str) -> tuple[PositionState, ...]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT fm, instrument_id, asset_class, qty, avg_cost
            FROM trading.positions
            WHERE book_id = %s AND qty <> 0
            """,
            (book_id,),
        )
        return tuple(
            PositionState(
                fm=r[0], instrument_id=r[1], asset_class=r[2], qty=r[3], avg_cost=r[4]
            )
            for r in cur.fetchall()
        )


def _load_limits(conn: psycopg.Connection, book_id: str) -> LimitsState | None:
    """risk.limits_state の行。無ければ None(→ ゲートが fail-closed で block)。"""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT dd_soft, dd_hard, vol_exceeded, es_exceeded
            FROM risk.limits_state WHERE book_id = %s
            """,
            (book_id,),
        )
        row = cur.fetchone()
    if row is None:
        return None
    return LimitsState(dd_soft=row[0], dd_hard=row[1], vol_exceeded=row[2], es_exceeded=row[3])


def _daily_turnover(conn: psycopg.Connection, book_id: str, trade_date: date) -> Decimal:
    """当日累計売買代金(JST 日付)。

    約定済み(executions の Σ|qty|×price)+未約定の通過注文(passed/submitted の
    Σ|qty|×判定価格)。部分約定は二重計上側(保守側)に倒れる — 暴走ガードの趣旨に沿う。
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT COALESCE(sum(abs(e.qty) * e.price), 0)
            FROM trading.executions e
            JOIN trading.orders o ON o.id = e.order_id
            WHERE o.book_id = %s
              AND (e.executed_at AT TIME ZONE 'Asia/Tokyo')::date = %s
            """,
            (book_id, trade_date),
        )
        executed = cur.fetchone()[0]
        cur.execute(
            """
            SELECT COALESCE(sum(qty * COALESCE(limit_price, ref_price)), 0)
            FROM trading.orders
            WHERE book_id = %s
              AND status IN ('passed', 'submitted')
              AND (created_at AT TIME ZONE 'Asia/Tokyo')::date = %s
            """,
            (book_id, trade_date),
        )
        pending = cur.fetchone()[0]
    return Decimal(executed) + Decimal(pending)


def _proposal_snapshot(proposal: OrderProposal) -> dict:
    """gate_log.order_ref 用のスナップショット(Decimal は文字列化)。"""
    raw = asdict(proposal)
    return {k: (str(v) if isinstance(v, Decimal) else v) for k, v in raw.items()}


def _state_snapshot(state: PortfolioState, trade_date: date) -> dict:
    """gate_log.state_ref 用の判定時状態スナップショット(監査再現性 — 審査条件6)。"""

    def _s(v):
        return None if v is None else str(v)

    return {
        "trading_state": state.trading_state,
        "nav": _s(state.nav),
        "cash": _s(state.cash),
        "daily_turnover": _s(state.daily_turnover),
        "limits_state": None if state.limits is None else asdict(state.limits),
        "prices": {str(k): str(v) for k, v in sorted(state.prices.items())},
        "positions": [
            {
                "fm": p.fm,
                "instrument_id": p.instrument_id,
                "asset_class": p.asset_class,
                "qty": str(p.qty),
                "avg_cost": str(p.avg_cost),
            }
            for p in (state.positions or ())
        ],
        "trade_date": trade_date.isoformat(),
    }


def gate_and_record(
    conn: psycopg.Connection,
    proposal: OrderProposal,
    *,
    nav: Decimal | None,
    cash: Decimal | None,
    run_id: int,
    prices: dict[int, Decimal] | None = None,
    ips: IPSConfig | None = None,
    mandates: dict[str, Mandate] | None = None,
    trade_date: date | None = None,
) -> tuple[int, int, GateResult]:
    """ゲート評価 → gate_log 記帳 → orders 行作成を1トランザクションで行う唯一の入口。

    - 取引状態(``ops.trading_state``)・ポジション・当日売買代金・リスク状態は DB から読む
    - ``nav``/``cash`` は評価エンジン(T-015)の管轄のため呼び出し側が渡す
      (None なら fail-closed で block)
    - 返り値は ``(order_id, gate_log_id, GateResult)``。コミットは呼び出し側

    verdict pass/warn → status='passed'、block → status='blocked'(端状態)。
    """
    if ips is None or mandates is None:
        loaded_ips, loaded_mandates = load_and_validate()
        ips = ips or loaded_ips
        mandates = mandates or loaded_mandates
    if trade_date is None:
        trade_date = datetime.now(UTC).astimezone(_JST).date()

    # 帳簿単位の直列化(トランザクション終了まで保持)。同一帳簿の並行ゲート判定が
    # 同じ「約定前状態」を読んで二重に枠を消費すること(TOCTOU)を防ぐ。
    with conn.cursor() as cur:
        cur.execute(
            "SELECT pg_advisory_xact_lock(%s, hashtext(%s))",
            (_GATE_LOCK_CLASS, proposal.book_id),
        )

    state = PortfolioState(
        trading_state=_load_trading_state(conn),
        nav=nav,
        cash=cash,
        positions=_load_positions(conn, proposal.book_id),
        daily_turnover=_daily_turnover(conn, proposal.book_id, trade_date),
        limits=_load_limits(conn, proposal.book_id),
        prices=prices or {},
    )
    result = evaluate(proposal, state, ips, mandates)

    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO compliance.gate_log
                (order_ref, book_id, fm, verdict, reasons, checked_rules, state_ref,
                 ips_version, mandates_hash, run_id)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING id
            """,
            (
                Json(_proposal_snapshot(proposal)),
                proposal.book_id,
                proposal.fm,
                result.verdict,
                Json([asdict(r) for r in result.reasons]),
                Json(list(result.checked_rules)),
                Json(_state_snapshot(state, trade_date)),
                ips.version,
                mandates_hash(mandates),
                run_id,
            ),
        )
        gate_log_id = cur.fetchone()[0]
        status = "blocked" if result.blocked else "passed"
        cur.execute(
            """
            INSERT INTO trading.orders
                (book_id, fm, instrument_id, side, qty, order_type, limit_price,
                 ref_price, status, gate_log_id, run_id)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING id
            """,
            (
                proposal.book_id,
                proposal.fm,
                proposal.instrument_id,
                proposal.side,
                proposal.qty,
                proposal.order_type,
                proposal.limit_price,
                proposal.ref_price,
                status,
                gate_log_id,
                run_id,
            ),
        )
        order_id = cur.fetchone()[0]
    return order_id, gate_log_id, result


# ── 約定の記録とポジション反映 ────────────────────────────────────────────────
def record_execution(
    conn: psycopg.Connection,
    *,
    order_id: int,
    qty: Decimal,
    price: Decimal,
    fee: Decimal = Decimal(0),
    executed_at: datetime,
    venue: str = "demo",
    broker_ref: str | None = None,
    run_id: int,
) -> int:
    """約定を ``trading.executions`` に記録し、ポジションへ反映して execution_id を返す。

    注文は submitted 状態でなければならない(blocked/passed のままの約定はゲート迂回
    または執行手順違反 — A-3 の検知対象になる前にここで拒否する)。累積約定数量
    (部分約定の合算+本約定)が注文数量を超える場合も例外(審査条件1)。
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT status, qty FROM trading.orders WHERE id = %s FOR UPDATE", (order_id,)
        )
        row = cur.fetchone()
        if row is None:
            raise ValueError(f"注文 {order_id} が存在しない")
        status, order_qty = row
        if status != "submitted":
            raise OrderStatusError(
                f"約定は submitted 状態の注文のみ受け付ける(注文 {order_id} は {status})"
            )
        # 累積約定 ≤ 注文数量(注文行は FOR UPDATE 済みなので並行 fill とも直列)。
        cur.execute(
            "SELECT COALESCE(sum(qty), 0) FROM trading.executions WHERE order_id = %s",
            (order_id,),
        )
        already_filled = Decimal(cur.fetchone()[0])
        if already_filled + qty > Decimal(order_qty):
            raise ValueError(
                f"累積約定数量 {already_filled}+{qty} が注文数量 {order_qty} を超過"
                f"(注文 {order_id})"
            )
        cur.execute(
            """
            INSERT INTO trading.executions
                (order_id, qty, price, fee, executed_at, venue, broker_ref, run_id)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING id
            """,
            (order_id, qty, price, fee, executed_at, venue, broker_ref, run_id),
        )
        execution_id = cur.fetchone()[0]
    apply_execution(conn, execution_id, run_id=run_id)
    return execution_id


def apply_execution(conn: psycopg.Connection, execution_id: int, *, run_id: int) -> bool:
    """約定を ``trading.positions`` に反映する(移動平均法・クローズ処理)。

    冪等: 同一 execution の再適用は ``trading.position_applies`` の PK 衝突で無視され
    False を返す(初回適用は True)。asset_class は注文のゲート判定スナップショット
    (gate_log.order_ref)から取る — ゲートを経ていない約定はここで参照が壊れて失敗する。
    """
    with conn.cursor() as cur:
        # 冪等性台帳への追記が「適用権」の獲得。衝突したら適用済み。
        cur.execute(
            """
            INSERT INTO trading.position_applies (execution_id, run_id)
            VALUES (%s, %s) ON CONFLICT (execution_id) DO NOTHING
            """,
            (execution_id, run_id),
        )
        if cur.rowcount == 0:
            return False

        cur.execute(
            """
            SELECT e.qty, e.price, o.book_id, o.fm, o.instrument_id, o.side,
                   g.order_ref ->> 'asset_class'
            FROM trading.executions e
            JOIN trading.orders o ON o.id = e.order_id
            JOIN compliance.gate_log g ON g.id = o.gate_log_id
            WHERE e.id = %s
            """,
            (execution_id,),
        )
        row = cur.fetchone()
        if row is None:
            raise ValueError(f"約定 {execution_id} が存在しない")
        exec_qty, price, book_id, fm, instrument_id, side, asset_class = row
        if not asset_class:
            raise ValueError(f"約定 {execution_id}: ゲート判定に asset_class が無い")
        delta = Decimal(exec_qty) if side in ("buy", "cover") else -Decimal(exec_qty)
        price = Decimal(price)

        cur.execute(
            """
            SELECT qty, avg_cost FROM trading.positions
            WHERE book_id = %s AND fm = %s AND instrument_id = %s
            FOR UPDATE
            """,
            (book_id, fm, instrument_id),
        )
        pos = cur.fetchone()
        pre_qty = Decimal(pos[0]) if pos else Decimal(0)
        pre_avg = Decimal(pos[1]) if pos else Decimal(0)
        new_qty = pre_qty + delta

        if pre_qty == 0 or (pre_qty > 0) == (delta > 0):
            # 新規または増し玉: 移動平均で取得単価を更新。
            new_avg = (abs(pre_qty) * pre_avg + abs(delta) * price) / (abs(pre_qty) + abs(delta))
        elif new_qty == 0:
            new_avg = Decimal(0)  # 全クローズ
        elif (new_qty > 0) == (pre_qty > 0):
            new_avg = pre_avg  # 部分クローズ: 残玉の単価は不変
        else:
            new_avg = price  # ドテン(反対売買が建玉を突き抜けた): 残玉は約定値で新規

        cur.execute(
            """
            INSERT INTO trading.positions
                (book_id, fm, instrument_id, asset_class, qty, avg_cost, updated_at, run_id)
            VALUES (%s, %s, %s, %s, %s, %s, now(), %s)
            ON CONFLICT (book_id, fm, instrument_id) DO UPDATE
            SET qty = EXCLUDED.qty, avg_cost = EXCLUDED.avg_cost,
                asset_class = EXCLUDED.asset_class,
                updated_at = now(), run_id = EXCLUDED.run_id
            """,
            (book_id, fm, instrument_id, asset_class, new_qty, new_avg, run_id),
        )
    return True


__all__ = [
    "OrderStatusError",
    "advance_order_status",
    "apply_execution",
    "gate_and_record",
    "record_execution",
]
