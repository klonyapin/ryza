"""fm.sizing のテスト(T-017)。

中心は **不変原則1 の固定**: サイジング経路に LLM 由来の値(確信度・スコア)が
入らないことを、シグネチャ検査と挙動の両面で固定する。DB 不要の純ロジック。
"""

from __future__ import annotations

import inspect
from decimal import Decimal

import pytest

from ryza.fm import sizing
from ryza.fm.config import BenConfig, JimConfig
from ryza.gate.compliance import PositionState
from ryza.ips import load_and_validate

# 「サイズに使ってはならない」語彙。引数名にこれらが現れたら不変原則1 違反。
_FORBIDDEN_PARAM_TOKENS = (
    "confidence", "conviction", "score", "rank", "weight", "probability", "llm", "signal_strength"
)


@pytest.fixture(scope="module")
def mandates():
    return load_and_validate()[1]


# ── 不変原則1: サイジング経路に LLM 値が入らない ─────────────────────────────
def test_sizing_functions_take_no_confidence_arguments():
    """公開関数のシグネチャに確信度・スコア系の引数が存在しないことを固定する。"""
    for name in sizing.__all__:
        obj = getattr(sizing, name)
        if not inspect.isfunction(obj):
            continue
        params = list(inspect.signature(obj).parameters)
        for token in _FORBIDDEN_PARAM_TOKENS:
            assert not any(token in p.lower() for p in params), (
                f"{name}{tuple(params)} に '{token}' を含む引数がある(不変原則1)"
            )


def test_entry_qty_depends_only_on_slot_and_price():
    """同じスロット・価格・単元なら、他に何があっても数量は同じ。"""
    a = sizing.entry_qty(slot_value=Decimal(400_000), price=Decimal(1_000), lot_size=Decimal(100))
    b = sizing.entry_qty(slot_value=Decimal(400_000), price=Decimal(1_000), lot_size=Decimal(100))
    assert a == b == Decimal(400)


# ── スロット計算 ─────────────────────────────────────────────────────────────
def test_slot_value_is_capital_divided_by_slots(mandates):
    plan = sizing.slot_plan(mandates["ben"], max_slots=5, positions=())
    assert plan.capital_jpy == Decimal(2_000_000)
    assert plan.slot_value == Decimal(400_000)
    assert plan.used_slots == 0 and plan.free_slots == 5


def test_held_positions_occupy_slots(mandates):
    positions = (
        PositionState(fm="ben", instrument_id=1, asset_class="equity_jp",
                      qty=Decimal(100), avg_cost=Decimal(1000)),
        PositionState(fm="ben", instrument_id=2, asset_class="equity_jp",
                      qty=Decimal(200), avg_cost=Decimal(500)),
        # 別ポッドの保有は Ben のスロットを消費しない。
        PositionState(fm="jim", instrument_id=3, asset_class="equity_jp",
                      qty=Decimal(300), avg_cost=Decimal(400)),
    )
    plan = sizing.slot_plan(mandates["ben"], max_slots=5, positions=positions)
    assert plan.used_slots == 2 and plan.free_slots == 3
    assert plan.held_instruments == (1, 2)


def test_slots_must_respect_pod_concentration_limit(mandates):
    """1スロットがポッド内集中度上限を超える設定は load 時に落とす(fail-fast)。"""
    # ben の上限は 40% → 2 スロット(50%)は不可、3 スロット(33%)は可。
    with pytest.raises(sizing.SizingError, match="集中度"):
        sizing.check_slots(mandates["ben"], 2)
    sizing.check_slots(mandates["ben"], 3)


def test_shipped_configs_are_consistent_with_mandates(mandates):
    """発効中の config/fm_*.yaml のスロット数がマンデートと整合する(リグレッション検知)。"""
    sizing.check_slots(mandates["ben"], BenConfig.load().max_slots)
    sizing.check_slots(mandates["jim"], JimConfig.load().max_slots)


# ── 数量計算 ─────────────────────────────────────────────────────────────────
def test_entry_qty_floors_to_lot():
    """1単元 100株・価格 ¥1,234 → 400,000/123,400 = 3.24 単元 → 300 株(切り捨て)。"""
    qty = sizing.entry_qty(
        slot_value=Decimal(400_000), price=Decimal(1_234), lot_size=Decimal(100)
    )
    assert qty == Decimal(300)
    assert qty * Decimal(1_234) <= Decimal(400_000)  # スロットを超えない


def test_entry_qty_zero_when_below_one_lot():
    """1単元が1スロットより高ければ発注しない(0 を返す)。"""
    assert sizing.entry_qty(
        slot_value=Decimal(400_000), price=Decimal(9_000), lot_size=Decimal(100)
    ) == Decimal(0)


def test_entry_qty_without_lot_size():
    """単元の定めがない銘柄(米国株等)は1株単位。"""
    assert sizing.entry_qty(
        slot_value=Decimal(400_000), price=Decimal(3_000), lot_size=None
    ) == Decimal(133)


def test_entry_qty_rejects_non_positive_price():
    with pytest.raises(sizing.SizingError):
        sizing.entry_qty(slot_value=Decimal(400_000), price=Decimal(0))


def test_close_qty_is_full_position():
    positions = (
        PositionState(fm="ben", instrument_id=1, asset_class="equity_jp",
                      qty=Decimal(300), avg_cost=Decimal(1000)),
    )
    assert sizing.close_qty(positions, "ben", 1) == Decimal(300)
    assert sizing.close_qty(positions, "ben", 2) == Decimal(0)
    assert sizing.close_qty(positions, "jim", 1) == Decimal(0)
