"""Pure Pump bonding-curve market-state reducer."""

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass
from uuid import UUID

import base58

from rugbot.domain.amounts import Slot
from rugbot.domain.decisions import AbstainReason, AbstainResult
from rugbot.domain.pump_market_state import PumpBondingCurveAccountSnapshot
from rugbot.domain.version_registry import (
    PumpProtocolVersionSnapshot,
)
from rugbot.ingest.pump.bonding_curve_account import (
    PINNED_PUMP_IDL_SHA256,
    PUMP_BONDING_CURVE_LAYOUT_ARTIFACT_VERSION,
    PumpBondingCurveAccountState,
    PumpBondingCurveDecodeRequest,
    decode_pump_bonding_curve_account,
)
from rugbot.replay.account_state_timeline import (
    FinalizedAccountStateReplayEvent,
    FinalizedAccountStateTimeline,
)

PUMP_BONDING_CURVE_REDUCER_VERSION = "pump-bonding-curve-reducer-v1"
SOLANA_PUBKEY_LENGTH = 32

AccountMetadataKey = tuple[bytes, int]


@dataclass(frozen=True, slots=True)
class PumpBondingCurveAccountMetadata:
    """Per-account/per-slot provenance needed to decode Pump curve state."""

    account_pubkey: bytes
    slot: Slot
    protocol_snapshot: PumpProtocolVersionSnapshot | None
    base_decimals: int | None
    quote_decimals: int | None
    base_mint: str | None
    quote_mint: str | None
    source_artifact_version: str
    idl_hash: str = PINNED_PUMP_IDL_SHA256
    layout_artifact_version: str = PUMP_BONDING_CURVE_LAYOUT_ARTIFACT_VERSION


@dataclass(frozen=True, slots=True)
class PumpBondingCurveMarketStateEvent:
    """Decoded Pump bonding-curve account update tied to replay evidence."""

    as_of_slot: Slot
    source_slot: Slot
    account_pubkey: bytes
    account_write_version: int
    snapshot: PumpBondingCurveAccountSnapshot
    source_raw_ids: tuple[UUID, ...]


@dataclass(frozen=True, slots=True)
class PumpBondingCurveMarketState:
    """Point-in-time Pump bonding-curve state derived from finalized accounts."""

    as_of_slot: Slot
    decoded_events: tuple[PumpBondingCurveMarketStateEvent, ...]
    latest_events: tuple[PumpBondingCurveMarketStateEvent, ...]
    source_event_count: int
    decoded_event_count: int
    reducer_version: str


PumpBondingCurveMarketStateResult = PumpBondingCurveMarketState | AbstainResult


def reduce_pump_bonding_curve_market_state(
    *,
    timeline: FinalizedAccountStateTimeline,
    metadata_by_event: Mapping[AccountMetadataKey, PumpBondingCurveAccountMetadata],
    reducer_version: str = PUMP_BONDING_CURVE_REDUCER_VERSION,
) -> PumpBondingCurveMarketStateResult:
    """Decode finalized Pump bonding-curve account updates into market state.

    Args:
        timeline: Finalized account-state replay timeline.
        metadata_by_event: Metadata keyed by `(account_pubkey, slot)`.
        reducer_version: Version of this reducer contract.

    Returns:
        Decoded market state, or an abstention when required point-in-time
        metadata or account evidence is missing or contradictory. This function
        is pure and performs no RPC or database access.
    """

    request_error = _validate_reducer_request(timeline, reducer_version)
    if request_error is not None:
        return request_error

    decoded_events: list[PumpBondingCurveMarketStateEvent] = []
    latest_by_account: dict[bytes, PumpBondingCurveMarketStateEvent] = {}

    for event in timeline.events:
        decoded_event = _decode_event(
            event=event,
            timeline_as_of_slot=timeline.as_of_slot,
            metadata_by_event=metadata_by_event,
        )
        if isinstance(decoded_event, AbstainResult):
            return decoded_event
        decoded_events.append(decoded_event)
        latest_by_account[event.account_pubkey] = decoded_event

    latest_events = tuple(
        latest_by_account[account_pubkey]
        for account_pubkey in sorted(latest_by_account)
    )
    return PumpBondingCurveMarketState(
        as_of_slot=timeline.as_of_slot,
        decoded_events=tuple(decoded_events),
        latest_events=latest_events,
        source_event_count=timeline.source_observation_count,
        decoded_event_count=len(decoded_events),
        reducer_version=reducer_version,
    )


