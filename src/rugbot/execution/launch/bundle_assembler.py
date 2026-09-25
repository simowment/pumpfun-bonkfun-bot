"""Atomic Jito bundle assembly for Pump.fun token launch and first buy."""

# ruff: noqa: PLR0913, PLR0915, TRY003, TC002

from __future__ import annotations

import base64
from dataclasses import dataclass
from typing import Final

from solders.hash import Hash
from solders.instruction import Instruction
from solders.keypair import Keypair
from solders.message import Message
from solders.pubkey import Pubkey
from solders.transaction import Transaction

from rugbot.execution.create_builder import build_create_v2_instruction
from rugbot.execution.sender.jito import (
    JITO_FALLBACK_TIP_ACCOUNTS,
    create_jito_tip_instruction,
)
from rugbot.execution.v2_builder import (
    PumpV2BuildContext,
    build_buy_v2_instructions,
)
from rugbot.ingest.pump.create_decoder import SPL_2022_PROGRAM_ID
from rugbot.utils.logger import get_logger

logger = get_logger(__name__)

PUMP_CANONICAL_FEE_RECIPIENT: Final[str] = (
    "CebN5WGQ4jvEPvsVU4EoHEpgzq1VV7AbicfhtW4xC9iM"
)
PUMP_CANONICAL_BUYBACK_RECIPIENT: Final[str] = (
    "FWsPcmCoPD5N4t5iQdfNuimkWupq55nnJyTFsmvvwGia"
)
INITIAL_VIRTUAL_TOKEN_RESERVES: Final[int] = 1_073_000_000_000_000
INITIAL_VIRTUAL_SOL_RESERVES: Final[int] = 30_000_000_000
SOLANA_TX_MTU_BYTES: Final[int] = 1232
LAMPORTS_PER_SOL: Final[int] = 1_000_000_000


@dataclass(frozen=True, slots=True)
class AssembledLaunchBundle:
    """Atomic Jito bundle containing token creation and optional first buy."""

    mint_pubkey: str
    payer_pubkey: str
    name: str
    symbol: str
    uri: str
    transactions: tuple[Transaction, ...]
    transactions_base64: tuple[str, ...]
    wire_bytes_list: tuple[bytes, ...]
    wire_sizes: tuple[int, ...]
    max_wire_size_bytes: int
    fits_mtu: bool
    expected_tokens: int
    buy_sol_lamports: int
    jito_tip_lamports: int
    instructions_count: int


def calculate_initial_buy_tokens(sol_lamports: int) -> int:
    """Calculate exact token units received on block 0 for a given SOL amount."""
    if sol_lamports <= 0:
        return 0
    # 100 bps protocol fee deducted on Pump
    net_sol = sol_lamports * 9900 // 10000
    return (INITIAL_VIRTUAL_TOKEN_RESERVES * net_sol) // (
        INITIAL_VIRTUAL_SOL_RESERVES + net_sol
    )


