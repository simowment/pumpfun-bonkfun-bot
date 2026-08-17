"""Pump create_v2 instruction decoder tests."""

import ast
import hashlib
import struct
import unittest
from dataclasses import dataclass
from pathlib import Path

import base58

from rugbot.domain.account_roles import AccountRoleProof
from rugbot.domain.amounts import Slot
from rugbot.domain.decisions import AbstainReason, AbstainResult
from rugbot.domain.launches import (
    LaunchActorProof,
    LaunchActorRole,
    LaunchCreatedV2,
)
from rugbot.protocol.pump.create_decoder import (
    ASSOCIATED_SPL_PROGRAM_ID,
    CREATE_V2_ACCOUNT_NAMES,
    CREATE_V2_DISCRIMINATOR,
    MAYHEM_PROGRAM_ID,
    PINNED_PUMP_IDL_SHA256,
    PUMP_PROGRAM_ID,
    SPL_2022_PROGRAM_ID,
    SYSTEM_PROGRAM_ID,
    WSOL_MINT_ID,
    CompiledPumpCreateV2Instruction,
    decode_pump_create_v2_instruction,
)

DECODER_MODULE = Path("src/rugbot/protocol/pump/create_decoder.py")
PUMP_IDL_PATH = Path("idl/pump_fun_idl.json")
DEFAULT_ACCOUNT_PUBKEYS = ("__default_create_account_pubkeys__",)
DEFAULT_ROLE_PROOFS = (AccountRoleProof("__default_create_role_proofs__", ""),)
PROGRAM_INDEX = len(CREATE_V2_ACCOUNT_NAMES)
FEE_PAYER_INDEX = PROGRAM_INDEX + 1
FIRST_BUYER_INDEX = PROGRAM_INDEX + 2
CREATOR_BYTES = bytes(range(32))
CREATOR_PUBKEY = base58.b58encode(CREATOR_BYTES).decode("ascii")
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


