"""0014_trading.sql と付帯アプリ層(gate_and_record / apply_execution)の受け入れテスト。

テスト専用 DB(tests/conftest.py の ``migrated_db``)に対して実行する。接続不可なら
skip。テストは commit せず rollback で隔離する(risk.limits_state 等の挿入も巻き戻る)。
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

import psycopg
import pytest

from ryza.gate.orders import (
    OrderStatusError,
    _daily_turnover,
    advance_order_status,
    apply_execution,
    gate_and_record,
    record_execution,
)

from .conftest import jp_stock_proposal

_NAV = Decimal(10_000_000)
_CASH = Decimal(5_000_000)


def _gate(conn, run_id, proposal=None, *, nav=_NAV, cash=_CASH, **kw):
    return gate_and_record(
        conn, proposal or jp_stock_proposal(), nav=nav, cash=cash, run_id=run_id, **kw
    )


def _order_row(conn, order_id):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT status, gate_log_id, ref_price FROM trading.orders WHERE id = %s",
            (order_id,),
        )
        return cur.fetchone()


def _position(conn, fm="ben", instrument_id=1):
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT qty, avg_cost, asset_class FROM trading.positions
            WHERE book_id = 'DEMO_FUND' AND fm = %s AND instrument_id = %s
            """,
            (fm, instrument_id),
        )
        return cur.fetchone()


# ── スキーマの存在 ────────────────────────────────────────────────────────────
def test_trading_tables_exist(conn):
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT table_schema, table_name FROM information_schema.tables
            WHERE table_schema IN ('trading', 'compliance', 'risk')
            """
        )
        tables = {(r[0], r[1]) for r in cur.fetchall()}
    assert {
        ("trading", "orders"),
        ("trading", "executions"),
        ("trading", "positions"),
        ("trading", "position_applies"),
        ("compliance", "gate_log"),
        ("risk", "limits_state"),
    }.issubset(tables)


# ── 受け入れ基準1: pass/block が journal どおり記録される ────────────────────
def test_gate_and_record_pass(conn, run_id, limits_row):
    limits_row()
    order_id, gate_log_id, result = _gate(conn, run_id)
    assert result.verdict == "pass"
    status, linked_gate_log, ref_price = _order_row(conn, order_id)
    assert status == "passed"
    assert linked_gate_log == gate_log_id
    assert ref_price == Decimal(1000)
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT verdict, reasons, checked_rules, order_ref, ips_version, mandates_hash
            FROM compliance.gate_log WHERE id = %s
            """,
            (gate_log_id,),
        )
        verdict, reasons, checked_rules, order_ref, ips_version, mandates_hash = cur.fetchone()
    assert verdict == "pass"
    assert reasons == []
    assert checked_rules[0] == "G-F" and "G-10" in checked_rules
    assert order_ref["fm"] == "ben" and order_ref["qty"] == "100"
    assert ips_version  # 判定に使った IPS 版が残る
    assert len(mandates_hash) == 64


def test_gate_and_record_block_fail_closed_without_limits(conn, run_id):
    """risk.limits_state の行が無ければ fail-closed で block(G-F)。"""
    order_id, gate_log_id, result = _gate(conn, run_id)
    assert result.verdict == "block"
    assert any("limits_state" in r.message for r in result.reasons)
    assert _order_row(conn, order_id)[0] == "blocked"


def test_gate_and_record_block_records_reasons(conn, run_id, limits_row):
    limits_row()
    proposal = jp_stock_proposal(side="short")  # Ben はショート禁止
    order_id, gate_log_id, result = _gate(conn, run_id, proposal)
    assert result.verdict == "block"
    with conn.cursor() as cur:
        cur.execute("SELECT reasons FROM compliance.gate_log WHERE id = %s", (gate_log_id,))
        reasons = cur.fetchone()[0]
    assert any(r["rule"] == "G-9" and r["severity"] == "block" for r in reasons)
    assert _order_row(conn, order_id)[0] == "blocked"


