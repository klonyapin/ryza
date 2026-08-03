"""記帳 API(posting.py)の検証。

受け入れ基準:
- 貸借不一致・証憑なし・OPS 費用のタグなしが例外になる
- 買い→値上がり→一部売却で実現損益(移動平均法)・未実現損益が手計算と一致
- 逆仕訳後の試算表が元に戻る
- すべての書き込みが run_id を持つ
"""

from __future__ import annotations

import re
from datetime import date
from decimal import Decimal

import psycopg
import pytest

from ryza.ledger import _util, posting, statements

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


def _carrying(conn, book_id, as_of=DAY, instrument_id=None):
    """帳簿価額 = 原価勘定 + 評価調整勘定(0034 の分離前に ``securities`` が持っていた値)。

    分離の**等価性**はこの合計で見る: 分離前後で NAV・帳簿価額・未実現損益は 1 円も
    変わらず、変わるのは内訳が 2 勘定に分かれたことだけである
    (docs/design/11-mtm-account-separation.md §2.2)。
    """
    return (_balance(conn, book_id, "securities", as_of, instrument_id)
            + _balance(conn, book_id, "securities_mtm", as_of, instrument_id))


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
    # 帳簿価額 = 残 60 の時価 = 60*620 = 37200(分離前の securities 残高と同値 — 等価性)。
    assert _carrying(conn, "DEMO_FUND", instrument_id=iid) == D(37200)
    # 内訳: 原価勘定は取得原価 60*500 = 30000、評価調整勘定が未実現 7200 を持つ(0034)。
    assert _balance(conn, "DEMO_FUND", "securities", instrument_id=iid) == D(30000)
    assert _balance(conn, "DEMO_FUND", "securities_mtm", instrument_id=iid) == D(7200)


def test_mark_to_market_writes_off_residue_without_a_price(conn, run_id):
    """``price=None`` は数量ゼロ専用の洗い替え経路(独立審査 新-10)。

    全売却後の帳簿価額には評価益ぶんの残渣が残る(売りは取得原価ぶんしか取り崩さない)。
    時価は価格に依らずゼロなので終値を引かずに戻せる — 建玉の無い銘柄の終値を要求すると
    上場廃止・バー欠測で締めごと落ちるため、この経路が必要になる。

    0034 の分離後、残渣は**評価調整勘定にだけ**現れる(原価勘定は売りで正確にゼロになる)。
    洗い替えが原価勘定に触れられないことが構造で保証される。
    """
    iid = 1003
    posting.post_fill(conn, book_id="DEMO_FUND", instrument_id=iid, side="buy",
                      qty=100, price=500, entry_date=DAY, run_id=run_id)
    posting.post_mark_to_market(conn, book_id="DEMO_FUND", instrument_id=iid,
                                price=600, entry_date=DAY, run_id=run_id)
    posting.post_fill(conn, book_id="DEMO_FUND", instrument_id=iid, side="sell",
                      qty=100, price=600, entry_date=DAY, run_id=run_id)
    # 売却直後: 取得原価 50,000 だけが取り崩され、評価益 10,000 が残渣として残る。
    assert _carrying(conn, "DEMO_FUND", instrument_id=iid) == D(10000)
    assert _balance(conn, "DEMO_FUND", "securities", instrument_id=iid) == D(0)
    assert _balance(conn, "DEMO_FUND", "securities_mtm", instrument_id=iid) == D(10000)

    entry_id = posting.post_mark_to_market(conn, book_id="DEMO_FUND", instrument_id=iid,
                                           price=None, entry_date=DAY, run_id=run_id)
    assert entry_id is not None
    assert _carrying(conn, "DEMO_FUND", instrument_id=iid) == D(0)
    assert _balance(conn, "DEMO_FUND", "securities_mtm", instrument_id=iid) == D(0)
    assert _balance(conn, "DEMO_FUND", "unrealized_pnl") == D(0)  # 未実現は全額戻る
    assert _balance(conn, "DEMO_FUND", "realized_pnl") == D(-10000)  # 実現益だけが残る
    # 残渣が無くなれば何も書かない(冪等)。
    assert posting.post_mark_to_market(conn, book_id="DEMO_FUND", instrument_id=iid,
                                       price=None, entry_date=DAY, run_id=run_id) is None


