"""ExecutionConfig ローダと発効 config(config/execution.yaml)実値のリグレッション。

07-development §3-2 の流儀: 発効 config の実値を直接検証し、意図しない値変更を
テストで露見させる(値を変えるときは根拠コメントとこのテストをセットで更新する)。
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from ryza.execution.config import ExecutionConfig, ExecutionConfigError, FeeSpec


def test_load_effective_config_values():
    cfg = ExecutionConfig.load()
    # スリッページ(根拠は execution.yaml のコメント: 平方根インパクト則)。
    assert cfg.slippage.half_spread_bps == Decimal(5)
    assert cfg.slippage.impact_coeff_bps == Decimal(140)
    assert cfg.slippage.max_bps == Decimal(100)
    # 手数料: 国内株ゼロ(SBI・楽天 2023 無料化ほか)、米国株 0.495%・上限 22 USD。
    assert cfg.fees["equity_jp"].commission_rate == Decimal(0)
    assert cfg.fees["equity_us"].commission_rate == Decimal("0.00495")
    assert cfg.fees["equity_us"].max_fee == Decimal(22)
    # 未知の資産クラスは高コスト側(default = 米国株と同率)。
    assert cfg.fees["default"].commission_rate == Decimal("0.00495")
    # 再締めの窓(根拠は execution.yaml のコメント: 独立審査 重要-2 / 週末を跨ぐ遅延)。
    assert cfg.close.reclose_business_days == 3


def test_load_rejects_missing_close_section(tmp_path):
    """close の既定値はコード側に持たない(窓の広さのハードコード禁止)。"""
    p = tmp_path / "execution.yaml"
    p.write_text(
        "slippage: {half_spread_bps: '5', impact_coeff_bps: '140', max_bps: '100'}\n"
        "fees:\n  default: {commission_rate: '0'}\n",
        encoding="utf-8",
    )
    with pytest.raises(ExecutionConfigError, match="reclose_business_days"):
        ExecutionConfig.load(p)


def test_load_rejects_negative_reclose_window(tmp_path):
    p = tmp_path / "execution.yaml"
    p.write_text(
        "slippage: {half_spread_bps: '5', impact_coeff_bps: '140', max_bps: '100'}\n"
        "fees:\n  default: {commission_rate: '0'}\n"
        "close: {reclose_business_days: -1}\n",
        encoding="utf-8",
    )
    with pytest.raises(ExecutionConfigError, match="非負"):
        ExecutionConfig.load(p)


def test_fee_for_fallback():
    cfg = ExecutionConfig.load()
    assert cfg.fee_for("equity_jp") is cfg.fees["equity_jp"]
    assert cfg.fee_for("crypto") is cfg.fees["default"]
    assert cfg.fee_for(None) is cfg.fees["default"]
    assert cfg.fee_for("") is cfg.fees["default"]


def test_load_rejects_missing_sections(tmp_path):
    p = tmp_path / "execution.yaml"
    p.write_text("version: '1'\nslippage:\n  half_spread_bps: '5'\n", encoding="utf-8")
    with pytest.raises(ExecutionConfigError, match="impact_coeff_bps"):
        ExecutionConfig.load(p)


def test_load_rejects_missing_default_fee(tmp_path):
    p = tmp_path / "execution.yaml"
    p.write_text(
        "slippage: {half_spread_bps: '5', impact_coeff_bps: '140', max_bps: '100'}\n"
        "fees:\n  equity_jp: {commission_rate: '0'}\n",
        encoding="utf-8",
    )
    with pytest.raises(ExecutionConfigError, match="default"):
        ExecutionConfig.load(p)


def test_load_rejects_negative_values(tmp_path):
    p = tmp_path / "execution.yaml"
    p.write_text(
        "slippage: {half_spread_bps: '-1', impact_coeff_bps: '140', max_bps: '100'}\n"
        "fees:\n  default: {commission_rate: '0'}\n",
        encoding="utf-8",
    )
    with pytest.raises(ExecutionConfigError, match="非負"):
        ExecutionConfig.load(p)


def test_fee_spec_defaults():
    spec = FeeSpec(commission_rate=Decimal(0))
    assert spec.min_fee == Decimal(0)
    assert spec.max_fee is None
