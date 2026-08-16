"""Dynamic exit controller tests."""

import unittest
from dataclasses import replace
from typing import Any, cast

from rugbot.decision.exit_controller import (
    ExitAction,
    ExitModelSnapshot,
    ExitSnapshotPolicy,
    decide_dynamic_exit,
)
from rugbot.domain.amounts import Lamports, Slot
from rugbot.domain.decisions import AbstainReason, AbstainResult
from rugbot.domain.quotes import ExecutableQuote, QuotePath


class DynamicExitControllerTests(unittest.TestCase):
    """Tests for the hold-versus-sell decision rule."""

    def test_holds_when_latency_and_hold_value_are_positive(self) -> None:
        """Hold only when the model edge clears latency and value rules."""

        decision = decide_dynamic_exit(_snapshot(), _policy())

        self.assertEqual(decision.action, ExitAction.HOLD)
        self.assertGreater(int(decision.hold_value_lamports), 0)
        self.assertEqual(decision.full_position_output_base_units, 100_000)

    def test_sells_when_q10_time_is_inside_exit_latency_budget(self) -> None:
        """The conservative latency rule overrides model edge."""

        decision = decide_dynamic_exit(
            _snapshot(
                q10_remaining_ms=1_000,
                p_dump_1s=100_000,
                p_dump_3s=120_000,
                p_dump_5s=140_000,
                p_dump_ppm=120_000,
            ),
            _policy(),
        )

        self.assertEqual(decision.action, ExitAction.SELL)
        self.assertEqual(
            decision.reason_codes,
            ("q10_remaining_time_inside_exit_latency",),
        )

    def test_sells_when_hold_value_is_not_positive(self) -> None:
        """Hold value <= 0 exits immediately."""

        decision = decide_dynamic_exit(
            _snapshot(
                p_dump_ppm=700_000,
                expected_extra_profit=100_000,
                expected_dump_loss=150_000,
            ),
            _policy(),
        )

        self.assertEqual(decision.action, ExitAction.SELL)
        self.assertEqual(decision.reason_codes, ("hold_value_not_positive",))
        self.assertLessEqual(int(decision.hold_value_lamports), 0)

    def test_sells_when_full_position_quote_abstains(self) -> None:
        """Missing executable full-position quote abstains instead of pricing."""

        snapshot = _snapshot(
            quote=AbstainResult(
                reason=AbstainReason.UNKNOWN_PROTOCOL_STATE,
                message="unknown",
                as_of_slot=10,
            )
        )

        result = decide_dynamic_exit(snapshot, _policy())

        self.assertIsInstance(result, AbstainResult)
        result = cast("AbstainResult", result)
        self.assertEqual(result.reason, AbstainReason.UNKNOWN_PROTOCOL_STATE)
        self.assertEqual(result.as_of_slot, 10)

    def test_sells_when_deterministic_safety_reason_present(self) -> None:
        """Deterministic exits sit above model decisions."""

        decision = decide_dynamic_exit(
            _snapshot(deterministic_sell_reasons=("stale_state",)), _policy()
        )

        self.assertEqual(decision.action, ExitAction.SELL)
        self.assertEqual(
            decision.reason_codes,
            ("deterministic_sell_rule", "stale_state"),
        )

    def test_partial_quote_abstains_instead_of_pricing_full_exit(self) -> None:
        """A partial sell quote cannot stand in for the full position."""

        result = decide_dynamic_exit(
            _snapshot(quote=_quote(input_amount=25_000), full_position=50_000),
            _policy(),
        )

        self.assertIsInstance(result, AbstainResult)
        result = cast("AbstainResult", result)
        self.assertEqual(result.reason, AbstainReason.MISSING_FEATURE)

    def test_conservative_loss_rounding_can_flip_marginal_hold_to_sell(self) -> None:
        """Expected dump loss uses ceiling rounding."""

        result = decide_dynamic_exit(
            _snapshot(
                p_dump_ppm=1,
                p_dump_1s=1,
                p_dump_3s=1,
                p_dump_5s=1,
                p_dump_10s=1,
                q10_remaining_ms=11_000,
                expected_extra_profit=2,
                expected_dump_loss=1,
                execution_cost=0,
                uncertainty_penalty=0,
            ),
            _policy(),
        )

        self.assertEqual(result.action, ExitAction.SELL)
        self.assertEqual(result.reason_codes, ("hold_value_not_positive",))
        self.assertEqual(result.hold_value_lamports, Lamports(0))

    def test_negative_execution_cost_forces_sell(self) -> None:
        """Invalid non-quote model fields force sell instead of holding."""

        result = decide_dynamic_exit(_snapshot(execution_cost=-1), _policy())

        self.assertEqual(result.action, ExitAction.SELL)
        self.assertEqual(result.reason_codes, ("invalid_exit_model_snapshot",))

    def test_float_dump_probability_abstains(self) -> None:
        """Exit probabilities must be integer PPM."""

        result = decide_dynamic_exit(_snapshot(p_dump_ppm=cast("Any", 0.5)), _policy())

        self.assertEqual(result.action, ExitAction.SELL)
        self.assertEqual(result.reason_codes, ("invalid_exit_probability",))

    def test_float_full_position_quote_input_abstains(self) -> None:
        """Executable full-position quotes must use integer amounts."""

        result = decide_dynamic_exit(
            _snapshot(
                quote=replace(
                    _quote(),
                    input_amount_base_units=cast("Any", 50_000.0),
                )
            ),
            _policy(),
        )

        self.assertIsInstance(result, AbstainResult)
        result = cast("AbstainResult", result)
        self.assertEqual(result.reason, AbstainReason.UNSUPPORTED_PROTOCOL_STATE)

    def test_malformed_full_position_quote_abstains(self) -> None:
        """Malformed loaded quote artifacts fail closed."""

        result = decide_dynamic_exit(_snapshot(quote=cast("Any", object())), _policy())

        self.assertIsInstance(result, AbstainResult)
        result = cast("AbstainResult", result)
        self.assertEqual(result.reason, AbstainReason.UNSUPPORTED_PROTOCOL_STATE)

    def test_unsupported_quote_decimals_abstain(self) -> None:
        """Executable full-position quote decimals must be validated."""

        bool_decimals = bool(1)
        result = decide_dynamic_exit(
            _snapshot(
                quote=replace(
                    _quote(),
                    base_decimals=cast("Any", bool_decimals),
                    quote_decimals=999,
                )
            ),
            _policy(),
        )

        self.assertIsInstance(result, AbstainResult)
        result = cast("AbstainResult", result)
        self.assertEqual(result.reason, AbstainReason.UNSUPPORTED_PROTOCOL_STATE)

    def test_malformed_deterministic_sell_reasons_abstain(self) -> None:
        """Safety-exit reasons must be immutable string tuples."""

        result = decide_dynamic_exit(
            _snapshot(deterministic_sell_reasons=cast("Any", ("",))), _policy()
        )

        self.assertEqual(result.action, ExitAction.SELL)
        self.assertEqual(result.reason_codes, ("missing_exit_evidence",))

    def test_zero_full_position_abstains(self) -> None:
        """A full-position decision requires a positive position size."""

        result = decide_dynamic_exit(
            _snapshot(full_position=0, quote=_quote(input_amount=0)), _policy()
        )

        self.assertIsInstance(result, AbstainResult)
        result = cast("AbstainResult", result)
        self.assertEqual(result.reason, AbstainReason.UNSUPPORTED_PROTOCOL_STATE)

    def test_malformed_top_level_snapshot_abstains(self) -> None:
        """Malformed top-level snapshots fail closed instead of raising."""

        result = decide_dynamic_exit(cast("Any", object()), _policy())

        self.assertIsInstance(result, AbstainResult)
        result = cast("AbstainResult", result)
        self.assertEqual(result.reason, AbstainReason.UNSUPPORTED_PROTOCOL_STATE)

    def test_malformed_policy_slot_abstains(self) -> None:
        """Policy slots must be strict integer slots, not float-equivalent values."""

        result = decide_dynamic_exit(_snapshot(), _policy(as_of_slot=10.0))

        self.assertIsInstance(result, AbstainResult)
        result = cast("AbstainResult", result)
        self.assertEqual(result.reason, AbstainReason.UNSUPPORTED_PROTOCOL_STATE)

    def test_unknown_quote_version_abstains(self) -> None:
        """Unknown quote decoder evidence cannot produce a sell intent."""

        result = decide_dynamic_exit(
            _snapshot(
                quote=replace(_quote(), decoder_version="unknown-decoder"),
            ),
            _policy(),
        )

        self.assertIsInstance(result, AbstainResult)
        result = cast("AbstainResult", result)
        self.assertEqual(result.reason, AbstainReason.DECODER_MISMATCH)

    def test_unknown_timing_version_forces_sell(self) -> None:
        """Unknown non-quote model evidence cannot support HOLD."""

        result = decide_dynamic_exit(
            _snapshot(timing_model_version="unknown-timing"),
            _policy(),
        )

        self.assertEqual(result.action, ExitAction.SELL)
        self.assertEqual(result.reason_codes, ("unknown_exit_evidence_version",))

    def test_missing_exit_evidence_forces_sell(self) -> None:
        """Missing timing/value/liquidity evidence IDs cannot support HOLD."""

        result = decide_dynamic_exit(_snapshot(evidence_ids=()), _policy())

        self.assertEqual(result.action, ExitAction.SELL)
        self.assertEqual(result.reason_codes, ("missing_exit_evidence",))

    def test_malformed_exit_evidence_forces_sell_without_raising(self) -> None:
        """Malformed non-quote evidence cannot support HOLD or crash the controller."""

        result = decide_dynamic_exit(
            _snapshot(evidence_ids=cast("Any", object())),
            _policy(),
        )

        self.assertEqual(result.action, ExitAction.SELL)
        self.assertEqual(result.reason_codes, ("missing_exit_evidence",))
        self.assertEqual(
            result.evidence_ids,
            (
                "timing-evidence",
                "value-evidence",
                "latency-evidence",
                "liquidity-evidence",
            ),
        )

    def test_stale_liquidity_interval_forces_sell(self) -> None:
        """Stale liquidity evidence exits instead of holding."""

        result = decide_dynamic_exit(
            _snapshot(liquidity_data_end_slot=Slot(9)),
            _policy(),
        )

        self.assertEqual(result.action, ExitAction.SELL)
        self.assertEqual(result.reason_codes, ("stale_exit_evidence",))

    def test_underreported_dump_probability_forces_sell(self) -> None:
        """Dump probability before exit cannot be below the latency horizon."""

        result = decide_dynamic_exit(_snapshot(p_dump_ppm=1), _policy())

        self.assertEqual(result.action, ExitAction.SELL)
        self.assertEqual(
            result.reason_codes,
            ("dump_probability_below_exit_latency_horizon",),
        )

    def test_latency_budget_outside_timing_horizon_forces_sell(self) -> None:
        """Exit budgets beyond the audited horizon cannot borrow the 10s model."""

        result = decide_dynamic_exit(
            _snapshot(p99_exit_latency_ms=9_500, safety_margin_ms=600),
            _policy(),
        )

        self.assertEqual(result.action, ExitAction.SELL)
        self.assertEqual(
            result.reason_codes,
            ("exit_latency_budget_outside_timing_horizon",),
        )

    def test_incoherent_q10_timing_quantile_forces_sell(self) -> None:
        """q10 timing must agree with the cumulative timing probabilities."""

        result = decide_dynamic_exit(
            _snapshot(
                q10_remaining_ms=5_000,
                p_dump_1s=100_000,
                p_dump_3s=120_000,
                p_dump_5s=140_000,
            ),
            _policy(),
        )

        self.assertEqual(result.action, ExitAction.SELL)
        self.assertEqual(result.reason_codes, ("incoherent_exit_timing_quantile",))

    def test_full_exit_failure_above_cap_forces_sell(self) -> None:
        """High full-exit failure risk exits above model edge."""

        result = decide_dynamic_exit(
            _snapshot(p_full_exit_failure=90_000),
            _policy(max_full_exit_failure=80_000),
        )

        self.assertEqual(result.action, ExitAction.SELL)
        self.assertEqual(result.reason_codes, ("full_exit_failure_above_cap",))

    def test_volume_liquidity_mismatch_above_cap_forces_sell(self) -> None:
        """Fake-volume mismatch blocks HOLD without increasing capacity."""

        result = decide_dynamic_exit(
            _snapshot(volume_mismatch=2),
            _policy(max_volume_mismatch=1),
        )

        self.assertEqual(result.action, ExitAction.SELL)
        self.assertEqual(
            result.reason_codes,
            ("volume_liquidity_mismatch_above_cap",),
        )

    def test_liquidity_position_mismatch_forces_sell(self) -> None:
        """Liquidity evidence must bind to the exact current position."""

        result = decide_dynamic_exit(_snapshot(liquidity_position=49_999), _policy())

        self.assertEqual(result.action, ExitAction.SELL)
        self.assertEqual(result.reason_codes, ("liquidity_position_mismatch",))

    def test_exit_capacity_breach_forces_sell(self) -> None:
        """A position above one-shot exit capacity exits immediately."""

        result = decide_dynamic_exit(
            _snapshot(max_one_shot_exit_size=49_999),
            _policy(),
        )

        self.assertEqual(result.action, ExitAction.SELL)
        self.assertEqual(result.reason_codes, ("exit_capacity_breach",))

    def test_quote_output_liquidity_mismatch_forces_sell(self) -> None:
        """Loaded liquidity output must match the executable full-exit quote."""

        result = decide_dynamic_exit(
            _snapshot(current_full_exit_output=99_999),
            _policy(),
        )

        self.assertEqual(result.action, ExitAction.SELL)
        self.assertEqual(result.reason_codes, ("liquidity_quote_output_mismatch",))

    def test_bool_full_position_abstains_before_quote_binding(self) -> None:
        """A bool position size cannot bind to a numerically equal quote input."""

        bool_position = True
        result = decide_dynamic_exit(
            _snapshot(
                full_position=cast("Any", bool_position),
                quote=_quote(input_amount=1),
            ),
            _policy(),
        )

        self.assertIsInstance(result, AbstainResult)
        result = cast("AbstainResult", result)
        self.assertEqual(result.reason, AbstainReason.UNSUPPORTED_PROTOCOL_STATE)


