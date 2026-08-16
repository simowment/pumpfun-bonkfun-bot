"""Strict decoder for the canonical Pump Global account.

The layout is pinned to Pump's official ``idl/pump.json`` at commit
``9c82f61cb711b044a17f770ab8ce9f9bdf78f333``. Its Global struct occupies
exactly 1,045 bytes including the Anchor discriminator. Any different account
length is an unsupported protocol state rather than an inferred extension.
"""

from dataclasses import dataclass
from hashlib import sha256
from typing import TypeAlias

import base58
from solders.pubkey import Pubkey

from rugbot.domain.amounts import Slot
from rugbot.domain.decisions import AbstainReason, AbstainResult
from rugbot.domain.fees import BASIS_POINTS_DENOMINATOR
from rugbot.domain.observations import RawChainObservation
from rugbot.protocol.pump.bonding_curve_account import PUMP_PROGRAM_ID

PUMP_GLOBAL_SEED = b"global"
PUMP_GLOBAL_DISCRIMINATOR = bytes([167, 232, 232, 177, 200, 108, 114, 127])
PINNED_OFFICIAL_PUMP_IDL_COMMIT = "9c82f61cb711b044a17f770ab8ce9f9bdf78f333"
PINNED_OFFICIAL_PUMP_IDL_SHA256 = (
    "b90bc471327f671449271d5d1d42354d1fae6f5a06502f5834459a3108138e49"
)

DISCRIMINATOR_SIZE = 8
BOOL_FIELD_COUNT = 5
U64_FIELD_COUNT = 9
PUBKEY_FIELD_COUNT = 30
PUBKEY_SIZE = 32
U64_SIZE = 8
PUMP_GLOBAL_ACCOUNT_SIZE = (
    DISCRIMINATOR_SIZE
    + BOOL_FIELD_COUNT
    + (U64_FIELD_COUNT * U64_SIZE)
    + (PUBKEY_FIELD_COUNT * PUBKEY_SIZE)
)

_PUMP_PROGRAM_PUBKEY = Pubkey.from_string(PUMP_PROGRAM_ID)
_PUMP_GLOBAL_PUBKEY, _ = Pubkey.find_program_address(
    [PUMP_GLOBAL_SEED],
    _PUMP_PROGRAM_PUBKEY,
)
PUMP_GLOBAL_PDA = str(_PUMP_GLOBAL_PUBKEY)
_PUMP_GLOBAL_ACCOUNT_BYTES = bytes(_PUMP_GLOBAL_PUBKEY)
_PUMP_PROGRAM_BYTES = bytes(_PUMP_PROGRAM_PUBKEY)


@dataclass(frozen=True, slots=True)
class PumpGlobalAccount:
    """Immutable decoded state of the finalized canonical Pump Global PDA."""

    as_of_slot: Slot
    account_pubkey: str
    owner_program_id: str
    raw_account_data_sha256: str
    initialized: bool
    authority_pubkey: str
    fee_recipient_pubkey: str
    initial_virtual_token_reserves: int
    initial_virtual_sol_reserves: int
    initial_real_token_reserves: int
    token_total_supply: int
    fee_basis_points: int
    withdraw_authority_pubkey: str
    enable_migrate: bool
    pool_migration_fee: int
    creator_fee_basis_points: int
    fee_recipients: tuple[str, ...]
    set_creator_authority_pubkey: str
    admin_set_creator_authority_pubkey: str
    create_v2_enabled: bool
    whitelist_pda: str
    reserved_fee_recipient: str
    mayhem_mode_enabled: bool
    reserved_fee_recipients: tuple[str, ...]
    is_cashback_enabled: bool
    buyback_fee_recipients: tuple[str, ...]
    buyback_basis_points: int
    initial_virtual_quote_reserves: int
    whitelisted_quote_mints: tuple[str, ...]


PumpGlobalDecodeResult: TypeAlias = PumpGlobalAccount | AbstainResult


