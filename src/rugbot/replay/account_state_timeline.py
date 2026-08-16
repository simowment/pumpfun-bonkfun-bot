"""Canonical finalized account-state timeline construction for replay."""

import hashlib
from dataclasses import dataclass
from uuid import UUID

from rugbot.domain.amounts import Slot
from rugbot.domain.decisions import AbstainReason, AbstainResult
from rugbot.domain.observations import RawChainObservation

FINALIZED_ACCOUNT_STATE_TIMELINE_VERSION = "finalized-account-state-timeline-v1"
SOLANA_PUBKEY_LENGTH = 32


@dataclass(frozen=True, slots=True)
class FinalizedAccountStateReplayEvent:
    """One finalized account-state update in deterministic replay order."""

    as_of_slot: Slot
    slot: Slot
    account_pubkey: bytes
    owner_program_id: bytes
    account_write_version: int
    raw_account_data: bytes
    raw_account_data_sha256: str
    decoder_name: str | None
    decoder_version: str | None
    idl_hash: str | None
    source_raw_ids: tuple[UUID, ...]


@dataclass(frozen=True, slots=True)
class FinalizedAccountStateTimeline:
    """Point-in-time finalized account-state timeline for replay consumers."""

    as_of_slot: Slot
    events: tuple[FinalizedAccountStateReplayEvent, ...]
    source_observation_count: int
    deduped_observation_count: int
    timeline_version: str


AccountStateTimelineResult = FinalizedAccountStateTimeline | AbstainResult

_EventIdentity = tuple[int, bytes, int]


def build_finalized_account_state_timeline(
    *,
    observations: tuple[RawChainObservation, ...],
    as_of_slot: Slot,
    timeline_version: str = FINALIZED_ACCOUNT_STATE_TIMELINE_VERSION,
) -> AccountStateTimelineResult:
    """Build a deterministic finalized account-state timeline.

    Args:
        observations: Raw observations loaded from immutable storage.
        as_of_slot: Inclusive slot boundary for the replay view.
        timeline_version: Version of this timeline builder contract.

    Returns:
        A finalized account-state timeline, or an abstention when the source
        observations cannot prove account identity, ownership, or update order.
        This function is pure and performs no RPC or database access.
    """

    validation_error = _validate_timeline_request(as_of_slot, timeline_version)
    if validation_error is not None:
        return validation_error

    events_by_identity: dict[_EventIdentity, FinalizedAccountStateReplayEvent] = {}

    for observation in observations:
        if _ignorable_observation(observation):
            continue
        event = _event_from_observation(observation, as_of_slot)
        if isinstance(event, AbstainResult):
            return event
        duplicate_error = _merge_event(
            events_by_identity=events_by_identity,
            event=event,
        )
        if duplicate_error is not None:
            return duplicate_error

    ordered_events = tuple(
        sorted(
            events_by_identity.values(),
            key=lambda event: (
                int(event.slot),
                event.account_write_version,
                event.account_pubkey,
            ),
        )
    )
    return FinalizedAccountStateTimeline(
        as_of_slot=as_of_slot,
        events=ordered_events,
        source_observation_count=len(observations),
        deduped_observation_count=len(ordered_events),
        timeline_version=timeline_version,
    )


def _ignorable_observation(observation: RawChainObservation) -> bool:
    return (
        observation.source_update_kind != "account"
        and observation.raw_account_data is None
    )


def _event_from_observation(
    observation: RawChainObservation,
    as_of_slot: Slot,
) -> FinalizedAccountStateReplayEvent | AbstainResult:
    validation_error = _validate_account_observation(observation, as_of_slot)
    if validation_error is not None:
        return validation_error

    account_pubkey = observation.account_pubkey
    owner_program_id = _account_owner_program_id(observation)
    account_write_version = observation.account_write_version
    raw_account_data = observation.raw_account_data
    if (
        account_pubkey is None
        or owner_program_id is None
        or account_write_version is None
        or raw_account_data is None
    ):
        return _missing_account_evidence(as_of_slot)

    return FinalizedAccountStateReplayEvent(
        as_of_slot=as_of_slot,
        slot=Slot(observation.slot),
        account_pubkey=account_pubkey,
        owner_program_id=owner_program_id,
        account_write_version=account_write_version,
        raw_account_data=raw_account_data,
        raw_account_data_sha256=hashlib.sha256(raw_account_data).hexdigest(),
        decoder_name=observation.decoder_name,
        decoder_version=observation.decoder_version,
        idl_hash=observation.idl_hash,
        source_raw_ids=(observation.raw_id,),
    )