def metadata_key_for_event(
    event: FinalizedAccountStateReplayEvent,
) -> AccountMetadataKey:
    """Build the required metadata lookup key for one account-state event."""

    return event.account_pubkey, int(event.slot)


def _decode_event(
    *,
    event: FinalizedAccountStateReplayEvent,
    timeline_as_of_slot: Slot,
    metadata_by_event: Mapping[AccountMetadataKey, PumpBondingCurveAccountMetadata],
) -> PumpBondingCurveMarketStateEvent | AbstainResult:
    event_error = _validate_event(event, timeline_as_of_slot)
    if event_error is not None:
        return event_error

    metadata = metadata_by_event.get(metadata_key_for_event(event))
    if metadata is None:
        return _unknown_protocol(
            "missing Pump bonding-curve metadata for account-state event",
            timeline_as_of_slot,
        )

    metadata_error = _validate_metadata(metadata, event)
    if metadata_error is not None:
        return metadata_error

    snapshot = decode_pump_bonding_curve_account(
        PumpBondingCurveDecodeRequest(
            account_state=PumpBondingCurveAccountState(
                as_of_slot=event.slot,
                account_pubkey=_pubkey_to_base58(event.account_pubkey),
                owner_program_id=_pubkey_to_base58(event.owner_program_id),
                raw_account_data=event.raw_account_data,
                source_artifact_version=metadata.source_artifact_version,
                layout_artifact_version=metadata.layout_artifact_version,
            ),
            protocol_snapshot=metadata.protocol_snapshot,
            idl_hash=metadata.idl_hash,
            base_decimals=metadata.base_decimals,
            quote_decimals=metadata.quote_decimals,
            base_mint=metadata.base_mint,
            quote_mint=metadata.quote_mint,
        )
    )
    if isinstance(snapshot, AbstainResult):
        return snapshot

    return PumpBondingCurveMarketStateEvent(
        as_of_slot=timeline_as_of_slot,
        source_slot=event.slot,
        account_pubkey=event.account_pubkey,
        account_write_version=event.account_write_version,
        snapshot=snapshot,
        source_raw_ids=event.source_raw_ids,
    )


def _validate_reducer_request(
    timeline: FinalizedAccountStateTimeline,
    reducer_version: str,
) -> AbstainResult | None:
    if int(timeline.as_of_slot) < 0:
        return _unsupported(
            "timeline as_of_slot must be non-negative", timeline.as_of_slot
        )
    if not timeline.timeline_version:
        return _decoder_mismatch("timeline version is required", timeline.as_of_slot)
    if not reducer_version:
        return _decoder_mismatch("reducer_version is required", timeline.as_of_slot)
    return None


def _validate_event(
    event: FinalizedAccountStateReplayEvent,
    timeline_as_of_slot: Slot,
) -> AbstainResult | None:
    for validation in (
        _validate_event_slots,
        _validate_event_pubkeys,
        _validate_event_write_version,
        _validate_event_raw_bytes,
        _validate_event_source_ids,
    ):
        validation_error = validation(event, timeline_as_of_slot)
        if validation_error is not None:
            return validation_error
    return None


def _validate_event_slots(
    event: FinalizedAccountStateReplayEvent,
    timeline_as_of_slot: Slot,
) -> AbstainResult | None:
    if int(event.slot) < 0:
        return _unsupported("account-state event slot must be non-negative", event.slot)
    if int(event.as_of_slot) != int(timeline_as_of_slot):
        return AbstainResult(
            reason=AbstainReason.STALE_STATE,
            message="account-state event as_of_slot differs from timeline",
            as_of_slot=int(timeline_as_of_slot),
        )
    if int(event.slot) > int(timeline_as_of_slot):
        return AbstainResult(
            reason=AbstainReason.STALE_STATE,
            message="account-state event is newer than timeline",
            as_of_slot=int(timeline_as_of_slot),
        )
    return None


