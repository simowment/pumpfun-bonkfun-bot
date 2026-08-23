"""Jito Block Engine bundle dispatcher and tip floor estimator (SDK adapter)."""

from __future__ import annotations

from dataclasses import dataclass

from sol_trade_sdk.jito import JITO_TIP_ACCOUNTS
from sol_trade_sdk.jito.client import JitoClient as SdkJitoClient


@dataclass(frozen=True, slots=True)
class JitoTipPercentiles:
    """Live tip floor percentiles in SOL from Jito API."""

    p25: float
    p50: float
    p75: float
    p95: float
    p99: float


JitoTipFloor = JitoTipPercentiles


class JitoClient:
    """Client wrapper for Jito Block Engine bundle submission and tip floor estimation."""

    def __init__(
        self,
        block_engine_url: str = "https://mainnet.block-engine.jito.wtf/api/v1/bundles",
    ) -> None:
        self._sdk_client = SdkJitoClient(endpoint=block_engine_url)

    async def fetch_tip_floor_async(self) -> JitoTipPercentiles:
        """Fetch current Jito bundle tip floor percentiles asynchronously."""
        data = await self._sdk_client.get_tip_floor()
        return JitoTipPercentiles(
            p25=data.get("p25", 0.001),
            p50=data.get("p50", 0.001),
            p75=data.get("p75", 0.002),
            p95=data.get("p95", 0.005),
            p99=data.get("p99", 0.010),
        )

    def fetch_tip_floor(self) -> JitoTipPercentiles | None:
        """Synchronous fallback for UI tip floor polling."""
        return JitoTipPercentiles(
            p25=0.001,
            p50=0.001,
            p75=0.002,
            p95=0.005,
            p99=0.010,
        )


__all__ = [
    "JITO_TIP_ACCOUNTS",
    "JitoClient",
    "JitoTipFloor",
    "JitoTipPercentiles",
]
