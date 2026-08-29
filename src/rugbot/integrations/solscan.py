"""Strict Solscan v2 indexed-candidate client for wallet intelligence."""

# Provider boundary validates the fixed HTTPS URL and translates response failures.
# ruff: noqa: S310, TRY003

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from math import isfinite
from time import monotonic
from typing import Any, Final

import base58

SOLSCAN_API_URL: Final[str] = "https://pro-api.solscan.io/v2.0"
SOLSCAN_PLAYGROUND_URL: Final[str] = "https://pro-api.solscan.io/playground"
SOLANA_ADDRESS_BYTES: Final[int] = 32
SOLANA_SIGNATURE_BYTES: Final[int] = 64
MAX_FUNDED_BY_ADDRESSES: Final[int] = 50
MAX_ENTITY_MINT_CANDIDATES: Final[int] = 500
DEFAULT_TIMEOUT_SECONDS: Final[int] = 15
PLAYGROUND_TRANSACTION_LIMIT: Final[int] = 10
DEFAULT_RATE_LIMIT_COOLDOWN_SECONDS: Final[float] = 30.0
HTTP_TOO_MANY_REQUESTS: Final[int] = 429


@dataclass(slots=True)
class _SolscanCircuitBreaker:
    cooldown_until: float = 0.0


_CIRCUIT_BREAKER = _SolscanCircuitBreaker()


class SolscanProviderError(RuntimeError):
    """Raised when Solscan cannot provide a validated candidate response."""


@dataclass(frozen=True, slots=True)
class SolscanFundingCandidate:
    """Indexed first-funder candidate that still requires RPC confirmation."""

    address: str
    funded_by: str
    transaction_signature: str
    block_time: int


@dataclass(frozen=True, slots=True)
class SolscanEnhancedTransactionPage:
    """Validated page of live raw transactions from Solscan playground."""

    transactions: tuple[dict[str, Any], ...]
    cursor: str | None


@dataclass(frozen=True, slots=True)
class SolscanTokenCreationCandidate:
    """Indexed token creation candidate requiring finalized RPC confirmation."""

    mint: str
    creator: str
    transaction_signature: str
    created_time: int
    name: str
    symbol: str


@dataclass(frozen=True, slots=True)
class SolscanMintTransactionCandidate:
    """Indexed transaction touching known mints; requires finalized RPC hydration."""

    signature: str
    slot: int
    transaction_index: int | None
    block_time: int
    matched_mints: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class SolscanMintTransactionDiscovery:
    """Bounded indexed mint matches with explicit pagination completeness."""

    candidates: tuple[SolscanMintTransactionCandidate, ...]
    pages_scanned: int
    complete: bool
    warning: str | None
    next_cursor: str | None


SolscanTransport = Callable[[urllib.request.Request, int], bytes]
SolscanClock = Callable[[], float]


