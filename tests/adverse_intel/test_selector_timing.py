"""Selector and timing snapshot builder tests."""

import ast
import unittest
from dataclasses import replace
from pathlib import Path
from typing import Any, cast

from rugbot.decision.selector_timing import (
    DiscreteHazardBin,
    DumpHazardForecast,
    OperatorChurnSelectorGate,
    OperatorChurnSelectorPolicy,
    RuggerSelectorConfig,
    SelectorDecisionReason,
    SelectorSupportEvidence,
    build_rug_timing_snapshot,
    build_rugger_selector_snapshot,
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
from rugbot.graph.entity_resolution import AddressRole
from rugbot.graph.wallet_churn import (
    HIGH_RISK_CHURN_ROLES,
    OPERATOR_WALLET_CHURN_SNAPSHOT_VERSION,
    OperatorWalletChurnSnapshot,
    WalletChurnAddress,
    WalletChurnStatus,
)
from rugbot.models.rule_hypotheses import (
    ObservedTriggerEvaluation,
    OperatorAction,
    RuleExpressionKind,
    RuleHypothesisMatch,
    TriggerMatchStatus,
)

SELECTOR_TIMING_MODULE = Path("src/rugbot/decision/selector_timing.py")
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
DEFAULT_OPERATOR_CHURN = object()


class SelectorTimingTests(unittest.TestCase):
    """Tests for selector and timing snapshot builders."""

    def test_selector_passes_when_match_trigger_and_support_pass(self) -> None:
        """Selector returns selected=true only when all configured gates pass."""

        result = build_rugger_selector_snapshot(
            matcher=_matcher(),
            trigger=_trigger(),
            support=_support(),
            config=_selector_config(),
        )

        self.assertIsInstance(result, RuggerSelectorSnapshot)
        selector = cast("RuggerSelectorSnapshot", result)
        self.assertTrue(selector.is_selected)
        self.assertEqual(
            selector.reason_codes, (SelectorDecisionReason.SELECTOR_PASSED.value,)
        )
        self.assertEqual(selector.min_trigger_risk_ppm, 500_000)
        self.assertEqual(selector.max_trigger_risk_ppm, 600_000)
        self.assertEqual(selector.trigger_generator_version, "rules-v1")
        self.assertEqual(selector.trigger_market_state_snapshot_version, "market-v1")
        self.assertEqual(selector.historical_launch_count, 7)
        self.assertEqual(selector.min_historical_launches, 5)

    def test_selector_passes_with_required_low_churn_snapshot(self) -> None:
        """Known-operator selection can require fresh low-churn evidence."""

        result = build_rugger_selector_snapshot(
            matcher=_matcher(),
            trigger=_trigger(),
            support=_support(),
            config=_selector_config(),
            operator_churn_gate=_churn_gate(),
        )

        self.assertIsInstance(result, RuggerSelectorSnapshot)
        selector = cast("RuggerSelectorSnapshot", result)
        self.assertTrue(selector.is_selected)
        self.assertEqual(
            selector.operator_churn_snapshot_version,
            OPERATOR_WALLET_CHURN_SNAPSHOT_VERSION,
        )
        self.assertEqual(selector.max_operator_churn_address_turnover_ppm, 500_000)
        self.assertEqual(selector.observed_operator_churn_address_turnover_ppm, 0)

    def test_selector_missing_required_churn_snapshot_abstains(self) -> None:
        """A required churn gate cannot silently treat missing evidence as safe."""

        result = build_rugger_selector_snapshot(
            matcher=_matcher(),
            trigger=_trigger(),
            support=_support(),
            config=_selector_config(),
            operator_churn_gate=_churn_gate(operator_churn=None),
        )

        self.assert_abstains(result, AbstainReason.MISSING_FEATURE, as_of_slot=30)

    def test_selector_high_new_churn_roles_returns_unselected_snapshot(self) -> None:
        """High-risk newly introduced roles block selection without corruption."""

        result = build_rugger_selector_snapshot(
            matcher=_matcher(),
            trigger=_trigger(),
            support=_support(),
            config=_selector_config(),
            operator_churn_gate=_churn_gate(
                operator_churn=_operator_churn(
                    new_addresses=(
                        _churn_address(
                            "new-funder",
                            status=WalletChurnStatus.NEW,
                            roles=(AddressRole.FUNDER,),
                        ),
                    ),
                ),
                policy=replace(_churn_policy(), max_new_high_risk_roles=0),
            ),
        )

        self.assertIsInstance(result, RuggerSelectorSnapshot)
        selector = cast("RuggerSelectorSnapshot", result)
        self.assertFalse(selector.is_selected)
        self.assertEqual(
            selector.reason_codes,
            (
                SelectorDecisionReason.OPERATOR_CHURN_NEW_HIGH_RISK_ROLES_ABOVE_CAP.value,
            ),
        )

    def test_selector_new_creator_churn_returns_unselected_snapshot(self) -> None:
        """Launch-origin wallet switches trip the high-risk churn cap."""

        result = build_rugger_selector_snapshot(
            matcher=_matcher(),
            trigger=_trigger(),
            support=_support(),
            config=_selector_config(),
            operator_churn_gate=_churn_gate(
                operator_churn=_operator_churn(
                    new_addresses=(
                        _churn_address(
                            "new-creator",
                            status=WalletChurnStatus.NEW,
                            roles=(AddressRole.CREATOR,),
                        ),
                        _churn_address(
                            "new-submitter",
                            status=WalletChurnStatus.NEW,
                            roles=(AddressRole.CREATION_SUBMITTER,),
                        ),
                    ),
                    retained_addresses=(
                        _churn_address(
                            "stable-a",
                            status=WalletChurnStatus.RETAINED,
                            roles=(AddressRole.FUNDER,),
                        ),
                        _churn_address(
                            "stable-b",
                            status=WalletChurnStatus.RETAINED,
                            roles=(AddressRole.DUMPER,),
                        ),
                        _churn_address(
                            "stable-c",
                            status=WalletChurnStatus.RETAINED,
                            roles=(AddressRole.RELAY_ADDRESS,),
                        ),
                    ),
                ),
                policy=replace(
                    _churn_policy(),
                    max_new_high_risk_roles=1,
                    max_address_turnover_ppm=500_000,
                ),
            ),
        )

        self.assertIsInstance(result, RuggerSelectorSnapshot)
        selector = cast("RuggerSelectorSnapshot", result)
        self.assertFalse(selector.is_selected)
        self.assertEqual(selector.max_operator_churn_new_high_risk_roles, 1)
        self.assertEqual(selector.observed_operator_churn_new_high_risk_roles, 2)
        self.assertEqual(selector.max_operator_churn_address_turnover_ppm, 500_000)
        self.assertEqual(selector.observed_operator_churn_address_turnover_ppm, 250_000)
        self.assertEqual(selector.max_operator_churn_retained_role_changes, 1)
        self.assertEqual(selector.observed_operator_churn_retained_role_changes, 0)
        self.assertEqual(
            selector.reason_codes,
            (
                SelectorDecisionReason.OPERATOR_CHURN_NEW_HIGH_RISK_ROLES_ABOVE_CAP.value,
            ),
        )

    def test_selector_high_churn_turnover_returns_unselected_snapshot(self) -> None:
        """Excessive address turnover means the known-operator match is unstable."""

        result = build_rugger_selector_snapshot(
            matcher=_matcher(),
            trigger=_trigger(),
            support=_support(),
            config=_selector_config(),
            operator_churn_gate=_churn_gate(
                operator_churn=_operator_churn(
                    new_addresses=(
                        _churn_address(
                            "new-creator",
                            status=WalletChurnStatus.NEW,
                            roles=(AddressRole.CREATOR,),
                        ),
                    ),
                    retained_addresses=(),
                    retired_addresses=(
                        _churn_address(
                            "retired-dumper",
                            status=WalletChurnStatus.RETIRED,
                            roles=(AddressRole.DUMPER,),
                        ),
                    ),
                ),
                policy=replace(_churn_policy(), max_address_turnover_ppm=500_000),
            ),
        )

        self.assertIsInstance(result, RuggerSelectorSnapshot)
        selector = cast("RuggerSelectorSnapshot", result)
        self.assertFalse(selector.is_selected)
        self.assertEqual(
            selector.reason_codes,
            (SelectorDecisionReason.OPERATOR_CHURN_ADDRESS_TURNOVER_ABOVE_CAP.value,),
        )

    def test_selector_high_retained_role_changes_returns_unselected_snapshot(
        self,
    ) -> None:
        """Retained wallets changing roles can also invalidate the match."""

        result = build_rugger_selector_snapshot(
            matcher=_matcher(),
            trigger=_trigger(),
            support=_support(),
            config=_selector_config(),
            operator_churn_gate=_churn_gate(
                operator_churn=_operator_churn(retained_role_change_count=1),
                policy=replace(_churn_policy(), max_retained_role_changes=0),
            ),
        )

        self.assertIsInstance(result, RuggerSelectorSnapshot)
        selector = cast("RuggerSelectorSnapshot", result)
        self.assertFalse(selector.is_selected)
        self.assertEqual(
            selector.reason_codes,
            (
                SelectorDecisionReason.OPERATOR_CHURN_RETAINED_ROLE_CHANGES_ABOVE_CAP.value,
            ),
        )

    def test_selector_below_entity_threshold_returns_unselected_snapshot(self) -> None:
        """Threshold misses are skip snapshots, not malformed-input abstentions."""

        result = build_rugger_selector_snapshot(
            matcher=replace(_matcher(), entity_probability_ppm=600_000),
            trigger=_trigger(),
            support=_support(),
            config=_selector_config(),
        )

        self.assertIsInstance(result, RuggerSelectorSnapshot)
        selector = cast("RuggerSelectorSnapshot", result)
        self.assertFalse(selector.is_selected)
        self.assertEqual(
            selector.reason_codes,
            (SelectorDecisionReason.ENTITY_PROBABILITY_BELOW_THRESHOLD.value,),
        )

    def test_selector_below_regime_threshold_returns_unselected_snapshot(self) -> None:
        """Regime threshold misses are skip snapshots, not abstentions."""

        result = build_rugger_selector_snapshot(
            matcher=replace(_matcher(), regime_probability_ppm=600_000),
            trigger=_trigger(),
            support=_support(),
            config=_selector_config(),
        )

        self.assertIsInstance(result, RuggerSelectorSnapshot)
        selector = cast("RuggerSelectorSnapshot", result)
        self.assertFalse(selector.is_selected)
        self.assertEqual(
            selector.reason_codes,
            (SelectorDecisionReason.REGIME_PROBABILITY_BELOW_THRESHOLD.value,),
        )

    def test_selector_below_historical_support_returns_unselected_snapshot(
        self,
    ) -> None:
        """Historical support threshold misses are skip snapshots."""

        result = build_rugger_selector_snapshot(
            matcher=_matcher(),
            trigger=_trigger(),
            support=replace(_support(), historical_launch_count=2),
            config=_selector_config(),
        )

        self.assertIsInstance(result, RuggerSelectorSnapshot)
        selector = cast("RuggerSelectorSnapshot", result)
        self.assertFalse(selector.is_selected)
        self.assertEqual(
            selector.reason_codes,
            (SelectorDecisionReason.HISTORICAL_SUPPORT_BELOW_THRESHOLD.value,),
        )

    def test_selector_below_trigger_risk_returns_unselected_snapshot(self) -> None:
        """Low observed trigger risk skips selection without claiming corruption."""

        result = build_rugger_selector_snapshot(
            matcher=_matcher(),
            trigger=_trigger(risk=300_000),
            support=_support(),
            config=_selector_config(),
        )

        self.assertIsInstance(result, RuggerSelectorSnapshot)
        selector = cast("RuggerSelectorSnapshot", result)
        self.assertFalse(selector.is_selected)
        self.assertEqual(
            selector.reason_codes,
            (SelectorDecisionReason.TRIGGER_RISK_BELOW_THRESHOLD.value,),
        )

    def test_stale_trigger_abstains(self) -> None:
        """All selector inputs must share as_of_slot."""

        result = build_rugger_selector_snapshot(
            matcher=_matcher(),
            trigger=_trigger(as_of_slot=29),
            support=_support(),
            config=_selector_config(),
        )

        self.assert_abstains(result, AbstainReason.STALE_STATE, as_of_slot=30)

    def test_stale_matcher_abstains(self) -> None:
        """Selector cannot combine matcher evidence from another slot."""

        result = build_rugger_selector_snapshot(
            matcher=replace(_matcher(), as_of_slot=Slot(29)),
            trigger=_trigger(),
            support=_support(),
            config=_selector_config(),
        )

        self.assert_abstains(result, AbstainReason.STALE_STATE, as_of_slot=30)

    def test_stale_support_abstains(self) -> None:
        """Selector cannot combine support evidence from another slot."""

        result = build_rugger_selector_snapshot(
            matcher=_matcher(),
            trigger=_trigger(),
            support=replace(_support(), as_of_slot=Slot(29)),
            config=_selector_config(),
        )

        self.assert_abstains(result, AbstainReason.STALE_STATE, as_of_slot=30)

    def test_support_profile_version_mismatch_abstains(self) -> None:
        """Support evidence must match matcher profile and regime versions."""

        result = build_rugger_selector_snapshot(
            matcher=_matcher(),
            trigger=_trigger(),
            support=replace(_support(), operator_profile_version="profile-v2"),
            config=_selector_config(),
        )

        self.assert_abstains(result, AbstainReason.DECODER_MISMATCH, as_of_slot=30)

    def test_trigger_target_action_mismatch_abstains(self) -> None:
        """Selector cannot evaluate a trigger for another operator action."""

        result = build_rugger_selector_snapshot(
            matcher=_matcher(),
            trigger=_trigger(action=OperatorAction.PARTIAL_SELL),
            support=_support(),
            config=_selector_config(),
        )

        self.assert_abstains(
            result,
            AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
            as_of_slot=30,
        )

    def test_loaded_trigger_max_risk_mismatch_abstains(self) -> None:
        """Loaded trigger evaluations are defensively revalidated."""

        result = build_rugger_selector_snapshot(
            matcher=_matcher(),
            trigger=replace(_trigger(), max_trigger_risk_ppm=900_000),
            support=_support(),
            config=_selector_config(),
        )

        self.assert_abstains(
            result,
            AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
            as_of_slot=30,
        )

    def test_float_trigger_risk_abstains(self) -> None:
        """Trigger probabilities must be integer PPM."""

        result = build_rugger_selector_snapshot(
            matcher=_matcher(),
            trigger=replace(_trigger(), max_trigger_risk_ppm=cast("Any", 0.5)),
            support=_support(),
            config=_selector_config(),
        )

        self.assert_abstains(
            result,
            AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
            as_of_slot=30,
        )

    def test_stale_operator_churn_abstains(self) -> None:
        """Churn evidence must share the selector as_of_slot."""

        result = build_rugger_selector_snapshot(
            matcher=_matcher(),
            trigger=_trigger(),
            support=_support(),
            config=_selector_config(),
            operator_churn_gate=_churn_gate(
                operator_churn=replace(_operator_churn(), as_of_slot=Slot(29)),
            ),
        )

        self.assert_abstains(result, AbstainReason.STALE_STATE, as_of_slot=30)

    def test_operator_churn_entity_mismatch_abstains(self) -> None:
        """Churn evidence for another entity cannot support selection."""

        result = build_rugger_selector_snapshot(
            matcher=_matcher(),
            trigger=_trigger(),
            support=_support(),
            config=_selector_config(),
            operator_churn_gate=_churn_gate(
                operator_churn=replace(_operator_churn(), entity_id="entity-2"),
            ),
        )

        self.assert_abstains(
            result,
            AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
            as_of_slot=30,
        )

    def test_operator_churn_version_mismatch_abstains(self) -> None:
        """Selector consumes only accepted churn snapshot versions."""

        result = build_rugger_selector_snapshot(
            matcher=_matcher(),
            trigger=_trigger(),
            support=_support(),
            config=_selector_config(),
            operator_churn_gate=_churn_gate(
                operator_churn=replace(
                    _operator_churn(),
                    churn_snapshot_version="wallet-churn-v1",
                ),
            ),
        )

        self.assert_abstains(result, AbstainReason.DECODER_MISMATCH, as_of_slot=30)

    def test_loaded_operator_churn_count_mismatch_abstains(self) -> None:
        """Loaded churn counts are revalidated against the address records."""

        result = build_rugger_selector_snapshot(
            matcher=_matcher(),
            trigger=_trigger(),
            support=_support(),
            config=_selector_config(),
            operator_churn_gate=_churn_gate(
                operator_churn=replace(_operator_churn(), new_address_count=1),
            ),
        )

        self.assert_abstains(
            result,
            AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
            as_of_slot=30,
        )

    def test_operator_churn_turnover_mismatch_abstains(self) -> None:
        """Loaded churn turnover is recomputed from preserved counts."""

        result = build_rugger_selector_snapshot(
            matcher=_matcher(),
            trigger=_trigger(),
            support=_support(),
            config=_selector_config(),
            operator_churn_gate=_churn_gate(
                operator_churn=replace(
                    _operator_churn(
                        new_addresses=(
                            _churn_address(
                                "new-funder",
                                status=WalletChurnStatus.NEW,
                                roles=(AddressRole.FUNDER,),
                            ),
                        ),
                    ),
                    address_turnover_ppm=1,
                ),
            ),
        )

        self.assert_abstains(
            result,
            AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
            as_of_slot=30,
        )

    def test_float_operator_churn_turnover_abstains(self) -> None:
        """Churn turnover must be integer PPM."""

        result = build_rugger_selector_snapshot(
            matcher=_matcher(),
            trigger=_trigger(),
            support=_support(),
            config=_selector_config(),
            operator_churn_gate=_churn_gate(
                operator_churn=replace(
                    _operator_churn(),
                    address_turnover_ppm=cast("Any", 0.5),
                ),
            ),
        )

        self.assert_abstains(
            result,
            AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
            as_of_slot=30,
        )

    def test_operator_churn_address_records_must_be_tuples(self) -> None:
        """Mutable loaded churn address containers fail closed."""

        result = build_rugger_selector_snapshot(
            matcher=_matcher(),
            trigger=_trigger(),
            support=_support(),
            config=_selector_config(),
            operator_churn_gate=_churn_gate(
                operator_churn=replace(
                    _operator_churn(),
                    retained_addresses=cast(
                        "Any", list(_operator_churn().retained_addresses)
                    ),
                ),
            ),
        )

        self.assert_abstains(
            result,
            AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
            as_of_slot=30,
        )

    def test_operator_churn_missing_address_records_abstains(self) -> None:
        """Unsized malformed address containers abstain instead of crashing."""

        result = build_rugger_selector_snapshot(
            matcher=_matcher(),
            trigger=_trigger(),
            support=_support(),
            config=_selector_config(),
            operator_churn_gate=_churn_gate(
                operator_churn=replace(
                    _operator_churn(),
                    retained_addresses=cast("Any", None),
                ),
            ),
        )

        self.assert_abstains(
            result,
            AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
            as_of_slot=30,
        )

    def test_timing_forecast_builds_snapshot(self) -> None:
        """Coherent precomputed timing forecasts publish timing snapshots."""

        result = build_rug_timing_snapshot(forecast=_forecast())

        self.assertIsInstance(result, RugTimingSnapshot)
        timing = cast("RugTimingSnapshot", result)
        self.assertEqual(timing.as_of_slot, Slot(30))
        self.assertEqual(timing.p_dump_next_1s_ppm, 200_000)
        self.assertEqual(timing.p_dump_next_10s_ppm, 664_000)
        self.assertEqual(timing.q10_remaining_dump_time_ms, 1_000)
        self.assertEqual(timing.q50_remaining_dump_time_ms, 5_000)

    def test_non_monotonic_hazard_horizons_abstain(self) -> None:
        """Timing hazard bins must be strictly ordered."""

        result = build_rug_timing_snapshot(
            forecast=replace(
                _forecast(),
                bins=(
                    _bin(horizon=1_000, hazard=200_000),
                    _bin(horizon=1_000, hazard=250_000),
                ),
            )
        )

        self.assert_abstains(
            result,
            AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
            as_of_slot=30,
        )

    def test_stale_hazard_bin_abstains(self) -> None:
        """Timing bins must share the forecast as_of_slot."""

        result = build_rug_timing_snapshot(
            forecast=replace(
                _forecast(),
                bins=(
                    _bin(horizon=1_000, hazard=200_000, as_of_slot=29),
                    _bin(horizon=3_000, hazard=250_000),
                    _bin(horizon=5_000, hazard=300_000),
                    _bin(horizon=10_000, hazard=200_000),
                ),
            )
        )

        self.assert_abstains(result, AbstainReason.STALE_STATE, as_of_slot=30)

    def test_missing_fixed_horizon_boundary_abstains(self) -> None:
        """Timing forecasts must expose exact fixed decision horizons."""

        result = build_rug_timing_snapshot(
            forecast=replace(
                _forecast(),
                bins=(
                    _bin(horizon=2_000, hazard=600_000),
                    _bin(horizon=3_000, hazard=300_000),
                    _bin(horizon=5_000, hazard=300_000),
                    _bin(horizon=10_000, hazard=200_000),
                ),
            )
        )

        self.assert_abstains(result, AbstainReason.MISSING_FEATURE, as_of_slot=30)

    def test_float_timing_probability_abstains(self) -> None:
        """Timing forecast probabilities must be integer PPM."""

        result = build_rug_timing_snapshot(
            forecast=replace(
                _forecast(),
                bins=(
                    _bin(horizon=1_000, hazard=cast("Any", 0.25)),
                    _bin(horizon=3_000, hazard=250_000),
                    _bin(horizon=5_000, hazard=300_000),
                    _bin(horizon=10_000, hazard=200_000),
                ),
            )
        )

        self.assert_abstains(
            result,
            AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
            as_of_slot=30,
        )

    def test_timing_forecast_without_q50_horizon_abstains(self) -> None:
        """Timing forecast must support q50 within the covered horizon."""

        result = build_rug_timing_snapshot(
            forecast=replace(
                _forecast(),
                bins=(
                    _bin(horizon=1_000, hazard=10_000),
                    _bin(horizon=3_000, hazard=10_000),
                    _bin(horizon=5_000, hazard=10_000),
                    _bin(horizon=10_000, hazard=10_000),
                ),
            )
        )

        self.assert_abstains(
            result,
            AbstainReason.MISSING_FEATURE,
            as_of_slot=30,
        )

    def test_selector_and_timing_outputs_validate_as_bundle(self) -> None:
        """Generated selector and timing snapshots fit DecisionSnapshotBundle."""

        selector = build_rugger_selector_snapshot(
            matcher=_matcher(),
            trigger=_trigger(),
            support=_support(),
            config=_selector_config(),
        )
        timing = build_rug_timing_snapshot(forecast=_forecast())
        self.assertIsInstance(selector, RuggerSelectorSnapshot)
        self.assertIsInstance(timing, RugTimingSnapshot)
        bundle = DecisionSnapshotBundle(
            as_of_slot=Slot(30),
            snapshot_bundle_version="bundle-v1",
            feature_snapshot_version="features-v1",
            market_state_snapshot_version="market-v1",
            matcher=_matcher(),
            selector=cast("RuggerSelectorSnapshot", selector),
            timing=cast("RugTimingSnapshot", timing),
        )

        result = validate_decision_snapshot_bundle(bundle)

        self.assertIs(result, bundle)

    def test_selector_timing_module_stays_pure_and_integer_only(self) -> None:
        """Selector and timing builders must not grow adapters, signers, or floats."""

        source = SELECTOR_TIMING_MODULE.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(SELECTOR_TIMING_MODULE))
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