def _validate_account_observation(
    observation: RawChainObservation,
    as_of_slot: Slot,
) -> AbstainResult | None:
    for validation in (
        _validate_canonical_account_observation,
        _validate_account_update_kind,
        _validate_account_slot,
        _validate_account_required_evidence,
    ):
        validation_error = validation(observation, as_of_slot)
        if validation_error is not None:
            return validation_error
    return None


def _validate_canonical_account_observation(
    observation: RawChainObservation,
    as_of_slot: Slot,
) -> AbstainResult | None:
    if (
        observation.commitment != "finalized"
        or observation.canonical_status != "canonical"
    ):
        return AbstainResult(
            reason=AbstainReason.STALE_STATE,
            message="account-state timeline requires finalized canonical observations",
            as_of_slot=int(as_of_slot),
        )
    return None


def _validate_account_update_kind(
    observation: RawChainObservation,
    as_of_slot: Slot,
) -> AbstainResult | None:
    if observation.source_update_kind != "account":
        return AbstainResult(
            reason=AbstainReason.MISSING_FEATURE,
            message="account-state replay requires account observations",
            as_of_slot=int(as_of_slot),
        )
    return None


def _validate_account_slot(
    observation: RawChainObservation,
    as_of_slot: Slot,
) -> AbstainResult | None:
    if observation.slot < 0:
        return _unsupported("account-state slot must be non-negative", as_of_slot)
    if observation.slot > int(as_of_slot):
        return AbstainResult(
            reason=AbstainReason.STALE_STATE,
            message="account-state observation is newer than as_of_slot",
            as_of_slot=int(as_of_slot),
        )
    return None


def _validate_account_required_evidence(
    observation: RawChainObservation,
    as_of_slot: Slot,
) -> AbstainResult | None:
    if not _is_solana_pubkey(observation.account_pubkey):
        return _missing_account_evidence(as_of_slot)
    if not _is_solana_pubkey(_account_owner_program_id(observation)):
        return _missing_account_evidence(as_of_slot)
    if (
        observation.account_write_version is None
        or observation.account_write_version < 0
    ):
        return _missing_account_evidence(as_of_slot)
    if observation.raw_account_data is None:
        return _missing_account_evidence(as_of_slot)
    return None


def _merge_event(
    *,
    events_by_identity: dict[_EventIdentity, FinalizedAccountStateReplayEvent],
    event: FinalizedAccountStateReplayEvent,
) -> AbstainResult | None:
    identity = _event_identity(event)

    existing_event = events_by_identity.get(identity)
    if existing_event is None:
        events_by_identity[identity] = event
        return None

    if existing_event.owner_program_id != event.owner_program_id:
        return _unsupported(
            "conflicting finalized account owner for replay identity",
            event.as_of_slot,
        )
    if existing_event.raw_account_data_sha256 != event.raw_account_data_sha256:
        return _unsupported(
            "conflicting finalized account bytes for replay identity",
            event.as_of_slot,
        )
    events_by_identity[identity] = _combine_duplicate_event(existing_event, event)
    return None


def _combine_duplicate_event(
    existing_event: FinalizedAccountStateReplayEvent,
    duplicate_event: FinalizedAccountStateReplayEvent,
) -> FinalizedAccountStateReplayEvent:
    return FinalizedAccountStateReplayEvent(
        as_of_slot=existing_event.as_of_slot,
        slot=existing_event.slot,
        account_pubkey=existing_event.account_pubkey,
        owner_program_id=existing_event.owner_program_id,
        account_write_version=existing_event.account_write_version,
        raw_account_data=existing_event.raw_account_data,
        raw_account_data_sha256=existing_event.raw_account_data_sha256,
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


def _event_identity(event: FinalizedAccountStateReplayEvent) -> _EventIdentity:
    return int(event.slot), event.account_pubkey, event.account_write_version


def _account_owner_program_id(observation: RawChainObservation) -> bytes | None:
    return observation.account_owner_program_id


def _is_solana_pubkey(value: bytes | None) -> bool:
    return value is not None and len(value) == SOLANA_PUBKEY_LENGTH


def _missing_account_evidence(as_of_slot: Slot) -> AbstainResult:
    return AbstainResult(
        reason=AbstainReason.MISSING_FEATURE,
        message=(
            "account-state replay requires account pubkey, owner, write "
            "version, and raw bytes"
        ),
        as_of_slot=int(as_of_slot),
    )


def _unsupported(message: str, as_of_slot: Slot) -> AbstainResult:
    return AbstainResult(
        reason=AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
        message=message,
        as_of_slot=int(as_of_slot),
    )