def assemble_launch_bundle(
    *,
    payer: Keypair,
    mint: Keypair,
    name: str,
    symbol: str,
    uri: str,
    recent_blockhash: Hash,
    creator: Pubkey | None = None,
    buy_sol_lamports: int | None = None,
    jito_tip_lamports: int = 3_000_000,
    jito_tip_account: Pubkey | None = None,
    mayhem_mode: bool = False,
    cashback: bool = False,
    fee_recipient: Pubkey | None = None,
    buyback_recipient: Pubkey | None = None,
) -> AssembledLaunchBundle:
    """Assemble create_v2 and buy_v2 into an atomic, sequential Jito bundle.

    Constructs 1 or 2 sequential transactions that fit strictly within Solana's
    1232-byte MTU limit:
    - If buy_sol_lamports is 0/None: Single transaction containing [create_v2, tip].
    - If buy_sol_lamports > 0:
        Tx 1: Token Creation [create_v2] signed by [payer, mint].
        Tx 2: Dev Purchase & MEV Tip [ATAs, buy_v2, tip] signed by [payer].
    Both transactions execute atomically in the same block via Jito Block Engine.
    """
    if not name:
        raise ValueError("Token name cannot be empty")
    if not symbol:
        raise ValueError("Token symbol cannot be empty")
    if not uri:
        raise ValueError("Metadata URI cannot be empty")

    creator_pubkey = creator if creator is not None else payer.pubkey()
    payer_pubkey = payer.pubkey()
    mint_pubkey = mint.pubkey()

    tip_lamports = max(0, int(jito_tip_lamports))
    tip_ix: Instruction | None = None
    if tip_lamports > 0:
        tip_pubkey = jito_tip_account
        if tip_pubkey is None:
            tip_pubkey = Pubkey.from_string(JITO_FALLBACK_TIP_ACCOUNTS[0])
        tip_ix = create_jito_tip_instruction(
            payer=payer_pubkey,
            tip_lamports=tip_lamports,
            tip_account=tip_pubkey,
        )

    # 1. Create Instruction
    create_ix = build_create_v2_instruction(
        payer=payer_pubkey,
        creator=creator_pubkey,
        mint=mint_pubkey,
        name=name,
        symbol=symbol,
        uri=uri,
        mayhem_mode=mayhem_mode,
        cashback=cashback,
    )

    buy_lamports = (
        int(buy_sol_lamports) if buy_sol_lamports and buy_sol_lamports > 0 else 0
    )
    expected_tokens = 0
    tx_list: list[Transaction] = []
    total_instructions = 0

    if buy_lamports == 0:
        # Single transaction bundle
        instructions = [create_ix]
        if tip_ix is not None:
            instructions.append(tip_ix)
        total_instructions = len(instructions)
        msg = Message(instructions, payer_pubkey)
        tx = Transaction([payer, mint], msg, recent_blockhash)
        tx_list.append(tx)
    else:
        # 2-Transaction Jito Bundle
        # Tx 1: Token creation
        msg1 = Message([create_ix], payer_pubkey)
        tx1 = Transaction([payer, mint], msg1, recent_blockhash)
        tx_list.append(tx1)

        # Tx 2: Dev purchase + Jito MEV tip
        expected_tokens = calculate_initial_buy_tokens(buy_lamports)
        if expected_tokens <= 0:
            raise ValueError(
                f"Calculated token amount too small for {buy_lamports} lamports"
            )

        actual_fee_recipient = (
            fee_recipient
            if fee_recipient is not None
            else Pubkey.from_string(PUMP_CANONICAL_FEE_RECIPIENT)
        )
        actual_buyback_recipient = (
            buyback_recipient
            if buyback_recipient is not None
            else Pubkey.from_string(PUMP_CANONICAL_BUYBACK_RECIPIENT)
        )

        buy_context = PumpV2BuildContext(
            mint=mint_pubkey,
            creator=creator_pubkey,
            user=payer_pubkey,
            base_token_program=Pubkey.from_string(SPL_2022_PROGRAM_ID),
            fee_recipient=actual_fee_recipient,
            buyback_fee_recipient=actual_buyback_recipient,
            amount=expected_tokens,
            quote_limit=buy_lamports,
        )
        buy_set = build_buy_v2_instructions(buy_context)
        buy_instructions = list(buy_set.instructions)
        if tip_ix is not None:
            buy_instructions.append(tip_ix)

        msg2 = Message(buy_instructions, payer_pubkey)
        tx2 = Transaction([payer], msg2, recent_blockhash)
        tx_list.append(tx2)
        total_instructions = 1 + len(buy_instructions)

    wire_bytes_list = tuple(bytes(tx) for tx in tx_list)
    wire_sizes = tuple(len(b) for b in wire_bytes_list)
    max_wire_size = max(wire_sizes) if wire_sizes else 0
    fits_mtu = all(size <= SOLANA_TX_MTU_BYTES for size in wire_sizes)

    b64_tx_list = tuple(base64.b64encode(b).decode("ascii") for b in wire_bytes_list)

    return AssembledLaunchBundle(
        mint_pubkey=str(mint_pubkey),
        payer_pubkey=str(payer_pubkey),
        name=name,
        symbol=symbol,
        uri=uri,
        transactions=tuple(tx_list),
        transactions_base64=b64_tx_list,
        wire_bytes_list=wire_bytes_list,
        wire_sizes=wire_sizes,
        max_wire_size_bytes=max_wire_size,
        fits_mtu=fits_mtu,
        expected_tokens=expected_tokens,
        buy_sol_lamports=buy_lamports,
        jito_tip_lamports=tip_lamports,
        instructions_count=total_instructions,
    )


__all__ = [
    "INITIAL_VIRTUAL_SOL_RESERVES",
    "INITIAL_VIRTUAL_TOKEN_RESERVES",
    "LAMPORTS_PER_SOL",
    "PUMP_CANONICAL_BUYBACK_RECIPIENT",
    "PUMP_CANONICAL_FEE_RECIPIENT",
    "SOLANA_TX_MTU_BYTES",
    "AssembledLaunchBundle",
    "assemble_launch_bundle",
    "calculate_initial_buy_tokens",
]
