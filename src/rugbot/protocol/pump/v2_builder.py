"""Pure Pump.fun V2 instruction construction.

The account order in this module follows the official Pump public documents:
``buy_v2`` has 27 accounts and ``sell_v2`` has 26 accounts.  This module is
pure: it does not perform RPC, signing, or transaction submission.
"""

# Protocol identifiers are intentionally literal allowlist constants.
# ruff: noqa: S105, TC001

from __future__ import annotations

import struct
from dataclasses import dataclass, field
from typing import Final, Literal

from solders.instruction import AccountMeta, Instruction
from solders.pubkey import Pubkey
from spl.token.instructions import (
    create_idempotent_associated_token_account,
    get_associated_token_address,
)

from rugbot.protocol.pump.bonding_curve_account import PUMP_PROGRAM_ID
from rugbot.protocol.pump.create_decoder import WSOL_MINT_ID
from rugbot.protocol.pump.global_account import PumpGlobalAccount
from rugbot.protocol.pump.trade_decoder import (
    BUY_V2_ACCOUNT_NAMES,
    BUY_V2_DISCRIMINATOR,
    SELL_V2_ACCOUNT_NAMES,
    SELL_V2_DISCRIMINATOR,
)

ASSOCIATED_TOKEN_PROGRAM_ID: Final[str] = "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL"
SYSTEM_PROGRAM_ID: Final[str] = "11111111111111111111111111111111"
TOKEN_PROGRAM_ID: Final[str] = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
FEE_PROGRAM_ID: Final[str] = "pfeeUxB6jkeY1Hxd7CsFCAjcbHA9rWtchMGdZ6VojVZ"
GLOBAL_PDA: Final[str] = "4wTV1YmiEkRvAtNtsSGPtUrqRYQMe5SKy2uB4Jjaxnjf"
EVENT_AUTHORITY_SEED: Final[bytes] = b"__event_authority"
BONDING_CURVE_SEED: Final[bytes] = b"bonding-curve"
CREATOR_VAULT_SEED: Final[bytes] = b"creator-vault"
SHARING_CONFIG_SEED: Final[bytes] = b"sharing-config"
GLOBAL_VOLUME_SEED: Final[bytes] = b"global_volume_accumulator"
USER_VOLUME_SEED: Final[bytes] = b"user_volume_accumulator"
FEE_CONFIG_SEED: Final[bytes] = b"fee_config"

TradeSide = Literal["buy", "sell"]


@dataclass(frozen=True, slots=True)
class PumpV2BuildContext:
    """Validated account and amount context for one Pump V2 instruction."""

    mint: Pubkey
    creator: Pubkey
    user: Pubkey
    base_token_program: Pubkey
    fee_recipient: Pubkey
    buyback_fee_recipient: Pubkey
    amount: int
    quote_limit: int
    quote_mint: Pubkey = field(default_factory=lambda: Pubkey.from_string(WSOL_MINT_ID))
    quote_token_program: Pubkey = field(
        default_factory=lambda: Pubkey.from_string(TOKEN_PROGRAM_ID)
    )


@dataclass(frozen=True, slots=True)
class PumpV2InstructionSet:
    """Instruction set and canonical account names for one V2 trade."""

    side: TradeSide
    instructions: tuple[Instruction, ...]
    trade_instruction: Instruction
    account_names: tuple[str, ...]


def build_buy_v2_instructions(context: PumpV2BuildContext) -> PumpV2InstructionSet:
    """Build ATA preparation plus a documented ``buy_v2`` instruction."""

    accounts = _buy_accounts(context)
    base_ata = accounts[14]
    quote_ata = accounts[15]
    ata_instructions = (
        create_idempotent_associated_token_account(
            context.user,
            context.user,
            context.mint,
            context.base_token_program,
        ),
        create_idempotent_associated_token_account(
            context.user,
            context.user,
            context.quote_mint,
            context.quote_token_program,
        ),
    )
    del base_ata, quote_ata
    trade = Instruction(
        Pubkey.from_string(PUMP_PROGRAM_ID),
        _trade_data(BUY_V2_DISCRIMINATOR, context.amount, context.quote_limit),
        _metas(accounts, BUY_V2_ACCOUNT_NAMES, context.user),
    )
    return PumpV2InstructionSet(
        side="buy",
        instructions=(*ata_instructions, trade),
        trade_instruction=trade,
        account_names=BUY_V2_ACCOUNT_NAMES,
    )


def build_sell_v2_instructions(context: PumpV2BuildContext) -> PumpV2InstructionSet:
    """Build a documented ``sell_v2`` instruction."""

    accounts = _sell_accounts(context)
    trade = Instruction(
        Pubkey.from_string(PUMP_PROGRAM_ID),
        _trade_data(SELL_V2_DISCRIMINATOR, context.amount, context.quote_limit),
        _metas(accounts, SELL_V2_ACCOUNT_NAMES, context.user),
    )
    return PumpV2InstructionSet(
        side="sell",
        instructions=(trade,),
        trade_instruction=trade,
        account_names=SELL_V2_ACCOUNT_NAMES,
    )


def derive_pump_pda(seed: bytes, *values: Pubkey) -> Pubkey:
    """Derive one PDA under the Pump program without RPC."""

    return Pubkey.find_program_address(
        [seed, *(bytes(value) for value in values)],
        Pubkey.from_string(PUMP_PROGRAM_ID),
    )[0]


def derive_fee_pda(seed: bytes, *values: Pubkey) -> Pubkey:
    """Derive one PDA under the Pump fee program without RPC."""

    return Pubkey.find_program_address(
        [seed, *(bytes(value) for value in values)],
        Pubkey.from_string(FEE_PROGRAM_ID),
    )[0]


