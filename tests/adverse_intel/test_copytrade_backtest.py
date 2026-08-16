"""Focused tests for the video-derived copy-trade backtest policy."""

# Wallet and mint strings are intentionally literal test identifiers.
# ruff: noqa: S106

import unittest
from dataclasses import replace
from typing import cast

from rugbot.backtest.copytrade import (
    CopyTradeConfig,
    CopyTradeHistorySample,
    CopyTradeLaunchCase,
    evaluate_copy_trade_launches,
)
from rugbot.backtest.evaluation import (
    BacktestAction,
    BacktestFillStatus,
    FrozenModelManifest,
)
from rugbot.domain.amounts import QuoteBaseUnits, Slot
from rugbot.domain.decisions import AbstainReason, AbstainResult
from rugbot.models.outcome_labels import (
    HorizonOutcomeLabel,
    LaunchOutcomeLabels,
    OutcomeObservationPoint,
)


class CopyTradeBacktestTests(unittest.TestCase):
    """Verify the reproducible video rules without RPC or signing keys."""

    def test_qualifies_wallet_delays_entry_and_takes_first_executable_tp(self) -> None:
        """A qualified wallet produces the canonical filled backtest result."""

        case = _case()
        strategy = _strategy()
        manifest = _manifest()

        results = evaluate_copy_trade_launches(
            cases=(case,), config=strategy, manifest=manifest
        )

        self.assertIsInstance(results, tuple)
        result = cast("tuple", results)[0]
        self.assertEqual(result.action, BacktestAction.ENTER)
        self.assertEqual(result.fill_status, BacktestFillStatus.FILLED)
        self.assertEqual(result.net_pnl_quote_base_units, 150)
        self.assertIn("copy_trade_entry_delayed", result.reason_codes)
        self.assertIn("copy_trade_tp_1500000_ppm", result.reason_codes)

    def test_entity_history_qualifies_after_wallet_churn(self) -> None:
        """A new entity wallet can use the entity's prior qualified history."""

        history_rows = list(_history(15))
        history_rows[-1] = replace(history_rows[-1], wallet="wallet-b")
        history = tuple(history_rows)
        case = replace(_case(history=history), wallet="wallet-b")

        results = evaluate_copy_trade_launches(
            cases=(case,), config=_strategy(), manifest=_manifest()
        )

        self.assertIsInstance(results, tuple)
        result = cast("tuple", results)[0]
        self.assertEqual(result.action, BacktestAction.ENTER)
        self.assertEqual(result.fill_status, BacktestFillStatus.FILLED)
        self.assertIn("copy_trade_wallet_qualified", result.reason_codes)

    def test_insufficient_history_and_late_block_are_skips(self) -> None:
        """The strategy skips unknown wallets and candidates after block 1."""

        manifest = _manifest()
        insufficient = _case(history=_history(14))
        late = _case(wallet_buy_transaction_index=2)
        late = replace(
            late,
            launch_id="late-launch",
            decision_id="late-decision",
            token_mint="late-mint",
            outcome=replace(
                late.outcome,
                launch_id="late-launch",
                token_mint="late-mint",
            ),
        )
        results = evaluate_copy_trade_launches(
            cases=(insufficient, late), config=_strategy(), manifest=manifest
        )

        self.assertIsInstance(results, tuple)
        evaluated = cast("tuple", results)
        self.assertEqual(evaluated[0].action, BacktestAction.SKIP)
        self.assertIn(
            "copy_trade_insufficient_wallet_history", evaluated[0].reason_codes
        )
        self.assertEqual(evaluated[1].action, BacktestAction.SKIP)
        self.assertIn("copy_trade_missed_block_0_or_1", evaluated[1].reason_codes)

    def test_exit_threshold_maximizes_pnl_before_winrate(self) -> None:
        """A lower win-rate threshold wins when its historical PnL is higher."""

        history = list(_history(15))
        for index in range(0, 10):
            history[index] = replace(
                history[index],
                trajectory=(
                    _point(elapsed_ms=150, output=180),
                    _point(elapsed_ms=300, output=300),
                ),
            )
        for index in range(10, 15):
            history[index] = replace(
                history[index],
                trajectory=(
                    _point(elapsed_ms=150, output=180),
                    _point(elapsed_ms=300, output=0),
                ),
            )
        strategy = replace(
            _strategy(),
            min_exit_win_rate_ppm=0,
            exit_peak_descent_step_ppm=500_000,
        )
        result = cast(
            "tuple",
            evaluate_copy_trade_launches(
                cases=(_case(history=tuple(history)),),
                config=strategy,
                manifest=_manifest(),
            ),
        )[0]

        self.assertIn("copy_trade_tp_1600000_ppm", result.reason_codes)

    def test_wallet_activity_cap_and_entry_consistency_are_enforced(self) -> None:
        """Excess activity and inconsistent historical entry caps reject a wallet."""

        active_history = _history(301)
        active = _case(history=active_history)
        active_result = cast(
            "tuple",
            evaluate_copy_trade_launches(
                cases=(active,),
                config=replace(_strategy(), max_weekly_buy_count=10),
                manifest=_manifest(),
            ),
        )[0]
        self.assertIn("copy_trade_wallet_too_active", active_result.reason_codes)

        inconsistent_history = _history(15)
        inconsistent_history = (
            *inconsistent_history[:-1],
            replace(
                inconsistent_history[-1],
                entry_market_cap_quote_base_units=QuoteBaseUnits(10_000),
            ),
        )
        inconsistent = _case(history=inconsistent_history)
        inconsistent_result = cast(
            "tuple",
            evaluate_copy_trade_launches(
                cases=(inconsistent,), config=_strategy(), manifest=_manifest()
            ),
        )[0]
        self.assertIn(
            "copy_trade_entry_market_cap_is_inconsistent",
            inconsistent_result.reason_codes,
        )

    def test_malformed_trajectory_abstains(self) -> None:
        """Malformed replay data abstains before any decision."""

        malformed = _case(trajectory=(_point(elapsed_ms=200), _point(elapsed_ms=100)))
        result = evaluate_copy_trade_launches(
            cases=(malformed,), config=_strategy(), manifest=_manifest()
        )
        self.assertIsInstance(result, AbstainResult)
        if isinstance(result, AbstainResult):
            self.assertEqual(result.reason, AbstainReason.UNSUPPORTED_PROTOCOL_STATE)


