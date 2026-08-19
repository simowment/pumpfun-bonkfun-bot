"""Liquidity sizing and counterfactual entry-gate tests."""

import ast
import unittest
from dataclasses import replace
from pathlib import Path
from typing import Any, cast

from rugbot.decision.sizing import (
    CandidateEntrySize,
    CounterfactualEntryCandidateInput,
    CounterfactualEntrySimulationResult,
    EnterSkipDecision,
    EntryDecisionAction,
    EntryEdgeSnapshot,
    EntryGateInputs,
    EntryGateThresholds,
    EntryLatencySnapshot,
    LiquiditySizingResult,
    LiquiditySnapshot,
    PolicyBackedEntryArtifactPolicy,
    PolicyBackedEntryGateInputs,
    PolicyBackedEntryThresholds,
    SizingConstraints,
    evaluate_counterfactual_entry,
    evaluate_policy_backed_counterfactual_entry,
    select_liquidity_size,
    simulate_counterfactual_entry_candidates,
)
from rugbot.decision.snapshots import (
    DecisionSnapshotBundle,
    DecisionSnapshotPolicy,
    LaunchMatcherSnapshot,
    RuggerSelectorSnapshot,
    RugTimingSnapshot,
)
from rugbot.domain.amounts import Lamports, QuoteBaseUnits, Slot
from rugbot.domain.decisions import AbstainReason, AbstainResult
from rugbot.domain.quotes import ExecutableQuote, QuotePath
from rugbot.graph.wallet_churn import (
    OPERATOR_WALLET_CHURN_SNAPSHOT_VERSION,
)

DECISION_MODULES = (Path("src/rugbot/decision/sizing.py"),)
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


class CounterfactualEntrySimulatorTests(unittest.TestCase):
    """Tests for pure state-after-entry candidate simulation."""

    def test_simulates_candidates_from_post_entry_quotes_and_timing(self) -> None:
        """Simulation output uses explicit state-after-entry timing and quotes."""

        result = simulate_counterfactual_entry_candidates(
            candidate_inputs=(
                _candidate_input(
                    proposed_quote=50_000,
                    entry_output=5_000,
                    immediate_exit_output=42_000,
                    p_dump_10s=200_000,
                    q10_ms=2_500,
                ),
                _candidate_input(
                    proposed_quote=100_000,
                    entry_output=9_000,
                    immediate_exit_output=85_000,
                    p_dump_10s=220_000,
                    q10_ms=2_000,
                ),
            )
        )

        self.assertIsInstance(result, CounterfactualEntrySimulationResult)
        result = cast("CounterfactualEntrySimulationResult", result)
        self.assertEqual(
            result.reason_codes, ("counterfactual_entry_candidates_simulated",)
        )
        self.assertEqual(len(result.candidates), 2)
        first = result.candidates[0]
        self.assertEqual(first.quote_amount_base_units, QuoteBaseUnits(50_000))
        self.assertEqual(first.expected_position_base_units, 5_000)
        self.assertEqual(first.hazard_after_entry_ppm, 200_000)
        self.assertEqual(first.q10_remaining_time_after_entry_ms, 2_500)
        self.assertEqual(first.immediate_exit_loss_lamports, Lamports(8_000))

    def test_entry_quote_abstention_abstains(self) -> None:
        """A quote abstention prevents fallback candidate generation."""

        result = simulate_counterfactual_entry_candidates(
            candidate_inputs=(
                _candidate_input(
                    proposed_quote=50_000,
                    entry_quote=AbstainResult(
                        reason=AbstainReason.UNKNOWN_PROTOCOL_STATE,
                        message="unknown",
                        as_of_slot=10,
                    ),
                ),
            )
        )

        self.assertIsInstance(result, AbstainResult)
        result = cast("AbstainResult", result)
        self.assertEqual(result.reason, AbstainReason.UNKNOWN_PROTOCOL_STATE)
        self.assertEqual(result.as_of_slot, 10)

    def test_malformed_candidate_input_member_abstains(self) -> None:
        """Malformed candidate input tuple members fail closed."""

        result = simulate_counterfactual_entry_candidates(
            candidate_inputs=(cast("Any", object()),)
        )

        self.assertIsInstance(result, AbstainResult)
        result = cast("AbstainResult", result)
        self.assertEqual(result.reason, AbstainReason.UNSUPPORTED_PROTOCOL_STATE)

    def test_stale_timing_after_entry_abstains(self) -> None:
        """Counterfactual timing must use the same slot as quote evidence."""

        result = simulate_counterfactual_entry_candidates(
            candidate_inputs=(_candidate_input(proposed_quote=50_000, timing_slot=9),)
        )

        self.assertIsInstance(result, AbstainResult)
        result = cast("AbstainResult", result)
        self.assertEqual(result.reason, AbstainReason.STALE_STATE)

    def test_quote_input_mismatch_abstains(self) -> None:
        """Entry quotes must match the proposed own-buy size."""

        result = simulate_counterfactual_entry_candidates(
            candidate_inputs=(
                _candidate_input(proposed_quote=50_000, entry_input=25_000),
            )
        )

        self.assertIsInstance(result, AbstainResult)
        result = cast("AbstainResult", result)
        self.assertEqual(result.reason, AbstainReason.MISSING_FEATURE)

    def test_quote_provenance_mismatch_abstains(self) -> None:
        """Entry and immediate-exit quotes must come from the same market state."""

        result = simulate_counterfactual_entry_candidates(
            candidate_inputs=(
                _candidate_input(
                    proposed_quote=50_000,
                    exit_quote=_quote(input_amount=5_000, decoder_version="decoder-v2"),
                ),
            )
        )

        self.assertIsInstance(result, AbstainResult)
        result = cast("AbstainResult", result)
        self.assertEqual(result.reason, AbstainReason.DECODER_MISMATCH)

    def test_float_timing_probability_abstains(self) -> None:
        """Post-entry timing probabilities must be integer PPM."""

        result = simulate_counterfactual_entry_candidates(
            candidate_inputs=(
                _candidate_input(proposed_quote=50_000, p_dump_10s=cast("Any", 0.5)),
            )
        )

        self.assertIsInstance(result, AbstainResult)
        result = cast("AbstainResult", result)
        self.assertEqual(result.reason, AbstainReason.UNSUPPORTED_PROTOCOL_STATE)

    def test_incoherent_q10_timing_abstains(self) -> None:
        """A q10 time requires cumulative probability to cross 10 percent."""

        result = simulate_counterfactual_entry_candidates(
            candidate_inputs=(
                _candidate_input(
                    proposed_quote=50_000,
                    p_dump_10s=50_000,
                    q10_ms=2_500,
                ),
            )
        )

        self.assertIsInstance(result, AbstainResult)
        result = cast("AbstainResult", result)
        self.assertEqual(result.reason, AbstainReason.UNSUPPORTED_PROTOCOL_STATE)

    def test_malformed_timing_artifact_abstains(self) -> None:
        """Malformed loaded timing artifacts fail closed."""

        result = simulate_counterfactual_entry_candidates(
            candidate_inputs=(
                _candidate_input(
                    proposed_quote=50_000,
                    timing_after_entry=cast("Any", object()),
                ),
            )
        )

        self.assertIsInstance(result, AbstainResult)
        result = cast("AbstainResult", result)
        self.assertEqual(result.reason, AbstainReason.UNSUPPORTED_PROTOCOL_STATE)
        self.assertEqual(result.as_of_slot, 10)

    def test_float_candidate_input_slot_abstains(self) -> None:
        """Counterfactual input slots must be strict integers."""

        result = simulate_counterfactual_entry_candidates(
            candidate_inputs=(
                _candidate_input(proposed_quote=50_000),
                _candidate_input(
                    as_of_slot=cast("Any", 10.0),
                    proposed_quote=100_000,
                ),
            )
        )

        self.assertIsInstance(result, AbstainResult)
        result = cast("AbstainResult", result)
        self.assertEqual(result.reason, AbstainReason.UNSUPPORTED_PROTOCOL_STATE)

    def test_float_quote_slot_abstains(self) -> None:
        """Executable quote slots must be strict integers."""

        result = simulate_counterfactual_entry_candidates(
            candidate_inputs=(
                _candidate_input(
                    proposed_quote=50_000,
                    entry_quote=_quote(
                        input_amount=50_000,
                        output_amount=5_000,
                        as_of_slot=cast("Any", 10.0),
                    ),
                ),
            )
        )

        self.assertIsInstance(result, AbstainResult)
        result = cast("AbstainResult", result)
        self.assertEqual(result.reason, AbstainReason.UNSUPPORTED_PROTOCOL_STATE)

    def test_counterfactual_version_mismatch_abstains(self) -> None:
        """Candidate inputs in one batch must use the same simulation version."""

        result = simulate_counterfactual_entry_candidates(
            candidate_inputs=(
                _candidate_input(proposed_quote=50_000),
                _candidate_input(
                    proposed_quote=100_000,
                    simulation_version="counterfactual-v2",
                ),
            )
        )

        self.assertIsInstance(result, AbstainResult)
        result = cast("AbstainResult", result)
        self.assertEqual(result.reason, AbstainReason.DECODER_MISMATCH)

    def test_missing_counterfactual_evidence_abstains(self) -> None:
        """Candidate inputs require immutable evidence IDs."""

        result = simulate_counterfactual_entry_candidates(
            candidate_inputs=(_candidate_input(proposed_quote=50_000, evidence_ids=()),)
        )

        self.assertIsInstance(result, AbstainResult)
        result = cast("AbstainResult", result)
        self.assertEqual(result.reason, AbstainReason.MISSING_FEATURE)


