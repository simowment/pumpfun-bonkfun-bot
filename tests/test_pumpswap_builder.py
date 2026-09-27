"""Comprehensive unit and integration tests for PumpSwap AMM builder and auto-routing."""

from __future__ import annotations

import struct
from typing import Any

import pytest
from solders.pubkey import Pubkey

from rugbot.execution.auto_router import (
    BONDING_CURVE_COMPLETE_OFFSET,
    BONDING_CURVE_MIN_SIZE,
    RouteVenue,
    detect_venue_from_bonding_curve_data,
    detect_venue_from_pool_data,
)
from rugbot.execution.pumpswap_builder import (
    PUMP_AMM_PROGRAM,
    PUMP_AMM_PROGRAM_ID,
    PUMP_SWAP_EVENT_AUTHORITY,
    PUMP_SWAP_EVENT_AUTHORITY_ID,
    PUMP_SWAP_GLOBAL_CONFIG,
    PUMP_SWAP_GLOBAL_CONFIG_ID,
    PUMPSWAP_BUY_DISCRIMINATOR,
    PUMPSWAP_BUY_EXACT_QUOTE_IN_DISCRIMINATOR,
    PUMPSWAP_POOL_DATA_MIN_SIZE,
    PUMPSWAP_POOL_DISCRIMINATOR,
    PUMPSWAP_SELL_DISCRIMINATOR,
    STANDARD_PUMPSWAP_FEE_RECIPIENT,
    STANDARD_PUMPSWAP_FEE_RECIPIENT_ID,
    TOKEN_PROGRAM,
    build_pumpswap_buy_exact_quote_in_instructions,
    build_pumpswap_buy_instructions,
    build_pumpswap_sell_instructions,
    derive_amm_creator_vault,
    derive_amm_fee_config,
    derive_amm_global_volume_accumulator,
    derive_amm_pool,
    derive_amm_pool_v2,
    derive_amm_user_volume_accumulator,
    derive_pool_authority,
    parse_pumpswap_pool_data,
)

SAMPLE_MINT = "279mMFSUjS2kg4S3yQwwv3zZBqCtZ1Quvmg8FUHYpump"
SAMPLE_USER = "11111111111111111111111111111111"
SAMPLE_CREATOR = "4vM3DuRzN7sm2gZ6h4KqC7qA8F6d8o5C3e2B1a9X7yZ"


# --- 1. Constants Verification ---


def test_pumpswap_constants() -> None:
    """Verify canonical PumpSwap program addresses and discriminators."""
    assert PUMP_AMM_PROGRAM_ID == "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA"
    assert str(PUMP_AMM_PROGRAM) == PUMP_AMM_PROGRAM_ID

    assert PUMP_SWAP_GLOBAL_CONFIG_ID == "ADyA8hdefvWN2dbGGWFotbzWxrAvLW83WG6QCVXvJKqw"
    assert str(PUMP_SWAP_GLOBAL_CONFIG) == PUMP_SWAP_GLOBAL_CONFIG_ID

    assert (
        PUMP_SWAP_EVENT_AUTHORITY_ID == "GS4CU59F31iL7aR2Q8zVS8DRrcRnXX1yjQ66TqNVQnaR"
    )
    assert str(PUMP_SWAP_EVENT_AUTHORITY) == PUMP_SWAP_EVENT_AUTHORITY_ID

    assert (
        STANDARD_PUMPSWAP_FEE_RECIPIENT_ID
        == "7VtfL8fvgNfhz17qKRMjzQEXgbdpnHHHQRh54R9jP2RJ"
    )
    assert str(STANDARD_PUMPSWAP_FEE_RECIPIENT) == STANDARD_PUMPSWAP_FEE_RECIPIENT_ID

    assert PUMPSWAP_BUY_DISCRIMINATOR == bytes([102, 6, 61, 18, 1, 218, 235, 234])
    assert PUMPSWAP_SELL_DISCRIMINATOR == bytes([51, 230, 133, 164, 1, 127, 131, 173])
    assert PUMPSWAP_BUY_EXACT_QUOTE_IN_DISCRIMINATOR == bytes(
        [198, 46, 21, 82, 180, 217, 232, 112]
    )
    assert PUMPSWAP_POOL_DISCRIMINATOR == bytes([241, 154, 109, 4, 17, 177, 109, 188])
    assert PUMPSWAP_POOL_DATA_MIN_SIZE == 243


