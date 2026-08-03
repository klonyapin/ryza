"""NAV_FLOW_SQL の書き換えが**意味を変えていない**ことの対照テスト。

``ryza.risk.navflow.NAV_FLOW_SQL`` の flow CTE は 2026-08-04 に書き換えた(reminders
``navflow-equity-flow-query-rewrite``)。旧形は ``journal_lines`` を ``accounts`` に
結合して ``category='equity'`` に絞っていたため、プランナが結合前に選択度を知れず
(実測: 真値 13 行に対し見積り 6,107 行)、どんな索引を置いてもハッシュ結合+逐次走査に
なることが実測で確定していた(``migrations/0027_query_indexes.sql`` の「入れなかったもの」(B))。

**このテストが固定するのは速さではなく等価性である。** 旧 SQL をこのファイルに凍結し、
同一データに対する新旧の結果(行の集合・列の値・並び)が完全一致することを確認する。
外部フローの取りこぼしは NAV リターン系列の誤りになり、EWMA 実現ボラを通じて発注
ブロック判定にまで波及する(navflow のモジュール docstring)。

科目リストをハードコードしていないことも、OPS 帳簿(equity 科目名が ``owner_capital`` で
DEMO_FUND と違う)で同じ結果になることで確かめる。
"""

from __future__ import annotations

import json
import random
from datetime import date, timedelta
from decimal import Decimal

import pytest

from ryza.ledger import posting
from ryza.risk import navflow

D = Decimal
DAY = date(2030, 3, 4)

#: 書き換え**前**の SQL(a760170 時点の ``navflow.NAV_FLOW_SQL`` の逐語コピー)。
#: 参照用に凍結する — 意味論の正は今もこちらである。**新形に合わせて更新してはならない**。
_LEGACY_NAV_FLOW_SQL = """
WITH flow AS (
    SELECT je.entry_date AS entry_date, sum(jl.credit - jl.debit) AS amount
    FROM ledger.journal_lines jl
    JOIN ledger.journal_entries je ON je.entry_id = jl.entry_id
    JOIN ledger.accounts a
      ON a.book_id = jl.book_id AND a.account_id = jl.account_id
    WHERE jl.book_id = %(book)s
      AND a.category = 'equity' AND a.account_id <> 'retained'
    GROUP BY je.entry_date
    HAVING sum(jl.credit - jl.debit) <> 0
), attributed AS (
    SELECT f.entry_date, f.amount, (
               SELECT min(s.snap_date) FROM ledger.nav_snapshots s
               WHERE s.book_id = %(book)s AND s.snap_date >= f.entry_date
           ) AS snap_date
    FROM flow f
), per_snap AS (
    SELECT snap_date,
           coalesce(sum(amount) FILTER (WHERE entry_date = snap_date), 0) AS flow_eop,
           coalesce(sum(amount) FILTER (WHERE entry_date < snap_date), 0) AS flow_bop
    FROM attributed WHERE snap_date IS NOT NULL GROUP BY snap_date
)
SELECT 'snapshot' AS kind, s.snap_date AS day, s.nav, s.status,
       coalesce(p.flow_eop, 0) AS flow_eop, coalesce(p.flow_bop, 0) AS flow_bop,
       coalesce((s.detail ->> 'recon_invalidated')::boolean, false) AS recon_invalidated,
       (s.detail ->> 'recon_invalidated_by_run')::bigint AS recon_invalidated_by_run
FROM ledger.nav_snapshots s
LEFT JOIN per_snap p ON p.snap_date = s.snap_date
WHERE s.book_id = %(book)s
UNION ALL
SELECT 'pending', a.entry_date, NULL, NULL, a.amount, 0, false, NULL
FROM attributed a WHERE a.snap_date IS NULL
ORDER BY 1, 2
"""


def _rows(conn, sql: str, book: str) -> list[tuple]:
    with conn.cursor() as cur:
        cur.execute(sql, {"book": book})
        return cur.fetchall()


def _assert_equivalent(conn, book: str = "DEMO_FUND") -> list[tuple]:
    """新旧 SQL の結果が完全一致することを確認し、結果を返す。"""
    legacy = _rows(conn, _LEGACY_NAV_FLOW_SQL, book)
    current = _rows(conn, navflow.NAV_FLOW_SQL, book)
    assert current == legacy, f"book={book}"
    return current


