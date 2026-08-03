"""ユニバースの point-in-time 化(0026 — 独立役員審査 T-017 C-4 の是正)。

C-4 の指摘は2つある:

1. **look-ahead**: 分類が後から付いた/変わった銘柄を、過去の判断時点で見てしまう
2. **静かに空になる**: 現在値表を ``as_of <= 判断時点`` で絞る旧実装では、分類が
   判断時点より後に書かれた銘柄が丸ごと落ち、リプレイのユニバースが理由の説明なく
   空になる

本ファイルは (1) を履歴読出しで、(2) を **E6 の但し書き**(``universe_pit_status``)で
塞いだことを固定する。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from ryza.fm import base
from ryza.ips import load_and_validate
from ryza.risk.classify import history_coverage_since

FM = "ben"


@pytest.fixture
def mandate():
    return load_and_validate()[1][FM]


def _universe_ids(conn, mandate, *, as_of, use_history=None) -> set[int]:
    return {
        c.instrument_id
        for c in base.load_universe(conn, mandate, as_of=as_of, use_history=use_history)
    }


def test_later_classification_change_does_not_leak_into_the_past(
    conn, mandate, instrument, classify
):
    """分類の**変更**は過去に漏れない — 当時のタグでユニバースが決まる。"""
    iid = instrument(symbol="6501.T")
    t_old = datetime.now(UTC) - timedelta(days=60)
    t_new = datetime.now(UTC) - timedelta(days=5)
    classify(iid, universe_tags=("jp_equity_cash",), as_of=t_old)
    # 後日ユニバースから外れた(上場廃止予備軍・商品性の再判定など)。
    classify(iid, universe_tags=(), as_of=t_new)

    replay = t_new - timedelta(days=1)
    assert _universe_ids(conn, mandate, as_of=replay) == {iid}
    # 当日(通常運転・現在値キャッシュ経路)では外れている。
    assert iid not in _universe_ids(conn, mandate, as_of=datetime.now(UTC))


def test_classification_added_later_is_invisible_in_the_past(
    conn, mandate, instrument, classify
):
    """分類が後から付いた銘柄を過去の候補にしない(look-ahead 排除)。"""
    iid = instrument(symbol="6502.T")
    classify(iid, universe_tags=("jp_equity_cash",), as_of=datetime.now(UTC) - timedelta(days=5))

    past = datetime.now(UTC) - timedelta(days=30)
    assert _universe_ids(conn, mandate, as_of=past) == set()
    assert iid in _universe_ids(conn, mandate, as_of=datetime.now(UTC))


def test_history_and_current_paths_agree_for_today(conn, mandate, instrument, classify):
    """当日 as_of では現在値キャッシュ(高速経路)と履歴経路の結果が一致する。

    一致しないなら「高速経路」ではなく別の意味論になっている。
    """
    ids = set()
    for symbol in ("6503.T", "6504.T"):
        iid = instrument(symbol=symbol)
        classify(iid, universe_tags=("jp_equity_cash",))
        ids.add(iid)
    as_of = datetime.now(UTC)
    assert _universe_ids(conn, mandate, as_of=as_of, use_history=False) == ids
    assert _universe_ids(conn, mandate, as_of=as_of, use_history=True) == ids


def test_replay_flag_switches_the_read_path(conn):
    """経路の自動判定は JST 日付基準(過去日=リプレイ)。"""
    assert base.is_replay(datetime.now(UTC) - timedelta(days=1)) is True
    assert base.is_replay(datetime.now(UTC)) is False


def test_same_day_newer_classification_falls_back_to_history(
    conn, mandate, instrument, classify
):
    """当日でも「判断時点より後の分類」があれば履歴経路に倒す。

    現在値キャッシュは銘柄あたり1行しかないため、後から書かれた分類があると
    ``as_of <= 判断時点`` のフィルタでその銘柄が**丸ごと**落ちる(当時の分類は
    あったのに候補から静かに消える — 審査 C-4 の①の同日版)。
    """
    iid = instrument(symbol="6506.T")
    morning = datetime.now(UTC) - timedelta(hours=6)
    classify(iid, universe_tags=("jp_equity_cash",), as_of=morning - timedelta(hours=1))
    # 同日午後に再分類(内容は同じでもタグ追加でも as_of が進めば現在値行は上書きされる)。
    classify(iid, universe_tags=("jp_equity_cash", "liquid_equity"), as_of=datetime.now(UTC))

    assert base.resolves_from_history(conn, as_of=morning) is True
    # 現在値キャッシュだけを見ると空になる(旧実装の穴)。
    assert _universe_ids(conn, mandate, as_of=morning, use_history=False) == set()
    # 自動判定は履歴へ倒れ、当時の分類で候補に残る。
    assert _universe_ids(conn, mandate, as_of=morning) == {iid}


def test_pit_status_marks_uncovered_as_of(conn):
    """履歴が始まる前の as_of には E6 未達の但し書きが付き、以降では外れる。"""
    since = history_coverage_since(conn)
    assert since is not None

    uncovered = base.universe_pit_status(conn, as_of=since - timedelta(days=1))
    assert uncovered["e6_covered"] is False
    assert "E6" in uncovered["note"]
    assert uncovered["source"] == "history"  # 過去 as_of は履歴経路

    covered = base.universe_pit_status(conn, as_of=since + timedelta(seconds=1))
    assert covered["e6_covered"] is True and covered["note"] is None


def test_empty_replay_universe_is_not_silent(conn, mandate, instrument, classify):
    """空ユニバースには必ず理由(カバレッジ)が添う — 「静かに空」を潰す。"""
    iid = instrument(symbol="6505.T")
    classify(iid, universe_tags=("jp_equity_cash",), as_of=datetime.now(UTC) - timedelta(days=1))
    past = history_coverage_since(conn) - timedelta(days=1)

    assert _universe_ids(conn, mandate, as_of=past) == set()
    status = base.universe_pit_status(conn, as_of=past)
    assert status["e6_covered"] is False and status["note"]