class PumpCreateV2DecoderTests(unittest.TestCase):
    """Tests for pinned Pump create_v2 launch evidence decoding."""

    def test_decodes_omitted_option_bool_as_disabled(self) -> None:
        """The optional trailing OptionBool may be omitted on the wire."""

        result = decode_pump_create_v2_instruction(
            _instruction(data=_create_data(is_cashback_enabled=b"")),
            idl_hash=PINNED_PUMP_IDL_SHA256,
        )

        self.assertIsInstance(result, LaunchCreatedV2)
        if isinstance(result, AbstainResult):
            self.fail(result.message)
        self.assertFalse(result.is_cashback_enabled)

    def test_decodes_create_v2_args_accounts_and_distinct_actors(self) -> None:
        """The decoder preserves IDL roles and supplied actor evidence."""

        result = decode_pump_create_v2_instruction(
            _instruction(
                overrides=_InstructionOverrides(
                    actor_role_proofs=_actor_proofs(),
                    transaction_slot_account_state_available=True,
                )
            ),
            idl_hash=PINNED_PUMP_IDL_SHA256,
        )

        self.assertIsInstance(result, LaunchCreatedV2)
        if isinstance(result, AbstainResult):
            self.fail(result.message)
        self.assertEqual(result.instruction_name, "create_v2")
        self.assertEqual(result.creation_instruction_type, "create_v2")
        self.assertEqual(result.program_id_index, PROGRAM_INDEX)
        self.assertEqual(result.launch_id, "create-account-0")
        self.assertEqual(result.mint_account_index, 0)
        self.assertEqual(result.bonding_curve_account_index, 2)
        self.assertEqual(result.user_account_index, 5)
        self.assertEqual(result.creator_pubkey, CREATOR_PUBKEY)
        self.assertEqual(result.user_pubkey, "create-account-5")
        self.assertEqual(result.fee_payer_pubkey, "fee-payer")
        self.assertEqual(result.first_buyer_pubkey, "first-buyer")
        self.assertEqual(result.name, "Test Coin")
        self.assertEqual(result.symbol, "TST")
        self.assertEqual(result.uri, "ipfs://metadata")
        self.assertTrue(result.is_mayhem_mode)
        self.assertFalse(result.is_cashback_enabled)
        self.assertEqual(result.base_token_program_pubkey, SPL_2022_PROGRAM_ID)
        self.assertEqual(result.quote_asset, "SOL")
        self.assertEqual(result.quote_mint_pubkey, WSOL_MINT_ID)
        self.assertEqual(result.quote_token_program_pubkey, SYSTEM_PROGRAM_ID)
        self.assertEqual(result.missing_evidence, ())
        self.assertEqual(result.idl_hash, PINNED_PUMP_IDL_SHA256)
        self.assertEqual(result.account_role_proofs[5], ("user", "create-account-5"))
        self.assertEqual(
            result.actor_role_proofs,
            (
                (
                    "fee_payer",
                    FEE_PAYER_INDEX,
                    "fee-payer",
                    ("transaction-message:fee-payer",),
                    "transaction-message-v1",
                ),
                (
                    "first_buyer",
                    FIRST_BUYER_INDEX,
                    "first-buyer",
                    ("first-fill:first-buyer",),
                    "first-fill-v1",
                ),
            ),
        )

    def test_missing_fee_payer_and_first_buyer_are_not_collapsed_to_user(self) -> None:
        """Unknown actors stay missing instead of being inferred from user."""

        result = decode_pump_create_v2_instruction(
            _instruction(),
            idl_hash=PINNED_PUMP_IDL_SHA256,
        )

        self.assertIsInstance(result, LaunchCreatedV2)
        if isinstance(result, AbstainResult):
            self.fail(result.message)
        self.assertIsNone(result.fee_payer_pubkey)
        self.assertIsNone(result.first_buyer_pubkey)
        self.assertEqual(
            result.missing_evidence,
            ("fee_payer", "first_buyer", "transaction_slot_account_state"),
        )

    def test_redecode_is_deterministic(self) -> None:
        """The same finalized evidence decodes to the same immutable object."""

        instruction = _instruction()

        first = decode_pump_create_v2_instruction(
            instruction,
            idl_hash=PINNED_PUMP_IDL_SHA256,
        )
        second = decode_pump_create_v2_instruction(
            instruction,
            idl_hash=PINNED_PUMP_IDL_SHA256,
        )

        self.assertEqual(first, second)

    def test_idl_hash_mismatch_abstains(self) -> None:
        """Unknown IDLs fail closed."""

        result = decode_pump_create_v2_instruction(
            _instruction(),
            idl_hash="wrong",
        )

        self.assert_abstains(result, AbstainReason.DECODER_MISMATCH)

    def test_decoder_version_mismatch_abstains(self) -> None:
        """Only the pinned decoder version may label launch evidence."""

        result = decode_pump_create_v2_instruction(
            _instruction(),
            idl_hash=PINNED_PUMP_IDL_SHA256,
            decoder_version="wrong-version",
        )

        self.assert_abstains(result, AbstainReason.DECODER_MISMATCH)

    def test_unknown_program_abstains(self) -> None:
        """Only the pinned Pump program can decode create_v2 evidence."""

        result = decode_pump_create_v2_instruction(
            _instruction(
                overrides=_InstructionOverrides(
                    program_id="11111111111111111111111111111111"
                )
            ),
            idl_hash=PINNED_PUMP_IDL_SHA256,
        )

        self.assert_abstains(result, AbstainReason.UNKNOWN_PROTOCOL_STATE)

    def test_wrong_discriminator_abstains(self) -> None:
        """Non-create_v2 instructions do not partially decode."""

        result = decode_pump_create_v2_instruction(
            _instruction(data=b"12345678"),
            idl_hash=PINNED_PUMP_IDL_SHA256,
        )

        self.assert_abstains(result, AbstainReason.UNSUPPORTED_PROTOCOL_STATE)

    def test_truncated_string_abstains(self) -> None:
        """Truncated Anchor string data abstains."""

        result = decode_pump_create_v2_instruction(
            _instruction(data=CREATE_V2_DISCRIMINATOR + struct.pack("<I", 4) + b"ab"),
            idl_hash=PINNED_PUMP_IDL_SHA256,
        )

        self.assert_abstains(result, AbstainReason.UNSUPPORTED_PROTOCOL_STATE)

    def test_invalid_utf8_string_abstains(self) -> None:
        """Invalid UTF-8 metadata fields abstain."""

        result = decode_pump_create_v2_instruction(
            _instruction(
                data=(
                    CREATE_V2_DISCRIMINATOR
                    + struct.pack("<I", 1)
                    + b"\xff"
                    + _string("TST")
                    + _string("ipfs://metadata")
                    + CREATOR_BYTES
                    + b"\x00\x00"
                )
            ),
            idl_hash=PINNED_PUMP_IDL_SHA256,
        )

        self.assert_abstains(result, AbstainReason.UNSUPPORTED_PROTOCOL_STATE)

    def test_invalid_bool_abstains(self) -> None:
        """Anchor bool values other than 0 or 1 abstain."""

        result = decode_pump_create_v2_instruction(
            _instruction(data=_create_data(is_mayhem_mode=b"\x02")),
            idl_hash=PINNED_PUMP_IDL_SHA256,
        )

        self.assert_abstains(result, AbstainReason.UNSUPPORTED_PROTOCOL_STATE)

    def test_invalid_cashback_bool_abstains(self) -> None:
        """The OptionBool struct byte must be a supported bool."""

        result = decode_pump_create_v2_instruction(
            _instruction(data=_create_data(is_cashback_enabled=b"\x02")),
            idl_hash=PINNED_PUMP_IDL_SHA256,
        )

        self.assert_abstains(result, AbstainReason.UNSUPPORTED_PROTOCOL_STATE)

    def test_account_count_mismatch_abstains(self) -> None:
        """The pinned create_v2 layout requires the exact account count."""

        result = decode_pump_create_v2_instruction(
            _instruction(account_count=len(CREATE_V2_ACCOUNT_NAMES) - 1),
            idl_hash=PINNED_PUMP_IDL_SHA256,
        )

        self.assert_abstains(result, AbstainReason.UNSUPPORTED_PROTOCOL_STATE)

    def test_changed_fixed_account_abstains(self) -> None:
        """Fixed IDL account addresses must match supplied pubkeys."""

        account_pubkeys = list(_account_pubkeys(len(CREATE_V2_ACCOUNT_NAMES)))
        account_pubkeys[7] = "not-token-2022"

        result = decode_pump_create_v2_instruction(
            _instruction(
                overrides=_InstructionOverrides(account_pubkeys=tuple(account_pubkeys))
            ),
            idl_hash=PINNED_PUMP_IDL_SHA256,
        )

        self.assert_abstains(result, AbstainReason.UNSUPPORTED_PROTOCOL_STATE)

    def test_missing_role_proofs_abstains(self) -> None:
        """Every create_v2 role needs pubkey proof before labeling."""

        result = decode_pump_create_v2_instruction(
            _instruction(overrides=_InstructionOverrides(role_proofs=())),
            idl_hash=PINNED_PUMP_IDL_SHA256,
        )

        self.assert_abstains(result, AbstainReason.UNSUPPORTED_PROTOCOL_STATE)

    def test_dynamic_role_order_mismatch_abstains(self) -> None:
        """Swapped dynamic roles fail closed."""

        proofs = list(_role_proofs(_account_pubkeys(len(CREATE_V2_ACCOUNT_NAMES))))
        proofs[5] = AccountRoleProof("user", "not-the-user-at-index-5")

        result = decode_pump_create_v2_instruction(
            _instruction(overrides=_InstructionOverrides(role_proofs=tuple(proofs))),
            idl_hash=PINNED_PUMP_IDL_SHA256,
        )

        self.assert_abstains(result, AbstainReason.UNSUPPORTED_PROTOCOL_STATE)

    def test_program_index_mismatch_abstains(self) -> None:
        """Program ID index must resolve to the Pump program."""

        result = decode_pump_create_v2_instruction(
            _instruction(overrides=_InstructionOverrides(program_id_index=0)),
            idl_hash=PINNED_PUMP_IDL_SHA256,
        )

        self.assert_abstains(result, AbstainReason.UNSUPPORTED_PROTOCOL_STATE)

    def test_actor_proof_index_outside_account_keys_abstains(self) -> None:
        """Supplied external actor proofs must resolve to account keys."""

        result = decode_pump_create_v2_instruction(
            _instruction(
                overrides=_InstructionOverrides(
                    actor_role_proofs=(
                        _actor_proof(
                            role=LaunchActorRole.FEE_PAYER,
                            account_index=FIRST_BUYER_INDEX + 10,
                            pubkey="fee-payer",
                        ),
                    )
                )
            ),
            idl_hash=PINNED_PUMP_IDL_SHA256,
        )

        self.assert_abstains(result, AbstainReason.UNSUPPORTED_PROTOCOL_STATE)

    def test_actor_proof_pubkey_mismatch_abstains(self) -> None:
        """Actor proofs cannot relabel an arbitrary account index."""

        result = decode_pump_create_v2_instruction(
            _instruction(
                overrides=_InstructionOverrides(
                    actor_role_proofs=(
                        _actor_proof(
                            role=LaunchActorRole.FIRST_BUYER,
                            account_index=5,
                            pubkey="not-the-user-pubkey",
                        ),
                    )
                )
            ),
            idl_hash=PINNED_PUMP_IDL_SHA256,
        )

        self.assert_abstains(result, AbstainReason.UNSUPPORTED_PROTOCOL_STATE)

    def test_actor_proof_missing_evidence_abstains(self) -> None:
        """A pubkey and index are not sufficient actor provenance."""

        result = decode_pump_create_v2_instruction(
            _instruction(
                overrides=_InstructionOverrides(
                    actor_role_proofs=(
                        _actor_proof(
                            role=LaunchActorRole.FEE_PAYER,
                            evidence_ids=(),
                        ),
                    )
                )
            ),
            idl_hash=PINNED_PUMP_IDL_SHA256,
        )

        self.assert_abstains(result, AbstainReason.MISSING_FEATURE)

    def test_actor_proof_slot_mismatch_abstains(self) -> None:
        """Actor proofs are point-in-time evidence."""

        result = decode_pump_create_v2_instruction(
            _instruction(
                overrides=_InstructionOverrides(
                    actor_role_proofs=(
                        _actor_proof(
                            role=LaunchActorRole.FEE_PAYER,
                            as_of_slot=Slot(788),
                        ),
                    )
                )
            ),
            idl_hash=PINNED_PUMP_IDL_SHA256,
        )

        self.assert_abstains(result, AbstainReason.STALE_STATE)

    def test_duplicate_actor_proof_abstains(self) -> None:
        """Each non-IDL actor role has one explicit proof at most."""

        result = decode_pump_create_v2_instruction(
            _instruction(
                overrides=_InstructionOverrides(
                    actor_role_proofs=(
                        _actor_proof(role=LaunchActorRole.FEE_PAYER),
                        _actor_proof(role=LaunchActorRole.FEE_PAYER),
                    )
                )
            ),
            idl_hash=PINNED_PUMP_IDL_SHA256,
        )

        self.assert_abstains(result, AbstainReason.UNSUPPORTED_PROTOCOL_STATE)

    def test_checked_in_pump_idl_matches_pinned_hash(self) -> None:
        """The decoder pin matches the local Pump IDL artifact."""

        idl_hash = hashlib.sha256(PUMP_IDL_PATH.read_bytes()).hexdigest()

        self.assertEqual(idl_hash, PINNED_PUMP_IDL_SHA256)

    def test_decoder_does_not_import_adapters_or_use_binary_numeric_shortcuts(
        self,
    ) -> None:
        """The protocol decoder stays pure and integer-only."""

        source = DECODER_MODULE.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(DECODER_MODULE))
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
            self.assertEqual(result.as_of_slot, 789)


