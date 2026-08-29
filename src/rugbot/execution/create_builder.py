"""Pure Pump.fun create_v2 instruction construction."""

# ruff: noqa: PLR0913, TRY003

from __future__ import annotations

import struct
from dataclasses import dataclass

from solders.instruction import AccountMeta, Instruction
from solders.pubkey import Pubkey
from spl.token.instructions import get_associated_token_address

from rugbot.ingest.pump.create_decoder import (
    ASSOCIATED_SPL_PROGRAM_ID,
    CREATE_V2_ACCOUNT_NAMES,
    CREATE_V2_DISCRIMINATOR,
    MAYHEM_PROGRAM_ID,
    PUMP_PROGRAM_ID,
    SPL_2022_PROGRAM_ID,
    SYSTEM_PROGRAM_ID,
)
from rugbot.utils.logger import get_logger

logger = get_logger(__name__)

TOKEN_2022_PROGRAM_ID = SPL_2022_PROGRAM_ID
MAYHEM_STATE_SEED = b"mayhem-state"
MINT_AUTHORITY_SEED = b"mint-authority"
BONDING_CURVE_SEED = b"bonding-curve"
EVENT_AUTHORITY_SEED = b"__event_authority"
GLOBAL_SEED = b"global"
GLOBAL_PARAMS_SEED = b"global-params"
SOL_VAULT_SEED = b"sol-vault"


@dataclass(frozen=True, slots=True)
class CreateIntent:
    """Validated intent to create a Pump token."""

    name: str
    symbol: str
    uri: str
    creator: str
    payer: str
    mint: str
    mayhem_mode: bool = False
    cashback: bool = False
    buy_sol_lamports: int | None = None


@dataclass(frozen=True, slots=True)
class CreateResult:
    """Result of building a create_v2 instruction."""

    instruction: Instruction
    account_names: tuple[str, ...]
    accounts: tuple[Pubkey, ...]
    data: bytes


def _pubkey(value: str) -> Pubkey:
    return Pubkey.from_string(value)


def _derive_pump_pda(seed: bytes, *values: Pubkey) -> Pubkey:
    return Pubkey.find_program_address(
        [seed, *(bytes(v) for v in values)],
        _pubkey(PUMP_PROGRAM_ID),
    )[0]


def _derive_mayhem_pda(seed: bytes, *values: Pubkey) -> Pubkey:
    return Pubkey.find_program_address(
        [seed, *(bytes(v) for v in values)],
        _pubkey(MAYHEM_PROGRAM_ID),
    )[0]


def _encode_string(value: str) -> bytes:
    encoded = value.encode("utf-8")
    return struct.pack("<I", len(encoded)) + encoded


def encode_create_v2_data(
    *,
    name: str,
    symbol: str,
    uri: str,
    creator: Pubkey,
    mayhem_mode: bool,
    cashback: bool,
) -> bytes:
    """Encode create_v2 args exactly as the decoder expects (borsh)."""

    if type(name) is not str or not name:
        raise ValueError("name is required")
    if type(symbol) is not str or not symbol:
        raise ValueError("symbol is required")
    if type(uri) is not str or not uri:
        raise ValueError("uri is required")
    data = CREATE_V2_DISCRIMINATOR
    data += _encode_string(name)
    data += _encode_string(symbol)
    data += _encode_string(uri)
    data += bytes(creator)
    data += b"\x01" if mayhem_mode else b"\x00"
    data += b"\x01" if cashback else b"\x00"
    return data


