"""Pump.fun API client for real-time OHLC candlesticks and market data.

Uses Pump.fun's live swap service:
  GET https://swap-api.pump.fun/v2/coins/{mint}/candles?createdTs=0&interval={interval}&limit={limit}
Supported intervals: 1s, 15s, 30s, 1m, 5m, 15m, 30m, 1h, 4h, 6h, 12h, 24h.
No authentication is required for candlestick and market data.
"""

# ruff: noqa: C901, PLR0912, PLR0915, BLE001, TRY300, S112, S310, PLC0415

from __future__ import annotations

import asyncio
import base64
import json
import struct
import urllib.error
import urllib.request
from typing import TYPE_CHECKING

from solders.pubkey import Pubkey

from rugbot.domain.ohlc import OHLCCandle, TradeTick, build_ohlc_candles
from rugbot.integrations.solana_rpc import SolanaClient
from rugbot.runtime.config import load_provider_settings, resolve_dotenv
from rugbot.utils.logger import get_logger

if TYPE_CHECKING:
    from rugbot.integrations.rpc_cache import RpcResponseCache

logger = get_logger(__name__)

PUMPFUN_SWAP_API_BASE = "https://swap-api.pump.fun"
PUMPFUN_FRONTEND_API_BASE = "https://frontend-api-v3.pump.fun"
PUMPFUN_ORIGIN = "https://pump.fun"
PUMP_PROGRAM_ID_STR = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
_TRADE_EVENT_DISCRIMINATOR = bytes([189, 219, 127, 211, 78, 230, 97, 238])
TOKEN_CACHE_TTL_SECONDS = 300.0
USER_COINS_CACHE_TTL_SECONDS = 120.0
RECENT_LAUNCHES_CACHE_TTL_SECONDS = 60.0
SOL_PRICE_CACHE_TTL_SECONDS = 60.0
CANDLESTICK_CACHE_TTL_SECONDS = 60.0
DEFAULT_CANDLESTICK_LIMIT = 300
HTTP_OK = 200
HTTP_NOT_FOUND = 404
MS_PER_SECOND = 1000
GECKOTERMINAL_OHLCV_FIELDS = 6
SECONDS_PER_MINUTE = 60
TIMEFRAME_1S = 1
TIMEFRAME_15S = 15
TIMEFRAME_30S = 30
TIMEFRAME_1M = 60
TIMEFRAME_5M = 300
TIMEFRAME_15M = 900
TIMEFRAME_30M = 1800
TIMEFRAME_1H = 3600
MS_TIMESTAMP_THRESHOLD = 100_000_000_000

VALID_INTERVALS = {
    "1s",
    "15s",
    "30s",
    "1m",
    "5m",
    "15m",
    "30m",
    "1h",
    "4h",
    "6h",
    "12h",
    "24h",
}


