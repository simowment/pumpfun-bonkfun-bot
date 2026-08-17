"""Launch artifact to matcher-signal adapter tests."""

import ast
import unittest
from dataclasses import replace
from pathlib import Path
from typing import cast

from rugbot.decision.launch_signals import (
    ACCEPTED_PUMP_CREATE_V2_DECODER_VERSION,
    ACCEPTED_PUMP_CREATE_V2_IDL_SHA256,
    ACCEPTED_PUMP_PROGRAM_ID,
    PUMP_CREATE_V2_LAUNCH_SIGNAL_SOURCE_VERSION,
    pump_create_v2_launch_address_signals,
)
from rugbot.decision.matcher import (
    KnownLaunchMatcherConfig,
    KnownLaunchMatchResult,
    LaunchAddressSignal,
    match_known_operator_launch,
)
from rugbot.domain.amounts import Slot
from rugbot.domain.decisions import AbstainReason, AbstainResult
from rugbot.domain.launches import LaunchCreatedV2
from rugbot.graph.entity_resolution import (
    AddressRole,
    AddressRoleAssignment,
)
from rugbot.graph.operator_profile import (
    CampaignSegment,
    OperatorAddressProfile,
    OperatorProfileSnapshot,
    OperatorRegimeKind,
    RegimeClassification,
)

ADAPTER_MODULE = Path("src/rugbot/decision/launch_signals.py")
BASE_PROGRAM_PUBKEY = "base-program-pubkey"
QUOTE_PROGRAM_PUBKEY = "quote-program-pubkey"
FORBIDDEN_IMPORT_PREFIXES = (
    "builtins",
    "importlib",
    "os",
    "pathlib",
    "requests",
    "aiohttp",
    "httpx",
    "subprocess",
    "socket",
    "sqlite",
    "psycopg",
    "urllib",
    "rugbot.ingest",
    "rugbot.storage",
    "rugbot.execution",
    "rugbot.protocol",
    "src.core",
    "src.trading",
    "src.platforms",
    "solana",
    "solders",
    "dotenv",
)


