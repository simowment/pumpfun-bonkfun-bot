"""High-resolution 1-second and multi-timeframe OHLC candlestick aggregation engine for Pump.fun tokens."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class TradeTick:
    """Raw decoded on-chain trade tick."""

    timestamp: int  # BlockTime in seconds
    price: float  # Price in SOL per token
    volume: float  # Volume in SOL
    is_buy: bool
    signature: str


@dataclass(frozen=True, slots=True)
class OHLCCandle:
    """Standardized Open-High-Low-Close-Volume candle."""

    timestamp: int  # Unix epoch timestamp in seconds
    open: float  # Price in SOL
    high: float
    low: float
    close: float
    volume: float  # Volume in SOL


def build_ohlc_candles(
    ticks: list[TradeTick],
    *,
    timeframe_seconds: int = 1,
    max_candles: int = 500,
    fill_empty: bool = True,
) -> list[OHLCCandle]:
    """Resample trade ticks into continuous 1-second (or custom interval) OHLCV candles."""
    if not ticks:
        return []

    sorted_ticks = sorted(ticks, key=lambda t: t.timestamp)
    start_ts = sorted_ticks[0].timestamp
    end_ts = sorted_ticks[-1].timestamp

    # Group ticks into timeframe buckets
    buckets: dict[int, list[TradeTick]] = defaultdict(list)
    for tick in sorted_ticks:
        b_ts = (tick.timestamp // timeframe_seconds) * timeframe_seconds
        buckets[b_ts].append(tick)

    candles: list[OHLCCandle] = []

    if fill_empty and (end_ts - start_ts) // timeframe_seconds <= max_candles * 3:
        # Continuous time series with forward-filled prices
        curr_price = sorted_ticks[0].price
        for ts in range(start_ts, end_ts + timeframe_seconds, timeframe_seconds):
            if ts in buckets:
                b_ticks = buckets[ts]
                prices = [t.price for t in b_ticks]
                vol = sum(t.volume for t in b_ticks)
                candles.append(
                    OHLCCandle(
                        timestamp=ts,
                        open=prices[0],
                        high=max(prices),
                        low=min(prices),
                        close=prices[-1],
                        volume=round(vol, 6),
                    )
                )
                curr_price = prices[-1]
            else:
                candles.append(
                    OHLCCandle(
                        timestamp=ts,
                        open=curr_price,
                        high=curr_price,
                        low=curr_price,
                        close=curr_price,
                        volume=0.0,
                    )
                )
    else:
        # Sparse non-empty buckets (Equal-width rendering)
        curr_price = sorted_ticks[0].price
        for ts in sorted(buckets.keys()):
            b_ticks = buckets[ts]
            prices = [t.price for t in b_ticks]
            vol = sum(t.volume for t in b_ticks)

            # Open at previous close to form contiguous visual bodies across time gaps
            c_open = curr_price
            c_close = prices[-1]
            c_high = max(*prices, c_open, c_close)
            c_low = min(*prices, c_open, c_close)

            candles.append(
                OHLCCandle(
                    timestamp=ts,
                    open=c_open,
                    high=c_high,
                    low=c_low,
                    close=c_close,
                    volume=round(vol, 6),
                )
            )
            curr_price = c_close

    return candles[-max_candles:]


__all__ = [
    "OHLCCandle",
    "TradeTick",
    "build_ohlc_candles",
]
