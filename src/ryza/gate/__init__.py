"""gate — コンプライアンスゲート(唯一の発注経路。保護領域 — 定款第5条)。

- ``compliance``: 純決定論のゲート判定(G-0〜G-10)。LLM はこの経路に一切関与しない
  (CLAUDE.md 不変原則1)
- ``orders``: 付帯アプリ層 — ``gate_and_record``(唯一の注文記録入口)・
  ``apply_execution``(約定のポジション反映)・``advance_order_status``(状態遷移の強制)
"""

from ryza.gate.compliance import (
    GateResult,
    LimitsState,
    OrderProposal,
    PortfolioState,
    PositionState,
    Reason,
    evaluate,
    mandates_hash,
)
from ryza.gate.orders import (
    OrderStatusError,
    advance_order_status,
    apply_execution,
    gate_and_record,
    record_execution,
)

__all__ = [
    "GateResult",
    "LimitsState",
    "OrderProposal",
    "OrderStatusError",
    "PortfolioState",
    "PositionState",
    "Reason",
    "advance_order_status",
    "apply_execution",
    "evaluate",
    "gate_and_record",
    "mandates_hash",
    "record_execution",
]