@dataclass(frozen=True, slots=True)
class _InstructionOverrides:
    program_id: str = PUMP_PROGRAM_ID
    program_id_index: int | None = PROGRAM_INDEX
    account_pubkeys: tuple[str, ...] | None = DEFAULT_ACCOUNT_PUBKEYS
    role_proofs: tuple[AccountRoleProof, ...] = DEFAULT_ROLE_PROOFS
    actor_role_proofs: tuple[LaunchActorProof, ...] = ()
    transaction_slot_account_state_available: bool = False


def _instruction(
    *,
    account_count: int = len(CREATE_V2_ACCOUNT_NAMES),
    data: bytes | None = None,
    overrides: _InstructionOverrides | None = None,
) -> CompiledPumpCreateV2Instruction:
    instruction_overrides = overrides or _InstructionOverrides()
    account_pubkeys = _resolve_account_pubkeys(
        instruction_overrides.account_pubkeys,
        account_count,
        instruction_overrides.program_id,
    )
    role_proofs = _resolve_role_proofs(instruction_overrides.role_proofs, account_count)

    return CompiledPumpCreateV2Instruction(
        as_of_slot=Slot(789),
        program_id=instruction_overrides.program_id,
        program_id_index=instruction_overrides.program_id_index,
        account_indices=tuple(range(account_count)),
        account_pubkeys=account_pubkeys,
        account_role_proofs=role_proofs,
        data=_create_data() if data is None else data,
        transaction_index=9,
        outer_instruction_index=10,
        signature=b"create-v2-sig",
        actor_role_proofs=instruction_overrides.actor_role_proofs,
        transaction_slot_account_state_available=(
            instruction_overrides.transaction_slot_account_state_available
        ),
    )


