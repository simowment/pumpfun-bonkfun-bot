"""Decode Pump bonding-curve ``TradeEvent`` records from program logs."""

from __future__ import annotations

import base64
import binascii
from typing import TYPE_CHECKING

from rugbot.domain.decisions import AbstainReason, AbstainResult
from rugbot.domain.trades import PumpTradeEventProof
from rugbot.ingest.pump.swap_event_decoder import EventReader

if TYPE_CHECKING:
    from collections.abc import Iterator, Sequence

TRADE_EVENT_DISCRIMINATOR = bytes([189, 219, 127, 211, 78, 230, 97, 238])
PROGRAM_DATA_LOG_PREFIX = "Program data: "
SHAREHOLDER_ENTRY_BYTES = 32 + 2
# holder_rewards_bps: u64 + holder_rewards: u64 appended by the current program.
HOLDER_REWARDS_TAIL_BYTES = 8 + 8


def pump_trade_payloads(logs: Sequence[str]) -> Iterator[tuple[int, bytes]]:
    """Yield ``(log index, payload)`` for each Pump ``TradeEvent`` log record."""
    for index, line in enumerate(logs):
        if not line.startswith(PROGRAM_DATA_LOG_PREFIX):
            continue
        try:
            payload = base64.b64decode(
                line[len(PROGRAM_DATA_LOG_PREFIX) :], validate=True
            )
        except (binascii.Error, ValueError):
            continue
        if payload.startswith(TRADE_EVENT_DISCRIMINATOR):
            yield index, payload


def decode_pump_trade_event(
    payload: bytes,
    as_of_slot: int,
) -> PumpTradeEventProof | AbstainResult:
    """Decode one pinned Pump ``TradeEvent`` payload (pre- or post-upgrade)."""
    reader = _TradeEventReader(payload)
    if reader.read_bytes(8) != TRADE_EVENT_DISCRIMINATOR:
        return _abstain(
            AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
            "unexpected Pump trade event discriminator",
            as_of_slot,
        )
    mint = reader.read_pubkey()
    sol_amount = reader.read_u64()
    token_amount = reader.read_u64()
    is_buy = reader.read_bool()
    user = reader.read_pubkey()
    timestamp = reader.read_i64()
    virtual_sol_reserves = reader.read_u64()
    virtual_token_reserves = reader.read_u64()
    real_sol_reserves = reader.read_u64()
    real_token_reserves = reader.read_u64()
    reader.skip_pubkey()
    protocol_fee_basis_points = reader.read_u64()
    protocol_fee = reader.read_u64()
    reader.skip_pubkey()
    creator_fee_basis_points = reader.read_u64()
    creator_fee = reader.read_u64()
    reader.skip_bool()
    reader.skip_u64(3)
    reader.skip_i64()
    instruction_name = reader.read_string()
    mayhem_mode = reader.read_bool()
    reader.skip_u64()
    cashback = reader.read_u64()
    buyback_fee_basis_points = reader.read_u64()
    buyback_fee = reader.read_u64()
    shareholders = reader.read_shareholders()
    quote_mint = reader.read_pubkey()
    quote_amount = reader.read_u64()
    virtual_quote_reserves = reader.read_u64()
    real_quote_reserves = reader.read_u64()
    # The current program appends holder_rewards_bps: u64 and holder_rewards: u64.
    # Holder rewards are not part of the modeled fee set, so a non-zero value
    # abstains instead of silently understating trade costs.
    if reader.error is None and reader.remaining == HOLDER_REWARDS_TAIL_BYTES:
        reader.skip_u64()
        if reader.read_u64() != 0:
            return _abstain(
                AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
                "Pump trade event carries holder rewards, which are not modeled",
                as_of_slot,
            )
    if reader.error is not None or reader.remaining != 0:
        return _abstain(
            AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
            "Pump trade event layout is not exactly pinned",
            as_of_slot,
        )
    return PumpTradeEventProof(
        mint=mint,
        user=user,
        sol_amount_base_units=sol_amount,
        token_amount_base_units=token_amount,
        is_buy=is_buy,
        instruction_name=instruction_name,
        timestamp=timestamp,
        virtual_sol_reserves_base_units=virtual_sol_reserves,
        virtual_token_reserves_base_units=virtual_token_reserves,
        real_sol_reserves_base_units=real_sol_reserves,
        real_token_reserves_base_units=real_token_reserves,
        protocol_fee_base_units=protocol_fee,
        creator_fee_base_units=creator_fee,
        protocol_fee_basis_points=protocol_fee_basis_points,
        creator_fee_basis_points=creator_fee_basis_points,
        cashback_base_units=cashback,
        encoded_event=payload,
        buyback_fee_basis_points=buyback_fee_basis_points,
        buyback_fee_base_units=buyback_fee,
        shareholders=shareholders,
        quote_mint=quote_mint,
        quote_amount_base_units=quote_amount,
        virtual_quote_reserves_base_units=virtual_quote_reserves,
        real_quote_reserves_base_units=real_quote_reserves,
        mayhem_mode=mayhem_mode,
    )


class _TradeEventReader(EventReader):
    """Pump bonding-curve trade event cursor.

    Adds only the Mayhem shareholder vector to the canonical bounded reader;
    every primitive read is inherited unchanged from ``EventReader``.
    """

    def read_shareholders(self) -> tuple[tuple[str, int], ...]:
        count = self.read_u32()
        if self.error is not None:
            return ()
        if count > self.remaining // SHAREHOLDER_ENTRY_BYTES:
            self.error = "shareholder vector is truncated"
            return ()
        return tuple((self.read_pubkey(), self.read_u16()) for _ in range(count))


def _abstain(reason: AbstainReason, message: str, as_of_slot: int) -> AbstainResult:
    return AbstainResult(reason=reason, message=message, as_of_slot=as_of_slot)
