"""Pure pre-signature firewall for Pump V2 execution."""

# The firewall deliberately keeps all checks in one auditable pure function.
# ruff: noqa: C901, PLR0912, PLR2004, TRY003, TC002, TC003

from __future__ import annotations

import struct
from collections.abc import Iterable
from dataclasses import dataclass

from solders.instruction import Instruction
from solders.pubkey import Pubkey

from rugbot.protocol.pump.bonding_curve_account import PUMP_PROGRAM_ID
from rugbot.protocol.pump.trade_decoder import (
    BUY_V2_ACCOUNT_NAMES,
    BUY_V2_DISCRIMINATOR,
    SELL_V2_ACCOUNT_NAMES,
    SELL_V2_DISCRIMINATOR,
)
from rugbot.protocol.pump.v2_builder import (
    ASSOCIATED_TOKEN_PROGRAM_ID,
    FEE_PROGRAM_ID,
    SYSTEM_PROGRAM_ID,
)

COMPUTE_BUDGET_PROGRAM_ID = "ComputeBudget111111111111111111111111111111"
SYSTEM_TRANSFER_TAG = 2


@dataclass(frozen=True, slots=True)
class FirewallPolicy:
    """Allowlist and hard caps applied before any signer is called."""

    payer: Pubkey
    mint: Pubkey
    max_tip_lamports: int
    allowed_tip_accounts: frozenset[Pubkey]
    expected_pump_accounts: tuple[Pubkey, ...] | None = None


class TransactionFirewallError(ValueError):
    """Raised when a transaction violates the execution allowlist."""


def validate_pump_v2_instructions(
    instructions: Iterable[Instruction],
    *,
    policy: FirewallPolicy,
) -> tuple[Instruction, ...]:
    """Validate a complete unsigned instruction list before signing.

    Only compute-budget instructions, ATA preparation, one Pump V2 trade, and
    explicitly allowed Jito System transfers are accepted.  No RPC or database
    access occurs here.
    """

    checked = tuple(instructions)
    if not checked:
        raise TransactionFirewallError("transaction has no instructions")
    pump_instructions = [
        instruction
        for instruction in checked
        if str(instruction.program_id) == PUMP_PROGRAM_ID
    ]
    if len(pump_instructions) != 1:
        raise TransactionFirewallError(
            "transaction must contain exactly one Pump V2 instruction"
        )
    pump = pump_instructions[0]
    side, expected_names = _trade_schema(pump)
    del side
    if len(pump.accounts) != len(expected_names):
        raise TransactionFirewallError(
            "Pump V2 account count does not match the pinned layout"
        )
    if pump.accounts[13].pubkey != policy.payer or not pump.accounts[13].is_signer:
        raise TransactionFirewallError(
            "Pump V2 user must be the configured payer signer"
        )
    if pump.accounts[1].pubkey != policy.mint:
        raise TransactionFirewallError(
            "Pump V2 mint does not match the configured target"
        )
    if not pump.accounts[13].is_writable:
        raise TransactionFirewallError("Pump V2 payer must be writable")
    if len(pump.data) != 24:
        raise TransactionFirewallError("Pump V2 trade data length is unsupported")
    amount, quote_limit = struct.unpack("<QQ", bytes(pump.data[8:]))
    if amount <= 0 or quote_limit <= 0:
        raise TransactionFirewallError("Pump V2 trade amounts must be positive")
    if (
        policy.expected_pump_accounts is not None
        and tuple(meta.pubkey for meta in pump.accounts)
        != policy.expected_pump_accounts
    ):
        raise TransactionFirewallError("Pump V2 account derivation does not match")

    compute_tags: set[int] = set()
    for instruction in checked:
        program_id = str(instruction.program_id)
        if program_id == COMPUTE_BUDGET_PROGRAM_ID:
            tag = _validate_compute_budget(instruction)
            if tag in compute_tags:
                raise TransactionFirewallError(
                    "duplicate Compute Budget instruction type"
                )
            compute_tags.add(tag)
        elif program_id == ASSOCIATED_TOKEN_PROGRAM_ID:
            _validate_associated_token_instruction(instruction, policy.payer)
        elif program_id == PUMP_PROGRAM_ID:
            if instruction is not pump:
                raise TransactionFirewallError(
                    "multiple Pump instructions are forbidden"
                )
        elif program_id == SYSTEM_PROGRAM_ID:
            _validate_system_transfer(instruction, policy)
        else:
            raise TransactionFirewallError(f"program {program_id} is not allowlisted")
    _validate_fixed_accounts(pump, expected_names)
    return checked


def _trade_schema(
    instruction: Instruction,
) -> tuple[str, tuple[str, ...]]:
    discriminator = bytes(instruction.data[:8])
    if discriminator == BUY_V2_DISCRIMINATOR:
        return "buy", BUY_V2_ACCOUNT_NAMES
    if discriminator == SELL_V2_DISCRIMINATOR:
        return "sell", SELL_V2_ACCOUNT_NAMES
    raise TransactionFirewallError("Pump instruction is not buy_v2 or sell_v2")


def _validate_fixed_accounts(
    instruction: Instruction,
    names: tuple[str, ...],
) -> None:
    expected = {
        "associated_token_program": ASSOCIATED_TOKEN_PROGRAM_ID,
        "fee_program": FEE_PROGRAM_ID,
        "system_program": SYSTEM_PROGRAM_ID,
        "program": PUMP_PROGRAM_ID,
    }
    for index, name in enumerate(names):
        expected_pubkey = expected.get(name)
        if (
            expected_pubkey is not None
            and str(instruction.accounts[index].pubkey) != expected_pubkey
        ):
            raise TransactionFirewallError(f"fixed Pump account {name} does not match")


def _validate_associated_token_instruction(
    instruction: Instruction,
    payer: Pubkey,
) -> None:
    if not instruction.accounts or instruction.accounts[0].pubkey != payer:
        raise TransactionFirewallError(
            "ATA instruction payer is not the configured payer"
        )
    if not instruction.accounts[0].is_signer or not instruction.accounts[0].is_writable:
        raise TransactionFirewallError("ATA instruction payer metadata is unsafe")


def _validate_system_transfer(
    instruction: Instruction,
    policy: FirewallPolicy,
) -> None:
    if len(instruction.accounts) != 2 or len(instruction.data) != 12:
        raise TransactionFirewallError("unexpected System instruction")
    tag, lamports = struct.unpack("<IQ", bytes(instruction.data))
    recipient = instruction.accounts[1].pubkey
    if tag != SYSTEM_TRANSFER_TAG or recipient not in policy.allowed_tip_accounts:
        raise TransactionFirewallError(
            "System transfer recipient is not an allowed tip account"
        )
    if lamports < 0 or lamports > policy.max_tip_lamports:
        raise TransactionFirewallError("System tip exceeds the configured maximum")
    if (
        instruction.accounts[0].pubkey != policy.payer
        or not instruction.accounts[0].is_signer
    ):
        raise TransactionFirewallError("System tip payer is not the configured signer")


def _validate_compute_budget(instruction: Instruction) -> int:
    data = bytes(instruction.data)
    lengths = {2: 5, 3: 9, 4: 5}
    if not data or data[0] not in lengths:
        raise TransactionFirewallError("unsupported Compute Budget instruction")
    if len(data) != lengths[data[0]]:
        raise TransactionFirewallError("Compute Budget instruction length is invalid")
    return data[0]


__all__ = [
    "FirewallPolicy",
    "TransactionFirewallError",
    "validate_pump_v2_instructions",
]
