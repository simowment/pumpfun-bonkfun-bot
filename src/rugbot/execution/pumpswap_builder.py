"""Pure PumpSwap AMM instruction construction, PDA derivation, and binary pool decoder.

Ported from chainstacklabs/pumpfun-cli into the Rugbot execution layer.
Follows the official PumpSwap AMM program (pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA)
account layouts and discriminators. This module is pure: it does not perform network I/O.
"""

# ruff: noqa: PLR0913, S105, TRY003, FBT001, FBT002

from __future__ import annotations

import struct
from typing import Any, Final

from solders.instruction import AccountMeta, Instruction
from solders.pubkey import Pubkey
from solders.system_program import TransferParams, transfer
from spl.token.instructions import (
    CloseAccountParams,
    SyncNativeParams,
    close_account,
    create_idempotent_associated_token_account,
    get_associated_token_address,
    sync_native,
)

# --- Program Identifiers and Canonical Addresses ---

PUMP_AMM_PROGRAM_ID: Final[str] = "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA"
PUMP_AMM_PROGRAM: Final[Pubkey] = Pubkey.from_string(PUMP_AMM_PROGRAM_ID)

PUMP_SWAP_GLOBAL_CONFIG_ID: Final[str] = "ADyA8hdefvWN2dbGGWFotbzWxrAvLW83WG6QCVXvJKqw"
PUMP_SWAP_GLOBAL_CONFIG: Final[Pubkey] = Pubkey.from_string(PUMP_SWAP_GLOBAL_CONFIG_ID)

PUMP_SWAP_EVENT_AUTHORITY_ID: Final[str] = (
    "GS4CU59F31iL7aR2Q8zVS8DRrcRnXX1yjQ66TqNVQnaR"
)
PUMP_SWAP_EVENT_AUTHORITY: Final[Pubkey] = Pubkey.from_string(
    PUMP_SWAP_EVENT_AUTHORITY_ID
)

STANDARD_PUMPSWAP_FEE_RECIPIENT_ID: Final[str] = (
    "7VtfL8fvgNfhz17qKRMjzQEXgbdpnHHHQRh54R9jP2RJ"
)
STANDARD_PUMPSWAP_FEE_RECIPIENT: Final[Pubkey] = Pubkey.from_string(
    STANDARD_PUMPSWAP_FEE_RECIPIENT_ID
)

PUMP_PROGRAM_ID: Final[str] = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
PUMP_PROGRAM: Final[Pubkey] = Pubkey.from_string(PUMP_PROGRAM_ID)

PUMP_FEE_PROGRAM_ID: Final[str] = "pfeeUxB6jkeY1Hxd7CsFCAjcbHA9rWtchMGdZ6VojVZ"
PUMP_FEE_PROGRAM: Final[Pubkey] = Pubkey.from_string(PUMP_FEE_PROGRAM_ID)

WSOL_MINT_ID: Final[str] = "So11111111111111111111111111111111111111112"
WSOL_MINT: Final[Pubkey] = Pubkey.from_string(WSOL_MINT_ID)

SYSTEM_PROGRAM_ID: Final[str] = "11111111111111111111111111111111"
SYSTEM_PROGRAM: Final[Pubkey] = Pubkey.from_string(SYSTEM_PROGRAM_ID)

TOKEN_PROGRAM_ID: Final[str] = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
TOKEN_PROGRAM: Final[Pubkey] = Pubkey.from_string(TOKEN_PROGRAM_ID)

TOKEN_2022_PROGRAM_ID: Final[str] = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"
TOKEN_2022_PROGRAM: Final[Pubkey] = Pubkey.from_string(TOKEN_2022_PROGRAM_ID)

ASSOCIATED_TOKEN_PROGRAM_ID: Final[str] = "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL"
ASSOCIATED_TOKEN_PROGRAM: Final[Pubkey] = Pubkey.from_string(
    ASSOCIATED_TOKEN_PROGRAM_ID
)

# --- Instruction Discriminators ---

