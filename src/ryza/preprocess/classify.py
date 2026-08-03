"""classify — 開示種別・ニュースカテゴリの分類（階層0・LLM 非依存）。

設計 20-research §3 ③「分類（開示種別・ニュースカテゴリ: 辞書・正規表現 + ロジスティック
回帰/fastText、教師データは運用中に蓄積）」。**初期はルール（辞書・正規表現）のみ**とし、
学習分類器は「器だけ」用意する（``Classifier`` プロトコル + ``RuleClassifier``。将来
``ModelClassifier`` を差し込めるようにする）。

判定根拠（どのルールがマッチしたか）を ``rationale`` に残し、``documents.meta`` 経由で
監査 A-13 のサンプル検査対象にする（誤分類の見逃しはここが最上流のため）。

DB 非依存（純関数）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Protocol


@dataclass(frozen=True)
class Rule:
    """1 分類ルール。``category`` は importance.yaml の category_weights のキーに対応。"""

    label: str          # 人間可読ラベル（例: '業績予想の修正'）
    category: str       # 正規化カテゴリ（例: 'filing_guidance_revision'）
    patterns: tuple[str, ...]  # いずれか 1 つでもマッチで該当
    applies_to: tuple[str, ...] = ()  # 対象 source_type（空=全て）


@dataclass(frozen=True)
class ClassifyResult:
    """分類結果。

    - ``category``: 最優先ルールの正規化カテゴリ（該当なしは ``'unknown'``）。
    - ``label``: その人間可読ラベル。
    - ``labels``: マッチした全ラベル（複数該当し得る）。
    - ``rationale``: どのルールがどのパターンでマッチしたか（監査用）。
    """

    category: str
    label: str | None
    labels: list[str] = field(default_factory=list)
    rationale: list[dict[str, str]] = field(default_factory=list)


class Classifier(Protocol):
    """分類器の差し替え口（初期はルール、将来は学習分類器）。"""

    def classify(
        self, title: str | None, body: str | None, source_type: str | None
    ) -> ClassifyResult: ...


# ── 開示種別（filing）ルール。優先度は上から高い（先に定義したものを優先採用）──────────
# TDnet/EDINET の適時開示タイトルに現れる定型表現をカバーする。
_FILING_RULES: tuple[Rule, ...] = (
    Rule("業績予想の修正", "filing_guidance_revision",
         (r"業績予想.{0,6}修正", r"通期.{0,4}予想.{0,4}修正", r"配当予想.{0,4}修正.{0,20}業績")),
    Rule("M&A・TOB・MBO", "filing_mna",
         (r"公開買付", r"TOB", r"MBO", r"株式交換", r"株式移転", r"合併", r"子会社化",
          r"買収")),
    Rule("大量保有報告", "filing_large_holding",
         (r"大量保有", r"変更報告書", r"保有割合")),
    Rule("自己株式取得", "filing_buyback",
         (r"自己株式.{0,4}取得", r"自社株買")),
    Rule("配当予想の修正", "filing_dividend",
         (r"配当予想.{0,4}修正", r"増配", r"減配", r"記念配当")),
    Rule("株式分割", "filing_stock_split",
         (r"株式分割", r"株式併合")),
    Rule("決算短信", "filing_earnings",
         (r"決算短信", r"四半期.{0,4}決算", r"通期.{0,4}決算", r"決算説明")),
)

# ── ニュースカテゴリ（news/gov/central_bank）ルール ─────────────────────────────
_NEWS_RULES: tuple[Rule, ...] = (
    Rule("金融政策", "news_monetary_policy",
         (r"金融政策", r"政策金利", r"利上げ", r"利下げ", r"日銀", r"FRB", r"FOMC",
          r"ECB", r"量的緩和", r"金融緩和", r"金融引[き締]"),),
    Rule("M&A", "news_mna",
         (r"買収", r"合併", r"経営統合", r"TOB", r"公開買付")),
    Rule("為替", "news_fx",
         (r"為替", r"円[高安]", r"ドル[高安]", r"USD/JPY", r"介入")),
    Rule("決算", "news_earnings",
         (r"決算", r"最終利益", r"営業[利益損失]", r"業績", r"増[益収]", r"減[益収]")),
)

# source_type ごとに適用するルール束。
_FILING_SOURCE_TYPES = frozenset({"filing"})


class RuleClassifier:
    """辞書・正規表現ベースの分類器（初期実装）。

    ``source_type`` が filing 系なら開示種別ルールを、それ以外（news/gov/central_bank/
    policy 等）はニュースカテゴリルールを適用する。source_type 不明時は両方を試す。
    """

    def __init__(
        self,
        filing_rules: tuple[Rule, ...] = _FILING_RULES,
        news_rules: tuple[Rule, ...] = _NEWS_RULES,
    ) -> None:
        self._filing_rules = filing_rules
        self._news_rules = news_rules

    def _rules_for(self, source_type: str | None) -> tuple[Rule, ...]:
        if source_type in _FILING_SOURCE_TYPES:
            return self._filing_rules
        if source_type is None:
            return self._filing_rules + self._news_rules
        return self._news_rules

    def classify(
        self, title: str | None, body: str | None, source_type: str | None
    ) -> ClassifyResult:
        text = f"{title or ''}\n{body or ''}"
        labels: list[str] = []
        rationale: list[dict[str, str]] = []
        top_category = "unknown"
        top_label: str | None = None
        for rule in self._rules_for(source_type):
            for pat in rule.patterns:
                m = re.search(pat, text)
                if m:
                    if rule.label not in labels:
                        labels.append(rule.label)
                        rationale.append(
                            {"label": rule.label, "category": rule.category,
                             "pattern": pat, "matched": m.group(0)}
                        )
                        if top_label is None:  # 先に定義した高優先ルールを採用
                            top_category = rule.category
                            top_label = rule.label
                    break  # このルールはマッチ済み。次のルールへ
        return ClassifyResult(
            category=top_category, label=top_label, labels=labels, rationale=rationale
        )


# 既定インスタンス（純関数的に使う）。
_DEFAULT = RuleClassifier()


def classify(
    title: str | None, body: str | None, source_type: str | None = None
) -> ClassifyResult:
    """既定のルール分類器で分類する（薄いショートカット）。"""
    return _DEFAULT.classify(title, body, source_type)