def test_mark_to_market_rejects_a_posted_by_outside_the_predicate(conn, run_id):
    """評価替えの ``posted_by`` は ``MTM_POSTED_BY`` に限る(独立審査 新-14)。

    0034 の勘定分離後、これは読み取り時の判定子ではなく**書き込み時のガード**である
    (残渣の同定は評価調整勘定の残高が行う)。分離は新-14 の攻撃を塞がず宛先を移すだけ
    なので、このガードを外すと防御が純減する(docs/design/11 §5.2-1)。
    """
    iid = 1006
    posting.post_fill(conn, book_id="DEMO_FUND", instrument_id=iid, side="buy",
                      qty=10, price=100, entry_date=DAY, run_id=run_id)
    with pytest.raises(ValueError, match="posted_by"):
        posting.post_mark_to_market(conn, book_id="DEMO_FUND", instrument_id=iid,
                                    price=120, entry_date=DAY, run_id=run_id,
                                    posted_by="test.ledger")


def test_mark_to_market_uses_the_same_as_of_for_qty_and_book_value(conn, run_id):
    """数量と帳簿価額を同じ ``as_of=entry_date`` で切る(独立審査 新-13)。

    将来日付の売りが先に記帳されていても、``entry_date`` 時点の建玉で評価替えする。
    数量だけ全期間再生にすると「数量ゼロ ⇒ 残渣」と誤判定して実在の建玉を消す。
    """
    iid = 1007
    d0, d1 = DAY, date(2026, 8, 5)
    posting.post_fill(conn, book_id="DEMO_FUND", instrument_id=iid, side="buy",
                      qty=100, price=500, entry_date=d0, run_id=run_id)
    posting.post_fill(conn, book_id="DEMO_FUND", instrument_id=iid, side="sell",
                      qty=100, price=600, entry_date=d1, run_id=run_id)  # 将来日付の売り

    posting.post_mark_to_market(conn, book_id="DEMO_FUND", instrument_id=iid,
                                price=600, entry_date=d0, run_id=run_id)
    # d0 時点は 100 株保有 → 時価 60,000。将来の売りに引きずられてゼロにしない。
    assert _carrying(conn, "DEMO_FUND", as_of=d0, instrument_id=iid) == D(60000)


def test_sell_releases_cost_at_the_entry_date_not_the_whole_history(conn, run_id):
    """売りの ``cost_released`` は ``as_of=entry_date`` の建玉から決める(独立審査 新-22)。

    審査の実測ケースそのもの: d0 買い 100@500 → **d2 買い 100@700 を先に記帳** →
    d1 売り 50@800。全期間再生だと平均原価が (50,000+70,000)/200=600 になり
    ``cost_released=30,000``、d1 時点の ``securities`` 残高は 20,000 に落ちる一方、
    恒等式側の再生原価は 25,000 で、健全な帳簿なのに 5,000 のずれが立った。
    """
    iid = 1040
    d0, d1, d2 = DAY, date(2026, 8, 4), date(2026, 8, 5)
    posting.post_fill(conn, book_id="DEMO_FUND", instrument_id=iid, side="buy",
                      qty=100, price=500, entry_date=d0, run_id=run_id)
    posting.post_fill(conn, book_id="DEMO_FUND", instrument_id=iid, side="buy",
                      qty=100, price=700, entry_date=d2, run_id=run_id)  # 後日付を先に記帳
    posting.post_fill(conn, book_id="DEMO_FUND", instrument_id=iid, side="sell",
                      qty=50, price=800, entry_date=d1, run_id=run_id)

    # d1 時点の平均原価は 500(d2 の買いは混ざらない)→ 取り崩しは 25,000。
    assert _balance(conn, "DEMO_FUND", "securities", as_of=d1, instrument_id=iid) == D(25000)
    # 実現損益 = 50*800 − 25,000 = 15,000(修正前は 30,000 取り崩しで 10,000 だった)。
    assert _balance(conn, "DEMO_FUND", "realized_pnl", as_of=d1) == D(-15000)

    # 原価恒等式(0034)が d1・d2 の両方で成立する = 締めが偽陽性を出さない。
    # d2 側は ``replay_position`` の再生順が ``(entry_date, entry_id)`` であることを固定する
    # — ``entry_id`` 順のままだと d2 の買いが d1 の売りより前に置かれて 90,000 になり、
    # 偽陽性が d1 から d2 へ移るだけになる(2 つの是正は対)。
    for day, expected in ((d1, D(25000)), (d2, D(95000))):
        _qty, cost = _util.replay_position(conn, "DEMO_FUND", iid, as_of=day)
        assert cost == expected
        assert _util.securities_cost_value(conn, "DEMO_FUND", iid, as_of=day) == expected


