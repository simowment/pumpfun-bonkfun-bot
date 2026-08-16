"""Decision snapshot contract tests."""

import ast
import unittest
from dataclasses import FrozenInstanceError, replace
from pathlib import Path
from typing import Any, cast

from rugbot.decision.snapshots import (
    DecisionSnapshotBundle,
    DecisionSnapshotPolicy,
    LaunchMatcherSnapshot,
    RuggerSelectorSnapshot,
    RugTimingSnapshot,
    validate_decision_snapshot_bundle,
    validate_decision_snapshot_bundle_with_policy,
)
from rugbot.domain.amounts import Slot
from rugbot.domain.decisions import AbstainReason, AbstainResult
from rugbot.graph.wallet_churn import (
    OPERATOR_WALLET_CHURN_SNAPSHOT_VERSION,
)

SNAPSHOT_MODULE = Path("src/rugbot/decision/snapshots.py")
DEFAULT_SLOT = Slot(10)
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


class DecisionSnapshotContractTests(unittest.TestCase):
    """Tests for immutable point-in-time decision snapshot bundles."""

    def test_valid_bundle_passes_unchanged(self) -> None:
        """Complete matcher, selector, and timing snapshots are accepted."""

        bundle = _bundle()

        result = validate_decision_snapshot_bundle(bundle)

        self.assertIs(result, bundle)

    def test_snapshot_bundle_is_immutable(self) -> None:
        """Decision snapshots cannot be mutated after publication."""

        bundle = _bundle()

        with self.assertRaises(FrozenInstanceError):
            bundle.feature_snapshot_version = "changed"

    def test_component_slot_mismatch_abstains(self) -> None:
        """All model snapshots must share the bundle as_of_slot."""

        result = validate_decision_snapshot_bundle(
            _bundle(matcher=replace(_matcher(), as_of_slot=Slot(9)))
        )

        self.assert_abstains(result, AbstainReason.STALE_STATE, as_of_slot=10)

    def test_negative_as_of_slot_abstains(self) -> None:
        """Negative slot boundaries are unsupported."""

        result = validate_decision_snapshot_bundle(_bundle(as_of_slot=Slot(-1)))

        self.assert_abstains(
            result,
            AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
            as_of_slot=-1,
        )

    def test_malformed_nested_snapshot_abstains(self) -> None:
        """Malformed bundle components fail closed instead of raising."""

        result = validate_decision_snapshot_bundle(
            replace(_bundle(), matcher=cast("Any", None))
        )

        self.assert_abstains(
            result,
            AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
            as_of_slot=10,
        )

    def test_missing_artifact_version_abstains(self) -> None:
        """Missing versioned artifacts cannot feed decisions."""

        result = validate_decision_snapshot_bundle(_bundle(feature_snapshot_version=""))

        self.assert_abstains(result, AbstainReason.DECODER_MISMATCH, as_of_slot=10)

    def test_missing_entity_id_abstains(self) -> None:
        """A matcher snapshot must identify the candidate entity and regime."""

        result = validate_decision_snapshot_bundle(
            _bundle(matcher=replace(_matcher(), entity_id=""))
        )

        self.assert_abstains(result, AbstainReason.MISSING_FEATURE, as_of_slot=10)

    def test_invalid_probability_abstains(self) -> None:
        """Model probabilities use integer ppm and must stay in range."""

        result = validate_decision_snapshot_bundle(
            _bundle(matcher=replace(_matcher(), entity_probability_ppm=1_000_001))
        )

        self.assert_abstains(
            result,
            AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
            as_of_slot=10,
        )

    def test_float_probability_abstains(self) -> None:
        """Model probabilities must be integer PPM, not floats."""

        result = validate_decision_snapshot_bundle(
            _bundle(
                matcher=replace(
                    _matcher(),
                    entity_probability_ppm=cast("Any", 0.5),
                )
            )
        )

        self.assert_abstains(
            result,
            AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
            as_of_slot=10,
        )

    def test_bool_probability_abstains(self) -> None:
        """Bool values are not valid integer PPM probabilities."""

        bool_probability = True
        result = validate_decision_snapshot_bundle(
            _bundle(
                timing=replace(
                    _timing(),
                    p_dump_next_1s_ppm=cast("Any", bool_probability),
                )
            )
        )

        self.assert_abstains(
            result,
            AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
            as_of_slot=10,
        )

    def test_selected_below_threshold_abstains(self) -> None:
        """A selected snapshot cannot contradict matcher probabilities."""

        result = validate_decision_snapshot_bundle(
            _bundle(matcher=replace(_matcher(), entity_probability_ppm=500_000))
        )

        self.assert_abstains(
            result,
            AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
            as_of_slot=10,
        )

    def test_selected_below_trigger_risk_abstains(self) -> None:
        """A selected snapshot cannot contradict trigger-risk thresholds."""

        result = validate_decision_snapshot_bundle(
            _bundle(selector=replace(_selector(), max_trigger_risk_ppm=300_000))
        )

        self.assert_abstains(
            result,
            AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
            as_of_slot=10,
        )

    def test_selector_with_operator_churn_audit_passes(self) -> None:
        """Churn audit fields may be present on an otherwise valid selector."""

        bundle = _bundle(selector=_selector_with_churn_audit())

        result = validate_decision_snapshot_bundle(bundle)

        self.assertIs(result, bundle)

    def test_policy_accepts_selected_bundle_with_required_churn_audit(self) -> None:
        """Strict action policy accepts selected bundles only with churn audit."""

        bundle = _bundle(selector=_selector_with_churn_audit())

        result = validate_decision_snapshot_bundle_with_policy(
            bundle=bundle,
            policy=_policy(),
        )

        self.assertIs(result, bundle)

    def test_policy_rejects_selected_bundle_without_churn_audit(self) -> None:
        """A selected bundle cannot bypass required churn audit under policy."""

        result = validate_decision_snapshot_bundle_with_policy(
            bundle=_bundle(),
            policy=_policy(),
        )

        self.assert_abstains(result, AbstainReason.MISSING_FEATURE, as_of_slot=10)

    def test_policy_allows_unselected_bundle_without_churn_audit(self) -> None:
        """Required churn audit applies to actionable selected bundles."""

        bundle = _bundle(selector=replace(_selector(), is_selected=False))

        result = validate_decision_snapshot_bundle_with_policy(
            bundle=bundle,
            policy=_policy(),
        )

        self.assertIs(result, bundle)

    def test_policy_rejects_unaccepted_churn_audit_version(self) -> None:
        """Loaded churn audit versions must be policy-accepted."""

        result = validate_decision_snapshot_bundle_with_policy(
            bundle=_bundle(selector=_selector_with_churn_audit()),
            policy=replace(
                _policy(),
                accepted_operator_churn_snapshot_versions=("wallet-churn-v1",),
            ),
        )

        self.assert_abstains(result, AbstainReason.DECODER_MISMATCH, as_of_slot=10)

    def test_stale_decision_snapshot_policy_abstains(self) -> None:
        """Action policy must be evaluated at the same slot as the bundle."""

        result = validate_decision_snapshot_bundle_with_policy(
            bundle=_bundle(),
            policy=replace(_policy(), as_of_slot=Slot(9)),
        )

        self.assert_abstains(result, AbstainReason.STALE_STATE, as_of_slot=10)

    def test_malformed_decision_snapshot_policy_abstains(self) -> None:
        """Action policy settings are fail-closed."""

        result = validate_decision_snapshot_bundle_with_policy(
            bundle=_bundle(),
            policy=replace(
                _policy(),
                require_selected_operator_churn_audit=cast("Any", 1),
            ),
        )

        self.assert_abstains(
            result,
            AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
            as_of_slot=10,
        )

    def test_policy_with_whitespace_churn_versions_abstains(self) -> None:
        """Accepted churn versions must be substantive string tuples."""

        result = validate_decision_snapshot_bundle_with_policy(
            bundle=_bundle(),
            policy=replace(
                _policy(),
                accepted_operator_churn_snapshot_versions=("   ",),
            ),
        )

        self.assert_abstains(result, AbstainReason.DECODER_MISMATCH, as_of_slot=10)

    def test_selected_above_operator_churn_caps_abstains(self) -> None:
        """A loaded selected snapshot cannot exceed its churn caps."""

        result = validate_decision_snapshot_bundle(
            _bundle(
                selector=replace(
                    _selector_with_churn_audit(),
                    observed_operator_churn_new_high_risk_roles=2,
                )
            )
        )

        self.assert_abstains(
            result,
            AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
            as_of_slot=10,
        )

    def test_incomplete_operator_churn_audit_abstains(self) -> None:
        """Partial churn audit fields cannot be loaded as a coherent snapshot."""

        result = validate_decision_snapshot_bundle(
            _bundle(
                selector=replace(
                    _selector(),
                    operator_churn_snapshot_version=(
                        OPERATOR_WALLET_CHURN_SNAPSHOT_VERSION
                    ),
                )
            )
        )

        self.assert_abstains(result, AbstainReason.MISSING_FEATURE, as_of_slot=10)

    def test_float_operator_churn_audit_abstains(self) -> None:
        """Churn audit probabilities must remain integer PPM."""

        result = validate_decision_snapshot_bundle(
            _bundle(
                selector=replace(
                    _selector_with_churn_audit(),
                    observed_operator_churn_address_turnover_ppm=cast("Any", 0.5),
                )
            )
        )

        self.assert_abstains(
            result,
            AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
            as_of_slot=10,
        )

    def test_whitespace_operator_churn_version_abstains(self) -> None:
        """Loaded churn audit versions must be substantive strings."""

        result = validate_decision_snapshot_bundle(
            _bundle(
                selector=replace(
                    _selector_with_churn_audit(),
                    operator_churn_snapshot_version="   ",
                )
            )
        )

        self.assert_abstains(result, AbstainReason.DECODER_MISMATCH, as_of_slot=10)

    def test_non_string_version_abstains(self) -> None:
        """Loaded artifact versions must be non-empty strings."""

        result = validate_decision_snapshot_bundle(
            _bundle(snapshot_bundle_version=cast("Any", 1))
        )

        self.assert_abstains(result, AbstainReason.DECODER_MISMATCH, as_of_slot=10)

    def test_malformed_selector_reason_codes_abstain(self) -> None:
        """Loaded selector reason codes must be immutable string tuples."""

        result = validate_decision_snapshot_bundle(
            _bundle(
                selector=replace(
                    _selector(),
                    reason_codes=cast("Any", "selector_passed"),
                )
            )
        )

        self.assert_abstains(result, AbstainReason.MISSING_FEATURE, as_of_slot=10)

    def test_trigger_market_state_version_mismatch_abstains(self) -> None:
        """Selector trigger evidence must match the bundle market-state version."""

        result = validate_decision_snapshot_bundle(
            _bundle(
                selector=replace(
                    _selector(),
                    trigger_market_state_snapshot_version="other-market-v1",
                )
            )
        )

        self.assert_abstains(result, AbstainReason.DECODER_MISMATCH, as_of_slot=10)

    def test_trigger_profile_version_mismatch_abstains(self) -> None:
        """Selector trigger evidence must match matcher profile versions."""

        result = validate_decision_snapshot_bundle(
            _bundle(
                selector=replace(
                    _selector(),
                    trigger_operator_profile_version="profile-v2",
                )
            )
        )

        self.assert_abstains(result, AbstainReason.DECODER_MISMATCH, as_of_slot=10)

    def test_selected_without_historical_support_abstains(self) -> None:
        """Selectors cannot mark a regime active without required support."""

        result = validate_decision_snapshot_bundle(
            _bundle(selector=replace(_selector(), historical_launch_count=2))
        )

        self.assert_abstains(
            result,
            AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
            as_of_slot=10,
        )

    def test_float_historical_support_abstains(self) -> None:
        """Historical support counts must be integer counts."""

        result = validate_decision_snapshot_bundle(
            _bundle(
                selector=replace(
                    _selector(),
                    historical_launch_count=cast("Any", 5.5),
                )
            )
        )

        self.assert_abstains(
            result,
            AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
            as_of_slot=10,
        )

    def test_non_monotonic_horizon_probabilities_abstain(self) -> None:
        """Coherent dump probabilities must not decrease over longer horizons."""

        result = validate_decision_snapshot_bundle(
            _bundle(
                timing=replace(
                    _timing(),
                    p_dump_next_3s_ppm=250_000,
                    p_dump_next_5s_ppm=200_000,
                )
            )
        )

        self.assert_abstains(
            result,
            AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
            as_of_slot=10,
        )

    def test_non_monotonic_time_quantiles_abstain(self) -> None:
        """Remaining-time quantiles must be internally coherent."""

        result = validate_decision_snapshot_bundle(
            _bundle(
                timing=replace(
                    _timing(),
                    q10_remaining_dump_time_ms=2_500,
                    q50_remaining_dump_time_ms=2_000,
                )
            )
        )

        self.assert_abstains(
            result,
            AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
            as_of_slot=10,
        )

    def test_float_time_quantile_abstains(self) -> None:
        """Timing quantiles must be integer milliseconds."""

        result = validate_decision_snapshot_bundle(
            _bundle(
                timing=replace(
                    _timing(),
                    q10_remaining_dump_time_ms=cast("Any", 2_000.5),
                )
            )
        )

        self.assert_abstains(
            result,
            AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
            as_of_slot=10,
        )

    def test_snapshot_validator_stays_pure_and_integer_only(self) -> None:
        """Snapshot validation must not grow adapters, signers, or floats."""

        source = SNAPSHOT_MODULE.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(SNAPSHOT_MODULE))
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


