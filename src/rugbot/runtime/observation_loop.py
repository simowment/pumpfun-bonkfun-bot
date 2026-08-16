"""One observation loop shared by online polling and offline replay."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol, TypeAlias, runtime_checkable
from uuid import UUID, uuid4

import base58

from rugbot.domain.amounts import Slot
from rugbot.domain.decisions import AbstainReason, AbstainResult
from rugbot.domain.observations import RawChainObservation
from rugbot.ingest.rpc_observer import (
    AddressHistoryCursor,
    observe_address,
)
from rugbot.storage.jsonl_observation_store import (
    JsonlObservationStore,
    observation_identity,
)

if TYPE_CHECKING:
    from pathlib import Path

    from rugbot.ingest.observation_pipeline import DurableObservationIngestor
    from rugbot.ingest.rpc_observer import RpcHttpTransport
    from rugbot.storage.handled_evidence_ledger import HandledEvidenceLedger

ObservationBatch: TypeAlias = tuple[RawChainObservation, ...]
ObservationReadResult: TypeAlias = ObservationBatch | AbstainResult
ObservationHandlerResult: TypeAlias = AbstainResult | None
ObservationHandlerFn: TypeAlias = Callable[
    [RawChainObservation], Awaitable[ObservationHandlerResult]
]


class _InvalidDurableCursorStateError(ValueError):
    """Raised when a durable transaction row cannot become a cursor."""


class _PendingBatchMismatchError(ValueError):
    """Raised when a source acknowledgement does not match its staged batch."""


class ObservationSource(Protocol):
    """One-shot source of raw observations for either runtime mode."""

    async def read(self) -> ObservationReadResult:
        """Read one bounded batch without interpreting protocol state."""


@runtime_checkable
class ObservationBatchAcknowledger(Protocol):
    """Optional source boundary for committing an accepted observation batch."""

    def acknowledge(self, batch: ObservationBatch) -> None:
        """Commit the exact batch most recently returned by ``read``."""


class ObservationHandler(Protocol):
    """Shared downstream handler used by online and offline observations."""

    async def handle(
        self,
        observation: RawChainObservation,
    ) -> ObservationHandlerResult:
        """Process one newly durable observation."""


@dataclass(slots=True)
class RpcAddressObservationSource:
    """Online finalized HTTP source for one address."""

    address: str
    endpoint: str
    source_id: str = "solana-http-rpc"
    observer_id: str = "rpc-observer"
    max_signatures: int = 20
    max_transactions: int = 5
    max_pages: int = 10
    cursor: AddressHistoryCursor | None = None
    raw_observation_path: Path | None = None
    handled_ledger: HandledEvidenceLedger | None = None
    transport: RpcHttpTransport | None = None
    _boot_id: UUID = field(default_factory=uuid4, init=False)
    _receive_sequence: int = field(default=0, init=False)
    _startup_abstention: AbstainResult | None = field(default=None, init=False)
    _pending_batch: _PendingOnlineBatch | None = field(default=None, init=False)

    def __post_init__(self) -> None:
        """Bind identity and restore only handled durable transaction evidence."""

        self.source_id = _address_bound_source_id(self.source_id, self.address)
        if (self.raw_observation_path is None) != (self.handled_ledger is None):
            self._startup_abstention = _abstain(
                AbstainReason.UNKNOWN_PROTOCOL_STATE,
                "durable source paths must be configured as a pair",
                as_of_slot=-1,
            )
            return

        if self.raw_observation_path is not None:
            try:
                restored_cursor = _restore_cursor(
                    path=self.raw_observation_path,
                    handled_ledger=self.handled_ledger,
                    address=self.address,
                    source_id=self.source_id,
                )
                if self.cursor is None:
                    self.cursor = restored_cursor
            except (OSError, UnicodeError, ValueError) as error:
                self._startup_abstention = _abstain(
                    AbstainReason.UNKNOWN_PROTOCOL_STATE,
                    f"durable cursor restoration failed: {type(error).__name__}",
                    as_of_slot=-1,
                )
        if self.cursor is not None:
            self._receive_sequence = self.cursor.receive_sequence

    async def read(self) -> ObservationReadResult:
        """Poll finalized transactions and stage the result until acknowledged."""

        if self._startup_abstention is not None:
            return self._startup_abstention
        if self._pending_batch is not None:
            return self._pending_batch.observations
        result = await observe_address(
            self.address,
            endpoint=self.endpoint,
            source_id=self.source_id,
            observer_id=self.observer_id,
            boot_id=self._boot_id,
            receive_sequence_start=self._receive_sequence,
            max_signatures=self.max_signatures,
            max_transactions=self.max_transactions,
            max_pages=self.max_pages,
            cursor=self.cursor,
            transport=self.transport,
        )
        if isinstance(result, AbstainResult):
            return result
        next_receive_sequence = self._receive_sequence + len(result)
        next_cursor = self.cursor
        if result:
            # The observer returns canonical observations in ascending order;
            # the cursor must point at the newest handled transaction.
            newest = max(
                result,
                key=lambda item: (
                    item.slot,
                    item.transaction_index
                    if item.transaction_index is not None
                    else -1,
                    item.receive_sequence,
                ),
            )
            signature = newest.signature
            if signature is None:
                return _abstain(
                    AbstainReason.UNKNOWN_PROTOCOL_STATE,
                    "online observation did not contain a transaction signature",
                    as_of_slot=result[-1].slot,
                )
            next_cursor = AddressHistoryCursor(
                address=self.address,
                source_id=self.source_id,
                until_signature=base58.b58encode(signature).decode("ascii"),
                receive_sequence=next_receive_sequence,
            )
        self._pending_batch = _PendingOnlineBatch(
            observations=result,
            cursor=next_cursor,
            receive_sequence=next_receive_sequence,
        )
        return result

    def acknowledge(self, batch: ObservationBatch) -> None:
        """Commit the exact staged batch after shared processing succeeds."""

        pending = self._pending_batch
        if pending is None or batch != pending.observations:
            raise _PendingBatchMismatchError
        self.cursor = pending.cursor
        self._receive_sequence = pending.receive_sequence
        self._pending_batch = None


def _restore_cursor(
    *,
    path: Path,
    handled_ledger: HandledEvidenceLedger | None,
    address: str,
    source_id: str,
) -> AddressHistoryCursor | None:
    """Restore the newest finalized transaction that was durably handled."""

    if handled_ledger is None:
        raise _InvalidDurableCursorStateError

    observations = JsonlObservationStore(path).read_all()
    candidates = [
        observation
        for observation in observations
        if (
            observation.source_id == source_id
            and observation.commitment == "finalized"
            and observation.canonical_status == "canonical"
            and observation.source_update_kind == "transaction"
        )
    ]
    handled_candidates = [
        observation
        for observation in candidates
        if handled_ledger.contains(observation_identity(observation))
    ]
    if not handled_candidates:
        return None

    newest = max(
        handled_candidates,
        key=lambda observation: (
            observation.slot,
            observation.transaction_index
            if observation.transaction_index is not None
            else -1,
            observation.receive_sequence,
        ),
    )
    if newest.signature is None:
        raise _InvalidDurableCursorStateError
    return AddressHistoryCursor(
        address=address,
        source_id=source_id,
        until_signature=base58.b58encode(newest.signature).decode("ascii"),
        receive_sequence=newest.receive_sequence,
    )


@dataclass(frozen=True, slots=True)
class JsonlReplayObservationSource:
    """Offline source that reads the same immutable raw observation format."""

    path: Path

    async def read(self) -> ObservationReadResult:
        """Read all durable observations through the canonical store decoder."""

        try:
            return tuple(JsonlObservationStore(self.path).read_all())
        except (OSError, UnicodeError, ValueError) as error:
            return _abstain(
                AbstainReason.UNKNOWN_PROTOCOL_STATE,
                f"offline observation source failed: {type(error).__name__}",
                as_of_slot=-1,
            )


@dataclass(frozen=True, slots=True)
class MemoryObservationSource:
    """Offline source for a preloaded immutable observation tuple."""

    observations: ObservationBatch

    async def read(self) -> ObservationReadResult:
        """Return the exact tuple supplied to the source."""

        return self.observations


@dataclass(frozen=True, slots=True)
class ObservationCycleReport:
    """Auditable result for one shared online or offline cycle."""

    as_of_slot: Slot
    observed_count: int
    persisted_count: int
    duplicate_count: int
    handled_count: int
    evidence_ids: tuple[str, ...]
    abstention: AbstainResult | None

    @property
    def accepted(self) -> bool:
        """Whether the cycle completed without an abstention."""

        return self.abstention is None


class SharedObservationLoop:
    """Run identical durable and downstream handling for every source mode."""

    def __init__(
        self,
        ingestor: DurableObservationIngestor,
        handled_evidence_ledger: HandledEvidenceLedger,
    ) -> None:
        """Initialize the loop with the required immutable-observation sink."""

        self._ingestor = ingestor
        self._handled_evidence_ledger = handled_evidence_ledger

    async def run_once(  # noqa: C901, PLR0911, PLR0912
        self,
        source: ObservationSource,
        handler: ObservationHandler | ObservationHandlerFn,
    ) -> ObservationCycleReport:
        """Read, durably append, deduplicate, order, and handle one batch.

        The source is the only online/offline difference. All observations use
        the same persistence, deduplication, ordering, and handler path.
        """

        try:
            result = await source.read()
        except Exception as error:  # noqa: BLE001
            return _abstained_report(
                AbstainReason.MISSING_FEATURE,
                f"observation source failed: {type(error).__name__}",
                as_of_slot=-1,
            )
        if isinstance(result, AbstainResult):
            return _abstained_report(
                result.reason,
                result.message,
                as_of_slot=result.as_of_slot,
            )
        validation_error = _validate_batch(result)
        if validation_error is not None:
            return _abstained_report(
                validation_error.reason,
                validation_error.message,
                as_of_slot=validation_error.as_of_slot,
            )

        ordered = tuple(sorted(result, key=_observation_order_key))
        persisted_count = 0
        duplicate_count = 0
        handled_count = 0
        evidence_ids: list[str] = []
        for observation in ordered:
            identity = observation_identity(observation)
            try:
                already_handled = self._handled_evidence_ledger.contains(identity)
            except Exception as error:  # noqa: BLE001
                return _partial_abstained_report(
                    as_of_slot=_batch_as_of_slot(ordered),
                    observed_count=len(ordered),
                    persisted_count=persisted_count,
                    duplicate_count=duplicate_count,
                    handled_count=handled_count,
                    evidence_ids=evidence_ids,
                    reason=AbstainReason.UNKNOWN_PROTOCOL_STATE,
                    message=f"handled-evidence ledger read failed: {type(error).__name__}",
                )
            if already_handled:
                duplicate_count += 1
                continue
            try:
                appended = self._ingestor.persist_observation(observation)
            except Exception as error:  # noqa: BLE001
                return _partial_abstained_report(
                    as_of_slot=_batch_as_of_slot(ordered),
                    observed_count=len(ordered),
                    persisted_count=persisted_count,
                    duplicate_count=duplicate_count,
                    handled_count=handled_count,
                    evidence_ids=evidence_ids,
                    reason=AbstainReason.UNKNOWN_PROTOCOL_STATE,
                    message=f"observation persistence failed: {type(error).__name__}",
                )
            if appended:
                persisted_count += 1
            evidence_ids.append(str(observation.raw_id))
            try:
                handled = await _handle(handler, observation)
            except Exception as error:  # noqa: BLE001
                return _partial_abstained_report(
                    as_of_slot=_batch_as_of_slot(ordered),
                    observed_count=len(ordered),
                    persisted_count=persisted_count,
                    duplicate_count=duplicate_count,
                    handled_count=handled_count,
                    evidence_ids=evidence_ids,
                    reason=AbstainReason.UNKNOWN_PROTOCOL_STATE,
                    message=f"observation handler failed: {type(error).__name__}",
                )
            if handled is not None:
                return _partial_abstained_report(
                    as_of_slot=_batch_as_of_slot(ordered),
                    observed_count=len(ordered),
                    persisted_count=persisted_count,
                    duplicate_count=duplicate_count,
                    handled_count=handled_count,
                    evidence_ids=evidence_ids,
                    reason=handled.reason,
                    message=handled.message,
                )
            try:
                recorded = self._handled_evidence_ledger.append(identity)
            except Exception as error:  # noqa: BLE001
                return _partial_abstained_report(
                    as_of_slot=_batch_as_of_slot(ordered),
                    observed_count=len(ordered),
                    persisted_count=persisted_count,
                    duplicate_count=duplicate_count,
                    handled_count=handled_count,
                    evidence_ids=evidence_ids,
                    reason=AbstainReason.UNKNOWN_PROTOCOL_STATE,
                    message=f"handled-evidence ledger append failed: {type(error).__name__}",
                )
            if not recorded:
                return _partial_abstained_report(
                    as_of_slot=_batch_as_of_slot(ordered),
                    observed_count=len(ordered),
                    persisted_count=persisted_count,
                    duplicate_count=duplicate_count,
                    handled_count=handled_count,
                    evidence_ids=evidence_ids,
                    reason=AbstainReason.UNKNOWN_PROTOCOL_STATE,
                    message="handled-evidence ledger identity was already recorded",
                )
            handled_count += 1

        try:
            _acknowledge_source(source, result)
        except Exception as error:  # noqa: BLE001
            return _partial_abstained_report(
                as_of_slot=_batch_as_of_slot(ordered),
                observed_count=len(ordered),
                persisted_count=persisted_count,
                duplicate_count=duplicate_count,
                handled_count=handled_count,
                evidence_ids=evidence_ids,
                reason=AbstainReason.UNKNOWN_PROTOCOL_STATE,
                message=f"observation source acknowledgement failed: {type(error).__name__}",
            )

        return ObservationCycleReport(
            as_of_slot=_batch_as_of_slot(ordered),
            observed_count=len(ordered),
            persisted_count=persisted_count,
            duplicate_count=duplicate_count,
            handled_count=handled_count,
            evidence_ids=tuple(evidence_ids),
            abstention=None,
        )


async def _handle(
    handler: ObservationHandler | ObservationHandlerFn,
    observation: RawChainObservation,
) -> ObservationHandlerResult:
    if callable(handler):
        return await handler(observation)
    return await handler.handle(observation)


def _acknowledge_source(
    source: ObservationSource,
    batch: ObservationBatch,
) -> None:
    if isinstance(source, ObservationBatchAcknowledger):
        source.acknowledge(batch)


def _validate_batch(batch: object) -> AbstainResult | None:
    if type(batch) is not tuple:
        return _abstain(
            AbstainReason.UNKNOWN_PROTOCOL_STATE,
            "observation source returned a malformed batch",
            as_of_slot=-1,
        )
    if any(type(observation) is not RawChainObservation for observation in batch):
        return _abstain(
            AbstainReason.UNKNOWN_PROTOCOL_STATE,
            "observation source returned malformed evidence",
            as_of_slot=-1,
        )
    if any(observation.slot < 0 for observation in batch):
        return _abstain(
            AbstainReason.UNKNOWN_PROTOCOL_STATE,
            "observation source returned a negative slot",
            as_of_slot=-1,
        )
    return None


def _observation_order_key(
    observation: RawChainObservation,
) -> tuple[int, bool, int, int, bytes]:
    transaction_index = observation.transaction_index
    return (
        observation.slot,
        transaction_index is None,
        transaction_index if transaction_index is not None else 0,
        observation.receive_sequence,
        observation.raw_id.bytes,
    )


def _batch_as_of_slot(batch: ObservationBatch) -> Slot:
    if not batch:
        return Slot(-1)
    return Slot(max(observation.slot for observation in batch))


def _abstained_report(
    reason: AbstainReason,
    message: str,
    *,
    as_of_slot: int,
) -> ObservationCycleReport:
    return ObservationCycleReport(
        as_of_slot=Slot(as_of_slot),
        observed_count=0,
        persisted_count=0,
        duplicate_count=0,
        handled_count=0,
        evidence_ids=(),
        abstention=_abstain(reason, message, as_of_slot=as_of_slot),
    )


def _partial_abstained_report(  # noqa: PLR0913
    *,
    as_of_slot: Slot,
    observed_count: int,
    persisted_count: int,
    duplicate_count: int,
    handled_count: int,
    evidence_ids: list[str],
    reason: AbstainReason,
    message: str,
) -> ObservationCycleReport:
    return ObservationCycleReport(
        as_of_slot=as_of_slot,
        observed_count=observed_count,
        persisted_count=persisted_count,
        duplicate_count=duplicate_count,
        handled_count=handled_count,
        evidence_ids=tuple(evidence_ids),
        abstention=_abstain(reason, message, as_of_slot=int(as_of_slot)),
    )


def _abstain(
    reason: AbstainReason,
    message: str,
    *,
    as_of_slot: int,
) -> AbstainResult:
    return AbstainResult(reason=reason, message=message, as_of_slot=as_of_slot)


__all__ = [
    "JsonlReplayObservationSource",
    "MemoryObservationSource",
    "ObservationBatch",
    "ObservationBatchAcknowledger",
    "ObservationCycleReport",
    "ObservationHandler",
    "ObservationHandlerFn",
    "ObservationReadResult",
    "ObservationSource",
    "RpcAddressObservationSource",
    "SharedObservationLoop",
]


@dataclass(frozen=True, slots=True)
class _PendingOnlineBatch:
    """Staged online state that becomes durable only after acknowledgement."""

    observations: ObservationBatch
    cursor: AddressHistoryCursor | None
    receive_sequence: int


def _address_bound_source_id(source_id: str, address: str) -> str:
    """Make HTTP source identity unambiguous when raw evidence lacks address."""

    suffix = f":{address}"
    return source_id if source_id.endswith(suffix) else f"{source_id}{suffix}"
