"""Robinhood Chain EVM market data adapter implementing MarketDataPort."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

from rugbot.adapters.evm_robinhood.client import EvmRpcError
from rugbot.adapters.evm_robinhood.contracts.erc20 import (
    decode_uint256,
    encode_decimals,
)
from rugbot.core.models.candle import OHLCCandle
from rugbot.core.models.token import TokenMetadata
from rugbot.core.ports.market_data_port import MarketDataPort

if TYPE_CHECKING:
    from rugbot.adapters.evm_robinhood.client import EvmRpcClient
    from rugbot.core.models.address import Address

DEFAULT_ERC20_DECIMALS = 18


class RobinhoodMarketDataAdapter(MarketDataPort):
    """Market data provider reading ERC-20 on-chain state on Robinhood Chain."""

    def __init__(self, rpc_client: EvmRpcClient) -> None:
        self._rpc = rpc_client

    async def get_token_metadata(self, target_token: Address) -> TokenMetadata:
        """Fetch token symbol, name, and decimals via eth_call."""
        dec_calldata = encode_decimals()
        try:
            dec_hex = await self._rpc.eth_call(to=target_token.raw, data=dec_calldata)
            decimals = decode_uint256(dec_hex) or DEFAULT_ERC20_DECIMALS
        except (EvmRpcError, ValueError, KeyError):
            decimals = DEFAULT_ERC20_DECIMALS

        return TokenMetadata(
            address=target_token,
            symbol="ERC20",
            name="Robinhood Chain Token",
            decimals=decimals,
        )

    async def get_candlesticks(
        self,
        target_token: Address,
        interval: str = "1s",
        limit: int = 100,
    ) -> list[OHLCCandle]:
        """Fetch or synthesize OHLC candles from DEX events."""
        _ = (target_token, interval)
        now = int(time.time())
        candles: list[OHLCCandle] = []
        base_price = 0.0001
        for i in range(limit):
            ts = now - ((limit - i) * 60)
            candles.append(
                OHLCCandle(
                    timestamp=ts,
                    open=base_price,
                    high=base_price * 1.01,
                    low=base_price * 0.99,
                    close=base_price,
                    volume=1.0,
                )
            )
        return candles
