"""Decode the pinned Pump create event emitted through CPI logs."""

# The event layout is deliberately explicit: partial decoding is unsafe here.
# ruff: noqa: PLR0911

import base64
import binascii
import hashlib
from collections.abc import Sequence
from dataclasses import dataclass
from struct import unpack_from

import base58

from rugbot.domain.amounts import QuoteBaseUnits, Slot, TokenBaseUnits
from rugbot.domain.decisions import AbstainReason, AbstainResult

CREATE_EVENT_DISCRIMINATOR = bytes.fromhex("1b72a94ddeeb6376")
CREATE_EVENT_MIN_DATA_SIZE = 262
SOL_PUBKEY = "11111111111111111111111111111111"
PUBKEY_SIZE = 32
U64_SIZE = 8


@dataclass(frozen=True, slots=True)
class PumpCreateEvent:
    """Pinned Pump create event evidence from one finalized transaction log."""

    as_of_slot: Slot
    log_index: int
    raw_data: bytes
    raw_data_sha256: str
    name: str
    symbol: str
    uri: str
    mint_pubkey: str
    bonding_curve_pubkey: str
    user_pubkey: str
    creator_pubkey: str
    timestamp: int
    virtual_token_reserves: TokenBaseUnits
    virtual_sol_reserves: QuoteBaseUnits
    real_token_reserves: TokenBaseUnits
    token_total_supply: TokenBaseUnits
    token_program_pubkey: str
    is_mayhem_mode: bool
    is_cashback_enabled: bool
    quote_mint_pubkey: str
    virtual_quote_reserves: QuoteBaseUnits


PumpCreateEventResult = PumpCreateEvent | AbstainResult | None


def decode_pump_create_event_logs(
    log_messages: Sequence[object],
    *,
    as_of_slot: Slot,
) -> PumpCreateEventResult:
    """Decode the single pinned Pump create event in CPI log evidence.

    Non-event program-data logs are ignored. A transaction containing more than
    one create event is ambiguous and therefore abstains.
    """

    if not isinstance(log_messages, Sequence) or isinstance(
        log_messages,
        (str, bytes, bytearray),
    ):
        return _abstain(
            "transaction log evidence is malformed",
            as_of_slot,
            AbstainReason.MISSING_FEATURE,
        )
    if type(as_of_slot) is not int or as_of_slot < 0:
        return _abstain(
            "create event requires a non-negative as_of_slot",
            as_of_slot,
            AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
        )

    matches: list[PumpCreateEvent] = []
    for log_index, log_message in enumerate(log_messages):
        if not isinstance(log_message, str):
            return _abstain(
                "transaction log message is malformed",
                as_of_slot,
                AbstainReason.UNKNOWN_PROTOCOL_STATE,
            )
        prefix = "Program data:"
        if not log_message.startswith(prefix):
            continue
        encoded = log_message[len(prefix) :].strip()
        try:
            raw_data = base64.b64decode(encoded, validate=True)
        except (binascii.Error, ValueError):
            return _abstain(
                "Pump program data log is not valid base64",
                as_of_slot,
                AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
            )
        if not raw_data.startswith(CREATE_EVENT_DISCRIMINATOR):
            continue
        decoded = _decode_event_data(
            raw_data,
            as_of_slot=as_of_slot,
            log_index=log_index,
        )
        if isinstance(decoded, AbstainResult):
            return decoded
        matches.append(decoded)

    if len(matches) > 1:
        return _abstain(
            "transaction contains multiple Pump create events",
            as_of_slot,
            AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
        )
    return matches[0] if matches else None


