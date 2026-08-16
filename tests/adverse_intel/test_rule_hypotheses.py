"""Rule-hypothesis and observed-trigger evaluation tests."""

import ast
import unittest
from dataclasses import replace
from pathlib import Path
from typing import Any, cast

from rugbot.domain.amounts import Slot
from rugbot.domain.decisions import AbstainReason, AbstainResult
from rugbot.models.rule_hypotheses import (
    ObservedTriggerEvaluation,
    ObservedTriggerEvaluationThresholds,
    OperatorAction,
    OperatorActionLabel,
    RuleExpressionKind,
    RuleHypothesis,
    RuleHypothesisArtifact,
    RuleHypothesisConfig,
    RuleHypothesisMatch,
    StateActionTrainingRow,
    TriggerFeatureSnapshot,
    TriggerMatchStatus,
    evaluate_observed_trigger_hypotheses,
    generate_rule_hypotheses,
)

RULE_MODULE = Path("src/rugbot/models/rule_hypotheses.py")
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


class RuleHypothesisTests(unittest.TestCase):
    """Tests for pure rule-hypothesis generation."""

    def test_generates_hypotheses_from_distinct_finalized_launches(self) -> None:
        """Stable target-action rows produce structured trigger hypotheses."""

        result = generate_rule_hypotheses(rows=_training_rows(), config=_config())

        self.assertIsInstance(result, RuleHypothesisArtifact)
        artifact = cast("RuleHypothesisArtifact", result)
        self.assertEqual(artifact.as_of_slot, Slot(100))
        self.assertEqual(artifact.target_action, OperatorAction.FULL_DUMP)
        self.assertGreaterEqual(artifact.accepted_hypothesis_count, 1)
        elapsed = _hypothesis(
            artifact,
            RuleExpressionKind.ELAPSED_MS_AT_OR_ABOVE,
        )
        self.assertEqual(elapsed.threshold_q10_value, 1_000)
        self.assertEqual(elapsed.threshold_q50_value, 1_100)
        self.assertEqual(elapsed.threshold_q90_value, 1_150)
        self.assertEqual(elapsed.distinct_launch_support, 4)
        self.assertEqual(elapsed.precision_ppm, 1_000_000)
        self.assertEqual(elapsed.generator_version, "rules-v1")
        self.assertEqual(artifact.min_distinct_launch_support, 4)
        self.assertEqual(artifact.min_precision_ppm, 700_000)
        self.assertEqual(artifact.min_confidence_ppm, 500_000)

    def test_duplicate_launches_do_not_satisfy_sparse_support(self) -> None:
        """Support is counted by distinct launch, not repeated rows."""

        result = generate_rule_hypotheses(
            rows=(
                _row(launch_id="launch-a", elapsed=1_000),
                _row(launch_id="launch-b", elapsed=1_050),
                _row(launch_id="launch-b", elapsed=1_075, row_slot=35),
            ),
            config=replace(_config(), min_distinct_launch_support=3),
        )

        self.assert_abstains(
            result,
            AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
            as_of_slot=100,
        )

    def test_temporal_leakage_abstains(self) -> None:
        """Feature state must precede the labeled operator action."""

        result = generate_rule_hypotheses(
            rows=(_row(feature_slot=20, action_slot=20),),
            config=replace(_config(), min_distinct_launch_support=1),
        )

        self.assert_abstains(result, AbstainReason.STALE_STATE, as_of_slot=100)

    def test_future_row_evidence_abstains(self) -> None:
        """Rows discovered after the artifact slot cannot enter the model."""

        result = generate_rule_hypotheses(
            rows=(_row(row_slot=101),),
            config=replace(_config(), min_distinct_launch_support=1),
        )

        self.assert_abstains(result, AbstainReason.STALE_STATE, as_of_slot=100)

    def test_campaign_mismatch_abstains(self) -> None:
        """Rows cannot be aggregated across campaigns or regimes."""

        result = generate_rule_hypotheses(
            rows=(_row(campaign_id="campaign-2"),),
            config=replace(_config(), min_distinct_launch_support=1),
        )

        self.assert_abstains(
            result,
            AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
            as_of_slot=100,
        )

    def test_float_feature_value_abstains(self) -> None:
        """Runtime validation rejects float-like feature values."""

        result = generate_rule_hypotheses(
            rows=(_row(elapsed=cast("Any", 1.5)),),
            config=replace(_config(), min_distinct_launch_support=1),
        )

        self.assert_abstains(
            result,
            AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
            as_of_slot=100,
        )

    def test_mutable_row_evidence_ids_abstain(self) -> None:
        """Published row provenance must use immutable tuple containers."""

        result = generate_rule_hypotheses(
            rows=(
                replace(
                    _row(),
                    evidence_ids=cast("Any", ["mutable-evidence"]),
                ),
            ),
            config=replace(_config(), min_distinct_launch_support=1),
        )

        self.assert_abstains(result, AbstainReason.MISSING_FEATURE, as_of_slot=100)

    def test_missing_generator_version_abstains(self) -> None:
        """Rule artifacts must be versioned."""

        result = generate_rule_hypotheses(
            rows=_training_rows(),
            config=replace(_config(), generator_version=""),
        )

        self.assert_abstains(result, AbstainReason.DECODER_MISMATCH, as_of_slot=100)

    def test_low_precision_hypotheses_abstain(self) -> None:
        """Weak trigger expressions are not published as low-confidence artifacts."""

        noisy_hold = _row(
            launch_id="hold-high",
            action=OperatorAction.HOLD,
            elapsed=2_000,
            quote_reserve=2_000_000,
            curve_progress=900_000,
            operator_pnl=200_000,
            buyer_count=20,
            idle_ms=2_000,
        )

        result = generate_rule_hypotheses(
            rows=(*_training_rows(), noisy_hold),
            config=replace(_config(), min_precision_ppm=900_000),
        )

        self.assert_abstains(result, AbstainReason.MISSING_FEATURE, as_of_slot=100)

    def test_evaluates_live_feature_against_observed_band(self) -> None:
        """Live evaluation reports proximity, status, and risk without trading."""

        artifact = self._artifact()

        result = evaluate_observed_trigger_hypotheses(
            feature=_feature(as_of_slot=100, elapsed=1_120, launch_id="live"),
            artifact=artifact,
            thresholds=_thresholds(),
        )

        self.assertIsInstance(result, ObservedTriggerEvaluation)
        evaluation = cast("ObservedTriggerEvaluation", result)
        self.assertGreaterEqual(evaluation.max_trigger_risk_ppm, 500_000)
        elapsed = _match(
            evaluation,
            RuleExpressionKind.ELAPSED_MS_AT_OR_ABOVE,
        )
        self.assertEqual(elapsed.status, TriggerMatchStatus.INSIDE_OBSERVED_BAND)
        self.assertEqual(elapsed.observed_value, 1_120)
        self.assertEqual(elapsed.labeler_version, "labels-v1")
        self.assertEqual(elapsed.row_schema_version, "rows-v1")
        self.assertEqual(evaluation.labeler_version, "labels-v1")
        self.assertEqual(evaluation.row_schema_version, "rows-v1")
        self.assertEqual(
            evaluation.reason_codes,
            (
                "observed_trigger_hypotheses_evaluated",
                "trigger_risk_threshold_crossed",
            ),
        )

    def test_live_feature_slot_mismatch_abstains(self) -> None:
        """Live features must share the artifact slot."""

        result = evaluate_observed_trigger_hypotheses(
            feature=_feature(as_of_slot=99, launch_id="live"),
            artifact=self._artifact(),
            thresholds=_thresholds(),
        )

        self.assert_abstains(result, AbstainReason.STALE_STATE, as_of_slot=100)

    def test_live_feature_version_mismatch_abstains(self) -> None:
        """Live feature schema must match the hypothesis artifact."""

        result = evaluate_observed_trigger_hypotheses(
            feature=replace(
                _feature(as_of_slot=100, launch_id="live"),
                feature_schema_version="features-v2",
            ),
            artifact=self._artifact(),
            thresholds=_thresholds(),
        )

        self.assert_abstains(result, AbstainReason.DECODER_MISMATCH, as_of_slot=100)

    def test_loaded_artifact_is_defensively_revalidated(self) -> None:
        """Malformed prebuilt artifacts abstain before live evaluation."""

        artifact = self._artifact()
        bad_hypothesis = replace(
            artifact.hypotheses[0],
            threshold_q10_value=9_999,
        )

        result = evaluate_observed_trigger_hypotheses(
            feature=_feature(as_of_slot=100, launch_id="live"),
            artifact=replace(artifact, hypotheses=(bad_hypothesis,)),
            thresholds=_thresholds(),
        )

        self.assert_abstains(
            result,
            AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
            as_of_slot=100,
        )

    def test_loaded_artifact_below_publication_gates_abstains(self) -> None:
        """Evaluation revalidates support, precision, and confidence gates."""

        artifact = self._artifact()
        weak_hypothesis = replace(
            artifact.hypotheses[0],
            distinct_launch_support=1,
            precision_ppm=100_000,
            confidence_ppm=900_000,
        )

        result = evaluate_observed_trigger_hypotheses(
            feature=_feature(as_of_slot=100, launch_id="live"),
            artifact=replace(
                artifact,
                hypotheses=(weak_hypothesis,),
                accepted_hypothesis_count=1,
            ),
            thresholds=_thresholds(),
        )

        self.assert_abstains(
            result,
            AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
            as_of_slot=100,
        )

    def test_loaded_artifact_future_seen_slot_abstains(self) -> None:
        """Hypotheses cannot contain source evidence newer than the artifact."""

        artifact = self._artifact()
        future_hypothesis = replace(
            artifact.hypotheses[0],
            last_seen_slot=Slot(101),
        )

        result = evaluate_observed_trigger_hypotheses(
            feature=_feature(as_of_slot=100, launch_id="live"),
            artifact=replace(
                artifact,
                hypotheses=(future_hypothesis,),
                accepted_hypothesis_count=1,
            ),
            thresholds=_thresholds(),
        )

        self.assert_abstains(result, AbstainReason.STALE_STATE, as_of_slot=100)

    def test_loaded_artifact_source_count_mismatch_abstains(self) -> None:
        """Hypothesis source counts must match the containing artifact."""

        artifact = self._artifact()
        mismatched_hypothesis = replace(
            artifact.hypotheses[0],
            source_distinct_launch_count=999,
        )

        result = evaluate_observed_trigger_hypotheses(
            feature=_feature(as_of_slot=100, launch_id="live"),
            artifact=replace(
                artifact,
                hypotheses=(mismatched_hypothesis,),
                accepted_hypothesis_count=1,
            ),
            thresholds=_thresholds(),
        )

        self.assert_abstains(
            result,
            AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
            as_of_slot=100,
        )

    def test_loaded_artifact_impossible_distinct_support_abstains(self) -> None:
        """Distinct launch support cannot exceed support row count."""

        artifact = self._artifact()
        impossible_hypothesis = replace(
            artifact.hypotheses[0],
            support_row_count=1,
            distinct_launch_support=artifact.min_distinct_launch_support,
        )

        result = evaluate_observed_trigger_hypotheses(
            feature=_feature(as_of_slot=100, launch_id="live"),
            artifact=replace(
                artifact,
                hypotheses=(impossible_hypothesis,),
                accepted_hypothesis_count=1,
            ),
            thresholds=_thresholds(),
        )

        self.assert_abstains(
            result,
            AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
            as_of_slot=100,
        )

    def test_rule_hypothesis_module_stays_pure_and_integer_only(self) -> None:
        """Rule hypothesis contracts must not grow adapters, signers, or floats."""

        source = RULE_MODULE.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(RULE_MODULE))
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

    def _artifact(self) -> RuleHypothesisArtifact:
        result = generate_rule_hypotheses(rows=_training_rows(), config=_config())
        self.assertIsInstance(result, RuleHypothesisArtifact)
        return cast("RuleHypothesisArtifact", result)

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


