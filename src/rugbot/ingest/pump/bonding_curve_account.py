"""Pure Pump bonding-curve account decoder for pinned IDL account evidence."""

import hashlib
from dataclasses import dataclass
from struct import unpack_from

from rugbot.domain.amounts import QuoteBaseUnits, Slot, TokenBaseUnits
from rugbot.domain.decisions import AbstainReason, AbstainResult
from rugbot.domain.pump_market_state import PumpBondingCurveAccountSnapshot
from rugbot.domain.quote_engine import PoolReserves
from rugbot.domain.version_registry import PumpProtocolVersionSnapshot

PUMP_PROGRAM_ID = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
PINNED_PUMP_IDL_SHA256 = (
    "b90bc471327f671449271d5d1d42354d1fae6f5a06502f5834459a3108138e49"
)
PUMP_BONDING_CURVE_ACCOUNT_DECODER_VERSION = "pump-bonding-curve-account-v1"
PUMP_BONDING_CURVE_LAYOUT_ARTIFACT_VERSION = "pump-bonding-curve-current-idl-layout-v1"
BONDING_CURVE_DISCRIMINATOR = bytes([23, 183, 248, 55, 96, 216, 172, 96])
DISCRIMINATOR_SIZE = 8
U64_SIZE = 8
PUBKEY_SIZE = 32
BOOL_FALSE = 0
BOOL_TRUE = 1
MAX_SUPPORTED_DECIMALS = 18
CURRENT_LAYOUT_SIZE = (
    DISCRIMINATOR_SIZE + (U64_SIZE * 5) + 1 + PUBKEY_SIZE + 1 + 1 + PUBKEY_SIZE
)


@dataclass(frozen=True, slots=True)
class PumpBondingCurveAccountState:
    """Raw finalized account-state evidence for a Pump bonding curve."""

    as_of_slot: Slot
    account_pubkey: str
    owner_program_id: str
    raw_account_data: bytes
    source_artifact_version: str
    layout_artifact_version: str


@dataclass(frozen=True, slots=True)
class PumpBondingCurveDecodeRequest:
    """Input contract for decoding one bonding-curve account snapshot."""

    account_state: PumpBondingCurveAccountState
    protocol_snapshot: PumpProtocolVersionSnapshot | None
    idl_hash: str
    base_decimals: int | None
    quote_decimals: int | None
    base_mint: str | None
    quote_mint: str | None
    decoder_version: str = PUMP_BONDING_CURVE_ACCOUNT_DECODER_VERSION


BondingCurveDecodeResult = PumpBondingCurveAccountSnapshot | AbstainResult
BondingCurveCreatorDecodeResult = bytes | AbstainResult
PoolReservesAdapterResult = PoolReserves | AbstainResult


def decode_pump_bonding_curve_creator(
    account_state: PumpBondingCurveAccountState,
) -> BondingCurveCreatorDecodeResult:
    """Decode the current creator without requiring quote-specific state."""

    provenance_error = _validate_account_state_provenance(account_state)
    if provenance_error is not None:
        return provenance_error
    layout_error = _validate_account_data_layout_from_state(account_state)
    if layout_error is not None:
        return layout_error
    creator = account_state.raw_account_data[49:81]
    if not any(creator):
        return _unknown_protocol(
            "bonding-curve creator is the zero public key", account_state.as_of_slot
        )
    return creator


