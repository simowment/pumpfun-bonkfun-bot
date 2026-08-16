"""Multi-horizon outcome label tests."""

import ast
import unittest
from dataclasses import replace
from pathlib import Path
from typing import Any, cast

from rugbot.domain.amounts import QuoteBaseUnits, Slot
from rugbot.domain.decisions import AbstainReason, AbstainResult
from rugbot.models.adverse_event import AdverseEvent
from rugbot.models.outcome_labels import (
    LaunchOutcomeLabels,
    OutcomeLabelConfig,
    OutcomeObservationPoint,
    build_launch_outcome_labels,
)

LABEL_MODULE = Path("src/rugbot/models/outcome_labels.py")
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


class OutcomeLabelTests(unittest.TestCase):
    """Tests for pure leakage-safe outcome labels."""

    def test_builds_multi_horizon_labels_from_finalized_points(self) -> None:
        """Labels preserve adverse timing, drawdown, recovery, PnL, and censoring."""

        result = build_launch_outcome_labels(
            points=(
                _point(slot=10, event_index=0, elapsed_ms=0, price=100_000),
                _point(
                    slot=11,
                    event_index=0,
                    elapsed_ms=1_000,
                    price=150_000,
                    exit_output=1_300,
                    exit_cost=10,
                ),
                _point(
                    slot=12,
                    event_index=0,
                    elapsed_ms=2_000,
                    price=90_000,
                    exit_output=800,
                    exit_cost=10,
                ),
                _point(
                    slot=13,
                    event_index=0,
                    elapsed_ms=3_000,
                    price=120_000,
                    exit_output=1_100,
                    exit_cost=10,
                ),
                _point(
                    slot=14,
                    event_index=0,
                    elapsed_ms=6_000,
                    price=180_000,
                    exit_output=1_600,
                    exit_cost=20,
                    curve_completed=True,
                    migration_observed=True,
                ),
            ),
            config=_config(horizons=(1_000, 3_000, 5_000, 7_000)),
            adverse_event=_event(collapse_elapsed_ms=2_500),
        )

        self.assertIsInstance(result, LaunchOutcomeLabels)
        result = cast("LaunchOutcomeLabels", result)
        self.assertEqual(result.as_of_slot, Slot(20))
        self.assertEqual(result.first_material_adverse_event_slot, Slot(13))
        self.assertEqual(result.first_material_adverse_event_elapsed_ms, 2_500)
        self.assertEqual(
            result.max_executable_full_position_net_profit_before_adverse_event,
            290,
        )
        self.assertEqual(result.source_point_count, 5)
        self.assertEqual(result.reason_codes, ("multi_horizon_outcome_labels_built",))

        first, third, fifth, seventh = result.horizon_labels
        self.assertFalse(first.censored)
        self.assertFalse(first.adverse_event_observed)
        self.assertEqual(first.full_exit_net_pnl_quote_base_units, 290)
        self.assertEqual(first.drawdown_ppm, 0)

        self.assertFalse(third.censored)
        self.assertTrue(third.adverse_event_observed)
        self.assertEqual(third.drawdown_ppm, 400_000)
        self.assertEqual(third.recovery_ppm, 333_333)
        self.assertEqual(third.full_exit_net_pnl_quote_base_units, 90)

        self.assertFalse(fifth.censored)
        self.assertEqual(fifth.last_observed_elapsed_ms, 3_000)
        self.assertEqual(fifth.full_exit_net_pnl_quote_base_units, 90)

        self.assertTrue(seventh.censored)
        self.assertEqual(seventh.last_observed_elapsed_ms, 6_000)
        self.assertIsNone(seventh.full_exit_net_pnl_quote_base_units)
        self.assertIsNone(seventh.drawdown_ppm)
        self.assertTrue(seventh.evidence_ids)

    def test_exact_observation_boundary_is_not_censored(self) -> None:
        """A horizon at the last observed point is labeled, not censored."""

        result = build_launch_outcome_labels(
            points=(
                _point(slot=10, event_index=0, elapsed_ms=0, price=100_000),
                _point(slot=11, event_index=0, elapsed_ms=5_000, price=120_000),
            ),
            config=_config(horizons=(5_000, 5_001)),
            adverse_event=None,
        )

        self.assertIsInstance(result, LaunchOutcomeLabels)
        result = cast("LaunchOutcomeLabels", result)
        self.assertFalse(result.horizon_labels[0].censored)
        self.assertTrue(result.horizon_labels[1].censored)

    def test_censored_horizon_before_first_point_preserves_evidence(self) -> None:
        """Censored labels keep provenance even when no point falls in horizon."""

        result = build_launch_outcome_labels(
            points=(
                _point(
                    slot=10,
                    event_index=0,
                    elapsed_ms=2_000,
                    price=100_000,
                    evidence_ids=("first-point",),
                ),
            ),
            config=_config(horizons=(1_000,)),
            adverse_event=None,
        )

        self.assertIsInstance(result, LaunchOutcomeLabels)
        result = cast("LaunchOutcomeLabels", result)
        label = result.horizon_labels[0]
        self.assertTrue(label.censored)
        self.assertEqual(label.evidence_ids, ("first-point",))

    def test_no_adverse_event_preserves_censored_negative_label(self) -> None:
        """No adverse event is an explicit label, not an abstention."""

        result = build_launch_outcome_labels(
            points=(
                _point(slot=10, event_index=0, elapsed_ms=0, price=100_000),
                _point(
                    slot=11,
                    event_index=0,
                    elapsed_ms=2_000,
                    price=130_000,
                    exit_output=1_400,
                    exit_cost=10,
                ),
            ),
            config=_config(horizons=(1_000, 2_000)),
            adverse_event=None,
        )

        self.assertIsInstance(result, LaunchOutcomeLabels)
        result = cast("LaunchOutcomeLabels", result)
        self.assertIsNone(result.first_material_adverse_event_slot)
        self.assertIsNone(result.first_material_adverse_event_elapsed_ms)
        self.assertFalse(
            any(label.adverse_event_observed for label in result.horizon_labels)
        )
        self.assertEqual(
            result.max_executable_full_position_net_profit_before_adverse_event,
            390,
        )

    def test_future_point_abstains(self) -> None:
        """Outcome labels cannot use evidence newer than as_of_slot."""

        result = build_launch_outcome_labels(
            points=(_point(slot=21, event_index=0, elapsed_ms=0, price=100_000),),
            config=_config(),
            adverse_event=None,
        )

        self.assert_abstains(result, AbstainReason.STALE_STATE)

    def test_malformed_config_abstains(self) -> None:
        """Malformed config artifacts fail closed."""

        result = build_launch_outcome_labels(
            points=(_point(slot=10, event_index=0, elapsed_ms=0, price=100_000),),
            config=cast("Any", object()),
            adverse_event=None,
        )

        self.assert_abstains(
            result,
            AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
            as_of_slot=-1,
        )

    def test_points_must_be_tuple(self) -> None:
        """Outcome point containers must be immutable tuple artifacts."""

        result = build_launch_outcome_labels(
            points=cast(
                "Any",
                [_point(slot=10, event_index=0, elapsed_ms=0, price=100_000)],
            ),
            config=_config(),
            adverse_event=None,
        )

        self.assert_abstains(result, AbstainReason.UNSUPPORTED_PROTOCOL_STATE)

    def test_float_point_price_abstains(self) -> None:
        """Financial and price-like fields must be strict integers."""

        result = build_launch_outcome_labels(
            points=(
                _point(
                    slot=10,
                    event_index=0,
                    elapsed_ms=0,
                    price=cast("Any", 100_000.5),
                ),
            ),
            config=_config(),
            adverse_event=None,
        )

        self.assert_abstains(result, AbstainReason.UNSUPPORTED_PROTOCOL_STATE)

    def test_bool_horizon_abstains(self) -> None:
        """Bool-backed integers do not pass horizon validation."""

        result = build_launch_outcome_labels(
            points=(_point(slot=10, event_index=0, elapsed_ms=0, price=100_000),),
            config=_config(horizons=(cast("Any", bool(1)),)),
            adverse_event=None,
        )

        self.assert_abstains(result, AbstainReason.UNSUPPORTED_PROTOCOL_STATE)

    def test_unordered_horizons_abstain(self) -> None:
        """Horizon labels must be strictly increasing."""

        result = build_launch_outcome_labels(
            points=(_point(slot=10, event_index=0, elapsed_ms=0, price=100_000),),
            config=_config(horizons=(5_000, 1_000)),
            adverse_event=None,
        )

        self.assert_abstains(result, AbstainReason.UNSUPPORTED_PROTOCOL_STATE)

    def test_duplicate_point_position_abstains(self) -> None:
        """Canonical point identity must be unique before labeling."""

        result = build_launch_outcome_labels(
            points=(
                _point(slot=10, event_index=0, elapsed_ms=0, price=100_000),
                _point(slot=10, event_index=0, elapsed_ms=100, price=101_000),
            ),
            config=_config(),
            adverse_event=None,
        )

        self.assert_abstains(result, AbstainReason.UNSUPPORTED_PROTOCOL_STATE)

    def test_missing_point_evidence_abstains(self) -> None:
        """Outcome labels must preserve source evidence IDs."""

        result = build_launch_outcome_labels(
            points=(
                _point(
                    slot=10,
                    event_index=0,
                    elapsed_ms=0,
                    price=100_000,
                    evidence_ids=(),
                ),
            ),
            config=_config(),
            adverse_event=None,
        )

        self.assert_abstains(result, AbstainReason.MISSING_FEATURE)

    def test_adverse_event_token_mismatch_abstains(self) -> None:
        """Adverse event labels must match the launch token."""

        result = build_launch_outcome_labels(
            points=(_point(slot=10, event_index=0, elapsed_ms=0, price=100_000),),
            config=_config(),
            adverse_event=_event(collapse_elapsed_ms=500, mint=_other_mint()),
        )

        self.assert_abstains(result, AbstainReason.UNSUPPORTED_PROTOCOL_STATE)

    def test_malformed_adverse_event_abstains(self) -> None:
        """Malformed adverse-event artifacts fail closed."""

        result = build_launch_outcome_labels(
            points=(_point(slot=10, event_index=0, elapsed_ms=0, price=100_000),),
            config=_config(),
            adverse_event=cast("Any", object()),
        )

        self.assert_abstains(result, AbstainReason.UNSUPPORTED_PROTOCOL_STATE)

    def test_float_adverse_event_slot_abstains(self) -> None:
        """Adverse event slots must be strict integers before equality checks."""

        result = build_launch_outcome_labels(
            points=(_point(slot=10, event_index=0, elapsed_ms=0, price=100_000),),
            config=_config(),
            adverse_event=replace(
                _event(collapse_elapsed_ms=500),
                as_of_slot=cast("Any", 20.0),
            ),
        )

        self.assert_abstains(result, AbstainReason.UNSUPPORTED_PROTOCOL_STATE)

    def test_malformed_adverse_event_numeric_fields_abstain(self) -> None:
        """Adverse-event structural fields must be well-formed."""

        malformed_events = (
            replace(_event(collapse_elapsed_ms=500), peak_slot=cast("Any", 20.0)),
            replace(_event(collapse_elapsed_ms=500), trough_elapsed_ms=-1),
            replace(_event(collapse_elapsed_ms=500), peak_price_ppm=0),
            replace(
                _event(collapse_elapsed_ms=500),
                source_point_count=cast("Any", bool(1)),
            ),
            replace(
                _event(collapse_elapsed_ms=500),
                collapse_start_elapsed_ms=499,
            ),
        )
        for adverse_event in malformed_events:
            with self.subTest(adverse_event=adverse_event):
                result = build_launch_outcome_labels(
                    points=(
                        _point(
                            slot=10,
                            event_index=0,
                            elapsed_ms=0,
                            price=100_000,
                        ),
                    ),
                    config=_config(),
                    adverse_event=adverse_event,
                )

                self.assert_abstains(
                    result,
                    AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
                )

    def test_source_stays_pure_and_integer_only(self) -> None:
        """Outcome labels must not grow adapters, signers, or floats."""

        source = LABEL_MODULE.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(LABEL_MODULE))
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
        as_of_slot: int = 20,
    ) -> None:
        self.assertIsInstance(result, AbstainResult)
        result = cast("AbstainResult", result)
        self.assertEqual(result.reason, reason)
        self.assertEqual(result.as_of_slot, as_of_slot)


