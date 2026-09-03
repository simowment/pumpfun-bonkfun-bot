"""Pump.fun frontend API client with wallet-signature JWT authentication.

Auth flow (documented at bankkroll-pumpfun-apis.mintlify.app):
  1. POST /auth/login  {wallet, signature, message}  → JWT token
  2. GET  /candlesticks/{mint}?offset=0&limit=N&timeframe=T

The keypair is loaded from SOLANA_PRIVATE_KEY (base58) via resolve_dotenv(include_signing=True).
The JWT is cached in-process and refreshed on 401.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

import base58
from solders.keypair import Keypair

from rugbot.utils.logger import get_logger

logger = get_logger(__name__)

PUMPFUN_API_BASE = "https://frontend-api-v3.pump.fun"
PUMPFUN_ORIGIN = "https://pump.fun"
PUMPFUN_CANDLESTICK_TIMEFRAME_MINUTES = 1  # smallest available granularity
PUMPFUN_CANDLESTICK_MAX_LIMIT = 1000  # server-side max per request
HTTP_OK = 200
HTTP_UNAUTHORIZED = 401
KEYPAIR_BYTES_FULL = 64  # seed + pubkey


def _load_keypair(private_key_b58: str) -> Keypair:
    raw_key = base58.b58decode(private_key_b58)
    if len(raw_key) == KEYPAIR_BYTES_FULL:
        return Keypair.from_bytes(raw_key)
    return Keypair.from_seed(raw_key)


def _sign_message(private_key_b58: str, message: str) -> str:
    """Sign a UTF-8 message with a base58-encoded Solana Ed25519 keypair.

    Returns the signature as a base58-encoded string, matching the format
    Pump.fun expects in the /auth/login body.
    """
    kp = _load_keypair(private_key_b58)
    sig = kp.sign_message(message.encode("utf-8"))
    return base58.b58encode(bytes(sig)).decode("ascii")


def _http_json(
    url: str,
    *,
    method: str = "GET",
    body: dict | None = None,
    headers: dict[str, str] | None = None,
    timeout: int = 8,
) -> dict:
    """Minimal synchronous JSON HTTP helper (no extra deps)."""
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
    """Authenticated Pump.fun REST API client.

    Holds a JWT in memory. Authenticates lazily on the first call that needs it.
    Re-authenticates on 401.  Thread-unsafe — intended for single-threaded async use.
    """

    def __init__(self, private_key_b58: str | None = None) -> None:
        self._private_key = private_key_b58
        self._jwt: str | None = os.environ.get("PUMPFUN_JWT") or None

    # ------------------------------------------------------------------
    # Auth
    # ------------------------------------------------------------------

    def login(self) -> str:
        """Perform wallet-signature auth and return the fresh JWT token."""
        if not self._private_key:
            raise RuntimeError(  # noqa: TRY003
                "SOLANA_PRIVATE_KEY is required to authenticate with Pump.fun API"
            )

        kp = _load_keypair(self._private_key)
        wallet = str(kp.pubkey())
        # The login message is just the wallet address (Pump.fun convention)
        message = wallet
        signature = _sign_message(self._private_key, message)

        logger.info("Authenticating with Pump.fun API as %s…", wallet)
        resp = _http_json(
            f"{PUMPFUN_API_BASE}/auth/login",
            method="POST",
            body={"wallet": wallet, "signature": signature, "message": message},
        )
        token: str = resp["token"]
        self._jwt = token
        logger.info(
            "Pump.fun JWT obtained (expires in %ss)", resp.get("expiresIn", "?")
        )
        return token

    def _auth_header(self) -> dict[str, str]:
        if not self._jwt:
            self.login()
        return {"Authorization": f"Bearer {self._jwt}"}

    # ------------------------------------------------------------------
    # Candlesticks
    # ------------------------------------------------------------------

    def fetch_candlesticks(
        self,
        mint: str,
        *,
        timeframe_minutes: int = PUMPFUN_CANDLESTICK_TIMEFRAME_MINUTES,
        limit: int = PUMPFUN_CANDLESTICK_MAX_LIMIT,
        offset: int = 0,
    ) -> list[dict]:
        """Return raw candlestick dicts from the Pump.fun API.

        Each dict has keys: timestamp, open, high, low, close, volume, trades.
        Prices are strings (API returns them that way to preserve precision).
        """
        url = (
            f"{PUMPFUN_API_BASE}/candlesticks/{mint}"
            f"?offset={offset}&limit={limit}&timeframe={timeframe_minutes}"
        )
        try:
            resp = _http_json(url, headers=self._auth_header())
            return resp.get("candlesticks", [])
        except urllib.error.HTTPError as exc:
            if exc.code == HTTP_UNAUTHORIZED:
                logger.info("Pump.fun JWT expired — re-authenticating")
                self.login()
                resp = _http_json(url, headers=self._auth_header())
                return resp.get("candlesticks", [])
            raise


# Module-level singleton — shared across the process lifetime.
# Instantiated lazily so tests that never touch the API don't pay the cost.
_client: PumpFunApiClient | None = None


def get_client() -> PumpFunApiClient:
    """Return the process-wide API client, creating it on first call."""
    global _client  # noqa: PLW0603
    if _client is None:
        from rugbot.runtime.config import resolve_dotenv

        resolve_dotenv(include_signing=True)
        _client = PumpFunApiClient(
            private_key_b58=os.environ.get("SOLANA_PRIVATE_KEY") or None,
        )
    return _client
