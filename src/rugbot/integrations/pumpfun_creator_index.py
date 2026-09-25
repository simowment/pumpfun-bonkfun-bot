"""Read-only Pump.fun creator-token index client."""

# The fixed first-party HTTPS endpoint is an indexed nomination source only.
# ruff: noqa: S310, TRY003, C901, PLR0912, BLE001, S110

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

import base58

if TYPE_CHECKING:
    from rugbot.integrations.rpc_cache import RpcResponseCache

PUMPFUN_COINS_URL: Final[str] = "https://frontend-api-v3.pump.fun/coins"
PAGE_SIZE: Final[int] = 50
MAX_CREATOR_TOKENS: Final[int] = 5_000
SOLANA_ADDRESS_BYTES: Final[int] = 32
DEFAULT_TIMEOUT_SECONDS: Final[int] = 15
CREATOR_INDEX_CACHE_KEY: Final[str] = "pumpfun/creator-index"
CREATOR_INDEX_FIRST_PAGE_TTL_SECONDS: Final[float] = 120.0


class PumpfunCreatorIndexError(RuntimeError):
    """Raised when the creator-token index response cannot be trusted."""


@dataclass(frozen=True, slots=True)
class PumpfunCreatedTokenCandidate:
    """One indexed creator-token nomination awaiting finalized confirmation."""

    mint: str
    creator: str
    name: str
    symbol: str
    created_timestamp: int


def fetch_pumpfun_created_tokens(
    creator: str,
    *,
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
    stop_after: int | None = None,
) -> tuple[PumpfunCreatedTokenCandidate, ...]:
    """Return every indexed Pump.fun token for one creator wallet.

    When ``stop_after`` is a positive int, pagination stops as soon as at least
    ``stop_after`` tokens have been collected (a whole page is appended first, so
    the returned tuple may be slightly larger). This lets a caller answer "does
    this wallet exceed N lifetime creations?" with a single page instead of
    paging through thousands of tokens — the bible §1 spam cap only needs to know
    that a mass deployer is over the threshold, not its exact 2000+ count.
    """

    _validate_address(creator)
    if timeout_seconds <= 0:
        raise ValueError("Pump.fun creator-index timeout must be positive")
    if stop_after is not None and stop_after <= 0:
        raise ValueError("stop_after must be positive when provided")
    tokens: list[PumpfunCreatedTokenCandidate] = []
    cache = _shared_cache()
    for offset in range(0, MAX_CREATOR_TOKENS, PAGE_SIZE):
        query = urllib.parse.urlencode(
            {
                "limit": PAGE_SIZE,
                "offset": offset,
                "sort": "created_timestamp",
                "order": "DESC",
                "includeNsfw": "true",
                "creator": creator,
            }
        )
        params: dict[str, object] = {"creator": creator, "offset": offset}
        payload: object | None = None
        if cache is not None:
            try:
                hit = cache.lookup(CREATOR_INDEX_CACHE_KEY, params)
            except Exception:
                hit = None
            if isinstance(hit, dict) and isinstance(hit.get("result"), list):
                payload = hit["result"]
        if payload is None:
            request = urllib.request.Request(
                f"{PUMPFUN_COINS_URL}?{query}",
                headers={"Accept": "application/json", "User-Agent": "rugbot/2.0"},
            )
            try:
                with urllib.request.urlopen(
                    request, timeout=timeout_seconds
                ) as response:
                    payload = json.loads(response.read().decode("utf-8"))
            except urllib.error.HTTPError as error:
                raise PumpfunCreatorIndexError(
                    f"Pump.fun creator index returned HTTP {error.code}"
                ) from error
            except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
                raise PumpfunCreatorIndexError(
                    "Pump.fun creator index request failed"
                ) from error
            if cache is not None and isinstance(payload, list):
                try:
                    ttl = (
                        CREATOR_INDEX_FIRST_PAGE_TTL_SECONDS
                        if offset == 0
                        else float("inf")
                    )
                    cache.store(
                        CREATOR_INDEX_CACHE_KEY,
                        params,
                        {"result": payload},
                        ttl_override=ttl,
                    )
                except Exception:
                    pass
        if not isinstance(payload, list):
            raise PumpfunCreatorIndexError(
                "Pump.fun creator index returned a non-list response"
            )
        page = tuple(_parse_token(item, creator) for item in payload)
        tokens.extend(page)
        if len(page) < PAGE_SIZE:
            return tuple(tokens)
        if stop_after is not None and len(tokens) >= stop_after:
            return tuple(tokens)
    raise PumpfunCreatorIndexError("Pump.fun creator index exceeded 5,000 tokens")


def _shared_cache() -> RpcResponseCache | None:
    """Return the process-wide shared cache, or None when unavailable."""
    try:
        from rugbot.tracker.funder_discovery import (  # noqa: PLC0415
            get_shared_rpc_cache,
        )

        return get_shared_rpc_cache()
    except Exception:
        return None


def _parse_token(item: object, creator: str) -> PumpfunCreatedTokenCandidate:
    if not isinstance(item, dict):
        raise PumpfunCreatorIndexError("Pump.fun creator token row is malformed")
    mint = item.get("mint")
    row_creator = item.get("creator")
    name = item.get("name")
    symbol = item.get("symbol")
    created_timestamp = item.get("created_timestamp")
    if (
        not isinstance(mint, str)
        or row_creator != creator
        or not isinstance(name, str)
        or not isinstance(symbol, str)
        or type(created_timestamp) is not int
        or created_timestamp < 0
    ):
        raise PumpfunCreatorIndexError("Pump.fun creator token row is incomplete")
    _validate_address(mint)
    return PumpfunCreatedTokenCandidate(
        mint=mint,
        creator=creator,
        name=name,
        symbol=symbol,
        created_timestamp=created_timestamp,
    )


def _validate_address(address: str) -> None:
    try:
        decoded = base58.b58decode(address)
    except ValueError as error:
        raise ValueError("Pump.fun address must be canonical base58") from error
    if (
        len(decoded) != SOLANA_ADDRESS_BYTES
        or base58.b58encode(decoded).decode("ascii") != address
    ):
        raise ValueError("Pump.fun address must be a Solana public key")


__all__ = [
    "PumpfunCreatedTokenCandidate",
    "PumpfunCreatorIndexError",
    "fetch_pumpfun_created_tokens",
]