class SolscanClient:
    """Read indexed Solana candidates without treating them as final evidence."""

    def __init__(
        self,
        api_key: str,
        *,
        timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
        transport: SolscanTransport | None = None,
        clock: SolscanClock = monotonic,
    ) -> None:
        """Initialize one authenticated Solscan v2 client."""

        if not api_key.strip():
            raise ValueError("Solscan API key is required")
        if timeout_seconds <= 0:
            raise ValueError("Solscan timeout must be positive")
        if not callable(clock):
            raise TypeError("Solscan clock must be callable")
        self._api_key = api_key.strip()
        self._timeout_seconds = timeout_seconds
        self._transport = transport or _urlopen_transport
        self._clock = clock

    def funded_by(
        self, addresses: tuple[str, ...]
    ) -> tuple[SolscanFundingCandidate, ...]:
        """Nominate indexed funders for up to 50 validated Solana wallets."""

        if not addresses or len(addresses) > MAX_FUNDED_BY_ADDRESSES:
            raise ValueError("Solscan funded-by requires between 1 and 50 addresses")
        for address in addresses:
            _validate_address(address)
        query = urllib.parse.urlencode(
            [("address[]", address) for address in addresses]
        )
        payload = self._request(f"/account/funded-by?{query}")
        data = payload.get("data")
        if payload.get("success") is not True or not isinstance(data, list):
            raise SolscanProviderError("Solscan funded-by response is incomplete")

        candidates: list[SolscanFundingCandidate] = []
        for item in data:
            if not isinstance(item, dict):
                raise SolscanProviderError("Solscan funded-by row is malformed")
            address = item.get("address")
            funded_by = item.get("funded_by")
            signature = item.get("tx_hash")
            block_time = item.get("block_time")
            if (
                not isinstance(address, str)
                or address not in addresses
                or not isinstance(funded_by, str)
                or not isinstance(signature, str)
                or type(block_time) is not int
            ):
                raise SolscanProviderError("Solscan funded-by row is incomplete")
            _validate_address(funded_by)
            candidates.append(
                SolscanFundingCandidate(
                    address=address,
                    funded_by=funded_by,
                    transaction_signature=signature,
                    block_time=block_time,
                )
            )
        return tuple(candidates)

    def token_creation(self, mint: str) -> SolscanTokenCreationCandidate:
        """Nominate the indexed creation transaction for one token mint."""

        _validate_address(mint)
        query = urllib.parse.urlencode({"address": mint})
        payload = self._request(f"/token/meta?{query}")
        data = payload.get("data")
        if payload.get("success") is not True or not isinstance(data, dict):
            raise SolscanProviderError("Solscan token-meta response is incomplete")
        address = data.get("address")
        creator = data.get("creator")
        signature = data.get("create_tx")
        created_time = data.get("created_time")
        name = data.get("name")
        symbol = data.get("symbol")
        if (
            address != mint
            or not isinstance(creator, str)
            or not isinstance(signature, str)
            or type(created_time) is not int
            or created_time < 0
            or not isinstance(name, str)
            or not isinstance(symbol, str)
        ):
            raise SolscanProviderError("Solscan token-meta row is incomplete")
        _validate_address(creator)
        _validate_signature(signature)
        return SolscanTokenCreationCandidate(
            mint=mint,
            creator=creator,
            transaction_signature=signature,
            created_time=created_time,
            name=name,
            symbol=symbol,
        )

    def enhanced_transactions(
        self,
        address: str,
        *,
        program: str,
        cursor: str | None = None,
        limit: int = PLAYGROUND_TRANSACTION_LIMIT,
    ) -> SolscanEnhancedTransactionPage:
        """Read one free-tier live page filtered to a program interaction."""

        _validate_address(address)
        _validate_address(program)
        if limit != PLAYGROUND_TRANSACTION_LIMIT:
            raise ValueError("Solscan playground transaction limit must be 10")
        query_values = [
            ("address", address),
            ("limit", str(limit)),
            ("status", "true"),
            ("encoding", "json"),
            ("program[]", program),
        ]
        if cursor is not None:
            query_values.append(("cursor", cursor))
        query = urllib.parse.urlencode(query_values)
        payload = self._request(
            f"{SOLSCAN_PLAYGROUND_URL}/account/transactions/enhanced?{query}",
            absolute_url=True,
        )
        data = payload.get("data")
        if payload.get("success") is not True or not isinstance(data, dict):
            raise SolscanProviderError(
                "Solscan enhanced-transactions response is incomplete"
            )
        transactions = data.get("transactions")
        cursor_value = data.get("cursor")
        if not isinstance(transactions, list) or not all(
            isinstance(transaction, dict) for transaction in transactions
        ):
            raise SolscanProviderError(
                "Solscan enhanced-transactions rows are malformed"
            )
        if cursor_value is not None and not isinstance(cursor_value, str):
            raise SolscanProviderError(
                "Solscan enhanced-transactions cursor is malformed"
            )
        return SolscanEnhancedTransactionPage(
            transactions=tuple(transactions),
            cursor=cursor_value,
        )

    def mint_transaction_candidates(  # noqa: PLR0913
        self,
        address: str,
        *,
        program: str,
        mints: frozenset[str],
        max_pages: int = 10,
        page_pause_seconds: float = 1.0,
        cursor: str | None = None,
    ) -> SolscanMintTransactionDiscovery:
        """Nominate indexed transactions intersecting a known entity mint set."""

        if not mints or len(mints) > MAX_ENTITY_MINT_CANDIDATES:
            raise ValueError("Solscan mint candidate set must contain 1 to 500 mints")
        for mint in mints:
            _validate_address(mint)
        _validate_address(address)
        _validate_address(program)
        candidates: dict[str, SolscanMintTransactionCandidate] = {}
        pages_scanned = 0
        complete = False
        warning: str | None = None
        for _ in range(max_pages):
            try:
                page = self.enhanced_transactions(
                    address,
                    program=program,
                    cursor=cursor,
                )
            except SolscanProviderError as error:
                warning = str(error)
                break
            pages_scanned += 1
            for row in page.transactions:
                candidate = _mint_transaction_candidate(row, mints)
                if candidate is not None:
                    candidates[candidate.signature] = candidate
            cursor = page.cursor
            if cursor is None:
                complete = True
                break
            if page_pause_seconds > 0:
                time.sleep(page_pause_seconds)
        else:
            warning = f"Solscan mint history reached the {max_pages}-page bound"
        return SolscanMintTransactionDiscovery(
            candidates=tuple(candidates.values()),
            pages_scanned=pages_scanned,
            complete=complete,
            warning=warning,
            next_cursor=cursor,
        )

    def _request(
        self,
        path_or_url: str,
        *,
        absolute_url: bool = False,
    ) -> dict[str, Any]:
        url = path_or_url if absolute_url else f"{SOLSCAN_API_URL}{path_or_url}"
        request = urllib.request.Request(
            url,
            headers={
                "Accept": "application/json",
                "User-Agent": "rugbot/2.0",
                "token": self._api_key,
            },
        )
        cooldown_remaining = _CIRCUIT_BREAKER.cooldown_until - self._clock()
        if cooldown_remaining > 0:
            raise SolscanProviderError(
                "Solscan rate-limit cooldown is active for "
                f"{cooldown_remaining:.1f} seconds"
            )
        try:
            raw = self._transport(request, self._timeout_seconds)
        except urllib.error.HTTPError as error:
            if error.code == HTTP_TOO_MANY_REQUESTS:
                retry_after = _retry_after_seconds(error)
                _CIRCUIT_BREAKER.cooldown_until = self._clock() + retry_after
            raise SolscanProviderError(
                f"Solscan request failed with HTTP {error.code}"
            ) from error
        except OSError as error:
            raise SolscanProviderError("Solscan request failed") from error
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise SolscanProviderError("Solscan returned invalid JSON") from error
        if not isinstance(payload, dict):
            raise SolscanProviderError("Solscan returned a non-object response")
        return payload


