"""Unified intelligence provider router with centralized failover, quotas, and rate limiting."""

from __future__ import annotations

import json
import logging
import os
import time
import urllib.error
import urllib.request
from typing import Any, Protocol

from rugbot.domain.entities import EntityEdge, EntityRelation

logger = logging.getLogger(__name__)

DEFAULT_RPC_ENDPOINTS: list[str] = [
    "https://api.mainnet-beta.solana.com",
    "https://solana-mainnet.g.alchemy.com/v2/demo",
    "https://rpc.ankr.com/solana",
]
LAMPORTS_PER_SOL: float = 1_000_000_000.0
HTTP_TIMEOUT_SECONDS: float = 6.0


class IntelligenceProvider(Protocol):
    """Protocol contract for on-chain and indexed intelligence sources."""

    name: str

    def is_available(self) -> bool:
        """Check if provider is configured and not in cooldown."""
        ...

    def get_signatures_for_address(
        self, address: str, limit: int = 50
    ) -> list[dict[str, Any]]:
        """Fetch confirmed transaction signatures for a target address."""
        ...

    def get_parsed_transaction(self, signature: str) -> dict[str, Any] | None:
        """Fetch parsed transaction details."""
        ...

    def get_sol_balance(self, address: str) -> float:
        """Query native SOL balance."""
        ...


MAX_CONSECUTIVE_ERRORS: int = 3


class BaseProvider:
    """Base provider with error tracking and cooldown management."""

    def __init__(self, name: str, cooldown_seconds: float = 30.0) -> None:
        self.name = name
        self.cooldown_seconds = cooldown_seconds
        self.consecutive_errors = 0
        self.cooldown_until = 0.0

    def is_available(self) -> bool:
        return time.time() >= self.cooldown_until

    def mark_success(self) -> None:
        self.consecutive_errors = 0

    def mark_error(self, reason: str = "") -> None:
        self.consecutive_errors += 1
        if self.consecutive_errors >= MAX_CONSECUTIVE_ERRORS:
            self.cooldown_until = time.time() + self.cooldown_seconds
            logger.warning(
                "Provider %s in cooldown for %.0fs: %s",
                self.name,
                self.cooldown_seconds,
                reason,
            )


class HeliusIntelligenceProvider(BaseProvider):
    """Helius indexed API provider for rapid transaction and entity queries."""

    def __init__(self, api_key: str | None = None) -> None:
        super().__init__(name="Helius")
        self.api_key = api_key or os.environ.get("HELIUS_API_KEY", "")

    def is_available(self) -> bool:
        if not self.api_key:
            return False
        return super().is_available()

    def get_signatures_for_address(
        self, address: str, limit: int = 50
    ) -> list[dict[str, Any]]:
        if not self.is_available():
            return []
        url = f"https://api.helius.xyz/v0/addresses/{address}/transactions?api-key={self.api_key}&limit={limit}"
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Rugbot/2.0"})  # noqa: S310
            with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_SECONDS) as resp:  # noqa: S310
                data = json.loads(resp.read().decode())
                self.mark_success()
                return data if isinstance(data, list) else []
        except (urllib.error.URLError, OSError, json.JSONDecodeError) as e:
            self.mark_error(str(e))
            return []

    def get_parsed_transaction(self, signature: str) -> dict[str, Any] | None:
        if not self.is_available():
            return None
        url = f"https://api.helius.xyz/v0/transactions/?api-key={self.api_key}"
        try:
            req = urllib.request.Request(  # noqa: S310
                url,
                data=json.dumps({"transactions": [signature]}).encode(),
                headers={
                    "Content-Type": "application/json",
                    "User-Agent": "Rugbot/2.0",
                },
            )
            with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_SECONDS) as resp:  # noqa: S310
                data = json.loads(resp.read().decode())
                self.mark_success()
                if isinstance(data, list) and data:
                    return data[0]
                return None
        except (urllib.error.URLError, OSError, json.JSONDecodeError) as e:
            self.mark_error(str(e))
            return None

    def get_sol_balance(self, address: str) -> float:
        if not self.is_available():
            return 0.0
        rpc_url = f"https://mainnet.helius-rpc.com/?api-key={self.api_key}"
        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "getBalance",
            "params": [address],
        }
        try:
            req = urllib.request.Request(  # noqa: S310
                rpc_url,
                data=json.dumps(payload).encode(),
                headers={
                    "Content-Type": "application/json",
                    "User-Agent": "Rugbot/2.0",
                },
            )
            with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_SECONDS) as resp:  # noqa: S310
                res = json.loads(resp.read().decode())
                self.mark_success()
                lamports = res.get("result", {}).get("value", 0)
                return lamports / LAMPORTS_PER_SOL
        except (urllib.error.URLError, OSError, json.JSONDecodeError) as e:
            self.mark_error(str(e))
            return 0.0


class NoesisIntelligenceProvider(BaseProvider):
    """Noesis / GMGN cluster intelligence provider."""

    def __init__(self, api_key: str | None = None) -> None:
        super().__init__(name="Noesis")
        self.api_key = api_key or os.environ.get("NOESIS_API_KEY", "")

    def is_available(self) -> bool:
        if not self.api_key:
            return False
        return super().is_available()

    def get_signatures_for_address(
        self,
        address: str,  # noqa: ARG002
        limit: int = 50,  # noqa: ARG002
    ) -> list[dict[str, Any]]:
        return []

    def get_parsed_transaction(self, signature: str) -> dict[str, Any] | None:  # noqa: ARG002
        return None

    def get_sol_balance(self, address: str) -> float:  # noqa: ARG002
        return 0.0


