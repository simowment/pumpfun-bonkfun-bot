"""Focused tests for finalized dataset construction and stressed OOS runs."""

import unittest
from dataclasses import replace
from typing import cast
from uuid import UUID

from rugbot.backtest.copytrade import (
    CopyTradeConfig,
    CopyTradeHistorySample,
    CopyTradeLaunchCase,
)
from rugbot.backtest.dataset import (
    FinalizedBacktestDataset,
    FinalizedBacktestResult,
    FinalizedTrade,
    FullExitStressConfig,
    build_finalized_dataset,
    run_finalized_backtest,
)
from rugbot.backtest.evaluation import (
    BacktestConfig,
    BacktestSplit,
    FrozenModelManifest,
)
from rugbot.domain.amounts import QuoteBaseUnits, Slot, TokenBaseUnits
from rugbot.domain.decisions import AbstainReason, AbstainResult
from rugbot.domain.trades import TradeSide
from rugbot.models.outcome_labels import (
    HorizonOutcomeLabel,
    LaunchOutcomeLabels,
    OutcomeObservationPoint,
)
from tests.adverse_intel.test_pump_create_observation import (
    _artifact,
    _observation,
)


class BacktestDatasetTests(unittest.TestCase):
    """Verify the finalized-to-dataset and stressed-report boundary."""

    def test_finalized_observation_decodes_launch_and_joins_typed_trade(self) -> None:
        artifact = _artifact()
        observation = _observation(artifact)
        as_of_slot = Slot(artifact["as_of_slot"])
        launch_dataset = build_finalized_dataset(
            observations=(observation,),
            cases=(),
            trades=(),
            as_of_slot=as_of_slot,
        )

        self.assertIsInstance(launch_dataset, FinalizedBacktestDataset)
        launch_dataset = cast("FinalizedBacktestDataset", launch_dataset)
        self.assertEqual(len(launch_dataset.launches), 1)
        launch = launch_dataset.launches[0]
        trade = FinalizedTrade(
            as_of_slot=as_of_slot,
            launch_id=launch.launch_id,
            token_mint=launch.mint_pubkey,
            wallet=launch.user_pubkey,
            side=TradeSide.BUY,
            slot=as_of_slot,
            transaction_index=0,
            signature=observation.signature,
            base_amount_base_units=TokenBaseUnits(10),
            quote_amount_base_units=QuoteBaseUnits(100),
            execution_cost_quote_base_units=QuoteBaseUnits(1),
            evidence_ids=("trade:finalized",),
        )
        joined = build_finalized_dataset(
            observations=(observation,),
            cases=(),
            trades=(trade,),
            as_of_slot=as_of_slot,
        )

        self.assertIsInstance(joined, FinalizedBacktestDataset)
        self.assertEqual(cast("FinalizedBacktestDataset", joined).trades, (trade,))

    def test_non_finalized_and_duplicate_canonical_evidence_abstain(self) -> None:
        artifact = _artifact()
        observation = _observation(artifact)
        as_of_slot = Slot(artifact["as_of_slot"])

        non_finalized = build_finalized_dataset(
            observations=(replace(observation, commitment="confirmed"),),
            cases=(),
            trades=(),
            as_of_slot=as_of_slot,
        )
        self.assertIsInstance(non_finalized, AbstainResult)
        if isinstance(non_finalized, AbstainResult):
            self.assertEqual(non_finalized.reason, AbstainReason.STALE_STATE)

        duplicate = build_finalized_dataset(
            observations=(observation, replace(observation, raw_id=UUID(int=9))),
            cases=(),
            trades=(),
            as_of_slot=as_of_slot,
        )
        self.assertIsInstance(duplicate, AbstainResult)
        if isinstance(duplicate, AbstainResult):
            self.assertEqual(
                duplicate.reason,
                AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
            )

    def test_trade_without_matching_finalized_transaction_abstains(self) -> None:
        artifact = _artifact()
        observation = _observation(artifact)
        as_of_slot = Slot(artifact["as_of_slot"])
        decoded = build_finalized_dataset(
            observations=(observation,),
            cases=(),
            trades=(),
            as_of_slot=as_of_slot,
        )
        decoded = cast("FinalizedBacktestDataset", decoded)
        trade = FinalizedTrade(
            as_of_slot=as_of_slot,
            launch_id=decoded.launches[0].launch_id,
            token_mint=decoded.launches[0].mint_pubkey,
            wallet=decoded.launches[0].user_pubkey,
            side=TradeSide.SELL,
            slot=as_of_slot,
            transaction_index=0,
            signature=b"unmatched-signature",
            base_amount_base_units=TokenBaseUnits(10),
            quote_amount_base_units=QuoteBaseUnits(100),
            execution_cost_quote_base_units=QuoteBaseUnits(1),
            evidence_ids=("trade:unmatched",),
        )

        result = build_finalized_dataset(
            observations=(observation,),
            cases=(),
            trades=(trade,),
            as_of_slot=as_of_slot,
        )

        self.assertIsInstance(result, AbstainResult)
        if isinstance(result, AbstainResult):
            self.assertEqual(result.reason, AbstainReason.MISSING_FEATURE)

    def test_run_stresses_full_exit_before_oos_report(self) -> None:
        manifest = _manifest()
        dataset = FinalizedBacktestDataset(
            as_of_slot=Slot(100),
            observations=(),
            launches=(),
            trades=(),
            cases=tuple(
                _case(slot=slot, entity=entity)
                for slot, entity in (
                    (20, "train"),
                    (50, "test"),
                    (70, "stress"),
                )
            ),
            evidence_ids=("dataset:test",),
        )
        strategy = CopyTradeConfig(
            as_of_slot=Slot(100),
            min_history_launch_count=1,
            max_history_launch_count=1,
            min_win_rate_ppm=0,
            max_entry_transaction_index=1,
            max_entry_market_cap_quote_base_units=1_000,
            fixed_entry_quote_base_units=QuoteBaseUnits(100),
            copy_delay_ms=0,
        )
        report_config = BacktestConfig(
            as_of_slot=Slot(100),
            evaluation_version="backtest-v1",
            manifest=manifest,
            train_end_slot=Slot(30),
            test_start_slot=Slot(40),
            test_end_slot=Slot(90),
            train_entity_ids=(),
            stress_entity_ids=("stress",),
            expected_shortfall_tail_ppm=500_000,
        )

        result = run_finalized_backtest(
            dataset=dataset,
            strategy=strategy,
            manifest=manifest,
            backtest_config=report_config,
            stress=FullExitStressConfig(
                as_of_slot=Slot(100),
                output_haircut_ppm=100_000,
                additional_execution_cost_quote_base_units=QuoteBaseUnits(5),
            ),
        )

        self.assertIsInstance(result, FinalizedBacktestResult)
        result = cast("FinalizedBacktestResult", result)
        self.assertTrue(result.full_exit_stress_applied)
        self.assertEqual(result.stressed_point_count, 6)
        test = next(
            metrics
            for metrics in result.report.split_metrics
            if metrics.split is BacktestSplit.TEST
        )
        self.assertEqual(test.observed_launch_count, 1)
        self.assertEqual(result.evaluated_launches[1].net_pnl_quote_base_units, 75)

    def test_history_after_decision_is_rejected_by_dataset_builder(self) -> None:
        case = _case(slot=50, entity="test")
        dataset = FinalizedBacktestDataset(
            as_of_slot=Slot(100),
            observations=(),
            launches=(),
            trades=(),
            cases=(case,),
            evidence_ids=("dataset:history",),
        )
        late_history = replace(
            case,
            history=(replace(case.history[0], as_of_slot=Slot(51)),),
        )
        # The public builder performs the temporal join before runtime eval;
        # use a raw-less dataset here only to keep this test independent of RPC.
        result = run_finalized_backtest(
            dataset=replace(dataset, cases=(late_history,)),
            strategy=_strategy(),
            manifest=_manifest(),
            backtest_config=_report_config(_manifest()),
            stress=_stress(),
        )

        self.assertIsInstance(result, AbstainResult)
        if isinstance(result, AbstainResult):
            self.assertEqual(result.reason, AbstainReason.STALE_STATE)


