"""Real-time sniper engine, position tracking, and automated exit execution."""

# ruff: noqa: ARG002

from __future__ import annotations

from typing import TYPE_CHECKING

from rugbot.execution.ports import (
    ExecutionIntent,
    ExecutionMode,
    ExecutionPort,
    ExecutionReceipt,
)
from rugbot.execution.position_runtime import (
    PaperPositionState,
    PositionMarketEvidence,
    advance_paper_position,
)
from rugbot.execution.target_index import TargetIndex
from rugbot.execution.transaction_builder import TransactionBuilder

if TYPE_CHECKING:
    from rugbot.tracker.models import TargetExecutionPolicy


class SniperEngine:
    """Orchestrates fast-path target matching, bundle construction, and automated position management."""

    def __init__(self, target_index: TargetIndex | None = None) -> None:
        self._target_index = target_index or TargetIndex()

    @property
    def target_index(self) -> TargetIndex:
        return self._target_index

    def evaluate_launch_candidate(
        self, mint: str, creator: str, root_funder: str | None = None
    ) -> TargetExecutionPolicy | None:
        """Check if an incoming launch matches an active, armed target policy."""
        matched = self._target_index.match(creator)
        if matched is None and root_funder:
            matched = self._target_index.match(root_funder)
        return matched

    def build_snipe_intent(
        self,
        mint: str,
        creator: str,
        policy: TargetExecutionPolicy,
        mode: ExecutionMode = ExecutionMode.PAPER,
    ) -> ExecutionIntent:
        """Construct a buy execution intent according to target policy."""
        return TransactionBuilder.build_buy_intent(
            mint=mint,
            creator=creator,
            quote_lamports=policy.quote_size_lamports,
            max_slippage_bps=policy.max_slippage_bps,
            mode=mode,
        )


__all__ = [
    "ExecutionIntent",
    "ExecutionMode",
    "ExecutionPort",
    "ExecutionReceipt",
    "PaperPositionState",
    "PositionMarketEvidence",
    "SniperEngine",
    "TargetIndex",
    "TransactionBuilder",
    "advance_paper_position",
]
