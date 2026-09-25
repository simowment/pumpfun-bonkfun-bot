"""WebSocket-first wallet-trade detection source (observe-only).

``WalletTradeSource`` implements the shared ``ObservationSource`` protocol over
a set of watched wallets. The native ``logsSubscribe`` stream is the primary
trigger path; each trigger is hydrated through the canonical finalized
single-transaction evidence path (``observe_finalized_transaction``: finalized
``getTransaction`` plus finalized block ordering). When the socket keeps
failing, the source falls back to ``RpcAddressObservationSource.read()`` and
recovers to WebSocket once the connection succeeds again.

The source never places orders and never imports execution machinery. It only
measures: every returned batch records the detecting transport and the
detection lag (local now minus the hydrated transaction ``blockTime``).
"""

from __future__ import annotations

import asyncio
import json
import time
from collections import deque
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Protocol

import base58

from rugbot.domain.decisions import AbstainReason, AbstainResult
from rugbot.ingest.rpc_observer import observe_finalized_transaction
from rugbot.integrations.solana_logs_stream import SolanaLogsStream
from rugbot.storage.jsonl_observation_store import observation_identity
from rugbot.utils.logger import get_logger

if TYPE_CHECKING:
    from collections.abc import Sequence

    from sol_trade_sdk.solana.provider_pool import RpcHttpTransport

    from rugbot.domain.observations import RawChainObservation
    from rugbot.integrations.solana_logs_stream import WalletLogNotification
    from rugbot.runtime.workers.observation_loop import (
        ObservationBatch,
        ObservationReadResult,
    )
    from rugbot.storage.handled_evidence_ledger import HandledEvidenceLedger

logger = get_logger(__name__)

VALID_COMMITMENTS = frozenset({"finalized", "confirmed", "processed"})
SOURCE_ID = "wallet-trade-source"
SEEN_SIGNATURE_BOUND = 4096
DEFAULT_READ_TIMEOUT_SECONDS = 10.0
HYDRATE_ATTEMPTS = 6
HYDRATE_RETRY_SECONDS = 3.0
LAMPORTS_PER_SOL = 1_000_000_000


class WalletLogStream(Protocol):
    """Structural WebSocket trigger stream used by the trade source."""

    @property
    def connected(self) -> bool:
        """Whether the shared socket is currently connected."""

    @property
    def failed(self) -> bool:
        """Whether at least one connection or receive attempt has failed."""

    async def reconcile(self, wallets: object) -> None:
        """Replace the active wallet subscription set on the shared socket."""

    async def next_notification(self) -> WalletLogNotification:
        """Wait for the next successful transaction trigger."""


HydrateFn = Callable[
    [str, str, int | None], Awaitable["ObservationBatch | AbstainResult | None"]
]


def observation_block_time_ms(observation: RawChainObservation) -> int | None:
    """Return the hydrated transaction ``blockTime`` in milliseconds.

    Args:
        observation: Finalized observation carrying the raw RPC payload.

    Returns:
        ``blockTime`` in ms, or ``None`` when the payload is absent or
        unproven.
    """
    payload = observation.raw_source_payload
    if not payload:
        return None
    try:
        document = json.loads(payload)
    except (ValueError, UnicodeError):
        return None
    if not isinstance(document, dict):
        return None
    result = document.get("result")
    if not isinstance(result, dict):
        return None
    block_time = result.get("blockTime")
    if isinstance(block_time, bool) or not isinstance(block_time, int):
        return None
    return block_time * 1000


def detection_lag_ms(observation: RawChainObservation, *, now_ms: int) -> int | None:
    """Return ``now_ms`` minus the observation ``blockTime`` in ms.

    Args:
        observation: Hydrated observation to measure.
        now_ms: Local wall-clock time in milliseconds.

    Returns:
        Detection lag in ms, or ``None`` when ``blockTime`` is unavailable.
    """
    block_ms = observation_block_time_ms(observation)
    if block_ms is None:
        return None
    return now_ms - block_ms


def observation_signature_str(observation: RawChainObservation) -> str | None:
    """Return the base58 transaction signature, if present."""
    if observation.signature is None:
        return None
    try:
        return base58.b58encode(observation.signature).decode("ascii")
    except (ValueError, UnicodeError):
        return None


