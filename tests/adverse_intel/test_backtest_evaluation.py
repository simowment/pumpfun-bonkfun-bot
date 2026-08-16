"""Leakage-safe backtest evaluation tests."""

import ast
import unittest
from dataclasses import replace
from pathlib import Path
from typing import Any, cast

from rugbot.backtest.evaluation import (
    BacktestAction,
    BacktestConfig,
    BacktestFillStatus,
    BacktestLaunchResult,
    BacktestReport,
    BacktestSplit,
    BacktestSplitMetrics,
    FrozenModelManifest,
    OrderingScenario,
    build_backtest_report,
)
from rugbot.domain.amounts import Slot
from rugbot.domain.decisions import AbstainReason, AbstainResult
from rugbot.models.outcome_labels import (
    HorizonOutcomeLabel,
    LaunchOutcomeLabels,
)

BACKTEST_MODULE = Path("src/rugbot/backtest/evaluation.py")
TEST_MINT = "mint-1"
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
DEFAULT_NET_PNL = 10
DEFAULT_GROSS_PROFIT = 20
DEFAULT_EXECUTION_COST = 10
DEFAULT_SELECTED_SIZE = 1_000


class BacktestEvaluationTests(unittest.TestCase):
    """Tests for pure leakage-safe backtest evaluation."""

    def test_builds_split_metrics_without_hiding_no_trade_policy(self) -> None:
        """Report exposes coverage, failures, PnL, missed opportunities, and stress."""

        result = build_backtest_report(
            launches=(
                _launch(
                    launch_id="train-abstain",
                    slot=10,
                    action=BacktestAction.ABSTAIN,
                    fill_status=BacktestFillStatus.NOT_ATTEMPTED,
                    opportunity=500,
                ),
                _launch(
                    launch_id="validation-fill",
                    slot=30,
                    net_pnl=100,
                    gross_profit=150,
                    execution_cost=50,
                    opportunity=200,
                ),
                _launch(
                    launch_id="test-failed-adverse",
                    slot=50,
                    fill_status=BacktestFillStatus.FAILED,
                    ordering_scenario=OrderingScenario.ADVERSE_SAME_SLOT,
                    net_pnl=-20,
                    gross_profit=0,
                    execution_cost=20,
                    opportunity=300,
                    adverse=True,
                ),
                _launch(
                    launch_id="stress-fill",
                    slot=55,
                    entity_id="stress-entity",
                    net_pnl=300,
                    gross_profit=350,
                    execution_cost=50,
                    opportunity=400,
                ),
            ),
            config=_config(),
        )

        self.assertIsInstance(result, BacktestReport)
        result = cast("BacktestReport", result)
        self.assertEqual(result.source_launch_count, 4)
        self.assertEqual(result.reason_codes, ("leakage_safe_backtest_report_built",))

        train = _metrics(result, BacktestSplit.TRAIN)
        self.assertEqual(train.observed_launch_count, 1)
        self.assertEqual(train.coverage_ppm, 0)
        self.assertEqual(train.abstained_launch_count, 1)
        self.assertEqual(train.profitable_launches_incorrectly_skipped_count, 1)
        self.assertIsNone(train.fill_failure_ppm)

        validation = _metrics(result, BacktestSplit.VALIDATION)
        self.assertEqual(validation.attempted_trade_count, 1)
        self.assertEqual(validation.filled_trade_count, 1)
        self.assertEqual(validation.net_pnl_filled_quote_base_units, 100)
        self.assertEqual(validation.coverage_ppm, 1_000_000)
        self.assertEqual(validation.fill_failure_ppm, 0)
        self.assertEqual(validation.cost_to_gross_profit_ppm, 333_333)
        self.assertEqual(validation.expected_shortfall_quote_base_units, 100)
        self.assertEqual(validation.profit_capture_ppm, 500_000)

        test = _metrics(result, BacktestSplit.TEST)
        self.assertEqual(test.failed_trade_count, 1)
        self.assertEqual(test.fill_failure_ppm, 1_000_000)
        self.assertEqual(test.net_pnl_attempted_quote_base_units, -20)
        self.assertEqual(test.maximum_drawdown_quote_base_units, 20)
        self.assertEqual(test.adverse_order_attempt_count, 1)
        self.assertEqual(test.adverse_launches_incorrectly_entered_count, 1)

        stress = _metrics(result, BacktestSplit.STRESS)
        self.assertEqual(stress.observed_launch_count, 1)
        self.assertEqual(stress.net_pnl_filled_quote_base_units, 300)
        self.assertEqual(stress.profit_capture_ppm, 750_000)

    def test_drawdown_uses_chronological_order_with_skips_as_zero(self) -> None:
        """Maximum drawdown is computed over chronological launch PnL."""

        result = build_backtest_report(
            launches=(
                _launch(launch_id="gain", slot=45, net_pnl=100),
                _launch(launch_id="loss", slot=50, net_pnl=-250),
                _launch(
                    launch_id="skip",
                    slot=55,
                    action=BacktestAction.SKIP,
                    fill_status=BacktestFillStatus.NOT_ATTEMPTED,
                    opportunity=0,
                ),
            ),
            config=_config(),
        )

        self.assertIsInstance(result, BacktestReport)
        result = cast("BacktestReport", result)
        test = _metrics(result, BacktestSplit.TEST)
        self.assertEqual(test.maximum_drawdown_quote_base_units, 250)

    def test_censored_outcome_is_counted_not_treated_as_safe(self) -> None:
        """Censored outcomes are explicit in metrics."""

        result = build_backtest_report(
            launches=(
                _launch(
                    launch_id="censored",
                    slot=50,
                    outcome=_outcome(launch_id="censored", censored=True),
                ),
            ),
            config=_config(),
        )

        self.assertIsInstance(result, BacktestReport)
        result = cast("BacktestReport", result)
        test = _metrics(result, BacktestSplit.TEST)
        self.assertEqual(test.censored_outcome_count, 1)

    def test_model_freeze_after_test_start_abstains(self) -> None:
        """Model artifacts must be frozen before the test window starts."""

        manifest = replace(_manifest(), model_freeze_slot=Slot(41))
        result = build_backtest_report(
            launches=(_launch(launch_id="launch-1", slot=50, manifest=manifest),),
            config=_config(manifest=manifest),
        )

        self.assert_abstains(result, AbstainReason.STALE_STATE)

    def test_train_and_stress_entity_overlap_abstains(self) -> None:
        """Cluster-disjoint stress split must prove entity disjointness."""

        result = build_backtest_report(
            launches=(_launch(launch_id="launch-1", slot=50),),
            config=replace(
                _config(),
                train_entity_ids=("entity-1",),
                stress_entity_ids=("entity-1",),
            ),
        )

        self.assert_abstains(result, AbstainReason.UNSUPPORTED_PROTOCOL_STATE)

    def test_train_entity_in_test_window_abstains(self) -> None:
        """Frozen train entities cannot leak into the test window."""

        result = build_backtest_report(
            launches=(_launch(launch_id="leaked-train-entity", slot=50),),
            config=replace(_config(), train_entity_ids=("entity-1",)),
        )

        self.assert_abstains(result, AbstainReason.UNSUPPORTED_PROTOCOL_STATE)

    def test_outcome_slot_mismatch_abstains(self) -> None:
        """Outcome labels must use the report as_of_slot."""

        result = build_backtest_report(
            launches=(
                _launch(
                    launch_id="stale-outcome",
                    slot=30,
                    outcome=replace(
                        _outcome(launch_id="stale-outcome"),
                        as_of_slot=Slot(59),
                    ),
                ),
            ),
            config=_config(),
        )

        self.assert_abstains(result, AbstainReason.STALE_STATE)

    def test_float_outcome_slot_abstains(self) -> None:
        """Outcome as_of_slot must be strict int before equality checks."""

        result = build_backtest_report(
            launches=(
                _launch(
                    launch_id="float-outcome-slot",
                    slot=30,
                    outcome=replace(
                        _outcome(launch_id="float-outcome-slot"),
                        as_of_slot=cast("Any", 60.0),
                    ),
                ),
            ),
            config=_config(),
        )

        self.assert_abstains(result, AbstainReason.UNSUPPORTED_PROTOCOL_STATE)

    def test_malformed_outcome_opportunity_abstains(self) -> None:
        """Outcome opportunity labels must be strict integers when present."""

        result = build_backtest_report(
            launches=(
                _launch(
                    launch_id="bad-opportunity",
                    slot=30,
                    outcome=replace(
                        _outcome(launch_id="bad-opportunity"),
                        max_executable_full_position_net_profit_before_adverse_event=(
                            cast("Any", bool(1))
                        ),
                    ),
                ),
            ),
            config=_config(),
        )

        self.assert_abstains(result, AbstainReason.UNSUPPORTED_PROTOCOL_STATE)

    def test_censored_horizon_with_outcome_values_abstains(self) -> None:
        """Censored labels cannot be counted while carrying realized outcomes."""

        result = build_backtest_report(
            launches=(
                _launch(
                    launch_id="bad-censored",
                    slot=30,
                    outcome=_outcome(
                        launch_id="bad-censored",
                        censored=True,
                        malformed_censored_values=True,
                    ),
                ),
            ),
            config=_config(),
        )

        self.assert_abstains(result, AbstainReason.UNSUPPORTED_PROTOCOL_STATE)

    def test_malformed_horizon_observation_abstains(self) -> None:
        """Horizon observation slots and flags must be strictly typed."""

        malformed_labels = (
            replace(_horizon("bad-horizon"), as_of_slot=cast("Any", 60.0)),
            replace(
                _horizon("bad-horizon"),
                last_observed_slot=None,
                last_observed_elapsed_ms=None,
            ),
            replace(_horizon("bad-horizon"), last_observed_elapsed_ms=None),
            replace(_horizon("bad-horizon"), last_observed_slot=cast("Any", 60.0)),
            replace(
                _horizon("bad-horizon"),
                last_observed_elapsed_ms=cast("Any", 5_000.0),
            ),
            replace(
                _horizon("bad-horizon"),
                adverse_event_observed=cast("Any", 1),
            ),
            replace(_horizon("bad-horizon"), curve_completed=cast("Any", 1)),
            replace(_horizon("bad-horizon"), migration_observed=cast("Any", 1)),
        )
        for label in malformed_labels:
            with self.subTest(label=label):
                result = build_backtest_report(
                    launches=(
                        _launch(
                            launch_id="bad-horizon",
                            slot=30,
                            outcome=replace(
                                _outcome(launch_id="bad-horizon"),
                                horizon_labels=(label,),
                            ),
                        ),
                    ),
                    config=_config(),
                )

                self.assert_abstains(
                    result,
                    AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
                )

    def test_launch_outside_test_window_abstains(self) -> None:
        """Backtests must not silently include launches outside split windows."""

        result = build_backtest_report(
            launches=(_launch(launch_id="future-launch", slot=61),),
            config=_config(),
        )

        self.assert_abstains(result, AbstainReason.STALE_STATE)

    def test_duplicate_ids_abstain(self) -> None:
        """Repeated launches or decisions would double-count outcomes."""

        result = build_backtest_report(
            launches=(
                _launch(launch_id="same", decision_id="same-decision", slot=10),
                _launch(launch_id="same", decision_id="same-decision-2", slot=20),
            ),
            config=_config(),
        )

        self.assert_abstains(result, AbstainReason.UNSUPPORTED_PROTOCOL_STATE)

    def test_manifest_mismatch_abstains(self) -> None:
        """Backtest reports require identical frozen manifests."""

        result = build_backtest_report(
            launches=(
                _launch(
                    launch_id="manifest-mismatch",
                    slot=10,
                    manifest=replace(_manifest(), model_version="other"),
                ),
            ),
            config=_config(),
        )

        self.assert_abstains(result, AbstainReason.DECODER_MISMATCH)

    def test_skipped_launch_with_trade_fields_abstains(self) -> None:
        """Skipped launches cannot carry trade fields."""

        result = build_backtest_report(
            launches=(
                _launch(
                    launch_id="bad-skip",
                    slot=10,
                    action=BacktestAction.SKIP,
                    fill_status=BacktestFillStatus.NOT_ATTEMPTED,
                    net_pnl=1,
                ),
            ),
            config=_config(),
        )

        self.assert_abstains(result, AbstainReason.UNSUPPORTED_PROTOCOL_STATE)

    def test_entered_launch_without_attempt_abstains(self) -> None:
        """Entered launches must have attempted fill status."""

        result = build_backtest_report(
            launches=(
                _launch(
                    launch_id="bad-enter",
                    slot=10,
                    fill_status=BacktestFillStatus.NOT_ATTEMPTED,
                ),
            ),
            config=_config(),
        )

        self.assert_abstains(result, AbstainReason.UNSUPPORTED_PROTOCOL_STATE)

    def test_float_net_pnl_abstains(self) -> None:
        """Backtest PnL fields must be strict integers."""

        result = build_backtest_report(
            launches=(
                _launch(
                    launch_id="float-pnl",
                    slot=10,
                    net_pnl=cast("Any", 1.5),
                ),
            ),
            config=_config(),
        )

        self.assert_abstains(result, AbstainReason.UNSUPPORTED_PROTOCOL_STATE)

    def test_bool_selected_size_abstains(self) -> None:
        """Bool-backed integer trade fields fail closed."""

        result = build_backtest_report(
            launches=(
                _launch(
                    launch_id="bool-size",
                    slot=10,
                    selected_size=cast("Any", bool(1)),
                ),
            ),
            config=_config(),
        )

        self.assert_abstains(result, AbstainReason.UNSUPPORTED_PROTOCOL_STATE)

    def test_malformed_config_abstains(self) -> None:
        """Malformed config artifacts fail closed."""

        result = build_backtest_report(
            launches=(_launch(launch_id="valid", slot=10),),
            config=cast("Any", object()),
        )

        self.assert_abstains(
            result,
            AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
            as_of_slot=-1,
        )

    def test_malformed_split_slot_abstains_without_manifest_comparison(self) -> None:
        """Split slots are validated before model-freeze comparisons."""

        result = build_backtest_report(
            launches=(_launch(launch_id="valid", slot=10),),
            config=replace(_config(), test_start_slot=cast("Any", object())),
        )

        self.assert_abstains(
            result,
            AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
            as_of_slot=-1,
        )

    def test_launches_must_be_tuple(self) -> None:
        """Launch result containers must be immutable tuple artifacts."""

        result = build_backtest_report(
            launches=cast("Any", [_launch(launch_id="valid", slot=10)]),
            config=_config(),
        )

        self.assert_abstains(result, AbstainReason.UNSUPPORTED_PROTOCOL_STATE)

    def test_source_stays_pure_and_integer_only(self) -> None:
        """Backtest evaluation must not grow adapters, signers, or floats."""

        source = BACKTEST_MODULE.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(BACKTEST_MODULE))
        violations = [
            imported_name
            for imported_name in _imported_module_names(tree)
            if imported_name.startswith(FORBIDDEN_IMPORT_PREFIXES)
        ]

        self.assertEqual(violations, [])
        for token in _forbidden_source_tokens():
            with self.subTest(token=token):
                self.assertNotIn(token, source)

    def assert_abstains(
        self,
        result: object,
        reason: AbstainReason,
        *,
        as_of_slot: int = 60,
    ) -> None:
        self.assertIsInstance(result, AbstainResult)
        result = cast("AbstainResult", result)
        self.assertEqual(result.reason, reason)
        self.assertEqual(result.as_of_slot, as_of_slot)


