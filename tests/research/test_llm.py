"""LLM 薄層のテスト: 構造化出力検証・リトライ・コスト記録(モック provider)。"""

from __future__ import annotations

import pytest

from ryza.research.llm import FixtureProvider, ProviderResult, StructuredLLM
from ryza.research.schemas import MACRO_SCHEMA, SchemaError

_VALID = {"regime": {"jp_equity": "risk_on"}, "rates_bias": 0.2,
          "fx_bias": 0.0, "refs": [1]}
_INVALID = {"regime": {}}  # 必須欠落


def _complete(llm):
    return llm.complete(
        system="sys", user="usr", schema=MACRO_SCHEMA,
        task_type="analysis.macro", model_tier="mid", model="mid-test",
    )


def test_structured_output_validates_and_returns(run):
    provider = FixtureProvider([_VALID])
    llm = StructuredLLM(provider, run)
    result = _complete(llm)
    assert result.content == _VALID
    assert result.attempts == 1
    # プロンプトが provider に渡っている。
    assert provider.calls[0]["system"] == "sys"


def test_cost_recorded_to_run(run, conn):
    provider = FixtureProvider([_VALID], tokens_in=800, tokens_out=200)
    llm = StructuredLLM(provider, run)
    _complete(llm)
    with conn.cursor() as cur:
        cur.execute("SELECT cost FROM meta.runs WHERE run_id = %s", (run.run_id,))
        cost = cur.fetchone()[0]
    assert cost["total_tokens"] == 1000
    assert cost["by_tier"]["mid"]["calls"] == 1
    # mid 単価 0.60/1k → 1000 tokens = 0.60。
    assert cost["by_tier"]["mid"]["cost_estimate"] == pytest.approx(0.60)


def test_retry_on_invalid_then_success(run, conn):
    # 1 回目 invalid → 2 回目 valid。両方コスト計上(失敗も実費)。
    provider = FixtureProvider([_INVALID, _VALID])
    llm = StructuredLLM(provider, run)
    result = _complete(llm)
    assert result.attempts == 2
    assert result.retries  # リトライ理由が記録される
    with conn.cursor() as cur:
        cur.execute("SELECT cost FROM meta.runs WHERE run_id = %s", (run.run_id,))
        cost = cur.fetchone()[0]
    assert cost["by_tier"]["mid"]["calls"] == 2


def test_exhausted_retries_raises(run):
    provider = FixtureProvider([_INVALID])  # 常に invalid
    llm = StructuredLLM(provider, run)
    with pytest.raises(SchemaError):
        llm.complete(
            system="s", user="u", schema=MACRO_SCHEMA,
            task_type="analysis.macro", model_tier="mid", model="m", max_retries=2,
        )


def test_provider_result_passthrough(run):
    pr = ProviderResult(content=_VALID, tokens_in=10, tokens_out=5, raw_text="{}")
    provider = FixtureProvider([pr])
    llm = StructuredLLM(provider, run)
    result = _complete(llm)
    assert result.tokens_in == 10 and result.tokens_out == 5
