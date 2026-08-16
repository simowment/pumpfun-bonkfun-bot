"""Known-operator launch matcher contract tests."""

import ast
import unittest
from dataclasses import replace
from pathlib import Path
from typing import Any, cast

from rugbot.decision.matcher import (
    KnownLaunchMatcherConfig,
    KnownLaunchMatchResult,
    LaunchAddressSignal,
    match_known_operator_launch,
)
from rugbot.decision.snapshots import (
    DecisionSnapshotBundle,
    LaunchMatcherSnapshot,
    RuggerSelectorSnapshot,
    RugTimingSnapshot,
    validate_decision_snapshot_bundle,
)
from rugbot.domain.amounts import Slot
from rugbot.domain.decisions import AbstainReason, AbstainResult
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

MATCHER_MODULE = Path("src/rugbot/decision/matcher.py")
FORBIDDEN_IMPORT_PREFIXES = (
    "requests",
    "aiohttp",
    "httpx",
    "sqlite",
    "psycopg",
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


class KnownLaunchMatcherTests(unittest.TestCase):
    """Tests for immediate known-operator launch matching."""

    def test_matches_known_creator_and_funder_into_snapshot(self) -> None:
        """Explicit address-role matches produce a decision-layer snapshot."""

        result = match_known_operator_launch(
            signals=(
                _signal(address="creator-a", role=AddressRole.CREATOR),
                _signal(address="funder-b", role=AddressRole.FUNDER),
            ),
            profile=_profile(),
            config=_config(),
        )

        self.assertIsInstance(result, KnownLaunchMatchResult)
        match = cast("KnownLaunchMatchResult", result)
        self.assertEqual(match.as_of_slot, Slot(20))
        self.assertEqual(match.campaign_id, "campaign-a")
        self.assertEqual(match.regime_kind, OperatorRegimeKind.FAKE_PUMP_THEN_FULL_DUMP)
        self.assertEqual(match.matched_role_count, 2)
        self.assertEqual(match.best_match_probability_ppm, 850_000)
        snapshot = match.matcher_snapshot
        self.assertEqual(snapshot.entity_id, "entity-1")
        self.assertEqual(snapshot.regime_id, "regime-a")
        self.assertEqual(snapshot.entity_probability_ppm, 850_000)
        self.assertEqual(snapshot.regime_probability_ppm, 850_000)
        self.assertEqual(snapshot.operator_profile_version, "profile-v1")
        self.assertEqual(snapshot.entity_graph_snapshot_version, "graph-v1")
        self.assertEqual(snapshot.matcher_version, "matcher-v1")

    def test_matcher_snapshot_validates_inside_decision_bundle(self) -> None:
        """Matcher output is compatible with existing decision snapshots."""

        result = match_known_operator_launch(
            signals=(
                _signal(address="creator-a", role=AddressRole.CREATOR),
                _signal(address="funder-b", role=AddressRole.FUNDER),
            ),
            profile=_profile(),
            config=_config(),
        )
        self.assertIsInstance(result, KnownLaunchMatchResult)
        match = cast("KnownLaunchMatchResult", result)
        bundle = _bundle(match.matcher_snapshot)

        validated = validate_decision_snapshot_bundle(bundle)

        self.assertIs(validated, bundle)

    def test_creation_submitter_role_is_explicit_not_silently_mapped(self) -> None:
        """Launch submitter/user semantics require an explicit matching role."""

        result = match_known_operator_launch(
            signals=(
                _signal(address="creator-a", role=AddressRole.CREATION_SUBMITTER),
            ),
            profile=_profile(),
            config=replace(_config(), min_required_role_matches=1),
        )

        self.assert_abstains(result, AbstainReason.MISSING_FEATURE, as_of_slot=20)

    def test_no_vague_address_similarity_match(self) -> None:
        """An unknown address never matches by resemblance or role alone."""

        result = match_known_operator_launch(
            signals=(_signal(address="creator-a-nearby", role=AddressRole.CREATOR),),
            profile=_profile(),
            config=replace(_config(), min_required_role_matches=1),
        )

        self.assert_abstains(result, AbstainReason.MISSING_FEATURE, as_of_slot=20)

    def test_shared_service_only_address_does_not_match(self) -> None:
        """Shared-service probability is not entity membership confidence."""

        result = match_known_operator_launch(
            signals=(_signal(address="weak-service", role=AddressRole.FEE_PAYER),),
            profile=_profile(),
            config=replace(_config(), min_required_role_matches=1),
        )

        self.assert_abstains(result, AbstainReason.MISSING_FEATURE, as_of_slot=20)

    def test_unknown_or_new_regime_abstains(self) -> None:
        """Observe-only regimes cannot produce positive launch matches."""

        profile = replace(
            _profile(),
            regimes=(
                _regime(
                    regime_id="regime-a",
                    kind=OperatorRegimeKind.UNKNOWN_OR_NEW_REGIME,
                ),
            ),
        )

        result = match_known_operator_launch(
            signals=(_signal(address="creator-a", role=AddressRole.CREATOR),),
            profile=profile,
            config=replace(_config(), min_required_role_matches=1),
        )

        self.assert_abstains(result, AbstainReason.MISSING_FEATURE, as_of_slot=20)

    def test_missing_current_regime_abstains(self) -> None:
        """Profile must identify the current active regime."""

        result = match_known_operator_launch(
            signals=(_signal(address="creator-a", role=AddressRole.CREATOR),),
            profile=replace(_profile(), current_active_regime_id=None),
            config=replace(_config(), min_required_role_matches=1),
        )

        self.assert_abstains(result, AbstainReason.MISSING_FEATURE, as_of_slot=20)

    def test_signal_slot_mismatch_abstains(self) -> None:
        """Launch signals must share the matcher as_of_slot."""

        result = match_known_operator_launch(
            signals=(
                _signal(address="creator-a", role=AddressRole.CREATOR, as_of_slot=19),
            ),
            profile=_profile(),
            config=replace(_config(), min_required_role_matches=1),
        )

        self.assert_abstains(result, AbstainReason.STALE_STATE, as_of_slot=20)

    def test_profile_slot_mismatch_abstains(self) -> None:
        """Profile snapshots from another slot are stale."""

        result = match_known_operator_launch(
            signals=(_signal(address="creator-a", role=AddressRole.CREATOR),),
            profile=replace(_profile(), as_of_slot=Slot(19)),
            config=replace(_config(), min_required_role_matches=1),
        )

        self.assert_abstains(result, AbstainReason.STALE_STATE, as_of_slot=20)

    def test_equal_valued_float_slots_abstain(self) -> None:
        """Runtime slot validation rejects float values even when equal."""

        signal_result = match_known_operator_launch(
            signals=(
                replace(
                    _signal(address="creator-a", role=AddressRole.CREATOR),
                    as_of_slot=cast("Any", 20.0),
                ),
            ),
            profile=_profile(),
            config=replace(_config(), min_required_role_matches=1),
        )
        profile_result = match_known_operator_launch(
            signals=(_signal(address="creator-a", role=AddressRole.CREATOR),),
            profile=replace(_profile(), as_of_slot=cast("Any", 20.0)),
            config=replace(_config(), min_required_role_matches=1),
        )

        self.assert_abstains(
            signal_result,
            AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
            as_of_slot=20,
        )
        self.assert_abstains(
            profile_result,
            AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
            as_of_slot=20,
        )

    def test_malformed_loaded_contracts_abstain_instead_of_raising(self) -> None:
        """Matcher revalidates externally loaded contracts before dereferencing."""

        malformed_config = match_known_operator_launch(
            signals=(_signal(address="creator-a", role=AddressRole.CREATOR),),
            profile=_profile(),
            config=cast("Any", object()),
        )
        malformed_profile = match_known_operator_launch(
            signals=(_signal(address="creator-a", role=AddressRole.CREATOR),),
            profile=cast("Any", object()),
            config=replace(_config(), min_required_role_matches=1),
        )
        malformed_signal = match_known_operator_launch(
            signals=(cast("Any", object()),),
            profile=_profile(),
            config=replace(_config(), min_required_role_matches=1),
        )

        self.assert_abstains(
            malformed_config,
            AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
            as_of_slot=-1,
        )
        self.assert_abstains(
            malformed_profile,
            AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
            as_of_slot=20,
        )
        self.assert_abstains(
            malformed_signal,
            AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
            as_of_slot=20,
        )

    def test_malformed_profile_collections_abstain_before_counting(self) -> None:
        """Loaded profile collections are checked before count validation."""

        for field_name in ("addresses", "campaigns", "regimes"):
            with self.subTest(field_name=field_name):
                result = match_known_operator_launch(
                    signals=(_signal(address="creator-a", role=AddressRole.CREATOR),),
                    profile=replace(_profile(), **{field_name: cast("Any", object())}),
                    config=replace(_config(), min_required_role_matches=1),
                )

                self.assert_abstains(
                    result,
                    AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
                    as_of_slot=20,
                )

    def test_regime_probability_below_threshold_abstains(self) -> None:
        """Low regime confidence cannot produce a positive matcher snapshot."""

        result = match_known_operator_launch(
            signals=(_signal(address="creator-a", role=AddressRole.CREATOR),),
            profile=replace(
                _profile(),
                regimes=(_regime(regime_id="regime-a", probability=600_000),),
            ),
            config=replace(_config(), min_required_role_matches=1),
        )

        self.assert_abstains(result, AbstainReason.MISSING_FEATURE, as_of_slot=20)

    def test_duplicate_signal_abstains(self) -> None:
        """Duplicate address-role signals are malformed input."""

        result = match_known_operator_launch(
            signals=(
                _signal(address="creator-a", role=AddressRole.CREATOR),
                _signal(address="creator-a", role=AddressRole.CREATOR),
            ),
            profile=_profile(),
            config=replace(_config(), min_required_role_matches=1),
        )

        self.assert_abstains(
            result,
            AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
            as_of_slot=20,
        )

    def test_cross_launch_signals_abstain(self) -> None:
        """Signals from separate launches cannot combine into one match."""

        result = match_known_operator_launch(
            signals=(
                _signal(
                    address="creator-a",
                    role=AddressRole.CREATOR,
                    launch_id="launch-a",
                ),
                _signal(
                    address="funder-b",
                    role=AddressRole.FUNDER,
                    launch_id="launch-b",
                ),
            ),
            profile=_profile(),
            config=_config(),
        )

        self.assert_abstains(
            result,
            AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
            as_of_slot=20,
        )

    def test_float_signal_probability_abstains(self) -> None:
        """Runtime validation rejects float-like signal probabilities."""

        result = match_known_operator_launch(
            signals=(
                _signal(
                    address="creator-a",
                    role=AddressRole.CREATOR,
                    probability=cast("Any", 0.5),
                ),
            ),
            profile=_profile(),
            config=replace(_config(), min_required_role_matches=1),
        )

        self.assert_abstains(
            result,
            AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
            as_of_slot=20,
        )

    def test_mutable_signal_evidence_ids_abstain(self) -> None:
        """Launch signal provenance must use immutable tuple data."""

        result = match_known_operator_launch(
            signals=(
                replace(
                    _signal(address="creator-a", role=AddressRole.CREATOR),
                    evidence_ids=cast("Any", ["mutable-evidence"]),
                ),
            ),
            profile=_profile(),
            config=replace(_config(), min_required_role_matches=1),
        )

        self.assert_abstains(result, AbstainReason.MISSING_FEATURE, as_of_slot=20)

    def test_malformed_profile_counts_abstain(self) -> None:
        """Loaded profiles are defensively revalidated before matching."""

        result = match_known_operator_launch(
            signals=(_signal(address="creator-a", role=AddressRole.CREATOR),),
            profile=replace(_profile(), active_address_count=99),
            config=replace(_config(), min_required_role_matches=1),
        )

        self.assert_abstains(
            result,
            AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
            as_of_slot=20,
        )

    def test_active_counts_cannot_exceed_source_counts(self) -> None:
        """Loaded profiles cannot claim more active artifacts than sources."""

        result = match_known_operator_launch(
            signals=(_signal(address="creator-a", role=AddressRole.CREATOR),),
            profile=replace(
                _profile(),
                source_membership_count=1,
                active_address_count=3,
            ),
            config=replace(_config(), min_required_role_matches=1),
        )

        self.assert_abstains(
            result,
            AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
            as_of_slot=20,
        )

    def test_missing_matcher_version_abstains(self) -> None:
        """Matcher snapshots must be versioned."""

        result = match_known_operator_launch(
            signals=(_signal(address="creator-a", role=AddressRole.CREATOR),),
            profile=_profile(),
            config=replace(_config(), matcher_version=""),
        )

        self.assert_abstains(result, AbstainReason.DECODER_MISMATCH, as_of_slot=20)

    def test_launch_matcher_module_stays_pure_and_integer_only(self) -> None:
        """Matcher contracts must not grow impure adapters, signers, or floats."""

        source = MATCHER_MODULE.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(MATCHER_MODULE))
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

        self.assertEqual(violations, [])
        self.assertEqual(float_literals, [])
        self.assertEqual(true_divisions, [])
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
                "funder-b",
                same_controller=0,
                cooperating=850_000,
                role=AddressRole.FUNDER,
            ),
            _address(
                "weak-service",
                same_controller=0,
                cooperating=100_000,
                shared_service=900_000,
                role=AddressRole.FEE_PAYER,
            ),
        ),
        campaigns=(_campaign(campaign_id="campaign-a"),),
        regimes=(_regime(regime_id="regime-a"),),
        current_active_regime_id="regime-a",
        source_membership_count=3,
        active_address_count=3,
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
    cooperating: int = 0,
    shared_service: int = 0,
) -> OperatorAddressProfile:
    return OperatorAddressProfile(
        as_of_slot=Slot(20),
        entity_id="entity-1",
        address=address,
        same_controller_probability_ppm=same_controller,
        cooperating_probability_ppm=cooperating,
        shared_service_probability_ppm=shared_service,
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


def _campaign(
    *,
    campaign_id: str,
    probability: int = 900_000,
) -> CampaignSegment:
    return CampaignSegment(
        as_of_slot=Slot(20),
        entity_id="entity-1",
        campaign_id=campaign_id,
        campaign_probability_ppm=probability,
        launch_count=7,
        evidence_ids=(f"{campaign_id}-evidence",),
        model_version="campaign-model-v1",
    )


def _regime(
    *,
    regime_id: str,
    campaign_id: str = "campaign-a",
    kind: OperatorRegimeKind = OperatorRegimeKind.FAKE_PUMP_THEN_FULL_DUMP,
    probability: int = 850_000,
) -> RegimeClassification:
    return RegimeClassification(
        as_of_slot=Slot(20),
        entity_id="entity-1",
        campaign_id=campaign_id,
        regime_id=regime_id,
        regime_kind=kind,
        regime_probability_ppm=probability,
        support_launch_count=5,
        evidence_ids=(f"{regime_id}-evidence",),
        model_version="regime-model-v1",
    )


def _signal(
    *,
    address: str,
    role: AddressRole,
    as_of_slot: int = 20,
    launch_id: str = "launch-1",
    probability: int = 950_000,
) -> LaunchAddressSignal:
    return LaunchAddressSignal(
        as_of_slot=Slot(as_of_slot),
        launch_id=launch_id,
        address=address,
        role=role,
        signal_probability_ppm=probability,
        evidence_ids=(f"{launch_id}-{address}-{role.value}",),
        source_version="launch-signals-v1",
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


def _bundle(matcher: LaunchMatcherSnapshot) -> DecisionSnapshotBundle:
    return DecisionSnapshotBundle(
        as_of_slot=Slot(20),
        snapshot_bundle_version="bundle-v1",
        feature_snapshot_version="features-v1",
        market_state_snapshot_version="market-v1",
        matcher=matcher,
        selector=RuggerSelectorSnapshot(
            as_of_slot=Slot(20),
            selector_version="selector-v1",
            is_selected=True,
            min_entity_probability_ppm=700_000,
            min_regime_probability_ppm=800_000,
            min_trigger_risk_ppm=500_000,
            max_trigger_risk_ppm=600_000,
            min_historical_launches=3,
            historical_launch_count=5,
            trigger_generator_version="rules-v1",
            trigger_feature_schema_version="features-v1",
            trigger_labeler_version="labels-v1",
            trigger_row_schema_version="rows-v1",
            trigger_market_state_snapshot_version="market-v1",
            trigger_operator_profile_version="profile-v1",
            trigger_regime_model_version="regime-model-v1",
            reason_codes=("selector-pass",),
        ),
        timing=RugTimingSnapshot(
            as_of_slot=Slot(20),
            timing_model_version="timing-v1",
            p_dump_next_1s_ppm=100_000,
            p_dump_next_3s_ppm=200_000,
            p_dump_next_5s_ppm=300_000,
            p_dump_next_10s_ppm=450_000,
            q05_remaining_dump_time_ms=1_500,
            q10_remaining_dump_time_ms=2_000,
            q50_remaining_dump_time_ms=5_000,
        ),
    )


def _imported_module_names(tree: ast.AST) -> list[str]:
    names: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            names.append(node.module)
    return names


def _forbidden_source_tokens() -> tuple[str, ...]:
    return (
        "Key" + "pair",
        "Wal" + "let",
        "PRIVATE" + "_KEY",
        "send" + "_transaction",
        "send" + "_raw_transaction",
        "float(",
    )


if __name__ == "__main__":
    unittest.main()