def test_gate_and_record_warn_passes_with_reasons(conn, run_id, limits_row):
    """dd_soft 中の小口新規建ては warn 付きで passed。"""
    limits_row(dd_soft=True)
    order_id, gate_log_id, result = _gate(
        conn, run_id, jp_stock_proposal(qty=Decimal(10))
    )
    assert result.verdict == "warn"
    assert _order_row(conn, order_id)[0] == "passed"


def test_gate_and_record_frozen_state_blocks(conn, run_id, limits_row):
    """ops.trading_state が normal 以外なら G-0 で block(DB からの読み出し)。"""
    limits_row()
    with conn.cursor() as cur:
        cur.execute("DELETE FROM ops.trading_state")  # トランザクション内・rollback で復元
        cur.execute(
            """
            INSERT INTO ops.trading_state (state, reason, updated_by)
            VALUES ('frozen', 'test', 'test')
            """
        )
    _, _, result = _gate(conn, run_id)
    assert result.verdict == "block"
    assert any(r.rule == "G-0" for r in result.reasons)


def test_gate_and_record_atomicity(conn, run_id, limits_row):
    """gate_log と orders は同一トランザクション — 失敗時はどちらも残らない。"""
    limits_row()
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM compliance.gate_log")
        before = cur.fetchone()[0]
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        with conn.transaction():  # savepoint — 失敗はここで巻き戻る
            _gate(conn, run_id, jp_stock_proposal(book_id="NO_SUCH_BOOK"))
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM compliance.gate_log")
        assert cur.fetchone()[0] == before
        cur.execute("SELECT count(*) FROM trading.orders WHERE book_id = 'NO_SUCH_BOOK'")
        assert cur.fetchone()[0] == 0


def test_daily_turnover_accumulates_passed_orders(conn, run_id, limits_row):
    """当日売買代金(G-7 の分子)は通過済み注文の想定代金を含む。"""
    limits_row()
    _gate(conn, run_id)  # 100 × ¥1,000 = ¥100,000
    today = datetime.now(UTC).astimezone(ZoneInfo("Asia/Tokyo")).date()
    assert _daily_turnover(conn, "DEMO_FUND", today) == Decimal(100_000)


# ── 追記オンリー(gate_log・executions)────────────────────────────────────────
def test_gate_log_append_only(conn, run_id, limits_row):
    limits_row()
    _, gate_log_id, _ = _gate(conn, run_id)
    for sql in (
        "UPDATE compliance.gate_log SET verdict = 'pass' WHERE id = %s",
        "DELETE FROM compliance.gate_log WHERE id = %s",
    ):
        with pytest.raises(psycopg.errors.RaiseException, match="追記オンリー"):
            with conn.transaction():
                with conn.cursor() as cur:
                    cur.execute(sql, (gate_log_id,))


def test_executions_append_only(conn, run_id, limits_row):
    limits_row()
    order_id, _, _ = _gate(conn, run_id)
    advance_order_status(conn, order_id, "submitted")
    execution_id = record_execution(
        conn, order_id=order_id, qty=Decimal(100), price=Decimal(1000),
        executed_at=datetime.now(UTC), run_id=run_id,
    )
    for sql in (
        "UPDATE trading.executions SET price = 1 WHERE id = %s",
        "DELETE FROM trading.executions WHERE id = %s",
    ):
        with pytest.raises(psycopg.errors.RaiseException, match="追記オンリー"):
            with conn.transaction():
                with conn.cursor() as cur:
                    cur.execute(sql, (execution_id,))