def _training_rows() -> tuple[StateActionTrainingRow, ...]:
    return (
        _row(launch_id="launch-a", elapsed=1_000, action_slot=20, row_slot=21),
        _row(launch_id="launch-b", elapsed=1_050, action_slot=30, row_slot=31),
        _row(launch_id="launch-c", elapsed=1_100, action_slot=40, row_slot=41),
        _row(launch_id="launch-d", elapsed=1_150, action_slot=50, row_slot=51),
        _row(
            launch_id="hold-a",
            action=OperatorAction.HOLD,
            elapsed=400,
            quote_reserve=400_000,
            curve_progress=200_000,
            operator_pnl=40_000,
            buyer_count=3,
            idle_ms=400,
            action_slot=60,
            row_slot=61,
        ),
        _row(
            launch_id="hold-b",
            action=OperatorAction.HOLD,
            elapsed=600,
            quote_reserve=600_000,
            curve_progress=250_000,
            operator_pnl=60_000,
            buyer_count=4,
            idle_ms=600,
            action_slot=70,
            row_slot=71,
        ),
    )


def _row(**overrides: object) -> StateActionTrainingRow:
    launch_id = _override_str(overrides, "launch_id", "launch-a")
    action_slot = _override_int(overrides, "action_slot", 20)
    row_slot = _override_int(overrides, "row_slot", action_slot + 1)
    return StateActionTrainingRow(
        as_of_slot=Slot(row_slot),
        feature=_feature(
            as_of_slot=_override_int(overrides, "feature_slot", action_slot - 1),
            entity_id=_override_str(overrides, "entity_id", "entity-1"),
            campaign_id=_override_str(overrides, "campaign_id", "campaign-a"),
            regime_id=_override_str(overrides, "regime_id", "regime-a"),
            launch_id=launch_id,
            elapsed=_override_int(overrides, "elapsed", 1_000),
            quote_reserve=_override_int(overrides, "quote_reserve", 1_000_000),
            curve_progress=_override_int(overrides, "curve_progress", 500_000),
            operator_pnl=_override_int(overrides, "operator_pnl", 100_000),
            buyer_count=_override_int(overrides, "buyer_count", 10),
            idle_ms=_override_int(overrides, "idle_ms", 1_000),
        ),
        label=_label(
            as_of_slot=_override_int(overrides, "label_slot", action_slot),
            entity_id=_override_str(overrides, "entity_id", "entity-1"),
            campaign_id=_override_str(overrides, "campaign_id", "campaign-a"),
            regime_id=_override_str(overrides, "regime_id", "regime-a"),
            launch_id=launch_id,
            action=cast(
                "OperatorAction",
                overrides.get("action", OperatorAction.FULL_DUMP),
            ),
            action_slot=action_slot,
        ),
        row_schema_version="rows-v1",
        evidence_ids=("row-evidence",),
    )


