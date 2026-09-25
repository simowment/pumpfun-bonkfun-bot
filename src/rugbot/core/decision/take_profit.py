"""Chain-agnostic multi-tier take-profit ladder calculator."""

from __future__ import annotations

from dataclasses import dataclass

DEFAULT_TP_LADDER = (
    (50.0, 0.33),  # +50% gain: sell 33%
    (100.0, 0.33),  # +100% gain: sell another 33%
    (200.0, 1.0),  # +200% gain: close remainder
)


@dataclass(frozen=True, slots=True)
class TakeProfitTier:
    """A single take-profit tier threshold and allocation."""

    target_gain_pct: float
    sell_fraction: float


@dataclass(frozen=True, slots=True)
class TakeProfitLadder:
    """Manages tiered profit-taking execution."""

    tiers: tuple[TakeProfitTier, ...]
    executed_tier_indices: tuple[int, ...] = ()

    @classmethod
    def from_tuples(
        cls, levels: tuple[tuple[float, float], ...] = DEFAULT_TP_LADDER
    ) -> TakeProfitLadder:
        """Construct ladder from (gain_pct, sell_fraction) tuples."""
        tiers = tuple(
            TakeProfitTier(target_gain_pct=g, sell_fraction=f) for g, f in levels
        )
        return cls(tiers=tiers)

    def evaluate(
        self, entry_price: float, current_price: float
    ) -> tuple[TakeProfitLadder, float]:
        """Evaluate whether an unexecuted TP tier is triggered.

        Args:
            entry_price: Original entry price.
            current_price: Current market price.

        Returns:
            Tuple of (updated_ladder, fraction_to_sell).
        """
        if entry_price <= 0 or current_price <= 0:
            return self, 0.0

        current_gain_pct = ((current_price - entry_price) / entry_price) * 100.0

        for idx, tier in enumerate(self.tiers):
            if (
                idx not in self.executed_tier_indices
                and current_gain_pct >= tier.target_gain_pct
            ):
                updated_indices = (*self.executed_tier_indices, idx)
                updated_ladder = TakeProfitLadder(
                    tiers=self.tiers, executed_tier_indices=updated_indices
                )
                return updated_ladder, tier.sell_fraction

        return self, 0.0
