"""llm — LLM クライアントの薄層(構造化出力・階層タグ・コスト記録・リトライ)。

設計原則(00-system-design §2): **LLM は判断材料を作る側**。ここは分析エージェントが
プロバイダを叩くための唯一の共通口で、次を担う:

- **構造化出力**: プロバイダに JSON Schema を渡し、返った JSON を ``schemas.validate`` で検証。
  不適合ならリトライ(最大回数まで)。
- **階層タグ**: すべての呼び出しに ``dept_tag``(部門)・``task_type``(タスク種別)・
  ``model_tier``(light|mid|fable)を付す(経営管理部のユニットエコノミクス台帳の前提・§5)。
- **コスト記録**: 呼び出しごとに ``Run.add_cost(model_tier, tokens, cost_estimate)``。
- **プロバイダ差し替え**: 実プロバイダ呼び出しは ``LLMProvider`` プロトコルの裏に隠し、
  テストは ``FixtureProvider``(構造化出力のフィクスチャ)を注入する。

本モジュールは DB に触れない(コスト記録は渡された ``Run`` 経由)。実 API SDK も import しない。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from ryza.provenance import Run
from ryza.research.schemas import SchemaError, validate


@dataclass(frozen=True)
class ProviderResult:
    """プロバイダ 1 回分の生結果。``content`` はパース済み JSON(dict)を期待する。"""

    content: dict[str, Any]
    tokens_in: int
    tokens_out: int
    raw_text: str = ""

    @property
    def tokens(self) -> int:
        return self.tokens_in + self.tokens_out


class LLMProvider(Protocol):
    """実プロバイダ呼び出しの差し替え口。実装(Anthropic 等)はここに閉じる。

    ``schema`` を渡して構造化出力を要求し、パース済み JSON を返す契約。
    """

    def generate(
        self,
        *,
        system: str,
        user: str,
        schema: dict[str, Any],
        model: str,
    ) -> ProviderResult: ...


# モデル階層別のトークン単価(¥/1k tokens・概算)。経営管理部が実測で更新する前提の初期値。
# コストは meta.runs.cost に記録され、ユニットエコノミクス台帳へ集計される(§5)。
DEFAULT_PRICE_PER_1K: dict[str, float] = {
    "light": 0.05,
    "mid": 0.60,
    "fable": 9.00,
}


@dataclass
class LLMResult:
    """``StructuredLLM.complete`` の結果。scores(検証済み)とコスト内訳を保持する。"""

    content: dict[str, Any]
    model: str
    model_tier: str
    task_type: str
    tokens_in: int
    tokens_out: int
    cost_estimate: float
    attempts: int
    retries: list[str] = field(default_factory=list)  # 各リトライの理由(検証エラー)


class StructuredLLM:
    """構造化出力・リトライ・コスト記録をまとめた薄いクライアント。

    - ``provider``: 実呼び出し(差し替え可能)。
    - ``run``: コスト記録先(``add_cost``)。省略時はコスト記録をスキップ(純粋計算テスト用)。
    - ``dept_tag``: 既定 ``'research'``。全呼び出しに付す。
    """

    def __init__(
        self,
        provider: LLMProvider,
        run: Run | None = None,
        *,
        dept_tag: str = "research",
        price_per_1k: dict[str, float] | None = None,
    ) -> None:
        self._provider = provider
        self._run = run
        self.dept_tag = dept_tag
        self._price = dict(price_per_1k or DEFAULT_PRICE_PER_1K)

    def complete(
        self,
        *,
        system: str,
        user: str,
        schema: dict[str, Any],
        task_type: str,
        model_tier: str,
        model: str,
        max_retries: int = 2,
    ) -> LLMResult:
        """構造化出力を得て検証・コスト記録して返す。

        検証に失敗するたびに再試行し(``max_retries`` 回まで)、その都度コストを計上する
        (失敗した呼び出しも実費なので記録対象)。全試行が失敗したら ``SchemaError``。
        """
        attempts = 0
        retries: list[str] = []
        last_errors: list[str] = []
        total_in = total_out = 0
        total_cost = 0.0

        while attempts <= max_retries:
            attempts += 1
            result = self._provider.generate(
                system=system, user=user, schema=schema, model=model
            )
            cost = self._record_cost(model_tier, result.tokens)
            total_in += result.tokens_in
            total_out += result.tokens_out
            total_cost += cost

            errors = validate(result.content, schema)
            if not errors:
                return LLMResult(
                    content=result.content,
                    model=model,
                    model_tier=model_tier,
                    task_type=task_type,
                    tokens_in=total_in,
                    tokens_out=total_out,
                    cost_estimate=total_cost,
                    attempts=attempts,
                    retries=retries,
                )
            last_errors = errors
            retries.append(f"attempt {attempts}: {'; '.join(errors)}")

        raise SchemaError(last_errors)

    def _record_cost(self, model_tier: str, tokens: int) -> float:
        cost = tokens / 1000.0 * self._price.get(model_tier, 0.0)
        if self._run is not None:
            self._run.add_cost(model_tier, tokens=tokens, cost_estimate=cost)
        return cost


class FixtureProvider:
    """テスト用の決定論プロバイダ。あらかじめ与えた応答(dict)を順に返す。

    - ``responses``: 返す ``content``(dict)または ``ProviderResult`` の列。
      最後の要素に到達したらそれを繰り返す(リトライ検証を単純化)。
    - 呼び出しごとに ``calls`` に (system, user, model) を記録する(プロンプト検査用)。
    """

    def __init__(
        self,
        responses: list[dict[str, Any] | ProviderResult],
        *,
        tokens_in: int = 100,
        tokens_out: int = 50,
    ) -> None:
        if not responses:
            raise ValueError("responses が空")
        self._responses = responses
        self._i = 0
        self._tokens_in = tokens_in
        self._tokens_out = tokens_out
        self.calls: list[dict[str, str]] = []

    def generate(
        self,
        *,
        system: str,
        user: str,
        schema: dict[str, Any],
        model: str,
    ) -> ProviderResult:
        self.calls.append({"system": system, "user": user, "model": model})
        item = self._responses[min(self._i, len(self._responses) - 1)]
        self._i += 1
        if isinstance(item, ProviderResult):
            return item
        return ProviderResult(
            content=item, tokens_in=self._tokens_in, tokens_out=self._tokens_out
        )
