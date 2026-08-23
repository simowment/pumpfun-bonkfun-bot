"""Reconstruct the exact initial Pump curve state at a create instruction."""

# This reducer combines two independent proofs and fails closed on divergence.
# ruff: noqa: PLR0911

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from rugbot.domain.amounts import QuoteBaseUnits, Slot, TokenBaseUnits
from rugbot.domain.decisions import AbstainReason, AbstainResult
from rugbot.domain.launches import LaunchCreatedV2
from rugbot.ingest.pump.create_event_decoder import (
    CREATE_EVENT_DISCRIMINATOR,
    CREATE_EVENT_MIN_DATA_SIZE,
    SOL_PUBKEY,
    PumpCreateEvent,
)

SIGNATURE_LENGTH = 64


@dataclass(frozen=True, slots=True)
class PumpBondingCurveAccountSnapshot:
    """Decoded on-chain point-in-time bonding-curve snapshot."""

    as_of_slot: Slot
    mint_pubkey: str
    bonding_curve_pubkey: str
    virtual_token_reserves: TokenBaseUnits
    virtual_sol_reserves: QuoteBaseUnits
    real_token_reserves: TokenBaseUnits
    real_sol_reserves: QuoteBaseUnits
    token_total_supply: TokenBaseUnits
    complete: bool
    creator_pubkey: str | None = None
    quote_mint_pubkey: str | None = None
    token_program_pubkey: str | None = None
    is_mayhem_mode: bool = False
    is_cashback_enabled: bool = False


@dataclass(frozen=True, slots=True)
class PumpCreateReserveSnapshot:
    """Initial Pump bonding-curve reserves proven at the create instruction."""

    as_of_slot: Slot
    mint_pubkey: str
    bonding_curve_pubkey: str
    creator_pubkey: str
    quote_mint_pubkey: str
    token_program_pubkey: str
    virtual_token_reserves: TokenBaseUnits
    virtual_quote_reserves: QuoteBaseUnits
    real_token_reserves: TokenBaseUnits
    real_quote_reserves: QuoteBaseUnits
    token_total_supply: TokenBaseUnits
    complete: bool
    is_mayhem_mode: bool
    is_cashback_enabled: bool
    source_signature: bytes
    transaction_index: int
    outer_instruction_index: int
    event_log_index: int
    event_raw_data_sha256: str


@dataclass(frozen=True, slots=True)
class PumpCreateMarketState:
    """Launch identity and exact initial reserves from one finalized create."""

    launch: LaunchCreatedV2
    create_event: PumpCreateEvent
    reserves: PumpCreateReserveSnapshot

    @property
    def as_of_slot(self) -> Slot:
        """Return the slot at which the create state was observed."""

        return self.reserves.as_of_slot


PumpCreateMarketStateResult = PumpCreateMarketState | AbstainResult


def reconstruct_pump_create_market_state(
    *,
    launch: LaunchCreatedV2,
    create_event: PumpCreateEvent,
) -> PumpCreateMarketStateResult:
    """Reconstruct initial reserves from external create and CPI event proofs."""

    validation_error = _validate_inputs(launch, create_event)
    if validation_error is not None:
        return validation_error

    match_error = _validate_event_matches_launch(launch, create_event)
    if match_error is not None:
        return match_error

    reserve_error = _validate_initial_reserves(create_event)
    if reserve_error is not None:
        return reserve_error

    signature = launch.signature
    transaction_index = launch.transaction_index
    if signature is None or transaction_index is None:
        raise AssertionError
    reserves = PumpCreateReserveSnapshot(
        as_of_slot=create_event.as_of_slot,
        mint_pubkey=create_event.mint_pubkey,
        bonding_curve_pubkey=create_event.bonding_curve_pubkey,
        creator_pubkey=create_event.creator_pubkey,
        quote_mint_pubkey=create_event.quote_mint_pubkey,
        token_program_pubkey=create_event.token_program_pubkey,
        virtual_token_reserves=create_event.virtual_token_reserves,
        virtual_quote_reserves=create_event.virtual_quote_reserves,
        real_token_reserves=create_event.real_token_reserves,
        real_quote_reserves=QuoteBaseUnits(0),
        token_total_supply=create_event.token_total_supply,
        complete=False,
        is_mayhem_mode=create_event.is_mayhem_mode,
        is_cashback_enabled=create_event.is_cashback_enabled,
        source_signature=signature,
        transaction_index=transaction_index,
        outer_instruction_index=launch.outer_instruction_index,
        event_log_index=create_event.log_index,
        event_raw_data_sha256=create_event.raw_data_sha256,
    )
    return PumpCreateMarketState(
        launch=launch,
        create_event=create_event,
        reserves=reserves,
    )


