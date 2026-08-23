"""Canonical string and number formatting utilities."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Final

LAMPORTS_PER_SOL = 1_000_000_000

SHORT_IDENTIFIER_LIMIT: Final[int] = 14
PPM_SCALE: Final[int] = 1_000_000
SECONDS_PER_MINUTE: Final[int] = 60
SECONDS_PER_HOUR: Final[int] = 3600
SECONDS_PER_DAY: Final[int] = 86400


def format_age(timestamp: int | None, current_timestamp: int | None = None) -> str:
    """Format the relative elapsed age dynamically (e.g. '0s', '3s', '45s', '2m', '1h 12m')."""
    if timestamp is None or timestamp <= 0:
        return "—"
    now_ts = (
        current_timestamp
        if current_timestamp is not None
        else int(datetime.now(UTC).timestamp())
    )
    elapsed = max(0, now_ts - timestamp)

    if elapsed < SECONDS_PER_MINUTE:
        return f"{elapsed}s"
    if elapsed < SECONDS_PER_HOUR:
        mins = elapsed // SECONDS_PER_MINUTE
        return f"{mins}m"
    if elapsed < SECONDS_PER_DAY:
        hours = elapsed // SECONDS_PER_HOUR
        mins = (elapsed % SECONDS_PER_HOUR) // SECONDS_PER_MINUTE
        return f"{hours}h {mins}m" if mins else f"{hours}h"
    days = elapsed // SECONDS_PER_DAY
    return f"{days}d"


def format_amount(lamports: int | None) -> str:
    """Format exact integer lamports as an explicit SOL string (e.g. '3.20 SOL' or '—')."""
    if lamports is None or lamports <= 0:
        return "—"
    return f"{format_sol(lamports)} SOL"


def format_sol(lamports: int) -> str:
    """Format exact integer lamports as a readable decimal SOL string."""
    if type(lamports) is not int or lamports <= 0:
        return "0"
    whole = lamports // LAMPORTS_PER_SOL
    fraction = f"{lamports % LAMPORTS_PER_SOL:09d}".rstrip("0")
    return f"{whole}.{fraction}" if fraction else str(whole)


def short_address(address: str | None) -> str:
    """Shorten an address or signature for compact table columns."""
    if address is None:
        return "--"
    if len(address) <= SHORT_IDENTIFIER_LIMIT:
        return address
    return f"{address[:6]}...{address[-6:]}"


def format_timestamp(ts: int | None) -> str:
    """Format a unix timestamp as HH:MM:SS."""
    if not ts or ts <= 0:
        return "--:--:--"
    return datetime.fromtimestamp(ts, tz=UTC).strftime("%H:%M:%S")


def format_ppm_percent(ppm: int | None) -> str:
    """Format parts-per-million as a human-readable signed percentage."""
    if ppm is None:
        return "—"
    pct = Decimal(ppm) / Decimal(10_000)
    sign = "+" if ppm > 0 else ""
    return f"{sign}{pct:.1f}%"


def format_network_endpoint(endpoint: str) -> str:
    """Extract host name from RPC endpoint URL."""
    without_scheme = endpoint.split("://", 1)[-1]
    host_and_port = without_scheme.split("/", 1)[0]
    return host_and_port.split("?", 1)[0]


__all__ = [
    "PPM_SCALE",
    "SHORT_IDENTIFIER_LIMIT",
    "format_age",
    "format_amount",
    "format_network_endpoint",
    "format_ppm_percent",
    "format_sol",
    "format_timestamp",
    "short_address",
]