class LiquiditySizingTests(unittest.TestCase):
    """Tests for pure liquidity sizing."""

    def test_selects_largest_candidate_inside_all_caps(self) -> None:
        """Sizing chooses the largest executable candidate, not max bankroll."""

        result = select_liquidity_size(
            candidates=(
                _candidate(quote_amount=50_000, position=5_000),
                _candidate(quote_amount=100_000, position=9_000),
                _candidate(quote_amount=200_000, position=12_000),
            ),
            liquidity_snapshots=_liquidity_snapshots(
                5_000, 9_000, 12_000, max_exit_position=10_000
            ),
            constraints=_constraints(stressed_cap=150_000),
        )

        self.assertIsInstance(result, LiquiditySizingResult)
        result = cast("LiquiditySizingResult", result)
        self.assertIsNotNone(result.selected_size)
        selected = cast("CandidateEntrySize", result.selected_size)
        self.assertEqual(selected.quote_amount_base_units, QuoteBaseUnits(100_000))
        self.assertEqual(result.max_entry_quote_base_units, QuoteBaseUnits(150_000))

    def test_returns_no_selection_when_full_exit_failure_too_high(self) -> None:
        """High full-exit failure probability prevents entry sizing."""

        result = select_liquidity_size(
            candidates=(_candidate(quote_amount=50_000, position=5_000),),
            liquidity_snapshots=(
                _liquidity(position=5_000, max_exit_position=10_000, p_failure=90_000),
            ),
            constraints=_constraints(max_full_exit_failure=80_000),
        )

        self.assertIsInstance(result, LiquiditySizingResult)
        result = cast("LiquiditySizingResult", result)
        self.assertIsNone(result.selected_size)
        self.assertEqual(result.reason_codes, ("full_exit_failure_above_cap",))

    def test_missing_candidates_abstains(self) -> None:
        """Missing candidate sizes are missing features."""

        result = select_liquidity_size(
            candidates=(),
            liquidity_snapshots=(_liquidity(position=5_000, max_exit_position=10_000),),
            constraints=_constraints(),
        )

        self.assertIsInstance(result, AbstainResult)
        result = cast("AbstainResult", result)
        self.assertEqual(result.reason, AbstainReason.MISSING_FEATURE)
        self.assertEqual(result.as_of_slot, 10)

    def test_slot_mismatch_abstains(self) -> None:
        """Sizing snapshots from different slots are stale."""

        result = select_liquidity_size(
            candidates=(_candidate(quote_amount=50_000, position=5_000),),
            liquidity_snapshots=(
                _liquidity(position=5_000, max_exit_position=10_000, as_of_slot=9),
            ),
            constraints=_constraints(as_of_slot=10),
        )

        self.assertIsInstance(result, AbstainResult)
        result = cast("AbstainResult", result)
        self.assertEqual(result.reason, AbstainReason.STALE_STATE)

    def test_float_liquidity_slot_abstains(self) -> None:
        """Liquidity slots must be strict integers before equality checks."""

        result = select_liquidity_size(
            candidates=(_candidate(quote_amount=50_000, position=5_000),),
            liquidity_snapshots=(
                _liquidity(
                    position=5_000,
                    max_exit_position=10_000,
                    as_of_slot=cast("Any", 10.0),
                ),
            ),
            constraints=_constraints(as_of_slot=10),
        )

        self.assertIsInstance(result, AbstainResult)
        result = cast("AbstainResult", result)
        self.assertEqual(result.reason, AbstainReason.UNSUPPORTED_PROTOCOL_STATE)

    def test_float_constraints_slot_abstains(self) -> None:
        """Sizing constraint slots must be strict integers before equality checks."""

        result = select_liquidity_size(
            candidates=(_candidate(quote_amount=50_000, position=5_000),),
            liquidity_snapshots=(
                _liquidity(position=5_000, max_exit_position=10_000, as_of_slot=10),
            ),
            constraints=_constraints(as_of_slot=cast("Any", 10.0)),
        )

        self.assertIsInstance(result, AbstainResult)
        result = cast("AbstainResult", result)
        self.assertEqual(result.reason, AbstainReason.UNSUPPORTED_PROTOCOL_STATE)

    def test_invalid_probability_abstains(self) -> None:
        """Invalid probability outputs abstain instead of entering."""

        result = select_liquidity_size(
            candidates=(_candidate(quote_amount=50_000, position=5_000),),
            liquidity_snapshots=(
                _liquidity(
                    position=5_000,
                    max_exit_position=10_000,
                    p_failure=1_000_001,
                ),
            ),
            constraints=_constraints(),
        )

        self.assertIsInstance(result, AbstainResult)
        result = cast("AbstainResult", result)
        self.assertEqual(result.reason, AbstainReason.UNSUPPORTED_PROTOCOL_STATE)

    def test_float_liquidity_probability_abstains(self) -> None:
        """Liquidity probabilities must be integer PPM."""

        result = select_liquidity_size(
            candidates=(_candidate(quote_amount=50_000, position=5_000),),
            liquidity_snapshots=(
                _liquidity(
                    position=5_000,
                    max_exit_position=10_000,
                    p_failure=cast("Any", 0.2),
                ),
            ),
            constraints=_constraints(),
        )

        self.assertIsInstance(result, AbstainResult)
        result = cast("AbstainResult", result)
        self.assertEqual(result.reason, AbstainReason.UNSUPPORTED_PROTOCOL_STATE)

    def test_invalid_candidate_hazard_abstains(self) -> None:
        """Corrupted candidate model output abstains instead of trying smaller size."""

        result = select_liquidity_size(
            candidates=(
                _candidate(quote_amount=50_000, position=5_000, hazard=1_000_001),
                _candidate(quote_amount=25_000, position=2_500),
            ),
            liquidity_snapshots=_liquidity_snapshots(
                5_000, 2_500, max_exit_position=10_000
            ),
            constraints=_constraints(),
        )

        self.assertIsInstance(result, AbstainResult)
        result = cast("AbstainResult", result)
        self.assertEqual(result.reason, AbstainReason.UNSUPPORTED_PROTOCOL_STATE)

    def test_candidate_slot_mismatch_abstains(self) -> None:
        """Candidate simulations are keyed by as_of_slot."""

        result = select_liquidity_size(
            candidates=(_candidate(quote_amount=50_000, position=5_000, slot=9),),
            liquidity_snapshots=(_liquidity(position=5_000, max_exit_position=10_000),),
            constraints=_constraints(as_of_slot=10),
        )

        self.assertIsInstance(result, AbstainResult)
        result = cast("AbstainResult", result)
        self.assertEqual(result.reason, AbstainReason.STALE_STATE)

    def test_float_candidate_quote_amount_abstains(self) -> None:
        """Candidate quote amounts must be exact integer base units."""

        result = select_liquidity_size(
            candidates=(
                _candidate(
                    quote_amount=cast("Any", 50_000.0),
                    position=5_000,
                ),
            ),
            liquidity_snapshots=(_liquidity(position=5_000, max_exit_position=10_000),),
            constraints=_constraints(),
        )

        self.assertIsInstance(result, AbstainResult)
        result = cast("AbstainResult", result)
        self.assertEqual(result.reason, AbstainReason.UNSUPPORTED_PROTOCOL_STATE)

    def test_missing_liquidity_evidence_abstains(self) -> None:
        """Liquidity snapshots must preserve source evidence IDs."""

        result = select_liquidity_size(
            candidates=(_candidate(quote_amount=50_000, position=5_000),),
            liquidity_snapshots=(
                _liquidity(
                    position=5_000,
                    max_exit_position=10_000,
                    evidence_ids=(),
                ),
            ),
            constraints=_constraints(),
        )

        self.assertIsInstance(result, AbstainResult)
        result = cast("AbstainResult", result)
        self.assertEqual(result.reason, AbstainReason.MISSING_FEATURE)

    def test_unknown_liquidity_version_abstains(self) -> None:
        """Unknown quote or reserve provenance never becomes usable capacity."""

        result = select_liquidity_size(
            candidates=(_candidate(quote_amount=50_000, position=5_000),),
            liquidity_snapshots=(
                _liquidity(
                    position=5_000,
                    max_exit_position=10_000,
                    reserve_snapshot_version="unknown-reserves",
                ),
            ),
            constraints=_constraints(),
        )

        self.assertIsInstance(result, AbstainResult)
        result = cast("AbstainResult", result)
        self.assertEqual(result.reason, AbstainReason.DECODER_MISMATCH)

    def test_stale_liquidity_interval_abstains(self) -> None:
        """Liquidity evidence must be current to the sizing decision slot."""

        result = select_liquidity_size(
            candidates=(_candidate(quote_amount=50_000, position=5_000),),
            liquidity_snapshots=(
                _liquidity(
                    position=5_000,
                    max_exit_position=10_000,
                    data_end_slot=9,
                ),
            ),
            constraints=_constraints(),
        )

        self.assertIsInstance(result, AbstainResult)
        result = cast("AbstainResult", result)
        self.assertEqual(result.reason, AbstainReason.STALE_STATE)

    def test_liquidity_position_mismatch_abstains(self) -> None:
        """Each candidate must have exact full-position liquidity evidence."""

        result = select_liquidity_size(
            candidates=(_candidate(quote_amount=50_000, position=5_000),),
            liquidity_snapshots=(_liquidity(position=4_999, max_exit_position=10_000),),
            constraints=_constraints(),
        )

        self.assertIsInstance(result, AbstainResult)
        result = cast("AbstainResult", result)
        self.assertEqual(result.reason, AbstainReason.MISSING_FEATURE)

    def test_volume_mismatch_above_cap_returns_no_selection(self) -> None:
        """Fake-volume context blocks sizing without increasing capacity."""

        result = select_liquidity_size(
            candidates=(_candidate(quote_amount=50_000, position=5_000),),
            liquidity_snapshots=(
                _liquidity(
                    position=5_000,
                    max_exit_position=10_000,
                    volume_mismatch_count=2,
                ),
            ),
            constraints=_constraints(max_volume_mismatch=1),
        )

        self.assertIsInstance(result, LiquiditySizingResult)
        result = cast("LiquiditySizingResult", result)
        self.assertIsNone(result.selected_size)
        self.assertEqual(result.reason_codes, ("volume_liquidity_mismatch_above_cap",))

    def test_exact_one_shot_capacity_equality_passes(self) -> None:
        """A candidate exactly at audited one-shot capacity remains selectable."""

        result = select_liquidity_size(
            candidates=(_candidate(quote_amount=50_000, position=5_000),),
            liquidity_snapshots=(_liquidity(position=5_000, max_exit_position=5_000),),
            constraints=_constraints(),
        )

        self.assertIsInstance(result, LiquiditySizingResult)
        result = cast("LiquiditySizingResult", result)
        selected = cast("CandidateEntrySize", result.selected_size)
        self.assertEqual(selected.expected_position_base_units, 5_000)

    def test_high_volume_does_not_select_over_capacity_candidate(self) -> None:
        """Large recent volume cannot override reserve-backed exit capacity."""

        result = select_liquidity_size(
            candidates=(
                _candidate(quote_amount=50_000, position=5_000),
                _candidate(quote_amount=100_000, position=9_000),
            ),
            liquidity_snapshots=(
                _liquidity(
                    position=5_000,
                    max_exit_position=5_000,
                    volume_mismatch_count=0,
                ),
                _liquidity(
                    position=9_000,
                    max_exit_position=8_999,
                    volume_mismatch_count=0,
                ),
            ),
            constraints=_constraints(max_volume_mismatch=10),
        )

        self.assertIsInstance(result, LiquiditySizingResult)
        result = cast("LiquiditySizingResult", result)
        selected = cast("CandidateEntrySize", result.selected_size)
        self.assertEqual(selected.expected_position_base_units, 5_000)

    def test_independent_volume_participation_caps_candidate_size(self) -> None:
        """Audited independent volume can reduce, not increase, entry size."""

        result = select_liquidity_size(
            candidates=(
                _candidate(quote_amount=50_000, position=5_000),
                _candidate(quote_amount=100_000, position=9_000),
            ),
            liquidity_snapshots=(
                _liquidity(
                    position=5_000,
                    max_exit_position=10_000,
                    independent_recent_volume=100_000,
                ),
                _liquidity(
                    position=9_000,
                    max_exit_position=10_000,
                    independent_recent_volume=100_000,
                ),
            ),
            constraints=_constraints(max_volume_participation=500_000),
        )

        self.assertIsInstance(result, LiquiditySizingResult)
        result = cast("LiquiditySizingResult", result)
        selected = cast("CandidateEntrySize", result.selected_size)
        self.assertEqual(selected.quote_amount_base_units, QuoteBaseUnits(50_000))
        self.assertEqual(
            result.volume_participation_cap_quote_base_units,
            QuoteBaseUnits(50_000),
        )
        self.assertEqual(result.max_entry_quote_base_units, QuoteBaseUnits(50_000))

    def test_zero_independent_volume_returns_no_selection(self) -> None:
        """Zero independent flow means no stealth participation budget."""

        result = select_liquidity_size(
            candidates=(_candidate(quote_amount=50_000, position=5_000),),
            liquidity_snapshots=(
                _liquidity(
                    position=5_000,
                    max_exit_position=10_000,
                    independent_recent_volume=0,
                ),
            ),
            constraints=_constraints(max_volume_participation=500_000),
        )

        self.assertIsInstance(result, LiquiditySizingResult)
        result = cast("LiquiditySizingResult", result)
        self.assertIsNone(result.selected_size)
        self.assertEqual(
            result.reason_codes,
            ("candidate_quote_amount_above_volume_participation_cap",),
        )
        self.assertEqual(result.max_entry_quote_base_units, QuoteBaseUnits(0))

    def test_float_independent_volume_abstains(self) -> None:
        """Independent-volume evidence must be integer quote base units."""

        result = select_liquidity_size(
            candidates=(_candidate(quote_amount=50_000, position=5_000),),
            liquidity_snapshots=(
                _liquidity(
                    position=5_000,
                    max_exit_position=10_000,
                    independent_recent_volume=cast("Any", 100_000.0),
                ),
            ),
            constraints=_constraints(),
        )

        self.assertIsInstance(result, AbstainResult)
        result = cast("AbstainResult", result)
        self.assertEqual(result.reason, AbstainReason.UNSUPPORTED_PROTOCOL_STATE)

    def test_invalid_volume_participation_cap_abstains(self) -> None:
        """Participation caps are strict integer probability PPM values."""

        result = select_liquidity_size(
            candidates=(_candidate(quote_amount=50_000, position=5_000),),
            liquidity_snapshots=(_liquidity(position=5_000, max_exit_position=10_000),),
            constraints=_constraints(max_volume_participation=cast("Any", 0.5)),
        )

        self.assertIsInstance(result, AbstainResult)
        result = cast("AbstainResult", result)
        self.assertEqual(result.reason, AbstainReason.UNSUPPORTED_PROTOCOL_STATE)


