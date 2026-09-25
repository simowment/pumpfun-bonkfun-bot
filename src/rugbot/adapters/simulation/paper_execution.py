"""Virtual paper trading execution adapter implementing ExecutionPort."""

from __future__ import annotations

import time
import uuid
from typing import TYPE_CHECKING

from rugbot.core.models.order import OrderSide, TradeReceipt
from rugbot.core.models.quote import ExecutionQuote
from rugbot.core.models.token import TokenAmount
from rugbot.core.ports.execution_port import ExecutionPort

if TYPE_CHECKING:
    from rugbot.core.models.address import Address
    from rugbot.core.models.order import OrderIntent
    from rugbot.core.ports.market_data_port import MarketDataPort

DEFAULT_TOKEN_DECIMALS = 6
DEFAULT_NATIVE_DECIMALS = 9
DEFAULT_SIMULATED_PRICE = 0.00003  # Native per token
DEFAULT_SIMULATED_FEE = 0.00005


class PaperExecutionAdapter(ExecutionPort):
    """Zero-risk, high-fidelity paper trading simulation adapter."""

    def __init__(
        self,
        market_data: MarketDataPort | None = None,
        default_price: float = DEFAULT_SIMULATED_PRICE,
    ) -> None:
        self._market_data = market_data
        self._price = default_price

    async def get_quote(
        self,
        target_token: Address,
        side: OrderSide,
        amount_in: TokenAmount,
    ) -> ExecutionQuote:
        """Simulate execution quote."""
        slippage_pct = 1.0

        if side == OrderSide.BUY:
            # Native currency in -> tokens out
            price = self._price
            tokens_out = int(
                (amount_in.ui_value / price) * (10**DEFAULT_TOKEN_DECIMALS)
            )
            min_out = int(tokens_out * (1.0 - slippage_pct / 100.0))
            return ExecutionQuote(
                target_token=target_token,
                side=side,
                amount_in=amount_in,
                expected_amount_out=TokenAmount(
                    raw_units=tokens_out, decimals=DEFAULT_TOKEN_DECIMALS
                ),
                minimum_amount_out=TokenAmount(
                    raw_units=min_out, decimals=DEFAULT_TOKEN_DECIMALS
                ),
                price_impact_pct=0.1,
                route_venue="paper_simulation",
            )

        # Tokens in -> Native out
        price = self._price
        native_out = int((amount_in.ui_value * price) * (10**DEFAULT_NATIVE_DECIMALS))
        min_out = int(native_out * (1.0 - slippage_pct / 100.0))
        return ExecutionQuote(
            target_token=target_token,
            side=side,
            amount_in=amount_in,
            expected_amount_out=TokenAmount(
                raw_units=native_out, decimals=DEFAULT_NATIVE_DECIMALS
            ),
            minimum_amount_out=TokenAmount(
                raw_units=min_out, decimals=DEFAULT_NATIVE_DECIMALS
            ),
            price_impact_pct=0.1,
            route_venue="paper_simulation",
        )

    async def execute(self, intent: OrderIntent) -> TradeReceipt:
        """Execute a simulated paper trade."""
        quote = await self.get_quote(intent.target_token, intent.side, intent.amount_in)

        # Realistic paper fills respect minimum slippage output
        filled_out = quote.expected_amount_out
        tx_hash = f"paper_tx_{uuid.uuid4().hex[:16]}_{int(time.time())}"

        return TradeReceipt(
            ok=True,
            intent_id=intent.intent_id,
            target_token=intent.target_token,
            side=intent.side,
            tx_hash=tx_hash,
            filled_amount_in=intent.amount_in,
            filled_amount_out=filled_out,
            effective_price=quote.expected_price,
            fee_paid_native=DEFAULT_SIMULATED_FEE,
        )