def decode_pump_bonding_curve_account(
    request: PumpBondingCurveDecodeRequest,
) -> BondingCurveDecodeResult:
    """Decode one finalized Pump bonding-curve account snapshot.

    Args:
        request: Artifact-backed account bytes and point-in-time provenance.

    Returns:
        A decoded bonding-curve snapshot, or an abstention when the account
        state cannot be proven to match the pinned current layout. This function
        is pure and performs no RPC or database access.
    """

    validation_error = _validate_request(request)
    if validation_error is not None:
        return validation_error

    data = request.account_state.raw_account_data
    return PumpBondingCurveAccountSnapshot(
        as_of_slot=request.account_state.as_of_slot,
        account_pubkey=request.account_state.account_pubkey,
        owner_program_id=request.account_state.owner_program_id,
        virtual_token_reserves=TokenBaseUnits(_u64_at(data, 8)),
        virtual_sol_reserves=QuoteBaseUnits(_u64_at(data, 16)),
        real_token_reserves=TokenBaseUnits(_u64_at(data, 24)),
        real_sol_reserves=QuoteBaseUnits(_u64_at(data, 32)),
        token_total_supply=TokenBaseUnits(_u64_at(data, 40)),
        complete=_bool_at(data, 48),
        creator=data[49:81],
        is_mayhem_mode=_bool_at(data, 81),
        is_cashback_coin=_bool_at(data, 82),
        base_decimals=int(request.base_decimals),
        quote_decimals=int(request.quote_decimals),
        base_mint=request.base_mint,
        quote_mint=request.quote_mint,
        raw_account_data_sha256=hashlib.sha256(data).hexdigest(),
        account_data_length=len(data),
        trailing_zero_padding_length=max(0, len(data) - CURRENT_LAYOUT_SIZE),
        decoder_version=request.decoder_version,
        idl_hash=request.idl_hash,
        program_config_version=request.protocol_snapshot.program_config_version,
        layout_artifact_version=request.account_state.layout_artifact_version,
        source_artifact_version=request.account_state.source_artifact_version,
    )


def bonding_curve_snapshot_to_pool_reserves(
    snapshot: PumpBondingCurveAccountSnapshot,
) -> PoolReservesAdapterResult:
    """Adapt a decoded Pump bonding-curve account snapshot for quote engines."""

    validation_error = _validate_snapshot_for_reserves(snapshot)
    if validation_error is not None:
        return validation_error

    return PoolReserves(
        virtual_base_reserves=snapshot.virtual_token_reserves,
        virtual_quote_reserves=snapshot.virtual_sol_reserves,
        real_base_reserves=snapshot.real_token_reserves,
        real_quote_reserves=snapshot.real_sol_reserves,
        is_complete=snapshot.complete,
        as_of_slot=snapshot.as_of_slot,
        base_decimals=snapshot.base_decimals,
        quote_decimals=snapshot.quote_decimals,
        decoder_version=snapshot.decoder_version,
        idl_hash=snapshot.idl_hash,
        program_config_version=snapshot.program_config_version,
    )


def _validate_request(
    request: PumpBondingCurveDecodeRequest,
) -> AbstainResult | None:
    for validation in (
        _validate_account_state_provenance,
        _validate_protocol_provenance,
        _validate_decimal_and_mint_provenance,
        _validate_account_data_layout,
    ):
        validation_error = validation(request)
        if validation_error is not None:
            return validation_error
    return None


def _validate_account_state_provenance(
    request: PumpBondingCurveDecodeRequest | PumpBondingCurveAccountState,
) -> AbstainResult | None:
    state = (
        request.account_state
        if isinstance(request, PumpBondingCurveDecodeRequest)
        else request
    )
    if int(state.as_of_slot) < 0:
        return _unsupported("as_of_slot must be non-negative", state.as_of_slot)
    if not state.account_pubkey:
        return _unknown_protocol("account_pubkey is required", state.as_of_slot)
    if state.owner_program_id != PUMP_PROGRAM_ID:
        return _unknown_protocol(
            "bonding-curve account owner is not the pinned Pump program",
            state.as_of_slot,
        )
    if not state.source_artifact_version:
        return _unknown_protocol(
            "source_artifact_version is required",
            state.as_of_slot,
        )
    if state.layout_artifact_version != PUMP_BONDING_CURVE_LAYOUT_ARTIFACT_VERSION:
        return _unknown_protocol(
            "unsupported bonding-curve layout artifact version",
            state.as_of_slot,
        )
    return None


