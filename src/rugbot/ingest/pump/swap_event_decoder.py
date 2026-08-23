"""Pure decoder for the pinned Pump AMM trade event layouts."""

# The field order is pinned to pump-public-docs/idl/pump_amm.json.  This
# module decodes bytes only; transaction/source validation belongs at ingest.
# ruff: noqa: PLR0913, PLR2004

from __future__ import annotations

from dataclasses import dataclass
from struct import unpack_from

import base58

from rugbot.domain.amounts import QuoteBaseUnits, Slot, TokenBaseUnits
from rugbot.domain.decisions import AbstainReason, AbstainResult
from rugbot.domain.trades import PumpSwapTradeEventEvidence, TradeSide

PUMP_AMM_BUY_EVENT_DISCRIMINATOR = bytes([103, 244, 82, 31, 44, 245, 119, 119])
PUMP_AMM_SELL_EVENT_DISCRIMINATOR = bytes([62, 47, 55, 10, 165, 3, 220, 42])
PUMP_AMM_EVENT_DISCRIMINATORS = frozenset(
    {PUMP_AMM_BUY_EVENT_DISCRIMINATOR, PUMP_AMM_SELL_EVENT_DISCRIMINATOR}
)
SOLANA_PUBKEY_BYTES = 32
EVENT_DISCRIMINATOR_BYTES = 8
SIGNATURE_BYTES = 64
U64_BYTES = 8
I64_BYTES = 8
STRING_LENGTH_BYTES = 4

SwapEventDecodeResult = PumpSwapTradeEventEvidence | AbstainResult


def decode_pump_swap_trade_event(
    payload: bytes,
    *,
    as_of_slot: Slot,
    signature: bytes,
    event_index: int,
) -> SwapEventDecodeResult:
    """Decode one exact Pump AMM ``BuyEvent`` or ``SellEvent`` payload."""

    validation = _validate_inputs(
        payload=payload,
        as_of_slot=as_of_slot,
        signature=signature,
        event_index=event_index,
    )
    if validation is not None:
        return validation
    reader = _EventReader(payload)
    discriminator = reader.read_bytes(8)
    if discriminator == PUMP_AMM_BUY_EVENT_DISCRIMINATOR:
        return _decode_buy(reader, payload, as_of_slot, signature, event_index)
    if discriminator == PUMP_AMM_SELL_EVENT_DISCRIMINATOR:
        return _decode_sell(reader, payload, as_of_slot, signature, event_index)
    return _abstain(
        AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
        "unsupported Pump AMM trade event discriminator",
        as_of_slot,
    )


def _decode_buy(
    reader: _EventReader,
    payload: bytes,
    as_of_slot: int,
    signature: bytes,
    event_index: int,
) -> SwapEventDecodeResult:
    timestamp = reader.read_i64()
    base_amount = reader.read_u64()
    reader.skip_u64()  # max_quote_amount_in
    reader.skip_u64(2)  # user reserves
    pool_base = reader.read_u64()
    pool_quote = reader.read_u64()
    quote_amount = reader.read_u64()
    lp_bps, lp_fee = reader.read_u64(), reader.read_u64()
    protocol_bps, protocol_fee = reader.read_u64(), reader.read_u64()
    reader.skip_u64()  # quote_amount_in_with_lp_fee
    user_quote = reader.read_u64()
    pool, user = _read_event_pubkeys(reader)
    creator_bps, creator_fee = reader.read_u64(), reader.read_u64()
    reader.skip_bool()
    reader.skip_u64(3)
    reader.skip_i64()
    reader.skip_u64()
    instruction_name = reader.read_string()
    reader.skip_u64(2)  # cashback bps and amount
    reader.skip_u64(2)  # buyback bps and amount
    virtual_quote = reader.read_i128()
    reader.skip_bool()
    reader.skip_u64()
    return _finish(
        reader=reader,
        payload=payload,
        as_of_slot=as_of_slot,
        signature=signature,
        event_index=event_index,
        side=TradeSide.BUY,
        timestamp=timestamp,
        pool=pool,
        user=user,
        base_amount=base_amount,
        quote_amount=quote_amount,
        user_quote=user_quote,
        pool_base=pool_base,
        pool_quote=pool_quote,
        virtual_quote=virtual_quote,
        lp_bps=lp_bps,
        lp_fee=lp_fee,
        protocol_bps=protocol_bps,
        protocol_fee=protocol_fee,
        creator_bps=creator_bps,
        creator_fee=creator_fee,
        instruction_name=instruction_name,
    )


