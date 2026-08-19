"""Execution telemetry recording latency milestones, slot deltas, and costs."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True)
class ExecutionMetrics:
    """Detailed execution telemetry for one trading intent."""

    target_wallet: str
    token_mint: str

    # Slots
    event_slot: int | None = None
    creation_slot: int | None = None
    landed_slot: int | None = None

    # Hot path milestones (nanoseconds)
    t_received_ns: int = 0
    t_decoded_ns: int = 0
    t_matched_ns: int = 0
    t_built_ns: int = 0
    t_signed_ns: int = 0

    # Submission ACKs
    jito_ack_ms: float | None = None
    rpc_ack_ms: float | None = None
    first_ack_sender: str | None = None

    # Observation
    first_observed_ns: int | None = None

    # Fees & Cost
    priority_fee_microlamports: int = 0
    jito_tip_lamports: int = 0

    # Status
    success: bool = False
    error: str | None = None

    @property
    def hot_path_ms(self) -> float:
        """Calculate hot-path latency from event receipt to local signature."""
        if self.t_signed_ns > 0 and self.t_received_ns > 0:
            return (self.t_signed_ns - self.t_received_ns) / 1_000_000.0
        return 0.0

    @property
    def observed_latency_ms(self) -> float | None:
        """Calculate total latency from event receipt to first on-chain observation."""
        if self.first_observed_ns and self.t_received_ns > 0:
            return (self.first_observed_ns - self.t_received_ns) / 1_000_000.0
        return None

    @property
    def delta_slots(self) -> int | None:
        """Calculate block delta between creation and inclusion."""
        if self.creation_slot is None or self.landed_slot is None:
            return None
        return self.landed_slot - self.creation_slot

    @property
    def block_class(self) -> str | None:
        """Classify landing speed based on slot delta."""
        delta = self.delta_slots
        if delta is None:
            return None
        if delta < 0:
            return "INVALID"
        if delta == 0:
            return "B0"
        if delta == 1:
            return "B1"
        return "B2+"
