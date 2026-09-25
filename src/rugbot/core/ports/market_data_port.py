"""Market data port boundary contract."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from rugbot.core.models.address import Address
    from rugbot.core.models.candle import OHLCCandle
    from rugbot.core.models.token import TokenMetadata


class MarketDataPort(ABC):
    """Port responsible for price feeds, OHLCV candles, and token metadata."""

    @abstractmethod
    async def get_token_metadata(self, target_token: Address) -> TokenMetadata:
        """Fetch token symbol, name, decimals, and total supply."""
        raise NotImplementedError

    @abstractmethod
    async def get_candlesticks(
        self,
        target_token: Address,
        interval: str = "1s",
        limit: int = 100,
    ) -> list[OHLCCandle]:
        """Fetch standardized OHLCV candles."""
        raise NotImplementedError
