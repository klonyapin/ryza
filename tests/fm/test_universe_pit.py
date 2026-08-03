"""ユニバースの point-in-time 化(0026 — 独立役員審査 T-017 C-4 / C-16〜C-20)。

C-4 の指摘は2つある:

1. **look-ahead**: 分類が後から付いた/変わった銘柄を、過去の判断時点で見てしまう
2. **静かに空になる**: 現在値表を ``as_of <= 判断時点`` で絞る実装では、分類が判断時点
   より後に書かれた銘柄が丸ごと落ち、リプレイのユニバースが理由の説明なく空になる

後続審査(C-16 / C-17)はこの 2 つが**是正後も別経路で再現する**ことを実測で示した:
バックデート追記(今日 1 行足すだけで過去のリプレイが変わる)と、現在値キャッシュの
as_of 巻き戻しである。本ファイルはその実測ケースを回帰テストとして固定する。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from ryza.fm import base
from ryza.ips import load_and_validate
from ryza.risk.classify import Classification, history_coverage_since

FM = "ben"


@pytest.fixture
def mandate():
    return load_and_validate()[1][FM]


def _universe_ids(conn, mandate, *, as_of) -> set[int]:
    return {c.instrument_id for c in base.load_universe(conn, mandate, as_of=as_of).candidates}


def _tagged(*tags: str) -> Classification:
    return Classification(
        universe_tags=tags,
        instrument_flags=(),
        is_single_name=True,
        product="listed_equity_cash",
        unit_size=Decimal(100),
    )


def test_later_classification_change_does_not_leak_into_the_past(
    conn, mandate, instrument, classify
):
    """分類の**変更**は過去に漏れない — 当時のタグでユニバースが決まる。"""
    iid = instrument(symbol="6501.T")
    t_old = datetime.now(UTC) - timedelta(days=60)
    t_new = datetime.now(UTC) - timedelta(days=5)
    classify(iid, universe_tags=("jp_equity_cash",), as_of=t_old, recorded_at=t_old)
    # 後日ユニバースから外れた(上場廃止予備軍・商品性の再判定など)。
    classify(iid, universe_tags=(), as_of=t_new, recorded_at=t_new)

    assert _universe_ids(conn, mandate, as_of=t_new - timedelta(days=1)) == {iid}
    assert iid not in _universe_ids(conn, mandate, as_of=datetime.now(UTC))


def test_classification_added_later_is_invisible_in_the_past(
    conn, mandate, instrument, classify
):
    """分類が後から付いた銘柄を過去の候補にしない(look-ahead 排除)。"""
    iid = instrument(symbol="6502.T")
    recent = datetime.now(UTC) - timedelta(days=5)
    classify(iid, universe_tags=("jp_equity_cash",), as_of=recent, recorded_at=recent)

    assert _universe_ids(conn, mandate, as_of=datetime.now(UTC) - timedelta(days=30)) == set()
    assert iid in _universe_ids(conn, mandate, as_of=datetime.now(UTC))


def test_backdated_append_cannot_change_a_past_replay(
    conn, mandate, instrument, classify, record_classification_history, run
):
    """**審査 C-16 の実測ケース**: 今日 1 行追記しても過去のリプレイは変わらない。

    審査者の実測: 30 日前に ``jp_equity_cash`` を付けた銘柄が 20 日前リプレイで
    ``{iid}`` → 25 日前 as_of の行を今日追記すると ``set()`` に変わった。追記オンリー
    トリガはこれを止めない(改変は UPDATE ではなく**追記**で成立する)。読出しを
    bitemporal にして初めて塞がる。
    """
    iid = instrument(symbol="6507.T")
    t30 = datetime.now(UTC) - timedelta(days=30)
    replay = datetime.now(UTC) - timedelta(days=20)
    classify(iid, universe_tags=("jp_equity_cash",), as_of=t30, recorded_at=t30)
    assert _universe_ids(conn, mandate, as_of=replay) == {iid}

    # 25 日前の知識時点を主張する行を**今日**追記する(ユニバースから外す内容)。
    record_classification_history(
        conn, iid, _tagged(),
        run_id=run.run_id,
        as_of=datetime.now(UTC) - timedelta(days=25),
        created_at=datetime.now(UTC),
    )

    # 20 日前のリプレイは変わらない(当時それは記録されていなかった)。
    assert _universe_ids(conn, mandate, as_of=replay) == {iid}
    # 今日の判断には反映される(記録済みの最新知識)。
    assert iid not in _universe_ids(conn, mandate, as_of=datetime.now(UTC))


def test_stale_current_cache_cannot_change_the_universe(conn, mandate, instrument, classify):
    """**審査 C-17 の実測ケース**: 現在値キャッシュの as_of 巻き戻しに影響されない。

    現在値行の as_of は上書き更新で古い値にも未来にも動き得るため、「現在値表を見て
    高速経路の等価性を判定する」設計は成立しない。読出しを常に履歴経路にしたことで、
    現在値表がどんな状態でも判断側のユニバースは変わらない。
    """
    iid = instrument(symbol="6508.T")
    t10 = datetime.now(UTC) - timedelta(days=10)
    classify(iid, universe_tags=("jp_equity_cash",), as_of=t10, recorded_at=t10)

    with conn.cursor() as cur:  # 現在値表だけを動かす(履歴には触れない)
        cur.execute(
            "UPDATE market.instrument_classification SET as_of = %s WHERE instrument_id = %s",
            (datetime.now(UTC) + timedelta(days=1), iid),
        )
    assert _universe_ids(conn, mandate, as_of=datetime.now(UTC)) == {iid}


def test_universe_read_reports_the_path_actually_used(conn, mandate):
    """経路名は読出し結果に同梱される(サマリのための再導出をしない — 審査 C-20)。"""
    read = base.load_universe(conn, mandate, as_of=datetime.now(UTC))
    assert read.source == base.UNIVERSE_SOURCE == "history"
    status = base.universe_pit_status(conn, as_of=datetime.now(UTC), source=read.source)
    assert status["source"] == read.source


def test_pit_status_marks_uncovered_as_of(conn, instrument, classify):
    """履歴が始まる前の as_of には E6 未達の但し書きが付き、以降では外れる。"""
    iid = instrument(symbol="6509.T")
    started = datetime.now(UTC) - timedelta(days=10)
    classify(iid, universe_tags=("jp_equity_cash",), as_of=started, recorded_at=started)
    since = history_coverage_since(conn)
    assert since is not None

    uncovered = base.universe_pit_status(
        conn, as_of=since - timedelta(days=1), source=base.UNIVERSE_SOURCE
    )
    assert uncovered["e6_covered"] is False
    assert "E6" in uncovered["note"]

    covered = base.universe_pit_status(
        conn, as_of=since + timedelta(seconds=1), source=base.UNIVERSE_SOURCE
    )
    assert covered["e6_covered"] is True and covered["note"] is None


def test_empty_universe_is_never_silent(conn, mandate, instrument, classify):
    """空ユニバースには必ず理由(カバレッジ)が添う — 「静かに空」を潰す。"""
    iid = instrument(symbol="6505.T")
    started = datetime.now(UTC) - timedelta(days=1)
    classify(iid, universe_tags=("jp_equity_cash",), as_of=started, recorded_at=started)
    past = history_coverage_since(conn) - timedelta(days=1)

    assert _universe_ids(conn, mandate, as_of=past) == set()
    status = base.universe_pit_status(conn, as_of=past, source=base.UNIVERSE_SOURCE)
    assert status["e6_covered"] is False and status["note"]


def test_replay_flag_is_reported(conn):
    """判断時点が過去日かどうかはサマリに残す(経路判定には使わない)。"""
    assert base.is_replay(datetime.now(UTC) - timedelta(days=1)) is True
    assert base.is_replay(datetime.now(UTC)) is False