def test_sell_rejects_quantity_not_held_on_the_entry_date(conn, run_id):
    """後日付の買いを先に記帳しても、その株は売却日には存在しない(不変原則4)。"""
    iid = 1041
    d0, d1, d2 = DAY, date(2026, 8, 4), date(2026, 8, 5)
    posting.post_fill(conn, book_id="DEMO_FUND", instrument_id=iid, side="buy",
                      qty=100, price=500, entry_date=d0, run_id=run_id)
    posting.post_fill(conn, book_id="DEMO_FUND", instrument_id=iid, side="buy",
                      qty=100, price=700, entry_date=d2, run_id=run_id)
    with pytest.raises(ValueError, match="売り数量が保有を超過"):
        posting.post_fill(conn, book_id="DEMO_FUND", instrument_id=iid, side="sell",
                          qty=150, price=800, entry_date=d1, run_id=run_id)


def test_sell_rejects_double_release_when_a_later_dated_sell_exists(conn, run_id):
    """後日付の売りが記帳済みなら、過去日付の売りは同じ株を二重に払い出せない(再22-1)。

    審査実測 P3 そのもの: 買 d0 100 → **売 d3 100 を記帳** → 売 d1 50。``as_of=d1`` の
    保有数量だけで判定すると 100 ≥ 50 で**受理**され、全履歴では d3 で qty=−50 の
    幻の売建が立つ。しかも残高も再生も −25,000 で一致するため**原価恒等式は沈黙し**、
    締めに負数量の検査も無い(recon は broker positions が来た日にしか掛からない)。
    新-22 の是正が記帳ガードに開けた穴であり、``as_of`` 判定に**加えて**塞ぐ。
    """
    iid = 1043
    d0, d1, d3 = DAY, date(2026, 8, 4), date(2026, 8, 6)
    posting.post_fill(conn, book_id="DEMO_FUND", instrument_id=iid, side="buy",
                      qty=100, price=500, entry_date=d0, run_id=run_id)
    posting.post_fill(conn, book_id="DEMO_FUND", instrument_id=iid, side="sell",
                      qty=100, price=900, entry_date=d3, run_id=run_id)
    with pytest.raises(ValueError, match="建玉が負になる"):
        posting.post_fill(conn, book_id="DEMO_FUND", instrument_id=iid, side="sell",
                          qty=50, price=800, entry_date=d1, run_id=run_id)

    # 台帳は動いていない(幻の売建が残らない)。
    assert _util.replay_position(conn, "DEMO_FUND", iid)[0] == D(0)


