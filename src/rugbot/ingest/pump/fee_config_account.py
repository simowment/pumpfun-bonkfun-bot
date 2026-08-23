"""Strict decoder for the pinned Pump fee-config account layout."""

from dataclasses import dataclass
from typing import TypeAlias

import base58
from solders.pubkey import Pubkey

from rugbot.domain.amounts import Slot
from rugbot.domain.decisions import AbstainReason, AbstainResult
from rugbot.domain.fees import BASIS_POINTS_DENOMINATOR, FeeConfig
from rugbot.domain.observations import RawChainObservation
from rugbot.ingest.pump.bonding_curve_account import PUMP_PROGRAM_ID

PUMP_FEE_PROGRAM_ID = "pfeeUxB6jkeY1Hxd7CsFCAjcbHA9rWtchMGdZ6VojVZ"
FEE_CONFIG_SEED = b"fee_config"
FEE_CONFIG_DISCRIMINATOR = bytes([143, 52, 146, 187, 219, 123, 76, 155])

DISCRIMINATOR_SIZE = 8
U8_SIZE = 1
PUBKEY_SIZE = 32
U32_SIZE = 4
U64_SIZE = 8
U128_SIZE = 16
FEES_SIZE = U64_SIZE * 3
FEE_TIER_SIZE = U128_SIZE + FEES_SIZE
FEE_TIERS_LENGTH_OFFSET = DISCRIMINATOR_SIZE + U8_SIZE + PUBKEY_SIZE + FEES_SIZE
FEE_TIERS_OFFSET = FEE_TIERS_LENGTH_OFFSET + U32_SIZE

_FEE_PROGRAM_PUBKEY = Pubkey.from_string(PUMP_FEE_PROGRAM_ID)
_PUMP_PROGRAM_PUBKEY = Pubkey.from_string(PUMP_PROGRAM_ID)
_FEE_CONFIG_PUBKEY, PUMP_FEE_CONFIG_BUMP = Pubkey.find_program_address(
    [FEE_CONFIG_SEED, bytes(_PUMP_PROGRAM_PUBKEY)],
    _FEE_PROGRAM_PUBKEY,
)
PUMP_FEE_CONFIG_PDA = str(_FEE_CONFIG_PUBKEY)
_FEE_CONFIG_ACCOUNT_BYTES = bytes(_FEE_CONFIG_PUBKEY)
_FEE_PROGRAM_BYTES = bytes(_FEE_PROGRAM_PUBKEY)


@dataclass(frozen=True, slots=True)
class PumpFeeTier:
    """One market-cap threshold and its immutable fee configuration."""

    market_cap_lamports_threshold: int
    fees: FeeConfig


@dataclass(frozen=True, slots=True)
class PumpFeeConfigAccount:
    """Decoded finalized state of the canonical Pump fee-config PDA."""

    as_of_slot: Slot
    account_pubkey: str
    owner_program_id: str
    bump: int
    admin_pubkey: str
    flat_fees: FeeConfig
    fee_tiers: tuple[PumpFeeTier, ...]
    stable_fee_tiers: tuple[PumpFeeTier, ...]


PumpFeeConfigDecodeResult: TypeAlias = PumpFeeConfigAccount | AbstainResult


def decode_pump_fee_config_account(  # noqa: PLR0911
    observation: RawChainObservation,
) -> PumpFeeConfigDecodeResult:
    """Decode one finalized canonical Pump FeeConfig account observation."""

    validation = _validate_observation(observation)
    if validation is not None:
        return validation

    data = observation.raw_account_data
    if len(data) < FEE_TIERS_OFFSET:
        return _unknown("Pump FeeConfig account is truncated", observation.slot)
    if data[:DISCRIMINATOR_SIZE] != FEE_CONFIG_DISCRIMINATOR:
        return _unknown(
            "Pump FeeConfig discriminator does not match the pinned IDL",
            observation.slot,
        )
    if data[DISCRIMINATOR_SIZE] != PUMP_FEE_CONFIG_BUMP:
        return _unknown(
            "Pump FeeConfig bump does not match the canonical PDA",
            observation.slot,
        )

    flat_fees = _decode_fees(data, DISCRIMINATOR_SIZE + U8_SIZE + PUBKEY_SIZE)
    fee_error = _validate_fees(flat_fees, observation.slot)
    if fee_error is not None:
        return fee_error

    fee_tiers, next_offset = _decode_fee_tiers(
        data, FEE_TIERS_LENGTH_OFFSET, observation.slot
    )
    if isinstance(fee_tiers, AbstainResult):
        return fee_tiers
    stable_result = _decode_fee_tiers(data, next_offset, observation.slot)
    if isinstance(stable_result, AbstainResult):
        return stable_result
    stable_fee_tiers, next_offset = stable_result
    if any(data[next_offset:]):
        return _unknown(
            "Pump FeeConfig account has non-zero trailing bytes", observation.slot
        )

    admin_start = DISCRIMINATOR_SIZE + U8_SIZE
    admin = data[admin_start : admin_start + PUBKEY_SIZE]
    return PumpFeeConfigAccount(
        as_of_slot=Slot(observation.slot),
        account_pubkey=PUMP_FEE_CONFIG_PDA,
        owner_program_id=PUMP_FEE_PROGRAM_ID,
        bump=PUMP_FEE_CONFIG_BUMP,
        admin_pubkey=base58.b58encode(admin).decode("ascii"),
        flat_fees=_fee_config(flat_fees, observation.slot),
        fee_tiers=fee_tiers,
        stable_fee_tiers=stable_fee_tiers,
    )