# ── 受け入れ基準2の構造的裏付け: gate_log_id は NOT NULL ─────────────────────
def test_orders_require_gate_log(conn, run_id):
    """ゲート判定を経ない orders 行はスキーマ上つくれない(gate_log_id NOT NULL)。"""
    with pytest.raises(psycopg.errors.NotNullViolation):
        with conn.transaction():
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO trading.orders
                        (book_id, fm, instrument_id, side, qty, order_type,
                         status, gate_log_id, run_id)
                    VALUES ('DEMO_FUND', 'ben', 1, 'buy', 100, 'market',
                            'passed', NULL, %s)
                    """,
                    (run_id,),
                )


# ── 状態遷移の強制 ────────────────────────────────────────────────────────────
def test_status_transitions(conn, run_id, limits_row):
    limits_row()
    order_id, _, _ = _gate(conn, run_id)
    assert advance_order_status(conn, order_id, "submitted") == "passed"
    assert advance_order_status(conn, order_id, "filled") == "submitted"
    with pytest.raises(OrderStatusError):
        advance_order_status(conn, order_id, "submitted")  # filled は端状態


def test_blocked_is_terminal(conn, run_id, limits_row):
    limits_row()
    order_id, _, result = _gate(conn, run_id, jp_stock_proposal(side="short"))
    assert result.verdict == "block"
    for target in ("submitted", "passed", "filled"):
        with pytest.raises(OrderStatusError):
            advance_order_status(conn, order_id, target)


def test_invalid_transition_passed_to_filled(conn, run_id, limits_row):
    limits_row()
    order_id, _, _ = _gate(conn, run_id)
    with pytest.raises(OrderStatusError):
        advance_order_status(conn, order_id, "filled")  # submitted を飛ばせない


def test_record_execution_requires_submitted(conn, run_id, limits_row):
    limits_row()
    order_id, _, _ = _gate(conn, run_id)  # passed のまま
    with pytest.raises(OrderStatusError):
        record_execution(
            conn, order_id=order_id, qty=Decimal(100), price=Decimal(1000),
            executed_at=datetime.now(UTC), run_id=run_id,
        )


# ── apply_execution: 移動平均・クローズ・冪等 ────────────────────────────────
def _submitted_order(conn, run_id, proposal):
    order_id, _, result = _gate(conn, run_id, proposal)
    assert result.verdict in ("pass", "warn"), result.reasons
    advance_order_status(conn, order_id, "submitted")
    return order_id


def test_apply_execution_moving_average_and_close(conn, run_id, limits_row):
    limits_row()
    # 買い 100@1000 → 買い増し 100@1200 → 平均 1100。
    o1 = _submitted_order(conn, run_id, jp_stock_proposal())
    record_execution(conn, order_id=o1, qty=Decimal(100), price=Decimal(1000),
                     executed_at=datetime.now(UTC), run_id=run_id)
    assert _position(conn) == (Decimal(100), Decimal(1000), "equity_jp")

    o2 = _submitted_order(conn, run_id, jp_stock_proposal(ref_price=Decimal(1200)))
    record_execution(conn, order_id=o2, qty=Decimal(100), price=Decimal(1200),
                     executed_at=datetime.now(UTC), run_id=run_id)
    qty, avg, _ = _position(conn)
    assert (qty, avg) == (Decimal(200), Decimal(1100))

    # 部分クローズ: 残玉の単価は不変。
    o3 = _submitted_order(
        conn, run_id, jp_stock_proposal(side="sell", qty=Decimal(50), ref_price=Decimal(1300))
    )
    record_execution(conn, order_id=o3, qty=Decimal(50), price=Decimal(1300),
                     executed_at=datetime.now(UTC), run_id=run_id)
    qty, avg, _ = _position(conn)
    assert (qty, avg) == (Decimal(150), Decimal(1100))

    # 全クローズ: qty 0・avg_cost 0。
    o4 = _submitted_order(
        conn, run_id, jp_stock_proposal(side="sell", qty=Decimal(150), ref_price=Decimal(1300))
    )
    record_execution(conn, order_id=o4, qty=Decimal(150), price=Decimal(1300),
                     executed_at=datetime.now(UTC), run_id=run_id)
    qty, avg, _ = _position(conn)
    assert (qty, avg) == (Decimal(0), Decimal(0))


def test_apply_execution_idempotent(conn, run_id, limits_row):
    limits_row()
    order_id = _submitted_order(conn, run_id, jp_stock_proposal())
    execution_id = record_execution(
        conn, order_id=order_id, qty=Decimal(100), price=Decimal(1000),
        executed_at=datetime.now(UTC), run_id=run_id,
    )
    assert _position(conn)[0] == Decimal(100)
    # 再適用は無視される(冪等)。
    assert apply_execution(conn, execution_id, run_id=run_id) is False
    assert _position(conn)[0] == Decimal(100)
