"""Adapt exact Pump create reserves to the canonical quote contract."""

from dataclasses import dataclass

from rugbot.domain.amounts import Slot
from rugbot.domain.decisions import AbstainReason, AbstainResult
from rugbot.domain.fees import BASIS_POINTS_DENOMINATOR, FeeConfig
from rugbot.domain.pump_market_state import PumpCreateReserveSnapshot
from rugbot.domain.quote_engine import (
    CANONICAL_PUMP_PROGRAM_CONFIG_VERSION,
    MAX_SUPPORTED_DECIMALS,
    PoolReserves,
)
from rugbot.domain.version_registry import (
    PUMP_VERSION_REGISTRY_VERSION,
    PumpProtocolVersionSnapshot,
)
from rugbot.ingest.pump.bonding_curve_account import (
    PINNED_PUMP_IDL_SHA256,
    PUMP_BONDING_CURVE_ACCOUNT_DECODER_VERSION,
    PUMP_PROGRAM_ID,
)
from rugbot.ingest.pump.create_decoder import (
    PUMP_CREATE_V2_DECODER_VERSION,
    SPL_2022_PROGRAM_ID,
)
from rugbot.ingest.pump.create_event_decoder import SOL_PUBKEY

SIGNATURE_LENGTH = 64
SHA256_HEX_LENGTH = 64


@dataclass(frozen=True, slots=True)
class PumpCreateMintMetadataProof:
    """Finalized mint metadata required to quote one Pump create state."""

    as_of_slot: Slot
    base_mint_pubkey: str
    quote_mint_pubkey: str
    base_decimals: int
    quote_decimals: int
    source_artifact: str


CreateStateAdapterResult = PoolReserves | AbstainResult


def pump_create_snapshot_to_pool_reserves(
    snapshot: PumpCreateReserveSnapshot,
    *,
    protocol_snapshot: PumpProtocolVersionSnapshot | None,
    mint_metadata: PumpCreateMintMetadataProof | None,
    create_decoder_version: str | None,
    create_idl_hash: str | None,
) -> CreateStateAdapterResult:
    """Convert an exact create snapshot to quote reserves without any I/O.

    The create event does not itself prove mint decimals or the historical fee
    and program configuration. Those proofs must be supplied explicitly at the
    same slot. The function fails closed instead of assuming Pump defaults.
    """

    if type(snapshot) is not PumpCreateReserveSnapshot:
        return _abstain(
            reason=AbstainReason.MISSING_FEATURE,
            message="Pump create reserve snapshot is required",
            as_of_slot=-1,
        )

    snapshot_error = _validate_snapshot(snapshot)
    if snapshot_error is not None:
        return snapshot_error

    decoder_error = _validate_create_decoder_provenance(
        snapshot=snapshot,
        create_decoder_version=create_decoder_version,
        create_idl_hash=create_idl_hash,
    )
    if decoder_error is not None:
        return decoder_error

    protocol_error = _validate_protocol_snapshot(snapshot, protocol_snapshot)
    if protocol_error is not None:
        return protocol_error

    metadata_error = _validate_mint_metadata(snapshot, mint_metadata)
    if metadata_error is not None:
        return metadata_error

    if protocol_snapshot is None or mint_metadata is None:
        raise AssertionError

    return PoolReserves(
        virtual_base_reserves=snapshot.virtual_token_reserves,
        virtual_quote_reserves=snapshot.virtual_quote_reserves,
        real_base_reserves=snapshot.real_token_reserves,
        real_quote_reserves=snapshot.real_quote_reserves,
        is_complete=snapshot.complete,
        as_of_slot=snapshot.as_of_slot,
        base_decimals=mint_metadata.base_decimals,
        quote_decimals=mint_metadata.quote_decimals,
        decoder_version=PUMP_BONDING_CURVE_ACCOUNT_DECODER_VERSION,
        idl_hash=protocol_snapshot.idl_hash,
        program_config_version=protocol_snapshot.program_config_version,
    )


