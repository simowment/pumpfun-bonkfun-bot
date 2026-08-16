"""Pump trade instruction decoder tests."""

import ast
import hashlib
import struct
import unittest
from dataclasses import dataclass
from pathlib import Path

from rugbot.domain.amounts import QuoteBaseUnits, Slot, TokenBaseUnits
from rugbot.domain.decisions import AbstainReason, AbstainResult
from rugbot.domain.trades import PumpTradeInstructionEvidence, TradeSide
from rugbot.protocol.pump.trade_decoder import (
    BUY_ACCOUNT_NAMES,
    BUY_DISCRIMINATOR,
    PINNED_PUMP_IDL_SHA256,
    PUMP_FEE_PROGRAM_ID,
    PUMP_PROGRAM_ID,
    SELL_ACCOUNT_NAMES,
    SELL_DISCRIMINATOR,
    SYSTEM_PROGRAM_ID,
    AccountRoleProof,
    CompiledPumpInstruction,
    decode_pump_trade_instruction,
)

DECODER_MODULE = Path("src/rugbot/protocol/pump/trade_decoder.py")
PUMP_IDL_PATH = Path("idl/pump_fun_idl.json")
BUY_EXACT_QUOTE_IN_DISCRIMINATOR = bytes([56, 252, 116, 8, 158, 223, 205, 95])
FORBIDDEN_IMPORT_PREFIXES = (
    "rugbot.ingest",
    "rugbot.storage",
    "rugbot.execution",
    "src.core",
    "src.trading",
    "src.platforms",
    "solana",
    "solders",
    "dotenv",
)
DEFAULT_ACCOUNT_PUBKEYS = ("__default_account_pubkeys__",)
DEFAULT_ROLE_PROOFS = (AccountRoleProof("__default_account_role_proofs__", ""),)
SYSTEM_PROGRAM_POSITION = 7
PROGRAM_POSITION = 11
SELL_ACCOUNT_COUNT = 14
BUY_ACCOUNT_COUNT = 16
SELL_FEE_PROGRAM_POSITION = 13
BUY_FEE_PROGRAM_POSITION = 15


