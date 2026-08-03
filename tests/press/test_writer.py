"""執筆(StructuredLLM 経由)の単体テスト。LLM は Echo プロバイダを注入。"""

from __future__ import annotations

from ryza.press import writer
from ryza.press.linter import lint_topic


def test_write_topic_produces_lintable_morning_topic(make_press_llm):
    llm, provider = make_press_llm()
    material = {"title": "半導体主導", "refs": [10], "source_kind": "document"}
    wr = writer.write_topic(llm, material)
    report = lint_topic(wr.topic, mode="morning", valid_source_ids={10})
    assert report.ok, report.reasons()
    # 玲音のペルソナが system に注入されている。
    assert "玲音" in provider.calls[0]["system"]
    # コストが記録されている(dept_tag=press)。
    assert wr.llm.cost_estimate > 0


def test_write_flash_prediction_has_label(make_press_llm):
    llm, _ = make_press_llm()
    wr = writer.write_flash(llm, {"summary": "予兆がある", "refs": [10]},
                            is_prediction=True)
    assert wr.topic.prediction is not None
    assert wr.topic.prediction.confidence == 0.6
    report = lint_topic(wr.topic, mode="flash", valid_source_ids={10}, is_prediction=True)
    assert report.ok, report.reasons()


def test_persona_loader_reads_lain():
    persona = writer.load_press_persona()
    assert "玲音" in persona
    assert "あたし" in persona  # 口調ガイドが結合されている
