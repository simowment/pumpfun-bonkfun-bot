"""Protocols and models for transaction dispatch."""

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol


class RoutingPolicy(StrEnum):
    """Routing policy for transaction dispatch."""

    RPC_ONLY = "rpc"
    JITO_ONLY = "jito"


@dataclass(slots=True, frozen=True)
class SubmissionResult:
    """Acknowledgment result from a transaction sender."""

    sender_name: str
    signature: str
    ack_ms: float
    acknowledged: bool
    error_message: str | None = None


class TransactionSender(Protocol):
    """Protocol for concrete transaction senders (Jito, RPC)."""

    @property
    def name(self) -> str:
        """Return the unique identifier for this sender."""
        ...

    async def send_transaction(self, raw_tx_bytes: bytes) -> SubmissionResult:
        """Send raw signed transaction bytes and return acknowledgment result."""
        ...
