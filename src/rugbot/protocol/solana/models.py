"""Normalized Solana protocol domain models."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class SolTransfer:
    """Normalized native SOL transfer event across outer and inner instructions."""

    signature: str
    instruction_index: int
    slot: int
    timestamp: int
    sender: str
    recipient: str
    lamports: int

    @property
    def sol_amount(self) -> float:
        """Convenience decimal representation for display and logging."""
        return self.lamports / 1_000_000_000