class PumpCreateV2LaunchSignalAdapterTests(unittest.TestCase):
    """Tests for pure Pump create_v2 launch signal projection."""

    def test_emits_only_explicit_roles_from_synthetic_launch(self) -> None:
        """Creator, submitter, fee payer, and first buyer remain separate roles."""

        result = pump_create_v2_launch_address_signals(
            launch=_launch_created_v2(first_buyer_pubkey="first-buyer-d"),
            as_of_slot=Slot(20),
        )

        self.assertIsInstance(result, tuple)
        signals = cast("tuple[LaunchAddressSignal, ...]", result)
        self.assertEqual(
            tuple((signal.address, signal.role) for signal in signals),
            (
                ("creator-a", AddressRole.CREATOR),
                ("submitter-b", AddressRole.CREATION_SUBMITTER),
                ("fee-payer-c", AddressRole.FEE_PAYER),
                ("first-buyer-d", AddressRole.FIRST_BUYER),
            ),
        )
        for signal in signals:
            self.assertEqual(signal.as_of_slot, Slot(20))
            self.assertEqual(signal.launch_id, "launch-1")
            self.assertEqual(signal.signal_probability_ppm, 1_000_000)
            self.assertEqual(
                signal.source_version,
                PUMP_CREATE_V2_LAUNCH_SIGNAL_SOURCE_VERSION,
            )
        self.assertEqual(signals[2].evidence_ids, ("fee-payer-proof",))
        self.assertEqual(signals[3].evidence_ids, ("first-buyer-proof",))
        self.assertEqual(
            signals[0].evidence_ids,
            ("launch:launch-1:slot:20:create_v2:args.creator:creator-a",),
        )
        self.assertEqual(
            signals[1].evidence_ids,
            ("launch:launch-1:slot:20:create_v2:accounts.user:submitter-b",),
        )

    def test_missing_optional_actors_are_omitted(self) -> None:
        """Missing optional actors are not inferred from creator or submitter."""

        result = pump_create_v2_launch_address_signals(
            launch=_launch_created_v2(
                fee_payer_pubkey=None,
                fee_payer_account_index=None,
                actor_role_proofs=(),
            ),
            as_of_slot=Slot(20),
        )

        self.assertIsInstance(result, tuple)
        signals = cast("tuple[LaunchAddressSignal, ...]", result)
        self.assertEqual(
            tuple(signal.role for signal in signals),
            (AddressRole.CREATOR, AddressRole.CREATION_SUBMITTER),
        )

    def test_actor_pubkey_present_without_proof_abstains(self) -> None:
        """Optional actor fields need matching explicit actor proof."""

        result = pump_create_v2_launch_address_signals(
            launch=_launch_created_v2(actor_role_proofs=()),
            as_of_slot=Slot(20),
        )

        self.assert_abstains(result, AbstainReason.MISSING_FEATURE, as_of_slot=20)

    def test_actor_proof_mismatch_abstains(self) -> None:
        """Actor proof pubkey and index must agree with decoded actor fields."""

        result = pump_create_v2_launch_address_signals(
            launch=_launch_created_v2(
                actor_role_proofs=(
                    (
                        "fee_payer",
                        6,
                        "different-fee-payer",
                        ("fee-payer-proof",),
                        "fee-payer-source-v1",
                    ),
                ),
            ),
            as_of_slot=Slot(20),
        )

        self.assert_abstains(
            result,
            AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
            as_of_slot=20,
        )

    def test_actor_proof_index_mismatch_abstains(self) -> None:
        """Actor proof account index must agree with decoded actor fields."""

        result = pump_create_v2_launch_address_signals(
            launch=_launch_created_v2(
                actor_role_proofs=(
                    (
                        "fee_payer",
                        99,
                        "fee-payer-c",
                        ("fee-payer-proof",),
                        "fee-payer-source-v1",
                    ),
                ),
            ),
            as_of_slot=Slot(20),
        )

        self.assert_abstains(
            result,
            AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
            as_of_slot=20,
        )

    def test_first_buyer_actor_proof_mismatch_abstains(self) -> None:
        """First-buyer proof mismatch cannot be ignored."""

        result = pump_create_v2_launch_address_signals(
            launch=_launch_created_v2(
                first_buyer_pubkey="first-buyer-d",
                actor_role_proofs=(
                    (
                        "fee_payer",
                        6,
                        "fee-payer-c",
                        ("fee-payer-proof",),
                        "fee-payer-source-v1",
                    ),
                    (
                        "first_buyer",
                        7,
                        "someone-else",
                        ("first-buyer-proof",),
                        "first-buyer-source-v1",
                    ),
                ),
            ),
            as_of_slot=Slot(20),
        )

        self.assert_abstains(
            result,
            AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
            as_of_slot=20,
        )

    def test_actor_proof_without_optional_actor_abstains(self) -> None:
        """A proof for an absent optional actor is inconsistent evidence."""

        result = pump_create_v2_launch_address_signals(
            launch=_launch_created_v2(
                fee_payer_pubkey=None,
                fee_payer_account_index=None,
                actor_role_proofs=(
                    (
                        "fee_payer",
                        6,
                        "fee-payer-c",
                        ("fee-payer-proof",),
                        "fee-payer-source-v1",
                    ),
                ),
            ),
            as_of_slot=Slot(20),
        )

        self.assert_abstains(
            result,
            AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
            as_of_slot=20,
        )

    def test_user_account_proof_is_required_and_exact(self) -> None:
        """Creation submitter signals require an explicit matching user proof."""

        missing = pump_create_v2_launch_address_signals(
            launch=replace(_launch_created_v2(), account_role_proofs=()),
            as_of_slot=Slot(20),
        )
        mismatched = pump_create_v2_launch_address_signals(
            launch=replace(
                _launch_created_v2(),
                account_role_proofs=(("user", "someone-else"),),
            ),
            as_of_slot=Slot(20),
        )

        self.assert_abstains(missing, AbstainReason.MISSING_FEATURE, as_of_slot=20)
        self.assert_abstains(
            mismatched,
            AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
            as_of_slot=20,
        )

    def test_slot_mismatch_abstains(self) -> None:
        """Adapter output is point-in-time bounded."""

        result = pump_create_v2_launch_address_signals(
            launch=_launch_created_v2(),
            as_of_slot=Slot(21),
        )

        self.assert_abstains(result, AbstainReason.STALE_STATE, as_of_slot=21)

    def test_unknown_decoder_or_idl_abstains(self) -> None:
        """Only the accepted create_v2 decoder and IDL identity can feed signals."""

        wrong_decoder = pump_create_v2_launch_address_signals(
            launch=replace(_launch_created_v2(), decoder_version="other-decoder"),
            as_of_slot=Slot(20),
        )
        wrong_idl = pump_create_v2_launch_address_signals(
            launch=replace(_launch_created_v2(), idl_hash="other-idl"),
            as_of_slot=Slot(20),
        )

        self.assert_abstains(
            wrong_decoder,
            AbstainReason.DECODER_MISMATCH,
            as_of_slot=20,
        )
        self.assert_abstains(
            wrong_idl,
            AbstainReason.DECODER_MISMATCH,
            as_of_slot=20,
        )

    def test_wrong_program_or_instruction_abstains(self) -> None:
        """Program and instruction identity are exact, not retro-compatible."""

        wrong_program = pump_create_v2_launch_address_signals(
            launch=replace(_launch_created_v2(), program_id="other-program"),
            as_of_slot=Slot(20),
        )
        wrong_instruction = pump_create_v2_launch_address_signals(
            launch=replace(_launch_created_v2(), instruction_name="create"),
            as_of_slot=Slot(20),
        )
        wrong_creation_type = pump_create_v2_launch_address_signals(
            launch=replace(_launch_created_v2(), creation_instruction_type="create"),
            as_of_slot=Slot(20),
        )

        for result in (wrong_program, wrong_instruction, wrong_creation_type):
            with self.subTest(result=result):
                self.assert_abstains(
                    result,
                    AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
                    as_of_slot=20,
                )

    def test_adapter_signals_feed_known_operator_matcher(self) -> None:
        """Adapter output is compatible with the known-operator matcher."""

        signals = pump_create_v2_launch_address_signals(
            launch=_launch_created_v2(),
            as_of_slot=Slot(20),
        )
        self.assertIsInstance(signals, tuple)

        result = match_known_operator_launch(
            signals=signals,
            profile=_profile(),
            config=_config(),
        )

        self.assertIsInstance(result, KnownLaunchMatchResult)
        match = cast("KnownLaunchMatchResult", result)
        self.assertEqual(match.matched_role_count, 2)
        self.assertEqual(
            tuple((item.address, item.role) for item in match.role_matches),
            (
                ("creator-a", AddressRole.CREATOR),
                ("fee-payer-c", AddressRole.FEE_PAYER),
            ),
        )

    def test_adapter_module_stays_pure_and_integer_only(self) -> None:
        """Launch signal adapters must not grow IO, execution, or floats."""

        source = ADAPTER_MODULE.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(ADAPTER_MODULE))
        violations = [
            imported_name
            for imported_name in _imported_module_names(tree)
            if imported_name.startswith(FORBIDDEN_IMPORT_PREFIXES)
        ]
        float_literals = [
            node.value
            for node in ast.walk(tree)
            if isinstance(node, ast.Constant) and isinstance(node.value, float)
        ]
        true_divisions = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div)
        ]
        forbidden_calls = [
            _call_name(node)
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and _call_name(node) in _forbidden_call_names()
        ]
        forbidden_attributes = [
            node.attr
            for node in ast.walk(tree)
            if isinstance(node, ast.Attribute)
            and node.attr in _forbidden_attribute_names()
        ]

        self.assertEqual(violations, [])
        self.assertEqual(float_literals, [])
        self.assertEqual(true_divisions, [])
        self.assertEqual(forbidden_calls, [])
        self.assertEqual(forbidden_attributes, [])
        for token in _forbidden_source_tokens():
            with self.subTest(token=token):
                self.assertNotIn(token, source)

    def assert_abstains(
        self,
        result: object,
        reason: AbstainReason,
        *,
        as_of_slot: int,
    ) -> None:
        self.assertIsInstance(result, AbstainResult)
        result = cast("AbstainResult", result)
        self.assertEqual(result.reason, reason)
        self.assertEqual(result.as_of_slot, as_of_slot)