class CounterfactualEntryGateTests(unittest.TestCase):
    """Tests for the counterfactual entry gate."""

    def test_entry_passes_when_probabilities_latency_and_edge_pass(self) -> None:
        """Entry requires entity, regime, latency, edge, and selected size."""

        sizing = _sizing_result(
            selected=_candidate(quote_amount=50_000, position=5_000)
        )

        decision = evaluate_counterfactual_entry(
            inputs=_entry_inputs(sizing_result=sizing),
            thresholds=_thresholds(),
        )

        self.assertIsInstance(decision, EnterSkipDecision)
        decision = cast("EnterSkipDecision", decision)
        self.assertEqual(decision.action, EntryDecisionAction.ENTER)
        self.assertEqual(decision.reason_codes, ("counterfactual_entry_passed",))

    def test_policy_backed_entry_passes_from_valid_selected_bundle(self) -> None:
        """Action-facing entry derives matcher and timing inputs from artifacts."""

        sizing = _sizing_result(
            selected=_candidate(quote_amount=50_000, position=5_000)
        )

        decision = evaluate_policy_backed_counterfactual_entry(
            inputs=_policy_entry_inputs(sizing_result=sizing),
            thresholds=_policy_thresholds(),
        )

        self.assertIsInstance(decision, EnterSkipDecision)
        decision = cast("EnterSkipDecision", decision)
        self.assertEqual(decision.action, EntryDecisionAction.ENTER)
        self.assertEqual(decision.reason_codes, ("counterfactual_entry_passed",))

    def test_policy_backed_entry_requires_selected_churn_audit(self) -> None:
        """Selected bundles cannot bypass the strict churn policy at entry."""

        result = evaluate_policy_backed_counterfactual_entry(
            inputs=_policy_entry_inputs(
                bundle=_decision_bundle(selector=_entry_selector()),
                sizing_result=_sizing_result(
                    selected=_candidate(quote_amount=50_000, position=5_000)
                ),
            ),
            thresholds=_policy_thresholds(),
        )

        self.assertIsInstance(result, AbstainResult)
        result = cast("AbstainResult", result)
        self.assertEqual(result.reason, AbstainReason.MISSING_FEATURE)

    def test_policy_backed_entry_rejects_non_strict_selected_policy(self) -> None:
        """A caller cannot make selected no-audit bundles actionable by policy."""

        result = evaluate_policy_backed_counterfactual_entry(
            inputs=_policy_entry_inputs(
                bundle=_decision_bundle(selector=_entry_selector()),
                policy=replace(
                    _decision_policy(),
                    require_selected_operator_churn_audit=False,
                ),
                sizing_result=_sizing_result(
                    selected=_candidate(quote_amount=50_000, position=5_000)
                ),
            ),
            thresholds=_policy_thresholds(),
        )

        self.assertIsInstance(result, AbstainResult)
        result = cast("AbstainResult", result)
        self.assertEqual(result.reason, AbstainReason.UNSUPPORTED_PROTOCOL_STATE)

    def test_policy_backed_entry_skips_unselected_selector_without_sizing(self) -> None:
        """Ordinary selector misses skip before requiring liquidity sizing."""

        selector = replace(
            _entry_selector(),
            is_selected=False,
            reason_codes=("entity_probability_below_threshold",),
        )

        decision = evaluate_policy_backed_counterfactual_entry(
            inputs=_policy_entry_inputs(
                bundle=_decision_bundle(selector=selector),
                sizing_result=None,
            ),
            thresholds=_policy_thresholds(),
        )

        self.assertIsInstance(decision, EnterSkipDecision)
        decision = cast("EnterSkipDecision", decision)
        self.assertEqual(decision.action, EntryDecisionAction.SKIP)
        self.assertIsNone(decision.selected_size)
        self.assertEqual(decision.reason_codes, ("selector_not_selected",))

    def test_policy_backed_unselected_selector_ignores_stale_latency(self) -> None:
        """Selector miss is terminal no-entry before unrelated action artifacts."""

        selector = replace(
            _entry_selector(),
            is_selected=False,
            reason_codes=("entity_probability_below_threshold",),
        )

        decision = evaluate_policy_backed_counterfactual_entry(
            inputs=_policy_entry_inputs(
                bundle=_decision_bundle(selector=selector),
                latency_snapshot=_entry_latency_snapshot(as_of_slot=9),
                sizing_result=None,
            ),
            thresholds=_policy_thresholds(),
        )

        self.assertIsInstance(decision, EnterSkipDecision)
        decision = cast("EnterSkipDecision", decision)
        self.assertEqual(decision.action, EntryDecisionAction.SKIP)
        self.assertEqual(decision.reason_codes, ("selector_not_selected",))

    def test_policy_backed_unselected_selector_ignores_missing_latency_edge(
        self,
    ) -> None:
        """Selector miss does not require selected-only action artifacts."""

        selector = replace(
            _entry_selector(),
            is_selected=False,
            reason_codes=("entity_probability_below_threshold",),
        )
        inputs = replace(
            _policy_entry_inputs(
                bundle=_decision_bundle(selector=selector),
                sizing_result=None,
            ),
            latency_snapshot=cast("Any", None),
            edge_snapshot=cast("Any", None),
        )

        decision = evaluate_policy_backed_counterfactual_entry(
            inputs=inputs,
            thresholds=_policy_thresholds(),
        )

        self.assertIsInstance(decision, EnterSkipDecision)
        decision = cast("EnterSkipDecision", decision)
        self.assertEqual(decision.action, EntryDecisionAction.SKIP)
        self.assertEqual(decision.reason_codes, ("selector_not_selected",))

    def test_policy_backed_unselected_selector_still_checks_bundle_versions(
        self,
    ) -> None:
        """Selector misses still fail closed on unaccepted loaded provenance."""

        selector = replace(
            _entry_selector(),
            is_selected=False,
            reason_codes=("entity_probability_below_threshold",),
        )
        bundle = _decision_bundle(
            matcher=replace(_entry_matcher(), matcher_version="matcher-v2"),
            selector=selector,
        )

        result = evaluate_policy_backed_counterfactual_entry(
            inputs=_policy_entry_inputs(
                bundle=bundle,
                sizing_result=None,
            ),
            thresholds=_policy_thresholds(),
        )

        self.assertIsInstance(result, AbstainResult)
        result = cast("AbstainResult", result)
        self.assertEqual(result.reason, AbstainReason.DECODER_MISMATCH)

    def test_policy_backed_entry_requires_sizing_for_selected_bundle(self) -> None:
        """Selected bundles need a loaded sizing result before action."""

        result = evaluate_policy_backed_counterfactual_entry(
            inputs=_policy_entry_inputs(sizing_result=None),
            thresholds=_policy_thresholds(),
        )

        self.assertIsInstance(result, AbstainResult)
        result = cast("AbstainResult", result)
        self.assertEqual(result.reason, AbstainReason.MISSING_FEATURE)

    def test_policy_backed_entry_rejects_stale_sizing_slot(self) -> None:
        """Entry cannot mix a selected bundle and sizing from different slots."""

        result = evaluate_policy_backed_counterfactual_entry(
            inputs=_policy_entry_inputs(
                sizing_result=_sizing_result(
                    selected=_candidate(
                        quote_amount=50_000,
                        position=5_000,
                        slot=9,
                    ),
                    as_of_slot=9,
                )
            ),
            thresholds=_policy_thresholds(),
        )

        self.assertIsInstance(result, AbstainResult)
        result = cast("AbstainResult", result)
        self.assertEqual(result.reason, AbstainReason.STALE_STATE)

    def test_policy_backed_entry_rejects_stale_sizing_before_versions(self) -> None:
        """Stale sizing slot fails before inspecting future artifact contents."""

        result = evaluate_policy_backed_counterfactual_entry(
            inputs=_policy_entry_inputs(
                sizing_result=_sizing_result(
                    selected=_candidate(
                        quote_amount=50_000,
                        position=5_000,
                        slot=11,
                    ),
                    as_of_slot=11,
                    simulator_version="",
                    accepted_simulator_versions=(),
                )
            ),
            thresholds=_policy_thresholds(),
        )

        self.assertIsInstance(result, AbstainResult)
        result = cast("AbstainResult", result)
        self.assertEqual(result.reason, AbstainReason.STALE_STATE)

    def test_policy_backed_entry_rejects_stale_latency_snapshot(self) -> None:
        """Latency inputs are versioned point-in-time artifacts."""

        result = evaluate_policy_backed_counterfactual_entry(
            inputs=_policy_entry_inputs(
                latency_snapshot=_entry_latency_snapshot(as_of_slot=9),
                sizing_result=_sizing_result(
                    selected=_candidate(quote_amount=50_000, position=5_000)
                ),
            ),
            thresholds=_policy_thresholds(),
        )

        self.assertIsInstance(result, AbstainResult)
        result = cast("AbstainResult", result)
        self.assertEqual(result.reason, AbstainReason.STALE_STATE)

    def test_policy_backed_entry_rejects_stale_threshold_policy(self) -> None:
        """Thresholds are explicit point-in-time action policy artifacts."""

        result = evaluate_policy_backed_counterfactual_entry(
            inputs=_policy_entry_inputs(
                sizing_result=_sizing_result(
                    selected=_candidate(quote_amount=50_000, position=5_000)
                )
            ),
            thresholds=_policy_thresholds(as_of_slot=9),
        )

        self.assertIsInstance(result, AbstainResult)
        result = cast("AbstainResult", result)
        self.assertEqual(result.reason, AbstainReason.STALE_STATE)

    def test_policy_backed_entry_rejects_malformed_bundle_component(self) -> None:
        """Malformed nested snapshots abstain instead of raising."""

        result = evaluate_policy_backed_counterfactual_entry(
            inputs=_policy_entry_inputs(
                bundle=replace(_decision_bundle(), matcher=cast("Any", None)),
                sizing_result=_sizing_result(
                    selected=_candidate(quote_amount=50_000, position=5_000)
                ),
            ),
            thresholds=_policy_thresholds(),
        )

        self.assertIsInstance(result, AbstainResult)
        result = cast("AbstainResult", result)
        self.assertEqual(result.reason, AbstainReason.UNSUPPORTED_PROTOCOL_STATE)

    def test_policy_backed_entry_rejects_unaccepted_matcher_version(self) -> None:
        """Bundle artifact versions are checked by external action policy."""

        bundle = _decision_bundle(
            matcher=replace(
                _entry_matcher(),
                entity_probability_ppm=990_000,
                matcher_version="matcher-v2",
            )
        )

        result = evaluate_policy_backed_counterfactual_entry(
            inputs=_policy_entry_inputs(
                bundle=bundle,
                sizing_result=_sizing_result(
                    selected=_candidate(quote_amount=50_000, position=5_000)
                ),
            ),
            thresholds=_policy_thresholds(),
        )

        self.assertIsInstance(result, AbstainResult)
        result = cast("AbstainResult", result)
        self.assertEqual(result.reason, AbstainReason.DECODER_MISMATCH)

    def test_policy_backed_entry_rejects_self_whitelisted_sizing_version(self) -> None:
        """Sizing artifacts cannot approve their own simulator version for entry."""

        result = evaluate_policy_backed_counterfactual_entry(
            inputs=_policy_entry_inputs(
                sizing_result=_sizing_result(
                    selected=_candidate(
                        quote_amount=50_000,
                        position=5_000,
                        q10_ms=10_000,
                    ),
                    simulator_version="simulator-v2",
                    accepted_simulator_versions=("simulator-v2",),
                ),
            ),
            thresholds=_policy_thresholds(),
        )

        self.assertIsInstance(result, AbstainResult)
        result = cast("AbstainResult", result)
        self.assertEqual(result.reason, AbstainReason.DECODER_MISMATCH)

    def test_policy_backed_entry_rejects_self_authorizing_artifact_policy(self) -> None:
        """The entry artifact policy must match pinned trusted allowlists."""

        result = evaluate_policy_backed_counterfactual_entry(
            inputs=_policy_entry_inputs(
                artifact_policy=replace(
                    _entry_artifact_policy(),
                    accepted_simulator_versions=("simulator-v2",),
                ),
                sizing_result=_sizing_result(
                    selected=_candidate(quote_amount=50_000, position=5_000),
                    simulator_version="simulator-v2",
                    accepted_simulator_versions=("simulator-v2",),
                ),
            ),
            thresholds=_policy_thresholds(),
        )

        self.assertIsInstance(result, AbstainResult)
        result = cast("AbstainResult", result)
        self.assertEqual(result.reason, AbstainReason.DECODER_MISMATCH)

    def test_policy_backed_entry_rejects_fake_churn_self_whitelist(self) -> None:
        """Churn audit provenance must be accepted by external action policy."""

        result = evaluate_policy_backed_counterfactual_entry(
            inputs=_policy_entry_inputs(
                bundle=_decision_bundle(
                    selector=replace(
                        _entry_selector_with_churn_audit(),
                        operator_churn_snapshot_version="fake-v2",
                    )
                ),
                policy=replace(
                    _decision_policy(),
                    accepted_operator_churn_snapshot_versions=("fake-v2",),
                ),
                sizing_result=_sizing_result(
                    selected=_candidate(quote_amount=50_000, position=5_000)
                ),
            ),
            thresholds=_policy_thresholds(),
        )

        self.assertIsInstance(result, AbstainResult)
        result = cast("AbstainResult", result)
        self.assertEqual(result.reason, AbstainReason.DECODER_MISMATCH)

    def test_policy_backed_entry_uses_bundle_probabilities(self) -> None:
        """The action-facing gate does not accept caller-supplied probabilities."""

        bundle = _decision_bundle(
            matcher=replace(_entry_matcher(), entity_probability_ppm=850_000),
            selector=_entry_selector_with_churn_audit(),
        )

        decision = evaluate_policy_backed_counterfactual_entry(
            inputs=_policy_entry_inputs(
                bundle=bundle,
                sizing_result=_sizing_result(
                    selected=_candidate(quote_amount=50_000, position=5_000)
                ),
            ),
            thresholds=PolicyBackedEntryThresholds(
                as_of_slot=Slot(10),
                threshold_policy_version="entry-thresholds-v1",
                entity_probability_threshold_ppm=900_000,
                regime_probability_threshold_ppm=800_000,
            ),
        )

        self.assertIsInstance(decision, EnterSkipDecision)
        decision = cast("EnterSkipDecision", decision)
        self.assertEqual(decision.action, EntryDecisionAction.SKIP)
        self.assertEqual(
            decision.reason_codes,
            ("entity_probability_below_threshold",),
        )

    def test_policy_backed_entry_uses_selected_candidate_q10(self) -> None:
        """Post-entry q10 comes from the audited selected candidate."""

        decision = evaluate_policy_backed_counterfactual_entry(
            inputs=_policy_entry_inputs(
                sizing_result=_sizing_result(
                    selected=_candidate(
                        quote_amount=50_000,
                        position=5_000,
                        q10_ms=1_000,
                    )
                )
            ),
            thresholds=_policy_thresholds(),
        )

        self.assertIsInstance(decision, EnterSkipDecision)
        decision = cast("EnterSkipDecision", decision)
        self.assertEqual(decision.action, EntryDecisionAction.SKIP)
        self.assertEqual(
            decision.reason_codes,
            ("q10_remaining_time_inside_latency_budget",),
        )

    def test_policy_backed_entry_rejects_malformed_policy(self) -> None:
        """Strict entry policy settings fail closed."""

        result = evaluate_policy_backed_counterfactual_entry(
            inputs=_policy_entry_inputs(
                policy=replace(
                    _decision_policy(),
                    require_selected_operator_churn_audit=cast("Any", 1),
                ),
                sizing_result=_sizing_result(
                    selected=_candidate(quote_amount=50_000, position=5_000)
                ),
            ),
            thresholds=_policy_thresholds(),
        )

        self.assertIsInstance(result, AbstainResult)
        result = cast("AbstainResult", result)
        self.assertEqual(result.reason, AbstainReason.UNSUPPORTED_PROTOCOL_STATE)

    def test_policy_backed_entry_rejects_malformed_threshold_policy(self) -> None:
        """Malformed action threshold policy fails closed."""

        result = evaluate_policy_backed_counterfactual_entry(
            inputs=_policy_entry_inputs(
                sizing_result=_sizing_result(
                    selected=_candidate(quote_amount=50_000, position=5_000)
                )
            ),
            thresholds=cast("Any", _thresholds()),
        )

        self.assertIsInstance(result, AbstainResult)
        result = cast("AbstainResult", result)
        self.assertEqual(result.reason, AbstainReason.UNSUPPORTED_PROTOCOL_STATE)

    def test_entry_skips_when_latency_budget_consumes_q10_time(self) -> None:
        """Own-buy entry is skipped when conservative remaining time is too short."""

        sizing = _sizing_result(
            selected=_candidate(quote_amount=50_000, position=5_000, q10_ms=1_000)
        )

        decision = evaluate_counterfactual_entry(
            inputs=_entry_inputs(
                sizing_result=sizing,
                q10_remaining_time_after_entry_ms=1_000,
            ),
            thresholds=_thresholds(),
        )

        self.assertIsInstance(decision, EnterSkipDecision)
        decision = cast("EnterSkipDecision", decision)
        self.assertEqual(decision.action, EntryDecisionAction.SKIP)
        self.assertEqual(
            decision.reason_codes,
            ("q10_remaining_time_inside_latency_budget",),
        )

    def test_entry_skips_when_no_size_selected(self) -> None:
        """Entry cannot proceed without an executable liquidity size."""

        decision = evaluate_counterfactual_entry(
            inputs=_entry_inputs(sizing_result=_sizing_result(selected=None)),
            thresholds=_thresholds(),
        )

        self.assertIsInstance(decision, EnterSkipDecision)
        decision = cast("EnterSkipDecision", decision)
        self.assertEqual(decision.action, EntryDecisionAction.SKIP)
        self.assertEqual(decision.reason_codes, ("no_liquidity_size_selected",))

    def test_entry_abstains_when_selected_size_slot_mismatches(self) -> None:
        """Entry cannot mix selected candidate state from another slot."""

        result = evaluate_counterfactual_entry(
            inputs=_entry_inputs(
                sizing_result=_sizing_result(
                    selected=_candidate(quote_amount=50_000, position=5_000, slot=9)
                )
            ),
            thresholds=_thresholds(),
        )

        self.assertIsInstance(result, AbstainResult)
        result = cast("AbstainResult", result)
        self.assertEqual(result.reason, AbstainReason.STALE_STATE)

    def test_entry_abstains_when_selected_q10_mismatches(self) -> None:
        """Entry timing must come from the selected counterfactual candidate."""

        result = evaluate_counterfactual_entry(
            inputs=_entry_inputs(
                sizing_result=_sizing_result(
                    selected=_candidate(quote_amount=50_000, position=5_000)
                ),
                q10_remaining_time_after_entry_ms=2_000,
            ),
            thresholds=_thresholds(),
        )

        self.assertIsInstance(result, AbstainResult)
        result = cast("AbstainResult", result)
        self.assertEqual(result.reason, AbstainReason.STALE_STATE)

    def test_entry_revalidates_loaded_sizing_result(self) -> None:
        """Forged loaded sizing artifacts cannot bypass candidate validation."""

        bool_amount = bool(1)
        result = evaluate_counterfactual_entry(
            inputs=_entry_inputs(
                sizing_result=_sizing_result(
                    selected=_candidate(
                        quote_amount=cast("Any", bool_amount),
                        position=5_000,
                        immediate_loss=cast("Any", 0.5),
                    )
                )
            ),
            thresholds=_thresholds(),
        )

        self.assertIsInstance(result, AbstainResult)
        result = cast("AbstainResult", result)
        self.assertEqual(result.reason, AbstainReason.UNSUPPORTED_PROTOCOL_STATE)

    def test_entry_enforces_loaded_sizing_risk_caps(self) -> None:
        """Loaded sizing artifacts must preserve audited risk caps."""

        result = evaluate_counterfactual_entry(
            inputs=_entry_inputs(
                sizing_result=_sizing_result(
                    selected=_candidate(
                        quote_amount=50_000,
                        position=5_000,
                        hazard=950_000,
                        immediate_loss=900_000,
                    ),
                    p_full_exit_failure=950_000,
                )
            ),
            thresholds=_thresholds(),
        )

        self.assertIsInstance(result, AbstainResult)
        result = cast("AbstainResult", result)
        self.assertEqual(result.reason, AbstainReason.UNSUPPORTED_PROTOCOL_STATE)

    def test_entry_revalidates_sizing_reason_codes(self) -> None:
        """Loaded sizing result reason codes must be immutable string tuples."""

        result = evaluate_counterfactual_entry(
            inputs=_entry_inputs(
                sizing_result=_sizing_result(
                    selected=_candidate(quote_amount=50_000, position=5_000),
                    reason_codes=cast("Any", "selected_liquidity_size"),
                )
            ),
            thresholds=_thresholds(),
        )

        self.assertIsInstance(result, AbstainResult)
        result = cast("AbstainResult", result)
        self.assertEqual(result.reason, AbstainReason.MISSING_FEATURE)

    def test_entry_revalidates_loaded_liquidity_provenance(self) -> None:
        """Loaded sizing artifacts must keep audited liquidity provenance."""

        result = evaluate_counterfactual_entry(
            inputs=_entry_inputs(
                sizing_result=_sizing_result(
                    selected=_candidate(quote_amount=50_000, position=5_000),
                    liquidity_evidence_ids=(),
                )
            ),
            thresholds=_thresholds(),
        )

        self.assertIsInstance(result, AbstainResult)
        result = cast("AbstainResult", result)
        self.assertEqual(result.reason, AbstainReason.MISSING_FEATURE)

    def test_entry_revalidates_loaded_liquidity_versions(self) -> None:
        """Loaded sizing artifacts reject unknown liquidity source versions."""

        result = evaluate_counterfactual_entry(
            inputs=_entry_inputs(
                sizing_result=_sizing_result(
                    selected=_candidate(quote_amount=50_000, position=5_000),
                    reserve_snapshot_version="unknown-reserves",
                )
            ),
            thresholds=_thresholds(),
        )

        self.assertIsInstance(result, AbstainResult)
        result = cast("AbstainResult", result)
        self.assertEqual(result.reason, AbstainReason.DECODER_MISMATCH)

    def test_entry_rejects_selected_size_without_matching_liquidity(self) -> None:
        """Loaded selected sizes must match the audited liquidity position."""

        result = evaluate_counterfactual_entry(
            inputs=_entry_inputs(
                sizing_result=_sizing_result(
                    selected=_candidate(quote_amount=50_000, position=5_000),
                    selected_liquidity_position=4_999,
                )
            ),
            thresholds=_thresholds(),
        )

        self.assertIsInstance(result, AbstainResult)
        result = cast("AbstainResult", result)
        self.assertEqual(result.reason, AbstainReason.UNSUPPORTED_PROTOCOL_STATE)

    def test_entry_revalidates_loaded_volume_mismatch_cap(self) -> None:
        """Loaded sizing artifacts cannot hide volume/liquidity mismatch."""

        result = evaluate_counterfactual_entry(
            inputs=_entry_inputs(
                sizing_result=_sizing_result(
                    selected=_candidate(quote_amount=50_000, position=5_000),
                    volume_mismatch_count=2,
                    max_volume_mismatch=1,
                )
            ),
            thresholds=_thresholds(),
        )

        self.assertIsInstance(result, AbstainResult)
        result = cast("AbstainResult", result)
        self.assertEqual(result.reason, AbstainReason.UNSUPPORTED_PROTOCOL_STATE)

    def test_entry_revalidates_loaded_volume_participation_cap(self) -> None:
        """Loaded sizing artifacts cannot forge independent-volume capacity."""

        result = evaluate_counterfactual_entry(
            inputs=_entry_inputs(
                sizing_result=_sizing_result(
                    selected=_candidate(quote_amount=50_000, position=5_000),
                    independent_recent_volume=100_000,
                    max_volume_participation=200_000,
                    volume_participation_cap=50_000,
                    max_entry_quote=50_000,
                )
            ),
            thresholds=_thresholds(),
        )

        self.assertIsInstance(result, AbstainResult)
        result = cast("AbstainResult", result)
        self.assertEqual(result.reason, AbstainReason.UNSUPPORTED_PROTOCOL_STATE)

    def test_entry_rejects_selected_size_above_volume_participation_cap(self) -> None:
        """Loaded selected size must fit the audited participation cap."""

        result = evaluate_counterfactual_entry(
            inputs=_entry_inputs(
                sizing_result=_sizing_result(
                    selected=_candidate(quote_amount=50_000, position=5_000),
                    independent_recent_volume=100_000,
                    max_volume_participation=200_000,
                    volume_participation_cap=20_000,
                    max_entry_quote=20_000,
                )
            ),
            thresholds=_thresholds(),
        )

        self.assertIsInstance(result, AbstainResult)
        result = cast("AbstainResult", result)
        self.assertEqual(result.reason, AbstainReason.UNSUPPORTED_PROTOCOL_STATE)

    def test_entry_rejects_malformed_sizing_result(self) -> None:
        """Malformed loaded sizing result artifacts fail closed."""

        result = evaluate_counterfactual_entry(
            inputs=_entry_inputs(sizing_result=cast("Any", object())),
            thresholds=_thresholds(),
        )

        self.assertIsInstance(result, AbstainResult)
        result = cast("AbstainResult", result)
        self.assertEqual(result.reason, AbstainReason.UNSUPPORTED_PROTOCOL_STATE)

    def test_entry_rejects_malformed_selected_size(self) -> None:
        """Malformed selected candidate artifacts fail closed."""

        result = evaluate_counterfactual_entry(
            inputs=_entry_inputs(
                sizing_result=_sizing_result(selected=cast("Any", object()))
            ),
            thresholds=_thresholds(),
        )

        self.assertIsInstance(result, AbstainResult)
        result = cast("AbstainResult", result)
        self.assertEqual(result.reason, AbstainReason.UNSUPPORTED_PROTOCOL_STATE)

    def test_entry_rejects_float_sizing_slot(self) -> None:
        """Loaded sizing result slots must be strict integers."""

        result = evaluate_counterfactual_entry(
            inputs=_entry_inputs(
                sizing_result=_sizing_result(
                    selected=_candidate(quote_amount=50_000, position=5_000),
                    as_of_slot=cast("Any", 10.0),
                )
            ),
            thresholds=_thresholds(),
        )

        self.assertIsInstance(result, AbstainResult)
        result = cast("AbstainResult", result)
        self.assertEqual(result.reason, AbstainReason.UNSUPPORTED_PROTOCOL_STATE)

    def test_entry_abstains_when_latency_is_negative(self) -> None:
        """Invalid timing model fields abstain instead of expanding window."""

        result = evaluate_counterfactual_entry(
            inputs=_entry_inputs(
                sizing_result=_sizing_result(
                    selected=_candidate(quote_amount=50_000, position=5_000)
                ),
                p99_entry_latency_ms=-1,
            ),
            thresholds=_thresholds(),
        )

        self.assertIsInstance(result, AbstainResult)
        result = cast("AbstainResult", result)
        self.assertEqual(result.reason, AbstainReason.UNSUPPORTED_PROTOCOL_STATE)