def _mint_transaction_candidate(
    row: dict[str, Any],
    mints: frozenset[str],
) -> SolscanMintTransactionCandidate | None:
    """Narrow one enhanced row to an indexed mint candidate."""

    transaction = row.get("transaction")
    message = transaction.get("message") if isinstance(transaction, dict) else None
    signatures = (
        transaction.get("signatures") if isinstance(transaction, dict) else None
    )
    account_keys = message.get("accountKeys") if isinstance(message, dict) else None
    slot = row.get("slot")
    transaction_index = row.get("transactionIndex")
    block_time = row.get("blockTime")
    if block_time is None:
        block_time = row.get("block_time")
    if (
        not isinstance(signatures, list)
        or not signatures
        or not isinstance(signatures[0], str)
        or not isinstance(account_keys, list)
        or not all(isinstance(key, str) for key in account_keys)
        or type(slot) is not int
        or slot < 0
        or type(block_time) is not int
        or block_time < 0
        or (
            transaction_index is not None
            and (type(transaction_index) is not int or transaction_index < 0)
        )
    ):
        raise SolscanProviderError("Solscan enhanced transaction row is incomplete")
    _validate_signature(signatures[0])
    matched_mints = tuple(sorted(mints.intersection(account_keys)))
    if not matched_mints:
        return None
    return SolscanMintTransactionCandidate(
        signature=signatures[0],
        slot=slot,
        transaction_index=transaction_index,
        block_time=block_time,
        matched_mints=matched_mints,
    )


def _urlopen_transport(request: urllib.request.Request, timeout: int) -> bytes:
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read()


def _validate_address(address: str) -> None:
    try:
        decoded = base58.b58decode(address)
    except ValueError as error:
        raise ValueError("Solscan address must be canonical base58") from error
    if (
        len(decoded) != SOLANA_ADDRESS_BYTES
        or base58.b58encode(decoded).decode("ascii") != address
    ):
        raise ValueError("Solscan address must be a Solana public key")


def _validate_signature(signature: str) -> None:
    try:
        decoded = base58.b58decode(signature)
    except ValueError as error:
        raise SolscanProviderError(
            "Solscan signature must be canonical base58"
        ) from error
    if (
        len(decoded) != SOLANA_SIGNATURE_BYTES
        or base58.b58encode(decoded).decode("ascii") != signature
    ):
        raise SolscanProviderError("Solscan signature must be canonical base58")


def _retry_after_seconds(error: urllib.error.HTTPError) -> float:
    header = error.headers.get("Retry-After") if error.headers is not None else None
    try:
        seconds = float(header) if header is not None else None
    except ValueError:
        seconds = None
    if seconds is None or not isfinite(seconds) or seconds < 0:
        return DEFAULT_RATE_LIMIT_COOLDOWN_SECONDS
    return seconds


def _reset_circuit_breaker_for_tests() -> None:
    _CIRCUIT_BREAKER.cooldown_until = 0.0


__all__ = [
    "SolscanClient",
    "SolscanEnhancedTransactionPage",
    "SolscanFundingCandidate",
    "SolscanMintTransactionCandidate",
    "SolscanMintTransactionDiscovery",
    "SolscanProviderError",
    "SolscanTokenCreationCandidate",
]
