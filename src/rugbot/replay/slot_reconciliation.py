"""Slot-status reconciliation from provisional to canonical history."""

from dataclasses import dataclass
from uuid import UUID

from rugbot.domain.observations import (
    CanonicalStatus,
    Commitment,
    RawChainObservation,
)

_COMMITMENT_RANK: dict[Commitment, int] = {
    "processed": 1,
    "confirmed": 2,
    "finalized": 3,
}
_TERMINAL_STATUSES: set[CanonicalStatus] = {"canonical", "dead_fork"}


class SlotReconciliationError(ValueError):
    """Raised when a non-slot observation is applied to slot reconciliation."""

    def __init__(self) -> None:
        """Initialize the slot reconciliation error."""

        super().__init__("observation is not a slot update")


@dataclass(frozen=True, slots=True)
class SlotReconciliationState:
    """Current source-derived reconciliation state for one slot."""

    slot: int
    parent_slot: int | None
    commitment: Commitment
    canonical_status: CanonicalStatus
    last_raw_id: UUID
    last_receive_sequence: int

    @property
    def is_terminal(self) -> bool:
        """Whether this slot can no longer be promoted by provisional updates."""

        return self.canonical_status in _TERMINAL_STATUSES


class SlotReconciler:
    """In-memory slot reconciliation state machine for M0 ingestion tests."""

    def __init__(self) -> None:
        """Initialize an empty reconciler."""

        self._states: dict[int, SlotReconciliationState] = {}

    def apply(self, observation: RawChainObservation) -> SlotReconciliationState:
        """Apply one slot observation to the reconciliation state.

        Args:
            observation: Raw slot observation to reconcile.

        Returns:
            Current state for the observation slot after reconciliation.
        """

        if observation.source_update_kind != "slot":
            raise SlotReconciliationError

        previous = self._states.get(observation.slot)
        next_state = _next_state(previous, _state_from_observation(observation))
        self._states[observation.slot] = next_state
        return next_state

    def get(self, slot: int) -> SlotReconciliationState | None:
        """Return the current state for a slot when known."""

        return self._states.get(slot)


def _next_state(
    previous: SlotReconciliationState | None,
    candidate: SlotReconciliationState,
) -> SlotReconciliationState:
    if previous is None:
        return candidate
    if previous.is_terminal:
        return previous
    if candidate.is_terminal:
        return candidate
    if _commitment_rank(candidate.commitment) >= _commitment_rank(previous.commitment):
        return candidate
    return previous


def _state_from_observation(
    observation: RawChainObservation,
) -> SlotReconciliationState:
    return SlotReconciliationState(
        slot=observation.slot,
        parent_slot=observation.parent_slot,
        commitment=observation.commitment,
        canonical_status=observation.canonical_status,
        last_raw_id=observation.raw_id,
        last_receive_sequence=observation.receive_sequence,
    )


def _commitment_rank(commitment: Commitment) -> int:
    return _COMMITMENT_RANK[commitment]
