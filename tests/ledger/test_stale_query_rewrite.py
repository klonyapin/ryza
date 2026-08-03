"""stale 検出クエリの書き換えが**意味を変えていない**ことの対照テスト。

``closing._STALE_SNAPSHOTS_SQL`` は 2026-08-04 に書き換えた(reminders
``reclose-stale-pruning``)。旧形はスナップショット 1 日ごとに相関サブクエリを回すため
コストが スナップショット数 × 仕訳数 で伸び、索引では直らないことが実測で確定していた
(``migrations/0027_query_indexes.sql`` 索引2「効かない用途」節)。

**このテストが固定するのは速さではなく等価性である。** 旧 SQL をこのファイルに凍結し、
同一データに対する新旧の結果(行の集合・列の値・並び)が完全一致することを、締めと遅延
記帳のあらゆる組み合わせで確認する。検出漏れは「遅延仕訳を取り込んだ日が永久に再締め
されない」という会計上の欠陥になるため、速さのためのいかなる近似も許されない。

``test_naive_max_watermark_pruning_would_miss_a_late_entry`` は、reminders と 0027 の
コメントが案として挙げていた枝刈り(**最大**水位を閾値にする)が実際に検出を落とすことを
反例で示す — 採用しなかった理由の証拠であり、将来「もっと速くできる」と再提案された
ときの反証材料である。
"""

from __future__ import annotations

import json
import random
from datetime import date, timedelta
from decimal import Decimal

import pytest

from ryza.ledger import closing, posting

D = Decimal
DAY = date(2026, 9, 1)

#: 書き換え**前**の SQL(a760170 時点の ``closing._STALE_SNAPSHOTS_SQL`` の逐語コピー)。
#: 参照用に凍結する — 意味論の正は今もこちらであり、新形はこれと同じ答えを返す限りにおいて
#: 正しい。**この定数を新形に合わせて更新してはならない**(対照が消える)。
_LEGACY_STALE_SNAPSHOTS_SQL = """
WITH snap AS (
    SELECT s.snap_date,
           (s.detail -> 'producer' -> 'input_refs' ->> %(wm_key)s)::bigint
               AS stored_watermark,
           row_number() OVER (ORDER BY s.snap_date DESC) - 1 AS age_business_days
    FROM ledger.nav_snapshots s
    WHERE s.book_id = %(book)s AND s.snap_date <= %(through)s
), measured AS (
    SELECT snap.snap_date, snap.stored_watermark, snap.age_business_days,
           (SELECT max(je.entry_id) FROM ledger.journal_entries je
             WHERE je.book_id = %(book)s AND je.entry_date <= snap.snap_date)
               AS current_watermark,
           EXISTS (
               SELECT 1 FROM ledger.journal_entries je
               JOIN ledger.journal_lines jl ON jl.entry_id = je.entry_id
               WHERE je.book_id = %(book)s AND je.entry_date <= snap.snap_date
                 AND je.entry_id > snap.stored_watermark
                 AND jl.instrument_id IS NOT NULL
           ) AS position_changing_late
    FROM snap
)
SELECT snap_date, stored_watermark, current_watermark, age_business_days,
       position_changing_late
FROM measured
WHERE stored_watermark IS NULL
   OR coalesce(current_watermark, 0) > stored_watermark
ORDER BY snap_date
"""


def _rows(conn, sql: str, through: date, book: str = "DEMO_FUND") -> list[tuple]:
    with conn.cursor() as cur:
        cur.execute(
            sql, {"book": book, "through": through, "wm_key": closing.WATERMARK_KEY}
        )
        return cur.fetchall()


def _assert_equivalent(conn, through: date, book: str = "DEMO_FUND") -> list[tuple]:
    """新旧 SQL の結果が完全一致することを確認し、結果を返す。"""
    legacy = _rows(conn, _LEGACY_STALE_SNAPSHOTS_SQL, through, book)
    current = _rows(conn, closing._STALE_SNAPSHOTS_SQL, through, book)
    assert current == legacy, f"through={through} book={book}"
    return current


def _watermark(conn, day: date, book: str = "DEMO_FUND") -> int:
    """その日までの仕訳の水位(締めが記録するはずの値)。"""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT coalesce(max(entry_id), 0) FROM ledger.journal_entries "
            "WHERE book_id = %s AND entry_date <= %s",
            (book, day),
        )
        return int(cur.fetchone()[0])


