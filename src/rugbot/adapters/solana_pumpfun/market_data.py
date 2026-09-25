"""Solana Pump.fun market data adapter implementing MarketDataPort."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from rugbot.core.models.token import TokenMetadata
from rugbot.core.ports.market_data_port import MarketDataPort
from rugbot.integrations.pumpfun_api import PumpFunApiClient

if TYPE_CHECKING:
    from rugbot.core.models.address import Address
    from rugbot.core.models.candle import OHLCCandle
    from rugbot.integrations.solana_rpc import SolanaClient

DEFAULT_SOLANA_TOKEN_DECIMALS = 6


class SolanaPumpMarketDataAdapter(MarketDataPort):
    """Fetches real-time market data, metadata, and candlesticks for Pump.fun tokens."""

    def __init__(
        self,
        api_client: PumpFunApiClient | None = None,
        rpc_client: SolanaClient | None = None,
    ) -> None:
        self._api = api_client or PumpFunApiClient()
        self._rpc = rpc_client

    async def get_token_metadata(self, target_token: Address) -> TokenMetadata:
        """Fetch token symbol, name, and decimals."""
        coin_info = await asyncio.to_thread(self._api.get_coin_info, target_token.raw)
        if coin_info:
            symbol = coin_info.get("symbol") or "UNKNOWN"
            name = coin_info.get("name") or symbol
            decimals = int(coin_info.get("decimals", DEFAULT_SOLANA_TOKEN_DECIMALS))
            total_supply = coin_info.get("total_supply")
            return TokenMetadata(
                address=target_token,
                symbol=symbol,
                name=name,
                decimals=decimals,
                total_supply=int(total_supply) if total_supply is not None else None,
            )

        # Fallback when API returns empty
        return TokenMetadata(
            address=target_token,
            symbol="UNKNOWN",
            name="Unknown Solana Token",
            decimals=DEFAULT_SOLANA_TOKEN_DECIMALS,
        )

    async def get_candlesticks(
        self,
        target_token: Address,
        interval: str = "1s",
        limit: int = 100,
    ) -> list[OHLCCandle]:
        """Fetch standardized OHLCV candles from swap-api.pump.fun."""
        return await asyncio.to_thread(
            self._api.get_candlesticks,
            target_token.raw,
            interval=interval,
            limit=limit,
        )