def _launch_created_v2(
    *,
    fee_payer_pubkey: str | None = "fee-payer-c",
    fee_payer_account_index: int | None = 6,
    first_buyer_pubkey: str | None = None,
    actor_role_proofs: tuple[tuple[str, int, str, tuple[str, ...], str], ...]
    | None = None,
) -> LaunchCreatedV2:
    actor_proofs = (
        _default_actor_role_proofs(
            fee_payer_pubkey=fee_payer_pubkey,
            fee_payer_account_index=fee_payer_account_index,
            first_buyer_pubkey=first_buyer_pubkey,
        )
        if actor_role_proofs is None
        else actor_role_proofs
    )
    first_buyer_account_index = 7 if first_buyer_pubkey is not None else None
    return LaunchCreatedV2(
        as_of_slot=Slot(20),
        launch_id="launch-1",
        program_id=ACCEPTED_PUMP_PROGRAM_ID,
        program_id_index=9,
        signature=None,
        instruction_name="create_v2",
        creation_instruction_type="create_v2",
        account_indices=tuple(range(16)),
        account_pubkeys=(
            "mint-a",
            "mint-authority",
            "bonding-curve",
            "associated-bonding-curve",
            "global",
            "submitter-b",
            fee_payer_pubkey or "absent-fee-payer",
            first_buyer_pubkey or "unused-first-buyer",
            "system-program",
            BASE_PROGRAM_PUBKEY,
            "associated-program",
            "mayhem-program",
            "global-params",
            "quote-vault",
            "mayhem-state",
            "event-authority",
        ),
        account_role_proofs=(("mint", "mint-a"), ("user", "submitter-b")),
        actor_role_proofs=actor_proofs,
        required_account_names=("mint", "user"),
        transaction_index=None,
        outer_instruction_index=2,
        inner_instruction_group_index=None,
        inner_instruction_index=None,
        mint_account_index=0,
        mint_pubkey="mint-a",
        mint_authority_account_index=1,
        bonding_curve_account_index=2,
        bonding_curve_pubkey="bonding-curve",
        associated_bonding_curve_account_index=3,
        global_account_index=4,
        user_account_index=5,
        user_pubkey="submitter-b",
        creator_pubkey="creator-a",
        fee_payer_account_index=fee_payer_account_index,
        fee_payer_pubkey=fee_payer_pubkey,
        first_buyer_account_index=first_buyer_account_index,
        first_buyer_pubkey=first_buyer_pubkey,
        system_program_account_index=8,
        token_program_account_index=9,
        base_token_program_pubkey=BASE_PROGRAM_PUBKEY,
        associated_token_program_account_index=10,
        mayhem_program_account_index=11,
        global_params_account_index=12,
        quote_vault_account_index=13,
        quote_asset="SOL",
        quote_mint_pubkey="wsol",
        quote_token_program_pubkey=QUOTE_PROGRAM_PUBKEY,
        mayhem_state_account_index=14,
        mayhem_token_vault_account_index=15,
        event_authority_account_index=15,
        name="Test Launch",
        symbol="TL",
        uri="https://example.test/token.json",
        is_mayhem_mode=False,
        is_cashback_enabled=True,
        transaction_slot_account_state_available=False,
        missing_evidence=_missing_evidence(first_buyer_pubkey),
        decoder_version=ACCEPTED_PUMP_CREATE_V2_DECODER_VERSION,
        idl_hash=ACCEPTED_PUMP_CREATE_V2_IDL_SHA256,
    )


