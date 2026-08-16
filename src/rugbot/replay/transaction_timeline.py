"""Canonical finalized transaction timeline construction for replay."""

import hashlib
from dataclasses import dataclass
from uuid import UUID

from rugbot.domain.amounts import Slot
from rugbot.domain.decisions import AbstainReason, AbstainResult
from rugbot.domain.observations import RawChainObservation

FINALIZED_TRANSACTION_TIMELINE_VERSION = "finalized-transaction-timeline-v1"


@dataclass(frozen=True, slots=True)
class FinalizedTransactionReplayEvent:
    """One finalized transaction event in deterministic replay order."""

    as_of_slot: Slot
    slot: Slot
    transaction_index: int
    signature: bytes
    raw_transaction: bytes
    raw_transaction_format: str
    raw_transaction_sha256: str
    program_id: bytes | None
    decoder_name: str | None
    decoder_version: str | None
    idl_hash: str | None
    source_raw_ids: tuple[UUID, ...]


@dataclass(frozen=True, slots=True)
class FinalizedTransactionTimeline:
    """Point-in-time finalized transaction timeline for replay consumers."""

    as_of_slot: Slot
    events: tuple[FinalizedTransactionReplayEvent, ...]
    source_observation_count: int
    deduped_observation_count: int
    timeline_version: str


TransactionTimelineResult = FinalizedTransactionTimeline | AbstainResult

_ReplayKey = tuple[int, int]
_EventIdentity = tuple[int, int, bytes]


def build_finalized_transaction_timeline(
    *,
    observations: tuple[RawChainObservation, ...],
    as_of_slot: Slot,
    timeline_version: str = FINALIZED_TRANSACTION_TIMELINE_VERSION,
) -> TransactionTimelineResult:
    """Build a deterministic finalized transaction timeline.

    Args:
        observations: Raw observations loaded from immutable storage.
        as_of_slot: Inclusive slot boundary for the replay view.
        timeline_version: Version of this timeline builder contract.

    Returns:
        A finalized transaction timeline, or an abstention when the source
        observations cannot prove a canonical replay order. This function is
        pure and performs no RPC or database access.
    """

    validation_error = _validate_timeline_request(as_of_slot, timeline_version)
    if validation_error is not None:
        return validation_error

    events_by_identity: dict[_EventIdentity, FinalizedTransactionReplayEvent] = {}
    replay_keys: dict[_ReplayKey, _EventIdentity] = {}

    for observation in observations:
        if _ignorable_observation(observation):
            continue
        event = _event_from_observation(observation, as_of_slot)
        if isinstance(event, AbstainResult):
            return event
        duplicate_error = _merge_event(
            events_by_identity=events_by_identity,
            replay_keys=replay_keys,
            event=event,
        )
        if duplicate_error is not None:
            return duplicate_error

    ordered_events = tuple(
        sorted(
            events_by_identity.values(),
            key=lambda event: (int(event.slot), event.transaction_index),
        )
    )
    return FinalizedTransactionTimeline(
        as_of_slot=as_of_slot,
        events=ordered_events,
        source_observation_count=len(observations),
        deduped_observation_count=len(ordered_events),
        timeline_version=timeline_version,
    )


def _ignorable_observation(observation: RawChainObservation) -> bool:
    return (
        observation.source_update_kind != "transaction"
        and observation.raw_transaction is None
    )


def _event_from_observation(
    observation: RawChainObservation,
    as_of_slot: Slot,
) -> FinalizedTransactionReplayEvent | AbstainResult:
    validation_error = _validate_transaction_observation(observation, as_of_slot)
    if validation_error is not None:
        return validation_error

    transaction_index = observation.transaction_index
    signature = observation.signature
    raw_transaction = observation.raw_transaction
    raw_transaction_format = observation.raw_transaction_format
    if (
        transaction_index is None
        or signature is None
        or raw_transaction is None
        or raw_transaction_format is None
    ):
        return _missing_transaction_evidence(as_of_slot)

    return FinalizedTransactionReplayEvent(
        as_of_slot=as_of_slot,
        slot=Slot(observation.slot),
        transaction_index=transaction_index,
        signature=signature,
        raw_transaction=raw_transaction,
        raw_transaction_format=raw_transaction_format,
        raw_transaction_sha256=hashlib.sha256(raw_transaction).hexdigest(),
        program_id=observation.program_id,
        decoder_name=observation.decoder_name,
        decoder_version=observation.decoder_version,
        idl_hash=observation.idl_hash,
        source_raw_ids=(observation.raw_id,),
    )


def _validate_transaction_observation(
    observation: RawChainObservation,
    as_of_slot: Slot,
) -> AbstainResult | None:
    for validation in (
        _validate_canonical_transaction_observation,
        _validate_transaction_update_kind,
        _validate_transaction_slot,
        _validate_transaction_required_evidence,
    ):
        validation_error = validation(observation, as_of_slot)
        if validation_error is not None:
            return validation_error
    return None