def _feature(**overrides: object) -> TriggerFeatureSnapshot:
    return TriggerFeatureSnapshot(
        as_of_slot=Slot(_override_int(overrides, "as_of_slot", 19)),
        entity_id=_override_str(overrides, "entity_id", "entity-1"),
        campaign_id=_override_str(overrides, "campaign_id", "campaign-a"),
        regime_id=_override_str(overrides, "regime_id", "regime-a"),
        launch_id=_override_str(overrides, "launch_id", "launch-a"),
        elapsed_ms=_override_int(overrides, "elapsed", 1_000),
        quote_reserve_base_units=_override_int(overrides, "quote_reserve", 1_000_000),
        curve_progress_ppm=_override_int(overrides, "curve_progress", 500_000),
        operator_pnl_lamports=_override_int(overrides, "operator_pnl", 100_000),
        independent_buyer_count=_override_int(overrides, "buyer_count", 10),
        time_since_last_independent_buy_ms=_override_int(overrides, "idle_ms", 1_000),
        feature_schema_version=_override_str(
            overrides,
            "feature_schema_version",
            "features-v1",
        ),
        market_state_snapshot_version="market-v1",
        operator_profile_version="profile-v1",
        evidence_ids=("feature-evidence",),
    )


def _label(**overrides: object) -> OperatorActionLabel:
    return OperatorActionLabel(
        as_of_slot=Slot(_override_int(overrides, "as_of_slot", 20)),
        entity_id=_override_str(overrides, "entity_id", "entity-1"),
        campaign_id=_override_str(overrides, "campaign_id", "campaign-a"),
        regime_id=_override_str(overrides, "regime_id", "regime-a"),
        launch_id=_override_str(overrides, "launch_id", "launch-a"),
        action=cast(
            "OperatorAction",
            overrides.get("action", OperatorAction.FULL_DUMP),
        ),
        action_slot=Slot(_override_int(overrides, "action_slot", 20)),
        action_index=0,
        labeler_version="labels-v1",
        evidence_ids=("label-evidence",),
    )