class PumpTradeDecoderTests(unittest.TestCase):
    """Tests for pinned Pump trade instruction evidence decoding."""

    def test_decodes_buy_instruction_args_and_indices(self) -> None:
        """The buy layout preserves account indices and integer args."""

        result = decode_pump_trade_instruction(
            _instruction(
                account_count=16,
                data=BUY_DISCRIMINATOR + _u64(123) + _u64(456) + b"\x01",
            ),
            idl_hash=PINNED_PUMP_IDL_SHA256,
        )

        self.assertIsInstance(result, PumpTradeInstructionEvidence)
        if isinstance(result, AbstainResult):
            self.fail(result.message)
        self.assertEqual(result.instruction_name, "buy")
        self.assertEqual(result.side, TradeSide.BUY)
        self.assertEqual(result.base_amount_base_units, TokenBaseUnits(123))
        self.assertEqual(result.max_quote_cost_base_units, QuoteBaseUnits(456))
        self.assertTrue(result.track_volume)
        self.assertEqual(result.mint_account_index, 2)
        self.assertEqual(result.token_program_account_index, 8)
        self.assertEqual(result.fee_config_account_index, 14)
        self.assertEqual(result.idl_hash, PINNED_PUMP_IDL_SHA256)
        self.assertEqual(result.program_id_index, 16)
        self.assertEqual(
            result.account_pubkeys, (*_account_pubkeys(16), PUMP_PROGRAM_ID)
        )
        self.assertEqual(
            result.account_role_proofs[2],
            ("mint", "account-2"),
        )
        self.assertEqual(result.signature, b"sig")
        self.assertEqual(result.missing_evidence, ("transaction_slot_account_state",))

    def test_decodes_sell_instruction_args_and_indices(self) -> None:
        """The sell layout preserves account indices and integer args."""

        result = decode_pump_trade_instruction(
            _instruction(
                account_count=14,
                data=SELL_DISCRIMINATOR + _u64(321) + _u64(654),
            ),
            idl_hash=PINNED_PUMP_IDL_SHA256,
        )

        self.assertIsInstance(result, PumpTradeInstructionEvidence)
        if isinstance(result, AbstainResult):
            self.fail(result.message)
        self.assertEqual(result.instruction_name, "sell")
        self.assertEqual(result.side, TradeSide.SELL)
        self.assertEqual(result.base_amount_base_units, TokenBaseUnits(321))
        self.assertEqual(result.min_quote_output_base_units, QuoteBaseUnits(654))
        self.assertEqual(result.token_program_account_index, 9)
        self.assertEqual(result.fee_config_account_index, 12)

    def test_preserves_remaining_accounts_without_relabeling_them(self) -> None:
        """Extra account indices are preserved as remaining accounts."""

        result = decode_pump_trade_instruction(
            _instruction(
                account_count=18,
                data=BUY_DISCRIMINATOR + _u64(1) + _u64(2) + b"\x00",
            ),
            idl_hash=PINNED_PUMP_IDL_SHA256,
        )

        self.assertIsInstance(result, PumpTradeInstructionEvidence)
        if isinstance(result, AbstainResult):
            self.fail(result.message)
        self.assertEqual(result.remaining_account_indices, (16, 17))
        self.assertFalse(result.track_volume)

    def test_account_state_evidence_flag_clears_missing_evidence(self) -> None:
        """Transaction-slot account state evidence is recorded when present."""

        result = decode_pump_trade_instruction(
            _instruction(
                account_count=16,
                data=BUY_DISCRIMINATOR + _u64(1) + _u64(2) + b"\x00",
                overrides=_InstructionOverrides(
                    transaction_slot_account_state_available=True
                ),
            ),
            idl_hash=PINNED_PUMP_IDL_SHA256,
        )

        self.assertIsInstance(result, PumpTradeInstructionEvidence)
        if isinstance(result, AbstainResult):
            self.fail(result.message)
        self.assertTrue(result.transaction_slot_account_state_available)
        self.assertEqual(result.missing_evidence, ())

    def test_idl_hash_mismatch_abstains(self) -> None:
        """The decoder refuses unpinned IDL hashes."""

        result = decode_pump_trade_instruction(
            _instruction(
                account_count=16,
                data=BUY_DISCRIMINATOR + _u64(1) + _u64(2) + b"\x00",
            ),
            idl_hash="wrong",
        )

        self.assert_abstains(result, AbstainReason.DECODER_MISMATCH)

    def test_decoder_version_mismatch_abstains(self) -> None:
        """Only the pinned trade decoder may label instruction evidence."""

        result = decode_pump_trade_instruction(
            _instruction(
                account_count=16,
                data=BUY_DISCRIMINATOR + _u64(1) + _u64(2) + b"\x00",
            ),
            idl_hash=PINNED_PUMP_IDL_SHA256,
            decoder_version="other-decoder",
        )

        self.assert_abstains(result, AbstainReason.DECODER_MISMATCH)

    def test_unknown_program_abstains(self) -> None:
        """Unknown programs do not decode as Pump trades."""

        result = decode_pump_trade_instruction(
            _instruction(
                account_count=16,
                data=BUY_DISCRIMINATOR + _u64(1) + _u64(2) + b"\x00",
                overrides=_InstructionOverrides(
                    program_id="11111111111111111111111111111111"
                ),
            ),
            idl_hash=PINNED_PUMP_IDL_SHA256,
        )

        self.assert_abstains(result, AbstainReason.UNKNOWN_PROTOCOL_STATE)

    def test_program_id_index_mismatch_abstains(self) -> None:
        """Resolved program account must match the instruction program ID."""

        result = decode_pump_trade_instruction(
            _instruction(
                account_count=16,
                data=BUY_DISCRIMINATOR + _u64(1) + _u64(2) + b"\x00",
                overrides=_InstructionOverrides(program_id_index=0),
            ),
            idl_hash=PINNED_PUMP_IDL_SHA256,
        )

        self.assert_abstains(result, AbstainReason.UNSUPPORTED_PROTOCOL_STATE)

    def test_account_index_outside_pubkeys_abstains(self) -> None:
        """Resolved account keys must cover every compiled account index."""

        result = decode_pump_trade_instruction(
            _instruction(
                account_count=16,
                data=BUY_DISCRIMINATOR + _u64(1) + _u64(2) + b"\x00",
                overrides=_InstructionOverrides(account_pubkeys=_account_pubkeys(14)),
            ),
            idl_hash=PINNED_PUMP_IDL_SHA256,
        )

        self.assert_abstains(result, AbstainReason.UNSUPPORTED_PROTOCOL_STATE)

    def test_missing_resolved_account_pubkeys_abstains(self) -> None:
        """Layout proof is required before labeling account roles."""

        result = decode_pump_trade_instruction(
            _instruction(
                account_count=16,
                data=BUY_DISCRIMINATOR + _u64(1) + _u64(2) + b"\x00",
                overrides=_InstructionOverrides(account_pubkeys=None),
            ),
            idl_hash=PINNED_PUMP_IDL_SHA256,
        )

        self.assert_abstains(result, AbstainReason.UNSUPPORTED_PROTOCOL_STATE)

    def test_missing_account_role_proofs_abstains(self) -> None:
        """Every required dynamic role needs layout proof."""

        result = decode_pump_trade_instruction(
            _instruction(
                account_count=16,
                data=BUY_DISCRIMINATOR + _u64(1) + _u64(2) + b"\x00",
                overrides=_InstructionOverrides(account_role_proofs=()),
            ),
            idl_hash=PINNED_PUMP_IDL_SHA256,
        )

        self.assert_abstains(result, AbstainReason.UNSUPPORTED_PROTOCOL_STATE)

    def test_changed_fixed_account_order_abstains(self) -> None:
        """Changed account order fails closed instead of relabeling roles."""

        account_pubkeys = list(_account_pubkeys(16))
        account_pubkeys[7] = "not-system-program"
        result = decode_pump_trade_instruction(
            _instruction(
                account_count=16,
                data=BUY_DISCRIMINATOR + _u64(1) + _u64(2) + b"\x00",
                overrides=_InstructionOverrides(account_pubkeys=tuple(account_pubkeys)),
            ),
            idl_hash=PINNED_PUMP_IDL_SHA256,
        )

        self.assert_abstains(result, AbstainReason.UNSUPPORTED_PROTOCOL_STATE)

    def test_unsupported_discriminator_abstains(self) -> None:
        """Unknown instruction discriminators do not partially decode."""

        result = decode_pump_trade_instruction(
            _instruction(account_count=16, data=b"12345678" + _u64(1)),
            idl_hash=PINNED_PUMP_IDL_SHA256,
        )

        self.assert_abstains(result, AbstainReason.UNSUPPORTED_PROTOCOL_STATE)

    def test_buy_exact_quote_in_is_not_claimed_as_m0_006_coverage(self) -> None:
        """Non-contracted buy routes abstain until fixture-backed support exists."""

        result = decode_pump_trade_instruction(
            _instruction(
                account_count=16,
                data=BUY_EXACT_QUOTE_IN_DISCRIMINATOR + _u64(1) + _u64(2),
            ),
            idl_hash=PINNED_PUMP_IDL_SHA256,
        )

        self.assert_abstains(result, AbstainReason.UNSUPPORTED_PROTOCOL_STATE)

    def test_truncated_data_abstains(self) -> None:
        """Truncated instruction bytes abstain."""

        result = decode_pump_trade_instruction(
            _instruction(account_count=16, data=BUY_DISCRIMINATOR + _u64(1)),
            idl_hash=PINNED_PUMP_IDL_SHA256,
        )

        self.assert_abstains(result, AbstainReason.UNSUPPORTED_PROTOCOL_STATE)

    def test_missing_accounts_abstains(self) -> None:
        """Required account index omissions abstain."""

        result = decode_pump_trade_instruction(
            _instruction(
                account_count=15,
                data=BUY_DISCRIMINATOR + _u64(1) + _u64(2) + b"\x00",
            ),
            idl_hash=PINNED_PUMP_IDL_SHA256,
        )

        self.assert_abstains(result, AbstainReason.UNSUPPORTED_PROTOCOL_STATE)

    def test_invalid_bool_abstains(self) -> None:
        """Anchor bool values other than 0 or 1 abstain."""

        result = decode_pump_trade_instruction(
            _instruction(
                account_count=16,
                data=BUY_DISCRIMINATOR + _u64(1) + _u64(2) + b"\x02",
            ),
            idl_hash=PINNED_PUMP_IDL_SHA256,
        )

        self.assert_abstains(result, AbstainReason.UNSUPPORTED_PROTOCOL_STATE)

    def test_zero_trade_amounts_abstain(self) -> None:
        """Zero-amount instructions cannot become actionable trade evidence."""

        cases = (
            BUY_DISCRIMINATOR + _u64(0) + _u64(1) + b"\x00",
            BUY_DISCRIMINATOR + _u64(1) + _u64(0) + b"\x00",
            SELL_DISCRIMINATOR + _u64(0) + _u64(1),
        )
        for data in cases:
            with self.subTest(data=data):
                account_count = 16 if data.startswith(BUY_DISCRIMINATOR) else 14
                result = decode_pump_trade_instruction(
                    _instruction(account_count=account_count, data=data),
                    idl_hash=PINNED_PUMP_IDL_SHA256,
                )
                self.assert_abstains(result, AbstainReason.UNSUPPORTED_PROTOCOL_STATE)

    def test_decoder_does_not_import_adapters_or_float(self) -> None:
        """The protocol decoder stays pure and integer-only."""

        source = DECODER_MODULE.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(DECODER_MODULE))
        imported_names = _imported_module_names(tree)
        violations = [
            imported_name
            for imported_name in imported_names
            if imported_name.startswith(FORBIDDEN_IMPORT_PREFIXES)
        ]

        self.assertEqual(violations, [])
        self.assertNotIn("float", source)

    def test_checked_in_pump_idl_matches_pinned_hash(self) -> None:
        """The decoder pin matches the local Pump IDL artifact."""

        idl_hash = hashlib.sha256(PUMP_IDL_PATH.read_bytes()).hexdigest()

        self.assertEqual(idl_hash, PINNED_PUMP_IDL_SHA256)

    def assert_abstains(
        self,
        result: object,
        reason: AbstainReason,
    ) -> None:
        self.assertIsInstance(result, AbstainResult)
        if isinstance(result, AbstainResult):
            self.assertEqual(result.reason, reason)
            self.assertEqual(result.as_of_slot, 123)