def _case(*, slot: int, entity: str) -> CopyTradeLaunchCase:
    trajectory = (_point(0, 100), _point(100, 200))
    outcome = LaunchOutcomeLabels(
        as_of_slot=Slot(100),
        launch_id=f"launch-{slot}",
        token_mint=f"mint-{slot}",
        labeler_version="outcome-labels-v1",
        first_material_adverse_event_slot=None,
        first_material_adverse_event_elapsed_ms=None,
        max_executable_full_position_net_profit_before_adverse_event=95,
        horizon_labels=(
            HorizonOutcomeLabel(
                as_of_slot=Slot(100),
                launch_id=f"launch-{slot}",
                token_mint=f"mint-{slot}",
                horizon_ms=500,
                censored=False,
                last_observed_slot=Slot(slot + 1),
                last_observed_elapsed_ms=100,
                adverse_event_observed=False,
                curve_completed=False,
                migration_observed=False,
                drawdown_ppm=0,
                recovery_ppm=0,
                full_exit_net_pnl_quote_base_units=95,
                labeler_version="outcome-labels-v1",
                evidence_ids=(f"outcome:{slot}",),
            ),
        ),
        source_point_count=2,
        evidence_ids=(f"outcome:{slot}",),
        reason_codes=("labels-built",),
    )
    history = CopyTradeHistorySample(
        as_of_slot=Slot(10),
        launch_id=f"history-{slot}",
        token_mint=f"history-mint-{slot}",
        wallet="wallet-a",
        launch_slot=Slot(5),
        launch_time_ms=1_000,
        first_buy_transaction_index=0,
        entry_market_cap_quote_base_units=QuoteBaseUnits(500),
        entry_cost_quote_base_units=QuoteBaseUnits(100),
        realized_net_pnl_quote_base_units=95,
        holding_time_ms=100,
        wallet_buy_elapsed_ms=0,
        trajectory=(_point(0, 100, as_of=10), _point(100, 200, as_of=10)),
        adverse_event_elapsed_ms=None,
        evidence_ids=(f"history:{slot}",),
    )
    return CopyTradeLaunchCase(
        as_of_slot=Slot(100),
        launch_id=f"launch-{slot}",
        decision_id=f"decision-{slot}",
        token_mint=f"mint-{slot}",
        entity_id=entity,
        regime_id="regime-1",
        decision_slot=Slot(slot),
        decision_index=0,
        wallet="wallet-a",
        launch_time_ms=2_000,
        wallet_buy_transaction_index=0,
        wallet_buy_elapsed_ms=0,
        entry_market_cap_quote_base_units=QuoteBaseUnits(500),
        history=(history,),
        trajectory=trajectory,
        outcome=outcome,
        evidence_ids=(f"case:{slot}",),
    )