def _config() -> RuleHypothesisConfig:
    return RuleHypothesisConfig(
        as_of_slot=Slot(100),
        entity_id="entity-1",
        campaign_id="campaign-a",
        regime_id="regime-a",
        target_action=OperatorAction.FULL_DUMP,
        generator_version="rules-v1",
        feature_schema_version="features-v1",
        labeler_version="labels-v1",
        row_schema_version="rows-v1",
        operator_profile_version="profile-v1",
        regime_model_version="regime-model-v1",
        min_distinct_launch_support=4,
        min_precision_ppm=700_000,
        min_confidence_ppm=500_000,
    )


def _thresholds() -> ObservedTriggerEvaluationThresholds:
    return ObservedTriggerEvaluationThresholds(
        as_of_slot=Slot(100),
        min_confidence_ppm=500_000,
        min_trigger_risk_ppm=500_000,
    )


def _hypothesis(
    artifact: RuleHypothesisArtifact,
    expression_kind: RuleExpressionKind,
) -> RuleHypothesis:
    for hypothesis in artifact.hypotheses:
        if hypothesis.expression_kind is expression_kind:
            return hypothesis
    raise AssertionError


def _match(
    evaluation: ObservedTriggerEvaluation,
    expression_kind: RuleExpressionKind,
) -> RuleHypothesisMatch:
    for match in evaluation.matches:
        if match.expression_kind is expression_kind:
            return match
    raise AssertionError


def _override_int(overrides: dict[str, object], key: str, default: int) -> int:
    return cast("int", overrides.get(key, default))


def _override_str(overrides: dict[str, object], key: str, default: str) -> str:
    return cast("str", overrides.get(key, default))


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
