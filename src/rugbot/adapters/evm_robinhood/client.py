"""Asynchronous JSON-RPC client for EVM and Arbitrum Orbit chains."""

# ruff: noqa: TRY003

from __future__ import annotations

import httpx

DEFAULT_TIMEOUT_SECONDS = 15.0


class EvmRpcError(Exception):
    """Raised when an EVM JSON-RPC call returns an error."""


class EvmRpcClient:
    """Async client communicating with an EVM JSON-RPC endpoint."""

    def __init__(self, rpc_url: str, timeout: float = DEFAULT_TIMEOUT_SECONDS) -> None:
        self.rpc_url = rpc_url
        self.timeout = timeout
        self._client: httpx.AsyncClient | None = None
        self._request_id = 0

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(timeout=self.timeout)
        return self._client

    async def close(self) -> None:
        """Close the underlying HTTP client."""
        if self._client is not None and not self._client.is_closed:
            await self._client.aclose()
            self._client = None

    async def call_rpc(self, method: str, params: list[object]) -> object:
        """Execute a standard JSON-RPC 2.0 request."""
        self._request_id += 1
        payload = {
            "jsonrpc": "2.0",
            "id": self._request_id,
            "method": method,
            "params": params,
        }
        client = await self._get_client()
        response = await client.post(self.rpc_url, json=payload)
        response.raise_for_status()
        data = response.json()

        if "error" in data:
            error_data = data["error"]
            msg = error_data.get("message", "Unknown RPC error")
            raise EvmRpcError(
                f"RPC {method} failed: {msg} (code {error_data.get('code')})"
            )

        return data.get("result")

    async def eth_call(self, to: str, data: str, block: str = "latest") -> str:
        """Execute an eth_call read operation."""
        params: list[object] = [{"to": to, "data": data}, block]
        result = await self.call_rpc("eth_call", params)
        return str(result)

    async def eth_get_balance(self, address: str, block: str = "latest") -> int:
        """Get native currency (ETH) balance in wei."""
        result = await self.call_rpc("eth_getBalance", [address, block])
        return int(result, 16) if isinstance(result, str) else int(result)

    async def eth_get_transaction_count(
        self, address: str, block: str = "pending"
    ) -> int:
        """Get nonce for an account."""
        result = await self.call_rpc("eth_getTransactionCount", [address, block])
        return int(result, 16) if isinstance(result, str) else int(result)

    async def eth_gas_price(self) -> int:
        """Get current gas price in wei."""
        result = await self.call_rpc("eth_gasPrice", [])
        return int(result, 16) if isinstance(result, str) else int(result)

    async def eth_estimate_gas(self, transaction: dict[str, object]) -> int:
        """Estimate gas limit for a transaction."""
        result = await self.call_rpc("eth_estimateGas", [transaction])
        return int(result, 16) if isinstance(result, str) else int(result)

    async def eth_send_raw_transaction(self, raw_tx_hex: str) -> str:
        """Broadcast a signed raw transaction."""
        result = await self.call_rpc("eth_sendRawTransaction", [raw_tx_hex])
        return str(result)
