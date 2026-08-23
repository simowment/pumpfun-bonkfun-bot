"""Pump.fun create stream via PumpPortal public WebSocket (no RPC subscription)."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable, Collection, Iterable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

import base58
import websockets

from rugbot.domain.decisions import AbstainReason, AbstainResult
from rugbot.ingest.rpc_observer import observe_finalized_transaction
from rugbot.runtime.workers.observation_loop import RpcAddressObservationSource

if TYPE_CHECKING:
    from pathlib import Path

    from rugbot.domain.observations import RawChainObservation
    from rugbot.ingest.rpc_observer import RpcHttpTransport
    from rugbot.storage.handled_evidence_ledger import HandledEvidenceLedger

# PumpPortal's free global creation feed requires no RPC subscription. The
# provider stream is only a trigger; finalized HTTP evidence remains required.
PUMPPORTAL_WS_URL = "wss://pumpportal.fun/api/data"
STREAM_SOURCE_ID = "solana-http-rpc"
SIGNATURE_BYTES = 64
PUBKEY_BYTES = 32
HTTP_OK = 200
RECONNECT_DELAY_SECONDS = 1.0
MAX_RECONNECT_DELAY_SECONDS = 30.0


@dataclass(frozen=True, slots=True)
class PumpPortalLaunchNotification:
    """One validated PumpPortal launch trigger for a tracked creator."""

    signature: str
    mint_pubkey: str
    creator_pubkey: str


class PumpPortalLaunchStream:
    """Share one free global PumpPortal stream across tracked entity wallets."""

    def __init__(self, websocket_endpoint: str = PUMPPORTAL_WS_URL) -> None:
        """Initialize a validated PumpPortal WebSocket endpoint."""

        if not websocket_endpoint.startswith("wss://"):
            raise ValueError("PumpPortal stream endpoint must use wss://")  # noqa: TRY003
        self._websocket_endpoint = websocket_endpoint
        self._wallets: frozenset[str] = frozenset()
        self._websocket: object | None = None
        self._wallets_available = asyncio.Event()
        self._connected = False

    @property
    def connected(self) -> bool:
        """Whether the global creation stream is currently connected."""

        return self._connected

    async def reconcile(self, wallets: Iterable[str]) -> None:
        """Replace the creator filter without opening another connection."""

        self._wallets = frozenset(wallets)
        if self._wallets:
            self._wallets_available.set()
            return
        self._wallets_available.clear()
        await self._disconnect()

    async def next_notification(self) -> PumpPortalLaunchNotification:
        """Wait for the next creation event by any tracked entity wallet."""

        return await self._next_notification(global_feed=False)

    async def next_global_notification(self) -> PumpPortalLaunchNotification:
        """Wait for the next validated creation event from any creator."""

        return await self._next_notification(global_feed=True)

    async def _next_notification(
        self, *, global_feed: bool
    ) -> PumpPortalLaunchNotification:
        """Receive one validated notification using the selected local filter."""

        reconnect_delay = RECONNECT_DELAY_SECONDS
        while True:
            if not global_feed and not self._wallets:
                await self._wallets_available.wait()
            try:
                if self._websocket is None:
                    await self._connect()
                message = await self._websocket.recv()
                parsed = parse_pumpportal_notification(
                    message, None if global_feed else self._wallets
                )
                if parsed is None:
                    continue
                return PumpPortalLaunchNotification(
                    signature=parsed["signature"],
                    mint_pubkey=parsed["mint"],
                    creator_pubkey=parsed["creator"],
                )
            except (OSError, TypeError, ValueError, websockets.WebSocketException):
                await self._disconnect()
                await asyncio.sleep(reconnect_delay)
                reconnect_delay = min(reconnect_delay * 2, MAX_RECONNECT_DELAY_SECONDS)

    async def close(self) -> None:
        """Close the global creation stream and clear its creator filter."""

        self._wallets = frozenset()
        self._wallets_available.clear()
        await self._disconnect()

    async def _connect(self) -> None:
        websocket = await websockets.connect(self._websocket_endpoint)
        await websocket.send(
            json.dumps({"method": "subscribeNewToken"}, separators=(",", ":"))
        )
        self._websocket = websocket
        self._connected = True

    async def _disconnect(self) -> None:
        websocket = self._websocket
        self._websocket = None
        self._connected = False
        if websocket is not None:
            await websocket.close()


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

    Phase 1 - boot catch-up: bounded HTTP ``getSignaturesForAddress`` poll on
    the target wallet to replay any missed events (same durable cursor path
    as the polling mode).

    Phase 2 - live stream: one persistent PumpPortal WebSocket connection
    receives free global ``subscribeNewToken`` events and filters them by the
    target creator. A matching create triggers ``processed_handler``
    immediately, then the module waits for finalized HTTP evidence.

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
    _launch_stream: PumpPortalLaunchStream = field(init=False, repr=False)
    _pending_signature: str | None = field(default=None, init=False)
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
        self._launch_stream = PumpPortalLaunchStream()

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
                self._pending_signature = notification.signature
            signature = self._pending_signature
            sequence = self._receive_sequence + 1
            result = await observe_finalized_transaction(
                signature,
                expected_slot=None,
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

        await self._launch_stream.close()

    async def _next_notification(self) -> ProcessedPumpCreateNotification:
        """Wait for the next Pump create notification with automatic reconnects."""

        await self._launch_stream.reconcile((self.wallet,))
        notification = await self._launch_stream.next_notification()
        slot = await _get_processed_slot(self.endpoint, self.transport)
        return ProcessedPumpCreateNotification(
            signature=notification.signature,
            slot=slot,
            mint_pubkey=notification.mint_pubkey,
            creator_pubkey=notification.creator_pubkey,
        )


def parse_pumpportal_notification(
    message: object,
    target_wallets: Collection[str] | None,
) -> dict[str, str] | None:
    """Extract create fields from a global PumpPortal launch message.

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
        or (target_wallets is not None and creator not in target_wallets)
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
