"""Solana standard RPC transaction sender."""

from __future__ import annotations

import time

from solana.rpc.commitment import Processed
from solana.rpc.types import TxOpts
from solders.transaction import Transaction

from rugbot.execution.rpc_client import SolanaClient
from rugbot.execution.sender.base import SubmissionResult
from rugbot.utils.logger import get_logger

logger = get_logger(__name__)


class RpcSender:
    """Sends raw signed transactions to a Solana RPC node with skip_preflight."""

    def __init__(
        self,
        endpoint: str,
        client: SolanaClient | None = None,
    ) -> None:
        self.endpoint = endpoint
        self._client = client or SolanaClient(endpoint)

    @property
    def name(self) -> str:
        return "rpc"

    async def send_transaction(self, raw_tx_bytes: bytes) -> SubmissionResult:
        """Send deserialized transaction directly to RPC with zero retries."""
        start_t = time.perf_counter()
        try:
            tx = Transaction.from_bytes(raw_tx_bytes)
            async_client = await self._client.get_client()
            tx_opts = TxOpts(
                skip_preflight=True,
                preflight_commitment=Processed,
                max_retries=0,
            )
            response = await async_client.send_transaction(tx, tx_opts)
            ack_ms = (time.perf_counter() - start_t) * 1000.0
            signature = str(response.value)
            return SubmissionResult(
                sender_name=self.name,
                signature=signature,
                ack_ms=ack_ms,
                acknowledged=True,
            )
        except Exception as error:  # noqa: BLE001
            ack_ms = (time.perf_counter() - start_t) * 1000.0
            return SubmissionResult(
                sender_name=self.name,
                signature="",
                ack_ms=ack_ms,
                acknowledged=False,
                error_message=f"{type(error).__name__}: {error}",
            )