def _snapshot(conn, day: date, nav, book: str = "DEMO_FUND", detail=None) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO ledger.nav_snapshots (book_id, snap_date, nav, status, detail)
            VALUES (%s, %s, %s, 'confirmed', %s)
            ON CONFLICT (book_id, snap_date) DO UPDATE SET nav = EXCLUDED.nav
            """,
            (book, day, D(nav), json.dumps(detail or {})),
        )


def _post(conn, run_id, day: date, amount, *, account: str, book="DEMO_FUND") -> int:
    """``account`` を貸方(出資 +)/借方(払戻 −)に立てる仕訳。"""
    amount = D(amount)
    cash = "cash" if book == "DEMO_FUND" else "cash_bank"
    if amount >= 0:
        lines = [
            {"account_id": cash, "debit": amount, "currency": "JPY"},
            {"account_id": account, "credit": amount, "currency": "JPY"},
        ]
    else:
        lines = [
            {"account_id": account, "debit": -amount, "currency": "JPY"},
            {"account_id": cash, "credit": -amount, "currency": "JPY"},
        ]
    return posting.post_entry(
        conn,
        book_id=book,
        entry_date=day,
        description="テスト仕訳",
        lines=lines,
        evidence={"kind": "decision", "payload": {"test": "navflow"}, "source": "test"},
        run_id=run_id,
        posted_by="test.navflow_query",
    )


# ── 個別シナリオ ────────────────────────────────────────────────────────────
def test_equivalent_on_the_seeded_book(conn):
    """シードだけの状態(スナップショットなし・出資のみ)でも一致する。"""
    _assert_equivalent(conn)


def test_equivalent_for_flows_on_and_between_snapshot_days(conn, run_id):
    days = [DAY + timedelta(days=i) for i in range(4)]
    for i, d in enumerate(days):
        _snapshot(conn, d, 1_000_000 + i)
    _post(conn, run_id, days[1], 500_000, account="capital")   # 締めのある日 = EOP
    _post(conn, run_id, days[2], 100_000, account="capital")
    rows = _assert_equivalent(conn)
    by_day = {r[1]: (r[4], r[5]) for r in rows if r[0] == "snapshot"}
    assert by_day[days[1]] == (D(500_000), D(0))
    assert by_day[days[2]] == (D(100_000), D(0))


def test_equivalent_for_a_flow_on_a_day_without_a_snapshot(conn, run_id):
    """締めが走らない日(休日)のフローは次のスナップショットへ BOP で帰属する。"""
    _snapshot(conn, DAY, 1_000_000)
    _snapshot(conn, DAY + timedelta(days=3), 1_500_000)
    _post(conn, run_id, DAY + timedelta(days=1), 500_000, account="capital")

    rows = _assert_equivalent(conn)
    by_day = {r[1]: (r[4], r[5]) for r in rows if r[0] == "snapshot"}
    assert by_day[DAY + timedelta(days=3)] == (D(0), D(500_000))


def test_equivalent_for_pending_flows_after_the_last_snapshot(conn, run_id):
    _snapshot(conn, DAY, 1_000_000)
    _post(conn, run_id, DAY + timedelta(days=2), 250_000, account="capital")
    rows = _assert_equivalent(conn)
    assert [(r[0], r[1], r[4]) for r in rows if r[0] == "pending"] == [
        ("pending", DAY + timedelta(days=2), D(250_000))
    ]


def test_equivalent_excludes_retained_earnings(conn, run_id):
    """``retained``(損益振替)は equity だが外部フローではない — 除外が保たれている。

    シード出資(0006/0011)が最初のスナップショットに BOP で帰属するため、絶対値では
    なく**記帳前後の差分ゼロ**で見る。
    """
    _snapshot(conn, DAY, 1_000_000)
    before = _assert_equivalent(conn)
    _post(conn, run_id, DAY, 300_000, account="retained")
    assert _assert_equivalent(conn) == before


def test_equivalent_for_a_withdrawal(conn, run_id):
    _snapshot(conn, DAY, 1_000_000)
    _post(conn, run_id, DAY, -400_000, account="capital")
    rows = _assert_equivalent(conn)
    assert [r[4] for r in rows if r[0] == "snapshot" and r[1] == DAY] == [D(-400_000)]


def test_equivalent_when_flows_cancel_out_on_the_same_day(conn, run_id):
    """同日で相殺されて純額ゼロになる日は行が立たない(HAVING <> 0)。"""
    _snapshot(conn, DAY + timedelta(days=1), 1_000_000)  # シード出資は前日以前 = BOP
    before = _assert_equivalent(conn)
    _post(conn, run_id, DAY + timedelta(days=1), 200_000, account="capital")
    _post(conn, run_id, DAY + timedelta(days=1), -200_000, account="capital")
    assert _assert_equivalent(conn) == before


def test_equivalent_for_the_ops_book_with_a_differently_named_equity_account(conn, run_id):
    """OPS の拠出資本は ``owner_capital`` — 科目名がハードコードされていないことの証拠。"""
    _snapshot(conn, DAY, 500_000, book="OPS")
    _post(conn, run_id, DAY, 120_000, account="owner_capital", book="OPS")
    rows = _assert_equivalent(conn, book="OPS")
    assert [r[4] for r in rows if r[0] == "snapshot" and r[1] == DAY] == [D(120_000)]
    _assert_equivalent(conn)  # DEMO_FUND 側へ漏れていない


def test_equivalent_when_an_equity_account_is_added_at_runtime(conn, run_id):
    """``accounts`` に equity 科目が増えたら新形も拾う(正は accounts であること)。"""
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO ledger.accounts (book_id, account_id, name, category) "
            "VALUES ('DEMO_FUND', 'capital_second', '第二出資金', 'equity')"
        )
    _snapshot(conn, DAY, 1_000_000)
    _post(conn, run_id, DAY, 700_000, account="capital_second")
    rows = _assert_equivalent(conn)
    assert [r[4] for r in rows if r[0] == "snapshot" and r[1] == DAY] == [D(700_000)]


def test_equivalent_with_recon_invalidated_detail(conn, run_id):
    """detail 由来の列(recon_invalidated / _by_run)も新旧で一致する。"""
    _snapshot(
        conn, DAY, 1_000_000,
        detail={"recon_invalidated": True, "recon_invalidated_by_run": 4242},
    )
    _post(conn, run_id, DAY, 10_000, account="capital")
    rows = _assert_equivalent(conn)
    assert [(r[6], r[7]) for r in rows if r[0] == "snapshot" and r[1] == DAY] == [
        (True, 4242)
    ]


def test_load_nav_flow_data_matches_the_legacy_query(conn, run_id):
    """公開 API の返り値も旧クエリの行と対応する(dataclass への詰め替えを含めた対照)。"""
    days = [DAY + timedelta(days=i) for i in range(3)]
    for i, d in enumerate(days):
        _snapshot(conn, d, 1_000_000 + 1000 * i)
    _post(conn, run_id, days[0], 50_000, account="capital")
    _post(conn, run_id, days[1] + timedelta(days=0), 20_000, account="capital")
    _post(conn, run_id, days[-1] + timedelta(days=1), 30_000, account="capital")

    legacy = _rows(conn, _LEGACY_NAV_FLOW_SQL, "DEMO_FUND")
    data = navflow.load_nav_flow_data(conn, "DEMO_FUND")
    legacy_points = [r for r in legacy if r[0] == "snapshot"]
    legacy_pending = [r for r in legacy if r[0] == "pending"]
    assert [(p.day, p.nav, p.flow_eop, p.flow_bop) for p in data.points] == [
        (r[1], D(r[2]), D(r[4]), D(r[5])) for r in legacy_points
    ]
    assert [(p.entry_date, p.amount) for p in data.pending] == [
        (r[1], D(r[4])) for r in legacy_pending
    ]


# ── ランダム化した総当たり(固定シード) ─────────────────────────────────────
@pytest.mark.parametrize("seed", [1, 2, 3, 4, 5])
def test_equivalent_under_randomized_history(conn, run_id, seed):
    rng = random.Random(seed)
    days = [DAY + timedelta(days=i) for i in range(8)]
    accounts = ["capital", "retained"]
    for _ in range(16):
        day = rng.choice(days)
        if rng.random() < 0.4:
            _snapshot(conn, day, rng.randrange(500_000, 2_000_000))
        else:
            _post(
                conn, run_id, day,
                rng.choice([-1, 1]) * rng.randrange(0, 300_000),
                account=rng.choice(accounts),
            )
        _assert_equivalent(conn)