def test_sell_rejects_a_negative_interval_hidden_by_a_later_dated_buy(conn, run_id):
    """端点ではなく**最小値**を見る(再22-1 の手計算ケース)。

    買 d0 100 / 買 d4 100 / 売 d3 60 が記帳済みのとき、売 d1 100 は
    ``as_of=d1`` で 100・全期間の期末数量で 140 と**どちらの端点検査も通る**が、
    挿入後の推移は d1:0 → **d3:−60** → d4:40 で d3 に負区間ができる。
    「``as_of`` 判定と全期間 ``qty>=q`` の AND」で済ませるとここを見逃す。
    """
    iid = 1044
    d0, d1, d3, d4 = DAY, date(2026, 8, 4), date(2026, 8, 6), date(2026, 8, 7)
    posting.post_fill(conn, book_id="DEMO_FUND", instrument_id=iid, side="buy",
                      qty=100, price=500, entry_date=d0, run_id=run_id)
    posting.post_fill(conn, book_id="DEMO_FUND", instrument_id=iid, side="buy",
                      qty=100, price=700, entry_date=d4, run_id=run_id)
    posting.post_fill(conn, book_id="DEMO_FUND", instrument_id=iid, side="sell",
                      qty=60, price=900, entry_date=d3, run_id=run_id)

    # 端点はどちらも足りている(素朴な AND が通してしまうことの対照)。
    assert _util.replay_position(conn, "DEMO_FUND", iid, as_of=d1)[0] == D(100)
    assert _util.replay_position(conn, "DEMO_FUND", iid)[0] == D(140)

    with pytest.raises(ValueError, match="建玉が負になる") as exc:
        posting.post_fill(conn, book_id="DEMO_FUND", instrument_id=iid, side="sell",
                          qty=100, price=800, entry_date=d1, run_id=run_id)
    # 「いつ足りないのか」を名指しする(端点 d1 ではなく負区間の起点 d3)。
    assert d3.isoformat() in str(exc.value)
    assert "-60" in str(exc.value)


def test_worst_running_qty_accepts_a_sell_that_stays_non_negative(conn, run_id):
    """拒否が広すぎないことの対照: 全時点で 0 以上なら通す(新-22 の本命ケース)。"""
    iid = 1045
    d0, d1, d2 = DAY, date(2026, 8, 4), date(2026, 8, 5)
    posting.post_fill(conn, book_id="DEMO_FUND", instrument_id=iid, side="buy",
                      qty=100, price=500, entry_date=d0, run_id=run_id)
    posting.post_fill(conn, book_id="DEMO_FUND", instrument_id=iid, side="buy",
                      qty=100, price=700, entry_date=d2, run_id=run_id)
    events = _util.position_events(conn, "DEMO_FUND", iid)
    # 推移は d1:50 → d2:150。最小は挿入直後の 50。
    assert _util.worst_running_qty_with_sell(events, entry_date=d1, qty=D(50)) == (D(50), d1)
    assert posting.post_fill(conn, book_id="DEMO_FUND", instrument_id=iid, side="sell",
                             qty=50, price=800, entry_date=d1, run_id=run_id) is not None


def test_backdated_buy_after_a_sell_still_breaks_the_cost_identity(conn, run_id):
    """**真陽性は消さない**: 売りより前の日付の買いを後から入れた帳簿は名指しされる。

    既記帳の ``cost_released`` は当時の平均原価で確定しており(仕訳は追記オンリー —
    0005)、後から過去日に買いを足すと再生原価と原価勘定残高が食い違う。これは偽陽性
    ではなく「実現損益が古い平均原価で確定している」事実であり、新-22 の是正で消して
    しまってはならない量である(消すには証憑に ``cost_released`` を焼き込んで再生を
    その値に従わせるしかなく、恒等式が内部整合の検査に退化する — 新-21 と同じ形)。
    """
    iid = 1042
    d0, d1, d5 = DAY, date(2026, 8, 4), date(2026, 8, 8)
    posting.post_fill(conn, book_id="DEMO_FUND", instrument_id=iid, side="buy",
                      qty=100, price=500, entry_date=d0, run_id=run_id)
    posting.post_fill(conn, book_id="DEMO_FUND", instrument_id=iid, side="sell",
                      qty=50, price=900, entry_date=d5, run_id=run_id)  # 取り崩し 25,000
    posting.post_fill(conn, book_id="DEMO_FUND", instrument_id=iid, side="buy",
                      qty=100, price=700, entry_date=d1, run_id=run_id)  # 過去日を後から

    # 残高 50,000+70,000−25,000 = 95,000 に対し、再生は 50 株を平均 600 で取り崩して 90,000。
    assert _balance(conn, "DEMO_FUND", "securities", as_of=d5, instrument_id=iid) == D(95000)
    _qty, cost = _util.replay_position(conn, "DEMO_FUND", iid, as_of=d5)
    assert cost == D(90000)