def _snapshot(**overrides: object) -> ExitModelSnapshot:
    q10_remaining_ms = _override_int(overrides, "q10_remaining_ms", 5_000)
    p_dump_1s = _override_int(overrides, "p_dump_1s", 40_000)
    p_dump_3s = _override_int(overrides, "p_dump_3s", 70_000)
    p_dump_5s = _override_int(overrides, "p_dump_5s", 100_000)
    p_dump_10s = _override_int(overrides, "p_dump_10s", 150_000)
    p_dump_ppm = _override_int(overrides, "p_dump_ppm", 100_000)
    expected_extra_profit = _override_int(overrides, "expected_extra_profit", 200_000)
    expected_dump_loss = _override_int(overrides, "expected_dump_loss", 300_000)
    execution_cost = _override_int(overrides, "execution_cost", 10_000)
    uncertainty_penalty = _override_int(overrides, "uncertainty_penalty", 20_000)
    full_position = _override_int(overrides, "full_position", 50_000)
    quote = overrides.get("quote", _quote())
    quote_output = (
        quote.output_amount_base_units
        if isinstance(quote, ExecutableQuote)
        else 100_000
    )
    deterministic_sell_reasons = cast(
        "tuple[str, ...]",
        overrides.get("deterministic_sell_reasons", ()),
    )
    return ExitModelSnapshot(
        as_of_slot=cast("Any", overrides.get("as_of_slot", Slot(10))),
        data_start_slot=cast("Any", overrides.get("data_start_slot", Slot(1))),
        data_end_slot=cast("Any", overrides.get("data_end_slot", Slot(10))),
        exit_snapshot_version=cast(
            "str", overrides.get("exit_snapshot_version", "exit-snapshot-v1")
        ),
        source_artifact_version=cast(
            "str", overrides.get("source_artifact_version", "exit-source-v1")
        ),
        timing_model_version=cast(
            "str", overrides.get("timing_model_version", "timing-v1")
        ),
        value_model_version=cast(
            "str", overrides.get("value_model_version", "value-v1")
        ),
        latency_model_version=cast(
            "str", overrides.get("latency_model_version", "latency-v1")
        ),
        liquidity_snapshot_version=cast(
            "str", overrides.get("liquidity_snapshot_version", "liquidity-v1")
        ),
        liquidity_source_artifact_version=cast(
            "str",
            overrides.get(
                "liquidity_source_artifact_version",
                "full-exit-liquidity-stress-v1",
            ),
        ),
        quote_engine_version=cast(
            "str", overrides.get("quote_engine_version", "quote-v1")
        ),
        simulator_version=cast(
            "str", overrides.get("simulator_version", "simulator-v1")
        ),
        market_snapshot_version=cast(
            "str", overrides.get("market_snapshot_version", "market-v1")
        ),
        reserve_snapshot_version=cast(
            "str", overrides.get("reserve_snapshot_version", "reserves-v1")
        ),
        fee_config_version=cast("str", overrides.get("fee_config_version", "fee-v1")),
        volume_classifier_version=cast(
            "str", overrides.get("volume_classifier_version", "volume-v1")
        ),
        q10_remaining_dump_time_ms=q10_remaining_ms,
        p_dump_next_1s_ppm=p_dump_1s,
        p_dump_next_3s_ppm=p_dump_3s,
        p_dump_next_5s_ppm=p_dump_5s,
        p_dump_next_10s_ppm=p_dump_10s,
        p_dump_before_exit_ppm=p_dump_ppm,
        expected_extra_profit_lamports=Lamports(expected_extra_profit),
        expected_dump_loss_lamports=Lamports(expected_dump_loss),
        execution_cost_lamports=Lamports(execution_cost),
        uncertainty_penalty_lamports=Lamports(uncertainty_penalty),
        p99_exit_latency_ms=_override_int(overrides, "p99_exit_latency_ms", 700),
        safety_margin_ms=_override_int(overrides, "safety_margin_ms", 500),
        full_position_base_units=full_position,
        liquidity_data_start_slot=cast(
            "Any", overrides.get("liquidity_data_start_slot", Slot(1))
        ),
        liquidity_data_end_slot=cast(
            "Any", overrides.get("liquidity_data_end_slot", Slot(10))
        ),
        liquidity_selected_full_position_base_units=_override_int(
            overrides,
            "liquidity_position",
            full_position,
        ),
        max_one_shot_exit_size_base_units=_override_int(
            overrides,
            "max_one_shot_exit_size",
            full_position,
        ),
        current_full_exit_output_base_units=_override_int(
            overrides,
            "current_full_exit_output",
            quote_output,
        ),
        stressed_full_exit_output_base_units=_override_int(
            overrides,
            "stressed_full_exit_output",
            80_000,
        ),
        p_full_exit_failure_ppm=_override_int(overrides, "p_full_exit_failure", 20_000),
        volume_liquidity_mismatch_count=_override_int(overrides, "volume_mismatch", 0),
        full_position_sell_quote=cast("Any", quote),
        evidence_ids=cast("Any", overrides.get("evidence_ids", ("exit-evidence",))),
        timing_evidence_ids=cast(
            "Any", overrides.get("timing_evidence_ids", ("timing-evidence",))
        ),
        value_evidence_ids=cast(
            "Any", overrides.get("value_evidence_ids", ("value-evidence",))
        ),
        latency_evidence_ids=cast(
            "Any", overrides.get("latency_evidence_ids", ("latency-evidence",))
        ),
        liquidity_evidence_ids=cast(
            "Any",
            overrides.get("liquidity_evidence_ids", ("liquidity-evidence",)),
        ),
        reason_codes=cast("Any", overrides.get("reason_codes", ("exit-built",))),
        deterministic_sell_reasons=deterministic_sell_reasons,
    )