class WalletTradeSource:
    """Detect wallet trades WebSocket-first with a finalized poll fallback.

    The source owns no execution path: it only returns hydrated finalized
    observations plus the transport and lag side-channels the observe-only
    CLI prints.
    """

    def __init__(  # noqa: PLR0913
        self,
        addresses: Sequence[str],
        *,
        endpoint: str,
        websocket_endpoint: str | None,
        commitment: str = "processed",
        poll_source: object,
        ws_failure_threshold: int = 3,
        poll_source_factory: Callable[[str], object] | None = None,
        stream: WalletLogStream | None = None,
        hydrate_fn: HydrateFn | None = None,
        transport: RpcHttpTransport | None = None,
        read_timeout_seconds: float = DEFAULT_READ_TIMEOUT_SECONDS,
        now_ms_fn: Callable[[], int] | None = None,
    ) -> None:
        """Initialize the source over one or more watched wallets.

        Args:
            addresses: Watched wallet addresses (at least one required).
            endpoint: HTTP JSON-RPC endpoint for finalized hydration/polling.
            websocket_endpoint: Native WSS endpoint; ``None`` means poll-only.
            commitment: ``logsSubscribe`` commitment level.
            poll_source: Finalized HTTP fallback source (one address).
            ws_failure_threshold: Consecutive WS failures before polling.
            poll_source_factory: Builds one fallback source per extra address.
            stream: Injected trigger stream (tests); built when omitted.
            hydrate_fn: Injected ``(wallet, signature)`` hydrator (tests).
            transport: Optional injected HTTP transport for hydration.
            read_timeout_seconds: Max wait per ``read`` for a WS trigger.
            now_ms_fn: Injected wall-clock in ms (tests).
        """
        if not addresses:
            raise ValueError("at least one wallet address is required")  # noqa: TRY003
        if commitment not in VALID_COMMITMENTS:
            raise ValueError(  # noqa: TRY003
                f"commitment must be one of {sorted(VALID_COMMITMENTS)}"
            )
        if ws_failure_threshold < 1:
            raise ValueError(  # noqa: TRY003
                "ws_failure_threshold must be positive"
            )
        self._addresses = tuple(addresses)
        self._endpoint = endpoint
        self._commitment = commitment
        self._poll_source = poll_source
        self._poll_sources: tuple[object, ...] = (poll_source,)
        if poll_source_factory is not None:
            extra = [poll_source_factory(address) for address in self._addresses[1:]]
            self._poll_sources = (poll_source, *extra)
        self._ws_failure_threshold = ws_failure_threshold
        if stream is not None:
            self._stream: WalletLogStream | None = stream
        elif websocket_endpoint is not None:
            self._stream = SolanaLogsStream(websocket_endpoint, commitment=commitment)
        else:
            self._stream = None
        self._hydrate_fn = hydrate_fn or self._default_hydrate
        self._transport = transport
        self._read_timeout_seconds = read_timeout_seconds
        self._now_ms_fn = now_ms_fn or (lambda: int(time.time() * 1000))
        self._consecutive_failures = 0
        self._using_poll = self._stream is None
        self._poll_index = 0
        self._seen_signatures: set[str] = set()
        self._seen_order: deque[str] = deque(maxlen=SEEN_SIGNATURE_BOUND)
        self._receive_sequence = 0
        self._last_detection_lag_ms: int | None = None
        self._last_transport: str | None = None
        self._last_wallet: str | None = None
        self._last_signature: str | None = None

    @property
    def last_detection_lag_ms(self) -> int | None:
        """Return the detection lag measured for the most recent batch."""

        return self._last_detection_lag_ms

    @property
    def last_transport(self) -> str | None:
        """Return ``"ws"`` or ``"poll"`` for the most recent batch."""

        return self._last_transport

    @property
    def last_wallet(self) -> str | None:
        """Return the wallet of the most recently detected trade."""

        return self._last_wallet

    @property
    def last_signature(self) -> str | None:
        """Return the signature of the most recently detected trade."""

        return self._last_signature

    @property
    def transport_in_use(self) -> str:
        """Return the transport the next ``read`` will prefer."""

        return "poll" if self._using_poll else "ws"

    async def read(self) -> ObservationReadResult:
        """Read one bounded batch, WebSocket-first with a poll fallback.

        Never raises for transport errors; those become abstentions (below
        the failure threshold) or a poll-fallback read (at/after it).
        """
        self._note_recovery()
        if not self._using_poll and self._stream is not None:
            result = await self._read_websocket()
            if result is not None:
                return result
            if not self._using_poll:
                return _abstain(
                    AbstainReason.STALE_STATE, "no wallet trade trigger yet"
                )
        return await self._read_poll_fallback()

    async def _read_websocket(  # noqa: PLR0911
        self,
    ) -> ObservationReadResult | None:
        """Attempt one WS trigger; ``None`` means the caller must abstain."""
        stream = self._stream
        if stream is None:
            return None
        try:
            await stream.reconcile(self._addresses)
            notification = await asyncio.wait_for(
                stream.next_notification(), timeout=self._read_timeout_seconds
            )
            hydrated = await self._hydrate_fn(
                notification.wallet, notification.signature, notification.slot
            )
        except TimeoutError:
            return None
        except asyncio.CancelledError:
            raise
        except Exception as error:  # noqa: BLE001 - fail-soft transport error
            self._register_ws_failure(type(error).__name__)
            return None
        if isinstance(hydrated, AbstainResult):
            if hydrated.reason is AbstainReason.MISSING_FEATURE:
                self._register_ws_failure(hydrated.reason.value)
            return None
        if not hydrated:
            return None
        fresh = self._filter_fresh(hydrated)
        if not fresh:
            return None
        self._consecutive_failures = 0
        self._record_side_channel(fresh, transport="ws", wallet=notification.wallet)
        return fresh

    async def _read_poll_fallback(self) -> ObservationReadResult:
        """Read one batch from the finalized HTTP fallback sources."""
        sources = self._poll_sources or (self._poll_source,)
        attempts = len(sources)
        last_abstention: AbstainResult | None = None
        for _ in range(attempts):
            source = sources[self._poll_index % len(sources)]
            self._poll_index += 1
            try:
                result = await source.read()  # type: ignore[union-attr]
            except asyncio.CancelledError:
                raise
            except Exception as error:  # noqa: BLE001 - fail-soft transport error
                last_abstention = _abstain(
                    AbstainReason.MISSING_FEATURE,
                    f"poll fallback failed: {type(error).__name__}",
                )
                continue
            if isinstance(result, AbstainResult):
                last_abstention = result
                continue
            fresh = self._filter_fresh(result)
            if not fresh:
                last_abstention = _abstain(
                    AbstainReason.STALE_STATE, "poll fallback saw only duplicates"
                )
                continue
            wallet = getattr(source, "address", None)
            self._record_side_channel(
                fresh,
                transport="poll",
                wallet=wallet if isinstance(wallet, str) else None,
            )
            return fresh
        if last_abstention is not None:
            return last_abstention
        return _abstain(AbstainReason.STALE_STATE, "poll fallback has no evidence")

    async def _default_hydrate(
        self, wallet: str, signature: str, slot: int | None
    ) -> ObservationBatch | AbstainResult | None:
        """Hydrate one WS signature into finalized transaction evidence.

        Uses the canonical single-transaction finalized path
        (``observe_finalized_transaction``: finalized ``getTransaction`` plus
        finalized block ordering) rather than a newest-history window. A
        history window cannot work on burst wallets: hundreds of newer
        signatures push the trigger beyond any bounded page within seconds,
        so the window systematically misses. The direct lookup costs three
        RPCs regardless of wallet spam rate. While finalization has not
        reached the notified slot the lookup abstains stale and is retried a
        bounded number of times; failed transactions resolve to ``None`` and
        are never reported as detections.
        """
        del wallet
        for attempt in range(HYDRATE_ATTEMPTS):
            try:
                result = await observe_finalized_transaction(
                    signature,
                    expected_slot=slot,
                    endpoint=self._endpoint,
                    source_id=SOURCE_ID,
                    transport=self._transport,
                )
            except asyncio.CancelledError:
                raise
            except Exception as error:  # noqa: BLE001 - fail-soft transport error
                return _abstain(
                    AbstainReason.MISSING_FEATURE,
                    f"wallet hydration failed: {type(error).__name__}",
                )
            if result is None:
                return None
            if isinstance(result, AbstainResult):
                if result.reason is not AbstainReason.STALE_STATE:
                    return result
            else:
                return (result,)
            if attempt + 1 < HYDRATE_ATTEMPTS:
                await asyncio.sleep(HYDRATE_RETRY_SECONDS)
        return None

    def _filter_fresh(self, batch: ObservationBatch) -> ObservationBatch:
        """Drop reorg-reprocessed or already-reported signatures."""
        fresh: list[RawChainObservation] = []
        for observation in batch:
            signature = observation_signature_str(observation)
            if signature is None:
                continue
            if signature in self._seen_signatures:
                continue
            if self._already_handled(observation):
                self._remember_signature(signature)
                continue
            self._remember_signature(signature)
            fresh.append(observation)
        return tuple(fresh)

    def _already_handled(self, observation: RawChainObservation) -> bool:
        """Check fallback handled ledgers without ever raising."""
        for source in self._poll_sources:
            ledger: HandledEvidenceLedger | None = getattr(
                source, "handled_ledger", None
            )
            if ledger is None:
                continue
            try:
                if ledger.contains(observation_identity(observation)):
                    return True
            except Exception:  # noqa: BLE001, S112 - fail-soft ledger read
                continue
        return False

    def _remember_signature(self, signature: str) -> None:
        """Record a signature in the bounded internal dedupe set."""
        if signature in self._seen_signatures:
            return
        if len(self._seen_order) == self._seen_order.maxlen:
            oldest = self._seen_order[0]
            self._seen_signatures.discard(oldest)
        self._seen_order.append(signature)
        self._seen_signatures.add(signature)

    def _record_side_channel(
        self, batch: ObservationBatch, *, transport: str, wallet: str | None
    ) -> None:
        """Record transport, lag, and identity for the returned batch."""
        now_ms = self._now_ms_fn()
        newest = max(batch, key=lambda item: (item.slot, item.receive_sequence))
        signature = observation_signature_str(newest)
        lag: int | None = None
        for observation in sorted(
            batch, key=lambda item: (item.slot, item.receive_sequence)
        ):
            measured = detection_lag_ms(observation, now_ms=now_ms)
            if measured is not None:
                lag = measured
        if signature is not None:
            self._last_signature = signature
        if wallet is not None:
            self._last_wallet = wallet
        self._last_transport = transport
        self._last_detection_lag_ms = lag
        self._receive_sequence += len(batch)

    def _register_ws_failure(self, reason: str) -> None:
        """Count one WS failure and switch to poll at the threshold (once)."""
        self._consecutive_failures += 1
        if (
            self._consecutive_failures >= self._ws_failure_threshold
            and not self._using_poll
        ):
            self._using_poll = True
            logger.info(
                "wallet trade source falling back to HTTP poll after %d "
                "WebSocket failures (%s)",
                self._consecutive_failures,
                reason,
            )

    def _note_recovery(self) -> None:
        """Recover to WebSocket when the connection succeeds again (once)."""
        stream = self._stream
        if stream is None:
            return
        try:
            connected = stream.connected
        except Exception:  # noqa: BLE001 - fail-soft health check
            return
        if connected:
            if self._using_poll:
                self._using_poll = False
                logger.info("wallet trade source recovered to WebSocket triggers")
            self._consecutive_failures = 0
        elif _stream_failed(stream):
            self._register_ws_failure("failed")


def _stream_failed(stream: WalletLogStream) -> bool:
    """Read the ``failed`` latch without ever raising."""
    try:
        return bool(stream.failed)
    except Exception:  # noqa: BLE001 - fail-soft health check
        return False


def _abstain(reason: AbstainReason, message: str) -> AbstainResult:
    """Build a typed abstention for an empty or failed read."""
    return AbstainResult(reason=reason, message=message, as_of_slot=-1)


__all__ = [
    "SOURCE_ID",
    "WalletLogStream",
    "WalletTradeSource",
    "detection_lag_ms",
    "observation_block_time_ms",
    "observation_signature_str",
]
