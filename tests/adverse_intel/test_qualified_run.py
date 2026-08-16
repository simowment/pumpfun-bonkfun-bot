"""Focused tests for the qualification/backtest composition boundary."""

import unittest
from unittest.mock import patch

from rugbot.backtest.copytrade import CopyTradeConfig
from rugbot.backtest.dataset import FinalizedTrade, FullExitStressConfig
from rugbot.backtest.evaluation import BacktestConfig, FrozenModelManifest
from rugbot.backtest.qualified_run import (
    QualifiedRunResult,
    run_qualified_finalized_backtest,
)
from rugbot.decision.operator_qualification import (
    CompletedLaunchOutcome,
    OperatorQualificationConfig,
    WalletEntityEvidence,
)
from rugbot.domain.amounts import QuoteBaseUnits, Slot, TokenBaseUnits
from rugbot.domain.decisions import AbstainReason, AbstainResult
from rugbot.domain.trades import TradeSide

TEST_MINT = "mint"


class QualifiedRunTests(unittest.TestCase):
    """Verify ordering, explicit metrics, and typed evidence gates."""

    def test_unqualified_returns_metrics_without_building_or_running_oos(self) -> None:
        with patch("rugbot.backtest.qualified_run.build_finalized_dataset") as build:
            with patch("rugbot.backtest.qualified_run.run_finalized_backtest") as run:
                result = run_qualified_finalized_backtest(
                    observations=(),
                    cases=(),
                    trades=(_trade(),),
                    outcomes=_outcomes(),
                    entity_evidence=_entity_evidence(),
                    qualification_config=_qualification_config(
                        min_expectancy_quote_base_units=10
                    ),
                    strategy=_strategy(),
                    manifest=_manifest(),
                    backtest_config=_backtest_config(),
                    stress=_stress(),
                )

        self.assertIsInstance(result, QualifiedRunResult)
        if isinstance(result, QualifiedRunResult):
            self.assertIsNone(result.backtest)
            self.assertEqual(result.qualification.sample_count, 3)
            self.assertEqual(result.qualification.expectancy_quote_base_units, 0)
            self.assertIn(
                "expectancy_below_threshold", result.qualification.reason_codes
            )
        build.assert_not_called()
        run.assert_not_called()

    def test_missing_typed_fills_abstains_before_qualification(self) -> None:
        result = run_qualified_finalized_backtest(
            observations=(),
            cases=(),
            trades=(),
            outcomes=_outcomes(),
            entity_evidence=_entity_evidence(),
            qualification_config=_qualification_config(),
            strategy=_strategy(),
            manifest=_manifest(),
            backtest_config=_backtest_config(),
            stress=_stress(),
        )

        self.assertIsInstance(result, AbstainResult)
        if isinstance(result, AbstainResult):
            self.assertIs(result.reason, AbstainReason.MISSING_FEATURE)
            self.assertIn("finalized fills", result.message)

    def test_missing_typed_outcomes_abstains(self) -> None:
        result = run_qualified_finalized_backtest(
            observations=(),
            cases=(),
            trades=(_trade(),),
            outcomes=(),
            entity_evidence=_entity_evidence(),
            qualification_config=_qualification_config(),
            strategy=_strategy(),
            manifest=_manifest(),
            backtest_config=_backtest_config(),
            stress=_stress(),
        )

        self.assertIsInstance(result, AbstainResult)
        if isinstance(result, AbstainResult):
            self.assertIs(result.reason, AbstainReason.MISSING_FEATURE)
            self.assertIn("outcomes", result.message)

    def test_qualified_calls_dataset_then_oos_backtest(self) -> None:
        dataset = object()
        backtest = object()
        with patch(
            "rugbot.backtest.qualified_run.build_finalized_dataset",
            return_value=dataset,
        ) as build:
            with patch(
                "rugbot.backtest.qualified_run.run_finalized_backtest",
                return_value=backtest,
            ) as run:
                result = run_qualified_finalized_backtest(
                    observations=(),
                    cases=(),
                    trades=(_trade(),),
                    outcomes=_outcomes(positive=True),
                    entity_evidence=_entity_evidence(),
                    qualification_config=_qualification_config(),
                    strategy=_strategy(),
                    manifest=_manifest(),
                    backtest_config=_backtest_config(),
                    stress=_stress(),
                )

        self.assertIsInstance(result, QualifiedRunResult)
        if isinstance(result, QualifiedRunResult):
            self.assertIs(result.backtest, backtest)
        build.assert_called_once()
        run.assert_called_once()
        self.assertEqual(build.call_args.kwargs["as_of_slot"], 100)
        self.assertIs(run.call_args.kwargs["dataset"], dataset)