def _quote(*, input_amount: int = 50_000) -> ExecutableQuote:
    return ExecutableQuote(
        path=QuotePath.PUMP_BONDING_CURVE,
        as_of_slot=Slot(10),
        input_amount_base_units=input_amount,
        output_amount_base_units=100_000,
        fee_amount_base_units=1_000,
        base_decimals=6,
        quote_decimals=9,
        fee_config_version="fee-v1",
        decoder_version="decoder-v1",
        idl_hash="idl-hash",
        program_config_version="program-config-v1",
    )


def _policy(**overrides: object) -> ExitSnapshotPolicy:
    return ExitSnapshotPolicy(
        as_of_slot=cast("Any", overrides.get("as_of_slot", Slot(10))),
        accepted_exit_snapshot_versions=("exit-snapshot-v1",),
        accepted_source_artifact_versions=("exit-source-v1",),
        accepted_timing_model_versions=("timing-v1",),
        accepted_value_model_versions=("value-v1",),
        accepted_latency_model_versions=("latency-v1",),
        accepted_liquidity_snapshot_versions=("liquidity-v1",),
        accepted_liquidity_source_artifact_versions=("full-exit-liquidity-stress-v1",),
        accepted_quote_engine_versions=("quote-v1",),
        accepted_simulator_versions=("simulator-v1",),
        accepted_market_snapshot_versions=("market-v1",),
        accepted_reserve_snapshot_versions=("reserves-v1",),
        accepted_fee_config_versions=("fee-v1",),
        accepted_volume_classifier_versions=("volume-v1",),
        accepted_quote_decoder_versions=("decoder-v1",),
        accepted_quote_idl_hashes=("idl-hash",),
        accepted_quote_program_config_versions=("program-config-v1",),
        max_full_exit_failure_ppm=_override_int(
            overrides,
            "max_full_exit_failure",
            80_000,
        ),
        max_volume_liquidity_mismatch_count=_override_int(
            overrides,
            "max_volume_mismatch",
            0,
        ),
    )


def _override_int(
    overrides: dict[str, object],
    key: str,
    default: int,
) -> int:
    return cast("int", overrides.get(key, default))


if __name__ == "__main__":
    unittest.main()