def _validate_protocol_provenance(
    request: PumpBondingCurveDecodeRequest,
) -> AbstainResult | None:
    for validation in (
        _validate_protocol_snapshot_presence,
        _validate_protocol_snapshot_slot,
        _validate_protocol_snapshot_decoder,
        _validate_protocol_snapshot_program,
    ):
        validation_error = validation(request)
        if validation_error is not None:
            return validation_error
    return None


def _validate_protocol_snapshot_presence(
    request: PumpBondingCurveDecodeRequest,
) -> AbstainResult | None:
    if request.protocol_snapshot is None:
        return _unknown_protocol(
            "protocol snapshot is required",
            request.account_state.as_of_slot,
        )
    return None


def _validate_protocol_snapshot_slot(
    request: PumpBondingCurveDecodeRequest,
) -> AbstainResult | None:
    state = request.account_state
    protocol = request.protocol_snapshot
    if protocol is not None and int(protocol.as_of_slot) != int(state.as_of_slot):
        return AbstainResult(
            reason=AbstainReason.STALE_STATE,
            message="account state and protocol snapshot use different slots",
            as_of_slot=int(state.as_of_slot),
        )
    return None


def _validate_protocol_snapshot_decoder(
    request: PumpBondingCurveDecodeRequest,
) -> AbstainResult | None:
    state = request.account_state
    protocol = request.protocol_snapshot
    if request.decoder_version != PUMP_BONDING_CURVE_ACCOUNT_DECODER_VERSION:
        return _decoder_mismatch(
            "decoder_version does not match the pinned bonding-curve decoder",
            state.as_of_slot,
        )
    if request.idl_hash != PINNED_PUMP_IDL_SHA256:
        return _decoder_mismatch(
            "Pump IDL hash does not match pinned bonding-curve decoder",
            state.as_of_slot,
        )
    if protocol is not None and protocol.idl_hash != request.idl_hash:
        return _decoder_mismatch(
            "protocol snapshot IDL hash does not match decoder IDL hash",
            state.as_of_slot,
        )
    return None


def _validate_protocol_snapshot_program(
    request: PumpBondingCurveDecodeRequest,
) -> AbstainResult | None:
    state = request.account_state
    protocol = request.protocol_snapshot
    if protocol is None:
        return None
    if protocol.program_id != state.owner_program_id:
        return _unknown_protocol(
            "protocol snapshot program_id does not match account owner",
            state.as_of_slot,
        )
    if not protocol.program_config_version:
        return _unknown_protocol(
            "protocol snapshot program_config_version is required",
            state.as_of_slot,
        )
    return None


def _validate_decimal_and_mint_provenance(
    request: PumpBondingCurveDecodeRequest,
) -> AbstainResult | None:
    state = request.account_state
    if request.base_decimals is None or request.quote_decimals is None:
        return _unknown_protocol("token decimals are required", state.as_of_slot)
    if not _valid_decimal(request.base_decimals):
        return _unsupported("base_decimals are unsupported", state.as_of_slot)
    if not _valid_decimal(request.quote_decimals):
        return _unsupported("quote_decimals are unsupported", state.as_of_slot)
    if not _valid_mint(request.base_mint) or not _valid_mint(request.quote_mint):
        return _unknown_protocol("mint provenance is required", state.as_of_slot)
    return None


def _validate_account_data_layout(
    request: PumpBondingCurveDecodeRequest,
) -> AbstainResult | None:
    return _validate_account_data_layout_from_state(request.account_state)


def _validate_account_data_layout_from_state(
    state: PumpBondingCurveAccountState,
) -> AbstainResult | None:
    data = state.raw_account_data
    if len(data) < CURRENT_LAYOUT_SIZE:
        return _unsupported(
            "bonding-curve account data is shorter than pinned current layout",
            state.as_of_slot,
        )
    if data[:DISCRIMINATOR_SIZE] != BONDING_CURVE_DISCRIMINATOR:
        return _decoder_mismatch(
            "bonding-curve account discriminator mismatch",
            state.as_of_slot,
        )
    bool_error = _validate_bool_fields(data, state.as_of_slot)
    if bool_error is not None:
        return bool_error
    if any(byte != 0 for byte in data[CURRENT_LAYOUT_SIZE:]):
        return _unsupported(
            "nonzero trailing bonding-curve account bytes are unsupported",
            state.as_of_slot,
        )
    return None


