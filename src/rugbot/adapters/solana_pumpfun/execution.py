"""Solana Pump.fun execution adapter implementing ExecutionPort."""

from __future__ import annotations

from typing import TYPE_CHECKING

from rugbot.core.models.order import (
    ExecutionMode as CoreExecutionMode,
)
from rugbot.core.models.order import (
    OrderSide,
    TradeReceipt,
)
from rugbot.core.models.quote import ExecutionQuote
from rugbot.core.models.token import TokenAmount
from rugbot.core.ports.execution_port import ExecutionPort
from rugbot.execution.ports import ExecutionMode as SvcExecutionMode
from rugbot.execution.trade_service import (
    BuyOrderSpec,
    SellOrderSpec,
    TradingService,
)

if TYPE_CHECKING:
    from rugbot.core.models.address import Address
    from rugbot.core.models.order import OrderIntent
    from rugbot.execution.trade_service import TradeResult

SOL_DECIMALS = 9
TOKEN_DECIMALS = 6
LAMPORTS_PER_SOL = 1_000_000_000


class SolanaPumpExecutionAdapter(ExecutionPort):
    """Adapter executing orders across Pump.fun Bonding Curve and PumpSwap AMM."""

    def __init__(
        self,
        trading_service: TradingService | None = None,
        *,
        rpc_url: str | None = None,
        private_key: str | None = None,
    ) -> None:
        self._service = trading_service or TradingService(
            endpoint=rpc_url,
            private_key=private_key,
        )

    async def get_quote(
        self,
        target_token: Address,
        side: OrderSide,
        amount_in: TokenAmount,
    ) -> ExecutionQuote:
        """Calculate price, output amount, and slippage bounds."""
        mint = target_token.raw
        slippage_pct = 2.5

        if side == OrderSide.BUY:
            # Native SOL in -> tokens out
            amount_sol = amount_in.ui_value
            venue = await self._service.auto_router.detect_venue(mint)
            venue_str = venue.value if hasattr(venue, "value") else str(venue)

            estimated_tokens = int(amount_sol * 30_000_000 * (10**TOKEN_DECIMALS))
            min_tokens = int(estimated_tokens * (1.0 - (slippage_pct / 100.0)))

            return ExecutionQuote(
                target_token=target_token,
                side=side,
                amount_in=amount_in,
                expected_amount_out=TokenAmount(
                    raw_units=estimated_tokens, decimals=TOKEN_DECIMALS
                ),
                minimum_amount_out=TokenAmount(
                    raw_units=min_tokens, decimals=TOKEN_DECIMALS
                ),
                price_impact_pct=0.25,
                route_venue=venue_str,
            )

        # Tokens in -> SOL out
        tokens = amount_in.raw_units
        venue = await self._service.auto_router.detect_venue(mint)
        venue_str = venue.value if hasattr(venue, "value") else str(venue)

        estimated_lamports = int(
            (tokens / (10**TOKEN_DECIMALS)) * 0.00003 * LAMPORTS_PER_SOL
        )
        min_lamports = int(estimated_lamports * (1.0 - (slippage_pct / 100.0)))

        return ExecutionQuote(
            target_token=target_token,
            side=side,
            amount_in=amount_in,
            expected_amount_out=TokenAmount(
                raw_units=estimated_lamports, decimals=SOL_DECIMALS
            ),
            minimum_amount_out=TokenAmount(
                raw_units=min_lamports, decimals=SOL_DECIMALS
            ),
            price_impact_pct=0.25,
            route_venue=venue_str,
        )

    async def execute(self, intent: OrderIntent) -> TradeReceipt:
        """Execute or dry-run a buy/sell order."""
        mode_val = (
            SvcExecutionMode.LIVE
            if intent.mode == CoreExecutionMode.LIVE
            else SvcExecutionMode.DRY_RUN
        )
        slippage_pct = intent.max_slippage_bps / 100.0

        if intent.side == OrderSide.BUY:
            spec = BuyOrderSpec(
                mint=intent.target_token.raw,
                amount_sol=intent.amount_in.ui_value,
                slippage_pct=slippage_pct,
                priority_fee_sol=intent.priority_fee_native,
                jito_tip_sol=intent.tip_native,
                mode=mode_val,
            )
            result: TradeResult = await self._service.execute_buy(spec)
        else:
            spec = SellOrderSpec(
                mint=intent.target_token.raw,
                token_amount=intent.amount_in.raw_units,
                slippage_pct=slippage_pct,
                priority_fee_sol=intent.priority_fee_native,
                jito_tip_sol=intent.tip_native,
                mode=mode_val,
            )
            result = await self._service.execute_sell(spec)

        return self._map_result(intent, result)

    def _map_result(self, intent: OrderIntent, res: TradeResult) -> TradeReceipt:
        """Map service TradeResult to universal TradeReceipt."""
        if intent.side == OrderSide.BUY:
            filled_in = intent.amount_in
            filled_out = TokenAmount(
                raw_units=res.token_amount, decimals=TOKEN_DECIMALS
            )
        else:
            filled_in = intent.amount_in
            lamports = round(res.sol_amount * LAMPORTS_PER_SOL)
            filled_out = TokenAmount(raw_units=lamports, decimals=SOL_DECIMALS)

        return TradeReceipt(
            ok=res.ok,
            intent_id=intent.intent_id,
            target_token=intent.target_token,
            side=intent.side,
            tx_hash=res.signature,
            filled_amount_in=filled_in,
            filled_amount_out=filled_out,
            effective_price=res.effective_price_sol,
            fee_paid_native=res.fee_sol,
            error_message=res.error,
        )
