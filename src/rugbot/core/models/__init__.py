"""Universal, blockchain-agnostic domain models."""

from __future__ import annotations

from rugbot.core.models.address import Address
from rugbot.core.models.candle import OHLCCandle, TradeTick, build_ohlc_candles
from rugbot.core.models.order import (
    DEFAULT_SLIPPAGE_BPS,
    MAX_SLIPPAGE_BPS,
    ExecutionMode,
    FillStatus,
    OrderIntent,
    OrderSide,
    TradeReceipt,
)
from rugbot.core.models.position import ActivePosition, ClosedTrade, PortfolioMetrics
from rugbot.core.models.quote import ExecutionQuote
from rugbot.core.models.token import TokenAmount, TokenMetadata

__all__ = [
    "DEFAULT_SLIPPAGE_BPS",
    "MAX_SLIPPAGE_BPS",
    "ActivePosition",
    "Address",
    "ClosedTrade",
    "ExecutionMode",
    "ExecutionQuote",
    "FillStatus",
    "OHLCCandle",
    "OrderIntent",
    "OrderSide",
    "PortfolioMetrics",
    "TokenAmount",
    "TokenMetadata",
    "TradeReceipt",
    "TradeTick",
    "build_ohlc_candles",
]
