"""記帳 API(posting.py)の検証。

受け入れ基準:
- 貸借不一致・証憑なし・OPS 費用のタグなしが例外になる
- 買い→値上がり→一部売却で実現損益(移動平均法)・未実現損益が手計算と一致
- 逆仕訳後の試算表が元に戻る
- すべての書き込みが run_id を持つ
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from ryza.ledger import posting, statements

D = Decimal
DAY = date(2026, 8, 3)


def _balance(conn, book_id, account_id, as_of=DAY, instrument_id=None):
    """勘定科目の残高(debit-credit)を返す。"""
    sql = """
        SELECT COALESCE(sum(jl.debit - jl.credit), 0)
        FROM ledger.journal_lines jl
        JOIN ledger.journal_entries je ON je.entry_id = jl.entry_id
        WHERE jl.book_id = %s AND jl.account_id = %s AND je.entry_date <= %s
    """
    params = [book_id, account_id, as_of]
    if instrument_id is not None:
        sql += " AND jl.instrument_id = %s"
        params.append(instrument_id)
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchone()[0]


# ── 例外系 ─────────────────────────────────────────────────────────────────
def test_unbalanced_entry_raises(conn, run_id):
    with pytest.raises(ValueError, match="貸借不一致"):
        posting.post_entry(
            conn,
            book_id="DEMO_FUND",
            entry_date=DAY,
            description="不均衡",
            lines=[
                {"account_id": "cash", "debit": 1000, "currency": "JPY"},
                {"account_id": "capital", "credit": 900, "currency": "JPY"},
            ],
            evidence={"kind": "decision", "payload": {"t": 1}, "source": "test"},
            run_id=run_id,
        )


def test_missing_evidence_raises(conn, run_id):
    with pytest.raises(ValueError, match="証憑"):
        posting.post_entry(
            conn,
            book_id="DEMO_FUND",
            entry_date=DAY,
            description="証憑なし",
            lines=[
                {"account_id": "cash", "debit": 1000, "currency": "JPY"},
                {"account_id": "capital", "credit": 1000, "currency": "JPY"},
            ],
            evidence=None,
            run_id=run_id,
        )


def test_ops_expense_without_tag_raises(conn, run_id):
    with pytest.raises(ValueError, match="strategy_tag"):
        posting.post_ops_cost(
            conn,
            category="gcp",
            amount=1200,
            entry_date=DAY,
            run_id=run_id,
        )


def test_ops_expense_with_dept_tag_ok(conn, run_id):
    entry_id = posting.post_ops_cost(
        conn,
        category="llm_mid",
        amount=800,
        entry_date=DAY,
        dept_tag="research",
        run_id=run_id,
    )
    assert entry_id > 0
    assert _balance(conn, "OPS", "llm_cost_mid") == 800


# ── 移動平均法: 買い→値上がり→一部売却 ─────────────────────────────────────
def test_moving_average_realized_and_unrealized(conn, run_id):
    iid = 1001
    # 買い 100 @ 500(手数料0)。平均原価 500。
    posting.post_fill(conn, book_id="DEMO_FUND", instrument_id=iid, side="buy",
                      qty=100, price=500, entry_date=DAY, run_id=run_id)
    # 値上がり: 600 で評価替え → 未実現 = 100*(600-500) = 10000
    posting.post_mark_to_market(conn, book_id="DEMO_FUND", instrument_id=iid,
                                price=600, entry_date=DAY, run_id=run_id)
    assert _balance(conn, "DEMO_FUND", "unrealized_pnl") == D(-10000)  # 収益は貸方=負のborrow

    # 一部売却 40 @ 620(手数料0)。実現損益 = 40*(620-500) = 4800(移動平均法)。
    posting.post_fill(conn, book_id="DEMO_FUND", instrument_id=iid, side="sell",
                      qty=40, price=620, entry_date=DAY, run_id=run_id)
    assert _balance(conn, "DEMO_FUND", "realized_pnl") == D(-4800)  # 実現益は貸方

    # 売却後に再評価 620 → 残 60 の未実現 = 60*(620-500) = 7200
    posting.post_mark_to_market(conn, book_id="DEMO_FUND", instrument_id=iid,
                                price=620, entry_date=DAY, run_id=run_id)
    assert _balance(conn, "DEMO_FUND", "unrealized_pnl") == D(-7200)
    # securities の帳簿価額 = 残 60 の時価 = 60*620 = 37200
    assert _balance(conn, "DEMO_FUND", "securities", instrument_id=iid) == D(37200)


def test_oversell_raises(conn, run_id):
    iid = 1002
    posting.post_fill(conn, book_id="DEMO_FUND", instrument_id=iid, side="buy",
                      qty=10, price=100, entry_date=DAY, run_id=run_id)
    with pytest.raises(ValueError, match="超過"):
        posting.post_fill(conn, book_id="DEMO_FUND", instrument_id=iid, side="sell",
                          qty=20, price=100, entry_date=DAY, run_id=run_id)


# ── 逆仕訳: 試算表が元に戻る ─────────────────────────────────────────────────
def _account_balances(conn, book_id):
    """勘定科目 -> balance(debit-credit) のマップ(試算表から)。"""
    tb = statements.trial_balance(conn, book_id, DAY)
    non_total = tb[tb["account_id"] != "_TOTAL"]
    return {r.account_id: r.balance for r in non_total.itertuples()}


def test_reverse_entry_restores_trial_balance(conn, run_id):
    before = _account_balances(conn, "DEMO_FUND")

    entry_id = posting.post_fill(
        conn, book_id="DEMO_FUND", instrument_id=1003, side="buy",
        qty=5, price=200, fee=3, entry_date=DAY, run_id=run_id,
    )
    after_post = _account_balances(conn, "DEMO_FUND")
    assert after_post != before  # 記帳で残高が動く

    posting.reverse_entry(conn, entry_id=entry_id, reason="誤記帳", run_id=run_id)
    after_reverse = _account_balances(conn, "DEMO_FUND")

    # 逆仕訳後、この test が触れた各勘定の純残高は元に戻る(相殺)。
    for acct in ("cash", "securities", "commission"):
        assert after_reverse.get(acct, D(0)) == before.get(acct, D(0)), acct


# ── run_id: すべての書き込みが run_id を持つ ───────────────────────────────
def test_all_writes_have_run_id(conn, run_id):
    posting.post_fill(conn, book_id="DEMO_FUND", instrument_id=1004, side="buy",
                      qty=3, price=100, entry_date=DAY, run_id=run_id)
    with conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM ledger.journal_entries WHERE run_id = %s", (run_id,)
        )
        assert cur.fetchone()[0] >= 1
        # run_id を持たない(NULL)エントリは存在し得ない(NOT NULL 制約)。
        cur.execute("SELECT count(*) FROM ledger.journal_entries WHERE run_id IS NULL")
        assert cur.fetchone()[0] == 0
