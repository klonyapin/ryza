"""文体リンターの決定論単体テスト(30-press §4)。DB・LLM 不使用。

受け入れ基準: 理想形 4→3→2→1→3→5 合格 / 非単谷・先頭<3・末尾≠5・L-4 出典欠落・L-7 欠落が不合格。
"""

from __future__ import annotations

import pytest

from ryza.press.linter import (
    Prediction,
    Sentence,
    Topic,
    TradeImplication,
    is_u_shape,
    lint_topic,
)


def _ti() -> TradeImplication:
    return TradeImplication(action="watch", target="日経平均", condition="上抜けで追随")


def _sent(level: int, *, src: list[int] | None = None, text: str = "文" * 10) -> Sentence:
    return Sentence(text=text, level=level, source_ids=src or [])


def _u_topic(*, refs: list[int] | None = None) -> Topic:
    """理想形 4→3→2→1→3→5(各 40 字・計 240 字)の合格トピック。"""
    refs = refs if refs is not None else [10]
    body = "本文" * 20  # 40 文字
    return Topic(
        argument="アーギュメント一文である。",
        sentences=[
            _sent(4, text=body),
            _sent(3, text=body),
            _sent(2, text=body),
            _sent(1, src=refs, text=body),
            _sent(3, text=body),
            _sent(5, text=body),
        ],
        trade_implication=_ti(),
    )


# ── U字判定(純関数)──────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    "levels,expected",
    [
        ([4, 3, 2, 1, 3, 5], True),   # 理想形
        ([3, 2, 1, 5], True),         # 一般形(先頭=3)
        ([5, 1, 5], True),            # 谷=1・最小構成
        ([4, 3, 2, 2, 3, 5], True),   # 谷=2
        ([3, 4, 2, 1, 5], False),     # 非単谷(先頭で増加)
        ([2, 1, 5], False),           # 先頭<3
        ([4, 2, 1, 3], False),        # 末尾≠5
        ([4, 3, 1, 2, 1, 5], False),  # 谷の後で減少(非単谷)
        ([4, 3, 4, 5], False),        # 谷が {1,2} でない(min=3)
        ([], False),
    ],
)
def test_is_u_shape(levels, expected):
    assert is_u_shape(levels) is expected


# ── 合格ケース ────────────────────────────────────────────────────────────────
def test_ideal_topic_passes():
    report = lint_topic(_u_topic(), mode="morning", valid_source_ids={10})
    assert report.ok, report.reasons()


# ── 不合格ケース ──────────────────────────────────────────────────────────────
def test_non_single_valley_fails():
    t = _u_topic()
    # level 系列を非単谷にする。
    t = Topic(argument=t.argument,
              sentences=[_sent(3), _sent(4), _sent(2), _sent(1), _sent(5)],
              trade_implication=_ti())
    report = lint_topic(t, mode="morning")
    assert not report.ok
    assert any(v.rule == "L-2" for v in report.violations)


def test_head_below_3_fails():
    body = "本文" * 20
    t = Topic(argument="論点。",
              sentences=[_sent(2, text=body), _sent(1, src=[1], text=body),
                         _sent(3, text=body), _sent(5, text=body)],
              trade_implication=_ti())
    report = lint_topic(t, mode="morning", valid_source_ids={1})
    assert not report.ok
    assert any(v.rule == "L-2" for v in report.violations)


def test_tail_not_5_fails():
    body = "本文" * 20
    t = Topic(argument="論点。",
              sentences=[_sent(4, text=body), _sent(2, text=body),
                         _sent(1, src=[1], text=body), _sent(3, text=body)],
              trade_implication=_ti())
    report = lint_topic(t, mode="morning", valid_source_ids={1})
    assert not report.ok
    assert any(v.rule == "L-2" for v in report.violations)


def test_l4_missing_source_fails():
    t = _u_topic(refs=[])  # level 1 の文に source_ids 無し
    report = lint_topic(t, mode="morning")
    assert not report.ok
    assert any(v.rule == "L-4" for v in report.violations)


def test_l4_unknown_source_id_fails():
    t = _u_topic(refs=[999])  # 存在しない ID
    report = lint_topic(t, mode="morning", valid_source_ids={10, 11})
    assert not report.ok
    assert any(v.rule == "L-4" and "存在しない" in v.message for v in report.violations)


