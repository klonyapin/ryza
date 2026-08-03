"""gate — コンプライアンスゲート(唯一の発注経路。保護領域 — 定款第5条)。

- ``compliance``: 純決定論のゲート判定(G-0〜G-10)。LLM はこの経路に一切関与しない
  (CLAUDE.md 不変原則1)
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

__all__ = [
    "GateResult",
    "LimitsState",
    "OrderProposal",
    "PortfolioState",
    "PositionState",
    "Reason",
    "evaluate",
    "mandates_hash",
]
