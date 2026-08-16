"""Pump migration instruction verifier tests."""

import ast
import hashlib
import unittest
from dataclasses import dataclass
from pathlib import Path

from rugbot.domain.account_roles import AccountRoleProof
from rugbot.domain.amounts import Slot
from rugbot.domain.decisions import AbstainReason, AbstainResult
from rugbot.domain.migrations import PumpMigrationInstructionEvidence
from rugbot.protocol.pump.migration import (
    ASSOCIATED_SPL_PROGRAM_ID,
    MIGRATE_ACCOUNT_NAMES,
    MIGRATE_DISCRIMINATOR,
    PINNED_PUMP_IDL_SHA256,
    PINNED_PUMP_SWAP_IDL_SHA256,
    PUMP_AMM_PROGRAM_ID,
    PUMP_PROGRAM_ID,
    SPL_2022_PROGRAM_ID,
    SPL_PROGRAM_ID,
    SYSTEM_PROGRAM_ID,
    WSOL_MINT_ID,
    CompiledPumpMigrationInstruction,
    verify_pump_migration_instruction,
)

MIGRATION_MODULE = Path("src/rugbot/protocol/pump/migration.py")
PUMP_IDL_PATH = Path("idl/pump_fun_idl.json")
PUMP_SWAP_IDL_PATH = Path("idl/pump_swap_idl.json")
DEFAULT_ACCOUNT_PUBKEYS = ("__default_migration_account_pubkeys__",)
DEFAULT_ROLE_PROOFS = (AccountRoleProof("__default_migration_role_proofs__", ""),)
PROGRAM_INDEX = len(MIGRATE_ACCOUNT_NAMES)
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