def _default_actor_role_proofs(
    *,
    fee_payer_pubkey: str | None,
    fee_payer_account_index: int | None,
    first_buyer_pubkey: str | None,
) -> tuple[tuple[str, int, str, tuple[str, ...], str], ...]:
    proofs: tuple[tuple[str, int, str, tuple[str, ...], str], ...] = ()
    if fee_payer_pubkey is not None and fee_payer_account_index is not None:
        proofs = (
            (
                "fee_payer",
                fee_payer_account_index,
                fee_payer_pubkey,
                ("fee-payer-proof",),
                "fee-payer-source-v1",
            ),
        )
    if first_buyer_pubkey is not None:
        proofs = (
            *proofs,
            (
                "first_buyer",
                7,
                first_buyer_pubkey,
                ("first-buyer-proof",),
                "first-buyer-source-v1",
            ),
        )
    return proofs


def _missing_evidence(first_buyer_pubkey: str | None) -> tuple[str, ...]:
    if first_buyer_pubkey is not None:
        return ("transaction_slot_account_state",)
    return ("first_buyer", "transaction_slot_account_state")


def _profile() -> OperatorProfileSnapshot:
    return OperatorProfileSnapshot(
        as_of_slot=Slot(20),
        entity_id="entity-1",
        profile_version="profile-v1",
        entity_resolver_version="resolver-v1",
        role_classifier_version="roles-v1",
        addresses=(
            _address("creator-a", same_controller=900_000, role=AddressRole.CREATOR),
            _address(
                "fee-payer-c",
                same_controller=880_000,
                role=AddressRole.FEE_PAYER,
            ),
        ),
        campaigns=(_campaign(),),
        regimes=(_regime(),),
        current_active_regime_id="regime-a",
        source_membership_count=2,
        active_address_count=2,
        source_campaign_count=1,
        active_campaign_count=1,
        source_regime_count=1,
        active_regime_count=1,
        reason_codes=("profile-built",),
    )