def _decode_fee_tiers(
    data: bytes,
    length_offset: int,
    as_of_slot: int,
) -> tuple[tuple[PumpFeeTier, ...], int] | AbstainResult:
    if len(data) < length_offset + U32_SIZE:
        return _unknown("Pump FeeConfig fee-tier vector is truncated", as_of_slot)
    tier_count = _integer_at(data, length_offset, U32_SIZE)
    vector_offset = length_offset + U32_SIZE
    expected_length = vector_offset + (tier_count * FEE_TIER_SIZE)
    if len(data) < expected_length:
        return _unknown("Pump FeeConfig fee-tier vector is truncated", as_of_slot)

    tiers: list[PumpFeeTier] = []
    for index in range(tier_count):
        offset = vector_offset + (index * FEE_TIER_SIZE)
        threshold = _integer_at(data, offset, U128_SIZE)
        fees = _decode_fees(data, offset + U128_SIZE)
        fee_error = _validate_fees(fees, as_of_slot)
        if fee_error is not None:
            return fee_error
        tiers.append(
            PumpFeeTier(
                market_cap_lamports_threshold=threshold,
                fees=_fee_config(fees, as_of_slot),
            )
        )
    return tuple(tiers), expected_length


def _validate_observation(observation: object) -> AbstainResult | None:
    if type(observation) is not RawChainObservation:
        return _missing("finalized Pump FeeConfig observation is required", -1)
    if (
        observation.commitment != "finalized"
        or observation.canonical_status != "canonical"
        or observation.source_update_kind != "account"
    ):
        return AbstainResult(
            reason=AbstainReason.STALE_STATE,
            message="Pump FeeConfig requires finalized canonical account evidence",
            as_of_slot=_safe_slot(observation.slot),
        )
    if (
        type(observation.slot) is not int
        or observation.slot < 0
        or type(observation.account_pubkey) is not bytes
        or type(observation.account_owner_program_id) is not bytes
        or type(observation.raw_account_data) is not bytes
    ):
        return _missing(
            "Pump FeeConfig account identity and raw bytes are required",
            _safe_slot(observation.slot),
        )
    if observation.account_pubkey != _FEE_CONFIG_ACCOUNT_BYTES:
        return _unknown(
            "account observation is not the canonical Pump FeeConfig PDA",
            observation.slot,
        )
    if observation.account_owner_program_id != _FEE_PROGRAM_BYTES:
        return _unknown(
            "Pump FeeConfig account is not owned by the pinned fee program",
            observation.slot,
        )
    return None


def _decode_fees(data: bytes, offset: int) -> tuple[int, int, int]:
    return (
        _integer_at(data, offset, U64_SIZE),
        _integer_at(data, offset + U64_SIZE, U64_SIZE),
        _integer_at(data, offset + (U64_SIZE * 2), U64_SIZE),
    )


def _validate_fees(
    fees: tuple[int, int, int],
    as_of_slot: int,
) -> AbstainResult | None:
    if any(value > BASIS_POINTS_DENOMINATOR for value in fees):
        return AbstainResult(
            reason=AbstainReason.UNKNOWN_FEE_CONFIG,
            message="Pump FeeConfig contains fee basis points above 100 percent",
            as_of_slot=as_of_slot,
        )
    if sum(fees) > BASIS_POINTS_DENOMINATOR:
        return AbstainResult(
            reason=AbstainReason.UNKNOWN_FEE_CONFIG,
            message="Pump FeeConfig total fee exceeds 100 percent",
            as_of_slot=as_of_slot,
        )
    return None


def _fee_config(fees: tuple[int, int, int], as_of_slot: int) -> FeeConfig:
    lp_fee_bps, protocol_fee_bps, creator_fee_bps = fees
    return FeeConfig(
        version="pump-fee-config-account",
        protocol_fee_bps=protocol_fee_bps,
        creator_fee_bps=creator_fee_bps,
        is_known=True,
        valid_from_slot=Slot(as_of_slot),
        lp_fee_bps=lp_fee_bps,
    )


def _integer_at(data: bytes, offset: int, size: int) -> int:
    return int.from_bytes(data[offset : offset + size], "little", signed=False)


def _safe_slot(value: object) -> int:
    return value if type(value) is int else -1


def _missing(message: str, as_of_slot: int) -> AbstainResult:
    return AbstainResult(
        reason=AbstainReason.MISSING_FEATURE,
        message=message,
        as_of_slot=as_of_slot,
    )


def _unknown(message: str, as_of_slot: int) -> AbstainResult:
    return AbstainResult(
        reason=AbstainReason.UNKNOWN_PROTOCOL_STATE,
        message=message,
        as_of_slot=as_of_slot,
    )


__all__ = [
    "FEE_CONFIG_DISCRIMINATOR",
    "PUMP_FEE_CONFIG_BUMP",
    "PUMP_FEE_CONFIG_PDA",
    "PUMP_FEE_PROGRAM_ID",
    "PumpFeeConfigAccount",
    "PumpFeeConfigDecodeResult",
    "PumpFeeTier",
    "decode_pump_fee_config_account",
]