def _config(
    *,
    manifest: FrozenModelManifest | None = None,
) -> BacktestConfig:
    return BacktestConfig(
        as_of_slot=Slot(60),
        evaluation_version="backtest-v1",
        manifest=manifest if manifest is not None else _manifest(),
        train_end_slot=Slot(20),
        test_start_slot=Slot(40),
        test_end_slot=Slot(60),
        train_entity_ids=(),
        stress_entity_ids=("stress-entity",),
        expected_shortfall_tail_ppm=500_000,
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


def _launch(  # noqa: PLR0913
    *,
    launch_id: str,
    slot: int,
    decision_id: str | None = None,
    entity_id: str = "entity-1",
    action: BacktestAction = BacktestAction.ENTER,
    fill_status: BacktestFillStatus = BacktestFillStatus.FILLED,
    ordering_scenario: OrderingScenario | None = OrderingScenario.OBSERVED_ORDER,
    net_pnl: int | None = DEFAULT_NET_PNL,
    gross_profit: int | None = DEFAULT_GROSS_PROFIT,
    execution_cost: int | None = DEFAULT_EXECUTION_COST,
    selected_size: int | None = DEFAULT_SELECTED_SIZE,
    opportunity: int | None = 100,
    adverse: bool = False,
    outcome: LaunchOutcomeLabels | None = None,
    manifest: FrozenModelManifest | None = None,
) -> BacktestLaunchResult:
    if action is not BacktestAction.ENTER:
        net_pnl = None if net_pnl == DEFAULT_NET_PNL else net_pnl
        gross_profit = None if gross_profit == DEFAULT_GROSS_PROFIT else gross_profit
        execution_cost = (
            None if execution_cost == DEFAULT_EXECUTION_COST else execution_cost
        )
        selected_size = (
            None if selected_size == DEFAULT_SELECTED_SIZE else selected_size
        )
        ordering_scenario = None
    return BacktestLaunchResult(
        as_of_slot=Slot(60),
        launch_id=launch_id,
        decision_id=decision_id if decision_id is not None else f"decision:{launch_id}",
        token_mint=TEST_MINT,
        entity_id=entity_id,
        regime_id="regime-1",
        decision_slot=Slot(slot),
        decision_index=0,
        action=action,
        fill_status=fill_status,
        ordering_scenario=ordering_scenario,
        net_pnl_quote_base_units=net_pnl,
        gross_profit_quote_base_units=gross_profit,
        execution_cost_quote_base_units=execution_cost,
        selected_size_quote_base_units=selected_size,
        outcome=outcome
        if outcome is not None
        else _outcome(launch_id=launch_id, opportunity=opportunity, adverse=adverse),
        manifest=manifest if manifest is not None else _manifest(),
        reason_codes=("evaluated",),
        evidence_ids=(f"decision:{launch_id}",),
    )


def _outcome(
    *,
    launch_id: str,
    opportunity: int | None = 100,
    adverse: bool = False,
    censored: bool = False,
    malformed_censored_values: bool = False,
) -> LaunchOutcomeLabels:
    return LaunchOutcomeLabels(
        as_of_slot=Slot(60),
        launch_id=launch_id,
        token_mint=TEST_MINT,
        labeler_version="outcome-labels-v1",
        first_material_adverse_event_slot=Slot(50) if adverse else None,
        first_material_adverse_event_elapsed_ms=5_000 if adverse else None,
        max_executable_full_position_net_profit_before_adverse_event=opportunity,
        horizon_labels=(
            _horizon(
                launch_id,
                opportunity=opportunity,
                adverse=adverse,
                censored=censored,
                malformed_censored_values=malformed_censored_values,
            ),
        ),
        source_point_count=1,
        evidence_ids=(f"label:{launch_id}",),
        reason_codes=("multi_horizon_outcome_labels_built",),
    )


def _horizon(
    launch_id: str,
    *,
    opportunity: int | None = 100,
    adverse: bool = False,
    censored: bool = False,
    malformed_censored_values: bool = False,
) -> HorizonOutcomeLabel:
    return HorizonOutcomeLabel(
        as_of_slot=Slot(60),
        launch_id=launch_id,
        token_mint=TEST_MINT,
        horizon_ms=5_000,
        censored=censored,
        last_observed_slot=Slot(60),
        last_observed_elapsed_ms=5_000,
        adverse_event_observed=adverse,
        curve_completed=False,
        migration_observed=False,
        drawdown_ppm=0 if malformed_censored_values else None if censored else 0,
        recovery_ppm=0 if malformed_censored_values else None if censored else 0,
        full_exit_net_pnl_quote_base_units=(
            1 if malformed_censored_values else None if censored else opportunity
        ),
        labeler_version="outcome-labels-v1",
        evidence_ids=(f"label:{launch_id}",),
    )


def _metrics(report: BacktestReport, split: BacktestSplit) -> BacktestSplitMetrics:
    for metrics in report.split_metrics:
        if metrics.split is split:
            return metrics
    raise AssertionError


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
