"""財務諸表(statements.py)のスキーマ・集計の検証。"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from ryza.ledger import posting, statements

D = Decimal
DAY = date(2026, 8, 3)


def test_trial_balance_schema_and_zero(conn):
    """シードのみ(開始仕訳)の DEMO_FUND 試算表: 列固定・ゼロバランス。"""
    tb = statements.trial_balance(conn, "DEMO_FUND", DAY)
    assert list(tb.columns) == ["account_id", "name", "category", "debit", "credit", "balance"]
    total = tb[tb["account_id"] == "_TOTAL"].iloc[0]
    assert total["balance"] == D(0)


def test_balance_sheet_identity(conn, run_id):
    posting.post_fill(conn, book_id="DEMO_FUND", instrument_id=1001, side="buy",
                      qty=10, price=1000, fee=50, entry_date=DAY, run_id=run_id)
    bs = statements.balance_sheet(conn, "DEMO_FUND", DAY)
    assert list(bs.columns) == ["section", "account_id", "name", "amount"]

    assets = bs[(bs["section"] == "asset") & (bs["account_id"] == "_SUBTOTAL")]["amount"].iloc[0]
    liab = bs[(bs["section"] == "liability") & (bs["account_id"] == "_SUBTOTAL")]["amount"]
    equity = bs[(bs["section"] == "equity") & (bs["account_id"] == "_SUBTOTAL")]["amount"].iloc[0]
    liab_val = liab.iloc[0] if len(liab) else D(0)
    assert assets == liab_val + equity


def test_income_statement_schema(conn, run_id):
    posting.post_ops_cost(conn, category="gcp", amount=500, entry_date=DAY,
                          dept_tag="ops", run_id=run_id)
    pl = statements.income_statement(conn, "OPS", (date(2026, 8, 1), DAY))
    assert list(pl.columns) == ["section", "account_id", "name", "amount"]
    net = pl[pl["account_id"] == "net_income"].iloc[0]
    # 費用のみ 500 → 純損益 = -500
    assert net["amount"] == D(-500)
    gcp = pl[pl["account_id"] == "gcp_cost"].iloc[0]
    assert gcp["amount"] == D(500)


def test_cash_flow_ops_direct_method(conn, run_id):
    # OPS: 現金で費用支払い(Dr misc / Cr cash_bank)。直接法で相手科目別に現金流出。
    posting.post_entry(
        conn, book_id="OPS", entry_date=DAY, description="現金払い費用",
        lines=[
            {"account_id": "misc", "debit": 300, "currency": "JPY", "dept_tag": "ops"},
            {"account_id": "cash_bank", "credit": 300, "currency": "JPY"},
        ],
        evidence={"kind": "invoice", "payload": {"x": 1}, "source": "test"},
        run_id=run_id,
    )
    cf = statements.cash_flow(conn, "OPS", (date(2026, 8, 1), DAY))
    assert list(cf.columns) == ["section", "account_id", "name", "amount"]
    misc = cf[cf["account_id"] == "misc"].iloc[0]
    assert misc["section"] == "operating"
    assert misc["amount"] == D(-300)  # 現金流出
    net = cf[cf["account_id"] == "net_change"].iloc[0]
    assert net["amount"] == D(-300)


def test_cash_flow_fund_investing_financing(conn, run_id):
    # ファンド: securities 購入 = investing の現金流出。
    posting.post_fill(conn, book_id="DEMO_FUND", instrument_id=1005, side="buy",
                      qty=10, price=100, entry_date=DAY, run_id=run_id)
    cf = statements.cash_flow(conn, "DEMO_FUND", (date(2026, 8, 1), DAY))
    sec = cf[cf["account_id"] == "securities"].iloc[0]
    assert sec["section"] == "investing"
    assert sec["amount"] == D(-1000)
    # 開始仕訳(capital)は financing。
    cap = cf[cf["account_id"] == "capital"]
    if len(cap):
        assert cap.iloc[0]["section"] == "financing"