def _validate_snapshot(
    snapshot: PumpCreateReserveSnapshot,
) -> AbstainResult | None:
    as_of_slot = _safe_slot(snapshot.as_of_slot)
    if type(snapshot.as_of_slot) is not int or snapshot.as_of_slot < 0:
        return _unsupported("create snapshot slot is malformed", as_of_slot)

    for validation in (
        _validate_snapshot_identity,
        _validate_snapshot_reserves,
        _validate_snapshot_flags,
        _validate_snapshot_evidence,
    ):
        error = validation(snapshot)
        if error is not None:
            return error
    return None


def _validate_snapshot_identity(
    snapshot: PumpCreateReserveSnapshot,
) -> AbstainResult | None:
    as_of_slot = snapshot.as_of_slot

    identity_fields = (
        snapshot.mint_pubkey,
        snapshot.bonding_curve_pubkey,
        snapshot.creator_pubkey,
        snapshot.quote_mint_pubkey,
        snapshot.token_program_pubkey,
    )
    if any(type(value) is not str or not value for value in identity_fields):
        return _missing("create snapshot identity provenance is required", as_of_slot)
    if snapshot.quote_mint_pubkey != SOL_PUBKEY:
        return _unknown("create snapshot quote mint is not native SOL", as_of_slot)
    if snapshot.token_program_pubkey != SPL_2022_PROGRAM_ID:
        return _unknown(
            "create snapshot token program is not pinned SPL-2022", as_of_slot
        )
    return None


def _validate_snapshot_reserves(
    snapshot: PumpCreateReserveSnapshot,
) -> AbstainResult | None:
    as_of_slot = snapshot.as_of_slot

    reserve_fields = (
        snapshot.virtual_token_reserves,
        snapshot.virtual_quote_reserves,
        snapshot.real_token_reserves,
        snapshot.real_quote_reserves,
        snapshot.token_total_supply,
    )
    if any(type(value) is not int for value in reserve_fields):
        return _unsupported("create snapshot reserves must be integers", as_of_slot)
    if (
        snapshot.virtual_token_reserves <= 0
        or snapshot.virtual_quote_reserves <= 0
        or snapshot.real_token_reserves < 0
        or snapshot.real_quote_reserves != 0
        or snapshot.token_total_supply <= 0
    ):
        return _unsupported(
            "create snapshot reserve values are unsupported", as_of_slot
        )
    if snapshot.real_token_reserves > snapshot.token_total_supply:
        return _unsupported("real token reserves exceed total supply", as_of_slot)
    if snapshot.virtual_token_reserves < snapshot.real_token_reserves:
        return _unsupported(
            "virtual token reserves are below real reserves", as_of_slot
        )
    return None


def _validate_snapshot_flags(
    snapshot: PumpCreateReserveSnapshot,
) -> AbstainResult | None:
    as_of_slot = snapshot.as_of_slot

    bool_fields = (
        snapshot.complete,
        snapshot.is_mayhem_mode,
        snapshot.is_cashback_enabled,
    )
    if any(type(value) is not bool for value in bool_fields):
        return _unsupported("create snapshot flags must be booleans", as_of_slot)
    if snapshot.complete:
        return _unsupported(
            "create snapshot bonding curve is already complete", as_of_slot
        )
    return None


def _validate_snapshot_evidence(
    snapshot: PumpCreateReserveSnapshot,
) -> AbstainResult | None:
    as_of_slot = snapshot.as_of_slot

    if (
        type(snapshot.source_signature) is not bytes
        or len(snapshot.source_signature) != SIGNATURE_LENGTH
    ):
        return _missing(
            "create transaction signature provenance is required", as_of_slot
        )
    index_fields = (
        snapshot.transaction_index,
        snapshot.outer_instruction_index,
        snapshot.event_log_index,
    )
    if any(type(value) is not int or value < 0 for value in index_fields):
        return _missing(
            "create transaction ordering provenance is required", as_of_slot
        )
    if not _valid_sha256(snapshot.event_raw_data_sha256):
        return _missing("create event raw-data hash provenance is required", as_of_slot)
    return None