def test_mark_to_market_rejects_missing_price_while_holding(conn, run_id):
    """数量が残っている銘柄に ``price=None`` を渡すのは呼び出し側の誤り(黙って 0 評価しない)。"""
    iid = 1004
    posting.post_fill(conn, book_id="DEMO_FUND", instrument_id=iid, side="buy",
                      qty=10, price=100, entry_date=DAY, run_id=run_id)
    with pytest.raises(ValueError, match="数量ゼロ"):
        posting.post_mark_to_market(conn, book_id="DEMO_FUND", instrument_id=iid,
                                    price=None, entry_date=DAY, run_id=run_id)


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


# ── 現物拠出(独立審査 新-17)────────────────────────────────────────────────
def test_in_kind_contribution_books_cost_and_replays_quantity(conn, run_id):
    """拠出は原価勘定に ``qty × price`` を立て、数量つき証憑で建玉を再生可能にする。"""
    iid = 1020
    entry_id = posting.post_in_kind_contribution(
        conn, book_id="DEMO_FUND", instrument_id=iid, qty=200, price=750,
        entry_date=DAY, run_id=run_id, reference="出資契約 2026-08",
    )
    assert entry_id > 0
    assert _balance(conn, "DEMO_FUND", "securities", instrument_id=iid) == D(150_000)
    assert _balance(conn, "DEMO_FUND", "securities_mtm", instrument_id=iid) == D(0)
    # 建玉が再生でき、原価恒等式(原価勘定 = 再生原価)が成立する。
    assert _util.replay_position(conn, "DEMO_FUND", iid, as_of=DAY) == (D(200), D(150_000))
    with conn.cursor() as cur:
        cur.execute(
            """SELECT e.kind FROM ledger.journal_entries je
               JOIN ledger.evidence e ON e.evidence_id = je.evidence_id
               WHERE je.entry_id = %s""",
            (entry_id,),
        )
        assert cur.fetchone()[0] == "in_kind_contribution"


def test_in_kind_contribution_rejects_non_positive_quantity_or_price(conn, run_id):
    """数量・単価は正。ゼロ単価の拠出は原価ゼロの建玉を作り、恒等式の意味を壊す。"""
    for kwargs in ({"qty": 0, "price": 100}, {"qty": 10, "price": 0}):
        with pytest.raises(ValueError, match="は正"):
            posting.post_in_kind_contribution(
                conn, book_id="DEMO_FUND", instrument_id=1021,
                entry_date=DAY, run_id=run_id, **kwargs,
            )


# ── 評価調整勘定の書き込みガード(migrations/0034)──────────────────────────
def test_mtm_account_rejects_writes_outside_the_closing_job(conn, run_id):
    """``securities_mtm`` へ書けるのは締めジョブか逆仕訳だけ(DB トリガ)。

    読み取り時の述語を書き込み時の拒否に移したもの。**防御であって境界ではない** —
    posted_by は呼び出し側が決める列なので、値を騙る記帳は依然として可能である
    (docs/design/11-mtm-account-separation.md §7)。
    """
    lines = [
        {"account_id": "securities_mtm", "debit": D(100), "currency": "JPY",
         "instrument_id": 1022},
        {"account_id": "capital", "credit": D(100), "currency": "JPY"},
    ]
    evidence = {"kind": "price_snapshot", "payload": {"forged": True}, "source": "test"}
    with pytest.raises(psycopg.errors.RaiseException, match="締めジョブ"):
        with conn.transaction():
            posting.post_entry(
                conn, book_id="DEMO_FUND", entry_date=DAY, description="偽装",
                lines=lines, evidence=evidence, run_id=run_id, posted_by="test.ledger",
            )

    # 銘柄の無い評価調整は「どの建玉の調整か」が失われ、洗い替えから永久に漏れる。
    with pytest.raises(psycopg.errors.RaiseException, match="instrument_id"):
        with conn.transaction():
            posting.post_entry(
                conn, book_id="DEMO_FUND", entry_date=DAY, description="銘柄なし評価替え",
                lines=[
                    {"account_id": "securities_mtm", "debit": D(100), "currency": "JPY"},
                    {"account_id": "unrealized_pnl", "credit": D(100), "currency": "JPY"},
                ],
                evidence=evidence, run_id=run_id, posted_by="ledger.closing",
            )


