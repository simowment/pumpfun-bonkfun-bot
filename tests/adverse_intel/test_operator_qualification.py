"""Focused tests for pure point-in-time operator qualification."""

import unittest
from dataclasses import replace
from typing import cast

from rugbot.decision.operator_qualification import (
    CompletedLaunchOutcome,
    OperatorQualification,
    OperatorQualificationConfig,
    QualificationStatus,
    WalletEntityEvidence,
    qualify_operator,
)
from rugbot.domain.amounts import Slot
from rugbot.domain.decisions import AbstainReason


class OperatorQualificationTests(unittest.TestCase):
    """Verify metrics, adverse repetition, and temporal fail-closed behavior."""

    def test_qualifies_with_integer_metrics_and_repeated_adverse_behavior(self) -> None:
        result = qualify_operator(
            outcomes=_outcomes(),
            entity_evidence=_entity_evidence(),
            config=_config(),
        )

        self.assertIsInstance(result, OperatorQualification)
        self.assertIs(result.status, QualificationStatus.QUALIFIED)
        self.assertEqual(result.sample_count, 4)
        self.assertEqual(result.win_count, 3)
        self.assertEqual(result.win_rate_ppm, 750_000)
        self.assertEqual(result.expectancy_quote_base_units, 25)
        self.assertEqual(result.average_peak_pnl_quote_base_units, 100)
        self.assertEqual(result.peak_pnl_quote_base_units, 100)
        self.assertEqual(result.adverse_launch_count, 3)
        self.assertEqual(result.adverse_rate_ppm, 750_000)
        self.assertTrue(result.repeated_adverse_behavior)
        self.assertEqual(result.matched_wallet_count, 2)
        self.assertIn("operator_qualified", result.reason_codes)

    def test_threshold_miss_abstains_but_keeps_computed_metrics(self) -> None:
        result = qualify_operator(
            outcomes=_outcomes(),
            entity_evidence=_entity_evidence(),
            config=replace(_config(), min_expectancy_quote_base_units=30),
        )

        self.assertIs(result.status, QualificationStatus.ABSTAIN)
        self.assertEqual(result.expectancy_quote_base_units, 25)
        self.assertIn("expectancy_below_threshold", result.reason_codes)
        self.assertIn("operator_qualification_abstained", result.reason_codes)

    def test_minimum_sample_is_required(self) -> None:
        result = qualify_operator(
            outcomes=_outcomes()[:2],
            entity_evidence=_entity_evidence()[:2],
            config=_config(),
        )

        self.assertIs(result.status, QualificationStatus.ABSTAIN)
        self.assertEqual(result.sample_count, 2)
        self.assertIn("insufficient_sample", result.reason_codes)

    def test_repeated_adverse_behavior_is_a_separate_gate(self) -> None:
        outcomes = tuple(
            replace(outcome, adverse_event_observed=False) for outcome in _outcomes()
        )
        result = qualify_operator(
            outcomes=outcomes,
            entity_evidence=_entity_evidence(),
            config=_config(),
        )

        self.assertIs(result.status, QualificationStatus.ABSTAIN)
        self.assertEqual(result.adverse_launch_count, 0)
        self.assertFalse(result.repeated_adverse_behavior)
        self.assertIn("repeated_adverse_behavior_not_confirmed", result.reason_codes)

    def test_future_outcome_is_rejected(self) -> None:
        result = qualify_operator(
            outcomes=(replace(_outcomes()[0], completed_slot=101), *_outcomes()[1:]),
            entity_evidence=_entity_evidence(),
            config=_config(),
        )

        self.assert_abstains(
            result, "future_outcome_evidence", AbstainReason.STALE_STATE
        )

    def test_future_entity_evidence_is_rejected(self) -> None:
        result = qualify_operator(
            outcomes=_outcomes(),
            entity_evidence=(
                replace(_entity_evidence()[0], observed_slot=101),
                *_entity_evidence()[1:],
            ),
            config=_config(),
        )

        self.assert_abstains(
            result, "future_entity_evidence", AbstainReason.STALE_STATE
        )

    def test_incomplete_outcome_is_rejected(self) -> None:
        result = qualify_operator(
            outcomes=(replace(_outcomes()[0], completed=False), *_outcomes()[1:]),
            entity_evidence=_entity_evidence(),
            config=_config(),
        )

        self.assert_abstains(
            result, "incomplete_historical_outcome", AbstainReason.MISSING_FEATURE
        )

    def test_missing_entity_evidence_is_rejected(self) -> None:
        result = qualify_operator(
            outcomes=_outcomes(),
            entity_evidence=_entity_evidence()[:-1],
            config=_config(),
        )

        self.assert_abstains(
            result, "missing_entity_evidence", AbstainReason.MISSING_FEATURE
        )

    def test_duplicate_outcome_is_rejected(self) -> None:
        result = qualify_operator(
            outcomes=(_outcomes()[0], _outcomes()[0]),
            entity_evidence=(_entity_evidence()[0],),
            config=_config(),
        )

        self.assert_abstains(
            result,
            "duplicate_historical_outcome",
            AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
        )

    def test_float_amount_is_rejected_without_coercion(self) -> None:
        result = qualify_operator(
            outcomes=(
                replace(
                    _outcomes()[0],
                    realized_net_pnl_quote_base_units=cast("object", 1.5),
                ),
                *_outcomes()[1:],
            ),
            entity_evidence=_entity_evidence(),
            config=_config(),
        )

        self.assert_abstains(
            result, "missing_historical_outcome_identity", AbstainReason.MISSING_FEATURE
        )

    def test_result_and_inputs_are_immutable(self) -> None:
        result = qualify_operator(
            outcomes=_outcomes(),
            entity_evidence=_entity_evidence(),
            config=_config(),
        )
        with self.assertRaises(AttributeError):
            result.status = QualificationStatus.ABSTAIN  # type: ignore[misc]

        self.assertEqual(_outcomes()[0].realized_net_pnl_quote_base_units, 100)

    def assert_abstains(
        self,
        result: OperatorQualification,
        reason_code: str,
        reason: AbstainReason,
    ) -> None:
        self.assertIs(result.status, QualificationStatus.ABSTAIN)
        self.assertIn(reason_code, result.reason_codes)
        self.assertIs(result.abstain_reason, reason)


