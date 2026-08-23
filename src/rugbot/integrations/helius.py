"""Helius Enhanced API and cluster history intelligence integration."""

# ruff: noqa: S310, BLE001

from __future__ import annotations

import json
import os
import time
import urllib.request
from typing import Any

from sol_trade_sdk.helius import HeliusClient as SdkHeliusClient

from rugbot.integrations.solana_logs_stream import (
    SolanaLogsStream,
    WalletLogNotification,
)
from rugbot.intelligence.token_resolver import (
    DiscoveredTokenLaunch,
    scan_helius_cluster_history,
)


class HeliusClient:
    """Client for Helius Enhanced Webhooks, cluster history, and Solana RPC endpoint rotation."""

    def __init__(self, api_key: str | None = None, rpc_url: str | None = None) -> None:
        self._api_key = api_key or os.environ.get("HELIUS_API_KEY")
        self._rpc_url = (
            rpc_url
            or os.environ.get("SOLANA_RPC_HTTP")
            or "https://api.mainnet-beta.solana.com"
        )
        self._sdk_client = SdkHeliusClient(api_key=self._api_key or "")

    def scan_cluster_history(
        self, wallet: str
    ) -> tuple[str | None, list[DiscoveredTokenLaunch]]:
        """Trace incoming SOL transfers and token creations for a wallet cluster."""
        if not self._api_key:
            return None, []
        return scan_helius_cluster_history(wallet, self._api_key)

    def rpc_call(self, method: str, params: list[object]) -> object:
        """Execute raw JSON-RPC call with retry rotation."""
        endpoints = [
            self._rpc_url,
            "https://api.mainnet-beta.solana.com",
            "https://rpc.ankr.com/solana",
        ]
        payload = json.dumps(
            {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
        ).encode()

        last_exc: Exception | None = None
        for ep in endpoints:
            for _ in range(2):
                try:
                    req = urllib.request.Request(
                        ep,
                        data=payload,
                        headers={
                            "Content-Type": "application/json",
                            "User-Agent": "Mozilla/5.0",
                        },
                    )
                    with urllib.request.urlopen(req, timeout=10) as resp:
                        data: dict[str, Any] = json.loads(resp.read().decode())
                        return data.get("result")
                except Exception as exc:
                    last_exc = exc
                    time.sleep(0.3)
        if last_exc:
            raise last_exc
        return None


__all__ = [
    "DiscoveredTokenLaunch",
    "HeliusClient",
    "SolanaLogsStream",
    "WalletLogNotification",
    "scan_helius_cluster_history",
]