def _selector_config() -> RuggerSelectorConfig:
    return RuggerSelectorConfig(
        as_of_slot=Slot(30),
        selector_version="selector-v1",
        target_action=OperatorAction.FULL_DUMP,
        min_entity_probability_ppm=800_000,
        min_regime_probability_ppm=800_000,
        min_historical_launches=5,
        min_trigger_risk_ppm=500_000,
    )


def _matcher() -> LaunchMatcherSnapshot:
    return LaunchMatcherSnapshot(
        as_of_slot=Slot(30),
        entity_id="entity-1",
        regime_id="regime-a",
        entity_probability_ppm=900_000,
        regime_probability_ppm=850_000,
        entity_graph_snapshot_version="graph-v1",
        operator_profile_version="profile-v1",
        regime_model_version="regime-v1",
        matcher_version="matcher-v1",
    )


def _support() -> SelectorSupportEvidence:
    return SelectorSupportEvidence(
        as_of_slot=Slot(30),
        entity_id="entity-1",
        regime_id="regime-a",
        historical_launch_count=7,
        support_snapshot_version="support-v1",
        operator_profile_version="profile-v1",
        regime_model_version="regime-v1",
        evidence_ids=("support-evidence",),
    )


def _churn_policy() -> OperatorChurnSelectorPolicy:
    return OperatorChurnSelectorPolicy(
        require_churn_snapshot=True,
        accepted_churn_snapshot_versions=(OPERATOR_WALLET_CHURN_SNAPSHOT_VERSION,),
        max_new_high_risk_roles=1,
        max_address_turnover_ppm=500_000,
        max_retained_role_changes=1,
    )


