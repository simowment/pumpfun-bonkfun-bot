"""Shared finalized-launch observation producer owned by RugbotCore.

The producer owns one finalized address-observation source per active tracked
wallet. Each source is polled with bounded concurrency and its decoded Pump
create_v2 launches are delivered serially into ``TrackerService.handle_launch``,
which remains the single idempotency gate for alert publication. A durable
per-address activation cursor (finalized slot) is persisted at producer start so
historical launches are never alerted as the "next launch".
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from enum import StrEnum
from typing import TYPE_CHECKING, TypeAlias

import base58
from sol_trade_sdk.solana.provider_pool import AiohttpRpcTransport

from rugbot.domain.decisions import AbstainReason, AbstainResult
from rugbot.ingest.pump.models import TokenLaunch
from rugbot.ingest.pump.pump_create_observation import decode_pump_create_v2_observation
from rugbot.ingest.rpc_observer import observe_finalized_transaction
from rugbot.integrations.solana_logs_stream import SolanaLogsStream
from rugbot.runtime.workers.observation_loop import (
    ObservationBatch,
    ObservationSource,
    RpcAddressObservationSource,
)
from rugbot.utils.logger import get_logger

if TYPE_CHECKING:
    from sol_trade_sdk.solana.provider_pool import RpcHttpTransport

    from rugbot.domain.launches import LaunchCreatedV2
    from rugbot.domain.observations import RawChainObservation
    from rugbot.ingest.pump.pump_stream import (
        PumpPortalLaunchNotification,
        PumpPortalLaunchStream,
    )
    from rugbot.storage.tracker import SQLiteTrackerRepository
    from rugbot.tracker.service import TrackerService

logger = get_logger(__name__)

HTTP_OK = 200
HTTP_TOO_MANY_REQUESTS = 429
MAX_SLOT_RESOLVE_ATTEMPTS = 3
FINALIZED_SLOT_RESOLVE_TIMEOUT_SECONDS = 5.0
FINALIZATION_WAIT_SECONDS = 60.0
NATIVE_STREAM_SOURCE_ID = "solana-native-wss"
PUMPPORTAL_STREAM_SOURCE_ID = "pumpportal-new-token"

FinalizedSlotResolver: TypeAlias = Callable[[], Awaitable[int]]
SourceFactory: TypeAlias = Callable[[str], ObservationSource]
GlobalLaunchHandler: TypeAlias = Callable[["PumpPortalLaunchNotification"], None]


class LaunchObservationStatus(StrEnum):
    """Operator-visible health of the tracked launch ingestion path."""

    STOPPED = "stopped"
    CONNECTING = "connecting"
    PUMPPORTAL_LIVE = "pumpportal_live"
    WSS_LIVE = "wss_live"
    HTTP_CATCHUP = "http_catchup"
    DISCONNECTED = "disconnected"


class _InvalidFinalizedSlotError(ValueError):
    """Raised when the RPC finalized slot response is malformed."""


class TrackedLaunchObservationProducer:
    """Own one finalized address-observation source per active tracked wallet.

    The producer reconciles its sources to the engine's active tracked-wallet
    set on ``refresh``, starting sources for newly active addresses and stopping
    sources for removed ones. Delivery into ``TrackerService.handle_launch`` is
    serialized so the shared engine is never mutated concurrently.
    """

    def __init__(  # noqa: PLR0913
        self,
        *,
        service: TrackerService,
        repository: SQLiteTrackerRepository,
        endpoint: str,
        websocket_endpoint: str | None = None,
        pumpportal_stream: PumpPortalLaunchStream | None = None,
        global_launch_handler: GlobalLaunchHandler | None = None,
        transport: RpcHttpTransport | None = None,
        finalized_slot_resolver: FinalizedSlotResolver | None = None,
        source_factory: SourceFactory | None = None,
        poll_interval_seconds: float = 5.0,
        max_concurrency: int = 4,
    ) -> None:
        """Initialize the producer with the shared tracker stack and RPC endpoint."""
        self._service = service
        self._repository = repository
        self._endpoint = endpoint
        self._stream = (
            SolanaLogsStream(websocket_endpoint) if websocket_endpoint else None
        )
        self._pumpportal_stream = pumpportal_stream
        self._global_launch_handler = global_launch_handler
        self._transport = transport
        self._finalized_slot_resolver = (
            finalized_slot_resolver or self._default_finalized_slot
        )
        self._source_factory = source_factory or self._default_source
        self._poll_interval_seconds = poll_interval_seconds
        self._max_concurrency = max_concurrency
        self._sources: dict[str, ObservationSource] = {}
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._stream_task: asyncio.Task[None] | None = None
        self._pumpportal_task: asyncio.Task[None] | None = None
        self._stream_addresses: set[str] = set()
        self._healthy_http_addresses: set[str] = set()
        self._attempted_http_addresses: set[str] = set()
        self._activation_failed = False
        self._delivery_lock = asyncio.Lock()
        self._semaphore = asyncio.Semaphore(max_concurrency)
        self._started = False
        self._closed = False

    @property
    def active_addresses(self) -> tuple[str, ...]:
        """Return the addresses currently being observed."""
        return tuple(self._tasks)

    @property
    def status(self) -> LaunchObservationStatus:
        """Return transport health without inventing provider telemetry."""

        if not self._started or self._closed:
            return LaunchObservationStatus.STOPPED
        status = LaunchObservationStatus.CONNECTING
        if self._pumpportal_stream is not None and self._pumpportal_stream.connected:
            status = LaunchObservationStatus.PUMPPORTAL_LIVE
        elif self._stream is not None and self._stream.connected:
            status = LaunchObservationStatus.WSS_LIVE
        elif self._healthy_http_addresses:
            status = LaunchObservationStatus.HTTP_CATCHUP
        active = set(self._tasks)
        if status is LaunchObservationStatus.CONNECTING:
            if self._activation_failed and not active:
                status = LaunchObservationStatus.DISCONNECTED
        if status is LaunchObservationStatus.CONNECTING and active:
            if self._stream is None or self._stream.failed:
                if active <= self._attempted_http_addresses:
                    status = LaunchObservationStatus.DISCONNECTED
        return status

    async def start(self) -> None:
        """Start observing every active tracked wallet."""
        if self._started:
            return
        self._started = True
        self._closed = False
        await self.refresh()
        if self._pumpportal_stream is not None:
            self._pumpportal_task = asyncio.create_task(self._poll_pumpportal())
        if self._stream is not None:
            self._stream_task = asyncio.create_task(self._poll_stream())

    async def refresh(self) -> None:
        """Reconcile sources to the engine's active tracked-wallet set."""
        active = set(self._service.engine.tracked_wallets)
        if self._pumpportal_stream is not None:
            await self._pumpportal_stream.reconcile(active)
        for address in list(self._tasks):
            if address not in active:
                await self._stop_address(address)
        new_addresses = [address for address in active if address not in self._tasks]
        new_stream_addresses = (
            active - self._stream_addresses if self._stream is not None else set()
        )
        activation_slot = None
        if new_addresses or new_stream_addresses:
            try:
                async with asyncio.timeout(FINALIZED_SLOT_RESOLVE_TIMEOUT_SECONDS):
                    activation_slot = await self._finalized_slot_resolver()
            except (OSError, ValueError, TimeoutError):
                self._activation_failed = True
                logger.warning(
                    "Failed to resolve finalized slot for new tracked addresses, will retry"
                )
                return
            self._activation_failed = False
            for address in new_stream_addresses:
                self._repository.set_launch_activation(address, activation_slot)
        if self._stream is not None:
            await self._stream.reconcile(active)
            self._stream_addresses = active
        for address in new_addresses:
            await self._start_address(address, activation_slot)

    async def stop(self) -> None:
        """Cancel all address observation tasks."""
        self._closed = True
        if self._pumpportal_task is not None:
            self._pumpportal_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._pumpportal_task
            self._pumpportal_task = None
        if self._pumpportal_stream is not None:
            await self._pumpportal_stream.close()
        if self._stream_task is not None:
            self._stream_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._stream_task
            self._stream_task = None
        if self._stream is not None:
            await self._stream.close()
        self._stream_addresses.clear()
        self._healthy_http_addresses.clear()
        self._attempted_http_addresses.clear()
        for address in list(self._tasks):
            await self._stop_address(address)
        self._started = False

    async def _poll_stream(self) -> None:
        """Hydrate finalized evidence for native WSS notifications serially."""

        if self._stream is None:
            return
        while not self._closed:
            try:
                notification = await self._stream.next_notification()
                activation_slot = self._repository.get_launch_activation(
                    notification.wallet
                )
                if activation_slot is None or notification.slot <= activation_slot:
                    continue
                observation = await self._read_finalized_stream_transaction(
                    notification.signature, notification.slot
                )
                if observation is not None:
                    await self._deliver((observation,))
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("tracked launch WebSocket observation failed")

    async def _read_finalized_stream_transaction(
        self, signature: str, slot: int
    ) -> ObservationBatch | None:
        """Wait until a WSS-triggered transaction has finalized HTTP evidence."""

        while not self._closed:
            observation = await observe_finalized_transaction(
                signature,
                expected_slot=slot,
                endpoint=self._endpoint,
                source_id=NATIVE_STREAM_SOURCE_ID,
                transport=self._transport,
            )
            if isinstance(observation, AbstainResult):
                if observation.reason is AbstainReason.STALE_STATE:
                    await asyncio.sleep(self._poll_interval_seconds)
                    continue
                logger.warning(
                    "Unable to hydrate streamed transaction %s: %s",
                    signature,
                    observation.message,
                )
                return None
            if observation is None:
                return None
            return (observation,)
        return None

    async def _poll_pumpportal(self) -> None:
        """Filter global creates and deliver only finalized tracked launches."""

        if self._pumpportal_stream is None:
            return
        while not self._closed:
            try:
                if set(self._tasks) != self._service.engine.tracked_wallets:
                    await self.refresh()
                notification = await self._pumpportal_stream.next_global_notification()
                if self._global_launch_handler is not None:
                    self._global_launch_handler(notification)
                if (
                    notification.creator_pubkey
                    not in self._service.engine.tracked_wallets
                ):
                    continue
                observation = await self._read_finalized_pumpportal_transaction(
                    notification.signature
                )
                if observation is not None:
                    await self._deliver((observation,))
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("PumpPortal launch observation failed")

    async def _read_finalized_pumpportal_transaction(
        self, signature: str
    ) -> RawChainObservation | None:
        """Resolve an exact finalized slot for a PumpPortal signature."""

        try:
            async with asyncio.timeout(FINALIZATION_WAIT_SECONDS):
                while not self._closed:
                    observation = await observe_finalized_transaction(
                        signature,
                        expected_slot=None,
                        endpoint=self._endpoint,
                        source_id=PUMPPORTAL_STREAM_SOURCE_ID,
                        transport=self._transport,
                    )
                    if isinstance(observation, AbstainResult):
                        if observation.reason in (
                            AbstainReason.MISSING_FEATURE,
                            AbstainReason.STALE_STATE,
                        ):
                            await asyncio.sleep(self._poll_interval_seconds)
                            continue
                        logger.warning(
                            "Unable to finalize PumpPortal transaction %s: %s",
                            signature,
                            observation.message,
                        )
                        return None
                    return observation
        except TimeoutError:
            logger.warning("PumpPortal transaction did not finalize: %s", signature)
        return None

    async def _start_address(
        self, address: str, activation_slot: int | None = None
    ) -> None:
        if activation_slot is None:
            activation_slot = await self._finalized_slot_resolver()
        self._repository.set_launch_activation(address, activation_slot)
        source = self._source_factory(address)
        self._sources[address] = source
        self._tasks[address] = asyncio.create_task(self._poll_address(address, source))

    async def _stop_address(self, address: str) -> None:
        task = self._tasks.pop(address, None)
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self._sources.pop(address, None)
        self._healthy_http_addresses.discard(address)
        self._attempted_http_addresses.discard(address)

    async def _poll_address(self, address: str, source: ObservationSource) -> None:
        while not self._closed:
            try:
                async with self._semaphore:
                    result = await source.read()
                self._attempted_http_addresses.add(address)
                if isinstance(result, AbstainResult):
                    self._healthy_http_addresses.discard(address)
                    await asyncio.sleep(self._poll_interval_seconds)
                    continue
                self._healthy_http_addresses.add(address)
                activation_slot = self._repository.get_launch_activation(address)
                fresh = tuple(o for o in result if o.slot > activation_slot)
                if fresh:
                    await self._deliver(fresh)
                _acknowledge(source, result)
            except asyncio.CancelledError:
                raise
            except Exception:
                self._attempted_http_addresses.add(address)
                self._healthy_http_addresses.discard(address)
                logger.exception("tracked launch observation failed for %s", address)
            await asyncio.sleep(self._poll_interval_seconds)

    async def _deliver(self, observations: ObservationBatch) -> None:
        async with self._delivery_lock:
            for observation in observations:
                decoded = decode_pump_create_v2_observation(observation)
                if isinstance(decoded, AbstainResult) or decoded is None:
                    continue
                self._service.handle_launch(self._to_token_launch(decoded))

    def _to_token_launch(self, launch: LaunchCreatedV2) -> TokenLaunch:
        signature = (
            base58.b58encode(launch.signature).decode("ascii")
            if launch.signature is not None
            else ""
        )
        return TokenLaunch(
            signature=signature,
            slot=int(launch.as_of_slot),
            timestamp=int(datetime.now(UTC).timestamp()),
            creator=launch.user_pubkey,
            mint=launch.mint_pubkey,
            symbol=launch.symbol,
            name=launch.name,
        )

    def _default_source(self, address: str) -> ObservationSource:
        return RpcAddressObservationSource(
            address=address,
            endpoint=self._endpoint,
            transport=self._transport,
        )

    async def _default_finalized_slot(self) -> int:
        transport = self._transport or AiohttpRpcTransport()
        body = json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "getSlot",
                "params": [{"commitment": "finalized"}],
            }
        ).encode("utf-8")
        last_attempt_index = MAX_SLOT_RESOLVE_ATTEMPTS - 1
        for attempt in range(MAX_SLOT_RESOLVE_ATTEMPTS):
            try:
                response = await transport(self._endpoint, body)
                if response.status == HTTP_OK:
                    data = json.loads(response.body)
                    slot = data.get("result")
                    if isinstance(slot, int) and slot >= 0:
                        return slot
                elif response.status == HTTP_TOO_MANY_REQUESTS:
                    logger.warning(
                        "RPC rate limit (429) on getSlot, backing off (attempt %d/%d)",
                        attempt + 1,
                        MAX_SLOT_RESOLVE_ATTEMPTS,
                    )
                    await asyncio.sleep(0.5 * (2**attempt))
                    continue
            except (json.JSONDecodeError, OSError):
                if attempt == last_attempt_index:
                    raise _InvalidFinalizedSlotError from None
                await asyncio.sleep(0.5 * (2**attempt))
        raise _InvalidFinalizedSlotError


def _acknowledge(source: ObservationSource, batch: ObservationBatch) -> None:
    acknowledge = getattr(source, "acknowledge", None)
    if callable(acknowledge):
        acknowledge(batch)


__all__ = ["LaunchObservationStatus", "TrackedLaunchObservationProducer"]
