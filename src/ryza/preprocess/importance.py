"""importance — 一次重要度スコア（階層0・LLM 非依存・決定論）。

設計 20-research §3 ⑤「一次重要度スコア（開示種別の重み + 対象銘柄の保有/ウォッチ状況 +
統計的異常の併発）」。ルールは ``config/importance.yaml``（コード外）に置き、改訂→再処理を
容易にする。

スコア式:
    score = clamp( category_weights[category] + Σ(適用 bonuses), 0.0, 1.0 )
ティア:
    score >= tiers.high            -> 'high'（直接 中位分析 + 速報候補）
    tiers.mid <= score < tiers.high -> 'mid' （軽量 LLM トリアージ）
    score < tiers.mid              -> 'low' （保存のみ）

判定根拠（``reasons``: どの重み・加点が効いたか）を残し、監査 A-13 のサンプル検査対象にする。

純関数（config は起動時に 1 度読む）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

# config/importance.yaml はリポジトリルート直下。src/ryza/preprocess/importance.py から 3 つ上。
_CONFIG_PATH = Path(__file__).resolve().parents[3] / "config" / "importance.yaml"


@dataclass(frozen=True)
class ImportanceConfig:
    """importance.yaml の内容。"""

    version: str
    tiers: dict[str, float]
    category_weights: dict[str, float]
    bonuses: dict[str, float]

    @classmethod
    def load(cls, path: str | Path = _CONFIG_PATH) -> ImportanceConfig:
        data: dict[str, Any] = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
        return cls(
            version=str(data.get("version", "1")),
            tiers=dict(data.get("tiers", {"high": 0.66, "mid": 0.33})),
            category_weights=dict(data.get("category_weights", {})),
            bonuses=dict(data.get("bonuses", {})),
        )


@dataclass(frozen=True)
class ImportanceResult:
    """一次重要度の算出結果。

    - ``score``: 0-1 のスコア。
    - ``tier``: ``'low'`` | ``'mid'`` | ``'high'``。
    - ``reasons``: 加点内訳（監査用）。各要素 ``{factor, delta}``。
    """

    score: float
    tier: str
    reasons: list[dict[str, Any]] = field(default_factory=list)


def _clamp(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))


def score_importance(
    config: ImportanceConfig,
    *,
    category: str,
    instrument_ids: list[int],
    held_ids: set[int] | None = None,
    watchlist_ids: set[int] | None = None,
    statistical_anomaly: bool = False,
) -> ImportanceResult:
    """分類カテゴリ・銘柄タグ・保有/ウォッチ/異常フラグから重要度を算出する。

    - ``category``: classify のカテゴリ（``unknown`` は最小重み）。
    - ``instrument_ids``: tagger が付けた銘柄。
    - ``held_ids`` / ``watchlist_ids``: 保有・ウォッチ中の銘柄集合（runner が DB から渡す）。
    - ``statistical_anomaly``: 価格・出来高等の統計的異常が併発しているか（上流が判定して渡す）。
    """
    held_ids = held_ids or set()
    watchlist_ids = watchlist_ids or set()
    tagged = set(instrument_ids)
    reasons: list[dict[str, Any]] = []

    base = float(
        config.category_weights.get(category, config.category_weights.get("unknown", 0.15))
    )
    reasons.append({"factor": f"category:{category}", "delta": base})
    score = base

    bonuses = config.bonuses
    if tagged & held_ids:
        d = float(bonuses.get("held_instrument", 0.0))
        score += d
        reasons.append({"factor": "held_instrument", "delta": d})
    if tagged & watchlist_ids:
        d = float(bonuses.get("watchlist_instrument", 0.0))
        score += d
        reasons.append({"factor": "watchlist_instrument", "delta": d})
    if statistical_anomaly:
        d = float(bonuses.get("statistical_anomaly", 0.0))
        score += d
        reasons.append({"factor": "statistical_anomaly", "delta": d})
    if len(tagged) >= 2:
        d = float(bonuses.get("multiple_instruments", 0.0))
        score += d
        reasons.append({"factor": "multiple_instruments", "delta": d})

    score = _clamp(score)
    tier = tier_of(config, score)
    return ImportanceResult(score=score, tier=tier, reasons=reasons)


def tier_of(config: ImportanceConfig, score: float) -> str:
    """スコアを config のしきい値でティアに写像する。"""
    high = float(config.tiers.get("high", 0.66))
    mid = float(config.tiers.get("mid", 0.33))
    if score >= high:
        return "high"
    if score >= mid:
        return "mid"
    return "low"
