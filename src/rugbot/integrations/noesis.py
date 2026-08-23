"""Noesis / GMGN & DexScreener serial operator intelligence integration."""

# ruff: noqa: S310, BLE001

from __future__ import annotations

import json
import os
import urllib.request
from typing import Any

from rugbot.intelligence.gmgn_creator_history import (
    DEFAULT_PUBLIC_API_KEY,
    DEFAULT_TIMEOUT_SECONDS,
    GmgnCreatorHistory,
    GmgnCreatorHistoryResult,
    GmgnCreatorToken,
    creator_history_to_json,
    fetch_gmgn_creator_history,
    fetch_gmgn_dev,
)


class NoesisProvider:
    """Client for querying serial operator cluster metadata and token analytics."""

    def __init__(
        self, api_key: str | None = None, timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS
    ) -> None:
        self._api_key = (
            api_key
            or os.environ.get("NOESIS_API_KEY")
            or os.environ.get("GMGN_API_KEY", DEFAULT_PUBLIC_API_KEY)
        )
        self._timeout_seconds = timeout_seconds

    async def fetch_operator_history(
        self, creator_address: str
    ) -> GmgnCreatorHistoryResult:
        """Query creator-wide token history and ATH performance track record."""
        return await fetch_gmgn_creator_history(
            creator_address, timeout_seconds=self._timeout_seconds
        )

    async def resolve_dev_for_mint(self, mint: str) -> str | None:
        """Attributed dev entity wallet for a token mint."""
        return await fetch_gmgn_dev(mint, timeout_seconds=self._timeout_seconds)

    @staticmethod
    def fetch_dexscreener_pair(mint: str) -> dict[str, Any] | None:
        """Fetch market pair metrics from DexScreener API."""
        try:
            url = f"https://api.dexscreener.com/latest/dex/tokens/{mint}"
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=3) as resp:
                data = json.loads(resp.read().decode())
                pairs = data.get("pairs") or []
                return pairs[0] if pairs else None
        except Exception:
            return None


__all__ = [
    "DEFAULT_PUBLIC_API_KEY",
    "DEFAULT_TIMEOUT_SECONDS",
    "GmgnCreatorHistory",
    "GmgnCreatorHistoryResult",
    "GmgnCreatorToken",
    "NoesisProvider",
    "creator_history_to_json",
    "fetch_gmgn_creator_history",
    "fetch_gmgn_dev",
]