def _http_json(
    url: str,
    *,
    method: str = "GET",
    body: dict | None = None,
    headers: dict[str, str] | None = None,
    timeout: int = 8,
) -> dict | list:
    """Synchronous JSON HTTP helper."""
    data = json.dumps(body).encode() if body is not None else None
    req_headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Origin": PUMPFUN_ORIGIN,
        "User-Agent": "Mozilla/5.0",
    }
    if headers:
        req_headers.update(headers)
    req = urllib.request.Request(url, data=data, headers=req_headers, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


class PumpFunApiClient:
    """Pump.fun public API client."""

    def __init__(
        self,
        base_url: str = PUMPFUN_SWAP_API_BASE,
        *,
        page_cache: RpcResponseCache | None = None,
    ) -> None:
        self._base_url = base_url
        self._page_cache = page_cache

    def _cached_json(
        self,
        cache_key: str,
        params: object,
        url: str,
        *,
        ttl: float,
    ) -> object | None:
        """Return a cached pump.fun GET payload or fetch and store it.

        Args:
            cache_key: Cache namespace for this read type.
            params: Canonical params identifying the request.
            url: Full HTTPS URL to fetch on a cache miss.
            ttl: Bounded TTL in seconds for the stored response.

        Returns:
            The decoded JSON payload (dict or list) on success.
        """
        cache = self._page_cache
        if cache is None:
            try:
                from rugbot.tracker.funder_discovery import (
                    get_shared_rpc_cache,
                )

                cache = get_shared_rpc_cache()
            except Exception:
                cache = None
        if cache is not None:
            try:
                hit = cache.lookup(cache_key, params)
            except Exception:
                hit = None
            if isinstance(hit, dict):
                if "result" in hit:
                    return hit["result"]
                return hit
        resp = _http_json(url)
        if cache is not None and isinstance(resp, (dict, list)):
            try:
                payload = resp if isinstance(resp, dict) else {"result": resp}
                cache.store(cache_key, params, payload, ttl_override=ttl)
            except Exception:
                logger.debug("Pump.fun cache store failed for %s", cache_key)
        return resp

    def fetch_candlesticks(
        self,
        mint: str,
        *,
        interval: str = "1s",
        limit: int = DEFAULT_CANDLESTICK_LIMIT,
        created_ts: int = 0,
    ) -> list[dict]:
        """Return raw candlestick dicts from Pump.fun swap API.

        Each item has keys: timestamp (ms), open, high, low, close, volume.
        """
        if interval not in VALID_INTERVALS:
            interval = "1s"

        url = (
            f"{self._base_url}/v2/coins/{mint}/candles"
            f"?createdTs={created_ts}&interval={interval}&limit={limit}"
        )
        try:
            resp = self._cached_json(
                "pumpfun/candles",
                {
                    "mint": mint,
                    "interval": interval,
                    "limit": limit,
                    "created_ts": created_ts,
                },
                url,
                ttl=CANDLESTICK_CACHE_TTL_SECONDS,
            )
            if isinstance(resp, list):
                return resp
            if isinstance(resp, dict):
                candles = resp.get("candlesticks", [])
                return candles if isinstance(candles, list) else []
            return []
        except urllib.error.HTTPError as exc:
            logger.warning(
                "Pump.fun API candlestick fetch failed (%s): %s", exc.code, exc
            )
            return []
        except Exception as exc:
            logger.warning("Pump.fun API request failed: %s", exc)
            return []

    def fetch_trades(
        self,
        mint: str,
        limit: int = 50,
        cursor: str | None = None,
    ) -> dict:
        """Return trades page for a coin from the swap API.

        Args:
            mint: Coin mint address.
            limit: Maximum trades to return.
            cursor: Optional pagination cursor.

        Returns:
            Dict with keys ``{"trades": [...], "pagination": {...}}``.

        Trade pages after the first (cursor given) are immutable — finalized
        fills never change — so they cache forever when a page cache is
        configured. The newest page stays short-lived. Every rerun then
        resumes where throttling stopped instead of re-paging from zero.
        """
        cache_params = {"mint": mint, "limit": limit, "cursor": cursor or ""}
        if self._page_cache is not None:
            try:
                hit = self._page_cache.lookup("pumpfun/trades", cache_params)
            except Exception:
                hit = None
            if isinstance(hit, dict) and isinstance(hit.get("trades"), list):
                return {
                    "trades": hit["trades"],
                    "pagination": hit.get("pagination", {}),
                }
        url = f"{self._base_url}/v2/coins/{mint}/trades?limit={limit}"
        if cursor:
            url += f"&cursor={cursor}"
        try:
            resp = _http_json(url)
            if isinstance(resp, dict):
                page = {
                    "trades": resp.get("trades", []),
                    "pagination": resp.get("pagination", {}),
                }
                if self._page_cache is not None and isinstance(page["trades"], list):
                    try:
                        # Cursor pages are immutable fills: keep forever.
                        # The newest page stays short-lived via auto TTL.
                        self._page_cache.store(
                            "pumpfun/trades",
                            cache_params,
                            page,
                            ttl_override=float("inf") if cursor else None,
                        )
                    except Exception:
                        logger.debug("Pump.fun trade page store failed for %s", mint)
                return page
            return {"trades": [], "pagination": {}}
        except urllib.error.HTTPError as exc:
            logger.warning("Pump.fun API trades fetch failed (%s): %s", exc.code, exc)
            return {"trades": [], "pagination": {}}
        except Exception as exc:
            logger.warning("Pump.fun API request failed: %s", exc)
            return {"trades": [], "pagination": {}}

    def fetch_token(self, mint: str) -> dict:
        """Return token metadata for a coin.

        Args:
            mint: Coin mint address.

        Returns:
            Token dict with mint, name, symbol, creator,
            ath_market_cap, market_cap, complete, created_timestamp,
            and bonding_curve. Empty dict on failure.
        """
        url = f"{PUMPFUN_FRONTEND_API_BASE}/coins/{mint}"
        try:
            resp = self._cached_json(
                "pumpfun/token",
                {"mint": mint},
                url,
                ttl=TOKEN_CACHE_TTL_SECONDS,
            )
            return resp if isinstance(resp, dict) else {}
        except urllib.error.HTTPError as exc:
            if exc.code == HTTP_NOT_FOUND:
                logger.debug(
                    "Pump.fun API token fetch 404 (non-pump or unindexed mint %s)", mint
                )
            else:
                logger.warning(
                    "Pump.fun API token fetch failed (%s): %s", exc.code, exc
                )
            return {}
        except Exception as exc:
            logger.debug("Pump.fun API request failed for %s: %s", mint, exc)
            return {}

    def fetch_user_created_coins(
        self,
        creator_wallet: str,
        limit: int = 50,
        offset: int = 0,
    ) -> dict:
        """Return coins created by a wallet.

        Args:
            creator_wallet: Creator wallet address.
            limit: Maximum coins to return.
            offset: Pagination offset.

        Returns:
            Dict with keys ``{"limit", "offset", "count", "coins"}``.
        """
        url = (
            f"{PUMPFUN_FRONTEND_API_BASE}/coins-v2/user-created-coins/"
            f"{creator_wallet}?limit={limit}&offset={offset}"
        )
        try:
            resp = self._cached_json(
                "pumpfun/user-created-coins",
                {"creator": creator_wallet, "limit": limit, "offset": offset},
                url,
                ttl=USER_COINS_CACHE_TTL_SECONDS,
            )
            if isinstance(resp, dict):
                return {
                    "limit": resp.get("limit", limit),
                    "offset": resp.get("offset", offset),
                    "count": resp.get("count", 0),
                    "coins": resp.get("coins", []),
                }
            return {"limit": limit, "offset": offset, "count": 0, "coins": []}
        except urllib.error.HTTPError as exc:
            logger.warning(
                "Pump.fun API user coins fetch failed (%s): %s",
                exc.code,
                exc,
            )
            return {"limit": limit, "offset": offset, "count": 0, "coins": []}
        except Exception as exc:
            logger.warning("Pump.fun API request failed: %s", exc)
            return {"limit": limit, "offset": offset, "count": 0, "coins": []}

    def fetch_recent_launches(
        self,
        limit: int = 50,
        offset: int = 0,
        *,
        sort: str = "created_timestamp",
    ) -> list[dict]:
        """Return launches from the frontend listing feed sorted by requested field.

        Args:
            limit: Maximum coins to return.
            offset: Pagination offset.
            sort: Sort field (e.g. 'created_timestamp', 'market_cap', 'last_trade_timestamp').

        Returns:
            List of coin dicts with ``mint``, ``creator`` and
            ``created_timestamp`` keys (plus ``market_cap`` /
            ``usd_market_cap`` / ``ath_market_cap`` when present).
            Empty list on failure.
        """
        url = (
            f"{PUMPFUN_FRONTEND_API_BASE}/coins?sort={sort}"
            f"&order=DESC&limit={limit}&offset={offset}&includeNsfw=false"
        )
        try:
            resp = self._cached_json(
                "pumpfun/recent-launches",
                {"limit": limit, "offset": offset, "sort": sort},
                url,
                ttl=RECENT_LAUNCHES_CACHE_TTL_SECONDS,
            )
            if isinstance(resp, list):
                return [c for c in resp if isinstance(c, dict)]
            if isinstance(resp, dict):
                coins = resp.get("coins", [])
                return [c for c in coins if isinstance(c, dict)]
            return []
        except urllib.error.HTTPError as exc:
            logger.warning(
                "Pump.fun API recent launches fetch failed (%s): %s",
                exc.code,
                exc,
            )
            return []
        except Exception as exc:
            logger.warning("Pump.fun API request failed: %s", exc)
            return []

    def fetch_sol_price(self) -> dict:
        """Return current SOL price.

        Returns:
            Dict with keys ``{"solPrice", "asOfTimestamp", "stale"}``.
            Empty dict on failure.
        """
        url = f"{PUMPFUN_FRONTEND_API_BASE}/sol-price"
        try:
            resp = self._cached_json(
                "pumpfun/sol-price",
                {"key": "sol-price"},
                url,
                ttl=SOL_PRICE_CACHE_TTL_SECONDS,
            )
            return resp if isinstance(resp, dict) else {}
        except urllib.error.HTTPError as exc:
            logger.warning(
                "Pump.fun API SOL price fetch failed (%s): %s",
                exc.code,
                exc,
            )
            return {}
        except Exception as exc:
            logger.warning("Pump.fun API request failed: %s", exc)
            return {}


# Module-level singleton
_client: PumpFunApiClient | None = None


def get_client() -> PumpFunApiClient:
    """Return the process-wide API client, creating it on first call."""
    global _client  # noqa: PLW0603
    if _client is None:
        _client = PumpFunApiClient()
    return _client


async def fetch_token_ohlc_candles(
    mint: str,
    *,
    timeframe_seconds: int = 1,
    max_candles: int = 300,
) -> list[OHLCCandle]:
    """Fetch OHLCV candles for a Pump.fun token.

    Primary source: Pump.fun swap API (/v2/coins/{mint}/candles) supporting
    native intervals (1s, 15s, 30s, 1m, 5m, 15m, 30m, 1h).
    Falls back to on-chain RPC decode when API is unavailable.
    """
    resolve_dotenv(include_signing=True)

    # Map timeframe_seconds to Pump.fun API interval string
    if timeframe_seconds <= TIMEFRAME_1S:
        interval = "1s"
    elif timeframe_seconds <= TIMEFRAME_15S:
        interval = "15s"
    elif timeframe_seconds <= TIMEFRAME_30S:
        interval = "30s"
    elif timeframe_seconds <= TIMEFRAME_1M:
        interval = "1m"
    elif timeframe_seconds <= TIMEFRAME_5M:
        interval = "5m"
    elif timeframe_seconds <= TIMEFRAME_15M:
        interval = "15m"
    elif timeframe_seconds <= TIMEFRAME_30M:
        interval = "30m"
    elif timeframe_seconds <= TIMEFRAME_1H:
        interval = "1h"
    else:
        interval = "1m"

    try:
        client = get_client()
        raw = client.fetch_candlesticks(
            mint,
            interval=interval,
            limit=max_candles,
        )
        if raw:
            candles = []
            for c in raw:
                if all(
                    k in c
                    for k in ("timestamp", "open", "high", "low", "close", "volume")
                ):
                    raw_ts = int(c["timestamp"])
                    ts = raw_ts // 1000 if raw_ts > MS_TIMESTAMP_THRESHOLD else raw_ts
                    candles.append(
                        OHLCCandle(
                            timestamp=ts,
                            open=float(c["open"]),
                            high=float(c["high"]),
                            low=float(c["low"]),
                            close=float(c["close"]),
                            volume=float(c["volume"]),
                        )
                    )
            if candles:
                candles.sort(key=lambda c: c.timestamp)
                logger.debug(
                    "Pump.fun API: fetched %d %s candles for %s",
                    len(candles),
                    interval,
                    mint[:8],
                )
                return candles[-max_candles:]
    except Exception as exc:
        logger.warning(
            "Pump.fun API candlestick fetch failed, falling back to RPC: %s", exc
        )

    # --- Fallback: on-chain RPC decode (supports any timeframe, incl. 1s) ---
    settings = load_provider_settings()
    rpc_client = SolanaClient(settings.rpc_http)

    try:
        mint_pk = Pubkey.from_string(mint.strip())
        pump_prog = Pubkey.from_string(PUMP_PROGRAM_ID_STR)
        bonding_curve_pda, _ = Pubkey.find_program_address(
            [b"bonding-curve", bytes(mint_pk)], pump_prog
        )

        sig_resp = await rpc_client.post_rpc(
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

        # Reverse to chronological order (API returns newest-first)
        chronological_sigs = list(reversed(raw_sigs))
        sampled_items = chronological_sigs[-1000:]
        sigs = [
            s["signature"]
            for s in sampled_items
            if isinstance(s, dict) and "signature" in s
        ]

        # Batch fetch concurrently in chunks of 100
        results: list = []
        chunk_size = 100
        for i in range(0, len(sigs), chunk_size):
            chunk_sigs = sigs[i : i + chunk_size]
            tasks = [
                rpc_client.post_rpc(
                    {
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "getTransaction",
                        "params": [
                            sig,
                            {
                                "commitment": "finalized",
                                "encoding": "json",
                                "maxSupportedTransactionVersion": 1,
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
            for log in res.get("meta", {}).get("logMessages", []):
                if log.startswith("Program data: "):
                    try:
                        raw_data = base64.b64decode(log[14:])
                        if (
                            len(raw_data) >= 8 + 32 + 8 + 8 + 1
                            and raw_data[:8] == _TRADE_EVENT_DISCRIMINATOR
                        ):
                            sol_amt = struct.unpack_from("<Q", raw_data, 8 + 32)[0]
                            tok_amt = struct.unpack_from("<Q", raw_data, 8 + 32 + 8)[0]
                            is_buy = bool(raw_data[8 + 32 + 8 + 8])
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
            ticks.sort(key=lambda t: t.timestamp)
            return build_ohlc_candles(
                ticks,
                timeframe_seconds=timeframe_seconds,
                max_candles=max_candles,
                fill_empty=False,
            )

    except Exception as exc:
        logger.warning("RPC OHLC decode failed: %s", exc)
    finally:
        await rpc_client.close()

    # --- Last resort: GeckoTerminal (graduated tokens only) ---
    try:
        from rugbot.intelligence.token_resolver import _resolve_pair_address

        pair = _resolve_pair_address(mint)
        if pair:
            url = (
                f"https://api.geckoterminal.com/api/v2/networks/solana/pools/{pair}"
                f"/ohlcv/minute?aggregate=1&limit={max_candles}"
            )
            req = urllib.request.Request(
                url,
                headers={
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
                    "Accept": "application/json",
                },
            )
            with urllib.request.urlopen(req, timeout=4) as resp:
                if resp.status == HTTP_OK:
                    data = json.loads(resp.read().decode())
                    raw_candles = (
                        data.get("data", {}).get("attributes", {}).get("ohlcv_list", [])
                    )
                    if raw_candles:
                        parsed: list[OHLCCandle] = [
                            OHLCCandle(
                                timestamp=int(c[0]),
                                open=float(c[1]),
                                high=float(c[2]),
                                low=float(c[3]),
                                close=float(c[4]),
                                volume=float(c[5]),
                            )
                            for c in raw_candles
                            if isinstance(c, list)
                            and len(c) >= GECKOTERMINAL_OHLCV_FIELDS
                        ]
                        if parsed:
                            parsed.sort(key=lambda x: x.timestamp)
                            return parsed[-max_candles:]
    except Exception as exc:
        logger.debug("GeckoTerminal OHLCV fallback failed: %s", exc)

    return []