def _bundle(**overrides: object) -> DecisionSnapshotBundle:
    as_of_slot = cast("Slot", overrides.get("as_of_slot", DEFAULT_SLOT))
    matcher = overrides.get("matcher")
    selector = overrides.get("selector")
    timing = overrides.get("timing")
    return DecisionSnapshotBundle(
        as_of_slot=as_of_slot,
        snapshot_bundle_version=cast(
            "str",
            overrides.get("snapshot_bundle_version", "bundle-v1"),
        ),
        feature_snapshot_version=cast(
            "str",
            overrides.get("feature_snapshot_version", "features-v1"),
        ),
        market_state_snapshot_version=cast(
            "str",
            overrides.get("market_state_snapshot_version", "market-state-v1"),
        ),
        matcher=(
            matcher
            if isinstance(matcher, LaunchMatcherSnapshot)
            else _matcher(as_of_slot=as_of_slot)
        ),
        selector=(
            selector
            if isinstance(selector, RuggerSelectorSnapshot)
            else _selector(as_of_slot=as_of_slot)
        ),
        timing=(
            timing
            if isinstance(timing, RugTimingSnapshot)
            else _timing(as_of_slot=as_of_slot)
        ),
    )


def _policy(*, as_of_slot: Slot = DEFAULT_SLOT) -> DecisionSnapshotPolicy:
    return DecisionSnapshotPolicy(
        as_of_slot=as_of_slot,
        policy_version="decision-policy-v1",
        require_selected_operator_churn_audit=True,
        accepted_operator_churn_snapshot_versions=(
            OPERATOR_WALLET_CHURN_SNAPSHOT_VERSION,
        ),
    )


