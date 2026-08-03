"""sizing — FM 共通の決定論サイジング(スロット制 MVP。T-017)。

**不変原則1 の実装**: LLM(および決定論シグナル)の出力は「どの銘柄を候補にするか」
= **採否**だけを決め、金額は決めない。本モジュールの関数が受け取るのは

- マンデート(``ryza.ips.Mandate``)の**仮想資本**
- config(``config/fm_<name>.yaml``)の**スロット数**
- 現在ポジション・参照価格・売買単位

だけであり、**確信度・スコア・ランキング・順位づけの値を受け取る引数は存在しない**。
この性質はテスト(``tests/fm/test_sizing.py``)がシグネチャ検査で固定する — 将来の改修で
確信度を持ち込もうとするとテストが落ちる。

スロット制の定義:

- ポッド仮想資本を最大 N スロットに**等分**する(1スロットの金額 = 資本 ÷ N)
- 新規建ては空きスロットが1つ必要。既に保有している銘柄は1スロットを占有している
- 数量は「1スロットの金額 ÷ 参照価格」を**売買単位で切り捨て**た株数。1単元に満たなければ 0
  (= 発注しない)。切り捨てにより約定代金は必ず1スロット以下に収まる
- invalidation 成立時のクローズは**保有数量の全量**(部分解消は行わない — MVP)

スロット数の妥当性はマンデートに照らして検証する(``check_slots``): 1スロットが
ポッド内集中度上限を超える設定は、ゲートで必ず block される無意味な設定なので load 時に
落とす(fail-fast)。**ゲートを緩めることは一切しない** — ここは狭める方向の自主規制のみ。
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from decimal import ROUND_FLOOR, Decimal

from ryza.gate.compliance import PositionState
from ryza.ips import Mandate


class SizingError(ValueError):
    """サイジング設定・入力の不整合(スロット数がマンデートに反する等)。"""


@dataclass(frozen=True)
class SlotPlan:
    """ある FM の、ある時点でのスロット状況(決定論)。"""

    fm: str
    capital_jpy: Decimal
    max_slots: int
    slot_value: Decimal  # 1スロットの金額(資本 ÷ スロット数)
    used_slots: int  # 保有中の銘柄数(= 占有スロット)
    held_instruments: tuple[int, ...]

    @property
    def free_slots(self) -> int:
        return max(self.max_slots - self.used_slots, 0)


def check_slots(mandate: Mandate, max_slots: int) -> None:
    """スロット数がマンデートと整合するか(狭める方向の自主検証)。

    1スロットの資本比率がポッド内集中度上限を超えるなら、その設定で建てた新規建ては
    ゲート G-3 で必ず block される。設定ミスを実行時ではなく load 時に露見させる。
    """
    if max_slots <= 0:
        raise SizingError(f"スロット数は正であるべき: {max_slots}")
    slot_ratio = Decimal(1) / Decimal(max_slots)
    limit = Decimal(str(mandate.pod_concentration_limit))
    if slot_ratio > limit:
        raise SizingError(
            f"{mandate.fm}: 1スロット {slot_ratio:.1%} がポッド内集中度上限 "
            f"{limit:.0%}(config/mandates/{mandate.fm}.yaml)を超える — "
            "ゲート G-3 で必ず block される設定"
        )


def held_positions(
    positions: Iterable[PositionState], fm: str
) -> dict[int, Decimal]:
    """当該 FM の保有(数量 ≠ 0)を instrument_id → 符号付き数量で返す。"""
    held: dict[int, Decimal] = {}
    for pos in positions:
        if pos.fm != fm or pos.qty == 0:
            continue
        held[pos.instrument_id] = held.get(pos.instrument_id, Decimal(0)) + pos.qty
    return {iid: qty for iid, qty in held.items() if qty != 0}


def slot_plan(
    mandate: Mandate, *, max_slots: int, positions: Iterable[PositionState]
) -> SlotPlan:
    """マンデートの仮想資本と現在ポジションからスロット状況を組む。

    引数に確信度・スコアの類は無い(モジュール docstring)。
    """
    check_slots(mandate, max_slots)
    capital = Decimal(mandate.capital_jpy)
    held = held_positions(positions, mandate.fm)
    return SlotPlan(
        fm=mandate.fm,
        capital_jpy=capital,
        max_slots=max_slots,
        slot_value=capital / Decimal(max_slots),
        used_slots=len(held),
        held_instruments=tuple(sorted(held)),
    )


def entry_qty(
    *, slot_value: Decimal, price: Decimal, lot_size: Decimal | None = None
) -> Decimal:
    """1スロット分の新規建て数量(売買単位で切り捨て)。1単元に満たなければ 0。

    切り捨てのため約定代金は必ず ``slot_value`` 以下になる(スロットを超過しない)。
    """
    if price <= 0:
        raise SizingError(f"参照価格は正であるべき: {price}")
    lot = Decimal(1) if lot_size is None or lot_size <= 0 else lot_size
    lots = (slot_value / (price * lot)).to_integral_value(rounding=ROUND_FLOOR)
    if lots <= 0:
        return Decimal(0)
    return lots * lot


def close_qty(positions: Iterable[PositionState], fm: str, instrument_id: int) -> Decimal:
    """クローズ数量 = 当該 FM の保有数量の絶対値(全量解消。MVP は部分解消なし)。"""
    held = held_positions(positions, fm)
    return abs(held.get(instrument_id, Decimal(0)))


__all__ = [
    "SizingError",
    "SlotPlan",
    "check_slots",
    "close_qty",
    "entry_qty",
    "held_positions",
    "slot_plan",
]