def _validate_bool_fields(data: bytes, as_of_slot: Slot) -> AbstainResult | None:
    for offset in (48, 81, 82):
        if data[offset] not in (BOOL_FALSE, BOOL_TRUE):
            return _unsupported(
                "bonding-curve bool field is not a canonical bool",
                as_of_slot,
            )
    return None


def _validate_snapshot_for_reserves(
    snapshot: PumpBondingCurveAccountSnapshot,
) -> AbstainResult | None:
    for validation in (
        _validate_snapshot_account_provenance,
        _validate_snapshot_decoder_provenance,
        _validate_snapshot_market_provenance,
    ):
        validation_error = validation(snapshot)
        if validation_error is not None:
            return validation_error
    return None


def _validate_snapshot_account_provenance(
    snapshot: PumpBondingCurveAccountSnapshot,
) -> AbstainResult | None:
    if int(snapshot.as_of_slot) < 0:
        return _unsupported(
            "snapshot as_of_slot must be non-negative", snapshot.as_of_slot
        )
    if not snapshot.account_pubkey:
        return _unknown_protocol(
            "snapshot account_pubkey is required", snapshot.as_of_slot
        )
    if snapshot.owner_program_id != PUMP_PROGRAM_ID:
        return _unknown_protocol(
            "snapshot owner is not the pinned Pump program",
            snapshot.as_of_slot,
        )
    if snapshot.layout_artifact_version != PUMP_BONDING_CURVE_LAYOUT_ARTIFACT_VERSION:
        return _unknown_protocol(
            "snapshot layout artifact version is unsupported",
            snapshot.as_of_slot,
        )
    if not snapshot.source_artifact_version:
        return _unknown_protocol(
            "snapshot source_artifact_version is required",
            snapshot.as_of_slot,
        )
    return None


def _validate_snapshot_decoder_provenance(
    snapshot: PumpBondingCurveAccountSnapshot,
) -> AbstainResult | None:
    if snapshot.idl_hash != PINNED_PUMP_IDL_SHA256:
        return _decoder_mismatch(
            "snapshot IDL hash does not match pinned bonding-curve decoder",
            snapshot.as_of_slot,
        )
    if snapshot.decoder_version != PUMP_BONDING_CURVE_ACCOUNT_DECODER_VERSION:
        return _decoder_mismatch(
            "snapshot decoder version is unsupported",
            snapshot.as_of_slot,
        )
    if not snapshot.program_config_version:
        return _unknown_protocol(
            "snapshot program_config_version is required",
            snapshot.as_of_slot,
        )
    return None


def _validate_snapshot_market_provenance(
    snapshot: PumpBondingCurveAccountSnapshot,
) -> AbstainResult | None:
    for validation in (
        _validate_snapshot_decimals,
        _validate_snapshot_mints,
        _validate_snapshot_reserve_values,
        _validate_snapshot_bool_values,
        _validate_snapshot_creator,
        _validate_snapshot_account_shape,
    ):
        validation_error = validation(snapshot)
        if validation_error is not None:
            return validation_error
    return None


def _validate_snapshot_decimals(
    snapshot: PumpBondingCurveAccountSnapshot,
) -> AbstainResult | None:
    if not _valid_decimal(snapshot.base_decimals):
        return _unsupported(
            "snapshot base_decimals are unsupported", snapshot.as_of_slot
        )
    if not _valid_decimal(snapshot.quote_decimals):
        return _unsupported(
            "snapshot quote_decimals are unsupported",
            snapshot.as_of_slot,
        )
    return None


def _validate_snapshot_mints(
    snapshot: PumpBondingCurveAccountSnapshot,
) -> AbstainResult | None:
    if not _valid_mint(snapshot.base_mint) or not _valid_mint(snapshot.quote_mint):
        return _unknown_protocol(
            "snapshot mint provenance is required", snapshot.as_of_slot
        )
    return None


