"""providers — 実 LLM プロバイダ(Anthropic)と階層設定ローダ(T-013)。

設計 00-system-design §2(LLM 境界)。``llm.py`` の ``LLMProvider`` プロトコルを実装し、
``StructuredLLM`` の裏で Anthropic Messages API を叩く唯一の口。構造化出力の検証・リトライ・
コスト記録は ``StructuredLLM`` 側が担うため、本モジュールの責務は次に限る:

- **リクエスト組立**: system に「スキーマ適合 JSON のみを返せ」指示を足し Messages API へ POST。
- **レスポンス解釈**: content の text ブロックから JSON を頑健に抽出して ``ProviderResult`` にする。
- **HTTP リトライ・タイムアウト**: 429/5xx/タイムアウトを指数バックオフで再試行(スキーマ検証の
  再試行は ``StructuredLLM`` の管轄。ここは通信レベルのみ)。
- **鍵の遅延ロード**: env ``RYZA_ANTHROPIC_API_KEY`` / ``ANTHROPIC_API_KEY`` を優先し、無ければ
  Secret Manager ``anthropic-api-key`` を GCE メタデータ + REST で取得(T-006 bot の stdlib 流儀)。

**SDK は import しない**(proto-plus/protobuf の版差回避・追加依存なし)。テストは HTTP を
差し替え可能な ``HttpPoster`` にモックを注入し、実 API・実ネットワークを一切呼ばない。

構造化出力に Anthropic の ``output_config.format``(strict JSON Schema)は使わない。分析
スキーマ(``schemas.py``)は数値 min/max・``additionalProperties: true`` を含み strict 形式の
制約(数値制約非対応・additionalProperties=false 必須)に適合しないため、スキーマは
プロンプトに載せ、返った JSON を ``StructuredLLM`` が ``validate`` で検査・再試行する方式にする。
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

import yaml

from ryza.research.llm import ProviderResult

_API_URL = "https://api.anthropic.com/v1/messages"
_CONFIG_PATH = Path(__file__).resolve().parents[3] / "config" / "llm.yaml"

# 通信レベルで再試行する HTTP ステータス(429=レート・5xx=サーバ・529=過負荷)。
_RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504, 529})


class ProviderError(RuntimeError):
    """Anthropic API 呼び出しが最終的に失敗したときに送出する。"""


# ── 階層設定(config/llm.yaml)────────────────────────────────────────────────
@dataclass(frozen=True)
class TierSpec:
    """1 階層のモデル ID・単価・出力上限。"""

    model: str
    price_per_1k: float
    max_tokens: int


@dataclass(frozen=True)
class LLMConfig:
    """``config/llm.yaml`` の内容(階層 → モデル・単価)。"""

    version: str
    api_version: str
    default_max_tokens: int
    tiers: dict[str, TierSpec]

    @classmethod
    def load(cls, path: str | Path = _CONFIG_PATH) -> LLMConfig:
        data: dict[str, Any] = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
        default_max = int(data.get("max_tokens", 4096))
        tiers: dict[str, TierSpec] = {}
        for name, spec in (data.get("tiers", {}) or {}).items():
            spec = spec or {}
            tiers[str(name)] = TierSpec(
                model=str(spec["model"]),
                price_per_1k=float(spec.get("price_per_1k", 0.0)),
                max_tokens=int(spec.get("max_tokens", default_max)),
            )
        return cls(
            version=str(data.get("version", "1")),
            api_version=str(data.get("api_version", "2023-06-01")),
            default_max_tokens=default_max,
            tiers=tiers,
        )

    def model_for(self, tier: str) -> str:
        """階層 → モデル ID。未知の階層は KeyError(設定漏れを早期に露見させる)。"""
        return self.tiers[tier].model

    def max_tokens_for(self, tier: str) -> int:
        spec = self.tiers.get(tier)
        return spec.max_tokens if spec is not None else self.default_max_tokens

    def price_map(self) -> dict[str, float]:
        """階層 → ¥/1k tokens(``StructuredLLM`` の ``price_per_1k`` にそのまま渡せる)。"""
        return {name: spec.price_per_1k for name, spec in self.tiers.items()}


# ── HTTP 抽象(テストはモックを注入)──────────────────────────────────────────
class HttpPoster(Protocol):
    """POST して ``(status, body_bytes)`` を返す差し替え口。タイムアウト等は例外送出。"""

    def __call__(
        self, url: str, *, headers: dict[str, str], body: bytes, timeout: float
    ) -> tuple[int, bytes]: ...


class UrllibPoster:
    """本番用 ``HttpPoster``。標準ライブラリ ``urllib`` のみ(追加依存なし)。

    HTTP エラー(4xx/5xx)はステータス付きで返す(呼び出し側が再試行判定する)。
    接続エラー・タイムアウトは例外として送出する(呼び出し側が再試行する)。
    """

    def __call__(
        self, url: str, *, headers: dict[str, str], body: bytes, timeout: float
    ) -> tuple[int, bytes]:
        req = urllib.request.Request(url, data=body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
                return resp.status, resp.read()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read() if exc.fp else b""


# ── 鍵の遅延ロード ─────────────────────────────────────────────────────────────
def load_api_key(
    *,
    secret: str = "anthropic-api-key",
    project: str | None = None,
    timeout: float = 10.0,
) -> str:
    """Anthropic API キーを取得。env 優先、無ければ Secret Manager(GCE メタデータ + REST)。

    T-006 bot(``ryza.bot.main.load_token``)と同じ stdlib REST 方式。SDK を使わないのは
    proto-plus/protobuf の版差で壊れやすいため。
    """
    import os

    key = os.environ.get("RYZA_ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_API_KEY")
    if key:
        return key
    project = project or os.environ.get("GCP_PROJECT", "")
    if not project:
        raise ProviderError(
            "Anthropic API キー未設定(env RYZA_ANTHROPIC_API_KEY / "
            "ANTHROPIC_API_KEY、または GCP_PROJECT + Secret 'anthropic-api-key')"
        )
    import base64

    meta = urllib.request.Request(
        "http://metadata.google.internal/computeMetadata/v1/instance/"
        "service-accounts/default/token",
        headers={"Metadata-Flavor": "Google"},
    )
    access_token = json.load(urllib.request.urlopen(meta, timeout=timeout))["access_token"]
    req = urllib.request.Request(
        f"https://secretmanager.googleapis.com/v1/projects/{project}"
        f"/secrets/{secret}/versions/latest:access",
        headers={"Authorization": f"Bearer {access_token}"},
    )
    payload = json.load(urllib.request.urlopen(req, timeout=timeout))
    return base64.b64decode(payload["payload"]["data"]).decode("utf-8")


# ── JSON 抽出(頑健化)─────────────────────────────────────────────────────────
def extract_json_object(text: str) -> dict[str, Any]:
    """モデル応答テキストから JSON オブジェクトを頑健に取り出す。

    素の ``json.loads`` を試し、失敗したらコードフェンス除去・最初の ``{`` から最後の ``}`` を
    切り出して再試行する(モデルが前置き文や ```json フェンスを付けるケースの吸収)。
    """
    text = text.strip()
    try:
        obj = json.loads(text)
        if isinstance(obj, dict):
            return obj
    except json.JSONDecodeError:
        pass
    # コードフェンス ```json ... ``` を剥がす。
    if text.startswith("```"):
        inner = text.split("```", 2)
        if len(inner) >= 2:
            body = inner[1]
            if body.startswith("json"):
                body = body[4:]
            text = body.strip()
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1 and end > start:
        candidate = text[start : end + 1]
        obj = json.loads(candidate)
        if isinstance(obj, dict):
            return obj
    raise ProviderError(f"応答から JSON オブジェクトを抽出できません: {text[:200]!r}")


# ── 出力指示(system に足す)───────────────────────────────────────────────────
def _json_directive(schema: dict[str, Any]) -> str:
    return (
        "\n\n---\n"
        "出力形式(厳守): 以下の JSON Schema に適合する **単一の JSON オブジェクトのみ** を返せ。"
        "前置き・後書き・コードフェンスを付けず JSON オブジェクトそのものだけを出力せよ。\n"
        f"JSON Schema:\n{json.dumps(schema, ensure_ascii=False)}"
    )


# ── Anthropic プロバイダ ───────────────────────────────────────────────────────
class AnthropicProvider:
    """``LLMProvider`` の実装。Anthropic Messages API を stdlib REST で叩く。

    - ``api_key``: 明示指定。省略時は初回 ``generate`` で ``load_api_key`` により遅延ロード。
    - ``http``: ``HttpPoster``。省略時 ``UrllibPoster``。テストはモックを注入する。
    - ``max_tokens``: 出力上限(構造化 JSON は短いので既定 4096)。
    - リトライ: 通信レベル(429/5xx/タイムアウト)のみ指数バックオフで再試行する。
      スキーマ検証の再試行は ``StructuredLLM`` の管轄。
    """

    def __init__(
        self,
        *,
        api_key: str | None = None,
        http: HttpPoster | None = None,
        api_version: str = "2023-06-01",
        max_tokens: int = 4096,
        timeout: float = 60.0,
        max_retries: int = 3,
        backoff_base: float = 1.0,
        backoff_cap: float = 30.0,
        secret: str = "anthropic-api-key",
        project: str | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._api_key = api_key
        self._http: HttpPoster = http or UrllibPoster()
        self.api_version = api_version
        self.max_tokens = max_tokens
        self.timeout = timeout
        self.max_retries = max_retries
        self.backoff_base = backoff_base
        self.backoff_cap = backoff_cap
        self._secret = secret
        self._project = project
        self._sleep = sleep

    def _key(self) -> str:
        if self._api_key is None:
            self._api_key = load_api_key(secret=self._secret, project=self._project)
        return self._api_key

    def _build_body(
        self, *, system: str, user: str, schema: dict[str, Any], model: str
    ) -> bytes:
        payload = {
            "model": model,
            "max_tokens": self.max_tokens,
            "system": system + _json_directive(schema),
            "messages": [{"role": "user", "content": user}],
        }
        return json.dumps(payload, ensure_ascii=False).encode("utf-8")

    def _headers(self) -> dict[str, str]:
        return {
            "x-api-key": self._key(),
            "anthropic-version": self.api_version,
            "content-type": "application/json",
        }

    def _post_with_retry(self, body: bytes) -> dict[str, Any]:
        headers = self._headers()
        last_error: str = ""
        for attempt in range(self.max_retries + 1):
            try:
                status, raw = self._http(
                    _API_URL, headers=headers, body=body, timeout=self.timeout
                )
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                last_error = f"通信エラー: {exc}"
                if attempt < self.max_retries:
                    self._sleep(self._delay(attempt))
                    continue
                raise ProviderError(last_error) from exc

            if 200 <= status < 300:
                return json.loads(raw.decode("utf-8"))

            snippet = raw.decode("utf-8", "replace")[:200]
            last_error = f"HTTP {status}: {snippet}"
            if status in _RETRYABLE_STATUS and attempt < self.max_retries:
                self._sleep(self._delay(attempt))
                continue
            raise ProviderError(last_error)
        raise ProviderError(last_error or "Anthropic API 呼び出しに失敗")

    def _delay(self, attempt: int) -> float:
        return min(self.backoff_base * (2**attempt), self.backoff_cap)

    def generate(
        self,
        *,
        system: str,
        user: str,
        schema: dict[str, Any],
        model: str,
    ) -> ProviderResult:
        """構造化出力を 1 回要求し、パース済み JSON を ``ProviderResult`` にして返す。"""
        body = self._build_body(system=system, user=user, schema=schema, model=model)
        data = self._post_with_retry(body)
        text = _first_text(data.get("content") or [])
        content = extract_json_object(text)
        usage = data.get("usage") or {}
        return ProviderResult(
            content=content,
            tokens_in=int(usage.get("input_tokens", 0)),
            tokens_out=int(usage.get("output_tokens", 0)),
            raw_text=text,
        )


def _first_text(content_blocks: list[Any]) -> str:
    """Messages API の content(ブロック列)から最初の text ブロックの文字列を返す。"""
    for block in content_blocks:
        if isinstance(block, dict) and block.get("type") == "text":
            return str(block.get("text", ""))
    raise ProviderError("応答に text ブロックがありません")


# ── ドライラン用フィクスチャプロバイダ ─────────────────────────────────────────
def _pad(base: str, n: int) -> str:
    """base を繰り返して非空白 n 文字ちょうどにする(字数を決定論化)。"""
    return (base * (n // len(base) + 1))[:n]


class DryRunProvider:
    """``jobs.daily --dry-run`` 用の決定論プロバイダ(実 API を呼ばない)。

    スキーマの ``required`` / ``properties`` を見て、各分析エージェント・朝刊執筆に対して
    「検証を通す最小の合法出力」を返す。CLI のスモーク(ローカル DB エンドツーエンド完走)専用で、
    テストは各部門の conftest プロバイダを使う(本クラスは import しない)。
    """

    def __init__(self) -> None:
        self.calls: list[dict[str, str]] = []

    def generate(
        self, *, system: str, user: str, schema: dict[str, Any], model: str
    ) -> ProviderResult:
        self.calls.append({"system": system, "user": user, "model": model})
        content = self._content_for(schema, user)
        return ProviderResult(
            content=content, tokens_in=80, tokens_out=40,
            raw_text=json.dumps(content, ensure_ascii=False),
        )

    def _content_for(self, schema: dict[str, Any], user: str) -> dict[str, Any]:
        props = schema.get("properties", {})
        required = set(schema.get("required", []))
        # 朝刊トピック(執筆): trade_implication 必須 + sentences。
        if "sentences" in props and "trade_implication" in required:
            return self._morning_topic(user)
        # 速報一次判定(triage)。
        if "newsworthiness" in props:
            return {"newsworthiness": 50.0, "kind": "fact", "reason": "dry-run"}
        # 分析エージェント各種。required から判別。
        if {"regime", "rates_bias", "fx_bias"} <= required:  # macro
            return {"regime": {}, "rates_bias": 0.0, "fx_bias": 0.0, "confidence": 0.5, "refs": []}
        if "instruments" in required:  # micro
            return {"instruments": [], "refs": []}
        if "by_asset_class" in required:  # sentiment
            return {"by_asset_class": {}, "anomaly": 0.0, "refs": []}
        if {"regime_changes", "key_risk_ops"} <= required:  # editor
            return {"regime_changes": {}, "key_risk_ops": [], "contradictions": [],
                    "morning_topics": [], "refs": []}
        # 未知スキーマ: required を素直に埋める(検証は StructuredLLM が担う)。
        return {k: [] for k in required}

    def _morning_topic(self, user: str) -> dict[str, Any]:
        try:
            data = json.loads(user)
        except json.JSONDecodeError:
            data = {}
        material = data.get("material", {}) if isinstance(data, dict) else {}
        refs = [int(x) for x in (material.get("refs") or [])]
        title = str(material.get("title", "トピック"))
        valley_level = 1 if refs else 2
        valley_refs = refs[:1] if refs else []
        s = "source_ids"
        sentences = [
            {"text": _pad("概念をまとめ広い例を示す文", 45), "level": 4, s: []},
            {"text": _pad("二つ以上の証拠をまとめた文", 45), "level": 3, s: []},
            {"text": _pad("観察の解釈的要約の文", 45), "level": 2, s: []},
            {"text": _pad("純粋なファクトの文", 45), "level": valley_level, s: valley_refs},
            {"text": _pad("再び概念的にまとめ直す文", 45), "level": 3, s: []},
            {"text": _pad("含意で締める一文", 45), "level": 5, s: []},
        ]
        return {
            "argument": "きょうの相場は半導体が主導したみたい。",
            "sentences": sentences,
            "trade_implication": {"action": "watch", "target": title, "condition": "上抜け"},
            "title": title,
        }


__all__ = [
    "AnthropicProvider",
    "DryRunProvider",
    "HttpPoster",
    "LLMConfig",
    "ProviderError",
    "TierSpec",
    "UrllibPoster",
    "extract_json_object",
    "load_api_key",
]