class PumpMigrationVerifierTests(unittest.TestCase):
    """Tests for pinned Pump migration evidence verification."""

    def test_verifies_migration_instruction_evidence_without_canonical_pool(
        self,
    ) -> None:
        """A layout-valid migration records missing provenance evidence."""

        result = verify_pump_migration_instruction(
            _instruction(),
            pump_idl_hash=PINNED_PUMP_IDL_SHA256,
            pump_swap_idl_hash=PINNED_PUMP_SWAP_IDL_SHA256,
        )

        self.assertIsInstance(result, PumpMigrationInstructionEvidence)
        if isinstance(result, AbstainResult):
            self.fail(result.message)
        self.assertEqual(result.instruction_name, "migrate")
        self.assertEqual(result.program_id_index, PROGRAM_INDEX)
        self.assertEqual(result.mint_account_index, 2)
        self.assertEqual(result.pool_account_index, 9)
        self.assertEqual(result.pool_authority_account_index, 10)
        self.assertEqual(result.pump_amm_program_id, PUMP_AMM_PROGRAM_ID)
        self.assertEqual(result.quote_mint_pubkey, WSOL_MINT_ID)
        self.assertFalse(result.is_canonical_pool_verified)
        self.assertEqual(
            result.missing_evidence,
            (
                "canonical_pool_artifact",
                "transaction_slot_account_state_artifact",
                "migration_pda_derivation_artifact",
                "pump_swap_pool_config_artifact",
                "mint_pair_artifact",
            ),
        )

    def test_fixtureless_slice_never_claims_canonical_pool(self) -> None:
        """Boolean provenance is not enough to publish a canonical pool."""

        result = verify_pump_migration_instruction(
            _instruction(),
            pump_idl_hash=PINNED_PUMP_IDL_SHA256,
            pump_swap_idl_hash=PINNED_PUMP_SWAP_IDL_SHA256,
        )

        self.assertIsInstance(result, PumpMigrationInstructionEvidence)
        if isinstance(result, AbstainResult):
            self.fail(result.message)
        self.assertFalse(result.is_canonical_pool_verified)
        self.assertIn("canonical_pool_artifact", result.missing_evidence)

    def test_pump_idl_hash_mismatch_abstains(self) -> None:
        """Unknown Pump IDLs fail closed."""

        result = verify_pump_migration_instruction(
            _instruction(),
            pump_idl_hash="wrong",
            pump_swap_idl_hash=PINNED_PUMP_SWAP_IDL_SHA256,
        )

        self.assert_abstains(result, AbstainReason.DECODER_MISMATCH)

    def test_pump_swap_idl_hash_mismatch_abstains(self) -> None:
        """Unknown PumpSwap IDLs fail closed."""

        result = verify_pump_migration_instruction(
            _instruction(),
            pump_idl_hash=PINNED_PUMP_IDL_SHA256,
            pump_swap_idl_hash="wrong",
        )

        self.assert_abstains(result, AbstainReason.DECODER_MISMATCH)

    def test_decoder_version_mismatch_abstains(self) -> None:
        """Only the pinned migration verifier may label migration evidence."""

        result = verify_pump_migration_instruction(
            _instruction(),
            pump_idl_hash=PINNED_PUMP_IDL_SHA256,
            pump_swap_idl_hash=PINNED_PUMP_SWAP_IDL_SHA256,
            decoder_version="other-verifier",
        )

        self.assert_abstains(result, AbstainReason.DECODER_MISMATCH)

    def test_unknown_program_abstains(self) -> None:
        """Only the pinned Pump program can verify migration evidence."""

        result = verify_pump_migration_instruction(
            _instruction(
                overrides=_InstructionOverrides(
                    program_id="11111111111111111111111111111111"
                )
            ),
            pump_idl_hash=PINNED_PUMP_IDL_SHA256,
            pump_swap_idl_hash=PINNED_PUMP_SWAP_IDL_SHA256,
        )

        self.assert_abstains(result, AbstainReason.UNKNOWN_PROTOCOL_STATE)

    def test_wrong_discriminator_abstains(self) -> None:
        """Non-migrate instructions do not partially verify."""

        result = verify_pump_migration_instruction(
            _instruction(data=b"12345678"),
            pump_idl_hash=PINNED_PUMP_IDL_SHA256,
            pump_swap_idl_hash=PINNED_PUMP_SWAP_IDL_SHA256,
        )

        self.assert_abstains(result, AbstainReason.UNSUPPORTED_PROTOCOL_STATE)

    def test_account_count_mismatch_abstains(self) -> None:
        """The pinned migration layout requires the exact account count."""

        result = verify_pump_migration_instruction(
            _instruction(account_count=len(MIGRATE_ACCOUNT_NAMES) - 1),
            pump_idl_hash=PINNED_PUMP_IDL_SHA256,
            pump_swap_idl_hash=PINNED_PUMP_SWAP_IDL_SHA256,
        )

        self.assert_abstains(result, AbstainReason.UNSUPPORTED_PROTOCOL_STATE)

    def test_fixed_account_mismatch_abstains(self) -> None:
        """Fixed IDL account addresses must match the supplied pubkeys."""

        account_pubkeys = list(_account_pubkeys(len(MIGRATE_ACCOUNT_NAMES)))
        account_pubkeys[6] = "not-system-program"

        result = verify_pump_migration_instruction(
            _instruction(
                overrides=_InstructionOverrides(account_pubkeys=tuple(account_pubkeys))
            ),
            pump_idl_hash=PINNED_PUMP_IDL_SHA256,
            pump_swap_idl_hash=PINNED_PUMP_SWAP_IDL_SHA256,
        )

        self.assert_abstains(result, AbstainReason.UNSUPPORTED_PROTOCOL_STATE)

    def test_missing_role_proofs_abstains(self) -> None:
        """Every migration role needs pubkey proof before verification."""

        result = verify_pump_migration_instruction(
            _instruction(overrides=_InstructionOverrides(role_proofs=())),
            pump_idl_hash=PINNED_PUMP_IDL_SHA256,
            pump_swap_idl_hash=PINNED_PUMP_SWAP_IDL_SHA256,
        )

        self.assert_abstains(result, AbstainReason.UNSUPPORTED_PROTOCOL_STATE)

    def test_dynamic_role_order_mismatch_abstains(self) -> None:
        """Swapped dynamic roles fail closed."""

        proofs = list(_role_proofs(_account_pubkeys(len(MIGRATE_ACCOUNT_NAMES))))
        proofs[2] = AccountRoleProof("mint", "not-the-mint-at-index-2")

        result = verify_pump_migration_instruction(
            _instruction(overrides=_InstructionOverrides(role_proofs=tuple(proofs))),
            pump_idl_hash=PINNED_PUMP_IDL_SHA256,
            pump_swap_idl_hash=PINNED_PUMP_SWAP_IDL_SHA256,
        )

        self.assert_abstains(result, AbstainReason.UNSUPPORTED_PROTOCOL_STATE)

    def test_program_index_mismatch_abstains(self) -> None:
        """Program ID index must resolve to the Pump program ID."""

        result = verify_pump_migration_instruction(
            _instruction(overrides=_InstructionOverrides(program_id_index=0)),
            pump_idl_hash=PINNED_PUMP_IDL_SHA256,
            pump_swap_idl_hash=PINNED_PUMP_SWAP_IDL_SHA256,
        )

        self.assert_abstains(result, AbstainReason.UNSUPPORTED_PROTOCOL_STATE)

    def test_checked_in_idls_match_pinned_hashes(self) -> None:
        """Verifier pins match the local IDL artifacts."""

        pump_hash = hashlib.sha256(PUMP_IDL_PATH.read_bytes()).hexdigest()
        pump_swap_hash = hashlib.sha256(PUMP_SWAP_IDL_PATH.read_bytes()).hexdigest()

        self.assertEqual(pump_hash, PINNED_PUMP_IDL_SHA256)
        self.assertEqual(pump_swap_hash, PINNED_PUMP_SWAP_IDL_SHA256)

    def test_verifier_does_not_import_adapters_or_float(self) -> None:
        """The migration verifier stays pure and integer-only."""

        source = MIGRATION_MODULE.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(MIGRATION_MODULE))
        violations = [
            imported_name
            for imported_name in _imported_module_names(tree)
            if imported_name.startswith(FORBIDDEN_IMPORT_PREFIXES)
        ]

        self.assertEqual(violations, [])
        self.assertNotIn("float", source)

    def assert_abstains(
        self,
        result: object,
        reason: AbstainReason,
    ) -> None:
        self.assertIsInstance(result, AbstainResult)
        if isinstance(result, AbstainResult):
            self.assertEqual(result.reason, reason)
            self.assertEqual(result.as_of_slot, 456)