# --- 2. PDA Derivations ---


def test_pda_derivations_deterministic() -> None:
    """Verify all 7 PumpSwap PDA derivations are deterministic and produce valid Pubkeys."""
    mint_pk = Pubkey.from_string(SAMPLE_MINT)
    user_pk = Pubkey.from_string(SAMPLE_USER)
    creator_pk = Pubkey.from_string(SAMPLE_CREATOR)

    # 1. Pool Authority
    auth_str = derive_pool_authority(SAMPLE_MINT)
    auth_pk = derive_pool_authority(mint_pk)
    assert auth_str == auth_pk
    assert isinstance(auth_str, Pubkey)

    # 2. AMM Pool
    pool_str = derive_amm_pool(SAMPLE_MINT)
    pool_pk = derive_amm_pool(mint_pk)
    assert pool_str == pool_pk
    assert isinstance(pool_str, Pubkey)

    # 3. AMM Pool V2
    pool_v2_str = derive_amm_pool_v2(SAMPLE_MINT)
    pool_v2_pk = derive_amm_pool_v2(mint_pk)
    assert pool_v2_str == pool_v2_pk
    assert isinstance(pool_v2_str, Pubkey)

    # 4. Creator Vault
    vault_str = derive_amm_creator_vault(SAMPLE_CREATOR)
    vault_pk = derive_amm_creator_vault(creator_pk)
    assert vault_str == vault_pk
    assert isinstance(vault_str, Pubkey)

    # 5. Fee Config
    fee_cfg = derive_amm_fee_config()
    assert isinstance(fee_cfg, Pubkey)

    # 6. Global Volume Accumulator
    g_vol = derive_amm_global_volume_accumulator()
    assert isinstance(g_vol, Pubkey)

    # 7. User Volume Accumulator
    u_vol_str = derive_amm_user_volume_accumulator(SAMPLE_USER)
    u_vol_pk = derive_amm_user_volume_accumulator(user_pk)
    assert u_vol_str == u_vol_pk
    assert isinstance(u_vol_str, Pubkey)


# --- 3. Binary Pool Decoder ---


def _make_dummy_pool_bytes() -> tuple[bytes, dict[str, Any]]:
    """Construct a synthetic valid 253-byte binary pool buffer."""
    disc = PUMPSWAP_POOL_DISCRIMINATOR
    bump = bytes([254])
    index = struct.pack("<H", 42)
    creator = bytes([10] * 32)
    base_mint = bytes([11] * 32)
    quote_mint = bytes([12] * 32)
    lp_mint = bytes([13] * 32)
    pool_base = bytes([14] * 32)
    pool_quote = bytes([15] * 32)
    lp_supply = struct.pack("<Q", 1_000_000_000)
    coin_creator = bytes([16] * 32)
    is_mayhem_mode = bytes([1])
    is_cashback_coin = bytes([0])

    raw = (
        disc
        + bump
        + index
        + creator
        + base_mint
        + quote_mint
        + lp_mint
        + pool_base
        + pool_quote
        + lp_supply
        + coin_creator
        + is_mayhem_mode
        + is_cashback_coin
    )
    expected = {
        "pool_bump": 254,
        "index": 42,
        "creator": Pubkey.from_bytes(creator),
        "base_mint": Pubkey.from_bytes(base_mint),
        "quote_mint": Pubkey.from_bytes(quote_mint),
        "lp_mint": Pubkey.from_bytes(lp_mint),
        "pool_base_token_account": Pubkey.from_bytes(pool_base),
        "pool_quote_token_account": Pubkey.from_bytes(pool_quote),
        "lp_supply": 1_000_000_000,
        "coin_creator": Pubkey.from_bytes(coin_creator),
        "is_mayhem_mode": True,
        "is_cashback_coin": False,
    }
    return raw, expected