def _create_data(
    *,
    is_mayhem_mode: bytes = b"\x01",
    is_cashback_enabled: bytes = b"\x00",
) -> bytes:
    return (
        CREATE_V2_DISCRIMINATOR
        + _string("Test Coin")
        + _string("TST")
        + _string("ipfs://metadata")
        + CREATOR_BYTES
        + is_mayhem_mode
        + is_cashback_enabled
    )


def _string(value: str) -> bytes:
    encoded = value.encode("utf-8")
    return struct.pack("<I", len(encoded)) + encoded


def _resolve_account_pubkeys(
    account_pubkeys: tuple[str, ...] | None,
    account_count: int,
    program_id: str,
) -> tuple[str, ...] | None:
    if account_pubkeys is None:
        return None
    if account_pubkeys != DEFAULT_ACCOUNT_PUBKEYS:
        return account_pubkeys
    return (*_account_pubkeys(account_count), program_id, "fee-payer", "first-buyer")


def _resolve_role_proofs(
    role_proofs: tuple[AccountRoleProof, ...],
    account_count: int,
) -> tuple[AccountRoleProof, ...]:
    if role_proofs != DEFAULT_ROLE_PROOFS:
        return role_proofs
    return _role_proofs(_account_pubkeys(account_count))


def _actor_proofs() -> tuple[LaunchActorProof, ...]:
    return (
        _actor_proof(role=LaunchActorRole.FEE_PAYER),
        _actor_proof(role=LaunchActorRole.FIRST_BUYER),
    )