def decode_pump_global_account(  # noqa: PLR0911
    observation: RawChainObservation,
) -> PumpGlobalDecodeResult:
    """Decode one finalized canonical Pump Global account observation."""

    validation = _validate_observation(observation)
    if validation is not None:
        return validation

    data = observation.raw_account_data
    if len(data) != PUMP_GLOBAL_ACCOUNT_SIZE:
        return _abstain(
            AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
            "Pump Global account length does not match the pinned official IDL",
            observation.slot,
        )
    if data[:DISCRIMINATOR_SIZE] != PUMP_GLOBAL_DISCRIMINATOR:
        return _abstain(
            AbstainReason.DECODER_MISMATCH,
            "Pump Global discriminator does not match the pinned official IDL",
            observation.slot,
        )

    reader = _GlobalReader(data, DISCRIMINATOR_SIZE)
    try:
        initialized = reader.boolean("initialized")
        authority = reader.pubkey()
        fee_recipient = reader.pubkey()
        initial_virtual_token_reserves = reader.u64()
        initial_virtual_sol_reserves = reader.u64()
        initial_real_token_reserves = reader.u64()
        token_total_supply = reader.u64()
        fee_basis_points = reader.u64()
        withdraw_authority = reader.pubkey()
        enable_migrate = reader.boolean("enable_migrate")
        pool_migration_fee = reader.u64()
        creator_fee_basis_points = reader.u64()
        fee_recipients = reader.pubkeys(7)
        set_creator_authority = reader.pubkey()
        admin_set_creator_authority = reader.pubkey()
        create_v2_enabled = reader.boolean("create_v2_enabled")
        whitelist_pda = reader.pubkey()
        reserved_fee_recipient = reader.pubkey()
        mayhem_mode_enabled = reader.boolean("mayhem_mode_enabled")
        reserved_fee_recipients = reader.pubkeys(7)
        is_cashback_enabled = reader.boolean("is_cashback_enabled")
        buyback_fee_recipients = reader.pubkeys(8)
        buyback_basis_points = reader.u64()
        initial_virtual_quote_reserves = reader.u64()
        whitelisted_quote_mints = reader.pubkeys(1)
    except _InvalidBooleanError as error:
        return _abstain(
            AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
            f"Pump Global {error.field_name} is not a canonical bool",
            observation.slot,
        )

    if reader.offset != PUMP_GLOBAL_ACCOUNT_SIZE:
        return _abstain(
            AbstainReason.DECODER_MISMATCH,
            "Pump Global decoder did not consume the pinned layout exactly",
            observation.slot,
        )
    invalid_fee = _validate_fee_basis_points(
        fee_basis_points=fee_basis_points,
        creator_fee_basis_points=creator_fee_basis_points,
        buyback_basis_points=buyback_basis_points,
        as_of_slot=observation.slot,
    )
    if invalid_fee is not None:
        return invalid_fee

    return PumpGlobalAccount(
        as_of_slot=Slot(observation.slot),
        account_pubkey=PUMP_GLOBAL_PDA,
        owner_program_id=PUMP_PROGRAM_ID,
        raw_account_data_sha256=sha256(data).hexdigest(),
        initialized=initialized,
        authority_pubkey=authority,
        fee_recipient_pubkey=fee_recipient,
        initial_virtual_token_reserves=initial_virtual_token_reserves,
        initial_virtual_sol_reserves=initial_virtual_sol_reserves,
        initial_real_token_reserves=initial_real_token_reserves,
        token_total_supply=token_total_supply,
        fee_basis_points=fee_basis_points,
        withdraw_authority_pubkey=withdraw_authority,
        enable_migrate=enable_migrate,
        pool_migration_fee=pool_migration_fee,
        creator_fee_basis_points=creator_fee_basis_points,
        fee_recipients=fee_recipients,
        set_creator_authority_pubkey=set_creator_authority,
        admin_set_creator_authority_pubkey=admin_set_creator_authority,
        create_v2_enabled=create_v2_enabled,
        whitelist_pda=whitelist_pda,
        reserved_fee_recipient=reserved_fee_recipient,
        mayhem_mode_enabled=mayhem_mode_enabled,
        reserved_fee_recipients=reserved_fee_recipients,
        is_cashback_enabled=is_cashback_enabled,
        buyback_fee_recipients=buyback_fee_recipients,
        buyback_basis_points=buyback_basis_points,
        initial_virtual_quote_reserves=initial_virtual_quote_reserves,
        whitelisted_quote_mints=whitelisted_quote_mints,
    )


