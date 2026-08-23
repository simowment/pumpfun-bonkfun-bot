"""Domain model for parsed native SOL transfers."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class SolTransfer:
    """Parsed native SOL transfer record."""

    signature: str
    instruction_index: int = 0
    slot: int = 0
    timestamp: int | None = None
    sender: str = ""
    recipient: str = ""
    lamports: int = 0
    source: str = ""
    destination: str = ""
    amount_lamports: int = 0

    def __post_init__(self) -> None:
        # Align aliases
        if self.sender and not self.source:
            object.__setattr__(self, "source", self.sender)
        elif self.source and not self.sender:
            object.__setattr__(self, "sender", self.source)

        if self.recipient and not self.destination:
            object.__setattr__(self, "destination", self.recipient)
        elif self.destination and not self.recipient:
            object.__setattr__(self, "recipient", self.destination)

        if self.lamports and not self.amount_lamports:
            object.__setattr__(self, "amount_lamports", self.lamports)
        elif self.amount_lamports and not self.lamports:
            object.__setattr__(self, "lamports", self.amount_lamports)

    @property
    def amount_sol(self) -> float:
        """Convert lamports to SOL float."""
        return (self.amount_lamports or self.lamports) / 1_000_000_000

    @property
    def sol_amount(self) -> float:
        """Convert lamports to SOL float."""
        return self.amount_sol


__all__ = ["SolTransfer"]
