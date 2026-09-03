"""Pump.fun API client for real-time OHLC candlesticks and market data.

Uses Pump.fun's live swap service:
  GET https://swap-api.pump.fun/v2/coins/{mint}/candles?createdTs=0&interval={interval}&limit={limit}
Supported intervals: 1s, 15s, 30s, 1m, 5m, 15m, 30m, 1h, 4h, 6h, 12h, 24h.
No authentication is required for candlestick and market data.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request

from rugbot.utils.logger import get_logger

logger = get_logger(__name__)

PUMPFUN_SWAP_API_BASE = "https://swap-api.pump.fun"
PUMPFUN_ORIGIN = "https://pump.fun"
DEFAULT_CANDLESTICK_LIMIT = 300
HTTP_OK = 200
MS_PER_SECOND = 1000

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
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
        return json.loads(resp.read().decode())


class PumpFunApiClient:
    """Pump.fun public API client."""

    def __init__(self, base_url: str = PUMPFUN_SWAP_API_BASE) -> None:
        self._base_url = base_url

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
            resp = _http_json(url)
            if isinstance(resp, list):
                return resp
            if isinstance(resp, dict):
                return resp.get("candlesticks", [])
            return []
        except urllib.error.HTTPError as exc:
            logger.warning(
                "Pump.fun API candlestick fetch failed (%s): %s", exc.code, exc
            )
            return []
        except Exception as exc:
            logger.warning("Pump.fun API request failed: %s", exc)
            return []


# Module-level singleton
_client: PumpFunApiClient | None = None


def get_client() -> PumpFunApiClient:
    """Return the process-wide API client, creating it on first call."""
    global _client  # noqa: PLW0603
    if _client is None:
        _client = PumpFunApiClient()
    return _client