def _validate_canonical_transaction_observation(
    observation: RawChainObservation,
    as_of_slot: Slot,
) -> AbstainResult | None:
    if (
        observation.commitment != "finalized"
        or observation.canonical_status != "canonical"
    ):
        return AbstainResult(
            reason=AbstainReason.STALE_STATE,
            message="transaction timeline requires finalized canonical observations",
            as_of_slot=int(as_of_slot),
        )
    return None


def _validate_transaction_update_kind(
    observation: RawChainObservation,
    as_of_slot: Slot,
) -> AbstainResult | None:
    if observation.source_update_kind != "transaction":
        return AbstainResult(
            reason=AbstainReason.MISSING_FEATURE,
            message="transaction replay requires transaction observations",
            as_of_slot=int(as_of_slot),
        )
    return None


def _validate_transaction_slot(
    observation: RawChainObservation,
    as_of_slot: Slot,
) -> AbstainResult | None:
    if observation.slot < 0:
        return _unsupported("transaction slot must be non-negative", as_of_slot)
    if observation.slot > int(as_of_slot):
        return AbstainResult(
            reason=AbstainReason.STALE_STATE,
            message="transaction observation is newer than as_of_slot",
            as_of_slot=int(as_of_slot),
        )
    return None


def _validate_transaction_required_evidence(
    observation: RawChainObservation,
    as_of_slot: Slot,
) -> AbstainResult | None:
    if observation.transaction_index is None or observation.transaction_index < 0:
        return _missing_transaction_evidence(as_of_slot)
    if not observation.signature:
        return _missing_transaction_evidence(as_of_slot)
    if not observation.raw_transaction:
        return _missing_transaction_evidence(as_of_slot)
    if not observation.raw_transaction_format:
        return _missing_transaction_evidence(as_of_slot)
    return None


def _merge_event(
    *,
    events_by_identity: dict[_EventIdentity, FinalizedTransactionReplayEvent],
    replay_keys: dict[_ReplayKey, _EventIdentity],
    event: FinalizedTransactionReplayEvent,
) -> AbstainResult | None:
    identity = _event_identity(event)
    replay_key = _replay_key(event)

    existing_identity = replay_keys.get(replay_key)
    if existing_identity is not None and existing_identity != identity:
        return _unsupported(
            "conflicting finalized transaction identity for replay key",
            event.as_of_slot,
        )

    existing_event = events_by_identity.get(identity)
    if existing_event is None:
        events_by_identity[identity] = event
        replay_keys[replay_key] = identity
        return None

    if existing_event.raw_transaction_sha256 != event.raw_transaction_sha256:
        return _unsupported(
            "conflicting finalized transaction bytes for replay identity",
            event.as_of_slot,
        )
    if existing_event.raw_transaction_format != event.raw_transaction_format:
        return _unsupported(
            "conflicting finalized transaction format for replay identity",
            event.as_of_slot,
        )
    events_by_identity[identity] = _combine_duplicate_event(existing_event, event)
    return None


def _combine_duplicate_event(
    existing_event: FinalizedTransactionReplayEvent,
    duplicate_event: FinalizedTransactionReplayEvent,
) -> FinalizedTransactionReplayEvent:
    return FinalizedTransactionReplayEvent(
        as_of_slot=existing_event.as_of_slot,
        slot=existing_event.slot,
        transaction_index=existing_event.transaction_index,
        signature=existing_event.signature,
        raw_transaction=existing_event.raw_transaction,
        raw_transaction_format=existing_event.raw_transaction_format,
        raw_transaction_sha256=existing_event.raw_transaction_sha256,
        program_id=existing_event.program_id,
        decoder_name=existing_event.decoder_name,
        decoder_version=existing_event.decoder_version,
        idl_hash=existing_event.idl_hash,
        source_raw_ids=(
            *existing_event.source_raw_ids,
            *duplicate_event.source_raw_ids,
        ),
    )


def _validate_timeline_request(
    as_of_slot: Slot,
    timeline_version: str,
) -> AbstainResult | None:
    if int(as_of_slot) < 0:
        return _unsupported("as_of_slot must be non-negative", as_of_slot)
    if not timeline_version:
        return AbstainResult(
            reason=AbstainReason.DECODER_MISMATCH,
            message="timeline_version is required",
            as_of_slot=int(as_of_slot),
        )
    return None


def _event_identity(event: FinalizedTransactionReplayEvent) -> _EventIdentity:
    return int(event.slot), event.transaction_index, event.signature


def _replay_key(event: FinalizedTransactionReplayEvent) -> _ReplayKey:
    return int(event.slot), event.transaction_index


def _missing_transaction_evidence(as_of_slot: Slot) -> AbstainResult:
    return AbstainResult(
        reason=AbstainReason.MISSING_FEATURE,
        message=(
            "transaction replay requires index, signature, raw bytes, and raw "
            "byte format"
        ),
        as_of_slot=int(as_of_slot),
    )


def _unsupported(message: str, as_of_slot: Slot) -> AbstainResult:
    return AbstainResult(
        reason=AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
        message=message,
        as_of_slot=int(as_of_slot),
    )
