"""Chain-agnostic trailing stop calculator."""

from __future__ import annotations

from dataclasses import dataclass

DEFAULT_TRAILING_STOP_PCT = 15.0


@dataclass(frozen=True, slots=True)
class TrailingStopState:
    """State tracking the highest price seen and trailing stop trigger."""

    high_water_mark: float
    trailing_pct: float = DEFAULT_TRAILING_STOP_PCT
    activation_threshold_pct: float = 0.0

    def update(self, current_price: float) -> tuple[TrailingStopState, bool]:
        """Update high-water mark and check if trailing stop triggered.

        Returns:
            Tuple of (updated_state, should_exit).
        """
        if current_price <= 0:
            return self, False

        new_high = max(self.high_water_mark, current_price)
        new_state = TrailingStopState(
            high_water_mark=new_high,
            trailing_pct=self.trailing_pct,
            activation_threshold_pct=self.activation_threshold_pct,
        )

        # Calculate drop from peak
        drop_pct = ((new_high - current_price) / new_high) * 100.0
        should_exit = drop_pct >= self.trailing_pct

        return new_state, should_exit