PUMPSWAP_BUY_DISCRIMINATOR: Final[bytes] = bytes([102, 6, 61, 18, 1, 218, 235, 234])
PUMPSWAP_SELL_DISCRIMINATOR: Final[bytes] = bytes([51, 230, 133, 164, 1, 127, 131, 173])
PUMPSWAP_BUY_EXACT_QUOTE_IN_DISCRIMINATOR: Final[bytes] = bytes(
    [198, 46, 21, 82, 180, 217, 232, 112]
)
PUMPSWAP_POOL_DISCRIMINATOR: Final[bytes] = bytes([241, 154, 109, 4, 17, 177, 109, 188])

# Single byte OptionBool(true) = 0x01
TRACK_VOLUME_FLAG: Final[bytes] = bytes([1])

# Minimum byte length of a PumpSwap binary pool account
PUMPSWAP_POOL_DATA_MIN_SIZE: Final[int] = 243
MAYHEM_MODE_OFFSET: Final[int] = 243
CASHBACK_COIN_OFFSET: Final[int] = 244

# Compute budget defaults for PumpSwap transactions
PUMPSWAP_BUY_COMPUTE_UNITS: Final[int] = 400_000
PUMPSWAP_SELL_COMPUTE_UNITS: Final[int] = 300_000


def _to_pubkey(val: Pubkey | str) -> Pubkey:
    """Normalize input to a Pubkey instance."""
    if isinstance(val, Pubkey):
        return val
    return Pubkey.from_string(str(val).strip())


# --- PDA Derivations ---


def derive_pool_authority(mint: Pubkey | str) -> Pubkey:
    """Derive the pool authority PDA used during Pump migration.

    Seeds: [b"pool-authority", mint] under PUMP_PROGRAM.
    """
    mint_pk = _to_pubkey(mint)
    addr, _ = Pubkey.find_program_address(
        [b"pool-authority", bytes(mint_pk)], PUMP_PROGRAM
    )
    return addr


def derive_amm_pool(mint: Pubkey | str, index: int = 0) -> Pubkey:
    """Derive the canonical PumpSwap AMM pool PDA for a token mint.

    Seeds: [b"pool", index_bytes, pool_authority, base_mint, quote_mint] under PUMP_AMM_PROGRAM.
    """
    mint_pk = _to_pubkey(mint)
    pool_authority = derive_pool_authority(mint_pk)
    addr, _ = Pubkey.find_program_address(
        [
            b"pool",
            struct.pack("<H", index),
            bytes(pool_authority),
            bytes(mint_pk),
            bytes(WSOL_MINT),
        ],
        PUMP_AMM_PROGRAM,
    )
    return addr


def derive_amm_pool_v2(base_mint: Pubkey | str) -> Pubkey:
    """Derive the PumpSwap pool-v2 PDA for a base mint.

    Seeds: [b"pool-v2", base_mint] under PUMP_AMM_PROGRAM.
    """
    base_pk = _to_pubkey(base_mint)
    addr, _ = Pubkey.find_program_address(
        [b"pool-v2", bytes(base_pk)], PUMP_AMM_PROGRAM
    )
    return addr


def derive_amm_creator_vault(coin_creator: Pubkey | str) -> Pubkey:
    """Derive the PumpSwap creator vault authority PDA.

    Seeds: [b"creator_vault", coin_creator] under PUMP_AMM_PROGRAM.
    """
    creator_pk = _to_pubkey(coin_creator)
    addr, _ = Pubkey.find_program_address(
        [b"creator_vault", bytes(creator_pk)], PUMP_AMM_PROGRAM
    )
    return addr


def derive_amm_fee_config() -> Pubkey:
    """Derive the PumpSwap fee config PDA.

    Seeds: [b"fee_config", PUMP_AMM_PROGRAM] under PUMP_FEE_PROGRAM.
    """
    addr, _ = Pubkey.find_program_address(
        [b"fee_config", bytes(PUMP_AMM_PROGRAM)], PUMP_FEE_PROGRAM
    )
    return addr