def _put_snapshot(
    conn, day: date, watermark: int | None, book: str = "DEMO_FUND"
) -> None:
    """スナップショットを直接書く(水位を任意に作れるテスト専用の近道)。

    ``watermark=None`` は本機能より前に書かれた日(判定材料が無く fail-safe で stale)。
    """
    refs = {} if watermark is None else {closing.WATERMARK_KEY: watermark}
    detail = {"producer": {"job": "test", "input_refs": refs}}
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO ledger.nav_snapshots (book_id, snap_date, nav, status, detail)
            VALUES (%s, %s, 0, 'provisional', %s)
            ON CONFLICT (book_id, snap_date)
            DO UPDATE SET detail = EXCLUDED.detail
            """,
            (book, day, json.dumps(detail)),
        )


def _close(conn, day: date, book: str = "DEMO_FUND") -> None:
    """その日の締めを模す(実際の締めと同じ水位を刻む)。"""
    _put_snapshot(conn, day, _watermark(conn, day, book), book)


def _post(conn, run_id, day: date, *, instrument: bool = False, book="DEMO_FUND") -> int:
    """仕訳を 1 本立てる。``instrument=True`` は建玉を動かす明細を含む。"""
    amount = D(1000)
    cash, capital = ("cash", "capital") if book == "DEMO_FUND" else (
        "cash_bank", "owner_capital"
    )
    lines: list[dict] = (
        [
            {"account_id": "securities", "debit": amount, "currency": "JPY",
             "instrument_id": 1001},
            {"account_id": capital, "credit": amount, "currency": "JPY"},
        ]
        if instrument
        else [
            {"account_id": cash, "debit": amount, "currency": "JPY"},
            {"account_id": capital, "credit": amount, "currency": "JPY"},
        ]
    )
    return posting.post_entry(
        conn,
        book_id=book,
        entry_date=day,
        description="テスト仕訳",
        lines=lines,
        evidence={"kind": "decision", "payload": {"test": "stale"}, "source": "test"},
        run_id=run_id,
        posted_by="test.stale_query",
    )


# ── 個別シナリオ ────────────────────────────────────────────────────────────
def test_equivalent_when_no_snapshots_exist(conn, run_id):
    _post(conn, run_id, DAY)
    assert _assert_equivalent(conn, DAY + timedelta(days=5)) == []


def test_equivalent_when_nothing_is_stale(conn, run_id):
    days = [DAY + timedelta(days=i) for i in range(5)]
    for d in days:
        _post(conn, run_id, d)
        _close(conn, d)
    assert _assert_equivalent(conn, days[-1]) == []


def test_equivalent_for_a_late_capital_entry(conn, run_id):
    days = [DAY + timedelta(days=i) for i in range(4)]
    for d in days:
        _close(conn, d)
    _post(conn, run_id, days[0])  # 最古の日に遅れて記帳(建玉は動かない)

    rows = _assert_equivalent(conn, days[-1])
    assert [r[0] for r in rows] == days  # その日以降すべてが stale
    assert all(r[4] is False for r in rows)  # 建玉は動いていない


def test_equivalent_for_a_late_position_changing_entry(conn, run_id):
    days = [DAY + timedelta(days=i) for i in range(4)]
    for d in days:
        _close(conn, d)
    _post(conn, run_id, days[1], instrument=True)

    rows = _assert_equivalent(conn, days[-1])
    assert [r[0] for r in rows] == days[1:]
    assert all(r[4] is True for r in rows)


def test_equivalent_when_both_kinds_of_late_entries_coexist(conn, run_id):
    days = [DAY + timedelta(days=i) for i in range(5)]
    for d in days:
        _close(conn, d)
    _post(conn, run_id, days[0])                    # 建玉を動かさない
    _post(conn, run_id, days[3], instrument=True)   # 建玉を動かす

    rows = _assert_equivalent(conn, days[-1])
    assert {r[0]: r[4] for r in rows} == {
        days[0]: False, days[1]: False, days[2]: False, days[3]: True, days[4]: True
    }


def test_equivalent_for_snapshots_without_a_watermark(conn, run_id):
    """水位を持たない日は fail-safe で stale。建玉性は主張しない(NULL 比較)。"""
    _post(conn, run_id, DAY, instrument=True)
    _put_snapshot(conn, DAY, None)
    _close(conn, DAY + timedelta(days=1))

    rows = _assert_equivalent(conn, DAY + timedelta(days=1))
    assert [(r[0], r[1], r[4]) for r in rows] == [(DAY, None, False)]


def test_equivalent_when_all_snapshots_lack_watermarks(conn, run_id):
    """全日が水位なし = 枝刈りの下限が NULL になる境界(全件 stale・建玉性は false)。"""
    days = [DAY + timedelta(days=i) for i in range(3)]
    for d in days:
        _post(conn, run_id, d, instrument=True)
        _put_snapshot(conn, d, None)

    rows = _assert_equivalent(conn, days[-1])
    assert [r[0] for r in rows] == days
    assert all(r[1] is None and r[4] is False for r in rows)


def test_equivalent_when_a_snapshot_predates_every_entry(conn, run_id):
    """仕訳が 1 本も無い日(current_watermark が NULL)も同じ扱いになる。"""
    early = date(2020, 1, 6)
    _put_snapshot(conn, early, 0)
    _put_snapshot(conn, early + timedelta(days=1), None)
    rows = _assert_equivalent(conn, early + timedelta(days=1))
    assert [(r[0], r[2]) for r in rows] == [(early + timedelta(days=1), None)]


def test_equivalent_across_the_through_boundary(conn, run_id):
    days = [DAY + timedelta(days=i) for i in range(6)]
    for d in days:
        _close(conn, d)
    _post(conn, run_id, days[1], instrument=True)
    for through in days:  # age_business_days は through で変わる — 各点で照合する
        _assert_equivalent(conn, through)


def test_equivalent_for_a_book_with_its_own_entries(conn, run_id):
    """帳簿は互いに干渉しない(book_id の絞り込みが新形でも効いている)。"""
    _close(conn, DAY)
    _put_snapshot(conn, DAY, 0, book="OPS")
    _post(conn, run_id, DAY, book="OPS")

    assert _assert_equivalent(conn, DAY) == []  # DEMO_FUND は無風
    ops = _assert_equivalent(conn, DAY, book="OPS")
    assert [r[0] for r in ops] == [DAY]


def test_equivalent_when_the_late_entry_is_dated_before_the_oldest_snapshot(conn, run_id):
    """スナップショットより古い日付の遅延仕訳(枝刈りの下限を下回る entry_date)。"""
    days = [DAY + timedelta(days=i) for i in range(3)]
    for d in days:
        _close(conn, d)
    _post(conn, run_id, DAY - timedelta(days=10), instrument=True)

    rows = _assert_equivalent(conn, days[-1])
    assert [r[0] for r in rows] == days
    assert all(r[4] is True for r in rows)


# ── 採らなかった枝刈りの反例 ────────────────────────────────────────────────
def _naive_max_watermark_candidates(conn, through: date) -> list[date]:
    """reminders / 0027 が案として挙げた枝刈りの候補日(**最大**水位を閾値にする)。

    「全スナップショットの stored_watermark の最大値より後ろの entry_id を持つ最古の
    entry_date を求め、その日以降のスナップショットだけを stale 候補にする」。
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            WITH snap AS (
                SELECT s.snap_date,
                       (s.detail -> 'producer' -> 'input_refs' ->> %(wm_key)s)::bigint
                           AS stored_watermark
                FROM ledger.nav_snapshots s
                WHERE s.book_id = %(book)s AND s.snap_date <= %(through)s
            )
            SELECT snap_date FROM snap
            WHERE stored_watermark IS NULL
               OR snap_date >= (
                   SELECT min(je.entry_date) FROM ledger.journal_entries je
                   WHERE je.book_id = %(book)s
                     AND je.entry_id > (SELECT max(stored_watermark) FROM snap)
               )
            ORDER BY snap_date
            """,
            {"book": "DEMO_FUND", "through": through, "wm_key": closing.WATERMARK_KEY},
        )
        return [r[0] for r in cur.fetchall()]


def test_naive_max_watermark_pruning_would_miss_a_late_entry(conn, run_id):
    """**最大**水位で枝刈りすると遅延仕訳の日が候補から落ちる(採用しなかった理由)。

    9/1 の締め(水位 W1)の後に 9/1 付けの仕訳が立ち、その仕訳を見た状態で 9/2 の締めが
    走る(水位 W2 > その仕訳の entry_id)。9/1 は依然 stale だが、**最大**水位 W2 より
    後ろの仕訳は存在しないため、最大水位を閾値にする枝刈りでは候補がゼロになる。
    採用した形(日ごと集約)は近似ではないのでこの日を落とさない。
    """
    d1, d2 = DAY, DAY + timedelta(days=1)
    _close(conn, d1)                     # 9/1 の締め: 水位 W1
    _post(conn, run_id, d1)              # 9/1 付けの遅延仕訳(entry_id = L > W1)
    _post(conn, run_id, d2)              # 9/2 に立った通常の仕訳(entry_id > L)
    _close(conn, d2)                     # 9/2 の締め: 水位 W2 > L

    detected = [r[0] for r in _assert_equivalent(conn, d2)]
    assert detected == [d1], "9/1 の遅延仕訳を検出できていない"
    assert _naive_max_watermark_candidates(conn, d2) == [], (
        "反例が成立していない — 最大水位の枝刈りが 9/1 を落とすことを前提にしている"
    )


# ── ランダム化した総当たり(固定シード) ─────────────────────────────────────
@pytest.mark.parametrize("seed", [1, 2, 3, 4, 5])
def test_equivalent_under_randomized_history(conn, run_id, seed):
    """締め・遅延記帳・水位欠落をランダムに混ぜても新旧が一致する。

    シードは固定(再現性)。各ステップ後に全 through で照合するため、1 シードあたり
    数十回の比較になる。
    """
    rng = random.Random(seed)
    days = [DAY + timedelta(days=i) for i in range(6)]
    for _ in range(14):
        op = rng.random()
        day = rng.choice(days)
        if op < 0.45:
            _post(conn, run_id, day, instrument=rng.random() < 0.5)
        elif op < 0.9:
            _close(conn, day)
        else:
            _put_snapshot(conn, day, None)
        for through in days:
            _assert_equivalent(conn, through)
