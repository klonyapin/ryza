"""記帳 API。

- post_entry: 汎用の複式仕訳記帳(貸借一致・証憑必須・OPS 費用のタグ必須を検証)
- reverse_entry: 逆仕訳(訂正)
- post_fill: 約定の記帳(現物買い/売り・手数料。実現損益は移動平均法)
- post_mark_to_market: 評価替え(未実現損益の洗い替え)
- post_ops_cost: 運営費用(GCP/LLM 等)の記帳

すべての関数は psycopg 接続 `conn` を第1引数に取り、呼び出し側がコミットを制御する。
すべての書き込みは run_id を持つ(不変原則3・受け入れ基準)。
"""

from __future__ import annotations

from datetime import date as _date
from decimal import Decimal
from typing import Any

import psycopg

from ryza.ledger import _util

# post_ops_cost の category -> 勘定科目 ID / 証憑 kind
_OPS_COST_ACCOUNTS = {
    "gcp": "gcp_cost",
    "llm_fable": "llm_cost_fable",
    "llm_mid": "llm_cost_mid",
    "llm_light": "llm_cost_light",
    "data": "data_cost",
    "broker": "broker_fee",
    "misc": "misc",
}
_OPS_COST_EVIDENCE_KIND = {
    "gcp": "gcp_billing",
    "llm_fable": "llm_usage",
    "llm_mid": "llm_usage",
    "llm_light": "llm_usage",
    "data": "invoice",
    "broker": "invoice",
    "misc": "invoice",
}


def post_entry(
    conn: psycopg.Connection,
    *,
    book_id: str,
    entry_date: _date,
    description: str,
    lines: list[dict[str, Any]],
    evidence: int | dict | None,
    run_id: int,
    posted_by: str = "ledger.posting",
    reversal_of: int | None = None,
) -> int:
    """複式仕訳を記帳し entry_id を返す。

    lines: [{account_id, debit|credit, currency, instrument_id?, strategy_tag?, dept_tag?}]
    検証:
      - lines が空、または貸借不一致(Σdebit != Σcredit)なら ValueError
      - evidence が None なら ValueError(証憑必須)
      - OPS 帳簿の費用行(category='expense')に strategy_tag も dept_tag も無ければ ValueError
    """
    if not lines:
        raise ValueError("lines が空です")

    evidence_id = _util.resolve_evidence(conn, evidence)

    bt = _util.book_type(conn, book_id)
    meta = _util.account_meta(conn, book_id)

    total_debit = Decimal(0)
    total_credit = Decimal(0)
    norm_lines: list[dict[str, Any]] = []
    for raw in lines:
        account_id = raw["account_id"]
        if account_id not in meta:
            raise ValueError(f"未知の勘定科目: {book_id}.{account_id}")
        debit = _util.to_decimal(raw.get("debit", 0) or 0)
        credit = _util.to_decimal(raw.get("credit", 0) or 0)
        if debit < 0 or credit < 0:
            raise ValueError(f"金額は非負: {account_id} debit={debit} credit={credit}")
        if debit != 0 and credit != 0:
            raise ValueError(f"1 行に借方・貸方の両方は不可: {account_id}")
        strategy_tag = raw.get("strategy_tag")
        dept_tag = raw.get("dept_tag")

        # OPS 帳簿の費用行は E4 配賦のため strategy_tag か dept_tag が必須。
        if bt == "ops" and meta[account_id]["category"] == "expense":
            if not strategy_tag and not dept_tag:
                raise ValueError(
                    f"OPS 費用行 {account_id} には strategy_tag か dept_tag が必須(E4 配賦)"
                )

        total_debit += debit
        total_credit += credit
        norm_lines.append(
            {
                "account_id": account_id,
                "debit": debit,
                "credit": credit,
                "currency": raw.get("currency", "JPY"),
                "instrument_id": raw.get("instrument_id"),
                "strategy_tag": strategy_tag,
                "dept_tag": dept_tag,
            }
        )

    if total_debit != total_credit:
        raise ValueError(f"貸借不一致: debit={total_debit} credit={total_credit}")

    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO ledger.journal_entries
                (book_id, entry_date, description, evidence_id, posted_by, reversal_of, run_id)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            RETURNING entry_id
            """,
            (book_id, entry_date, description, evidence_id, posted_by, reversal_of, run_id),
        )
        entry_id = cur.fetchone()[0]
        for i, ln in enumerate(norm_lines, start=1):
            cur.execute(
                """
                INSERT INTO ledger.journal_lines
                    (entry_id, line_no, book_id, account_id, debit, credit, currency,
                     instrument_id, strategy_tag, dept_tag)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    entry_id,
                    i,
                    book_id,
                    ln["account_id"],
                    ln["debit"],
                    ln["credit"],
                    ln["currency"],
                    ln["instrument_id"],
                    ln["strategy_tag"],
                    ln["dept_tag"],
                ),
            )
    return entry_id


