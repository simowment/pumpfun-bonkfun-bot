"""Normalized Pump.fun protocol models."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class TokenLaunch:
    """Normalized Pump.fun token creation event."""

    signature: str
    slot: int
    timestamp: int
    creator: str
    mint: str
    symbol: str
    name: str