def test_mtm_account_allows_the_reversal_of_a_revaluation(conn, run_id):
    """評価替えの逆仕訳は通す — 逆仕訳を塞ぐと訂正の唯一の手段が消える(0005)。"""
    iid = 1023
    posting.post_fill(conn, book_id="DEMO_FUND", instrument_id=iid, side="buy",
                      qty=10, price=100, entry_date=DAY, run_id=run_id)
    mtm_entry = posting.post_mark_to_market(conn, book_id="DEMO_FUND", instrument_id=iid,
                                            price=120, entry_date=DAY, run_id=run_id)
    assert _balance(conn, "DEMO_FUND", "securities_mtm", instrument_id=iid) == D(200)
    posting.reverse_entry(conn, entry_id=mtm_entry, reason="評価替えの取消(テスト)",
                          run_id=run_id)
    assert _balance(conn, "DEMO_FUND", "securities_mtm", instrument_id=iid) == D(0)


def test_reversal_exemption_requires_a_real_matching_line(conn, run_id):
    """逆仕訳免除は**実突合**であってフラグではない(独立審査 新-18)。

    ``reversal_of`` は ``post_entry`` の公開引数であり、対象仕訳とも帳簿とも突合されない。
    審査の実測では、無関係な entry_id を入れるだけで posted_by 検証を迂回して
    ``Dr securities_mtm 3,000,000 / Cr capital`` が通り、次の締めが全額を洗い替えて
    NAV 13,000,001→10,000,001・偽の未実現損 3,000,000 を立てた(新-14(a) の完全再現)。
    免除条件を「対象仕訳に借貸を入れ替えた同一勘定・同一銘柄・同額の明細があるか」に
    狭めたので、この攻撃は書込時に落ちる。
    """
    iid = 1024
    buy = posting.post_fill(conn, book_id="DEMO_FUND", instrument_id=iid, side="buy",
                            qty=10, price=100, entry_date=DAY, run_id=run_id)
    mtm_entry = posting.post_mark_to_market(conn, book_id="DEMO_FUND", instrument_id=iid,
                                            price=120, entry_date=DAY, run_id=run_id)

    def _forged_reversal(target: int, amount, account="securities_mtm"):
        posting.post_entry(
            conn, book_id="DEMO_FUND", entry_date=DAY, description="逆仕訳を騙る記帳",
            lines=[
                {"account_id": account, "debit": amount, "currency": "JPY",
                 "instrument_id": iid},
                {"account_id": "capital", "credit": amount, "currency": "JPY"},
            ],
            evidence={"kind": "price_snapshot", "payload": {"forged": True},
                      "source": "test"},
            run_id=run_id, posted_by="probe.attacker", reversal_of=target,
        )

    # (a) 審査の攻撃そのもの: 無関係な仕訳(買い約定)を reversal_of に入れる。
    with pytest.raises(psycopg.errors.RaiseException, match="締めジョブ"):
        with conn.transaction():
            _forged_reversal(buy, D(3_000_000))

    # (b) 対象は本物の評価替えだが金額が違う(打ち消しになっていない)。
    with pytest.raises(psycopg.errors.RaiseException, match="締めジョブ"):
        with conn.transaction():
            _forged_reversal(mtm_entry, D(3_000_000))

    # (c) 原価勘定側にも同じ免除規則が効く(数量つき証憑を持たない偽の逆仕訳)。
    with pytest.raises(psycopg.errors.RaiseException, match="数量を再生できる証憑"):
        with conn.transaction():
            _forged_reversal(mtm_entry, D(3_000_000), account="securities")

    # 台帳は 1 円も動いていない。
    assert _balance(conn, "DEMO_FUND", "securities_mtm", instrument_id=iid) == D(200)
    assert _balance(conn, "DEMO_FUND", "securities", instrument_id=iid) == D(1000)


