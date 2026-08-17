"""Persistent Pump.fun create stream with finalized transaction hydration."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any
from uuid import UUID, uuid4

import base58
import websockets

from rugbot.domain.decisions import AbstainReason, AbstainResult
from rugbot.ingest.rpc_observer import observe_finalized_transaction
from rugbot.runtime.observation_loop import RpcAddressObservationSource

if TYPE_CHECKING:
    from pathlib import Path
    from typing import Any

    from rugbot.domain.observations import RawChainObservation
    from rugbot.ingest.rpc_observer import RpcHttpTransport
    from rugbot.storage.handled_evidence_ledger import HandledEvidenceLedger

PUMP_PROGRAM_ID = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
STREAM_SOURCE_ID = "solana-http-rpc"
SIGNATURE_BYTES = 64
PUBKEY_BYTES = 32


@dataclass(slots=True)
class PumpCreateStreamSource:
    """Read Pump create notifications and emit finalized observations.

    The first read performs bounded HTTP catch-up through the same source
    identity and durable cursor as normal polling. Subsequent reads keep one
    processed WebSocket subscription open, but only return finalized HTTP
    transaction evidence to the shared observation loop.
    """

    wallet: str
    websocket_endpoint: str
    endpoint: str
    raw_observation_path: Path
    handled_ledger: HandledEvidenceLedger
    max_catchup_transactions: int = 20
    transport: RpcHttpTransport | None = None
    observer_id: str = "pump-create-stream"
    _boot_id: UUID = field(default_factory=uuid4, init=False)
    _receive_sequence: int = field(default=0, init=False)
    _catchup_complete: bool = field(default=False, init=False)
    _catchup_source: RpcAddressObservationSource = field(init=False)
    _websocket: Any = field(default=None, init=False, repr=False)
    _subscription_id: int | None = field(default=None, init=False)
    _pending_signature: tuple[str, int] | None = field(default=None, init=False)
    _pending_batch: tuple[RawChainObservation, ...] | None = field(
        default=None, init=False
    )
    _pending_kind: str | None = field(default=None, init=False)

    def __post_init__(self) -> None:
        """Validate the wallet and create the durable HTTP catch-up source."""

        if not _valid_pubkey(self.wallet):
            raise ValueError("stream wallet must be a valid Solana public key")  # noqa: TRY003
        if not self.websocket_endpoint.strip():
            raise ValueError("stream WebSocket endpoint is required")  # noqa: TRY003
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

    async def read(
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
                self._pending_signature = await self._next_signature()
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
                    await _sleep_async(0.25)
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
        """Close the persistent WebSocket connection."""

        websocket = self._websocket
        self._websocket = None
        if websocket is not None:
            await websocket.close()

    async def _next_signature(self) -> tuple[str, int]:
        """Wait for the next Pump create notification with reconnects."""

        while True:
            try:
                if self._websocket is None:
                    await self._connect()
                message = await self._websocket.recv()
                parsed = _parse_notification(message)
                if parsed is None:
                    continue
                else:
                    return parsed
            except (OSError, ValueError, websockets.WebSocketException):
                await self._disconnect()
                await _sleep_async(1.0)

    async def _connect(self) -> None:
        """Open and validate one logsSubscribe connection."""

        websocket = await websockets.connect(self.websocket_endpoint)
        request = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "logsSubscribe",
            "params": [
                {"mentions": [self.wallet]},
                {"commitment": "processed"},
            ],
        }
        await websocket.send(json.dumps(request, separators=(",", ":")))
        confirmation = json.loads(await websocket.recv())
        subscription_id = (
            confirmation.get("result") if type(confirmation) is dict else None
        )
        if type(subscription_id) is not int:
            await websocket.close()
            raise ValueError(  # noqa: TRY003
                "Pump stream subscription was not confirmed"
            )
        self._websocket = websocket
        self._subscription_id = subscription_id

    async def _disconnect(self) -> None:
        websocket = self._websocket
        self._websocket = None
        self._subscription_id = None
        if websocket is not None:
            await websocket.close()


def _parse_notification(message: object) -> tuple[str, int] | None:
    """Extract one Pump create signature from a logs notification."""

    if isinstance(message, bytes):
        message = message.decode("utf-8")
    if not isinstance(message, str):
        raise TypeError("Pump stream returned a non-text message")  # noqa: TRY003
    payload = json.loads(message)
    if type(payload) is not dict or payload.get("method") != "logsNotification":
        return None
    params = payload.get("params")
    result = params.get("result") if type(params) is dict else None
    context = result.get("context") if type(result) is dict else None
    value = result.get("value") if type(result) is dict else None
    logs = value.get("logs") if type(value) is dict else None
    signature = value.get("signature") if type(value) is dict else None
    error = value.get("err") if type(value) is dict else None
    slot = context.get("slot") if type(context) is dict else None
    if (
        type(logs) is not list
        or any(type(item) is not str for item in logs)
        or error is not None
        or not _is_pump_create(logs)
        or not _valid_signature(signature)
        or type(slot) is not int
        or slot < 0
    ):
        return None
    return signature, slot


def _is_pump_create(logs: list[str]) -> bool:
    """Require Pump.fun program execution and a create instruction."""

    return PUMP_PROGRAM_ID in "\n".join(logs) and any(
        "Instruction: Create" in log or "Instruction: CreateV2" in log for log in logs
    )


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


async def _sleep_async(seconds: float) -> None:
    await asyncio.sleep(seconds)