def _validate_observation(observation: object) -> AbstainResult | None:
    if type(observation) is not RawChainObservation:
        return _abstain(
            AbstainReason.MISSING_FEATURE,
            "finalized Pump Global account observation is required",
            -1,
        )
    if (
        observation.commitment != "finalized"
        or observation.canonical_status != "canonical"
        or observation.source_update_kind != "account"
    ):
        return _abstain(
            AbstainReason.STALE_STATE,
            "Pump Global requires finalized canonical account evidence",
            _safe_slot(observation.slot),
        )
    if (
        type(observation.slot) is not int
        or observation.slot < 0
        or type(observation.account_pubkey) is not bytes
        or type(observation.account_owner_program_id) is not bytes
        or type(observation.raw_account_data) is not bytes
    ):
        return _abstain(
            AbstainReason.MISSING_FEATURE,
            "Pump Global account identity and raw bytes are required",
            _safe_slot(observation.slot),
        )
    if observation.account_pubkey != _PUMP_GLOBAL_ACCOUNT_BYTES:
        return _abstain(
            AbstainReason.UNKNOWN_PROTOCOL_STATE,
            "account observation is not the canonical Pump Global PDA",
            observation.slot,
        )
    if observation.account_owner_program_id != _PUMP_PROGRAM_BYTES:
        return _abstain(
            AbstainReason.UNKNOWN_PROTOCOL_STATE,
            "Pump Global account is not owned by the pinned Pump program",
            observation.slot,
        )
    return None


def _validate_fee_basis_points(
    *,
    fee_basis_points: int,
    creator_fee_basis_points: int,
    buyback_basis_points: int,
    as_of_slot: int,
) -> AbstainResult | None:
    if any(
        value > BASIS_POINTS_DENOMINATOR
        for value in (
            fee_basis_points,
            creator_fee_basis_points,
            buyback_basis_points,
        )
    ):
        return _abstain(
            AbstainReason.UNKNOWN_FEE_CONFIG,
            "Pump Global contains fee basis points above 100 percent",
            as_of_slot,
        )
    return None


class _GlobalReader:
    def __init__(self, data: bytes, offset: int) -> None:
        self._data = data
        self.offset = offset

    def boolean(self, field_name: str) -> bool:
        value = self._take(1)[0]
        if value not in (0, 1):
            raise _InvalidBooleanError(field_name)
        return bool(value)

    def u64(self) -> int:
        return int.from_bytes(self._take(U64_SIZE), "little", signed=False)

    def pubkey(self) -> str:
        return base58.b58encode(self._take(PUBKEY_SIZE)).decode("ascii")

    def pubkeys(self, count: int) -> tuple[str, ...]:
        return tuple(self.pubkey() for _ in range(count))

    def _take(self, size: int) -> bytes:
        start = self.offset
        self.offset += size
        return self._data[start : self.offset]


class _InvalidBooleanError(ValueError):
    def __init__(self, field_name: str) -> None:
        self.field_name = field_name
        super().__init__(field_name)


def _safe_slot(value: object) -> int:
    return value if type(value) is int else -1


def _abstain(
    reason: AbstainReason,
    message: str,
    as_of_slot: int,
) -> AbstainResult:
    return AbstainResult(reason=reason, message=message, as_of_slot=as_of_slot)


__all__ = [
    "PINNED_OFFICIAL_PUMP_IDL_COMMIT",
    "PINNED_OFFICIAL_PUMP_IDL_SHA256",
    "PUMP_GLOBAL_ACCOUNT_SIZE",
    "PUMP_GLOBAL_DISCRIMINATOR",
    "PUMP_GLOBAL_PDA",
    "PumpGlobalAccount",
    "PumpGlobalDecodeResult",
    "decode_pump_global_account",
]