def _validate_create_decoder_provenance(
    *,
    snapshot: PumpCreateReserveSnapshot,
    create_decoder_version: str | None,
    create_idl_hash: str | None,
) -> AbstainResult | None:
    if create_decoder_version is None or create_idl_hash is None:
        return _missing(
            "create decoder provenance is required",
            snapshot.as_of_slot,
        )
    if create_decoder_version != PUMP_CREATE_V2_DECODER_VERSION:
        return _decoder_mismatch(
            "create decoder version does not match pinned create_v2 decoder",
            snapshot.as_of_slot,
        )
    if create_idl_hash != PINNED_PUMP_IDL_SHA256:
        return _decoder_mismatch(
            "create IDL hash does not match pinned Pump IDL",
            snapshot.as_of_slot,
        )
    return None


def _validate_protocol_snapshot(
    snapshot: PumpCreateReserveSnapshot,
    protocol: PumpProtocolVersionSnapshot | None,
) -> AbstainResult | None:
    if protocol is None or type(protocol) is not PumpProtocolVersionSnapshot:
        return _missing("protocol snapshot is required", snapshot.as_of_slot)
    if (
        type(protocol.as_of_slot) is not int
        or protocol.as_of_slot != snapshot.as_of_slot
    ):
        return _stale("protocol snapshot uses a different slot", snapshot.as_of_slot)

    identity_error = _validate_protocol_identity(snapshot, protocol)
    if identity_error is not None:
        return identity_error
    artifact_error = _validate_protocol_artifacts(snapshot, protocol)
    if artifact_error is not None:
        return artifact_error
    return _validate_fee_config(snapshot, protocol.fee_config, protocol)


def _validate_protocol_identity(
    snapshot: PumpCreateReserveSnapshot,
    protocol: PumpProtocolVersionSnapshot,
) -> AbstainResult | None:
    if protocol.program_id != PUMP_PROGRAM_ID:
        return _unknown(
            "protocol snapshot is not for pinned Pump program", snapshot.as_of_slot
        )
    if protocol.idl_hash != PINNED_PUMP_IDL_SHA256:
        return _decoder_mismatch(
            "protocol snapshot IDL hash does not match pinned Pump IDL",
            snapshot.as_of_slot,
        )
    if protocol.program_config_version != CANONICAL_PUMP_PROGRAM_CONFIG_VERSION:
        return _unknown(
            "protocol snapshot config is not canonical", snapshot.as_of_slot
        )
    return None


def _validate_protocol_artifacts(
    snapshot: PumpCreateReserveSnapshot,
    protocol: PumpProtocolVersionSnapshot,
) -> AbstainResult | None:
    provenance_fields = (
        protocol.global_config_hash,
        protocol.program_config_source_artifact_version,
        protocol.fee_source_artifact_version,
    )
    if any(type(value) is not str or not value for value in provenance_fields):
        return _missing("protocol artifact provenance is required", snapshot.as_of_slot)
    if protocol.registry_version != PUMP_VERSION_REGISTRY_VERSION:
        return _unknown(
            "protocol registry provenance is unsupported", snapshot.as_of_slot
        )
    return None


def _validate_fee_config(
    snapshot: PumpCreateReserveSnapshot,
    fee_config: FeeConfig,
    protocol: PumpProtocolVersionSnapshot,
) -> AbstainResult | None:
    if type(fee_config) is not FeeConfig or not fee_config.is_known:
        return _missing("known fee configuration is required", snapshot.as_of_slot)

    provenance_error = _validate_fee_provenance(snapshot, fee_config, protocol)
    if provenance_error is not None:
        return provenance_error
    slot_error = _validate_fee_slot(snapshot, fee_config)
    if slot_error is not None:
        return slot_error
    return _validate_fee_amounts(snapshot, fee_config)


def _validate_fee_provenance(
    snapshot: PumpCreateReserveSnapshot,
    fee_config: FeeConfig,
    protocol: PumpProtocolVersionSnapshot,
) -> AbstainResult | None:
    if (
        type(fee_config.version) is not str
        or not fee_config.version
        or fee_config.program_config_version != protocol.program_config_version
        or fee_config.source_artifact_version != protocol.fee_source_artifact_version
    ):
        return _missing(
            "fee configuration provenance is incomplete", snapshot.as_of_slot
        )
    return None


