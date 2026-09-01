"""High-resolution 1-second and multi-timeframe OHLC candlestick aggregation engine for Pump.fun tokens."""

from __future__ import annotations

import asyncio
import base64
import json
import struct
import urllib.request
from collections import defaultdict
from dataclasses import dataclass

from solders.pubkey import Pubkey

from rugbot.integrations.solana_rpc import SolanaClient
from rugbot.runtime.config import load_provider_settings, resolve_dotenv
from rugbot.utils.logger import get_logger

logger = get_logger(__name__)

PUMP_PROGRAM_ID_STR = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
_TRADE_EVENT_DISCRIMINATOR = bytes([189, 219, 127, 211, 78, 230, 97, 238])


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


async def fetch_token_ohlc_candles(
    mint: str,
    *,
    timeframe_seconds: int = 1,
    max_candles: int = 300,
) -> list[OHLCCandle]:
    """Fetch exact on-chain trade ticks and aggregate into 1-second (or custom) OHLCV candles."""
    resolve_dotenv()
    settings = load_provider_settings()
    client = SolanaClient(settings.rpc_http)

    try:
        mint_pk = Pubkey.from_string(mint.strip())
        pump_prog = Pubkey.from_string(PUMP_PROGRAM_ID_STR)
        bonding_curve_pda, _ = Pubkey.find_program_address(
            [b"bonding-curve", bytes(mint_pk)], pump_prog
        )

        # Fetch signatures on bonding curve PDA across full lifecycle
        sig_resp = await client.post_rpc(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "getSignaturesForAddress",
                "params": [str(bonding_curve_pda), {"limit": 1000}],
            }
        )

        raw_sigs = sig_resp.get("result", [])
        if not raw_sigs:
            return []

        # Signatures from getSignaturesForAddress are returned in REVERSE chronological order (newest first).
        # We must reverse to get chronological order from launch -> pump -> dump
        chronological_sigs = list(reversed(raw_sigs))

        # If more than 1000 signatures, we only take the most recent 1000 to avoid overloading
        sampled_items = chronological_sigs[-1000:]

        sigs = [
            s["signature"]
            for s in sampled_items
            if isinstance(s, dict) and "signature" in s
        ]

        # Batch fetch parsed transactions concurrently in chunks of 100 to avoid rate limits
        results = []
        chunk_size = 100
        for i in range(0, len(sigs), chunk_size):
            chunk_sigs = sigs[i : i + chunk_size]
            tasks = [
                client.post_rpc(
                    {
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "getTransaction",
                        "params": [
                            sig,
                            {
                                "commitment": "finalized",
                                "encoding": "json",
                                "maxSupportedTransactionVersion": 0,
                            },
                        ],
                    }
                )
                for sig in chunk_sigs
            ]
            chunk_results = await asyncio.gather(*tasks, return_exceptions=True)
            results.extend(chunk_results)

        ticks: list[TradeTick] = []
        for sig, r in zip(sigs, results, strict=False):
            if not isinstance(r, dict) or not r.get("result"):
                continue
            res = r["result"]
            bt = res.get("blockTime")
            if not bt:
                continue

            logs = res.get("meta", {}).get("logMessages", [])
            for log in logs:
                if log.startswith("Program data: "):
                    try:
                        raw = base64.b64decode(log[14:])
                        if (
                            len(raw) >= 8 + 32 + 8 + 8 + 1
                            and raw[:8] == _TRADE_EVENT_DISCRIMINATOR
                        ):
                            sol_amt = struct.unpack_from("<Q", raw, 8 + 32)[0]
                            tok_amt = struct.unpack_from("<Q", raw, 8 + 32 + 8)[0]
                            is_buy = bool(raw[8 + 32 + 8 + 8])
                            if sol_amt > 0 and tok_amt > 0:
                                price = (sol_amt / 1e9) / (tok_amt / 1e6)
                                ticks.append(
                                    TradeTick(
                                        timestamp=int(bt),
                                        price=price,
                                        volume=sol_amt / 1e9,
                                        is_buy=is_buy,
                                        signature=sig,
                                    )
                                )
                    except Exception:
                        continue

        if ticks:
            # Sort chronologically so Open is pre-dump and Close is post-dump
            ticks.sort(key=lambda t: t.timestamp)
            return build_ohlc_candles(
                ticks,
                timeframe_seconds=timeframe_seconds,
                max_candles=max_candles,
                fill_empty=False,
            )

    except Exception as exc:
        logger.warning("Failed to extract on-chain ticks for OHLC: %s", exc)
    finally:
        await client.close()

    # Fallback to GeckoTerminal OHLCV if pool exists
    try:
        from rugbot.intelligence.token_resolver import _resolve_pair_address

        pair = _resolve_pair_address(mint)
        if pair:
            url = f"https://api.geckoterminal.com/api/v2/networks/solana/pools/{pair}/ohlcv/minute?aggregate=1&limit={max_candles}"
            req = urllib.request.Request(
                url,
                headers={
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
                    "Accept": "application/json",
                },
            )
            with urllib.request.urlopen(req, timeout=4) as resp:
                if resp.status == 200:
                    data = json.loads(resp.read().decode())
                    raw_candles = (
                        data.get("data", {}).get("attributes", {}).get("ohlcv_list", [])
                    )
                    if raw_candles:
                        parsed: list[OHLCCandle] = []
                        for c in raw_candles:
                            if isinstance(c, list) and len(c) >= 6:
                                parsed.append(
                                    OHLCCandle(
                                        timestamp=int(c[0]),
                                        open=float(c[1]),
                                        high=float(c[2]),
                                        low=float(c[3]),
                                        close=float(c[4]),
                                        volume=float(c[5]),
                                    )
                                )
                        if parsed:
                            parsed.sort(key=lambda x: x.timestamp)
                            return parsed[-max_candles:]
    except Exception as exc:
        logger.debug("GeckoTerminal OHLCV fallback failed: %s", exc)

    return []