def _decode_sell(
    reader: _EventReader,
    payload: bytes,
    as_of_slot: int,
    signature: bytes,
    event_index: int,
) -> SwapEventDecodeResult:
    timestamp = reader.read_i64()
    base_amount = reader.read_u64()
    reader.skip_u64()  # min_quote_amount_out
    reader.skip_u64(2)  # user reserves
    pool_base = reader.read_u64()
    pool_quote = reader.read_u64()
    quote_amount = reader.read_u64()
    lp_bps, lp_fee = reader.read_u64(), reader.read_u64()
    protocol_bps, protocol_fee = reader.read_u64(), reader.read_u64()
    reader.skip_u64()  # quote amount without LP fee
    user_quote = reader.read_u64()
    pool, user = _read_event_pubkeys(reader)
    creator_bps, creator_fee = reader.read_u64(), reader.read_u64()
    reader.skip_u64(2)  # cashback bps and amount
    reader.skip_u64(2)  # buyback bps and amount
    virtual_quote = reader.read_i128()
    reader.skip_bool()
    reader.skip_u64()
    return _finish(
        reader=reader,
        payload=payload,
        as_of_slot=as_of_slot,
        signature=signature,
        event_index=event_index,
        side=TradeSide.SELL,
        timestamp=timestamp,
        pool=pool,
        user=user,
        base_amount=base_amount,
        quote_amount=quote_amount,
        user_quote=user_quote,
        pool_base=pool_base,
        pool_quote=pool_quote,
        virtual_quote=virtual_quote,
        lp_bps=lp_bps,
        lp_fee=lp_fee,
        protocol_bps=protocol_bps,
        protocol_fee=protocol_fee,
        creator_bps=creator_bps,
        creator_fee=creator_fee,
        instruction_name="sell",
    )


def _read_event_pubkeys(reader: _EventReader) -> tuple[str, str]:
    pool = reader.read_pubkey()
    user = reader.read_pubkey()
    reader.skip_pubkey(2)
    reader.skip_pubkey(2)
    reader.skip_pubkey()
    return pool, user


def _finish(
    *,
    reader: _EventReader,
    payload: bytes,
    as_of_slot: int,
    signature: bytes,
    event_index: int,
    side: TradeSide,
    timestamp: int,
    pool: str,
    user: str,
    base_amount: int,
    quote_amount: int,
    user_quote: int,
    pool_base: int,
    pool_quote: int,
    virtual_quote: int,
    lp_bps: int,
    lp_fee: int,
    protocol_bps: int,
    protocol_fee: int,
    creator_bps: int,
    creator_fee: int,
    instruction_name: str,
) -> SwapEventDecodeResult:
    if reader.error is not None or reader.remaining != 0:
        return _abstain(
            AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
            "Pump AMM trade event layout is not exactly pinned",
            as_of_slot,
        )
    if any(
        value < 0
        for value in (
            timestamp,
            base_amount,
            quote_amount,
            user_quote,
            pool_base,
            pool_quote,
            virtual_quote,
            lp_bps,
            lp_fee,
            protocol_bps,
            protocol_fee,
            creator_bps,
            creator_fee,
        )
    ):
        return _abstain(
            AbstainReason.UNKNOWN_PROTOCOL_STATE,
            "Pump AMM trade event contains negative unsigned state",
            as_of_slot,
        )
    return PumpSwapTradeEventEvidence(
        as_of_slot=Slot(as_of_slot),
        signature=signature,
        event_index=event_index,
        side=side,
        timestamp=timestamp,
        pool=pool,
        user=user,
        base_amount_base_units=TokenBaseUnits(base_amount),
        quote_amount_base_units=QuoteBaseUnits(quote_amount),
        user_quote_amount_base_units=QuoteBaseUnits(user_quote),
        pool_base_reserves_base_units=TokenBaseUnits(pool_base),
        pool_quote_reserves_base_units=QuoteBaseUnits(pool_quote),
        virtual_quote_reserves_base_units=QuoteBaseUnits(virtual_quote),
        lp_fee_basis_points=lp_bps,
        lp_fee_base_units=QuoteBaseUnits(lp_fee),
        protocol_fee_basis_points=protocol_bps,
        protocol_fee_base_units=QuoteBaseUnits(protocol_fee),
        creator_fee_basis_points=creator_bps,
        creator_fee_base_units=QuoteBaseUnits(creator_fee),
        instruction_name=instruction_name,
        encoded_event=payload,
    )


