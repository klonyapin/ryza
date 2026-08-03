"""config — ``config/press.yaml`` の型付きローダ（30-press §2〜§4・§6・§7）。

決定論コード（topics/linter/flash/images/morning）が閾値・上限・画像タグをここから読む。
market_view.py の ``MarketViewConfig`` と同じ流儀（frozen dataclass + ``load``）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

_CONFIG_PATH = Path(__file__).resolve().parents[3] / "config" / "press.yaml"


@dataclass(frozen=True)
class TopicsConfig:
    max_topics: int = 5
    min_chars: int = 200
    max_chars: int = 400
    min_sentences: int = 4
    max_sentences: int = 8
    max_regens: int = 2
    policy_geo_bonus: float = 0.15
    policy_geo_categories: tuple[str, ...] = ()
    novelty_lookback_days: int = 7


@dataclass(frozen=True)
class FlashConfig:
    newsworthiness_threshold: float = 60.0
    per_hour: int = 3
    per_day: int = 12
    dedup_similarity: float = 0.9
    max_regens: int = 1
    default_verify_hours: int = 72


@dataclass(frozen=True)
class ImagesConfig:
    board: str = "safebooru"
    base_url: str = "https://safebooru.org/index.php"
    exclude_tags: tuple[str, ...] = ()
    mascot_tags: tuple[str, ...] = ()
    thumbnail_tags: tuple[str, ...] = ()
    max_rating: str = "safe"


@dataclass(frozen=True)
class PressConfig:
    version: str
    topics: TopicsConfig
    flash: FlashConfig
    images: ImagesConfig
    raw: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def load(cls, path: str | Path = _CONFIG_PATH) -> PressConfig:
        data: dict[str, Any] = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
        t = dict(data.get("topics", {}))
        f = dict(data.get("flash", {}))
        im = dict(data.get("images", {}))
        return cls(
            version=str(data.get("version", "1")),
            topics=TopicsConfig(
                max_topics=int(t.get("max_topics", 5)),
                min_chars=int(t.get("min_chars", 200)),
                max_chars=int(t.get("max_chars", 400)),
                min_sentences=int(t.get("min_sentences", 4)),
                max_sentences=int(t.get("max_sentences", 8)),
                max_regens=int(t.get("max_regens", 2)),
                policy_geo_bonus=float(t.get("policy_geo_bonus", 0.15)),
                policy_geo_categories=tuple(t.get("policy_geo_categories", []) or []),
                novelty_lookback_days=int(t.get("novelty_lookback_days", 7)),
            ),
            flash=FlashConfig(
                newsworthiness_threshold=float(f.get("newsworthiness_threshold", 60)),
                per_hour=int(f.get("per_hour", 3)),
                per_day=int(f.get("per_day", 12)),
                dedup_similarity=float(f.get("dedup_similarity", 0.9)),
                max_regens=int(f.get("max_regens", 1)),
                default_verify_hours=int(f.get("default_verify_hours", 72)),
            ),
            images=ImagesConfig(
                board=str(im.get("board", "safebooru")),
                base_url=str(im.get("base_url", "https://safebooru.org/index.php")),
                exclude_tags=tuple(im.get("exclude_tags", []) or []),
                mascot_tags=tuple(im.get("mascot_tags", []) or []),
                thumbnail_tags=tuple(im.get("thumbnail_tags", []) or []),
                max_rating=str(im.get("max_rating", "safe")),
            ),
            raw=data,
        )