def derive_amm_global_volume_accumulator() -> Pubkey:
    """Derive the PumpSwap global volume accumulator PDA.

    Seeds: [b"global_volume_accumulator"] under PUMP_AMM_PROGRAM.
    """
    addr, _ = Pubkey.find_program_address(
        [b"global_volume_accumulator"], PUMP_AMM_PROGRAM
    )
    return addr


def derive_amm_user_volume_accumulator(user: Pubkey | str) -> Pubkey:
    """Derive a user's PumpSwap volume accumulator PDA.

    Seeds: [b"user_volume_accumulator", user] under PUMP_AMM_PROGRAM.
    """
    user_pk = _to_pubkey(user)
    addr, _ = Pubkey.find_program_address(
        [b"user_volume_accumulator", bytes(user_pk)], PUMP_AMM_PROGRAM
    )
    return addr


# --- Binary Pool Decoder ---


def parse_pumpswap_pool_data(
    data: bytes,
    *,
    validate_discriminator: bool = True,
) -> dict[str, Any]:
    """Parse binary PumpSwap pool account data into a structured dictionary.

    Binary layout (Anchor account:Pool):
        [0:8]     discriminator            PUMPSWAP_POOL_DISCRIMINATOR
        [8]       pool_bump                u8
        [9:11]    index                    u16 (little-endian)
        [11:43]   creator                  pubkey
        [43:75]   base_mint                pubkey
        [75:107]  quote_mint               pubkey
        [107:139] lp_mint                  pubkey
        [139:171] pool_base_token_account  pubkey
        [171:203] pool_quote_token_account pubkey
        [203:211] lp_supply                u64 (little-endian)
        [211:243] coin_creator             pubkey
        [243]     is_mayhem_mode           bool (optional, default False)
        [244]     is_cashback_coin         bool (optional, default False)
    """
    if len(data) < PUMPSWAP_POOL_DATA_MIN_SIZE:
        raise ValueError(
            f"PumpSwap pool data length ({len(data)}) is shorter than minimum {PUMPSWAP_POOL_DATA_MIN_SIZE} bytes"
        )
    if validate_discriminator and data[:8] != PUMPSWAP_POOL_DISCRIMINATOR:
        raise ValueError(
            f"Invalid PumpSwap pool discriminator: {data[:8].hex()} != {PUMPSWAP_POOL_DISCRIMINATOR.hex()}"
        )

    pool_bump = data[8]
    index = struct.unpack_from("<H", data, 9)[0]
    creator = Pubkey.from_bytes(data[11:43])
    base_mint = Pubkey.from_bytes(data[43:75])
    quote_mint = Pubkey.from_bytes(data[75:107])
    lp_mint = Pubkey.from_bytes(data[107:139])
    pool_base_token_account = Pubkey.from_bytes(data[139:171])
    pool_quote_token_account = Pubkey.from_bytes(data[171:203])
    lp_supply = struct.unpack_from("<Q", data, 203)[0]
    coin_creator = Pubkey.from_bytes(data[211:243])

    is_mayhem_mode = (
        bool(data[MAYHEM_MODE_OFFSET]) if len(data) > MAYHEM_MODE_OFFSET else False
    )
    is_cashback_coin = (
        bool(data[CASHBACK_COIN_OFFSET]) if len(data) > CASHBACK_COIN_OFFSET else False
    )

    return {
        "pool_bump": pool_bump,
        "index": index,
        "creator": creator,
        "base_mint": base_mint,
        "quote_mint": quote_mint,
        "lp_mint": lp_mint,
        "pool_base_token_account": pool_base_token_account,
        "pool_quote_token_account": pool_quote_token_account,
        "lp_supply": lp_supply,
        "coin_creator": coin_creator,
        "is_mayhem_mode": is_mayhem_mode,
        "is_cashback_coin": is_cashback_coin,
    }


# --- Instruction Builders ---