def _validate_inputs(
    *, payload: object, as_of_slot: object, signature: object, event_index: object
) -> AbstainResult | None:
    if type(as_of_slot) is not int or as_of_slot < 0:
        return _abstain(
            AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
            "event slot must be a non-negative integer",
            -1,
        )
    if not isinstance(payload, bytes) or len(payload) < EVENT_DISCRIMINATOR_BYTES:
        return _abstain(
            AbstainReason.MISSING_FEATURE,
            "Pump AMM event payload is missing or truncated",
            as_of_slot,
        )
    if not isinstance(signature, bytes) or len(signature) != SIGNATURE_BYTES:
        return _abstain(
            AbstainReason.MISSING_FEATURE,
            "finalized transaction signature is required",
            as_of_slot,
        )
    if type(event_index) is not int or event_index < 0:
        return _abstain(
            AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
            "event index must be a non-negative integer",
            as_of_slot,
        )
    return None


@dataclass(slots=True)
class _EventReader:
    payload: bytes
    offset: int = 0
    error: str | None = None

    @property
    def remaining(self) -> int:
        return max(len(self.payload) - self.offset, 0)

    def read_bytes(self, size: int) -> bytes:
        if self.error is not None or size < 0 or self.remaining < size:
            self.error = "event payload is truncated"
            return b""
        value = self.payload[self.offset : self.offset + size]
        self.offset += size
        return value

    def read_u64(self) -> int:
        value = self.read_bytes(U64_BYTES)
        return unpack_from("<Q", value)[0] if len(value) == U64_BYTES else 0

    def read_i64(self) -> int:
        value = self.read_bytes(I64_BYTES)
        return unpack_from("<q", value)[0] if len(value) == I64_BYTES else 0

    def read_i128(self) -> int:
        value = self.read_bytes(16)
        return int.from_bytes(value, byteorder="little", signed=True) if value else 0

    def read_bool(self) -> bool:
        value = self.read_bytes(1)
        if len(value) != 1 or value[0] not in (0, 1):
            self.error = "event boolean is malformed"
            return False
        return bool(value[0])

    def read_pubkey(self) -> str:
        value = self.read_bytes(SOLANA_PUBKEY_BYTES)
        return base58.b58encode(value).decode("ascii") if len(value) == 32 else ""

    def read_string(self) -> str:
        length = self.read_bytes(STRING_LENGTH_BYTES)
        if len(length) != STRING_LENGTH_BYTES:
            return ""
        size = unpack_from("<I", length)[0]
        raw = self.read_bytes(size)
        if len(raw) != size:
            return ""
        try:
            return raw.decode("utf-8")
        except UnicodeDecodeError:
            self.error = "event string is not UTF-8"
            return ""

    def skip_u64(self, count: int = 1) -> None:
        self.read_bytes(U64_BYTES * count)

    def skip_i64(self) -> None:
        self.read_bytes(I64_BYTES)

    def skip_bool(self) -> None:
        self.read_bool()

    def skip_pubkey(self, count: int = 1) -> None:
        self.read_bytes(SOLANA_PUBKEY_BYTES * count)


def _abstain(reason: AbstainReason, message: str, as_of_slot: int) -> AbstainResult:
    return AbstainResult(reason=reason, message=message, as_of_slot=as_of_slot)


__all__ = [
    "PUMP_AMM_BUY_EVENT_DISCRIMINATOR",
    "PUMP_AMM_EVENT_DISCRIMINATORS",
    "PUMP_AMM_SELL_EVENT_DISCRIMINATOR",
    "SwapEventDecodeResult",
    "decode_pump_swap_trade_event",
]