class CounterfactualSourceSafetyTests(unittest.TestCase):
    """Source-level safety checks for entry, sizing, and exit decisions."""

    def test_decision_modules_stay_pure_and_integer_only(self) -> None:
        """Decision logic must not grow adapters, signers, floats, or division."""

        for module_path in DECISION_MODULES:
            with self.subTest(module=str(module_path)):
                source = module_path.read_text(encoding="utf-8")
                tree = ast.parse(source, filename=str(module_path))
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
                    self.assertNotIn(token, source)

    def test_production_entry_callers_use_policy_backed_wrapper(self) -> None:
        """Production code must not call the low-level entry gate directly."""

        low_level_tokens = ("EntryGateInputs", "evaluate_counterfactual_entry")
        allowed_module = Path("src/rugbot/decision/sizing.py")
        violations: list[tuple[str, str]] = []
        for module_path in Path("src").rglob("*.py"):
            if module_path == allowed_module:
                continue
            source = module_path.read_text(encoding="utf-8")
            for token in low_level_tokens:
                if token in source:
                    violations.append((str(module_path), token))

        self.assertEqual(violations, [])


def _candidate(  # noqa: PLR0913
    *,
    quote_amount: int,
    position: int,
    slot: int = 10,
    hazard: int = 50_000,
    q10_ms: int = 3_000,
    immediate_loss: int = 10_000,
) -> CandidateEntrySize:
    return CandidateEntrySize(
        as_of_slot=Slot(slot),
        quote_amount_base_units=QuoteBaseUnits(quote_amount),
        expected_position_base_units=position,
        hazard_after_entry_ppm=hazard,
        q10_remaining_time_after_entry_ms=q10_ms,
        immediate_exit_loss_lamports=Lamports(immediate_loss),
    )