def build_pumpswap_buy_instructions(
    user: Pubkey | str,
    pool_address: Pubkey | str,
    pool: dict[str, Any],
    amount_out: int,
    max_sol_in: int,
    token_program_id: Pubkey | str | None = None,
    sol_wrap_lamports: int | None = None,
    fee_recipient: Pubkey | str = STANDARD_PUMPSWAP_FEE_RECIPIENT,
    fee_recipient_ata: Pubkey | str | None = None,
    track_volume: bool = True,
) -> list[Instruction]:
    """Build PumpSwap buy instructions (WSOL wrap + buy swap).

    Returns 5 instructions:
    1. Create WSOL ATA (idempotent)
    2. Transfer SOL to WSOL ATA
    3. Sync native on WSOL ATA
    4. Create base token ATA (idempotent)
    5. Buy swap instruction (24 accounts matching Anchor IDL + pool_v2 trailing)
    """
    if sol_wrap_lamports is None:
        sol_wrap_lamports = max_sol_in
    user_pk = _to_pubkey(user)
    pool_pk = _to_pubkey(pool_address)
    token_prog_pk = _to_pubkey(
        token_program_id or pool.get("token_program_id") or TOKEN_PROGRAM
    )
    fee_rec_pk = _to_pubkey(fee_recipient)
    base_mint_pk = _to_pubkey(pool["base_mint"])
    coin_creator_pk = _to_pubkey(pool["coin_creator"])
    pool_base_ata = _to_pubkey(pool["pool_base_token_account"])
    pool_quote_ata = _to_pubkey(pool["pool_quote_token_account"])

    user_wsol_ata = get_associated_token_address(user_pk, WSOL_MINT, TOKEN_PROGRAM)
    user_token_ata = get_associated_token_address(user_pk, base_mint_pk, token_prog_pk)

    creator_vault_authority = derive_amm_creator_vault(coin_creator_pk)
    creator_vault_ata = get_associated_token_address(
        creator_vault_authority, WSOL_MINT, TOKEN_PROGRAM
    )

    fee_rec_ata = (
        _to_pubkey(fee_recipient_ata)
        if fee_recipient_ata is not None
        else get_associated_token_address(fee_rec_pk, WSOL_MINT, TOKEN_PROGRAM)
    )

    # 1. Create WSOL ATA
    create_wsol_ata = create_idempotent_associated_token_account(
        payer=user_pk,
        owner=user_pk,
        mint=WSOL_MINT,
        token_program_id=TOKEN_PROGRAM,
    )

    # 2. Transfer SOL to WSOL ATA
    transfer_ix = transfer(
        TransferParams(
            from_pubkey=user_pk,
            to_pubkey=user_wsol_ata,
            lamports=sol_wrap_lamports,
        )
    )

    # 3. Sync native
    sync_ix = sync_native(
        SyncNativeParams(
            program_id=TOKEN_PROGRAM,
            account=user_wsol_ata,
        )
    )

    # 4. Create base token ATA
    create_token_ata = create_idempotent_associated_token_account(
        payer=user_pk,
        owner=user_pk,
        mint=base_mint_pk,
        token_program_id=token_prog_pk,
    )

    # 5. Buy instruction (24 accounts)
    pool_v2 = derive_amm_pool_v2(base_mint_pk)

    buy_accounts = [
        AccountMeta(pubkey=pool_pk, is_signer=False, is_writable=True),
        AccountMeta(pubkey=user_pk, is_signer=True, is_writable=True),
        AccountMeta(pubkey=PUMP_SWAP_GLOBAL_CONFIG, is_signer=False, is_writable=False),
        AccountMeta(pubkey=base_mint_pk, is_signer=False, is_writable=False),
        AccountMeta(pubkey=WSOL_MINT, is_signer=False, is_writable=False),
        AccountMeta(pubkey=user_token_ata, is_signer=False, is_writable=True),
        AccountMeta(pubkey=user_wsol_ata, is_signer=False, is_writable=True),
        AccountMeta(pubkey=pool_base_ata, is_signer=False, is_writable=True),
        AccountMeta(pubkey=pool_quote_ata, is_signer=False, is_writable=True),
        AccountMeta(pubkey=fee_rec_pk, is_signer=False, is_writable=False),
        AccountMeta(pubkey=fee_rec_ata, is_signer=False, is_writable=True),
        AccountMeta(pubkey=token_prog_pk, is_signer=False, is_writable=False),
        AccountMeta(pubkey=TOKEN_PROGRAM, is_signer=False, is_writable=False),
        AccountMeta(pubkey=SYSTEM_PROGRAM, is_signer=False, is_writable=False),
        AccountMeta(
            pubkey=ASSOCIATED_TOKEN_PROGRAM, is_signer=False, is_writable=False
        ),
        AccountMeta(
            pubkey=PUMP_SWAP_EVENT_AUTHORITY, is_signer=False, is_writable=False
        ),
        AccountMeta(pubkey=PUMP_AMM_PROGRAM, is_signer=False, is_writable=False),
        AccountMeta(pubkey=creator_vault_ata, is_signer=False, is_writable=True),
        AccountMeta(pubkey=creator_vault_authority, is_signer=False, is_writable=False),
        AccountMeta(
            pubkey=derive_amm_global_volume_accumulator(),
            is_signer=False,
            is_writable=False,
        ),
        AccountMeta(
            pubkey=derive_amm_user_volume_accumulator(user_pk),
            is_signer=False,
            is_writable=True,
        ),
        AccountMeta(pubkey=derive_amm_fee_config(), is_signer=False, is_writable=False),
        AccountMeta(pubkey=PUMP_FEE_PROGRAM, is_signer=False, is_writable=False),
        AccountMeta(pubkey=pool_v2, is_signer=False, is_writable=False),
    ]

    volume_flag = TRACK_VOLUME_FLAG if track_volume else bytes([0])
    instruction_data = (
        PUMPSWAP_BUY_DISCRIMINATOR
        + struct.pack("<QQ", amount_out, max_sol_in)
        + volume_flag
    )

    buy_ix = Instruction(
        program_id=PUMP_AMM_PROGRAM,
        accounts=buy_accounts,
        data=instruction_data,
    )

    return [create_wsol_ata, transfer_ix, sync_ix, create_token_ata, buy_ix]