def _qualification_config(**changes: object) -> OperatorQualificationConfig:
    values: dict[str, object] = {
        "as_of_slot": Slot(100),
        "entity_id": "operator-a",
        "min_sample_count": 3,
        "min_win_rate_ppm": 500_000,
        "min_expectancy_quote_base_units": 0,
        "min_peak_pnl_quote_base_units": 0,
        "min_adverse_launch_count": 2,
        "min_adverse_rate_ppm": 500_000,
        "min_entity_probability_ppm": 500_000,
    }
    values.update(changes)
    return OperatorQualificationConfig(**values)  # type: ignore[arg-type]


def _outcomes(*, positive: bool = False) -> tuple[CompletedLaunchOutcome, ...]:
    pnl = 10 if positive else 0
    return tuple(
        CompletedLaunchOutcome(
            as_of_slot=Slot(100),
            entity_id="operator-a",
            launch_id=f"launch-{index}",
            launch_slot=Slot(10 + index),
            completed_slot=Slot(20 + index),
            completed=True,
            realized_net_pnl_quote_base_units=pnl,
            peak_net_pnl_quote_base_units=10,
            adverse_event_observed=True,
            evidence_ids=(f"outcome:{index}",),
        )
        for index in range(3)
    )


def _entity_evidence() -> tuple[WalletEntityEvidence, ...]:
    return tuple(
        WalletEntityEvidence(
            as_of_slot=Slot(100),
            observed_slot=Slot(10 + index),
            entity_id="operator-a",
            launch_id=f"launch-{index}",
            wallet="wallet-a",
            entity_probability_ppm=900_000,
            evidence_ids=(f"entity:{index}",),
        )
        for index in range(3)
    )


def _trade() -> FinalizedTrade:
    return FinalizedTrade(
        as_of_slot=Slot(100),
        launch_id="launch-0",
        token_mint=TEST_MINT,
        wallet="wallet-a",
        side=TradeSide.BUY,
        slot=Slot(100),
        transaction_index=0,
        signature=b"signature",
        base_amount_base_units=TokenBaseUnits(1),
        quote_amount_base_units=QuoteBaseUnits(1),
        execution_cost_quote_base_units=QuoteBaseUnits(0),
        evidence_ids=("trade:0",),
    )


def _strategy() -> CopyTradeConfig:
    return CopyTradeConfig(
        as_of_slot=Slot(100),
        min_history_launch_count=1,
        max_history_launch_count=3,
        min_win_rate_ppm=0,
    )


def _manifest() -> FrozenModelManifest:
    return FrozenModelManifest(
        as_of_slot=Slot(100),
        model_freeze_slot=Slot(100),
        decision_version="decision",
        model_version="model",
        outcome_labeler_version="labels",
        profile_snapshot_version="profile",
        graph_snapshot_version="graph",
        feature_snapshot_version="features",
        market_snapshot_version="market",
        latency_model_version="latency",
        fee_config_version="fees",
    )


def _backtest_config() -> BacktestConfig:
    manifest = _manifest()
    return BacktestConfig(
        as_of_slot=Slot(100),
        evaluation_version="evaluation",
        manifest=manifest,
        train_end_slot=Slot(30),
        test_start_slot=Slot(40),
        test_end_slot=Slot(90),
        train_entity_ids=(),
        stress_entity_ids=(),
        expected_shortfall_tail_ppm=500_000,
    )


def _stress() -> FullExitStressConfig:
    return FullExitStressConfig(
        as_of_slot=Slot(100),
        output_haircut_ppm=0,
        additional_execution_cost_quote_base_units=QuoteBaseUnits(0),
    )


if __name__ == "__main__":
    unittest.main()