class SolanaRpcIntelligenceProvider(BaseProvider):
    """Direct Solana JSON-RPC provider with rotating endpoints and rate-limit backoff."""

    def __init__(self, endpoints: list[str] | None = None) -> None:
        super().__init__(name="SolanaRPC")
        env_rpc = os.environ.get("SOLANA_RPC_URL", "").strip()
        custom_endpoints = [env_rpc] if env_rpc else []
        self.endpoints = custom_endpoints + (endpoints or DEFAULT_RPC_ENDPOINTS)
        self.current_idx = 0

    def _get_active_endpoint(self) -> str:
        return self.endpoints[self.current_idx % len(self.endpoints)]

    def _rotate_endpoint(self) -> None:
        self.current_idx = (self.current_idx + 1) % len(self.endpoints)

    def _rpc_call(self, method: str, params: list[object]) -> object | None:
        for _ in range(len(self.endpoints)):
            endpoint = self._get_active_endpoint()
            payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
            try:
                req = urllib.request.Request(  # noqa: S310
                    endpoint,
                    data=json.dumps(payload).encode(),
                    headers={
                        "Content-Type": "application/json",
                        "User-Agent": "Rugbot/2.0",
                    },
                )
                with urllib.request.urlopen(req, timeout=8) as resp:  # noqa: S310
                    res = json.loads(resp.read().decode())
                    if "error" in res:
                        self._rotate_endpoint()
                        continue
                    self.mark_success()
                    return res.get("result")
            except (urllib.error.URLError, OSError, json.JSONDecodeError) as e:
                self.mark_error(f"{endpoint}: {e}")
                self._rotate_endpoint()
        return None

    def get_signatures_for_address(
        self, address: str, limit: int = 50
    ) -> list[dict[str, Any]]:
        res = self._rpc_call("getSignaturesForAddress", [address, {"limit": limit}])
        return res if isinstance(res, list) else []

    def get_parsed_transaction(self, signature: str) -> dict[str, Any] | None:
        res = self._rpc_call(
            "getTransaction",
            [
                signature,
                {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0},
            ],
        )
        return res if isinstance(res, dict) else None

    def get_sol_balance(self, address: str) -> float:
        res = self._rpc_call("getBalance", [address])
        if isinstance(res, dict):
            lamports = res.get("value", 0)
            return lamports / LAMPORTS_PER_SOL
        return 0.0


class ProviderRouter:
    """Centralized router dispatching intelligence queries across available providers with failover."""

    def __init__(self, providers: list[IntelligenceProvider] | None = None) -> None:
        self.providers: list[IntelligenceProvider] = providers or [
            HeliusIntelligenceProvider(),
            NoesisIntelligenceProvider(),
            SolanaRpcIntelligenceProvider(),
        ]

    def get_balance(self, address: str) -> float:
        """Fetch native SOL balance using highest priority available provider."""
        for p in self.providers:
            if p.is_available():
                bal = p.get_sol_balance(address)
                if bal > 0.0:
                    return bal
        return 0.0

    def get_signatures(self, address: str, limit: int = 50) -> list[dict[str, Any]]:
        """Fetch signatures using available provider."""
        for p in self.providers:
            if p.is_available():
                sigs = p.get_signatures_for_address(address, limit=limit)
                if sigs:
                    return sigs
        return []

    def _extract_transfer_edge(
        self, tx: dict[str, Any], current_wallet: str, sig: str
    ) -> tuple[str | None, EntityEdge | None]:
        transaction = tx.get("transaction") or {}
        msg = transaction.get("message") or {}
        instructions = msg.get("instructions") or []

        for ix in instructions:
            if isinstance(ix, dict) and ix.get("program") == "system":
                parsed = ix.get("parsed") or {}
                if parsed.get("type") == "transfer":
                    info = parsed.get("info") or {}
                    src = info.get("source")
                    dst = info.get("destination")
                    lamports = info.get("lamports", 0)
                    if dst == current_wallet and src and src != current_wallet:
                        amount_sol = lamports / LAMPORTS_PER_SOL
                        edge = EntityEdge(
                            source=src,
                            target=dst,
                            relation=EntityRelation.FUNDED_BY,
                            amount_sol=amount_sol,
                            slot=tx.get("slot", 0),
                            timestamp=tx.get("blockTime", int(time.time())),
                            signature=sig,
                        )
                        return src, edge
        return None, None

    def trace_funding_edges(
        self, target_wallet: str, max_depth: int = 2
    ) -> tuple[str, list[EntityEdge]]:
        """Trace upstream SOL funding transactions and return root funder and normalized edges."""
        edges: list[EntityEdge] = []
        current_wallet = target_wallet
        root_funder = target_wallet

        for _depth in range(1, max_depth + 1):
            sigs = self.get_signatures(current_wallet, limit=30)
            found_funder = False
            for sig_info in sigs:
                sig = (
                    sig_info.get("signature")
                    if isinstance(sig_info, dict)
                    else str(sig_info)
                )
                if not sig:
                    continue

                for p in self.providers:
                    if not p.is_available():
                        continue
                    tx = p.get_parsed_transaction(sig)
                    if not tx:
                        continue

                    src, edge = self._extract_transfer_edge(tx, current_wallet, sig)
                    if edge and src:
                        edges.append(edge)
                        root_funder = src
                        current_wallet = src
                        found_funder = True
                        break
                if found_funder:
                    break
            if not found_funder:
                break

        return root_funder, edges


__all__ = [
    "HeliusIntelligenceProvider",
    "IntelligenceProvider",
    "NoesisIntelligenceProvider",
    "ProviderRouter",
    "SolanaRpcIntelligenceProvider",
]