def build_pumpswap_buy_exact_quote_in_instructions(
    user: Pubkey | str,
    pool_address: Pubkey | str,
    pool: dict[str, Any],
    spendable_quote_in: int,
    min_base_amount_out: int,
    token_program_id: Pubkey | str | None = None,
    fee_recipient: Pubkey | str = STANDARD_PUMPSWAP_FEE_RECIPIENT,
    fee_recipient_ata: Pubkey | str | None = None,
    track_volume: bool = True,
) -> list[Instruction]:
    """Build PumpSwap buy exact quote in instructions (WSOL wrap + buy swap).

    Spends exact quote lamports (SOL wrapped into WSOL) to receive at least min_base_amount_out.
    Returns 5 instructions:
    1. Create WSOL ATA (idempotent)
    2. Transfer SOL to WSOL ATA (spendable_quote_in)
    3. Sync native on WSOL ATA
    4. Create base token ATA (idempotent)
    5. Buy exact quote in swap instruction (24 accounts)
    """
    user_pk = _to_pubkey(user)
    pool_pk = _to_pubkey(pool_address)
    token_prog_pk = _to_pubkey(
        token_program_id or pool.get("token_program_id") or TOKEN_PROGRAM
    )
    fee_rec_pk = _to_pubkey(fee_recipient)
    base_mint_pk = _to_pubkey(pool["base_mint"])
    coin_creator_pk = _to_pubkey(pool["coin_creator"])
    pool_base_ata = _to_pubkey(pool["pool_base_token_account"])
    pool_quote_ata = _to_pubkey(pool["pool_quote_token_account"])

    user_wsol_ata = get_associated_token_address(user_pk, WSOL_MINT, TOKEN_PROGRAM)
    user_token_ata = get_associated_token_address(user_pk, base_mint_pk, token_prog_pk)

    creator_vault_authority = derive_amm_creator_vault(coin_creator_pk)
    creator_vault_ata = get_associated_token_address(
        creator_vault_authority, WSOL_MINT, TOKEN_PROGRAM
    )

    fee_rec_ata = (
        _to_pubkey(fee_recipient_ata)
        if fee_recipient_ata is not None
        else get_associated_token_address(fee_rec_pk, WSOL_MINT, TOKEN_PROGRAM)
    )

    # 1. Create WSOL ATA
    create_wsol_ata = create_idempotent_associated_token_account(
        payer=user_pk,
        owner=user_pk,
        mint=WSOL_MINT,
        token_program_id=TOKEN_PROGRAM,
    )

    # 2. Transfer SOL to WSOL ATA
    transfer_ix = transfer(
        TransferParams(
            from_pubkey=user_pk,
            to_pubkey=user_wsol_ata,
            lamports=spendable_quote_in,
        )
    )

    # 3. Sync native
    sync_ix = sync_native(
        SyncNativeParams(
            program_id=TOKEN_PROGRAM,
            account=user_wsol_ata,
        )
    )

    # 4. Create base token ATA
    create_token_ata = create_idempotent_associated_token_account(
        payer=user_pk,
        owner=user_pk,
        mint=base_mint_pk,
        token_program_id=token_prog_pk,
    )

    # 5. Buy exact quote in instruction (24 accounts)
    pool_v2 = derive_amm_pool_v2(base_mint_pk)

    buy_accounts = [
        AccountMeta(pubkey=pool_pk, is_signer=False, is_writable=True),
        AccountMeta(pubkey=user_pk, is_signer=True, is_writable=True),
        AccountMeta(pubkey=PUMP_SWAP_GLOBAL_CONFIG, is_signer=False, is_writable=False),
        AccountMeta(pubkey=base_mint_pk, is_signer=False, is_writable=False),
        AccountMeta(pubkey=WSOL_MINT, is_signer=False, is_writable=False),
        AccountMeta(pubkey=user_token_ata, is_signer=False, is_writable=True),
        AccountMeta(pubkey=user_wsol_ata, is_signer=False, is_writable=True),
        AccountMeta(pubkey=pool_base_ata, is_signer=False, is_writable=True),
        AccountMeta(pubkey=pool_quote_ata, is_signer=False, is_writable=True),
        AccountMeta(pubkey=fee_rec_pk, is_signer=False, is_writable=False),
        AccountMeta(pubkey=fee_rec_ata, is_signer=False, is_writable=True),
        AccountMeta(pubkey=token_prog_pk, is_signer=False, is_writable=False),
        AccountMeta(pubkey=TOKEN_PROGRAM, is_signer=False, is_writable=False),
        AccountMeta(pubkey=SYSTEM_PROGRAM, is_signer=False, is_writable=False),
        AccountMeta(
            pubkey=ASSOCIATED_TOKEN_PROGRAM, is_signer=False, is_writable=False
        ),
        AccountMeta(
            pubkey=PUMP_SWAP_EVENT_AUTHORITY, is_signer=False, is_writable=False
        ),
        AccountMeta(pubkey=PUMP_AMM_PROGRAM, is_signer=False, is_writable=False),
        AccountMeta(pubkey=creator_vault_ata, is_signer=False, is_writable=True),
        AccountMeta(pubkey=creator_vault_authority, is_signer=False, is_writable=False),
        AccountMeta(
            pubkey=derive_amm_global_volume_accumulator(),
            is_signer=False,
            is_writable=False,
        ),
        AccountMeta(
            pubkey=derive_amm_user_volume_accumulator(user_pk),
            is_signer=False,
            is_writable=True,
        ),
        AccountMeta(pubkey=derive_amm_fee_config(), is_signer=False, is_writable=False),
        AccountMeta(pubkey=PUMP_FEE_PROGRAM, is_signer=False, is_writable=False),
        AccountMeta(pubkey=pool_v2, is_signer=False, is_writable=False),
    ]

    volume_flag = TRACK_VOLUME_FLAG if track_volume else bytes([0])
    instruction_data = (
        PUMPSWAP_BUY_EXACT_QUOTE_IN_DISCRIMINATOR
        + struct.pack("<QQ", spendable_quote_in, min_base_amount_out)
        + volume_flag
    )

    buy_ix = Instruction(
        program_id=PUMP_AMM_PROGRAM,
        accounts=buy_accounts,
        data=instruction_data,
    )

    return [create_wsol_ata, transfer_ix, sync_ix, create_token_ata, buy_ix]


