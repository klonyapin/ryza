"""providers のテスト(T-013)。

AnthropicProvider のリクエスト組立・レスポンス解釈・HTTP リトライ・コスト記録を **HTTP モック**で
検証する(実 API・実ネットワークは呼ばない)。LLMConfig・extract_json_object・DryRunProvider も。
"""

from __future__ import annotations

import json

import pytest

from ryza.press.writer import MORNING_TOPIC_SCHEMA
from ryza.research.llm import StructuredLLM
from ryza.research.providers import (
    AnthropicProvider,
    DryRunProvider,
    LLMConfig,
    ProviderError,
    extract_json_object,
)
from ryza.research.schemas import EDITOR_SCHEMA, MACRO_SCHEMA

_MACRO_SCORES = {
    "regime": {"jp_equity": "risk_on"}, "rates_bias": 0.2, "fx_bias": -0.1, "refs": [1],
}


def _anthropic_body(text: str, *, tin: int = 120, tout: int = 30) -> bytes:
    """Anthropic Messages API レスポンス相当の JSON バイト列。"""
    return json.dumps(
        {"content": [{"type": "text", "text": text}],
         "usage": {"input_tokens": tin, "output_tokens": tout}}
    ).encode("utf-8")


class RecordingPoster:
    """(status, body) 列を順に返すフェイク HttpPoster。リクエストを記録する。"""

    def __init__(self, responses: list[tuple[int, bytes]]) -> None:
        self._responses = responses
        self.calls: list[dict] = []
        self.i = 0

    def __call__(self, url, *, headers, body, timeout):
        self.calls.append({"url": url, "headers": headers, "body": body, "timeout": timeout})
        item = self._responses[min(self.i, len(self._responses) - 1)]
        self.i += 1
        return item


# ── リクエスト組立・レスポンス解釈 ─────────────────────────────────────────────
def test_generate_builds_request_and_parses_response():
    poster = RecordingPoster([(200, _anthropic_body(json.dumps(_MACRO_SCORES)))])
    provider = AnthropicProvider(api_key="test-key", http=poster, api_version="2023-06-01")
    result = provider.generate(
        system="あなたはマクロ担当", user="{}", schema=MACRO_SCHEMA, model="claude-sonnet-5"
    )

    assert result.content == _MACRO_SCORES
    assert result.tokens_in == 120 and result.tokens_out == 30
    # リクエスト検証: 認証ヘッダ・モデル・system にスキーマ指示・user メッセージ。
    call = poster.calls[0]
    assert call["headers"]["x-api-key"] == "test-key"
    assert call["headers"]["anthropic-version"] == "2023-06-01"
    payload = json.loads(call["body"])
    assert payload["model"] == "claude-sonnet-5"
    assert payload["messages"] == [{"role": "user", "content": "{}"}]
    assert "JSON Schema" in payload["system"] and "rates_bias" in payload["system"]
    assert payload["max_tokens"] > 0


def test_generate_parses_fenced_json():
    fenced = "```json\n" + json.dumps(_MACRO_SCORES) + "\n```"
    poster = RecordingPoster([(200, _anthropic_body(fenced))])
    provider = AnthropicProvider(api_key="k", http=poster)
    result = provider.generate(system="s", user="u", schema=MACRO_SCHEMA, model="m")
    assert result.content == _MACRO_SCORES


# ── HTTP リトライ ──────────────────────────────────────────────────────────────
def test_retries_on_429_then_succeeds():
    slept: list[float] = []
    poster = RecordingPoster(
        [(429, b"rate limited"), (200, _anthropic_body(json.dumps(_MACRO_SCORES)))]
    )
    provider = AnthropicProvider(
        api_key="k", http=poster, max_retries=3, sleep=slept.append
    )
    result = provider.generate(system="s", user="u", schema=MACRO_SCHEMA, model="m")
    assert result.content == _MACRO_SCORES
    assert len(poster.calls) == 2  # 1回失敗 → 1回成功
    assert len(slept) == 1  # 1回バックオフ


def test_retries_exhausted_raises():
    slept: list[float] = []
    poster = RecordingPoster([(503, b"unavailable")])
    provider = AnthropicProvider(api_key="k", http=poster, max_retries=2, sleep=slept.append)
    with pytest.raises(ProviderError):
        provider.generate(system="s", user="u", schema=MACRO_SCHEMA, model="m")
    assert len(poster.calls) == 3  # 初回 + 2 リトライ


def test_non_retryable_status_raises_immediately():
    poster = RecordingPoster([(400, b'{"error":"bad"}')])
    provider = AnthropicProvider(api_key="k", http=poster, max_retries=3)
    with pytest.raises(ProviderError):
        provider.generate(system="s", user="u", schema=MACRO_SCHEMA, model="m")
    assert len(poster.calls) == 1  # 4xx は即エラー(リトライしない)


def test_transient_exception_is_retried():
    class FlakyPoster:
        def __init__(self):
            self.n = 0

        def __call__(self, url, *, headers, body, timeout):
            self.n += 1
            if self.n == 1:
                raise TimeoutError("boom")
            return (200, _anthropic_body(json.dumps(_MACRO_SCORES)))

    poster = FlakyPoster()
    provider = AnthropicProvider(api_key="k", http=poster, max_retries=2, sleep=lambda _: None)
    result = provider.generate(system="s", user="u", schema=MACRO_SCHEMA, model="m")
    assert result.content == _MACRO_SCORES
    assert poster.n == 2


