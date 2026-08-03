"""記帳 API。

- post_entry: 汎用の複式仕訳記帳(貸借一致・証憑必須・OPS 費用のタグ必須を検証)
- reverse_entry: 逆仕訳(訂正)
- post_fill: 約定の記帳(現物買い/売り・手数料。実現損益は移動平均法)
- post_in_kind_contribution: 現物拠出(約定を経ない建玉の受け入れ。数量つき証憑を作る)
- post_mark_to_market: 評価替え(未実現損益の洗い替え。記帳先は評価調整勘定)
- post_ops_cost: 運営費用(GCP/LLM 等)の記帳

すべての関数は psycopg 接続 `conn` を第1引数に取り、呼び出し側がコミットを制御する。
すべての書き込みは run_id を持つ(不変原則3・受け入れ基準)。
"""

from __future__ import annotations

from datetime import date as _date
from decimal import Decimal
from typing import Any

import psycopg

from ryza import org
from ryza.bot import COLOR_FLASH, DISCLAIMER
from ryza.bot.outbox import enqueue
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

    **売りの建玉再生は ``as_of=entry_date`` で切る**(独立審査 新-22)。以前は ``as_of``
    なしの全期間再生で ``cost_released`` を決めていたため、締めの原価恒等式
    (``securities`` 残高(as_of) = ``replay_position`` の取得原価(as_of) — 0034)と
    **日付境界が非対称**になり、**後日付の約定が先に記帳されている日**は健全な帳簿でも
    ``cost_identity_broken`` が鳴った。審査実測: d0 買い 100@500 → d2 買い 100@700 を先に
    記帳 → d1 売り 50@800 で、d1 の締めが
    ``{book_value: 20000, replay_cost: 25000, qty: 50, reason: cost_identity_broken}``
    (d2 の締めでは消える)。毎日 #運営 に流す検査なので、偽陽性の第一の源は先に潰す
    (通知疲れは検出器を殺す)。新-13 が ``post_mark_to_market`` で行った是正と同型である。

    **売却可能性の判定は原価の日付境界とは別物である**(独立審査 再22-1)。原価は
    ``as_of=entry_date`` で切るが、**売れるかどうかは全履歴で見る** — 候補の売りを
    ``entry_date`` の位置に挿入した全期間再生で、running 数量が全時点で 0 以上であることを
    要求する(``_util.worst_running_qty_with_sell``)。``as_of`` の保有数量だけで通すと、
    **後日付の売りが既に記帳されているとき同じ株を二重に払い出せる**(審査実測 P3: 買 d0
    100 → 売 d3 100 を記帳した後の 売 d1 50 が受理され qty=−50 の幻の売建が立つ。
    しかも残高も再生も −25,000 で一致するため**原価恒等式は沈黙する**)。全期間の期末数量
    との AND でも足りない — 買いが後日付で先行していると途中の負区間を見逃す。端点ではなく
    最小値を見ること。

    **これ単独では偽陽性は消えない**: ``replay_position`` の再生順も
    ``(entry_date, entry_id)`` にする必要がある(同関数の docstring)。``entry_id`` 順のまま
    ここだけ ``as_of`` で切ると、上の実測ケースの偽陽性は d1 から**d2 へ移るだけ**である
    (as_of=d2 の再生は d2 の買いを d1 の売りより前に置くので原価 90,000 / 残高 95,000)。
    2 つの是正は対になっている。

    **残る真陽性**: 売りを記帳した**後から**その売りより前の日付の買いを入れると、既記帳の
    ``cost_released`` は当時の平均原価のままなので恒等式は破れる(実測: d0 買い 100@500 →
    d5 売り 50 を記帳 → 後から d1 買い 100@700 を記帳すると 残高 95,000 / 再生 90,000)。
    これは偽陽性ではなく**実現損益が古い平均原価で確定している**という事実であり、名指し
    されるべきものである。
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
            {"account_id": _util.COST_ACCOUNT, "debit": gross, "currency": currency,
             "instrument_id": int(instrument_id)}
        )
        if f > 0:
            lines.append({"account_id": "commission", "debit": f, "currency": currency})
        lines.append({"account_id": "cash", "credit": gross + f, "currency": currency})
        desc = f"買約定 銘柄{instrument_id} {q}@{p}"
    else:
        # 売却可能性は**全履歴**で見る(独立審査 再22-1)。``as_of=entry_date`` の保有数量
        # だけで通すと、後日付の売りが記帳済みのとき同じ株を二重に払い出せる。
        worst_qty, worst_day = _util.worst_running_qty_with_sell(
            _util.position_events(conn, book_id, instrument_id),
            entry_date=entry_date,
            qty=q,
        )
        if worst_qty < 0:
            raise ValueError(
                f"売り数量が保有を超過: sell={q} で建玉が負になる"
                f"(銘柄{instrument_id} {worst_day.isoformat()} 時点で {worst_qty})"
            )
        # 原価は恒等式と同じ日付境界で切る(新-22)。全期間再生にすると後日付の買いが
        # 平均原価に混ざり、その日の securities 残高と再生原価がずれる。
        held_qty, cost = _util.replay_position(
            conn, book_id, instrument_id, as_of=entry_date
        )
        cost_released = cost * q / held_qty if held_qty > 0 else Decimal(0)
        realized = gross - cost_released  # 正=実現益

        lines.append({"account_id": "cash", "debit": gross - f, "currency": currency})
        if f > 0:
            lines.append({"account_id": "commission", "debit": f, "currency": currency})
        lines.append(
            {"account_id": _util.COST_ACCOUNT, "credit": cost_released, "currency": currency,
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


def post_in_kind_contribution(
    conn: psycopg.Connection,
    *,
    book_id: str,
    instrument_id: int,
    qty: Any,
    price: Any,
    entry_date: _date,
    run_id: int,
    currency: str = "JPY",
    credit_account: str = "capital",
    description: str | None = None,
    source: str = "in_kind",
    reference: str | None = None,
    posted_by: str = _util.IN_KIND_POSTED_BY[0],
) -> int:
    """現物拠出(約定を経ない建玉の受け入れ)を記帳する。Dr securities / Cr capital。

    **なぜ専用 API が要るのか**(独立審査 新-17): 拠出を素の ``post_entry`` で
    ``Dr securities / Cr capital`` と立てると、``replay_position`` はその建玉の**数量を
    知らない**。数量ゼロに見える建玉は ``post_mark_to_market`` の対象から外れ、**一度も
    評価替えされない**(審査実測: 終値 1500/2000/500 を渡しても残高は拠出額 1,000,000 の
    まま、``detail.positions`` にも現れない)。数量ゼロ残渣の洗い替えが「約定外の建玉に
    触れない」で正しくいられるのはこの前提のおかげであり、in-kind を時価評価する必要が
    出た時点で前提は崩れる。

    **建玉数量の真実は証憑に持たせる**(reminder が挙げた選択肢②のうち後者)。拠出時に
    ``kind='in_kind_contribution'`` の証憑(instrument_id / qty / price)を要求し、
    ``replay_position`` の再生対象に含める。``trade.fills`` 相当の建玉イベント表を新設する
    案は採らない — ``trade.fills`` と証憑という既存の 2 つの真実に 3 つ目を足し、どれが
    正かを毎回決め直す必要が出るためである。

    取得原価は ``qty × price`` であり、これがそのまま原価勘定の借方になる。したがって
    ``securities`` 残高と ``replay_position`` の原価は拠出後も一致し続ける(0034 の
    原価恒等式)。以後この建玉は約定と同じ移動平均法で扱われ、売却時の実現損益も、締めの
    評価替えも通常どおり効く。

    **これは NAV 生成プリミティブである**(独立審査 新-21): 証憑には呼び出し側が書いた
    数量・単価がそのまま入るため、**原価恒等式は構造的に必ず成立する** — 恒等式は「同じ
    呼び出しが書いた 2 つの成果物の内部整合」であって実在性の検査ではない。しかも新-17 の
    是正により、架空の拠出は評価替えにも乗る(是正前は原価のまま動かなかった)。台帳の中に
    実在性を確かめる手段は無いので、統制は 2 つ置く:

    1. ``posted_by`` を ``_util.IN_KIND_POSTED_BY``(運用オペレーション経路)に限る
    2. 記帳と**同一トランザクションで** #運営 へ通知を投入する(``press.outbox``)。
       呼び出し側の作法に依存しない — 通知だけを落とすことができない

    どちらも申告制の域を出ない(``posted_by`` は呼び出し側が決める列である)。塞いだのは
    「黙って NAV が増える」経路であって「嘘を申告する」経路ではない。後者は人が見る側での
    照合(#運営 通知 + 監査)に委ねる — 限界の全文は
    docs/design/11-mtm-account-separation.md §7。

    **未対応**: 現物払戻(拠出の逆向き)と株式分割・併合。どちらも証憑 kind を分けて
    数量の増減を表現する拡張になる。未対応の経路が ``securities`` を直接動かそうとしても
    0034 の原価勘定ガードが書込時に拒否する(数量を再生できる証憑でなければ通らない)。
    """
    if posted_by not in _util.IN_KIND_POSTED_BY:
        raise ValueError(
            f"現物拠出の posted_by は {_util.IN_KIND_POSTED_BY} のいずれか"
            f"(拠出は運用オペレーション経路のみ — 独立審査 新-21): {posted_by!r}"
        )
    q = _util.to_decimal(qty)
    p = _util.to_decimal(price)
    if q <= 0:
        raise ValueError(f"qty は正: {q}")
    if p <= 0:
        raise ValueError(f"price は正: {p}")
    amount = q * p

    evidence = _util.create_evidence(
        conn,
        kind="in_kind_contribution",
        payload={
            "instrument_id": int(instrument_id),
            "qty": str(q),
            "price": str(p),
            "amount": str(amount),
            "currency": currency,
            "credit_account": credit_account,
            "as_of": entry_date.isoformat(),
            "reference": reference,
        },
        source=source,
    )

    lines = [
        {"account_id": _util.COST_ACCOUNT, "debit": amount, "currency": currency,
         "instrument_id": int(instrument_id)},
        {"account_id": credit_account, "credit": amount, "currency": currency},
    ]
    entry_id = post_entry(
        conn,
        book_id=book_id,
        entry_date=entry_date,
        description=description or f"現物拠出 銘柄{instrument_id} {q}@{p}",
        lines=lines,
        evidence=evidence,
        run_id=run_id,
        posted_by=posted_by,
    )
    _notify_in_kind(
        conn,
        book_id=book_id,
        instrument_id=int(instrument_id),
        qty=q,
        price=p,
        amount=amount,
        entry_id=entry_id,
        entry_date=entry_date,
        run_id=run_id,
        posted_by=posted_by,
        reference=reference,
    )
    return entry_id


def _notify_in_kind(
    conn: psycopg.Connection,
    *,
    book_id: str,
    instrument_id: int,
    qty: Decimal,
    price: Decimal,
    amount: Decimal,
    entry_id: int,
    entry_date: _date,
    run_id: int,
    posted_by: str,
    reference: str | None,
) -> None:
    """現物拠出を #運営 へ通知する(記帳と**同一トランザクション**— 独立審査 新-21)。

    通知を呼び出し側の責務にすると「拠出だけして通知を落とす」ことができてしまう。
    ``press.outbox`` への投入は同じ ``conn`` の同じトランザクションで行うので、記帳が
    コミットされたなら通知も必ずコミットされている(逆も同じ)。

    照合ブレイク・残渣と同格の専用 embed にし、``urgent`` で上げる。拠出は運用上ごく稀な
    事象であり、身に覚えのない拠出は**それ自体が事故か偽装**である — 頻度が低いので
    urgent の乱発にならない(通知疲れの評価は risk 側 ``PENDING_MATERIALITY_NAV`` と同じ姿勢)。

    ``ryza.bot`` への依存は定数と outbox 投入だけであり、``ledger`` スキーマの書き込み権限
    (会計エンジンのみ)には触れない。
    """
    embed = {
        "title": "⚠️ 現物拠出の記帳",
        "description": (
            f"{book_id} {entry_date.isoformat()}: 銘柄 {instrument_id} を {qty}@{price}"
            f"(取得原価 {amount})で受け入れた。**約定を経ずに NAV が増える経路**であり、"
            "台帳の中に実在性を確かめる手段は無い(証憑の数量・単価は記帳した側の申告)。"
            "出資の受け入れとして意図したものか確認すること。"
        ),
        "color": COLOR_FLASH,
        "fields": [
            {"name": "仕訳", "value": f"entry {entry_id} / run {run_id}", "inline": True},
            {"name": "記帳経路", "value": posted_by, "inline": True},
            {"name": "参照", "value": reference or "(なし)", "inline": True},
        ],
        "author": org.author_for_role("audit"),
        "footer": {"text": DISCLAIMER},
    }
    enqueue(conn, "ops", embed, run_id, urgent=True)


def post_mark_to_market(
    conn: psycopg.Connection,
    *,
    book_id: str,
    instrument_id: int,
    price: Any | None,
    entry_date: _date,
    run_id: int,
    currency: str = "JPY",
    posted_by: str = _util.MTM_POSTED_BY[0],
) -> int | None:
    """評価替え(未実現損益の洗い替え)。建玉の帳簿価額を時価に一致させる。

    delta = 時価総額(保有数量×price) − 現在の帳簿価額(原価勘定 + 評価調整勘定)。
    delta>0: Dr securities_mtm / Cr unrealized_pnl、delta<0: 逆。**記帳先は評価調整勘定**
    ``securities_mtm`` であり原価勘定ではない(0034 の分離 —
    docs/design/11-mtm-account-separation.md)。
    差分計上のため unrealized_pnl の累計は常に (時価 − 取得原価) に一致する(洗い替えと等価)。
    delta=0(または保有ゼロで帳簿価額もゼロ)なら記帳せず None を返す。

    **``price=None`` は「数量ゼロの銘柄」専用の経路**(独立審査 新-10)。全売却した銘柄の
    時価は価格に依らずゼロなので、終値を引かずに残渣(= 売却時に取り崩されなかった評価益
    ぶんの帳簿価額)をゼロへ洗い替えられる。建玉が無い銘柄の終値を要求すると、上場廃止や
    バー欠測で締めそのものが落ちる(``execution.close._make_price_source`` は終値が無ければ
    例外)。数量が残っている銘柄に ``None`` を渡すのは呼び出し側の誤りなので ValueError。

    なぜ残渣が出るか: ``post_fill`` の売りは **取得原価ぶん**(移動平均法の
    ``cost_released``)しか原価勘定を取り崩さず、差額を realized_pnl に振る。評価替えで
    積んだ「時価 − 取得原価」は評価調整勘定に残ったままなので、ここでゼロへ落として
    unrealized_pnl を戻さないと NAV が恒久的に過大になる(残った未実現益が消えない)。

    **数量ゼロで消すのは ``mtm_book_value``(評価調整勘定の残高)だけ**で、帳簿価額の総額
    ではない。証憑つきの建玉イベントを伴わない直接記帳(数量つき証憑の無い手仕訳)は数量
    ゼロに見えるため、総額を消すと実在の資産を帳簿から消してしまう(実測: 現物拠出
    1,000,000 の日の NAV が丸ごと戻る)。0034 の分離後は消す対象が勘定残高そのものなので、
    原価勘定には**構造的に触れられない**。

    **数量も帳簿価額も同じ ``as_of=entry_date`` で切る**(独立審査 新-13)。数量だけ全期間
    再生にすると、**将来日付の売りが先に記帳されている日**の締めが「数量ゼロ ⇒ 残渣」と
    誤判定し、その日に実在する建玉の評価額を消す(審査実測: d2 付けの全売りを先に記帳した
    状態で d1 を締めると NAV 10,200,000 → 10,000,000、returns ``[-0.019608]`` ← 真値
    ``[0.0]``。符号が逆なだけで新-10 と同じ恒久的偽リターン)。時価と帳簿価額を別の
    日付境界で測ること自体が差分計算の前提を壊す。

    ``posted_by`` は ``_util.MTM_POSTED_BY`` に限る(新-14): 後で「評価替えが作った残高」を
    同定する判定子がこの列なので、それ以外の値で書くと自分が書いた評価替えを自分で
    認識できなくなる。
    """
    if posted_by not in _util.MTM_POSTED_BY:
        raise ValueError(
            f"評価替えの posted_by は {_util.MTM_POSTED_BY} のいずれか"
            f"(mtm_book_value の判定子): {posted_by!r}"
        )
    qty, _cost = _util.replay_position(conn, book_id, instrument_id, as_of=entry_date)
    if price is None:
        if qty != 0:
            raise ValueError(
                f"price=None は数量ゼロの銘柄のみ(全売却後の残渣の洗い替え): "
                f"銘柄{instrument_id} qty={qty}"
            )
        p = None
        market_value = Decimal(0)
        # 評価替えが作った残高 = 消すべき残渣。約定外の securities には触れない。
        book_value = _util.mtm_book_value(conn, book_id, instrument_id, as_of=entry_date)
    else:
        p = _util.to_decimal(price)
        market_value = qty * p
        book_value = _util.securities_book_value(
            conn, book_id, instrument_id, as_of=entry_date
        )
    delta = market_value - book_value
    if delta == 0:
        return None

    evidence = _util.create_evidence(
        conn,
        kind="price_snapshot",
        payload={
            "instrument_id": int(instrument_id),
            # 数量ゼロの洗い替えは終値を引いていない。存在しない価格を "0" と書くと
            # 「終値 0 円で評価した」証憑になるため、null + 理由を明示する(新-6 の教訓)。
            "price": None if p is None else str(p),
            "qty": str(qty),
            "market_value": str(market_value),
            "as_of": entry_date.isoformat(),
            # 数量ゼロのときの book_value は「評価替えが作った残高」= 洗い替える額。
            **({"zero_qty_writeoff": True, "mtm_book_value": str(book_value)}
               if p is None else {}),
        },
        source="price_source",
    )

    # 記帳先は**評価調整勘定**であって原価勘定ではない(0034 の分離)。取得原価と評価調整を
    # 同じ勘定に足し込まないことで、原価恒等式(securities 残高 = 建玉再生の原価)が
    # 締めで検査可能になる。
    if delta > 0:
        lines = [
            {"account_id": _util.MTM_ACCOUNT, "debit": delta, "currency": currency,
             "instrument_id": int(instrument_id)},
            {"account_id": "unrealized_pnl", "credit": delta, "currency": currency},
        ]
    else:
        lines = [
            {"account_id": "unrealized_pnl", "debit": -delta, "currency": currency},
            {"account_id": _util.MTM_ACCOUNT, "credit": -delta, "currency": currency,
             "instrument_id": int(instrument_id)},
        ]

    desc = f"評価替え 銘柄{instrument_id} 時価{market_value}"
    if p is None:
        desc += "(建玉ゼロ — 全売却後の残渣を洗い替え)"
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