def _candidate_input(  # noqa: PLR0913
    *,
    as_of_slot: int = 10,
    proposed_quote: int,
    entry_input: int | None = None,
    entry_output: int = 5_000,
    immediate_exit_output: int = 42_000,
    p_dump_10s: int = 200_000,
    q10_ms: int = 2_500,
    timing_slot: int = 10,
    timing_after_entry: object | None = None,
    entry_quote: ExecutableQuote | AbstainResult | None = None,
    exit_quote: ExecutableQuote | AbstainResult | None = None,
    simulation_version: str = "counterfactual-v1",
    evidence_ids: tuple[str, ...] = ("counterfactual-evidence",),
) -> CounterfactualEntryCandidateInput:
    entry = (
        entry_quote
        if entry_quote is not None
        else _quote(
            input_amount=entry_input if entry_input is not None else proposed_quote,
            output_amount=entry_output,
        )
    )
    exit_after_entry = (
        exit_quote
        if exit_quote is not None
        else _quote(
            input_amount=entry_output,
            output_amount=immediate_exit_output,
        )
    )
    return CounterfactualEntryCandidateInput(
        as_of_slot=Slot(as_of_slot),
        proposed_quote_amount_base_units=QuoteBaseUnits(proposed_quote),
        entry_quote=entry,
        immediate_exit_quote_after_entry=exit_after_entry,
        timing_after_entry=cast(
            "RugTimingSnapshot",
            timing_after_entry
            if timing_after_entry is not None
            else _timing(
                as_of_slot=timing_slot,
                p_dump_10s=p_dump_10s,
                q10_ms=q10_ms,
            ),
        ),
        simulation_version=simulation_version,
        market_state_snapshot_version="market-v1",
        evidence_ids=evidence_ids,
    )


