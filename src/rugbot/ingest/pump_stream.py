"""Pump.fun create stream via PumpPortal public WebSocket (no RPC subscription)."""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

import base58
import websockets

from rugbot.domain.decisions import AbstainReason, AbstainResult
from rugbot.ingest.rpc_observer import observe_finalized_transaction
from rugbot.runtime.observation_loop import RpcAddressObservationSource

if TYPE_CHECKING:
    from pathlib import Path

    from rugbot.domain.observations import RawChainObservation
    from rugbot.ingest.rpc_observer import RpcHttpTransport
    from rugbot.storage.handled_evidence_ledger import HandledEvidenceLedger

# PumpPortal public WebSocket — no RPC logsSubscribe required.
# subscribeAccountTrade is metered (0.01 SOL / 10k events) but does not
# require any Solana RPC subscription.  subscribeNewToken is free but streams
# all tokens; we filter by creator on receipt.
PUMPPORTAL_WS_URL = "wss://pumpportal.fun/api/data"
STREAM_SOURCE_ID = "solana-http-rpc"
SIGNATURE_BYTES = 64
PUBKEY_BYTES = 32
HTTP_OK = 200


@dataclass(frozen=True, slots=True)
class ProcessedPumpCreateNotification:
    """Pinned create-event identity decoded from a PumpPortal notification."""

    signature: str
    slot: int
    mint_pubkey: str
    creator_pubkey: str


ProcessedCreateHandler = Callable[
    [ProcessedPumpCreateNotification],
    Awaitable[None],
]


@dataclass(slots=True)
class PumpCreateStreamSource:
    """Detect Pump.fun creates via PumpPortal WebSocket and hydrate finalized tx.

    Phase 1 — boot catch-up: bounded HTTP ``getSignaturesForAddress`` poll on
    the target wallet to replay any missed events (same durable cursor path
    as the polling mode).

    Phase 2 — live stream: one persistent PumpPortal WebSocket connection
    receives ``subscribeAccountTrade`` events.  A create trade (``txType ==
    "create"``) triggers ``processed_handler`` immediately, then the module
    waits for the finalized HTTP transaction before returning to the shared
    observation loop.

    No RPC ``logsSubscribe``, ``accountSubscribe``, or ``blockSubscribe`` is
    used.  The PumpPortal WebSocket is a public third-party stream.
    """

    wallet: str
    endpoint: str
    raw_observation_path: Path
    handled_ledger: HandledEvidenceLedger
    max_catchup_transactions: int = 20
    transport: RpcHttpTransport | None = None
    observer_id: str = "pump-create-stream"
    processed_handler: ProcessedCreateHandler | None = None
    _boot_id: UUID = field(default_factory=uuid4, init=False)
    _receive_sequence: int = field(default=0, init=False)
    _catchup_complete: bool = field(default=False, init=False)
    _catchup_source: RpcAddressObservationSource = field(init=False)
    _websocket: object = field(default=None, init=False, repr=False)
    _pending_signature: tuple[str, int] | None = field(default=None, init=False)
    _pending_batch: tuple[RawChainObservation, ...] | None = field(
        default=None, init=False
    )
    _pending_kind: str | None = field(default=None, init=False)

    def __post_init__(self) -> None:
        """Validate the wallet and initialise the durable HTTP catch-up source."""

        if not _valid_pubkey(self.wallet):
            raise ValueError("stream wallet must be a valid Solana public key")  # noqa: TRY003
        if not self.endpoint.strip():
            raise ValueError("stream HTTP endpoint is required")  # noqa: TRY003
        self._catchup_source = RpcAddressObservationSource(
            address=self.wallet,
            endpoint=self.endpoint,
            source_id=_source_id(self.wallet),
            observer_id="rpc-observer",
            max_signatures=self.max_catchup_transactions,
            max_transactions=self.max_catchup_transactions,
            raw_observation_path=self.raw_observation_path,
            handled_ledger=self.handled_ledger,
            transport=self.transport,
        )

    async def read(  # noqa: C901
        self,
    ) -> tuple[RawChainObservation, ...] | AbstainResult:
        """Read one staged catch-up or streamed finalized observation."""

        if self._pending_batch is not None:
            return self._pending_batch
        if not self._catchup_complete:
            catchup = await self._catchup_source.read()
            if isinstance(catchup, AbstainResult):
                return catchup
            if catchup:
                self._pending_batch = catchup
                self._pending_kind = "catchup"
                return catchup
            self._catchup_source.acknowledge(catchup)
            self._catchup_complete = True

        while True:
            if self._pending_signature is None:
                notification = await self._next_notification()
                if self.processed_handler is not None:
                    await self.processed_handler(notification)
                self._pending_signature = (notification.signature, notification.slot)
            signature, slot = self._pending_signature
            sequence = self._receive_sequence + 1
            result = await observe_finalized_transaction(
                signature,
                expected_slot=slot,
                endpoint=self.endpoint,
                source_id=_source_id(self.wallet),
                observer_id=self.observer_id,
                boot_id=self._boot_id,
                receive_sequence=sequence,
                transport=self.transport,
            )
            if isinstance(result, AbstainResult):
                if result.reason is AbstainReason.STALE_STATE:
                    await asyncio.sleep(0.25)
                    continue
                return result
            self._pending_signature = None
            if result is None:
                continue
            batch = (result,)
            self._pending_batch = batch
            self._pending_kind = "stream"
            return batch

    def acknowledge(self, batch: tuple[RawChainObservation, ...]) -> None:
        """Commit the exact batch returned by the previous read."""

        if self._pending_batch != batch or self._pending_kind is None:
            raise ValueError(  # noqa: TRY003
                "stream acknowledgement does not match staged batch"
            )
        if self._pending_kind == "catchup":
            self._catchup_source.acknowledge(batch)
            self._receive_sequence = max(
                self._receive_sequence,
                *(observation.receive_sequence for observation in batch),
            )
            self._catchup_complete = True
        else:
            self._receive_sequence = batch[-1].receive_sequence
        self._pending_batch = None
        self._pending_kind = None

    async def close(self) -> None:
        """Close the PumpPortal WebSocket connection."""

        websocket = self._websocket
        self._websocket = None
        if websocket is not None:
            await websocket.close()

    async def _next_notification(self) -> ProcessedPumpCreateNotification:
        """Wait for the next Pump create notification with automatic reconnects."""

        while True:
            try:
                if self._websocket is None:
                    await self._connect()
                message = await self._websocket.recv()
                parsed = _parse_pumpportal_notification(message, self.wallet)
                if parsed is None:
                    continue
                slot = await _get_processed_slot(self.endpoint, self.transport)
                return ProcessedPumpCreateNotification(
                    signature=parsed["signature"],
                    slot=slot,
                    mint_pubkey=parsed["mint"],
                    creator_pubkey=parsed["creator"],
                )
            except (OSError, ValueError, websockets.WebSocketException):
                await self._disconnect()
                await asyncio.sleep(1.0)

    async def _connect(self) -> None:
        """Open one PumpPortal WebSocket and subscribe to account trades."""

        api_key = os.environ.get("PUMPPORTAL_API_KEY", "")
        url = f"{PUMPPORTAL_WS_URL}?api-key={api_key}" if api_key else PUMPPORTAL_WS_URL
        websocket = await websockets.connect(url)
        subscribe = {
            "method": "subscribeAccountTrade",
            "keys": [self.wallet],
        }
        await websocket.send(json.dumps(subscribe, separators=(",", ":")))
        self._websocket = websocket

    async def _disconnect(self) -> None:
        websocket = self._websocket
        self._websocket = None
        if websocket is not None:
            await websocket.close()


