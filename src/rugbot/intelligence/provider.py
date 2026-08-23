"""Unified intelligence provider with Noesis, GMGN, Birdeye, and DexScreener fallbacks."""

# ruff: noqa: ANN401

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from rugbot.integrations.helius import HeliusClient
from rugbot.integrations.noesis import NoesisProvider
from rugbot.intelligence.token_resolver import (
    DiscoveredTokenLaunch,
    fetch_token_metadata,
)


@dataclass(frozen=True, slots=True)
class MarketTokenIntel:
    """Enriched token intelligence data."""

    mint: str
    name: str
    symbol: str
    market_cap_usd: float
    ath_multiplier: float
    creator_wallet: str | None = None
    pair_address: str | None = None


class IntelligenceProvider:
    """Multi-source intelligence provider with automatic fallbacks."""

    def __init__(self) -> None:
        self._noesis = NoesisProvider()
        self._helius = HeliusClient()

    async def fetch_token_intel(self, mint: str) -> MarketTokenIntel:
        """Fetch verified token metadata using DexScreener with Pump.fun frontend fallback."""
        name, symbol, mc, ath = fetch_token_metadata(mint)
        dev_wallet = await self._noesis.resolve_dev_for_mint(mint)
        return MarketTokenIntel(
            mint=mint,
            name=name,
            symbol=symbol,
            market_cap_usd=mc,
            ath_multiplier=ath,
            creator_wallet=dev_wallet,
        )

    def scan_wallet_cluster(
        self, wallet: str
    ) -> tuple[str | None, list[DiscoveredTokenLaunch]]:
        """Trace incoming SOL transfers and token creation history via Helius."""
        return self._helius.scan_cluster_history(wallet)

    async def fetch_operator_history(self, creator_address: str) -> Any:
        """Query operator track record from Noesis/GMGN."""
        return await self._noesis.fetch_operator_history(creator_address)


__all__ = [
    "IntelligenceProvider",
    "MarketTokenIntel",
]