def select_fee_recipients(global_state: PumpGlobalAccount) -> tuple[Pubkey, Pubkey]:
    """Select documented normal and buyback recipients from finalized Global state."""

    fee_recipient = Pubkey.from_string(global_state.fee_recipient_pubkey)
    if not global_state.buyback_fee_recipients:
        raise ValueError("Pump Global has no buyback fee recipient")  # noqa: TRY003
    buyback_recipient = Pubkey.from_string(global_state.buyback_fee_recipients[0])
    return fee_recipient, buyback_recipient


def _buy_accounts(context: PumpV2BuildContext) -> tuple[Pubkey, ...]:
    return _common_accounts(context, include_user_volume=True)


def _sell_accounts(context: PumpV2BuildContext) -> tuple[Pubkey, ...]:
    common = list(_common_accounts(context, include_user_volume=True))
    del common[19]
    return tuple(common)


def _common_accounts(
    context: PumpV2BuildContext,
    *,
    include_user_volume: bool,
) -> tuple[Pubkey, ...]:
    pump_program = Pubkey.from_string(PUMP_PROGRAM_ID)
    fee_program = Pubkey.from_string(FEE_PROGRAM_ID)
    system_program = Pubkey.from_string(SYSTEM_PROGRAM_ID)
    associated_program = Pubkey.from_string(ASSOCIATED_TOKEN_PROGRAM_ID)
    global_account = Pubkey.from_string(GLOBAL_PDA)
    bonding_curve = derive_pump_pda(BONDING_CURVE_SEED, context.mint)
    global_volume = derive_pump_pda(GLOBAL_VOLUME_SEED)
    user_volume = derive_pump_pda(USER_VOLUME_SEED, context.user)
    fee_config = derive_fee_pda(FEE_CONFIG_SEED, pump_program)
    sharing_config = derive_fee_pda(SHARING_CONFIG_SEED, context.mint)
    event_authority = derive_pump_pda(EVENT_AUTHORITY_SEED)
    creator_vault = derive_pump_pda(CREATOR_VAULT_SEED, context.creator)

    base_bonding_ata = get_associated_token_address(
        bonding_curve,
        context.mint,
        context.base_token_program,
    )
    quote_bonding_ata = get_associated_token_address(
        bonding_curve,
        context.quote_mint,
        context.quote_token_program,
    )
    base_user_ata = get_associated_token_address(
        context.user,
        context.mint,
        context.base_token_program,
    )
    quote_user_ata = get_associated_token_address(
        context.user,
        context.quote_mint,
        context.quote_token_program,
    )
    quote_fee_ata = get_associated_token_address(
        context.fee_recipient,
        context.quote_mint,
        context.quote_token_program,
    )
    quote_buyback_ata = get_associated_token_address(
        context.buyback_fee_recipient,
        context.quote_mint,
        context.quote_token_program,
    )
    creator_quote_ata = get_associated_token_address(
        creator_vault,
        context.quote_mint,
        context.quote_token_program,
    )
    user_volume_ata = get_associated_token_address(
        user_volume,
        context.quote_mint,
        context.quote_token_program,
    )

    accounts = (
        global_account,
        context.mint,
        context.quote_mint,
        context.base_token_program,
        context.quote_token_program,
        associated_program,
        context.fee_recipient,
        quote_fee_ata,
        context.buyback_fee_recipient,
        quote_buyback_ata,
        bonding_curve,
        base_bonding_ata,
        quote_bonding_ata,
        context.user,
        base_user_ata,
        quote_user_ata,
        creator_vault,
        creator_quote_ata,
        sharing_config,
        global_volume,
        user_volume,
        user_volume_ata,
        fee_config,
        fee_program,
        system_program,
        event_authority,
        pump_program,
    )
    if not include_user_volume:
        return accounts
    return accounts


def _trade_data(discriminator: bytes, amount: int, quote_limit: int) -> bytes:
    if type(amount) is not int or amount <= 0:
        raise ValueError("trade amount must be a positive integer")  # noqa: TRY003
    if type(quote_limit) is not int or quote_limit <= 0:
        raise ValueError("trade quote limit must be a positive integer")  # noqa: TRY003
    return discriminator + struct.pack("<QQ", amount, quote_limit)


def _metas(
    accounts: tuple[Pubkey, ...],
    names: tuple[str, ...],
    user: Pubkey,
) -> list[AccountMeta]:
    if len(accounts) != len(names):
        raise ValueError("Pump V2 account list does not match official layout")  # noqa: TRY003
    writable = {
        "associated_quote_fee_recipient",
        "associated_quote_buyback_fee_recipient",
        "bonding_curve",
        "associated_base_bonding_curve",
        "associated_quote_bonding_curve",
        "user",
        "associated_base_user",
        "associated_quote_user",
        "creator_vault",
        "associated_creator_vault",
        "global_volume_accumulator",
        "user_volume_accumulator",
        "associated_user_volume_accumulator",
    }
    return [
        AccountMeta(
            pubkey=pubkey,
            is_signer=name == "user",
            is_writable=name in writable or (pubkey == user and name == "user"),
        )
        for name, pubkey in zip(names, accounts, strict=True)
    ]


__all__ = [
    "ASSOCIATED_TOKEN_PROGRAM_ID",
    "FEE_PROGRAM_ID",
    "GLOBAL_PDA",
    "PUMP_PROGRAM_ID",
    "PumpV2BuildContext",
    "PumpV2InstructionSet",
    "build_buy_v2_instructions",
    "build_sell_v2_instructions",
    "derive_fee_pda",
    "derive_pump_pda",
    "select_fee_recipients",
]