def test_l7_missing_trade_implication_fails():
    t = _u_topic()
    t = Topic(argument=t.argument, sentences=t.sentences, trade_implication=None)
    report = lint_topic(t, mode="morning", valid_source_ids={10})
    assert not report.ok
    assert any(v.rule == "L-7" for v in report.violations)


def test_l7_bad_action_fails():
    t = _u_topic()
    t = Topic(argument=t.argument, sentences=t.sentences,
              trade_implication=TradeImplication(action="爆買い", target="X", condition="Y"))
    report = lint_topic(t, mode="morning", valid_source_ids={10})
    assert not report.ok
    assert any(v.rule == "L-7" for v in report.violations)


def test_l1_argument_empty_fails():
    t = _u_topic()
    t = Topic(argument="   ", sentences=t.sentences, trade_implication=_ti())
    report = lint_topic(t, mode="morning", valid_source_ids={10})
    assert not report.ok
    assert any(v.rule == "L-1" for v in report.violations)


def test_l1_argument_duplicate_of_body_fails():
    body = "本文" * 20
    t = Topic(argument=body,  # 本文の一文と一致
              sentences=[_sent(4, text=body), _sent(3, text=body), _sent(2, text=body),
                         _sent(1, src=[10], text=body), _sent(3, text=body), _sent(5, text=body)],
              trade_implication=_ti())
    report = lint_topic(t, mode="morning", valid_source_ids={10})
    assert any(v.rule == "L-1" for v in report.violations)


def test_l3_too_short_fails():
    t = Topic(argument="論点。",
              sentences=[_sent(4, text="短"), _sent(3, text="短"), _sent(2, text="短"),
                         _sent(1, src=[10], text="短"), _sent(3, text="短"), _sent(5, text="短")],
              trade_implication=_ti())
    report = lint_topic(t, mode="morning", valid_source_ids={10})
    assert not report.ok
    assert any(v.rule == "L-3" for v in report.violations)


def test_l3_too_few_sentences_fails():
    body = "本文" * 100  # 字数は足りるが文数不足
    t = Topic(argument="論点。",
              sentences=[_sent(3, text=body), _sent(1, src=[10], text=body), _sent(5, text=body)],
              trade_implication=_ti())
    report = lint_topic(t, mode="morning", valid_source_ids={10})
    assert not report.ok
    assert any(v.rule == "L-3" and "文数" in v.message for v in report.violations)


# ── 速報(短縮形)────────────────────────────────────────────────────────────
def test_flash_fact_passes():
    t = Topic(
        argument="相場が動いたよ。",
        sentences=[Sentence("価格が急落した。", 1, [10]), Sentence("……明日のことだよ。", 5, [])],
    )
    report = lint_topic(t, mode="flash", valid_source_ids={10})
    assert report.ok, report.reasons()


def test_flash_prediction_requires_label():
    t = Topic(
        argument="予兆があるみたい。",
        sentences=[Sentence("シグナルが揃った。", 1, [10]), Sentence("……続くよ。", 5, [])],
        prediction=None,  # L-5 欠落
    )
    report = lint_topic(t, mode="flash", valid_source_ids={10}, is_prediction=True)
    assert not report.ok
    assert any(v.rule == "L-5" for v in report.violations)


def test_flash_prediction_with_label_passes():
    t = Topic(
        argument="予兆があるみたい。",
        sentences=[Sentence("シグナルが揃った。", 1, [10]), Sentence("……続くよ。", 5, [])],
        prediction=Prediction(claim="円安が続く", confidence=0.6, verify_by="2026-08-10T00:00:00Z"),
    )
    report = lint_topic(t, mode="flash", valid_source_ids={10}, is_prediction=True)
    assert report.ok, report.reasons()


def test_flash_missing_fact_level_fails():
    t = Topic(argument="論点。", sentences=[Sentence("観察。", 2, []), Sentence("含意。", 5, [])])
    report = lint_topic(t, mode="flash")
    assert not report.ok
    assert any(v.rule == "L-2" for v in report.violations)