def _matcher(*, as_of_slot: Slot = DEFAULT_SLOT) -> LaunchMatcherSnapshot:
    return LaunchMatcherSnapshot(
        as_of_slot=as_of_slot,
        entity_id="entity-1",
        regime_id="fake-pump-v1",
        entity_probability_ppm=900_000,
        regime_probability_ppm=850_000,
        entity_graph_snapshot_version="graph-v1",
        operator_profile_version="profile-v1",
        regime_model_version="regime-v1",
        matcher_version="matcher-v1",
    )


def _selector(*, as_of_slot: Slot = DEFAULT_SLOT) -> RuggerSelectorSnapshot:
    return RuggerSelectorSnapshot(
        as_of_slot=as_of_slot,
        selector_version="selector-v1",
        is_selected=True,
        min_entity_probability_ppm=800_000,
        min_regime_probability_ppm=800_000,
        min_trigger_risk_ppm=500_000,
        max_trigger_risk_ppm=600_000,
        min_historical_launches=3,
        historical_launch_count=5,
        trigger_generator_version="rules-v1",
        trigger_feature_schema_version="features-v1",
        trigger_labeler_version="labels-v1",
        trigger_row_schema_version="rows-v1",
        trigger_market_state_snapshot_version="market-state-v1",
        trigger_operator_profile_version="profile-v1",
        trigger_regime_model_version="regime-v1",
        reason_codes=("selector_passed",),
    )