def _config() -> OperatorQualificationConfig:
    return OperatorQualificationConfig(
        as_of_slot=Slot(100),
        entity_id="operator-a",
        min_sample_count=3,
        min_win_rate_ppm=500_000,
        min_expectancy_quote_base_units=0,
        min_peak_pnl_quote_base_units=50,
        min_adverse_launch_count=2,
        min_adverse_rate_ppm=500_000,
        min_entity_probability_ppm=800_000,
    )


def _outcomes() -> tuple[CompletedLaunchOutcome, ...]:
    return tuple(
        CompletedLaunchOutcome(
            as_of_slot=Slot(100),
            entity_id="operator-a",
            launch_id=f"launch-{index}",
            launch_slot=Slot(10 + index),
            completed_slot=Slot(20 + index),
            completed=True,
            realized_net_pnl_quote_base_units=pnl,
            peak_net_pnl_quote_base_units=peak,
            adverse_event_observed=adverse,
            evidence_ids=(f"outcome:{index}",),
        )
        for index, (pnl, peak, adverse) in enumerate(
            ((100, 200, True), (-50, 50, True), (25, 75, False), (25, 75, True))
        )
    )


def _entity_evidence() -> tuple[WalletEntityEvidence, ...]:
    return tuple(
        WalletEntityEvidence(
            as_of_slot=Slot(100),
            observed_slot=Slot(10 + index),
            entity_id="operator-a",
            launch_id=f"launch-{index}",
            wallet=f"wallet-{index % 2}",
            entity_probability_ppm=900_000,
            evidence_ids=(f"entity:{index}",),
        )
        for index in range(4)
    )


if __name__ == "__main__":
    unittest.main()