def test_parse_pumpswap_pool_data_valid() -> None:
    """Verify parsing a valid binary PumpSwap pool payload."""
    raw_bytes, expected = _make_dummy_pool_bytes()
    parsed = parse_pumpswap_pool_data(raw_bytes)

    assert parsed["pool_bump"] == expected["pool_bump"]
    assert parsed["index"] == expected["index"]
    assert parsed["creator"] == expected["creator"]
    assert parsed["base_mint"] == expected["base_mint"]
    assert parsed["quote_mint"] == expected["quote_mint"]
    assert parsed["lp_mint"] == expected["lp_mint"]
    assert parsed["pool_base_token_account"] == expected["pool_base_token_account"]
    assert parsed["pool_quote_token_account"] == expected["pool_quote_token_account"]
    assert parsed["lp_supply"] == expected["lp_supply"]
    assert parsed["coin_creator"] == expected["coin_creator"]
    assert parsed["is_mayhem_mode"] == expected["is_mayhem_mode"]
    assert parsed["is_cashback_coin"] == expected["is_cashback_coin"]


def test_parse_pumpswap_pool_data_too_short() -> None:
    """Verify parse_pumpswap_pool_data raises ValueError on truncated data."""
    with pytest.raises(ValueError, match="shorter than minimum"):
        parse_pumpswap_pool_data(b"\x00" * 100)


def test_parse_pumpswap_pool_data_invalid_discriminator() -> None:
    """Verify parse_pumpswap_pool_data raises ValueError on invalid discriminator."""
    raw_bytes, _ = _make_dummy_pool_bytes()
    corrupted = bytes([0] * 8) + raw_bytes[8:]
    with pytest.raises(ValueError, match="Invalid PumpSwap pool discriminator"):
        parse_pumpswap_pool_data(corrupted, validate_discriminator=True)


# --- 4. Instruction Builders ---


def test_build_pumpswap_buy_instructions() -> None:
    """Verify build_pumpswap_buy_instructions creates expected instructions."""
    _, pool = _make_dummy_pool_bytes()
    pool_address = derive_amm_pool(SAMPLE_MINT)

    ixs = build_pumpswap_buy_instructions(
        user=SAMPLE_USER,
        pool_address=pool_address,
        pool=pool,
        max_sol_in=100_000_000,  # 0.1 SOL
        amount_out=1_000_000,
    )

    # Expected: 5 instructions (3 idempotent ATA creates + 1 SOL transfer + 1 swap buy)
    assert len(ixs) == 5
    buy_swap_ix = ixs[4]
    assert buy_swap_ix.program_id == PUMP_AMM_PROGRAM
    assert buy_swap_ix.data.startswith(PUMPSWAP_BUY_DISCRIMINATOR)
    # Buy instruction layout requires exactly 24 accounts
    assert len(buy_swap_ix.accounts) == 24


def test_build_pumpswap_sell_instructions() -> None:
    """Verify build_pumpswap_sell_instructions creates expected instructions."""
    _, pool = _make_dummy_pool_bytes()
    pool_address = derive_amm_pool(SAMPLE_MINT)

    ixs = build_pumpswap_sell_instructions(
        user=SAMPLE_USER,
        pool_address=pool_address,
        pool=pool,
        token_amount=500_000,
        min_sol_out=10_000_000,
    )

    # Expected: 2 instructions (create WSOL ATA + sell swap)
    assert len(ixs) == 2
    sell_swap_ix = ixs[1]
    assert sell_swap_ix.program_id == PUMP_AMM_PROGRAM
    assert sell_swap_ix.data.startswith(PUMPSWAP_SELL_DISCRIMINATOR)
    # Sell instruction layout requires exactly 22 accounts
    assert len(sell_swap_ix.accounts) == 22