def _config(
    *,
    horizons: tuple[int, ...] = (1_000,),
) -> OutcomeLabelConfig:
    return OutcomeLabelConfig(
        as_of_slot=Slot(20),
        launch_id="launch-1",
        token_mint=TEST_MINT,
        labeler_version="outcome-labels-v1",
        horizon_ms=horizons,
        entry_total_cost_quote_base_units=QuoteBaseUnits(1_000),
    )


def _point(  # noqa: PLR0913
    *,
    slot: int,
    event_index: int,
    elapsed_ms: int,
    price: int,
    exit_output: int = 900,
    exit_cost: int = 10,
    curve_progress: int | None = 100_000,
    curve_completed: bool = False,
    migration_observed: bool = False,
    evidence_ids: tuple[str, ...] = ("point-evidence",),
) -> OutcomeObservationPoint:
    return OutcomeObservationPoint(
        as_of_slot=Slot(20),
        slot=Slot(slot),
        event_index=event_index,
        elapsed_ms=elapsed_ms,
        price_quote_base_units_per_token_base_unit_ppm=price,
        full_exit_output_quote_base_units=QuoteBaseUnits(exit_output),
        full_exit_execution_cost_quote_base_units=QuoteBaseUnits(exit_cost),
        curve_progress_ppm=curve_progress,
        curve_completed=curve_completed,
        migration_observed=migration_observed,
        evidence_ids=evidence_ids,
    )


def _event(*, collapse_elapsed_ms: int, mint: str = TEST_MINT) -> AdverseEvent:
    return AdverseEvent(
        as_of_slot=Slot(20),
        token_mint=mint,
        collapse_start_slot=Slot(13),
        collapse_start_elapsed_ms=collapse_elapsed_ms,
        peak_slot=Slot(12),
        peak_elapsed_ms=2_000,
        peak_price_ppm=150_000,
        trough_slot=Slot(13),
        trough_elapsed_ms=collapse_elapsed_ms,
        trough_price_ppm=90_000,
        drawdown_ppm=400_000,
        recovery_ppm=0,
        detector_version="detector-v1",
        source_point_count=4,
    )


def _other_mint() -> str:
    return "other-mint"


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