def reverse_entry(
    conn: psycopg.Connection,
    *,
    entry_id: int,
    reason: str,
    run_id: int,
    entry_date: _date | None = None,
    posted_by: str = "ledger.posting",
) -> int:
    """entry_id の逆仕訳を生成し、新しい entry_id を返す。借方・貸方を入れ替える。"""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT book_id, entry_date FROM ledger.journal_entries WHERE entry_id = %s",
            (entry_id,),
        )
        head = cur.fetchone()
        if head is None:
            raise ValueError(f"逆仕訳対象が存在しない: entry_id={entry_id}")
        book_id, orig_date = head
        cur.execute(
            """
            SELECT account_id, debit, credit, currency, instrument_id, strategy_tag, dept_tag
            FROM ledger.journal_lines WHERE entry_id = %s ORDER BY line_no
            """,
            (entry_id,),
        )
        orig_lines = cur.fetchall()

    reversed_lines = [
        {
            "account_id": r[0],
            "debit": r[2],  # 元 credit -> debit
            "credit": r[1],  # 元 debit -> credit
            "currency": r[3],
            "instrument_id": r[4],
            "strategy_tag": r[5],
            "dept_tag": r[6],
        }
        for r in orig_lines
    ]

    evidence = {
        "kind": "decision",
        "payload": {"reversal_of": entry_id, "reason": reason},
        "source": "ledger.reverse_entry",
    }
    return post_entry(
        conn,
        book_id=book_id,
        entry_date=entry_date or orig_date,
        description=f"逆仕訳: {reason}(元 entry {entry_id})",
        lines=reversed_lines,
        evidence=evidence,
        run_id=run_id,
        posted_by=posted_by,
        reversal_of=entry_id,
    )


def post_fill(
    conn: psycopg.Connection,
    *,
    book_id: str,
    instrument_id: int,
    side: str,
    qty: Any,
    price: Any,
    entry_date: _date,
    run_id: int,
    fee: Any = 0,
    currency: str = "JPY",
    fill_id: int | None = None,
    source: str = "broker",
    posted_by: str = "ledger.posting",
) -> int:
    """約定を記帳する。現物買い/売り + 手数料。売りの実現損益は移動平均法。

    - buy:  Dr securities(qty*price)/ Dr commission(fee)/ Cr cash(qty*price+fee)
    - sell: Dr cash(gross-fee)/ Dr commission(fee)/ Cr securities(平均原価×qty)
            差額を実現損益(realized_pnl)に計上
    証憑は kind='broker_fill'、約定内容(instrument/side/qty/price/fee)を payload に格納し、
    ポジション再生(移動平均法)の元データになる。
    """
    if side not in ("buy", "sell"):
        raise ValueError(f"side は buy|sell: {side}")
    q = _util.to_decimal(qty)
    p = _util.to_decimal(price)
    f = _util.to_decimal(fee)
    if q <= 0:
        raise ValueError(f"qty は正: {q}")
    gross = q * p

    evidence = _util.create_evidence(
        conn,
        kind="broker_fill",
        payload={
            "fill_id": fill_id,
            "instrument_id": int(instrument_id),
            "side": side,
            "qty": str(q),
            "price": str(p),
            "fee": str(f),
            "currency": currency,
        },
        source=source,
    )

    lines: list[dict[str, Any]] = []
    if side == "buy":
        lines.append(
            {"account_id": "securities", "debit": gross, "currency": currency,
             "instrument_id": int(instrument_id)}
        )
        if f > 0:
            lines.append({"account_id": "commission", "debit": f, "currency": currency})
        lines.append({"account_id": "cash", "credit": gross + f, "currency": currency})
        desc = f"買約定 銘柄{instrument_id} {q}@{p}"
    else:
        held_qty, cost = _util.replay_position(conn, book_id, instrument_id)
        if q > held_qty:
            raise ValueError(
                f"売り数量が保有を超過: sell={q} held={held_qty}(銘柄{instrument_id})"
            )
        cost_released = cost * q / held_qty if held_qty > 0 else Decimal(0)
        realized = gross - cost_released  # 正=実現益

        lines.append({"account_id": "cash", "debit": gross - f, "currency": currency})
        if f > 0:
            lines.append({"account_id": "commission", "debit": f, "currency": currency})
        lines.append(
            {"account_id": "securities", "credit": cost_released, "currency": currency,
             "instrument_id": int(instrument_id)}
        )
        if realized >= 0:
            lines.append({"account_id": "realized_pnl", "credit": realized, "currency": currency})
        else:
            lines.append({"account_id": "realized_pnl", "debit": -realized, "currency": currency})
        desc = f"売約定 銘柄{instrument_id} {q}@{p} 実現損益={realized}"

    return post_entry(
        conn,
        book_id=book_id,
        entry_date=entry_date,
        description=desc,
        lines=lines,
        evidence=evidence,
        run_id=run_id,
        posted_by=posted_by,
    )