def build_pumpswap_sell_instructions(
    user: Pubkey | str,
    pool_address: Pubkey | str,
    pool: dict[str, Any],
    token_amount: int,
    min_sol_out: int,
    token_program_id: Pubkey | str | None = None,
    fee_recipient: Pubkey | str = STANDARD_PUMPSWAP_FEE_RECIPIENT,
    fee_recipient_ata: Pubkey | str | None = None,
    unwrap_sol: bool = False,
) -> list[Instruction]:
    """Build PumpSwap sell instructions (WSOL ATA + sell swap + optional SOL unwrap).

    Returns 2 instructions by default (create WSOL ATA + sell swap), or 3 if unwrap_sol=True.
    """
    user_pk = _to_pubkey(user)
    pool_pk = _to_pubkey(pool_address)
    token_prog_pk = _to_pubkey(
        token_program_id or pool.get("token_program_id") or TOKEN_PROGRAM
    )
    fee_rec_pk = _to_pubkey(fee_recipient)
    base_mint_pk = _to_pubkey(pool["base_mint"])
    coin_creator_pk = _to_pubkey(pool["coin_creator"])
    pool_base_ata = _to_pubkey(pool["pool_base_token_account"])
    pool_quote_ata = _to_pubkey(pool["pool_quote_token_account"])

    user_wsol_ata = get_associated_token_address(user_pk, WSOL_MINT, TOKEN_PROGRAM)
    user_token_ata = get_associated_token_address(user_pk, base_mint_pk, token_prog_pk)

    creator_vault_authority = derive_amm_creator_vault(coin_creator_pk)
    creator_vault_ata = get_associated_token_address(
        creator_vault_authority, WSOL_MINT, TOKEN_PROGRAM
    )

    fee_rec_ata = (
        _to_pubkey(fee_recipient_ata)
        if fee_recipient_ata is not None
        else get_associated_token_address(fee_rec_pk, WSOL_MINT, TOKEN_PROGRAM)
    )

    # 1. Create WSOL ATA
    create_wsol_ata = create_idempotent_associated_token_account(
        payer=user_pk,
        owner=user_pk,
        mint=WSOL_MINT,
        token_program_id=TOKEN_PROGRAM,
    )

    # 2. Sell instruction (22 accounts)
    pool_v2 = derive_amm_pool_v2(base_mint_pk)

    sell_accounts = [
        AccountMeta(pubkey=pool_pk, is_signer=False, is_writable=True),
        AccountMeta(pubkey=user_pk, is_signer=True, is_writable=True),
        AccountMeta(pubkey=PUMP_SWAP_GLOBAL_CONFIG, is_signer=False, is_writable=False),
        AccountMeta(pubkey=base_mint_pk, is_signer=False, is_writable=False),
        AccountMeta(pubkey=WSOL_MINT, is_signer=False, is_writable=False),
        AccountMeta(pubkey=user_token_ata, is_signer=False, is_writable=True),
        AccountMeta(pubkey=user_wsol_ata, is_signer=False, is_writable=True),
        AccountMeta(pubkey=pool_base_ata, is_signer=False, is_writable=True),
        AccountMeta(pubkey=pool_quote_ata, is_signer=False, is_writable=True),
        AccountMeta(pubkey=fee_rec_pk, is_signer=False, is_writable=False),
        AccountMeta(pubkey=fee_rec_ata, is_signer=False, is_writable=True),
        AccountMeta(pubkey=token_prog_pk, is_signer=False, is_writable=False),
        AccountMeta(pubkey=TOKEN_PROGRAM, is_signer=False, is_writable=False),
        AccountMeta(pubkey=SYSTEM_PROGRAM, is_signer=False, is_writable=False),
        AccountMeta(
            pubkey=ASSOCIATED_TOKEN_PROGRAM, is_signer=False, is_writable=False
        ),
        AccountMeta(
            pubkey=PUMP_SWAP_EVENT_AUTHORITY, is_signer=False, is_writable=False
        ),
        AccountMeta(pubkey=PUMP_AMM_PROGRAM, is_signer=False, is_writable=False),
        AccountMeta(pubkey=creator_vault_ata, is_signer=False, is_writable=True),
        AccountMeta(pubkey=creator_vault_authority, is_signer=False, is_writable=False),
        AccountMeta(pubkey=derive_amm_fee_config(), is_signer=False, is_writable=False),
        AccountMeta(pubkey=PUMP_FEE_PROGRAM, is_signer=False, is_writable=False),
        AccountMeta(pubkey=pool_v2, is_signer=False, is_writable=False),
    ]

    instruction_data = PUMPSWAP_SELL_DISCRIMINATOR + struct.pack(
        "<QQ", token_amount, min_sol_out
    )

    sell_ix = Instruction(
        program_id=PUMP_AMM_PROGRAM,
        accounts=sell_accounts,
        data=instruction_data,
    )

    instructions = [create_wsol_ata, sell_ix]
    if unwrap_sol:
        close_wsol_ix = close_account(
            CloseAccountParams(
                program_id=TOKEN_PROGRAM,
                account=user_wsol_ata,
                dest=user_pk,
                owner=user_pk,
            )
        )
        instructions.append(close_wsol_ix)

    return instructions