def test_build_pumpswap_buy_exact_quote_in_instructions() -> None:
    """Verify build_pumpswap_buy_exact_quote_in_instructions creates expected instructions."""
    _, pool = _make_dummy_pool_bytes()
    pool_address = derive_amm_pool(SAMPLE_MINT)

    ixs = build_pumpswap_buy_exact_quote_in_instructions(
        user=SAMPLE_USER,
        pool_address=pool_address,
        pool=pool,
        spendable_quote_in=100_000_000,  # 0.1 SOL
        min_base_amount_out=500_000,
    )

    # Expected: 5 instructions (3 idempotent ATA creates + 1 SOL transfer + 1 swap buy)
    assert len(ixs) == 5
    buy_swap_ix = ixs[4]
    assert buy_swap_ix.program_id == PUMP_AMM_PROGRAM
    assert buy_swap_ix.data.startswith(PUMPSWAP_BUY_EXACT_QUOTE_IN_DISCRIMINATOR)
    # Buy instruction layout requires exactly 24 accounts
    assert len(buy_swap_ix.accounts) == 24


def test_build_pumpswap_sell_instructions_with_unwrap_sol() -> None:
    """Verify build_pumpswap_sell_instructions appends close_account when unwrap_sol=True."""
    _, pool = _make_dummy_pool_bytes()
    pool_address = derive_amm_pool(SAMPLE_MINT)

    ixs = build_pumpswap_sell_instructions(
        user=SAMPLE_USER,
        pool_address=pool_address,
        pool=pool,
        token_amount=500_000,
        min_sol_out=10_000_000,
        unwrap_sol=True,
    )

    # Expected: 3 instructions (create WSOL ATA + sell swap + close WSOL ATA)
    assert len(ixs) == 3
    sell_swap_ix = ixs[1]
    assert sell_swap_ix.program_id == PUMP_AMM_PROGRAM
    close_ix = ixs[2]
    assert close_ix.program_id == TOKEN_PROGRAM


# --- 5. AutoRouter Venue Detection ---


def test_detect_venue_from_bonding_curve_data() -> None:
    """Verify pure venue detection from bonding curve bytes."""
    # Complete: byte at offset 48 is 1
    data_complete = bytearray(BONDING_CURVE_MIN_SIZE)
    data_complete[BONDING_CURVE_COMPLETE_OFFSET] = 1
    assert (
        detect_venue_from_bonding_curve_data(bytes(data_complete))
        == RouteVenue.PUMPSWAP_AMM
    )

    # Incomplete: byte at offset 48 is 0
    data_incomplete = bytearray(BONDING_CURVE_MIN_SIZE)
    data_incomplete[BONDING_CURVE_COMPLETE_OFFSET] = 0
    assert (
        detect_venue_from_bonding_curve_data(bytes(data_incomplete))
        == RouteVenue.BONDING_CURVE
    )

    # None or short
    assert detect_venue_from_bonding_curve_data(None) == RouteVenue.BONDING_CURVE
    assert detect_venue_from_bonding_curve_data(b"") == RouteVenue.BONDING_CURVE


def test_detect_venue_from_pool_data() -> None:
    """Verify pure venue detection from PumpSwap AMM pool bytes."""
    pool_bytes, _ = _make_dummy_pool_bytes()
    assert detect_venue_from_pool_data(pool_bytes) == RouteVenue.PUMPSWAP_AMM
    assert detect_venue_from_pool_data(b"\x00" * 10) == RouteVenue.BONDING_CURVE
    assert detect_venue_from_pool_data(b"\x00" * 250) == RouteVenue.BONDING_CURVE
    assert detect_venue_from_pool_data(None) == RouteVenue.BONDING_CURVE