def _timing(
    *,
    as_of_slot: int = 10,
    p_dump_10s: int = 200_000,
    q10_ms: int = 2_500,
) -> RugTimingSnapshot:
    return RugTimingSnapshot(
        as_of_slot=Slot(as_of_slot),
        timing_model_version="timing-v1",
        p_dump_next_1s_ppm=60_000,
        p_dump_next_3s_ppm=120_000,
        p_dump_next_5s_ppm=160_000,
        p_dump_next_10s_ppm=p_dump_10s,
        q05_remaining_dump_time_ms=1_000,
        q10_remaining_dump_time_ms=q10_ms,
        q50_remaining_dump_time_ms=12_000,
    )


def _decision_bundle(
    *,
    as_of_slot: int = 10,
    matcher: LaunchMatcherSnapshot | None = None,
    selector: RuggerSelectorSnapshot | None = None,
) -> DecisionSnapshotBundle:
    return DecisionSnapshotBundle(
        as_of_slot=Slot(as_of_slot),
        snapshot_bundle_version="bundle-v1",
        feature_snapshot_version="features-v1",
        market_state_snapshot_version="market-v1",
        matcher=matcher if matcher is not None else _entry_matcher(as_of_slot),
        selector=(
            selector
            if selector is not None
            else _entry_selector_with_churn_audit(as_of_slot)
        ),
        timing=_timing(as_of_slot=as_of_slot),
    )


