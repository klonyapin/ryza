"""``config/execution.yaml``(手数料・スリッページ)のローダ(T-016)。

初期値の根拠は execution.yaml のコメントが正。E4「全コスト込み」評価の入力になるため
値のハードコードは禁止 — 参照は必ず本ローダ経由(受け入れ基準)。

流儀は ``research/providers.py`` の ``LLMConfig.load`` / ``ryza.ips`` と同じ:
classmethod ``load`` + 既定パスは ``__file__`` 相対、frozen dataclass、値域検証は
load 時に行い違反は即座に露見させる。金額・率は Decimal(文字列経由)で扱う。
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

import yaml

_CONFIG_PATH = Path(__file__).resolve().parents[3] / "config" / "execution.yaml"

# fees の必須キー。未知の資産クラスの引き当て先(高コスト側に倒す — yaml コメント参照)。
_DEFAULT_FEE_KEY = "default"


class ExecutionConfigError(ValueError):
    """execution.yaml の欠落・値域違反。"""


def _dec(raw: Any, field: str) -> Decimal:
    try:
        value = Decimal(str(raw))
    except (InvalidOperation, TypeError) as exc:
        raise ExecutionConfigError(f"{field} が数値でない: {raw!r}") from exc
    if value < 0:
        raise ExecutionConfigError(f"{field} は非負: {value}")
    return value


def _int(raw: Any, field: str) -> int:
    """非負整数として読む(営業日数などの計数値)。"""
    try:
        value = int(raw)
    except (TypeError, ValueError) as exc:
        raise ExecutionConfigError(f"{field} が整数でない: {raw!r}") from exc
    if value < 0:
        raise ExecutionConfigError(f"{field} は非負: {value}")
    return value


@dataclass(frozen=True)
class FeeSpec:
    """1 資産クラスの売買委託手数料。fee = clamp(rate×約定代金, min_fee, max_fee)。"""

    commission_rate: Decimal
    min_fee: Decimal = Decimal(0)
    max_fee: Decimal | None = None


@dataclass(frozen=True)
class SlippageSpec:
    """スリッページ率(bps)= half_spread + impact_coeff×√参加率(max_bps でキャップ)。"""

    half_spread_bps: Decimal
    impact_coeff_bps: Decimal
    max_bps: Decimal


@dataclass(frozen=True)
class CloseSpec:
    """締めの再実行窓。日次の締めは当日に加えて直近 N 営業日の NAV を再計算・上書きする。"""

    reclose_business_days: int


@dataclass(frozen=True)
class ExecutionConfig:
    """``config/execution.yaml`` の内容。"""

    version: str
    slippage: SlippageSpec
    fees: dict[str, FeeSpec]
    close: CloseSpec

    @classmethod
    def load(cls, path: str | Path = _CONFIG_PATH) -> ExecutionConfig:
        data: dict[str, Any] = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}

        slip_raw = data.get("slippage") or {}
        for key in ("half_spread_bps", "impact_coeff_bps", "max_bps"):
            if key not in slip_raw:
                raise ExecutionConfigError(f"slippage.{key} が無い")
        slippage = SlippageSpec(
            half_spread_bps=_dec(slip_raw["half_spread_bps"], "slippage.half_spread_bps"),
            impact_coeff_bps=_dec(slip_raw["impact_coeff_bps"], "slippage.impact_coeff_bps"),
            max_bps=_dec(slip_raw["max_bps"], "slippage.max_bps"),
        )
        if slippage.max_bps <= 0:
            raise ExecutionConfigError(f"slippage.max_bps は正: {slippage.max_bps}")

        fees_raw = data.get("fees") or {}
        fees: dict[str, FeeSpec] = {}
        for asset_class, spec in fees_raw.items():
            spec = spec or {}
            if "commission_rate" not in spec:
                raise ExecutionConfigError(f"fees.{asset_class}.commission_rate が無い")
            max_fee = spec.get("max_fee")
            fees[str(asset_class)] = FeeSpec(
                commission_rate=_dec(spec["commission_rate"],
                                     f"fees.{asset_class}.commission_rate"),
                min_fee=_dec(spec.get("min_fee", 0), f"fees.{asset_class}.min_fee"),
                max_fee=None if max_fee is None else _dec(
                    max_fee, f"fees.{asset_class}.max_fee"),
            )
        if _DEFAULT_FEE_KEY not in fees:
            raise ExecutionConfigError(
                f"fees.{_DEFAULT_FEE_KEY} が無い(未知の資産クラスの引き当て先)"
            )

        # close セクションも必須。既定値をコード側に持つと「窓の広さ」がハードコード
        # されるため(値の根拠は yaml のコメントが正)、欠落は即座にエラーにする。
        close_raw = data.get("close") or {}
        if "reclose_business_days" not in close_raw:
            raise ExecutionConfigError("close.reclose_business_days が無い")
        reclose_days = _int(
            close_raw["reclose_business_days"], "close.reclose_business_days"
        )

        return cls(
            version=str(data.get("version", "1")),
            slippage=slippage,
            fees=fees,
            close=CloseSpec(reclose_business_days=reclose_days),
        )

    def fee_for(self, asset_class: str | None) -> FeeSpec:
        """資産クラス → 手数料スペック。未知・欠損は default(高コスト側)。"""
        if asset_class and asset_class in self.fees:
            return self.fees[asset_class]
        return self.fees[_DEFAULT_FEE_KEY]


__all__ = [
    "CloseSpec",
    "ExecutionConfig",
    "ExecutionConfigError",
    "FeeSpec",
    "SlippageSpec",
]