def _validate_event_pubkeys(
    event: FinalizedAccountStateReplayEvent,
    timeline_as_of_slot: Slot,
) -> AbstainResult | None:
    if not _is_pubkey(event.account_pubkey) or not _is_pubkey(event.owner_program_id):
        return _missing_account_evidence(timeline_as_of_slot)
    return None


def _validate_event_write_version(
    event: FinalizedAccountStateReplayEvent,
    timeline_as_of_slot: Slot,
) -> AbstainResult | None:
    if type(event.account_write_version) is not int or event.account_write_version < 0:
        return _missing_account_evidence(timeline_as_of_slot)
    return None


def _validate_event_raw_bytes(
    event: FinalizedAccountStateReplayEvent,
    timeline_as_of_slot: Slot,
) -> AbstainResult | None:
    if type(event.raw_account_data) is not bytes:
        return _missing_account_evidence(timeline_as_of_slot)
    expected_hash = hashlib.sha256(event.raw_account_data).hexdigest()
    if event.raw_account_data_sha256 != expected_hash:
        return _unsupported(
            "account-state event raw byte hash does not match raw bytes",
            timeline_as_of_slot,
        )
    return None


def _validate_event_source_ids(
    event: FinalizedAccountStateReplayEvent,
    timeline_as_of_slot: Slot,
) -> AbstainResult | None:
    if not event.source_raw_ids or any(
        type(raw_id) is not UUID for raw_id in event.source_raw_ids
    ):
        return _missing_account_evidence(timeline_as_of_slot)
    return None


def _validate_metadata(
    metadata: PumpBondingCurveAccountMetadata,
    event: FinalizedAccountStateReplayEvent,
) -> AbstainResult | None:
    if metadata.account_pubkey != event.account_pubkey:
        return _unknown_protocol(
            "metadata account_pubkey does not match account-state event",
            event.slot,
        )
    if int(metadata.slot) != int(event.slot):
        return AbstainResult(
            reason=AbstainReason.STALE_STATE,
            message="metadata slot does not match account-state event slot",
            as_of_slot=int(event.slot),
        )
    if metadata.protocol_snapshot is None:
        return _unknown_protocol(
            "protocol snapshot is required for account-state event",
            event.slot,
        )
    if int(metadata.protocol_snapshot.as_of_slot) != int(event.slot):
        return AbstainResult(
            reason=AbstainReason.STALE_STATE,
            message="protocol snapshot slot does not match account-state event slot",
            as_of_slot=int(event.slot),
        )
    if not metadata.source_artifact_version:
        return _unknown_protocol(
            "source_artifact_version is required for account-state event",
            event.slot,
        )
    return None


def _is_pubkey(value: object) -> bool:
    return type(value) is bytes and len(value) == SOLANA_PUBKEY_LENGTH


def _pubkey_to_base58(value: bytes) -> str:
    return base58.b58encode(value).decode("ascii")


def _unknown_protocol(message: str, as_of_slot: Slot) -> AbstainResult:
    return AbstainResult(
        reason=AbstainReason.UNKNOWN_PROTOCOL_STATE,
        message=message,
        as_of_slot=int(as_of_slot),
    )


def _unsupported(message: str, as_of_slot: Slot) -> AbstainResult:
    return AbstainResult(
        reason=AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
        message=message,
        as_of_slot=int(as_of_slot),
    )


def _missing_account_evidence(as_of_slot: Slot) -> AbstainResult:
    return AbstainResult(
        reason=AbstainReason.MISSING_FEATURE,
        message=(
            "account-state event requires valid account pubkey, owner, write "
            "version, raw bytes, raw byte hash, and source IDs"
        ),
        as_of_slot=int(as_of_slot),
    )


def _decoder_mismatch(message: str, as_of_slot: Slot) -> AbstainResult:
    return AbstainResult(
        reason=AbstainReason.DECODER_MISMATCH,
        message=message,
        as_of_slot=int(as_of_slot),
    )