def _validate_fee_slot(
    snapshot: PumpCreateReserveSnapshot,
    fee_config: FeeConfig,
) -> AbstainResult | None:
    if (
        type(fee_config.valid_from_slot) is not int
        or fee_config.valid_from_slot < 0
        or snapshot.as_of_slot < fee_config.valid_from_slot
    ):
        return _stale(
            "fee configuration is not valid at create slot", snapshot.as_of_slot
        )
    if fee_config.valid_to_slot is not None and (
        type(fee_config.valid_to_slot) is not int
        or fee_config.valid_to_slot <= fee_config.valid_from_slot
        or snapshot.as_of_slot >= fee_config.valid_to_slot
    ):
        return _stale(
            "fee configuration is not valid at create slot", snapshot.as_of_slot
        )
    return None


def _validate_fee_amounts(
    snapshot: PumpCreateReserveSnapshot,
    fee_config: FeeConfig,
) -> AbstainResult | None:
    fee_fields = (
        fee_config.protocol_fee_bps,
        fee_config.creator_fee_bps,
        fee_config.lp_fee_bps,
    )
    if any(type(value) is not int or value < 0 for value in fee_fields):
        return _unsupported("fee basis points are malformed", snapshot.as_of_slot)
    if fee_config.total_fee_bps >= BASIS_POINTS_DENOMINATOR:
        return _unsupported("Pump fee total is unsupported", snapshot.as_of_slot)
    return None


def _validate_mint_metadata(
    snapshot: PumpCreateReserveSnapshot,
    metadata: PumpCreateMintMetadataProof | None,
) -> AbstainResult | None:
    if metadata is None or type(metadata) is not PumpCreateMintMetadataProof:
        return _missing(
            "finalized mint metadata proof is required", snapshot.as_of_slot
        )
    if (
        type(metadata.as_of_slot) is not int
        or metadata.as_of_slot != snapshot.as_of_slot
    ):
        return _stale("mint metadata uses a different slot", snapshot.as_of_slot)
    if (
        metadata.base_mint_pubkey != snapshot.mint_pubkey
        or metadata.quote_mint_pubkey != snapshot.quote_mint_pubkey
    ):
        return _unknown(
            "mint metadata does not match create snapshot", snapshot.as_of_slot
        )
    if not _valid_decimals(metadata.base_decimals) or not _valid_decimals(
        metadata.quote_decimals
    ):
        return _unsupported("mint decimals are unsupported", snapshot.as_of_slot)
    if type(metadata.source_artifact) is not str or not metadata.source_artifact:
        return _missing(
            "mint metadata artifact provenance is required", snapshot.as_of_slot
        )
    return None


def _valid_decimals(value: object) -> bool:
    return type(value) is int and 0 <= value <= MAX_SUPPORTED_DECIMALS


def _valid_sha256(value: object) -> bool:
    if type(value) is not str or len(value) != SHA256_HEX_LENGTH:
        return False
    return all(character in "0123456789abcdef" for character in value)


def _safe_slot(value: object) -> int:
    return value if type(value) is int else -1


def _abstain(*, reason: AbstainReason, message: str, as_of_slot: int) -> AbstainResult:
    return AbstainResult(reason=reason, message=message, as_of_slot=as_of_slot)


def _missing(message: str, as_of_slot: int) -> AbstainResult:
    return _abstain(
        reason=AbstainReason.MISSING_FEATURE,
        message=message,
        as_of_slot=as_of_slot,
    )


def _unsupported(message: str, as_of_slot: int) -> AbstainResult:
    return _abstain(
        reason=AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
        message=message,
        as_of_slot=as_of_slot,
    )


def _unknown(message: str, as_of_slot: int) -> AbstainResult:
    return _abstain(
        reason=AbstainReason.UNKNOWN_PROTOCOL_STATE,
        message=message,
        as_of_slot=as_of_slot,
    )


def _decoder_mismatch(message: str, as_of_slot: int) -> AbstainResult:
    return _abstain(
        reason=AbstainReason.DECODER_MISMATCH,
        message=message,
        as_of_slot=as_of_slot,
    )


def _stale(message: str, as_of_slot: int) -> AbstainResult:
    return _abstain(
        reason=AbstainReason.STALE_STATE,
        message=message,
        as_of_slot=as_of_slot,
    )
