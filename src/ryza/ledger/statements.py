"""財務諸表の生成。試算表・BS・PL・CF。

出力は pandas.DataFrame(スキーマ固定)。すべて journal_lines の集計。
基準通貨は JPY(設計書 §9)。外貨建て明細は fx_rates で期末レート換算し、換算差損益を
PL の独立行(fx_translation)に計上する(既定は全 JPY = 換算差ゼロ)。
"""

from __future__ import annotations

from datetime import date as _date
from decimal import Decimal
from typing import Any

import pandas as pd
import psycopg

from ryza.ledger import _util

# BS/PL の並び順
_CATEGORY_ORDER = {"asset": 0, "liability": 1, "equity": 2, "income": 3, "expense": 4}


def _account_aggregates(
    conn: psycopg.Connection,
    book_id: str,
    *,
    as_of: _date | None = None,
    period: tuple[_date, _date] | None = None,
    fx_rates: dict[str, Any] | None = None,
) -> dict[str, dict[str, Any]]:
    """勘定科目別に debit/credit 合計を集計する。

    fx_rates(currency->基準通貨レート)が与えられれば通貨別に換算する。
    戻り値: account_id -> {name, category, debit, credit}
    """
    meta = _util.account_meta(conn, book_id)
    sql = """
        SELECT jl.account_id, jl.currency, sum(jl.debit) AS d, sum(jl.credit) AS c
        FROM ledger.journal_lines jl
        JOIN ledger.journal_entries je ON je.entry_id = jl.entry_id
        WHERE jl.book_id = %s
    """
    params: list[Any] = [book_id]
    if as_of is not None:
        sql += " AND je.entry_date <= %s"
        params.append(as_of)
    if period is not None:
        sql += " AND je.entry_date BETWEEN %s AND %s"
        params.extend([period[0], period[1]])
    sql += " GROUP BY jl.account_id, jl.currency"

    agg: dict[str, dict[str, Any]] = {}
    with conn.cursor() as cur:
        cur.execute(sql, params)
        for account_id, currency, d, c in cur.fetchall():
            rate = Decimal(1)
            if fx_rates and currency in fx_rates:
                rate = _util.to_decimal(fx_rates[currency])
            rec = agg.setdefault(
                account_id,
                {
                    "name": meta.get(account_id, {}).get("name", account_id),
                    "category": meta.get(account_id, {}).get("category", "asset"),
                    "debit": Decimal(0),
                    "credit": Decimal(0),
                },
            )
            rec["debit"] += _util.to_decimal(d) * rate
            rec["credit"] += _util.to_decimal(c) * rate
    return agg


def trial_balance(
    conn: psycopg.Connection,
    book_id: str,
    as_of_date: _date,
    *,
    fx_rates: dict[str, Any] | None = None,
) -> pd.DataFrame:
    """試算表。account_id, name, category, debit, credit, balance(=debit-credit)。

    最終行に合計(account_id='_TOTAL')を付す。貸借一致なら total balance は 0。
    """
    agg = _account_aggregates(conn, book_id, as_of=as_of_date, fx_rates=fx_rates)
    rows = []
    for account_id, rec in agg.items():
        rows.append(
            {
                "account_id": account_id,
                "name": rec["name"],
                "category": rec["category"],
                "debit": rec["debit"],
                "credit": rec["credit"],
                "balance": rec["debit"] - rec["credit"],
            }
        )
    rows.sort(key=lambda r: (_CATEGORY_ORDER.get(r["category"], 9), r["account_id"]))
    total = {
        "account_id": "_TOTAL",
        "name": "合計",
        "category": "",
        "debit": sum((r["debit"] for r in rows), Decimal(0)),
        "credit": sum((r["credit"] for r in rows), Decimal(0)),
        "balance": sum((r["balance"] for r in rows), Decimal(0)),
    }
    rows.append(total)
    return pd.DataFrame(
        rows, columns=["account_id", "name", "category", "debit", "credit", "balance"]
    )


def book_totals(
    conn: psycopg.Connection,
    book_id: str,
    as_of_date: _date,
    *,
    fx_rates: dict[str, Any] | None = None,
) -> dict[str, Decimal]:
    """as_of 時点の {assets, liabilities, equity, income, expense, net_income, nav}。

    NAV = 資産 − 負債。net_income = 収益 − 費用(未クローズ)。
    """
    agg = _account_aggregates(conn, book_id, as_of=as_of_date, fx_rates=fx_rates)
    assets = liabilities = equity = income = expense = Decimal(0)
    for rec in agg.values():
        bal = rec["debit"] - rec["credit"]
        cat = rec["category"]
        if cat == "asset":
            assets += bal
        elif cat == "liability":
            liabilities += -bal
        elif cat == "equity":
            equity += -bal
        elif cat == "income":
            income += -bal
        elif cat == "expense":
            expense += bal
    net_income = income - expense
    return {
        "assets": assets,
        "liabilities": liabilities,
        "equity": equity,
        "income": income,
        "expense": expense,
        "net_income": net_income,
        "nav": assets - liabilities,
    }


