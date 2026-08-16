"""Versioned fee configuration contracts."""

from dataclasses import dataclass

from rugbot.domain.amounts import Slot

BASIS_POINTS_DENOMINATOR = 10_000


@dataclass(frozen=True, slots=True)
class FeeConfig:
    """Known versioned fee configuration.

    Args:
        version: Historical fee/config version identifier.
        protocol_fee_bps: Protocol fee in basis points.
        creator_fee_bps: Creator fee in basis points.
        is_known: Whether this config came from a verified historical artifact.
        program_config_version: Program/config version this fee schedule applies to.
        valid_from_slot: Inclusive slot where this fee schedule becomes valid.
        valid_to_slot: Exclusive slot where this fee schedule stops being valid.
        source_artifact_version: Artifact version proving the fee schedule.
    """

    version: str
    protocol_fee_bps: int
    creator_fee_bps: int
    is_known: bool
    program_config_version: str | None = None
    valid_from_slot: Slot | None = None
    valid_to_slot: Slot | None = None
    source_artifact_version: str | None = None
    lp_fee_bps: int = 0

    @property
    def total_fee_bps(self) -> int:
        """Total protocol plus creator fee in basis points."""

        return self.protocol_fee_bps + self.creator_fee_bps

    @property
    def swap_total_fee_bps(self) -> int:
        """Total PumpSwap LP, protocol, and creator fee in basis points."""

        return self.lp_fee_bps + self.protocol_fee_bps + self.creator_fee_bps
