"""Strict Nansen indexed-candidate client for wallet intelligence."""

# Provider boundary validates the fixed HTTPS URL and translates response failures.
# ruff: noqa: S310, TRY003

from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Final

import base58

NANSEN_API_URL: Final[str] = "https://api.nansen.ai"
SOLANA_ADDRESS_BYTES: Final[int] = 32
DEFAULT_TIMEOUT_SECONDS: Final[int] = 30
HTTP_TOO_MANY_REQUESTS: Final[int] = 429
SOLANA_CHAIN: Final[str] = "solana"
MAX_PER_PAGE: Final[int] = 100
_DATE_RE: Final[str] = r"^\d{4}-\d{2}-\d{2}$"


class NansenProviderError(RuntimeError):
    """Raised when Nansen cannot provide a validated candidate response."""


@dataclass(frozen=True, slots=True)
class NansenCounterparty:
    """One indexed wallet counterparty with directional flow totals."""

    wallet: str
    labels: tuple[str, ...]
    interaction_count: int
    total_volume_usd: float
    volume_in_usd: float
    volume_out_usd: float


@dataclass(frozen=True, slots=True)
class NansenCounterpartyPage:
    """Validated counterparty page with explicit pagination completeness."""

    counterparties: tuple[NansenCounterparty, ...]
    page: int
    is_last_page: bool


NansenTransport = Callable[[urllib.request.Request, int], bytes]


def counterparties_to_json(
    rows: tuple[NansenCounterparty, ...] | list[NansenCounterparty],
) -> list[dict[str, object]]:
    """Serialize counterparty rows for the existing JSON pipeline."""
    return [
        {
            "wallet": row.wallet,
            "labels": list(row.labels),
            "interaction_count": row.interaction_count,
            "total_volume_usd": row.total_volume_usd,
            "volume_in_usd": row.volume_in_usd,
            "volume_out_usd": row.volume_out_usd,
            "source": "nansen",
        }
        for row in rows
    ]


class NansenClient:
    """Read indexed Nansen candidates without treating them as final evidence."""

    def __init__(
        self,
        api_key: str,
        *,
        timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
        transport: NansenTransport | None = None,
    ) -> None:
        """Initialize one authenticated Nansen client."""

        if not api_key.strip():
            raise ValueError("Nansen API key is required")
        if timeout_seconds <= 0:
            raise ValueError("Nansen timeout must be positive")
        self._api_key = api_key.strip()
        self._timeout_seconds = timeout_seconds
        self._transport = transport or _urlopen_transport

    def counterparties(  # noqa: PLR0913 - address+chain+range+paging vary per call
        self,
        address: str,
        *,
        chain: str = SOLANA_CHAIN,
        date_from: str,
        date_to: str,
        page: int = 1,
        per_page: int = 10,
    ) -> NansenCounterpartyPage:
        """Read one page of indexed wallet counterparties.

        Args:
            address: Wallet address to list counterparties for.
            chain: Chain scope (validated non-empty; Solana is tested).
            date_from: Range start as ``YYYY-MM-DD`` (API requires a date).
            date_to: Range end as ``YYYY-MM-DD``.
            page: 1-based page number.
            per_page: Rows per page (1-100).

        Returns:
            Validated counterparty page.

        Raises:
            ValueError: On malformed request arguments.
            NansenProviderError: On transport failure or malformed rows.
                The message contains the HTTP status (e.g. ``429``) so
                callers can fail fast on rate limiting.
        """

        _validate_address(address)
        if not chain.strip():
            raise ValueError("Nansen chain must be non-empty")
        for label, value in (("date_from", date_from), ("date_to", date_to)):
            if not isinstance(value, str) or re.match(_DATE_RE, value) is None:
                raise ValueError(f"Nansen {label} must be YYYY-MM-DD")
        if page < 1:
            raise ValueError("Nansen page must be positive")
        if not 1 <= per_page <= MAX_PER_PAGE:
            raise ValueError("Nansen per_page must be 1-100")
        payload = self._request(
            "/api/v1/profiler/address/counterparties",
            {
                "address": address,
                "chain": chain,
                "date": {"from": date_from, "to": date_to},
                "group_by": "wallet",
                "pagination": {"page": page, "per_page": per_page},
            },
        )
        data = payload.get("data")
        if not isinstance(data, list):
            raise NansenProviderError("Nansen counterparties rows are malformed")
        pagination = payload.get("pagination", {})
        is_last_page = (
            pagination.get("is_last_page", True)
            if isinstance(pagination, dict)
            else True
        )
        return NansenCounterpartyPage(
            counterparties=tuple(_parse_counterparty(item) for item in data),
            page=page,
            is_last_page=is_last_page is True,
        )

    def _request(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        url = f"{NANSEN_API_URL}{path}"
        request = urllib.request.Request(
            url,
            data=json.dumps(body).encode(),
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
                "User-Agent": "rugbot/2.0",
                "apikey": self._api_key,
            },
            method="POST",
        )
        try:
            raw = self._transport(request, self._timeout_seconds)
        except urllib.error.HTTPError as error:
            raise NansenProviderError(
                f"Nansen request failed with HTTP {error.code}"
            ) from error
        except OSError as error:
            raise NansenProviderError("Nansen request failed") from error
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise NansenProviderError("Nansen returned invalid JSON") from error
        if not isinstance(payload, dict):
            raise NansenProviderError("Nansen returned a non-object response")
        return payload


def _parse_counterparty(item: object) -> NansenCounterparty:
    """Validate one raw counterparty row into a typed record."""
    if not isinstance(item, dict):
        raise NansenProviderError("Nansen counterparty row is malformed")
    wallet = item.get("counterparty_address")
    labels = item.get("counterparty_address_label")
    count = item.get("interaction_count")
    total = item.get("total_volume_usd")
    volume_in = item.get("volume_in_usd")
    volume_out = item.get("volume_out_usd")
    if not isinstance(wallet, str):
        raise NansenProviderError("Nansen counterparty row lacks a wallet")
    try:
        _validate_address(wallet)
    except ValueError as error:
        raise NansenProviderError("Nansen counterparty wallet is invalid") from error
    if labels is None:
        label_tuple: tuple[str, ...] = ()
    elif isinstance(labels, list) and all(isinstance(entry, str) for entry in labels):
        label_tuple = tuple(labels)
    else:
        raise NansenProviderError("Nansen counterparty labels are malformed")
    if type(count) is not int or count < 0:
        raise NansenProviderError("Nansen interaction count is invalid")
    amounts: list[float] = []
    for value in (total, volume_in, volume_out):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise NansenProviderError("Nansen counterparty volume is invalid")
        amounts.append(float(value))
    return NansenCounterparty(
        wallet=wallet,
        labels=label_tuple,
        interaction_count=count,
        total_volume_usd=amounts[0],
        volume_in_usd=amounts[1],
        volume_out_usd=amounts[2],
    )


def _urlopen_transport(request: urllib.request.Request, timeout: int) -> bytes:
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read()


def _validate_address(address: str) -> None:
    try:
        decoded = base58.b58decode(address)
    except ValueError as error:
        raise ValueError("Nansen address must be canonical base58") from error
    if (
        len(decoded) != SOLANA_ADDRESS_BYTES
        or base58.b58encode(decoded).decode("ascii") != address
    ):
        raise ValueError("Nansen address must be a Solana public key")


__all__ = [
    "NansenClient",
    "NansenCounterparty",
    "NansenCounterpartyPage",
    "NansenProviderError",
    "counterparties_to_json",
]