@dataclass(frozen=True, slots=True)
class _InstructionOverrides:
    program_id: str = PUMP_PROGRAM_ID
    program_id_index: int | None = None
    account_pubkeys: tuple[str, ...] | None = DEFAULT_ACCOUNT_PUBKEYS
    account_role_proofs: tuple[AccountRoleProof, ...] = DEFAULT_ROLE_PROOFS
    transaction_slot_account_state_available: bool = False


def _instruction(
    *,
    account_count: int,
    data: bytes,
    overrides: _InstructionOverrides | None = None,
) -> CompiledPumpInstruction:
    instruction_overrides = overrides or _InstructionOverrides()
    resolved_program_index = instruction_overrides.program_id_index
    resolved_account_pubkeys = _resolve_account_pubkeys(
        instruction_overrides.account_pubkeys,
        account_count,
        instruction_overrides.program_id,
    )
    resolved_role_proofs = _resolve_account_role_proofs(
        instruction_overrides.account_role_proofs,
        account_count,
    )
    if resolved_program_index is None:
        resolved_program_index = account_count
    return CompiledPumpInstruction(
        as_of_slot=Slot(123),
        program_id=instruction_overrides.program_id,
        account_indices=tuple(range(account_count)),
        data=data,
        transaction_index=3,
        outer_instruction_index=4,
        program_id_index=resolved_program_index,
        account_pubkeys=resolved_account_pubkeys,
        account_role_proofs=resolved_role_proofs,
        signature=b"sig",
        transaction_slot_account_state_available=(
            instruction_overrides.transaction_slot_account_state_available
        ),
    )