def _decision_policy(*, as_of_slot: int = 10) -> DecisionSnapshotPolicy:
    return DecisionSnapshotPolicy(
        as_of_slot=Slot(as_of_slot),
        policy_version="decision-policy-v1",
        require_selected_operator_churn_audit=True,
        accepted_operator_churn_snapshot_versions=(
            OPERATOR_WALLET_CHURN_SNAPSHOT_VERSION,
        ),
    )


def _entry_artifact_policy(*, as_of_slot: int = 10) -> PolicyBackedEntryArtifactPolicy:
    return PolicyBackedEntryArtifactPolicy(
        as_of_slot=Slot(as_of_slot),
        policy_version="entry-artifacts-v1",
        accepted_decision_policy_versions=("decision-policy-v1",),
        accepted_operator_churn_snapshot_versions=(
            OPERATOR_WALLET_CHURN_SNAPSHOT_VERSION,
        ),
        accepted_snapshot_bundle_versions=("bundle-v1",),
        accepted_feature_snapshot_versions=("features-v1",),
        accepted_market_state_snapshot_versions=("market-v1",),
        accepted_entity_graph_snapshot_versions=("graph-v1",),
        accepted_operator_profile_versions=("profile-v1",),
        accepted_regime_model_versions=("regime-v1",),
        accepted_matcher_versions=("matcher-v1",),
        accepted_selector_versions=("selector-v1",),
        accepted_trigger_generator_versions=("rules-v1",),
        accepted_trigger_feature_schema_versions=("features-v1",),
        accepted_trigger_labeler_versions=("labels-v1",),
        accepted_trigger_row_schema_versions=("rows-v1",),
        accepted_timing_model_versions=("timing-v1",),
        accepted_liquidity_snapshot_versions=("liquidity-snapshot-v1",),
        accepted_liquidity_source_artifact_versions=("full-exit-liquidity-stress-v1",),
        accepted_quote_engine_versions=("quote-v1",),
        accepted_simulator_versions=("simulator-v1",),
        accepted_sizing_market_snapshot_versions=("market-v1",),
        accepted_reserve_snapshot_versions=("reserves-v1",),
        accepted_fee_config_versions=("fees-v1",),
        accepted_volume_classifier_versions=("volume-v1",),
        accepted_latency_snapshot_versions=("latency-v1",),
        accepted_edge_model_versions=("edge-v1",),
        accepted_threshold_policy_versions=("entry-thresholds-v1",),
    )


def _entry_latency_snapshot(  # noqa: PLR0913
    *,
    as_of_slot: int = 10,
    latency_snapshot_version: str = "latency-v1",
    p99_entry_latency_ms: int = 500,
    p99_exit_latency_ms: int = 700,
    safety_margin_ms: int = 300,
    evidence_ids: tuple[str, ...] = ("latency-evidence",),
) -> EntryLatencySnapshot:
    return EntryLatencySnapshot(
        as_of_slot=Slot(as_of_slot),
        latency_snapshot_version=latency_snapshot_version,
        p99_entry_latency_ms=p99_entry_latency_ms,
        p99_exit_latency_ms=p99_exit_latency_ms,
        safety_margin_ms=safety_margin_ms,
        evidence_ids=evidence_ids,
    )


def _entry_edge_snapshot(
    *,
    as_of_slot: int = 10,
    edge_model_version: str = "edge-v1",
    expected_net_pnl_lcb_lamports: int = 100_000,
    minimum_required_edge_lamports: int = 50_000,
    evidence_ids: tuple[str, ...] = ("edge-evidence",),
) -> EntryEdgeSnapshot:
    return EntryEdgeSnapshot(
        as_of_slot=Slot(as_of_slot),
        edge_model_version=edge_model_version,
        expected_net_pnl_lcb_lamports=Lamports(expected_net_pnl_lcb_lamports),
        minimum_required_edge_lamports=Lamports(minimum_required_edge_lamports),
        evidence_ids=evidence_ids,
    )


def _entry_matcher(as_of_slot: int = 10) -> LaunchMatcherSnapshot:
    return LaunchMatcherSnapshot(
        as_of_slot=Slot(as_of_slot),
        entity_id="entity-1",
        regime_id="regime-1",
        entity_probability_ppm=900_000,
        regime_probability_ppm=850_000,
        entity_graph_snapshot_version="graph-v1",
        operator_profile_version="profile-v1",
        regime_model_version="regime-v1",
        matcher_version="matcher-v1",
    )


def _entry_selector(as_of_slot: int = 10) -> RuggerSelectorSnapshot:
    return RuggerSelectorSnapshot(
        as_of_slot=Slot(as_of_slot),
        selector_version="selector-v1",
        is_selected=True,
        min_entity_probability_ppm=800_000,
        min_regime_probability_ppm=800_000,
        min_trigger_risk_ppm=500_000,
        max_trigger_risk_ppm=600_000,
        min_historical_launches=5,
        historical_launch_count=7,
        trigger_generator_version="rules-v1",
        trigger_feature_schema_version="features-v1",
        trigger_labeler_version="labels-v1",
        trigger_row_schema_version="rows-v1",
        trigger_market_state_snapshot_version="market-v1",
        trigger_operator_profile_version="profile-v1",
        trigger_regime_model_version="regime-v1",
        reason_codes=("selector_passed",),
    )


def _entry_selector_with_churn_audit(
    as_of_slot: int = 10,
) -> RuggerSelectorSnapshot:
    return replace(
        _entry_selector(as_of_slot),
        operator_churn_snapshot_version=OPERATOR_WALLET_CHURN_SNAPSHOT_VERSION,
        max_operator_churn_new_high_risk_roles=1,
        observed_operator_churn_new_high_risk_roles=0,
        max_operator_churn_address_turnover_ppm=500_000,
        observed_operator_churn_address_turnover_ppm=0,
        max_operator_churn_retained_role_changes=1,
        observed_operator_churn_retained_role_changes=0,
    )


def _quote(
    *,
    as_of_slot: int = 10,
    input_amount: int,
    output_amount: int = 5_000,
    decoder_version: str = "decoder-v1",
) -> ExecutableQuote:
    return ExecutableQuote(
        path=QuotePath.PUMP_BONDING_CURVE,
        as_of_slot=Slot(as_of_slot),
        input_amount_base_units=input_amount,
        output_amount_base_units=output_amount,
        fee_amount_base_units=1_000,
        base_decimals=6,
        quote_decimals=9,
        fee_config_version="fee-v1",
        decoder_version=decoder_version,
        idl_hash="idl-hash",
        program_config_version="program-config-v1",
    )


def _liquidity(  # noqa: PLR0913
    *,
    position: int,
    max_exit_position: int,
    as_of_slot: int = 10,
    data_end_slot: int | None = None,
    p_failure: int = 20_000,
    independent_recent_volume: int = 1_000_000,
    volume_mismatch_count: int = 0,
    liquidity_snapshot_version: str = "liquidity-snapshot-v1",
    source_artifact_version: str = "full-exit-liquidity-stress-v1",
    quote_engine_version: str = "quote-v1",
    simulator_version: str = "simulator-v1",
    market_snapshot_version: str = "market-v1",
    reserve_snapshot_version: str = "reserves-v1",
    fee_config_version: str = "fees-v1",
    volume_classifier_version: str = "volume-v1",
    evidence_ids: tuple[str, ...] = ("liquidity-evidence",),
    reason_codes: tuple[str, ...] = ("liquidity_snapshot_built",),
) -> LiquiditySnapshot:
    return LiquiditySnapshot(
        as_of_slot=Slot(as_of_slot),
        data_start_slot=Slot(1),
        data_end_slot=Slot(data_end_slot if data_end_slot is not None else as_of_slot),
        liquidity_snapshot_version=liquidity_snapshot_version,
        source_artifact_version=source_artifact_version,
        selected_full_position_base_units=position,
        max_one_shot_exit_size_base_units=max_exit_position,
        current_full_exit_output_base_units=QuoteBaseUnits(100_000),
        stressed_full_exit_output_base_units=QuoteBaseUnits(80_000),
        p_full_exit_failure_ppm=p_failure,
        independent_recent_volume_quote_base_units=QuoteBaseUnits(
            independent_recent_volume
        ),
        volume_liquidity_mismatch_count=volume_mismatch_count,
        quote_engine_version=quote_engine_version,
        simulator_version=simulator_version,
        market_snapshot_version=market_snapshot_version,
        reserve_snapshot_version=reserve_snapshot_version,
        fee_config_version=fee_config_version,
        volume_classifier_version=volume_classifier_version,
        evidence_ids=evidence_ids,
        reason_codes=reason_codes,
    )