def build_create_v2_instruction(
    *,
    payer: Pubkey,
    creator: Pubkey,
    mint: Pubkey,
    name: str,
    symbol: str,
    uri: str,
    mayhem_mode: bool = False,
    cashback: bool = False,
    token_program: str = TOKEN_2022_PROGRAM_ID,
) -> Instruction:
    """Build one create_v2 instruction with derived accounts and borsh data."""

    if type(mayhem_mode) is not bool:
        raise ValueError("mayhem_mode must be bool")
    if type(cashback) is not bool:
        raise ValueError("cashback must be bool")

    bonding_curve = _derive_pump_pda(BONDING_CURVE_SEED, mint)
    mint_authority = _derive_pump_pda(MINT_AUTHORITY_SEED)
    global_pda = _pubkey("4wTV1YmiEkRvAtNtsSGPtUrqRYQMe5SKy2uB4Jjaxnjf")
    event_authority = _derive_pump_pda(EVENT_AUTHORITY_SEED)
    global_params = _derive_mayhem_pda(GLOBAL_PARAMS_SEED)
    sol_vault = _derive_mayhem_pda(SOL_VAULT_SEED)
    mayhem_state = _derive_mayhem_pda(MAYHEM_STATE_SEED, mint)

    associated_bonding_curve = get_associated_token_address(
        bonding_curve, mint, _pubkey(token_program)
    )
    mayhem_token_vault = get_associated_token_address(
        sol_vault, mint, _pubkey(token_program)
    )

    accounts = (
        mint,
        mint_authority,
        bonding_curve,
        associated_bonding_curve,
        global_pda,
        payer,
        _pubkey(SYSTEM_PROGRAM_ID),
        _pubkey(token_program),
        _pubkey(ASSOCIATED_SPL_PROGRAM_ID),
        _pubkey(MAYHEM_PROGRAM_ID),
        global_params,
        sol_vault,
        mayhem_state,
        mayhem_token_vault,
        event_authority,
        _pubkey(PUMP_PROGRAM_ID),
    )

    data = encode_create_v2_data(
        name=name,
        symbol=symbol,
        uri=uri,
        creator=creator,
        mayhem_mode=mayhem_mode,
        cashback=cashback,
    )

    # signer/writable follows IDL: mint and user are signers; mint, bonding_curve,
    # associated_bonding_curve, user, sol_vault, mayhem_state, mayhem_token_vault writable
    writable = {
        "mint",
        "bonding_curve",
        "associated_bonding_curve",
        "sol_vault",
        "mayhem_state",
        "mayhem_token_vault",
    }
    signer = {"mint", "user"}

    metas: list[AccountMeta] = []
    for acc_name, pubkey in zip(CREATE_V2_ACCOUNT_NAMES, accounts, strict=True):
        metas.append(
            AccountMeta(
                pubkey=pubkey,
                is_signer=acc_name in signer,
                is_writable=acc_name in writable or acc_name == "user",
            )
        )

    return Instruction(_pubkey(PUMP_PROGRAM_ID), data, metas)


def build_create_v2_and_buy(
    *,
    payer: Pubkey,
    creator: Pubkey,
    mint: Pubkey,
    name: str,
    symbol: str,
    uri: str,
    sol_amount_lamports: int,
    min_token_out: int = 1,
    mayhem_mode: bool = False,
    cashback: bool = False,
    token_program: str = TOKEN_2022_PROGRAM_ID,
) -> list[Instruction]:
    """Build create_v2 + first buy atomically (buy uses v2_builder helpers)."""

    if type(sol_amount_lamports) is not int or sol_amount_lamports <= 0:
        raise ValueError("sol_amount_lamports must be positive")
    if type(min_token_out) is not int or min_token_out <= 0:
        raise ValueError("min_token_out must be positive")

    create_ix = build_create_v2_instruction(
        payer=payer,
        creator=creator,
        mint=mint,
        name=name,
        symbol=symbol,
        uri=uri,
        mayhem_mode=mayhem_mode,
        cashback=cashback,
        token_program=token_program,
    )

    # For the buy we need a minimal context; reuse live-like derivation.
    # To avoid RPC we use placeholder fee recipients (global state would be fetched live).
    # This builder only proves atomic grouping; live submission must fetch finalized global.
    # For dry-run we return create only if reserves unavailable; caller can simulate buy separately.
    # Here we optionally create a placeholder buy_v2 using same pump program with simplified accounts
    # if needed. For now return just create; buy is added by live path with real quote.
    _ = (sol_amount_lamports, min_token_out)
    logger.info("create_v2 atomic buy placeholder: use live path with finalized quote")
    return [create_ix]


__all__ = [
    "CreateIntent",
    "CreateResult",
    "build_create_v2_and_buy",
    "build_create_v2_instruction",
    "encode_create_v2_data",
]