def _churn_gate(
    *,
    operator_churn: OperatorWalletChurnSnapshot
    | None
    | object = DEFAULT_OPERATOR_CHURN,
    policy: OperatorChurnSelectorPolicy | None = None,
) -> OperatorChurnSelectorGate:
    selected_operator_churn = (
        _operator_churn()
        if operator_churn is DEFAULT_OPERATOR_CHURN
        else cast("OperatorWalletChurnSnapshot | None", operator_churn)
    )
    return OperatorChurnSelectorGate(
        operator_churn=selected_operator_churn,
        policy=policy if policy is not None else _churn_policy(),
    )


def _operator_churn(
    *,
    new_addresses: tuple[WalletChurnAddress, ...] = (),
    retained_addresses: tuple[WalletChurnAddress, ...] | None = None,
    retired_addresses: tuple[WalletChurnAddress, ...] = (),
    retained_role_change_count: int = 0,
) -> OperatorWalletChurnSnapshot:
    selected_retained_addresses = (
        retained_addresses
        if retained_addresses is not None
        else (
            _churn_address(
                "stable-creator",
                status=WalletChurnStatus.RETAINED,
                roles=(AddressRole.CREATOR,),
            ),
        )
    )
    new_high_risk_role_count = sum(
        address.high_risk_role_count for address in new_addresses
    )
    current_active_address_count = len(new_addresses) + len(selected_retained_addresses)
    previous_active_address_count = len(retired_addresses) + len(
        selected_retained_addresses
    )
    address_turnover_ppm = _address_turnover_ppm(
        new_count=len(new_addresses),
        retired_count=len(retired_addresses),
        previous_count=previous_active_address_count,
        current_count=current_active_address_count,
    )
    return OperatorWalletChurnSnapshot(
        as_of_slot=Slot(30),
        entity_id="entity-1",
        churn_snapshot_version=OPERATOR_WALLET_CHURN_SNAPSHOT_VERSION,
        current_profile_version="profile-v1",
        previous_profile_version="profile-v1",
        previous_as_of_slot=Slot(20),
        current_active_address_count=current_active_address_count,
        previous_active_address_count=previous_active_address_count,
        new_address_count=len(new_addresses),
        retained_address_count=len(selected_retained_addresses),
        retired_address_count=len(retired_addresses),
        new_high_risk_role_count=new_high_risk_role_count,
        retained_role_change_count=retained_role_change_count,
        address_turnover_ppm=address_turnover_ppm,
        new_addresses=new_addresses,
        retained_addresses=selected_retained_addresses,
        retired_addresses=retired_addresses,
        evidence_ids=("churn-evidence",),
        reason_codes=("operator_wallet_churn_snapshot_built",),
    )