def balance_sheet(
    conn: psycopg.Connection,
    book_id: str,
    as_of_date: _date,
    *,
    fx_rates: dict[str, Any] | None = None,
) -> pd.DataFrame:
    """貸借対照表。section(asset|liability|equity), account_id, name, amount。

    未クローズの純損益を equity セクションに current_earnings 行として合成する。
    各セクションに小計行(account_id='_SUBTOTAL')を付す。
    資産合計 = 負債合計 + 資本合計(current_earnings 含む)が成り立つ。
    """
    agg = _account_aggregates(conn, book_id, as_of=as_of_date, fx_rates=fx_rates)
    totals = book_totals(conn, book_id, as_of_date, fx_rates=fx_rates)

    rows: list[dict[str, Any]] = []
    for section, sign in (("asset", 1), ("liability", -1), ("equity", -1)):
        items = [
            (aid, rec) for aid, rec in agg.items() if rec["category"] == section
        ]
        items.sort(key=lambda t: t[0])
        for aid, rec in items:
            amount = (rec["debit"] - rec["credit"]) * sign
            if amount == 0:
                continue
            rows.append(
                {"section": section, "account_id": aid, "name": rec["name"], "amount": amount}
            )
        if section == "equity":
            rows.append(
                {
                    "section": "equity",
                    "account_id": "current_earnings",
                    "name": "当期純損益(未クローズ)",
                    "amount": totals["net_income"],
                }
            )
        subtotal = {
            "asset": totals["assets"],
            "liability": totals["liabilities"],
            "equity": totals["equity"] + totals["net_income"],
        }[section]
        rows.append(
            {"section": section, "account_id": "_SUBTOTAL", "name": f"{section} 合計",
             "amount": subtotal}
        )
    return pd.DataFrame(rows, columns=["section", "account_id", "name", "amount"])


def income_statement(
    conn: psycopg.Connection,
    book_id: str,
    period: tuple[_date, _date],
    *,
    fx_rates: dict[str, Any] | None = None,
) -> pd.DataFrame:
    """損益計算書。section(income|expense|summary), account_id, name, amount。

    収益は credit-debit、費用は debit-credit(いずれも正が通常残高)。
    最終行に純損益(account_id='net_income')。
    """
    agg = _account_aggregates(conn, book_id, period=period, fx_rates=fx_rates)
    rows: list[dict[str, Any]] = []
    income_total = expense_total = Decimal(0)
    for section, sign in (("income", -1), ("expense", 1)):
        items = [(aid, rec) for aid, rec in agg.items() if rec["category"] == section]
        items.sort(key=lambda t: t[0])
        for aid, rec in items:
            amount = (rec["debit"] - rec["credit"]) * sign
            if amount == 0:
                continue
            rows.append(
                {"section": section, "account_id": aid, "name": rec["name"], "amount": amount}
            )
            if section == "income":
                income_total += amount
            else:
                expense_total += amount
    rows.append(
        {"section": "summary", "account_id": "net_income", "name": "純損益",
         "amount": income_total - expense_total}
    )
    return pd.DataFrame(rows, columns=["section", "account_id", "name", "amount"])


def cash_flow(
    conn: psycopg.Connection,
    book_id: str,
    period: tuple[_date, _date],
) -> pd.DataFrame:
    """キャッシュフロー計算書。section, account_id, name, amount。

    現金勘定(fund=cash / ops=cash_bank)を含む仕訳の相手科目別に現金増減を集計する。
    - OPS: 直接法(相手科目別に operating へ集計)
    - ファンド: 相手科目を investing(securities)/ financing(capital 等)/ operating に区分
    最終行に純増減(account_id='net_change')。
    """
    bt = _util.book_type(conn, book_id)
    cash = _util.cash_account(bt)
    meta = _util.account_meta(conn, book_id)

    with conn.cursor() as cur:
        cur.execute(
            """
            WITH cash_entries AS (
                SELECT DISTINCT je.entry_id
                FROM ledger.journal_entries je
                JOIN ledger.journal_lines jl ON jl.entry_id = je.entry_id
                WHERE jl.book_id = %s AND jl.account_id = %s
                  AND je.entry_date BETWEEN %s AND %s
            )
            SELECT jl.account_id, sum(jl.credit - jl.debit) AS cash_flow
            FROM ledger.journal_lines jl
            WHERE jl.entry_id IN (SELECT entry_id FROM cash_entries)
              AND jl.account_id <> %s
            GROUP BY jl.account_id
            """,
            (book_id, cash, period[0], period[1], cash),
        )
        counterparts = cur.fetchall()

    financing_accounts = {"capital", "borrowings", "short_positions", "margin_deposit",
                          "owner_capital"}
    investing_accounts = {"securities", "receivable_unsettled"}

    def classify(account_id: str) -> str:
        if bt == "ops":
            return "operating"
        if account_id in investing_accounts:
            return "investing"
        if account_id in financing_accounts:
            return "financing"
        return "operating"

    rows: list[dict[str, Any]] = []
    net = Decimal(0)
    for account_id, cf in sorted(counterparts, key=lambda t: t[0]):
        amount = _util.to_decimal(cf)
        if amount == 0:
            continue
        net += amount
        rows.append(
            {
                "section": classify(account_id),
                "account_id": account_id,
                "name": meta.get(account_id, {}).get("name", account_id),
                "amount": amount,
            }
        )
    rows.sort(key=lambda r: ({"operating": 0, "investing": 1, "financing": 2}.get(r["section"], 9),
                             r["account_id"]))
    rows.append(
        {"section": "summary", "account_id": "net_change", "name": "現金純増減", "amount": net}
    )
    return pd.DataFrame(rows, columns=["section", "account_id", "name", "amount"])
