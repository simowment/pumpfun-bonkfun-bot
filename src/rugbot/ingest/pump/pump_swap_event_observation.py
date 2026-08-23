"""Decode Pump AMM trade events from immutable finalized observations."""

# The response envelope is validated field by field so malformed evidence
# cannot be silently converted into an empty event set.
# ruff: noqa: PLR0911

from __future__ import annotations

import base64
import json
from collections.abc import Mapping

import base58

from rugbot.domain.decisions import AbstainReason, AbstainResult
from rugbot.domain.observations import RawChainObservation
from rugbot.domain.trades import PumpSwapTradeEventEvidence
from rugbot.ingest.pump.swap_event_decoder import (
    PUMP_AMM_EVENT_DISCRIMINATORS,
    decode_pump_swap_trade_event,
)

PumpSwapEventObservationResult = tuple[PumpSwapTradeEventEvidence, ...] | AbstainResult


def decode_pump_swap_events_observation(
    observation: RawChainObservation,
) -> PumpSwapEventObservationResult:
    """Decode all pinned Pump AMM trade events in one finalized transaction."""

    validation = _validate_observation(observation)
    if validation is not None:
        return validation
    payload = _load_payload(observation)
    if isinstance(payload, AbstainResult):
        return payload
    logs = payload
    decoded: list[PumpSwapTradeEventEvidence] = []
    for message in logs:
        if not message.startswith("Program data: "):
            continue
        try:
            encoded = base64.b64decode(
                message.removeprefix("Program data: "), validate=True
            )
        except (ValueError, TypeError):
            continue
        if encoded[:8] not in PUMP_AMM_EVENT_DISCRIMINATORS:
            continue
        event = decode_pump_swap_trade_event(
            encoded,
            as_of_slot=observation.slot,
            signature=observation.signature or b"",
            event_index=len(decoded),
        )
        if isinstance(event, AbstainResult):
            return event
        decoded.append(event)
    return tuple(decoded)


def _validate_observation(observation: object) -> AbstainResult | None:
    if type(observation) is not RawChainObservation:
        return _abstain(
            AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
            "Pump AMM event observation is malformed",
            -1,
        )
    if (
        observation.commitment != "finalized"
        or observation.canonical_status != "canonical"
        or observation.source_update_kind != "transaction"
        or not isinstance(observation.raw_source_payload, bytes)
        or observation.signature is None
        or observation.transaction_index is None
    ):
        return _abstain(
            AbstainReason.STALE_STATE,
            "Pump AMM event decoder requires finalized transaction evidence",
            observation.slot,
        )
    return None


def _load_payload(observation: RawChainObservation) -> list[str] | AbstainResult:
    try:
        envelope = json.loads(observation.raw_source_payload or b"")
    except (UnicodeDecodeError, json.JSONDecodeError):
        return _abstain(
            AbstainReason.UNKNOWN_PROTOCOL_STATE,
            "Pump AMM event observation contains invalid JSON",
            observation.slot,
        )
    if not isinstance(envelope, Mapping) or envelope.get("jsonrpc") != "2.0":
        return _abstain(
            AbstainReason.UNKNOWN_PROTOCOL_STATE,
            "Pump AMM event response envelope is malformed",
            observation.slot,
        )
    result = envelope.get("result")
    if not isinstance(result, Mapping) or result.get("slot") != observation.slot:
        return _abstain(
            AbstainReason.STALE_STATE,
            "Pump AMM event payload slot does not match observation",
            observation.slot,
        )
    transaction = result.get("transaction")
    meta = result.get("meta")
    if not isinstance(transaction, Mapping) or not isinstance(meta, Mapping):
        return _abstain(
            AbstainReason.MISSING_FEATURE,
            "Pump AMM event transaction metadata is incomplete",
            observation.slot,
        )
    if meta.get("err") is not None:
        return _abstain(
            AbstainReason.UNKNOWN_PROTOCOL_STATE,
            "failed finalized transaction cannot produce a Pump AMM event",
            observation.slot,
        )
    signatures = transaction.get("signatures")
    expected = base58.b58encode(observation.signature or b"").decode("ascii")
    if not isinstance(signatures, list) or not signatures or signatures[0] != expected:
        return _abstain(
            AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
            "Pump AMM transaction signature does not match observation",
            observation.slot,
        )
    logs = meta.get("logMessages")
    if not isinstance(logs, list) or any(not isinstance(item, str) for item in logs):
        return _abstain(
            AbstainReason.MISSING_FEATURE,
            "Pump AMM event logs are missing",
            observation.slot,
        )
    return logs


def _abstain(reason: AbstainReason, message: str, as_of_slot: int) -> AbstainResult:
    return AbstainResult(reason=reason, message=message, as_of_slot=as_of_slot)


__all__ = [
    "PumpSwapEventObservationResult",
    "decode_pump_swap_events_observation",
]