@dataclass(frozen=True, slots=True)
class _InstructionOverrides:
    program_id: str = PUMP_PROGRAM_ID
    program_id_index: int | None = PROGRAM_INDEX
    account_pubkeys: tuple[str, ...] | None = DEFAULT_ACCOUNT_PUBKEYS
    role_proofs: tuple[AccountRoleProof, ...] = DEFAULT_ROLE_PROOFS


def _instruction(
    *,
    account_count: int = len(MIGRATE_ACCOUNT_NAMES),
    data: bytes = MIGRATE_DISCRIMINATOR,
    overrides: _InstructionOverrides | None = None,
) -> CompiledPumpMigrationInstruction:
    instruction_overrides = overrides or _InstructionOverrides()
    account_pubkeys = _resolve_account_pubkeys(
        instruction_overrides.account_pubkeys,
        account_count,
        instruction_overrides.program_id,
    )
    role_proofs = _resolve_role_proofs(instruction_overrides.role_proofs, account_count)

    return CompiledPumpMigrationInstruction(
        as_of_slot=Slot(456),
        program_id=instruction_overrides.program_id,
        program_id_index=instruction_overrides.program_id_index,
        account_indices=tuple(range(account_count)),
        account_pubkeys=account_pubkeys,
        account_role_proofs=role_proofs,
        data=data,
        transaction_index=7,
        outer_instruction_index=8,
        signature=b"migration-sig",
    )


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


def _resolve_role_proofs(
    role_proofs: tuple[AccountRoleProof, ...],
    account_count: int,
) -> tuple[AccountRoleProof, ...]:
    if role_proofs != DEFAULT_ROLE_PROOFS:
        return role_proofs
    return _role_proofs(_account_pubkeys(account_count))


def _role_proofs(account_pubkeys: tuple[str, ...]) -> tuple[AccountRoleProof, ...]:
    return tuple(
        AccountRoleProof(name=name, pubkey=account_pubkeys[index])
        for index, name in enumerate(MIGRATE_ACCOUNT_NAMES)
        if index < len(account_pubkeys)
    )


def _account_pubkeys(account_count: int) -> tuple[str, ...]:
    account_pubkeys = [f"migration-account-{index}" for index in range(account_count)]
    fixed_positions = {
        6: SYSTEM_PROGRAM_ID,
        7: SPL_PROGRAM_ID,
        8: PUMP_AMM_PROGRAM_ID,
        14: WSOL_MINT_ID,
        19: SPL_2022_PROGRAM_ID,
        20: ASSOCIATED_SPL_PROGRAM_ID,
        23: PUMP_PROGRAM_ID,
    }
    for index, pubkey in fixed_positions.items():
        if index < len(account_pubkeys):
            account_pubkeys[index] = pubkey
    return tuple(account_pubkeys)


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