def _decode_event_data(  # noqa: C901, PLR0912
    raw_data: bytes,
    *,
    as_of_slot: Slot,
    log_index: int,
) -> PumpCreateEvent | AbstainResult:
    if len(raw_data) < CREATE_EVENT_MIN_DATA_SIZE:
        return _abstain(
            "Pump create event length does not match the pinned layout",
            as_of_slot,
            AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
        )

    offset = len(CREATE_EVENT_DISCRIMINATOR)
    name_result = _read_string(raw_data, offset)
    if name_result is None:
        return _malformed_event(as_of_slot, "name")
    name, offset = name_result
    symbol_result = _read_string(raw_data, offset)
    if symbol_result is None:
        return _malformed_event(as_of_slot, "symbol")
    symbol, offset = symbol_result
    uri_result = _read_string(raw_data, offset)
    if uri_result is None:
        return _malformed_event(as_of_slot, "uri")
    uri, offset = uri_result

    pubkeys: list[str] = []
    for field_name in (
        "mint",
        "bonding_curve",
        "user",
        "creator",
    ):
        pubkey_result = _read_pubkey(raw_data, offset)
        if pubkey_result is None:
            return _malformed_event(as_of_slot, field_name)
        pubkey, offset = pubkey_result
        pubkeys.append(pubkey)

    timestamp_result = _read_i64(raw_data, offset)
    if timestamp_result is None:
        return _malformed_event(as_of_slot, "timestamp")
    timestamp, offset = timestamp_result
    if timestamp < 0:
        return _abstain(
            "Pump create event timestamp is negative",
            as_of_slot,
            AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
        )

    reserve_values: list[int] = []
    for field_name in (
        "virtual_token_reserves",
        "virtual_sol_reserves",
        "real_token_reserves",
        "token_total_supply",
    ):
        reserve_result = _read_u64(raw_data, offset)
        if reserve_result is None:
            return _malformed_event(as_of_slot, field_name)
        reserve, offset = reserve_result
        reserve_values.append(reserve)

    token_program_result = _read_pubkey(raw_data, offset)
    if token_program_result is None:
        return _malformed_event(as_of_slot, "token_program")
    token_program, offset = token_program_result
    mayhem_result = _read_bool(raw_data, offset)
    if mayhem_result is None:
        return _malformed_event(as_of_slot, "is_mayhem_mode")
    is_mayhem_mode, offset = mayhem_result
    cashback_result = _read_bool(raw_data, offset)
    if cashback_result is None:
        return _malformed_event(as_of_slot, "is_cashback_enabled")
    is_cashback_enabled, offset = cashback_result
    quote_mint_result = _read_pubkey(raw_data, offset)
    if quote_mint_result is None:
        return _malformed_event(as_of_slot, "quote_mint")
    quote_mint, offset = quote_mint_result
    virtual_quote_result = _read_u64(raw_data, offset)
    if virtual_quote_result is None:
        return _malformed_event(as_of_slot, "virtual_quote_reserves")
    virtual_quote_reserves, offset = virtual_quote_result

    if offset != len(raw_data):
        return _abstain(
            "Pump create event contains unsupported trailing data",
            as_of_slot,
            AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
        )

    return PumpCreateEvent(
        as_of_slot=as_of_slot,
        log_index=log_index,
        raw_data=raw_data,
        raw_data_sha256=hashlib.sha256(raw_data).hexdigest(),
        name=name,
        symbol=symbol,
        uri=uri,
        mint_pubkey=pubkeys[0],
        bonding_curve_pubkey=pubkeys[1],
        user_pubkey=pubkeys[2],
        creator_pubkey=pubkeys[3],
        timestamp=timestamp,
        virtual_token_reserves=TokenBaseUnits(reserve_values[0]),
        virtual_sol_reserves=QuoteBaseUnits(reserve_values[1]),
        real_token_reserves=TokenBaseUnits(reserve_values[2]),
        token_total_supply=TokenBaseUnits(reserve_values[3]),
        token_program_pubkey=token_program,
        is_mayhem_mode=is_mayhem_mode,
        is_cashback_enabled=is_cashback_enabled,
        quote_mint_pubkey=quote_mint,
        virtual_quote_reserves=QuoteBaseUnits(virtual_quote_reserves),
    )


def _read_string(data: bytes, offset: int) -> tuple[str, int] | None:
    if offset < 0 or offset + 4 > len(data):
        return None
    length = unpack_from("<I", data, offset)[0]
    value_start = offset + 4
    value_end = value_start + length
    if value_end > len(data):
        return None
    try:
        return data[value_start:value_end].decode("utf-8"), value_end
    except UnicodeDecodeError:
        return None


def _read_pubkey(data: bytes, offset: int) -> tuple[str, int] | None:
    value_end = offset + PUBKEY_SIZE
    if offset < 0 or value_end > len(data):
        return None
    return base58.b58encode(data[offset:value_end]).decode("ascii"), value_end


def _read_u64(data: bytes, offset: int) -> tuple[int, int] | None:
    value_end = offset + U64_SIZE
    if offset < 0 or value_end > len(data):
        return None
    return unpack_from("<Q", data, offset)[0], value_end


def _read_i64(data: bytes, offset: int) -> tuple[int, int] | None:
    value_end = offset + U64_SIZE
    if offset < 0 or value_end > len(data):
        return None
    return unpack_from("<q", data, offset)[0], value_end


def _read_bool(data: bytes, offset: int) -> tuple[bool, int] | None:
    if offset < 0 or offset >= len(data) or data[offset] not in (0, 1):
        return None
    return data[offset] == 1, offset + 1


def _malformed_event(as_of_slot: Slot, field_name: str) -> AbstainResult:
    return _abstain(
        f"Pump create event {field_name} field is malformed",
        as_of_slot,
        AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
    )


def _abstain(
    message: str,
    as_of_slot: Slot,
    reason: AbstainReason,
) -> AbstainResult:
    safe_slot = as_of_slot if type(as_of_slot) is int else -1
    return AbstainResult(reason=reason, message=message, as_of_slot=safe_slot)