def _address(
    address: str,
    *,
    same_controller: int,
    role: AddressRole,
) -> OperatorAddressProfile:
    return OperatorAddressProfile(
        as_of_slot=Slot(20),
        entity_id="entity-1",
        address=address,
        same_controller_probability_ppm=same_controller,
        cooperating_probability_ppm=0,
        shared_service_probability_ppm=0,
        incidental_interaction_probability_ppm=0,
        probable_roles=(
            AddressRoleAssignment(
                as_of_slot=Slot(20),
                address=address,
                role=role,
                role_probability_ppm=900_000,
                evidence_ids=(f"{address}-{role.value}-role",),
                model_version="role-model-v1",
            ),
        ),
        evidence_ids=(f"{address}-membership",),
        model_version="membership-model-v1",
    )


def _campaign() -> CampaignSegment:
    return CampaignSegment(
        as_of_slot=Slot(20),
        entity_id="entity-1",
        campaign_id="campaign-a",
        campaign_probability_ppm=900_000,
        launch_count=7,
        evidence_ids=("campaign-evidence",),
        model_version="campaign-model-v1",
    )


def _regime() -> RegimeClassification:
    return RegimeClassification(
        as_of_slot=Slot(20),
        entity_id="entity-1",
        campaign_id="campaign-a",
        regime_id="regime-a",
        regime_kind=OperatorRegimeKind.FAKE_PUMP_THEN_FULL_DUMP,
        regime_probability_ppm=850_000,
        support_launch_count=5,
        evidence_ids=("regime-evidence",),
        model_version="regime-model-v1",
    )


def _config() -> KnownLaunchMatcherConfig:
    return KnownLaunchMatcherConfig(
        as_of_slot=Slot(20),
        matcher_version="matcher-v1",
        entity_graph_snapshot_version="graph-v1",
        min_signal_probability_ppm=700_000,
        min_address_probability_ppm=700_000,
        min_profile_role_probability_ppm=700_000,
        min_entity_probability_ppm=700_000,
        min_regime_probability_ppm=800_000,
        min_required_role_matches=2,
    )


def _imported_module_names(tree: ast.AST) -> list[str]:
    names: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            names.append(node.module)
    return names


def _call_name(node: ast.Call) -> str:
    if isinstance(node.func, ast.Name):
        return node.func.id
    if isinstance(node.func, ast.Attribute):
        return node.func.attr
    return ""


def _forbidden_call_names() -> tuple[str, ...]:
    return (
        "__import__",
        "compile",
        "eval",
        "exec",
        "getenv",
        "open",
    )


def _forbidden_attribute_names() -> tuple[str, ...]:
    return (
        "environ",
        "read_bytes",
        "read_text",
        "write_bytes",
        "write_text",
    )


def _forbidden_source_tokens() -> tuple[str, ...]:
    return (
        "Key" + "pair",
        "Wal" + "let",
        "PRIVATE" + "_KEY",
        "__" + "import__",
        "send" + "_transaction",
        "send" + "_raw_transaction",
        "float(",
    )


if __name__ == "__main__":
    unittest.main()
