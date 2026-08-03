"""執行ループ(runner)の受け入れテスト。

ゲート通過(gate_and_record)→ 執行 → record_execution → ledger 記帳 → 状態遷移の
E2E と、原子性(仕訳失敗時に executions が残らない)・帳簿分離を検証する。
"""

from __future__ import annotations

from decimal import Decimal

from ryza.execution.config import ExecutionConfig
from ryza.execution.demo import DemoBroker
from ryza.execution.runner import run_pending

from .conftest import make_test_config


def _broker(conn, today) -> DemoBroker:
    return DemoBroker(conn, config=make_test_config(), trade_date=today)


def _order_status(conn, order_id):
    with conn.cursor() as cur:
        cur.execute("SELECT status FROM trading.orders WHERE id = %s", (order_id,))
        return cur.fetchone()[0]


def _executions(conn, order_id):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT qty, price, fee, venue FROM trading.executions WHERE order_id = %s",
            (order_id,),
        )
        return cur.fetchall()


def _cash_balance(conn, book_id="DEMO_FUND"):
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT COALESCE(sum(debit - credit), 0) FROM ledger.journal_lines
            WHERE book_id = %s AND account_id = 'cash'
            """,
            (book_id,),
        )
        return Decimal(cur.fetchone()[0])


# ── E2E: ゲート通過 → 約定 → 記帳 → filled ───────────────────────────────────
def test_market_buy_end_to_end(conn, run_id, passed_order, insert_bar, today_jst):
    # 終値 1000・出来高 100 万株 → 参加率 1e-4 → rate = 5+140×0.01 = 6.4bps → 1000.64。
    insert_bar(1, today_jst, close=Decimal(1000), volume=Decimal(1_000_000))
    order_id = passed_order()
    cash_before = _cash_balance(conn)

    summary = run_pending(
        conn, book_id="DEMO_FUND", broker=_broker(conn, today_jst), run_id=run_id
    )

    assert summary["processed"] == 1 and summary["filled"] == 1
    assert summary["errors"] == []
    assert _order_status(conn, order_id) == "filled"

    # trading.executions(record_execution 経由)。
    rows = _executions(conn, order_id)
    assert len(rows) == 1
    qty, price, fee, venue = rows[0]
    assert (Decimal(qty), Decimal(price), Decimal(fee)) == (
        Decimal(100), Decimal("1000.64"), Decimal(0),
    )
    assert venue == "demo"

    # trading.positions(apply_execution 経由・移動平均)。
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT qty, avg_cost FROM trading.positions
            WHERE book_id = 'DEMO_FUND' AND fm = 'ben' AND instrument_id = 1
            """
        )
        pos = cur.fetchone()
    assert (Decimal(pos[0]), Decimal(pos[1])) == (Decimal(100), Decimal("1000.64"))

    # ledger 仕訳(post_fill 経由): 現金が約定代金だけ減り、証憑が execution を指す。
    assert cash_before - _cash_balance(conn) == Decimal("100064.00")
    execution_id = summary["orders"][0]["execution_id"]
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT count(*) FROM ledger.journal_entries je
            JOIN ledger.evidence e ON e.evidence_id = je.evidence_id
            WHERE je.book_id = 'DEMO_FUND' AND e.kind = 'broker_fill'
              AND e.source = 'trading.executions'
              AND e.payload_ref::jsonb ->> 'fill_id' = %s
            """,
            (str(execution_id),),
        )
        assert cur.fetchone()[0] == 1


def test_sell_realizes_pnl(conn, run_id, passed_order, insert_bar, today_jst):
    """買い → 売りの往復で実現損益が ledger に記帳される。"""
    insert_bar(1, today_jst, close=Decimal(1000), volume=Decimal(1_000_000))
    broker = _broker(conn, today_jst)
    buy_id = passed_order()
    run_pending(conn, book_id="DEMO_FUND", broker=broker, run_id=run_id)
    assert _order_status(conn, buy_id) == "filled"

    sell_id = passed_order(side="sell")
    summary = run_pending(conn, book_id="DEMO_FUND", broker=broker, run_id=run_id)
    assert summary["filled"] == 1, summary
    assert _order_status(conn, sell_id) == "filled"
    # 買い 1000.64 → 売り 999.36(1000×(1−6.4bps) 切り捨て): 実現損 128.00(100 株)。
    # realized_pnl 借方に立つ。
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT COALESCE(sum(debit - credit), 0) FROM ledger.journal_lines
            WHERE book_id = 'DEMO_FUND' AND account_id = 'realized_pnl'
            """
        )
        assert Decimal(cur.fetchone()[0]) == Decimal("128.00")


def test_no_bar_rejects_order(conn, run_id, passed_order, today_jst):
    order_id = passed_order(instrument_id=555, ref_price=Decimal(1000))
    summary = run_pending(
        conn, book_id="DEMO_FUND", broker=_broker(conn, today_jst), run_id=run_id
    )
    assert summary["rejected"] == 1
    assert _order_status(conn, order_id) == "rejected"
    assert _executions(conn, order_id) == []