__all__ = [
    "ASSOCIATED_TOKEN_PROGRAM",
    "ASSOCIATED_TOKEN_PROGRAM_ID",
    "PUMPSWAP_BUY_COMPUTE_UNITS",
    "PUMPSWAP_BUY_DISCRIMINATOR",
    "PUMPSWAP_BUY_EXACT_QUOTE_IN_DISCRIMINATOR",
    "PUMPSWAP_POOL_DATA_MIN_SIZE",
    "PUMPSWAP_POOL_DISCRIMINATOR",
    "PUMPSWAP_SELL_COMPUTE_UNITS",
    "PUMPSWAP_SELL_DISCRIMINATOR",
    "PUMP_AMM_PROGRAM",
    "PUMP_AMM_PROGRAM_ID",
    "PUMP_FEE_PROGRAM",
    "PUMP_FEE_PROGRAM_ID",
    "PUMP_PROGRAM",
    "PUMP_PROGRAM_ID",
    "PUMP_SWAP_EVENT_AUTHORITY",
    "PUMP_SWAP_EVENT_AUTHORITY_ID",
    "PUMP_SWAP_GLOBAL_CONFIG",
    "PUMP_SWAP_GLOBAL_CONFIG_ID",
    "STANDARD_PUMPSWAP_FEE_RECIPIENT",
    "STANDARD_PUMPSWAP_FEE_RECIPIENT_ID",
    "SYSTEM_PROGRAM",
    "SYSTEM_PROGRAM_ID",
    "TOKEN_2022_PROGRAM",
    "TOKEN_2022_PROGRAM_ID",
    "TOKEN_PROGRAM",
    "TOKEN_PROGRAM_ID",
    "TRACK_VOLUME_FLAG",
    "WSOL_MINT",
    "WSOL_MINT_ID",
    "build_pumpswap_buy_exact_quote_in_instructions",
    "build_pumpswap_buy_instructions",
    "build_pumpswap_sell_instructions",
    "derive_amm_creator_vault",
    "derive_amm_fee_config",
    "derive_amm_global_volume_accumulator",
    "derive_amm_pool",
    "derive_amm_pool_v2",
    "derive_amm_user_volume_accumulator",
    "derive_pool_authority",
    "parse_pumpswap_pool_data",
]