def post_mark_to_market(
    conn: psycopg.Connection,
    *,
    book_id: str,
    instrument_id: int,
    price: Any,
    entry_date: _date,
    run_id: int,
    currency: str = "JPY",
    posted_by: str = "ledger.posting",
) -> int | None:
    """評価替え(未実現損益の洗い替え)。securities 帳簿価額を時価に一致させる。

    delta = 時価総額(保有数量×price) − 現在の securities 帳簿価額。
    delta>0: Dr securities / Cr unrealized_pnl、delta<0: 逆。
    差分計上のため unrealized_pnl の累計は常に (時価 − 取得原価) に一致する(洗い替えと等価)。
    delta=0(または保有ゼロで時価ゼロ)なら記帳せず None を返す。
    """
    qty, _cost = _util.replay_position(conn, book_id, instrument_id)
    p = _util.to_decimal(price)
    market_value = qty * p
    book_value = _util.securities_book_value(conn, book_id, instrument_id, as_of=entry_date)
    delta = market_value - book_value
    if delta == 0:
        return None

    evidence = _util.create_evidence(
        conn,
        kind="price_snapshot",
        payload={
            "instrument_id": int(instrument_id),
            "price": str(p),
            "qty": str(qty),
            "market_value": str(market_value),
            "as_of": entry_date.isoformat(),
        },
        source="price_source",
    )

    if delta > 0:
        lines = [
            {"account_id": "securities", "debit": delta, "currency": currency,
             "instrument_id": int(instrument_id)},
            {"account_id": "unrealized_pnl", "credit": delta, "currency": currency},
        ]
    else:
        lines = [
            {"account_id": "unrealized_pnl", "debit": -delta, "currency": currency},
            {"account_id": "securities", "credit": -delta, "currency": currency,
             "instrument_id": int(instrument_id)},
        ]

    return post_entry(
        conn,
        book_id=book_id,
        entry_date=entry_date,
        description=f"評価替え 銘柄{instrument_id} 時価{market_value}",
        lines=lines,
        evidence=evidence,
        run_id=run_id,
        posted_by=posted_by,
    )


def post_ops_cost(
    conn: psycopg.Connection,
    *,
    category: str,
    amount: Any,
    entry_date: _date,
    run_id: int,
    strategy_tag: str | None = None,
    dept_tag: str | None = None,
    credit_account: str = "payable",
    currency: str = "JPY",
    description: str | None = None,
    source: str = "billing",
    posted_by: str = "ledger.posting",
) -> int:
    """運営帳簿(OPS)の費用を記帳する。GCP/LLM 費用など。

    Dr <費用勘定>(strategy_tag/dept_tag 付き)/ Cr <credit_account>。
    費用行のタグ必須は post_entry が検証する。
    """
    amt = _util.to_decimal(amount)
    if amt <= 0:
        raise ValueError(f"amount は正: {amt}")
    account_id = _OPS_COST_ACCOUNTS.get(category)
    if account_id is None:
        raise ValueError(f"未知の費用カテゴリ: {category}")
    ev_kind = _OPS_COST_EVIDENCE_KIND.get(category, "invoice")

    evidence = _util.create_evidence(
        conn,
        kind=ev_kind,
        payload={
            "category": category,
            "amount": str(amt),
            "strategy_tag": strategy_tag,
            "dept_tag": dept_tag,
            "as_of": entry_date.isoformat(),
        },
        source=source,
    )

    lines = [
        {"account_id": account_id, "debit": amt, "currency": currency,
         "strategy_tag": strategy_tag, "dept_tag": dept_tag},
        {"account_id": credit_account, "credit": amt, "currency": currency},
    ]
    return post_entry(
        conn,
        book_id="OPS",
        entry_date=entry_date,
        description=description or f"運営費用 {category} {amt}",
        lines=lines,
        evidence=evidence,
        run_id=run_id,
        posted_by=posted_by,
    )
