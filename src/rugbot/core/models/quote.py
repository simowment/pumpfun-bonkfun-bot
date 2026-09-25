"""Quote calculations and price impact contracts."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from rugbot.core.models.order import OrderSide

if TYPE_CHECKING:
    from rugbot.core.models.address import Address
    from rugbot.core.models.token import TokenAmount


@dataclass(frozen=True, slots=True)
class ExecutionQuote:
    """Pre-trade quote modeling expected returns, slippage bounds, and venue."""

    target_token: Address
    side: OrderSide
    amount_in: TokenAmount
    expected_amount_out: TokenAmount
    minimum_amount_out: TokenAmount
    price_impact_pct: float
    route_venue: str

    @property
    def expected_price(self) -> float:
        """Calculate the expected price in quote units per token."""
        if self.side == OrderSide.BUY:
            # quote in -> tokens out
            return (
                self.amount_in.ui_value / self.expected_amount_out.ui_value
                if self.expected_amount_out.ui_value > 0
                else 0.0
            )
        # tokens in -> quote out
        return (
            self.expected_amount_out.ui_value / self.amount_in.ui_value
            if self.amount_in.ui_value > 0
            else 0.0
        )