def _parse_pumpportal_notification(
    message: object,
    target_wallet: str,
) -> dict[str, str] | None:
    """Extract create event fields from a PumpPortal account-trade message.

    Returns a dict with ``signature``, ``mint``, ``creator`` keys when the
    message is a create event from the watched wallet; ``None`` otherwise.
    """

    if isinstance(message, bytes):
        message = message.decode("utf-8")
    if not isinstance(message, str):
        raise TypeError("PumpPortal stream returned a non-text message")  # noqa: TRY003
    try:
        payload = json.loads(message)
    except json.JSONDecodeError:
        return None
    if type(payload) is not dict:
        return None

    # PumpPortal may send a status/ack message on connect — ignore it.
    tx_type = payload.get("txType")
    if tx_type != "create":
        return None

    signature = payload.get("signature")
    mint = payload.get("mint")
    # traderPublicKey is the wallet that executed the create transaction.
    creator = payload.get("traderPublicKey")

    if (
        not isinstance(signature, str)
        or not _valid_signature(signature)
        or not isinstance(mint, str)
        or not _valid_pubkey(mint)
        or not isinstance(creator, str)
        or not _valid_pubkey(creator)
        or creator != target_wallet
    ):
        return None

    return {"signature": signature, "mint": mint, "creator": creator}


async def _get_processed_slot(
    endpoint: str,
    transport: RpcHttpTransport | None,
) -> int:
    """Return an approximate current processed slot via HTTP (no subscription)."""

    import aiohttp  # noqa: PLC0415

    body = json.dumps(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "getSlot",
            "params": [{"commitment": "processed"}],
        },
        separators=(",", ":"),
    ).encode()

    if transport is not None:
        from rugbot.ingest.rpc_observer import RpcHttpResponse  # noqa: PLC0415

        response = transport(endpoint, body)
        if asyncio.isfuture(response) or asyncio.iscoroutine(response):
            response = await response
        if not isinstance(response, RpcHttpResponse) or response.status != HTTP_OK:
            return 0
        result_body = response.body
    else:
        async with (
            aiohttp.ClientSession() as session,
            session.post(
                endpoint,
                data=body,
                headers={"Content-Type": "application/json"},
            ) as resp,
        ):
            if resp.status != HTTP_OK:
                return 0
            result_body = await resp.read()

    try:
        data = json.loads(result_body)
        slot = data.get("result") if type(data) is dict else None
        return slot if type(slot) is int and slot >= 0 else 0
    except (json.JSONDecodeError, AttributeError):
        return 0


def _valid_signature(value: object) -> bool:
    if not isinstance(value, str):
        return False
    try:
        return len(base58.b58decode(value)) == SIGNATURE_BYTES
    except ValueError:
        return False


def _valid_pubkey(value: str) -> bool:
    try:
        return len(base58.b58decode(value)) == PUBKEY_BYTES
    except ValueError:
        return False


def _source_id(wallet: str) -> str:
    return f"{STREAM_SOURCE_ID}:{wallet}"
