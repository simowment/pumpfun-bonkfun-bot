"""Risk management and capital preservation gatekeeper."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class RiskConfig:
    """Configurable risk thresholds."""

    max_concurrent_positions: int = 5
    max_total_exposure_native: float = 10.0
    max_single_position_native: float = 2.0
    max_daily_loss_native: float = 5.0
    emergency_halt: bool = False


class RiskGatekeeper:
    """Enforces fail-closed risk controls before order execution."""

    def __init__(self, config: RiskConfig) -> None:
        self.config = config

    def can_open_position(
        self,
        current_open_count: int,
        current_total_exposure_native: float,
        proposed_size_native: float,
        current_daily_loss_native: float,
    ) -> tuple[bool, str | None]:
        """Validate whether a new buy order passes all risk parameters."""
        if self.config.emergency_halt:
            return False, "Emergency halt is active"

        if current_open_count >= self.config.max_concurrent_positions:
            return (
                False,
                f"Max concurrent positions reached ({self.config.max_concurrent_positions})",
            )

        if proposed_size_native > self.config.max_single_position_native:
            return False, (
                f"Proposed order size {proposed_size_native:.4f} exceeds max "
                f"position cap {self.config.max_single_position_native:.4f}"
            )

        new_total = current_total_exposure_native + proposed_size_native
        if new_total > self.config.max_total_exposure_native:
            return False, (
                f"Total exposure {new_total:.4f} would exceed max capital cap "
                f"{self.config.max_total_exposure_native:.4f}"
            )

        if current_daily_loss_native >= self.config.max_daily_loss_native:
            return False, (
                f"Daily loss limit {self.config.max_daily_loss_native:.4f} reached "
                f"(current: {current_daily_loss_native:.4f})"
            )

        return True, None
