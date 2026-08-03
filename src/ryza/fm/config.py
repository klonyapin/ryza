"""config — ``config/fm_ben.yaml`` / ``config/fm_jim.yaml`` のローダ(T-017)。

流儀は ``ryza.ips`` / ``ryza.execution.config`` と同じ: frozen dataclass + classmethod
``load`` + 既定パスは ``__file__`` 相対、値域検証は load 時に行い違反は即座に露見させる。

**ここにリスク上限は書かない**: ポッド資本・集中度・レバ・DD の上限は IPS と
``config/mandates/<fm>.yaml`` が正で、判定はコンプライアンスゲートが行う(81 §2-4)。
本ファイルが持つのは「哲学の器の中のパラメータ」(観測窓・スロット数・実行曜日)のみ。
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

import yaml

_CONFIG_DIR = Path(__file__).resolve().parents[3] / "config"
_BEN_PATH = _CONFIG_DIR / "fm_ben.yaml"
_JIM_PATH = _CONFIG_DIR / "fm_jim.yaml"


class FMConfigError(ValueError):
    """FM 設定の欠落・値域違反。"""


def _load_yaml(path: str | Path) -> dict[str, Any]:
    return yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}


def _positive_int(raw: Any, name: str) -> int:
    try:
        value = int(raw)
    except (TypeError, ValueError) as exc:
        raise FMConfigError(f"{name} が整数でない: {raw!r}") from exc
    if value <= 0:
        raise FMConfigError(f"{name} は正であるべき: {value}")
    return value


def _decimal(raw: Any, name: str) -> Decimal:
    try:
        value = Decimal(str(raw))
    except (InvalidOperation, TypeError) as exc:
        raise FMConfigError(f"{name} が数値でない: {raw!r}") from exc
    if value < 0:
        raise FMConfigError(f"{name} は非負であるべき: {value}")
    return value


@dataclass(frozen=True)
class JimConfig:
    """Jim(非 LLM・日次)のパラメータ。"""

    version: str
    producer: str
    fast_window: int
    slow_window: int
    volume_window: int
    min_volume_ratio: Decimal
    timeframe: str
    max_slots: int
    max_new_positions: int
    max_universe: int

    @classmethod
    def load(cls, path: str | Path = _JIM_PATH) -> JimConfig:
        data = _load_yaml(path)
        signal = data.get("signal") or {}
        sizing = data.get("sizing") or {}
        cfg = cls(
            version=str(data.get("version", "")),
            producer=str(data.get("producer") or "fm.jim.daily"),
            fast_window=_positive_int(signal.get("fast_window"), "signal.fast_window"),
            slow_window=_positive_int(signal.get("slow_window"), "signal.slow_window"),
            volume_window=_positive_int(signal.get("volume_window"), "signal.volume_window"),
            min_volume_ratio=_decimal(
                signal.get("min_volume_ratio", 0), "signal.min_volume_ratio"
            ),
            timeframe=str(signal.get("timeframe") or "1d"),
            max_slots=_positive_int(sizing.get("max_slots"), "sizing.max_slots"),
            max_new_positions=_positive_int(
                data.get("max_new_positions"), "max_new_positions"
            ),
            max_universe=_positive_int(data.get("max_universe"), "max_universe"),
        )
        if cfg.fast_window >= cfg.slow_window:
            raise FMConfigError(
                f"fast_window({cfg.fast_window})は slow_window({cfg.slow_window})未満のはず"
            )
        return cfg

    @property
    def min_bars(self) -> int:
        """シグナル計算に必要な最小バー本数(前日値との比較に +1 本)。"""
        return max(self.slow_window, self.volume_window) + 1


@dataclass(frozen=True)
class BenConfig:
    """Ben(LLM・週次)のパラメータ。"""

    version: str
    producer: str
    model_tier: str
    weekday: int  # ISO 8601(1=月曜 … 7=日曜)
    max_slots: int
    max_candidates: int
    max_documents: int
    doc_body_chars: int
    recent_theses: int

    @classmethod
    def load(cls, path: str | Path = _BEN_PATH) -> BenConfig:
        data = _load_yaml(path)
        sizing = data.get("sizing") or {}
        weekday = _positive_int(data.get("weekday"), "weekday")
        if weekday > 7:
            raise FMConfigError(f"weekday は ISO 8601 の 1〜7: {weekday}")
        model_tier = str(data.get("model_tier") or "")
        if model_tier not in ("light", "mid", "fable"):
            raise FMConfigError(
                f"model_tier は light|mid|fable のいずれか(モデル階層 — 不変原則7): "
                f"{model_tier!r}"
            )
        return cls(
            version=str(data.get("version", "")),
            producer=str(data.get("producer") or "fm.ben.weekly"),
            model_tier=model_tier,
            weekday=weekday,
            max_slots=_positive_int(sizing.get("max_slots"), "sizing.max_slots"),
            max_candidates=_positive_int(data.get("max_candidates"), "max_candidates"),
            max_documents=_positive_int(data.get("max_documents"), "max_documents"),
            doc_body_chars=_positive_int(data.get("doc_body_chars"), "doc_body_chars"),
            recent_theses=_positive_int(data.get("recent_theses"), "recent_theses"),
        )


__all__ = ["BenConfig", "FMConfigError", "JimConfig"]
