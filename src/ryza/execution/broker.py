"""ブローカー抽象(T-016)。

00-system-design §9「執行はデモ/実の二系統・同一コードパス」の境界。執行ループ
(``runner``)は本 Protocol にのみ依存し、デモ(``DemoBroker``)と将来の実ブローカー
(IBKR — docs/research/broker-data-apis.md の選定に基づく)を同一コードパスで
差し替える。

Broker は「1 注文を市場に出し、結果を返す」だけの境界であり、DB への記帳
(``trading.executions`` / ledger)と注文の状態遷移は runner の管轄。実装は
ここに DB 書込の副作用を持ち込まないこと(デモ実装が市場データを *読む* のは可)。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Protocol

# BrokerResult.status の値域。runner が注文状態へ写像する
# (filled→filled / rejected→rejected / expired→cancelled)。
FILLED = "filled"
REJECTED = "rejected"
EXPIRED = "expired"


@dataclass(frozen=True)
class BrokerOrder:
    """執行に必要な注文スナップショット(``trading.orders`` + gate_log の asset_class)。"""

    order_id: int
    book_id: str
    fm: str
    instrument_id: int
    side: str  # buy|sell|short|cover(trading.orders の CHECK と同値域)
    qty: Decimal
    order_type: str  # market|limit
    limit_price: Decimal | None = None
    ref_price: Decimal | None = None
    asset_class: str | None = None  # 手数料テーブルの引き当て(IPS §8.1 タクソノミー)


@dataclass(frozen=True)
class BrokerResult:
    """約定結果。``status=filled`` のとき qty/price/fee/executed_at は必須。

    - ``filled``: 全量約定(デモは部分約定を発生させない。``record_execution`` は
      部分約定対応済みのため、実ブローカーが部分約定を返す場合は複数回に分けて返す設計)
    - ``rejected``: 執行不能(バー欠落等)。``reason`` 必須
    - ``expired``: 当日中に約定条件へ達しなかった指値(guaranteed fill はしない)
    """

    status: str
    qty: Decimal | None = None
    price: Decimal | None = None
    fee: Decimal | None = None
    executed_at: datetime | None = None
    venue: str = "demo"
    broker_ref: str | None = None
    reason: str | None = None


class Broker(Protocol):
    """執行境界。実装: ``DemoBroker``(本タスク)/ IBKR 実装(実弾移行時)。"""

    def submit(self, order: BrokerOrder) -> BrokerResult:
        """注文 1 件を執行し、結果を返す(DB への書込はしない)。"""
        ...


__all__ = [
    "EXPIRED",
    "FILLED",
    "REJECTED",
    "Broker",
    "BrokerOrder",
    "BrokerResult",
]