def _validate_snapshot_reserve_values(
    snapshot: PumpBondingCurveAccountSnapshot,
) -> AbstainResult | None:
    amount_fields = (
        ("virtual_token_reserves", snapshot.virtual_token_reserves),
        ("virtual_sol_reserves", snapshot.virtual_sol_reserves),
        ("real_token_reserves", snapshot.real_token_reserves),
        ("real_sol_reserves", snapshot.real_sol_reserves),
        ("token_total_supply", snapshot.token_total_supply),
    )
    for field_name, value in amount_fields:
        if type(value) is not int:
            return _unsupported(
                f"snapshot {field_name} must be an integer",
                snapshot.as_of_slot,
            )
        if value < 0:
            return _unsupported(
                f"snapshot {field_name} must be non-negative",
                snapshot.as_of_slot,
            )
    if (
        int(snapshot.virtual_token_reserves) <= 0
        or int(snapshot.virtual_sol_reserves) <= 0
    ):
        return _unsupported(
            "snapshot virtual reserves must be positive",
            snapshot.as_of_slot,
        )
    return None


def _validate_snapshot_bool_values(
    snapshot: PumpBondingCurveAccountSnapshot,
) -> AbstainResult | None:
    bool_fields = (
        ("complete", snapshot.complete),
        ("is_mayhem_mode", snapshot.is_mayhem_mode),
        ("is_cashback_coin", snapshot.is_cashback_coin),
    )
    for field_name, value in bool_fields:
        if type(value) is not bool:
            return _unsupported(
                f"snapshot {field_name} must be a bool",
                snapshot.as_of_slot,
            )
    return None


def _validate_snapshot_creator(
    snapshot: PumpBondingCurveAccountSnapshot,
) -> AbstainResult | None:
    if type(snapshot.creator) is not bytes or len(snapshot.creator) != PUBKEY_SIZE:
        return _unknown_protocol(
            "snapshot creator pubkey evidence is required",
            snapshot.as_of_slot,
        )
    return None


def _validate_snapshot_account_shape(
    snapshot: PumpBondingCurveAccountSnapshot,
) -> AbstainResult | None:
    if type(snapshot.account_data_length) is not int:
        return _unsupported(
            "snapshot account_data_length must be an integer",
            snapshot.as_of_slot,
        )
    if type(snapshot.trailing_zero_padding_length) is not int:
        return _unsupported(
            "snapshot trailing_zero_padding_length must be an integer",
            snapshot.as_of_slot,
        )
    if snapshot.account_data_length < CURRENT_LAYOUT_SIZE:
        return _unsupported(
            "snapshot account_data_length is shorter than pinned layout",
            snapshot.as_of_slot,
        )
    if snapshot.trailing_zero_padding_length != (
        snapshot.account_data_length - CURRENT_LAYOUT_SIZE
    ):
        return _unsupported(
            "snapshot trailing padding length is inconsistent",
            snapshot.as_of_slot,
        )
    if not snapshot.raw_account_data_sha256:
        return _unknown_protocol(
            "snapshot raw_account_data_sha256 is required",
            snapshot.as_of_slot,
        )
    return None


def _u64_at(data: bytes, offset: int) -> int:
    return int(unpack_from("<Q", data, offset)[0])


def _bool_at(data: bytes, offset: int) -> bool:
    return data[offset] == BOOL_TRUE


def _valid_decimal(value: object) -> bool:
    return type(value) is int and 0 <= value <= MAX_SUPPORTED_DECIMALS


def _valid_mint(value: object) -> bool:
    return type(value) is str and bool(value)


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


def _decoder_mismatch(message: str, as_of_slot: Slot) -> AbstainResult:
    return AbstainResult(
        reason=AbstainReason.DECODER_MISMATCH,
        message=message,
        as_of_slot=int(as_of_slot),
    )