def _strategy() -> CopyTradeConfig:
    return CopyTradeConfig(
        as_of_slot=Slot(60),
        max_entry_market_cap_quote_base_units=QuoteBaseUnits(1_000),
        fixed_entry_quote_base_units=QuoteBaseUnits(100),
        copy_delay_ms=50,
        max_history_entry_deviation_ppm=100_000,
    )


def _case(
    *,
    history: tuple[CopyTradeHistorySample, ...] | None = None,
    trajectory: tuple[OutcomeObservationPoint, ...] | None = None,
    wallet_buy_transaction_index: int = 0,
) -> CopyTradeLaunchCase:
    return CopyTradeLaunchCase(
        as_of_slot=Slot(60),
        launch_id="target-launch",
        decision_id="target-decision",
        token_mint="target-mint",
        entity_id="known-entity",
        regime_id="fake-pump",
        decision_slot=Slot(50),
        decision_index=0,
        wallet="wallet-a",
        launch_time_ms=2_000_000,
        wallet_buy_transaction_index=wallet_buy_transaction_index,
        wallet_buy_elapsed_ms=100,
        entry_market_cap_quote_base_units=QuoteBaseUnits(500),
        history=_history(15) if history is None else history,
        trajectory=(
            (
                _point(elapsed_ms=100, output=100, execution_cost=0),
                _point(elapsed_ms=200, output=260, execution_cost=10),
            )
            if trajectory is None
            else trajectory
        ),
        outcome=_outcome(),
        evidence_ids=("candidate:target-launch",),
    )


def _history(count: int) -> tuple[CopyTradeHistorySample, ...]:
    return tuple(
        CopyTradeHistorySample(
            as_of_slot=Slot(50),
            launch_id=f"history-{index}",
            token_mint=f"history-mint-{index}",
            wallet="wallet-a",
            launch_slot=Slot(10 + index),
            launch_time_ms=1_000_000 + index * 1_000,
            first_buy_transaction_index=0,
            entry_market_cap_quote_base_units=QuoteBaseUnits(500),
            entry_cost_quote_base_units=QuoteBaseUnits(100),
            realized_net_pnl_quote_base_units=10,
            holding_time_ms=1_000,
            wallet_buy_elapsed_ms=100,
            trajectory=(
                _point(elapsed_ms=150, output=180, execution_cost=0),
                _point(elapsed_ms=300, output=260, execution_cost=10),
            ),
            adverse_event_elapsed_ms=None,
            evidence_ids=(f"history:{index}",),
        )
        for index in range(count)
    )


def _point(
    *, elapsed_ms: int, output: int = 100, execution_cost: int = 0
) -> OutcomeObservationPoint:
    return OutcomeObservationPoint(
        as_of_slot=Slot(60),
        slot=Slot(50 + elapsed_ms // 100),
        event_index=0,
        elapsed_ms=elapsed_ms,
        price_quote_base_units_per_token_base_unit_ppm=1,
        full_exit_output_quote_base_units=QuoteBaseUnits(output),
        full_exit_execution_cost_quote_base_units=QuoteBaseUnits(execution_cost),
        curve_progress_ppm=None,
        curve_completed=False,
        migration_observed=False,
        evidence_ids=(f"point:{elapsed_ms}",),
    )


def _outcome() -> LaunchOutcomeLabels:
    return LaunchOutcomeLabels(
        as_of_slot=Slot(60),
        launch_id="target-launch",
        token_mint="target-mint",
        labeler_version="outcome-labels-v1",
        first_material_adverse_event_slot=None,
        first_material_adverse_event_elapsed_ms=None,
        max_executable_full_position_net_profit_before_adverse_event=160,
        horizon_labels=(
            HorizonOutcomeLabel(
                as_of_slot=Slot(60),
                launch_id="target-launch",
                token_mint="target-mint",
                horizon_ms=5_000,
                censored=False,
                last_observed_slot=Slot(60),
                last_observed_elapsed_ms=5_000,
                adverse_event_observed=False,
                curve_completed=False,
                migration_observed=False,
                drawdown_ppm=0,
                recovery_ppm=0,
                full_exit_net_pnl_quote_base_units=160,
                labeler_version="outcome-labels-v1",
                evidence_ids=("outcome:target-launch",),
            ),
        ),
        source_point_count=2,
        evidence_ids=("outcome:target-launch",),
        reason_codes=("labels-built",),
    )


def _manifest() -> FrozenModelManifest:
    return FrozenModelManifest(
        as_of_slot=Slot(60),
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


if __name__ == "__main__":
    unittest.main()