def _liquidity_snapshots(
    *positions: int,
    max_exit_position: int,
) -> tuple[LiquiditySnapshot, ...]:
    return tuple(
        _liquidity(position=position, max_exit_position=max_exit_position)
        for position in positions
    )


def _constraints(
    *,
    as_of_slot: int = 10,
    stressed_cap: int = 500_000,
    max_full_exit_failure: int = 80_000,
    max_volume_participation: int = 1_000_000,
    max_volume_mismatch: int = 0,
) -> SizingConstraints:
    return SizingConstraints(
        as_of_slot=Slot(as_of_slot),
        accepted_liquidity_snapshot_versions=("liquidity-snapshot-v1",),
        accepted_liquidity_source_artifact_versions=("full-exit-liquidity-stress-v1",),
        accepted_quote_engine_versions=("quote-v1",),
        accepted_simulator_versions=("simulator-v1",),
        accepted_market_snapshot_versions=("market-v1",),
        accepted_reserve_snapshot_versions=("reserves-v1",),
        accepted_fee_config_versions=("fees-v1",),
        accepted_volume_classifier_versions=("volume-v1",),
        fixed_cap_quote_base_units=QuoteBaseUnits(500_000),
        bankroll_risk_cap_quote_base_units=QuoteBaseUnits(300_000),
        pool_depth_cap_quote_base_units=QuoteBaseUnits(250_000),
        stressed_exit_cap_quote_base_units=QuoteBaseUnits(stressed_cap),
        max_immediate_exit_loss_lamports=Lamports(50_000),
        max_hazard_after_entry_ppm=100_000,
        max_full_exit_failure_ppm=max_full_exit_failure,
        max_exit_volume_participation_ppm=max_volume_participation,
        max_volume_liquidity_mismatch_count=max_volume_mismatch,
    )


def _sizing_result(  # noqa: PLR0913
    *,
    selected: CandidateEntrySize | None,
    as_of_slot: int = 10,
    p_full_exit_failure: int = 20_000,
    max_hazard_after_entry: int = 100_000,
    max_full_exit_failure: int = 80_000,
    max_immediate_exit_loss: int = 50_000,
    independent_recent_volume: int = 1_000_000,
    volume_participation_cap: int | None = None,
    max_volume_participation: int = 100_000,
    volume_mismatch_count: int = 0,
    max_volume_mismatch: int = 0,
    max_entry_quote: int | None = None,
    liquidity_evidence_ids: tuple[str, ...] = ("liquidity-evidence",),
    liquidity_reason_codes: tuple[str, ...] = ("liquidity_snapshot_built",),
    liquidity_snapshot_version: str = "liquidity-snapshot-v1",
    liquidity_source_artifact_version: str = "full-exit-liquidity-stress-v1",
    reserve_snapshot_version: str = "reserves-v1",
    simulator_version: str = "simulator-v1",
    accepted_simulator_versions: tuple[str, ...] = ("simulator-v1",),
    selected_liquidity_position: int | None = None,
    reason_codes: tuple[str, ...] = ("test",),
) -> LiquiditySizingResult:
    return LiquiditySizingResult(
        as_of_slot=Slot(as_of_slot),
        selected_size=selected,
        liquidity_data_start_slot=Slot(1),
        liquidity_data_end_slot=Slot(as_of_slot),
        liquidity_snapshot_version=liquidity_snapshot_version,
        liquidity_source_artifact_version=liquidity_source_artifact_version,
        accepted_liquidity_snapshot_versions=("liquidity-snapshot-v1",),
        accepted_liquidity_source_artifact_versions=("full-exit-liquidity-stress-v1",),
        accepted_quote_engine_versions=("quote-v1",),
        accepted_simulator_versions=accepted_simulator_versions,
        accepted_market_snapshot_versions=("market-v1",),
        accepted_reserve_snapshot_versions=("reserves-v1",),
        accepted_fee_config_versions=("fees-v1",),
        accepted_volume_classifier_versions=("volume-v1",),
        selected_liquidity_position_base_units=(
            selected_liquidity_position
            if selected_liquidity_position is not None
            else selected.expected_position_base_units
            if isinstance(selected, CandidateEntrySize)
            else 0
        ),
        max_entry_quote_base_units=QuoteBaseUnits(
            max_entry_quote
            if max_entry_quote is not None
            else min(
                500_000,
                300_000,
                250_000,
                100_000,
                volume_participation_cap
                if volume_participation_cap is not None
                else independent_recent_volume * max_volume_participation // 1_000_000,
            )
        ),
        fixed_cap_quote_base_units=QuoteBaseUnits(500_000),
        bankroll_risk_cap_quote_base_units=QuoteBaseUnits(300_000),
        pool_depth_cap_quote_base_units=QuoteBaseUnits(250_000),
        stressed_exit_cap_quote_base_units=QuoteBaseUnits(100_000),
        max_one_shot_exit_size_base_units=10_000,
        current_full_exit_output_base_units=QuoteBaseUnits(100_000),
        stressed_full_exit_output_base_units=QuoteBaseUnits(80_000),
        p_full_exit_failure_ppm=p_full_exit_failure,
        independent_recent_volume_quote_base_units=QuoteBaseUnits(
            independent_recent_volume
        ),
        volume_participation_cap_quote_base_units=QuoteBaseUnits(
            volume_participation_cap
            if volume_participation_cap is not None
            else independent_recent_volume * max_volume_participation // 1_000_000
        ),
        volume_liquidity_mismatch_count=volume_mismatch_count,
        max_hazard_after_entry_ppm=max_hazard_after_entry,
        max_full_exit_failure_ppm=max_full_exit_failure,
        max_exit_volume_participation_ppm=max_volume_participation,
        max_volume_liquidity_mismatch_count=max_volume_mismatch,
        max_immediate_exit_loss_lamports=Lamports(max_immediate_exit_loss),
        quote_engine_version="quote-v1",
        simulator_version=simulator_version,
        market_snapshot_version="market-v1",
        reserve_snapshot_version=reserve_snapshot_version,
        fee_config_version="fees-v1",
        volume_classifier_version="volume-v1",
        liquidity_evidence_ids=liquidity_evidence_ids,
        liquidity_reason_codes=liquidity_reason_codes,
        reason_codes=reason_codes,
    )


def _entry_inputs(
    *,
    sizing_result: LiquiditySizingResult,
    q10_remaining_time_after_entry_ms: int = 3_000,
    p99_entry_latency_ms: int = 500,
) -> EntryGateInputs:
    return EntryGateInputs(
        as_of_slot=Slot(10),
        entity_probability_ppm=900_000,
        regime_probability_ppm=850_000,
        q10_remaining_time_after_entry_ms=q10_remaining_time_after_entry_ms,
        p99_entry_latency_ms=p99_entry_latency_ms,
        p99_exit_latency_ms=700,
        safety_margin_ms=300,
        expected_net_pnl_lcb_lamports=Lamports(100_000),
        minimum_required_edge_lamports=Lamports(50_000),
        sizing_result=sizing_result,
    )


def _policy_entry_inputs(  # noqa: PLR0913
    *,
    bundle: DecisionSnapshotBundle | None = None,
    policy: DecisionSnapshotPolicy | None = None,
    artifact_policy: PolicyBackedEntryArtifactPolicy | None = None,
    latency_snapshot: EntryLatencySnapshot | None = None,
    edge_snapshot: EntryEdgeSnapshot | None = None,
    sizing_result: LiquiditySizingResult | None,
) -> PolicyBackedEntryGateInputs:
    return PolicyBackedEntryGateInputs(
        decision_bundle=bundle if bundle is not None else _decision_bundle(),
        decision_policy=policy if policy is not None else _decision_policy(),
        artifact_policy=(
            artifact_policy if artifact_policy is not None else _entry_artifact_policy()
        ),
        sizing_result=sizing_result,
        latency_snapshot=(
            latency_snapshot
            if latency_snapshot is not None
            else _entry_latency_snapshot()
        ),
        edge_snapshot=edge_snapshot
        if edge_snapshot is not None
        else _entry_edge_snapshot(),
    )


def _policy_thresholds(
    *,
    as_of_slot: int = 10,
    threshold_policy_version: str = "entry-thresholds-v1",
    entity_probability_threshold_ppm: int = 800_000,
    regime_probability_threshold_ppm: int = 800_000,
) -> PolicyBackedEntryThresholds:
    return PolicyBackedEntryThresholds(
        as_of_slot=Slot(as_of_slot),
        threshold_policy_version=threshold_policy_version,
        entity_probability_threshold_ppm=entity_probability_threshold_ppm,
        regime_probability_threshold_ppm=regime_probability_threshold_ppm,
    )


def _thresholds() -> EntryGateThresholds:
    return EntryGateThresholds(
        entity_probability_threshold_ppm=800_000,
        regime_probability_threshold_ppm=800_000,
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
    )


if __name__ == "__main__":
    unittest.main()