def _actor_proof(
    *,
    role: LaunchActorRole,
    account_index: int | None = None,
    pubkey: str | None = None,
    as_of_slot: Slot | None = None,
    evidence_ids: tuple[str, ...] | None = None,
) -> LaunchActorProof:
    selected_account_index = account_index
    if selected_account_index is None:
        selected_account_index = (
            FEE_PAYER_INDEX if role is LaunchActorRole.FEE_PAYER else FIRST_BUYER_INDEX
        )
    selected_pubkey = pubkey
    if selected_pubkey is None:
        selected_pubkey = (
            "fee-payer" if role is LaunchActorRole.FEE_PAYER else "first-buyer"
        )
    selected_evidence_ids = evidence_ids
    if selected_evidence_ids is None:
        selected_evidence_ids = (
            ("transaction-message:fee-payer",)
            if role is LaunchActorRole.FEE_PAYER
            else ("first-fill:first-buyer",)
        )
    return LaunchActorProof(
        as_of_slot=Slot(789) if as_of_slot is None else as_of_slot,
        role=role,
        account_index=selected_account_index,
        pubkey=selected_pubkey,
        evidence_ids=selected_evidence_ids,
        source_version=(
            "transaction-message-v1"
            if role is LaunchActorRole.FEE_PAYER
            else "first-fill-v1"
        ),
    )


def _role_proofs(account_pubkeys: tuple[str, ...]) -> tuple[AccountRoleProof, ...]:
    return tuple(
        AccountRoleProof(name=name, pubkey=account_pubkeys[index])
        for index, name in enumerate(CREATE_V2_ACCOUNT_NAMES)
        if index < len(account_pubkeys)
    )


def _account_pubkeys(account_count: int) -> tuple[str, ...]:
    account_pubkeys = [f"create-account-{index}" for index in range(account_count)]
    fixed_positions = {
        6: SYSTEM_PROGRAM_ID,
        7: SPL_2022_PROGRAM_ID,
        8: ASSOCIATED_SPL_PROGRAM_ID,
        9: MAYHEM_PROGRAM_ID,
        15: PUMP_PROGRAM_ID,
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