def _validate_inputs(
    launch: LaunchCreatedV2,
    create_event: PumpCreateEvent,
) -> AbstainResult | None:
    if type(launch) is not LaunchCreatedV2 or type(create_event) is not PumpCreateEvent:
        return _abstain(
            "Pump create state inputs are malformed",
            -1,
            AbstainReason.UNKNOWN_PROTOCOL_STATE,
        )
    if type(launch.as_of_slot) is not int or launch.as_of_slot < 0:
        return _abstain(
            "external create as_of_slot is malformed",
            int(create_event.as_of_slot),
            AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
        )
    event_shape_error = _validate_event_shape(create_event)
    if event_shape_error is not None:
        return event_shape_error
    if int(launch.as_of_slot) != int(create_event.as_of_slot):
        return _abstain(
            "external create and CPI event use different slots",
            int(create_event.as_of_slot),
            AbstainReason.STALE_STATE,
        )
    if launch.signature is None or type(launch.signature) is not bytes:
        return _abstain(
            "external create signature evidence is missing",
            int(create_event.as_of_slot),
            AbstainReason.MISSING_FEATURE,
        )
    if len(launch.signature) != SIGNATURE_LENGTH:
        return _abstain(
            "external create signature evidence is malformed",
            int(create_event.as_of_slot),
            AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
        )
    if type(launch.transaction_index) is not int or launch.transaction_index < 0:
        return _abstain(
            "external create transaction index is missing",
            int(create_event.as_of_slot),
            AbstainReason.MISSING_FEATURE,
        )
    if (
        type(launch.outer_instruction_index) is not int
        or type(create_event.log_index) is not int
        or launch.outer_instruction_index < 0
        or create_event.log_index < 0
    ):
        return _abstain(
            "create evidence ordering is malformed",
            int(create_event.as_of_slot),
            AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
        )
    return None


def _validate_event_shape(event: PumpCreateEvent) -> AbstainResult | None:
    if type(event.as_of_slot) is not int or event.as_of_slot < 0:
        return _abstain(
            "CPI create event as_of_slot is malformed",
            -1,
            AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
        )
    if type(event.log_index) is not int or event.log_index < 0:
        return _abstain(
            "CPI create event log index is malformed",
            event.as_of_slot,
            AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
        )
    if (
        type(event.raw_data) is not bytes
        or len(event.raw_data) < CREATE_EVENT_MIN_DATA_SIZE
        or not event.raw_data.startswith(CREATE_EVENT_DISCRIMINATOR)
        or event.raw_data_sha256 != hashlib.sha256(event.raw_data).hexdigest()
    ):
        return _abstain(
            "CPI create event raw evidence is malformed",
            event.as_of_slot,
            AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
        )
    text_values = (
        event.name,
        event.symbol,
        event.uri,
        event.mint_pubkey,
        event.bonding_curve_pubkey,
        event.user_pubkey,
        event.creator_pubkey,
        event.token_program_pubkey,
        event.quote_mint_pubkey,
        event.raw_data_sha256,
    )
    if any(type(value) is not str or not value for value in text_values):
        return _abstain(
            "CPI create event text evidence is malformed",
            event.as_of_slot,
            AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
        )
    if type(event.timestamp) is not int or event.timestamp < 0:
        return _abstain(
            "CPI create event timestamp is malformed",
            event.as_of_slot,
            AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
        )
    if (
        type(event.is_mayhem_mode) is not bool
        or type(event.is_cashback_enabled) is not bool
    ):
        return _abstain(
            "CPI create event flags are malformed",
            event.as_of_slot,
            AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
        )
    return None


def _validate_event_matches_launch(
    launch: LaunchCreatedV2,
    create_event: PumpCreateEvent,
) -> AbstainResult | None:
    matches = (
        ("mint", launch.mint_pubkey, create_event.mint_pubkey),
        (
            "bonding curve",
            launch.bonding_curve_pubkey,
            create_event.bonding_curve_pubkey,
        ),
        ("user", launch.user_pubkey, create_event.user_pubkey),
        ("creator", launch.creator_pubkey, create_event.creator_pubkey),
        ("name", launch.name, create_event.name),
        ("symbol", launch.symbol, create_event.symbol),
        ("uri", launch.uri, create_event.uri),
        (
            "token program",
            launch.base_token_program_pubkey,
            create_event.token_program_pubkey,
        ),
        (
            "mayhem mode",
            launch.is_mayhem_mode,
            create_event.is_mayhem_mode,
        ),
        (
            "cashback flag",
            launch.is_cashback_enabled,
            create_event.is_cashback_enabled,
        ),
    )
    for field_name, external_value, event_value in matches:
        if external_value != event_value:
            return _abstain(
                f"Pump create {field_name} differs between external and CPI evidence",
                int(create_event.as_of_slot),
                AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
            )
    if create_event.quote_mint_pubkey != SOL_PUBKEY:
        return _abstain(
            "Pump create quote mint is not the pinned native-SOL state",
            int(create_event.as_of_slot),
            AbstainReason.UNKNOWN_PROTOCOL_STATE,
        )
    if create_event.virtual_quote_reserves != create_event.virtual_sol_reserves:
        return _abstain(
            "Pump create quote reserves differ from SOL reserves",
            int(create_event.as_of_slot),
            AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
        )
    return None


def _validate_initial_reserves(event: PumpCreateEvent) -> AbstainResult | None:
    values = (
        event.virtual_token_reserves,
        event.virtual_sol_reserves,
        event.real_token_reserves,
        event.token_total_supply,
    )
    if any(type(value) is not int or value <= 0 for value in values):
        return _abstain(
            "Pump create reserves must be positive integers",
            int(event.as_of_slot),
            AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
        )
    if event.real_token_reserves > event.token_total_supply:
        return _abstain(
            "Pump create real token reserves exceed total supply",
            int(event.as_of_slot),
            AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
        )
    if event.virtual_token_reserves < event.real_token_reserves:
        return _abstain(
            "Pump create virtual token reserves are below real reserves",
            int(event.as_of_slot),
            AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
        )
    return None


def _abstain(
    message: str,
    as_of_slot: int,
    reason: AbstainReason,
) -> AbstainResult:
    return AbstainResult(reason=reason, message=message, as_of_slot=as_of_slot)


__all__ = [
    "SIGNATURE_LENGTH",
    "PumpCreateMarketState",
    "PumpCreateMarketStateResult",
    "PumpCreateReserveSnapshot",
    "reconstruct_pump_create_market_state",
]
