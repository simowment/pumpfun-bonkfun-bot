"""Decision and abstention domain objects."""

from dataclasses import dataclass
from enum import Enum


class AbstainReason(Enum):
    """Reasons that force the system to abstain from executable decisions."""

    UNKNOWN_PROTOCOL_STATE = "unknown_protocol_state"
    UNSUPPORTED_PROTOCOL_STATE = "unsupported_protocol_state"
    UNKNOWN_FEE_CONFIG = "unknown_fee_config"
    MISSING_FEATURE = "missing_feature"
    STALE_STATE = "stale_state"
    DECODER_MISMATCH = "decoder_mismatch"


@dataclass(frozen=True, slots=True)
class AbstainResult:
    """A deterministic abstention result.

    Args:
        reason: Machine-readable abstention reason.
        message: Human-readable diagnostic text.
        as_of_slot: Slot boundary for the state used to decide.
    """

    reason: AbstainReason
    message: str
    as_of_slot: int