def _u64(value: int) -> bytes:
    return struct.pack("<Q", value)


def _account_pubkeys(account_count: int) -> tuple[str, ...]:
    account_pubkeys = [f"account-{index}" for index in range(account_count)]
    if account_count > SYSTEM_PROGRAM_POSITION:
        account_pubkeys[SYSTEM_PROGRAM_POSITION] = SYSTEM_PROGRAM_ID
    if account_count > PROGRAM_POSITION:
        account_pubkeys[PROGRAM_POSITION] = PUMP_PROGRAM_ID
    if account_count == SELL_ACCOUNT_COUNT:
        account_pubkeys[SELL_FEE_PROGRAM_POSITION] = PUMP_FEE_PROGRAM_ID
    if account_count >= BUY_ACCOUNT_COUNT:
        account_pubkeys[BUY_FEE_PROGRAM_POSITION] = PUMP_FEE_PROGRAM_ID
    return tuple(account_pubkeys)


def _resolve_account_pubkeys(
    account_pubkeys: tuple[str, ...] | None,
    account_count: int,
    program_id: str,
) -> tuple[str, ...] | None:
    if account_pubkeys is None:
        return None
    if account_pubkeys != DEFAULT_ACCOUNT_PUBKEYS:
        return account_pubkeys
    return (*_account_pubkeys(account_count), program_id)


def _resolve_account_role_proofs(
    role_proofs: tuple[AccountRoleProof, ...],
    account_count: int,
) -> tuple[AccountRoleProof, ...]:
    if role_proofs != DEFAULT_ROLE_PROOFS:
        return role_proofs

    account_names = BUY_ACCOUNT_NAMES
    if account_count == SELL_ACCOUNT_COUNT:
        account_names = SELL_ACCOUNT_NAMES
    account_pubkeys = _account_pubkeys(account_count)
    return tuple(
        AccountRoleProof(name=name, pubkey=account_pubkeys[index])
        for index, name in enumerate(account_names)
        if index < len(account_pubkeys)
    )


def _imported_module_names(tree: ast.AST) -> list[str]:
    names: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            names.append(node.module)
    return names


if __name__ == "__main__":
    unittest.main()