def _point(
    elapsed_ms: int, output: int, *, as_of: int = 100
) -> OutcomeObservationPoint:
    return OutcomeObservationPoint(
        as_of_slot=Slot(as_of),
        slot=Slot(as_of),
        event_index=elapsed_ms // 100,
        elapsed_ms=elapsed_ms,
        price_quote_base_units_per_token_base_unit_ppm=1_000 + output,
        full_exit_output_quote_base_units=QuoteBaseUnits(output),
        full_exit_execution_cost_quote_base_units=QuoteBaseUnits(0),
        curve_progress_ppm=None,
        curve_completed=False,
        migration_observed=False,
        evidence_ids=(f"point:{as_of}:{elapsed_ms}",),
    )


def _manifest() -> FrozenModelManifest:
    return FrozenModelManifest(
        as_of_slot=Slot(100),
        model_freeze_slot=Slot(39),
        decision_version="decision-v1",
        model_version="model-v1",
        outcome_labeler_version="outcome-labels-v1",
        profile_snapshot_version="profile-v1",
        graph_snapshot_version="graph-v1",
        feature_snapshot_version="features-v1",
        market_snapshot_version="market-v1",
        latency_model_version="latency-v1",
        fee_config_version="fees-v1",
    )


def _strategy() -> CopyTradeConfig:
    return CopyTradeConfig(
        as_of_slot=Slot(100),
        min_history_launch_count=1,
        max_history_launch_count=1,
        min_win_rate_ppm=0,
        max_entry_transaction_index=1,
        max_entry_market_cap_quote_base_units=QuoteBaseUnits(1_000),
        fixed_entry_quote_base_units=QuoteBaseUnits(100),
    )


def _report_config(manifest: FrozenModelManifest) -> BacktestConfig:
    return BacktestConfig(
        as_of_slot=Slot(100),
        evaluation_version="backtest-v1",
        manifest=manifest,
        train_end_slot=Slot(30),
        test_start_slot=Slot(40),
        test_end_slot=Slot(90),
        train_entity_ids=(),
        stress_entity_ids=("stress",),
        expected_shortfall_tail_ppm=500_000,
    )


def _stress() -> FullExitStressConfig:
    return FullExitStressConfig(
        as_of_slot=Slot(100),
        output_haircut_ppm=100_000,
        additional_execution_cost_quote_base_units=QuoteBaseUnits(5),
    )


if __name__ == "__main__":
    unittest.main()