def _churn_address(
    address: str,
    *,
    status: WalletChurnStatus,
    roles: tuple[AddressRole, ...],
    as_of_slot: int = 30,
    entity_id: str = "entity-1",
) -> WalletChurnAddress:
    return WalletChurnAddress(
        as_of_slot=Slot(as_of_slot),
        entity_id=entity_id,
        address=address,
        status=status,
        membership_probability_ppm=900_000,
        same_controller_probability_ppm=900_000,
        cooperating_probability_ppm=100_000,
        roles=roles,
        high_risk_role_count=sum(1 for role in roles if role in HIGH_RISK_CHURN_ROLES),
        evidence_ids=(f"{address}-churn-evidence",),
        model_version="churn-address-v1",
    )


def _address_turnover_ppm(
    *,
    new_count: int,
    retired_count: int,
    previous_count: int,
    current_count: int,
) -> int:
    denominator = previous_count + current_count
    if denominator == 0:
        return 0
    return (new_count + retired_count) * 1_000_000 // denominator


def _trigger(
    *,
    as_of_slot: int = 30,
    action: OperatorAction = OperatorAction.FULL_DUMP,
    risk: int = 600_000,
) -> ObservedTriggerEvaluation:
    return ObservedTriggerEvaluation(
        as_of_slot=Slot(as_of_slot),
        entity_id="entity-1",
        campaign_id="campaign-a",
        regime_id="regime-a",
        target_action=action,
        matches=(
            RuleHypothesisMatch(
                as_of_slot=Slot(as_of_slot),
                entity_id="entity-1",
                campaign_id="campaign-a",
                regime_id="regime-a",
                expression_kind=RuleExpressionKind.ELAPSED_MS_AT_OR_ABOVE,
                target_action=action,
                status=TriggerMatchStatus.INSIDE_OBSERVED_BAND,
                observed_value=1_100,
                threshold_q50_value=1_000,
                proximity_ppm=1_000_000,
                trigger_risk_ppm=risk,
                confidence_ppm=risk,
                precision_ppm=900_000,
                generator_version="rules-v1",
                feature_schema_version="features-v1",
                labeler_version="labels-v1",
                row_schema_version="rows-v1",
                operator_profile_version="profile-v1",
                regime_model_version="regime-v1",
            ),
        ),
        max_trigger_risk_ppm=risk,
        generator_version="rules-v1",
        feature_schema_version="features-v1",
        labeler_version="labels-v1",
        row_schema_version="rows-v1",
        market_state_snapshot_version="market-v1",
        operator_profile_version="profile-v1",
        regime_model_version="regime-v1",
        reason_codes=("observed_trigger_hypotheses_evaluated",),
    )


def _forecast() -> DumpHazardForecast:
    return DumpHazardForecast(
        as_of_slot=Slot(30),
        timing_model_version="timing-v1",
        forecast_snapshot_version="forecast-v1",
        bins=(
            _bin(horizon=1_000, hazard=200_000),
            _bin(horizon=3_000, hazard=250_000),
            _bin(horizon=5_000, hazard=300_000),
            _bin(horizon=10_000, hazard=200_000),
        ),
        evidence_ids=("forecast-evidence",),
    )


def _bin(*, horizon: int, hazard: int, as_of_slot: int = 30) -> DiscreteHazardBin:
    return DiscreteHazardBin(
        as_of_slot=Slot(as_of_slot),
        horizon_ms=horizon,
        hazard_ppm=hazard,
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
        "PRIVATE" + "_KEY",
        "send" + "_transaction",
        "send" + "_raw_transaction",
        "float(",
        "pair" + "wise",
    )


if __name__ == "__main__":
    unittest.main()