def test_every_mtm_posted_by_value_is_accepted_by_the_database(conn, run_id):
    """``MTM_POSTED_BY`` の全要素が DB トリガに受理される(独立審査 新-24)。

    **振る舞い側**の固定: 値を足したときに「Python は通し DB が
    ``RaiseException`` で拒否する」不整合を、実際に記帳して落とす。定義文字列側の
    突合は ``test_mtm_posted_by_matches_the_database_trigger``。
    """
    for i, posted_by in enumerate(_util.MTM_POSTED_BY):
        iid = 1030 + i
        posting.post_fill(conn, book_id="DEMO_FUND", instrument_id=iid, side="buy",
                          qty=10, price=100, entry_date=DAY, run_id=run_id)
        assert posting.post_mark_to_market(
            conn, book_id="DEMO_FUND", instrument_id=iid, price=120, entry_date=DAY,
            run_id=run_id, posted_by=posted_by,
        ) is not None


# ── 新-24 / 再22-2: 建玉勘定ガードの許可値の単一ソース化(トリガ本文 ⇔ Python 定数)──
#: 0034 のガードは許可値を ``<変数> IS DISTINCT FROM '<値>'`` の(値が増えたときは AND
#: 連結の)形で持つ。``check_mtm_line`` は ``parent_posted_by``、``check_cost_line`` は
#: ``parent_kind`` を突き合わせる。
def _trigger_allowed_values(function_source: str, variable: str) -> set[str]:
    """トリガ関数の定義文字列から、``variable`` に対して許可される値の集合を抜き出す。

    抜き出せなければ空集合を返す。呼び出し側が「空でないこと」を別途表明するので、
    トリガの書き方をこの正規表現が想定しない形(``= ANY (ARRAY[...])`` 等)に変えた
    場合は**黙って通らず**、抽出器を直せという形で落ちる。
    """
    pattern = re.compile(
        rf"{re.escape(variable)}\s+IS\s+DISTINCT\s+FROM\s+'((?:[^']|'')*)'", re.IGNORECASE
    )
    return {m.group(1).replace("''", "'") for m in pattern.finditer(function_source)}


def _assert_trigger_matches_constant(conn, function, variable, constant, constant_name):
    """トリガ本文の許可値と Python 定数を**双方向で**突合する。

    部分文字列検査だけでは「トリガ側にだけ余分な値がある」= Python が知らない記帳が
    DB で通る、という片方向の漏れを検出できない(0019 C-11 と同じ論点)。
    """
    with conn.cursor() as cur:
        cur.execute("SELECT pg_get_functiondef(%s::regprocedure)", (function,))
        row = cur.fetchone()
    assert row is not None, f"{function} が存在しない(0034 未適用?)"
    values = _trigger_allowed_values(row[0], variable)
    assert values, (
        f"{function} の本文から {variable} の許可値を抽出できなかった。"
        "書き方を変えたなら _trigger_allowed_values も直すこと(黙って素通りさせない)"
    )
    assert values == set(constant), (
        f"トリガの許可値 {sorted(values)} と {constant_name} {sorted(constant)} が"
        f"一致しない。値を足すときは Python と migration"
        f"(CREATE OR REPLACE FUNCTION {function.split('(')[0]})の両方に足す。"
    )


def test_mtm_posted_by_matches_the_database_trigger(conn):
    """``ledger.check_mtm_line`` の許可 posted_by ⇔ ``_util.MTM_POSTED_BY``(独立審査 新-24)。

    トリガは適用済み migration の中にあり Python 定数を読めないため、単一ソース化は
    「片側だけ変えたら落ちる」ことの固定で行う。変更手順は ``_util.MTM_POSTED_BY``
    の docstring。
    """
    _assert_trigger_matches_constant(
        conn, "ledger.check_mtm_line()", "parent_posted_by",
        _util.MTM_POSTED_BY, "_util.MTM_POSTED_BY",
    )