def _selector_with_churn_audit(
    *,
    as_of_slot: Slot = DEFAULT_SLOT,
) -> RuggerSelectorSnapshot:
    return replace(
        _selector(as_of_slot=as_of_slot),
        operator_churn_snapshot_version=OPERATOR_WALLET_CHURN_SNAPSHOT_VERSION,
        max_operator_churn_new_high_risk_roles=1,
        observed_operator_churn_new_high_risk_roles=0,
        max_operator_churn_address_turnover_ppm=500_000,
        observed_operator_churn_address_turnover_ppm=0,
        max_operator_churn_retained_role_changes=1,
        observed_operator_churn_retained_role_changes=0,
    )


def _timing(*, as_of_slot: Slot = DEFAULT_SLOT) -> RugTimingSnapshot:
    return RugTimingSnapshot(
        as_of_slot=as_of_slot,
        timing_model_version="timing-v1",
        p_dump_next_1s_ppm=100_000,
        p_dump_next_3s_ppm=200_000,
        p_dump_next_5s_ppm=300_000,
        p_dump_next_10s_ppm=450_000,
        q05_remaining_dump_time_ms=1_500,
        q10_remaining_dump_time_ms=2_000,
        q50_remaining_dump_time_ms=5_000,
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
        "pair" + "wise",
    )


if __name__ == "__main__":
    unittest.main()