def test_limit_not_touched_cancels(conn, run_id, passed_order, insert_bar, today_jst):
    insert_bar(1, today_jst, close=Decimal(1000), open_=Decimal(1005),
               high=Decimal(1010), low=Decimal(990), volume=Decimal(1_000_000))
    order_id = passed_order(order_type="limit", limit_price=Decimal(985))
    summary = run_pending(
        conn, book_id="DEMO_FUND", broker=_broker(conn, today_jst), run_id=run_id
    )
    assert summary["expired"] == 1
    assert _order_status(conn, order_id) == "cancelled"
    assert _executions(conn, order_id) == []


def test_short_rejected_before_broker(conn, run_id, passed_order, insert_bar, today_jst):
    """short/cover は ledger 未対応のため rejected(ブローカーへ出さない)。"""
    insert_bar(1, today_jst, close=Decimal(1000), volume=Decimal(1_000_000))
    order_id = passed_order()
    # ゲートは Ben の short を block するため、通過済み注文の side を書き換えて
    # 「short が passed になった」状況を作る(orders は追記オンリー対象外)。
    with conn.cursor() as cur:
        cur.execute("UPDATE trading.orders SET side = 'short' WHERE id = %s", (order_id,))
    summary = run_pending(
        conn, book_id="DEMO_FUND", broker=_broker(conn, today_jst), run_id=run_id
    )
    assert summary["rejected"] == 1
    assert "buy/sell のみ" in summary["orders"][0]["reason"]
    assert _order_status(conn, order_id) == "rejected"
    assert _executions(conn, order_id) == []


# ── 原子性: 仕訳失敗時に executions が残らない ──────────────────────────────
def test_posting_failure_rolls_back_execution(
    conn, run_id, passed_order, insert_bar, today_jst
):
    """保有ゼロの売り → post_fill が保有超過で失敗 → execution・状態遷移ごと巻き戻る。"""
    insert_bar(1, today_jst, close=Decimal(1000), volume=Decimal(1_000_000))
    order_id = passed_order()
    with conn.cursor() as cur:  # 買いで通過させた注文を売りに書き換え(保有なし売り)
        cur.execute("UPDATE trading.orders SET side = 'sell' WHERE id = %s", (order_id,))

    summary = run_pending(
        conn, book_id="DEMO_FUND", broker=_broker(conn, today_jst), run_id=run_id
    )
    assert len(summary["errors"]) == 1
    assert "超過" in summary["errors"][0]["error"]
    # 原子性: executions・position_applies・positions・状態遷移が何も残らない。
    assert _executions(conn, order_id) == []
    assert _order_status(conn, order_id) == "passed"  # 次回再試行可能
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM trading.positions WHERE instrument_id = 1")
        assert cur.fetchone()[0] == 0
    # 他の注文には波及しない(失敗許容)。
    ok_order = passed_order(instrument_id=2, ref_price=Decimal(500), qty=Decimal(100))
    insert_bar(2, today_jst, close=Decimal(500), volume=Decimal(1_000_000))
    summary2 = run_pending(
        conn, book_id="DEMO_FUND", broker=_broker(conn, today_jst), run_id=run_id
    )
    assert summary2["filled"] == 1
    assert _order_status(conn, ok_order) == "filled"


# ── 帳簿分離: DEMO_FUND 以外への記帳は勘定科目制約で落ちる ───────────────────
def test_book_separation_ops_posting_fails(
    conn, run_id, passed_order, insert_bar, today_jst
):
    """OPS(運営帳簿)にはファンド勘定(securities 等)が無く、post_fill が失敗する。

    帳簿分離はスキーマ(ledger.accounts の book_id 別定義+journal_lines の整合
    トリガ)が守る — 執行ループが誤って OPS を対象にしても記帳できない。
    """
    insert_bar(1, today_jst, close=Decimal(1000), volume=Decimal(1_000_000))
    order_id = passed_order()
    with conn.cursor() as cur:  # 通過済み注文の帳簿を OPS に書き換えて誤配線を再現
        cur.execute("UPDATE trading.orders SET book_id = 'OPS' WHERE id = %s", (order_id,))

    summary = run_pending(
        conn, book_id="OPS", broker=_broker(conn, today_jst), run_id=run_id
    )
    assert len(summary["errors"]) == 1
    assert "OPS.securities" in summary["errors"][0]["error"]
    assert _executions(conn, order_id) == []
    assert _order_status(conn, order_id) == "passed"


def test_no_orders_noop(conn, run_id, today_jst):
    summary = run_pending(
        conn, book_id="DEMO_FUND", broker=_broker(conn, today_jst), run_id=run_id
    )
    assert summary == {
        "processed": 0, "filled": 0, "rejected": 0, "expired": 0,
        "errors": [], "orders": [],
    }


def test_runner_uses_effective_config(conn, run_id, passed_order, insert_bar, today_jst):
    """発効 config(execution.yaml)でも E2E が通る(equity_jp 手数料ゼロ)。"""
    insert_bar(1, today_jst, close=Decimal(1000), volume=Decimal(1_000_000))
    passed_order()
    broker = DemoBroker(conn, config=ExecutionConfig.load(), trade_date=today_jst)
    summary = run_pending(conn, book_id="DEMO_FUND", broker=broker, run_id=run_id)
    assert summary["filled"] == 1
    assert summary["orders"][0]["fee"] == "0.00"