def test_cost_evidence_kinds_match_the_database_trigger(conn):
    """``ledger.check_cost_line`` の許可 kind ⇔ ``_util.POSITION_EVIDENCE_KINDS``(再22-2)。

    新-24 と**同じ二重管理**が原価勘定ガードにもある。こちらを外すと影響はより直接的で、
    トリガ側にだけ kind を足すと原価勘定に**数量を再生できない行**が入り、締めの
    原価恒等式が毎日鳴る — 新-22 の是正で守った通知経路をまた汚す経路である。
    逆に Python 側にだけ足すと ``replay_position`` が再生する証憑を DB が拒む。
    """
    _assert_trigger_matches_constant(
        conn, "ledger.check_cost_line()", "parent_kind",
        _util.POSITION_EVIDENCE_KINDS, "_util.POSITION_EVIDENCE_KINDS",
    )


@pytest.mark.parametrize("variable", ["parent_posted_by", "parent_kind"])
@pytest.mark.parametrize(
    ("literals", "constant", "expected_match"),
    [
        # 両方に足した(正常な変更手順)。
        (["a", "b"], ("a", "b"), True),
        # Python にだけ足した = DB が RaiseException で拒否する状態。
        (["a"], ("a", "b"), False),
        # トリガにだけ足した = Python が知らない記帳が DB で通る状態。
        (["a", "b"], ("a",), False),
    ],
)
def test_trigger_single_source_check_detects_one_sided_changes(
    variable, literals, constant, expected_match
):
    """突合が**片側だけの変更**を両向き・両トリガで捕まえる(検出器そのものの検証)。

    DB を触らずに済むよう、トリガ本文は 0034 と同じ述語の形で組み立てる。
    """
    body = "\n".join(
        f"    IF {variable} IS DISTINCT FROM '{lit}' THEN RAISE EXCEPTION 'x'; END IF;"
        for lit in literals
    )
    assert (_trigger_allowed_values(body, variable) == set(constant)) is expected_match


# ── 現物拠出の統制(独立審査 新-21)──────────────────────────────────────────
def test_in_kind_contribution_rejects_callers_outside_the_ops_path(conn, run_id):
    """拠出は運用オペレーション経路のみ(``IN_KIND_POSTED_BY``)。

    拠出は約定を経ずに NAV を増やすプリミティブであり、証憑の数量・単価は記帳した側の
    申告なので**原価恒等式は構造的に必ず成立する**(実在性の検査にならない)。せめて
    呼び出し元を台帳の上で一意にする。申告制の域を出ないことは docs/design/11 §7 に明記。
    """
    with pytest.raises(ValueError, match="posted_by"):
        posting.post_in_kind_contribution(
            conn, book_id="DEMO_FUND", instrument_id=1025, qty=10, price=100,
            entry_date=DAY, run_id=run_id, posted_by="probe.attacker",
        )


def test_in_kind_contribution_notifies_ops_in_the_same_transaction(conn, run_id):
    """拠出の通知は記帳と**同一トランザクション**で投入される(落とせない)。

    通知を呼び出し側の責務にすると「拠出だけして通知を落とす」ことができる。同じ conn の
    同じトランザクションで ``press.outbox`` に入れるので、記帳がコミットされたなら通知も
    必ずコミットされている。
    """
    entry_id = posting.post_in_kind_contribution(
        conn, book_id="DEMO_FUND", instrument_id=1026, qty=200, price=750,
        entry_date=DAY, run_id=run_id, reference="出資契約 2026-08",
    )
    with conn.cursor() as cur:
        cur.execute(
            "SELECT channel, urgent, embed_json FROM press.outbox "
            "WHERE run_id = %s ORDER BY id DESC LIMIT 1",
            (run_id,),
        )
        channel, urgent, embed = cur.fetchone()
    assert (channel, urgent) == ("ops", True)
    assert "現物拠出" in embed["title"]
    assert f"entry {entry_id}" in embed["fields"][0]["value"]
    assert embed["fields"][1]["value"] == "ops.capital_ops"