# ── コスト記録(StructuredLLM 経由・DB フィクスチャ)──────────────────────────────
def test_cost_recorded_via_structured_llm(conn, run):
    poster = RecordingPoster([(200, _anthropic_body(json.dumps(_MACRO_SCORES), tin=120, tout=30))])
    provider = AnthropicProvider(api_key="k", http=poster)
    llm = StructuredLLM(provider, run, price_per_1k={"mid": 0.6})
    result = llm.complete(
        system="s", user="u", schema=MACRO_SCHEMA, task_type="analysis.macro",
        model_tier="mid", model="claude-sonnet-5",
    )
    assert result.content == _MACRO_SCORES
    assert result.attempts == 1
    with conn.cursor() as cur:
        cur.execute("SELECT cost FROM meta.runs WHERE run_id = %s", (run.run_id,))
        cost = cur.fetchone()[0]
    assert cost["by_tier"]["mid"]["calls"] == 1
    assert cost["total_tokens"] == 150
    assert cost["total_cost_estimate"] == pytest.approx(150 / 1000 * 0.6)


def test_structured_llm_retries_on_bad_schema(conn, run):
    # 1回目は不適合、2回目は適合 → StructuredLLM が再試行して 2 回 POST する。
    bad = _anthropic_body(json.dumps({"regime": {}}))  # 必須 rates_bias 等が無い
    good = _anthropic_body(json.dumps(_MACRO_SCORES))
    poster = RecordingPoster([(200, bad), (200, good)])
    provider = AnthropicProvider(api_key="k", http=poster)
    llm = StructuredLLM(provider, run)
    result = llm.complete(
        system="s", user="u", schema=MACRO_SCHEMA, task_type="analysis.macro",
        model_tier="mid", model="m", max_retries=2,
    )
    assert result.attempts == 2
    assert len(poster.calls) == 2


# ── extract_json_object ────────────────────────────────────────────────────────
def test_extract_json_object_variants():
    assert extract_json_object('{"a": 1}') == {"a": 1}
    assert extract_json_object("前置き {\"a\": 2} 後書き") == {"a": 2}
    assert extract_json_object("```json\n{\"a\": 3}\n```") == {"a": 3}
    with pytest.raises(ProviderError):
        extract_json_object("no json here")


# ── LLMConfig ──────────────────────────────────────────────────────────────────
def test_llm_config_load():
    cfg = LLMConfig.load()
    assert cfg.model_for("mid") == "claude-sonnet-5"
    assert cfg.model_for("light") == "claude-haiku-4-5-20251001"
    assert set(cfg.price_map()) >= {"light", "mid", "fable"}
    assert cfg.max_tokens_for("light") == 2048


# ── DryRunProvider ─────────────────────────────────────────────────────────────
def test_dryrun_provider_returns_valid_shapes():
    from ryza.research.schemas import validate

    p = DryRunProvider()
    macro = p.generate(system="s", user="{}", schema=MACRO_SCHEMA, model="m")
    assert validate(macro.content, MACRO_SCHEMA) == []
    editor = p.generate(system="s", user="{}", schema=EDITOR_SCHEMA, model="m")
    assert validate(editor.content, EDITOR_SCHEMA) == []
    # 朝刊トピック: refs を level1 に引用した U字。
    user = json.dumps({"task": "t", "material": {"title": "半導体", "refs": [7]}})
    topic = p.generate(system="s", user=user, schema=MORNING_TOPIC_SCHEMA, model="m")
    assert validate(topic.content, MORNING_TOPIC_SCHEMA) == []
    lvl1 = [s for s in topic.content["sentences"] if s["level"] == 1]
    assert lvl1 and lvl1[0]["source_ids"] == [7]


# ── load_api_key(Issue #30: ryza.secrets へ抽出後の後方互換)───────────────────
def test_load_api_key_env_priority(monkeypatch, fake_secret_manager):
    calls = fake_secret_manager({"anthropic-api-key": "SMKEY"})
    monkeypatch.setenv("RYZA_ANTHROPIC_API_KEY", "ENVKEY")
    monkeypatch.setenv("GCP_PROJECT", "proj")
    from ryza.research.providers import load_api_key

    assert load_api_key() == "ENVKEY"
    assert calls == []


def test_load_api_key_secret_fallback(monkeypatch, fake_secret_manager):
    fake_secret_manager({"anthropic-api-key": "SMKEY"})
    monkeypatch.delenv("RYZA_ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("GCP_PROJECT", "proj")
    from ryza.research.providers import load_api_key

    assert load_api_key() == "SMKEY"


def test_load_api_key_missing_raises_provider_error(monkeypatch, fake_secret_manager):
    fake_secret_manager({})
    monkeypatch.delenv("RYZA_ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("GCP_PROJECT", raising=False)
    from ryza.research.providers import load_api_key

    with pytest.raises(ProviderError):
        load_api_key()
