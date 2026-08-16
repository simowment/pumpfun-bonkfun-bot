"""Pure liquidity sizing and counterfactual entry-gate logic."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import cast

from rugbot.decision.snapshots import (
    DecisionSnapshotBundle,
    DecisionSnapshotPolicy,
    RugTimingSnapshot,
    validate_decision_snapshot_bundle_with_policy,
)
from rugbot.domain.amounts import Lamports, QuoteBaseUnits, Slot
from rugbot.domain.decisions import AbstainReason, AbstainResult
from rugbot.domain.quotes import ExecutableQuote, QuotePath
from rugbot.graph.wallet_churn import (
    OPERATOR_WALLET_CHURN_SNAPSHOT_VERSION,
)

PROBABILITY_PPM_DENOMINATOR = 1_000_000
MAX_SUPPORTED_DECIMALS = 18
Q05_PROBABILITY_PPM = 50_000
Q10_PROBABILITY_PPM = 100_000
Q50_PROBABILITY_PPM = 500_000
TRUSTED_ENTRY_ARTIFACT_POLICY_VERSION = "entry-artifacts-v1"
TRUSTED_ENTRY_ARTIFACT_POLICY_ALLOWLISTS: dict[str, tuple[str, ...]] = {
    "accepted_decision_policy_versions": ("decision-policy-v1",),
    "accepted_operator_churn_snapshot_versions": (
        OPERATOR_WALLET_CHURN_SNAPSHOT_VERSION,
    ),
    "accepted_snapshot_bundle_versions": ("bundle-v1",),
    "accepted_feature_snapshot_versions": ("features-v1",),
    "accepted_market_state_snapshot_versions": ("market-v1",),
    "accepted_entity_graph_snapshot_versions": ("graph-v1",),
    "accepted_operator_profile_versions": ("profile-v1",),
    "accepted_regime_model_versions": ("regime-v1",),
    "accepted_matcher_versions": ("matcher-v1",),
    "accepted_selector_versions": ("selector-v1",),
    "accepted_trigger_generator_versions": ("rules-v1",),
    "accepted_trigger_feature_schema_versions": ("features-v1",),
    "accepted_trigger_labeler_versions": ("labels-v1",),
    "accepted_trigger_row_schema_versions": ("rows-v1",),
    "accepted_timing_model_versions": ("timing-v1",),
    "accepted_liquidity_snapshot_versions": ("liquidity-snapshot-v1",),
    "accepted_liquidity_source_artifact_versions": ("full-exit-liquidity-stress-v1",),
    "accepted_quote_engine_versions": ("quote-v1",),
    "accepted_simulator_versions": ("simulator-v1",),
    "accepted_sizing_market_snapshot_versions": ("market-v1",),
    "accepted_reserve_snapshot_versions": ("reserves-v1",),
    "accepted_fee_config_versions": ("fees-v1",),
    "accepted_volume_classifier_versions": ("volume-v1",),
    "accepted_latency_snapshot_versions": ("latency-v1",),
    "accepted_edge_model_versions": ("edge-v1",),
    "accepted_threshold_policy_versions": ("entry-thresholds-v1",),
}


class EntryDecisionAction(Enum):
    """Counterfactual entry decision action."""

    ENTER = "enter"
    SKIP = "skip"


@dataclass(frozen=True, slots=True)
class CandidateEntrySize:
    """Candidate entry size after counterfactual own-buy simulation."""

    as_of_slot: Slot
    quote_amount_base_units: QuoteBaseUnits
    expected_position_base_units: int
    hazard_after_entry_ppm: int
    q10_remaining_time_after_entry_ms: int
    immediate_exit_loss_lamports: Lamports


@dataclass(frozen=True, slots=True)
class CounterfactualEntryCandidateInput:
    """Prequoted state-after-entry evidence for one proposed buy size."""

    as_of_slot: Slot
    proposed_quote_amount_base_units: QuoteBaseUnits
    entry_quote: ExecutableQuote | AbstainResult
    immediate_exit_quote_after_entry: ExecutableQuote | AbstainResult
    timing_after_entry: RugTimingSnapshot
    simulation_version: str
    market_state_snapshot_version: str
    evidence_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class CounterfactualEntrySimulationResult:
    """Candidate entry sizes derived from explicit counterfactual artifacts."""

    as_of_slot: Slot
    simulation_version: str
    market_state_snapshot_version: str
    candidates: tuple[CandidateEntrySize, ...]
    reason_codes: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class LiquiditySnapshot:
    """Executable exit-capacity snapshot for a proposed full position."""

    as_of_slot: Slot
    data_start_slot: Slot
    data_end_slot: Slot
    liquidity_snapshot_version: str
    source_artifact_version: str
    selected_full_position_base_units: int
    max_one_shot_exit_size_base_units: int
    current_full_exit_output_base_units: QuoteBaseUnits
    stressed_full_exit_output_base_units: QuoteBaseUnits
    p_full_exit_failure_ppm: int
    independent_recent_volume_quote_base_units: QuoteBaseUnits
    volume_liquidity_mismatch_count: int
    quote_engine_version: str
    simulator_version: str
    market_snapshot_version: str
    reserve_snapshot_version: str
    fee_config_version: str
    volume_classifier_version: str
    evidence_ids: tuple[str, ...]
    reason_codes: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class SizingConstraints:
    """Entry-size caps and risk limits."""

    as_of_slot: Slot
    accepted_liquidity_snapshot_versions: tuple[str, ...]
    accepted_liquidity_source_artifact_versions: tuple[str, ...]
    accepted_quote_engine_versions: tuple[str, ...]
    accepted_simulator_versions: tuple[str, ...]
    accepted_market_snapshot_versions: tuple[str, ...]
    accepted_reserve_snapshot_versions: tuple[str, ...]
    accepted_fee_config_versions: tuple[str, ...]
    accepted_volume_classifier_versions: tuple[str, ...]
    fixed_cap_quote_base_units: QuoteBaseUnits
    bankroll_risk_cap_quote_base_units: QuoteBaseUnits
    pool_depth_cap_quote_base_units: QuoteBaseUnits
    stressed_exit_cap_quote_base_units: QuoteBaseUnits
    max_immediate_exit_loss_lamports: Lamports
    max_hazard_after_entry_ppm: int
    max_full_exit_failure_ppm: int
    max_exit_volume_participation_ppm: int
    max_volume_liquidity_mismatch_count: int


@dataclass(frozen=True, slots=True)
class LiquiditySizingResult:
    """Liquidity sizing output for an entry gate."""

    as_of_slot: Slot
    selected_size: CandidateEntrySize | None
    liquidity_data_start_slot: Slot
    liquidity_data_end_slot: Slot
    liquidity_snapshot_version: str
    liquidity_source_artifact_version: str
    accepted_liquidity_snapshot_versions: tuple[str, ...]
    accepted_liquidity_source_artifact_versions: tuple[str, ...]
    accepted_quote_engine_versions: tuple[str, ...]
    accepted_simulator_versions: tuple[str, ...]
    accepted_market_snapshot_versions: tuple[str, ...]
    accepted_reserve_snapshot_versions: tuple[str, ...]
    accepted_fee_config_versions: tuple[str, ...]
    accepted_volume_classifier_versions: tuple[str, ...]
    selected_liquidity_position_base_units: int
    max_entry_quote_base_units: QuoteBaseUnits
    fixed_cap_quote_base_units: QuoteBaseUnits
    bankroll_risk_cap_quote_base_units: QuoteBaseUnits
    pool_depth_cap_quote_base_units: QuoteBaseUnits
    stressed_exit_cap_quote_base_units: QuoteBaseUnits
    max_one_shot_exit_size_base_units: int
    current_full_exit_output_base_units: QuoteBaseUnits
    stressed_full_exit_output_base_units: QuoteBaseUnits
    p_full_exit_failure_ppm: int
    independent_recent_volume_quote_base_units: QuoteBaseUnits
    volume_participation_cap_quote_base_units: QuoteBaseUnits
    volume_liquidity_mismatch_count: int
    max_hazard_after_entry_ppm: int
    max_full_exit_failure_ppm: int
    max_exit_volume_participation_ppm: int
    max_volume_liquidity_mismatch_count: int
    max_immediate_exit_loss_lamports: Lamports
    quote_engine_version: str
    simulator_version: str
    market_snapshot_version: str
    reserve_snapshot_version: str
    fee_config_version: str
    volume_classifier_version: str
    liquidity_evidence_ids: tuple[str, ...]
    liquidity_reason_codes: tuple[str, ...]
    reason_codes: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class EntryGateInputs:
    """Inputs for a counterfactual entry decision."""

    as_of_slot: Slot
    entity_probability_ppm: int
    regime_probability_ppm: int
    q10_remaining_time_after_entry_ms: int
    p99_entry_latency_ms: int
    p99_exit_latency_ms: int
    safety_margin_ms: int
    expected_net_pnl_lcb_lamports: Lamports
    minimum_required_edge_lamports: Lamports
    sizing_result: LiquiditySizingResult


@dataclass(frozen=True, slots=True)
class EntryLatencySnapshot:
    """Point-in-time latency artifact for action-facing entry."""

    as_of_slot: Slot
    latency_snapshot_version: str
    p99_entry_latency_ms: int
    p99_exit_latency_ms: int
    safety_margin_ms: int
    evidence_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class EntryEdgeSnapshot:
    """Point-in-time expected edge artifact for action-facing entry."""

    as_of_slot: Slot
    edge_model_version: str
    expected_net_pnl_lcb_lamports: Lamports
    minimum_required_edge_lamports: Lamports
    evidence_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class PolicyBackedEntryThresholds:
    """Point-in-time probability thresholds for action-facing entry."""

    as_of_slot: Slot
    threshold_policy_version: str
    entity_probability_threshold_ppm: int
    regime_probability_threshold_ppm: int


@dataclass(frozen=True, slots=True)
class PolicyBackedEntryArtifactPolicy:
    """Trusted version policy for action-facing entry artifacts."""

    as_of_slot: Slot
    policy_version: str
    accepted_decision_policy_versions: tuple[str, ...]
    accepted_operator_churn_snapshot_versions: tuple[str, ...]
    accepted_snapshot_bundle_versions: tuple[str, ...]
    accepted_feature_snapshot_versions: tuple[str, ...]
    accepted_market_state_snapshot_versions: tuple[str, ...]
    accepted_entity_graph_snapshot_versions: tuple[str, ...]
    accepted_operator_profile_versions: tuple[str, ...]
    accepted_regime_model_versions: tuple[str, ...]
    accepted_matcher_versions: tuple[str, ...]
    accepted_selector_versions: tuple[str, ...]
    accepted_trigger_generator_versions: tuple[str, ...]
    accepted_trigger_feature_schema_versions: tuple[str, ...]
    accepted_trigger_labeler_versions: tuple[str, ...]
    accepted_trigger_row_schema_versions: tuple[str, ...]
    accepted_timing_model_versions: tuple[str, ...]
    accepted_liquidity_snapshot_versions: tuple[str, ...]
    accepted_liquidity_source_artifact_versions: tuple[str, ...]
    accepted_quote_engine_versions: tuple[str, ...]
    accepted_simulator_versions: tuple[str, ...]
    accepted_sizing_market_snapshot_versions: tuple[str, ...]
    accepted_reserve_snapshot_versions: tuple[str, ...]
    accepted_fee_config_versions: tuple[str, ...]
    accepted_volume_classifier_versions: tuple[str, ...]
    accepted_latency_snapshot_versions: tuple[str, ...]
    accepted_edge_model_versions: tuple[str, ...]
    accepted_threshold_policy_versions: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class PolicyBackedEntryGateInputs:
    """Strict action-facing inputs for counterfactual entry decisions."""

    decision_bundle: DecisionSnapshotBundle
    decision_policy: DecisionSnapshotPolicy
    artifact_policy: PolicyBackedEntryArtifactPolicy
    sizing_result: LiquiditySizingResult | None
    latency_snapshot: EntryLatencySnapshot | None
    edge_snapshot: EntryEdgeSnapshot | None


@dataclass(frozen=True, slots=True)
class EntryGateThresholds:
    """Probability thresholds for entry."""

    entity_probability_threshold_ppm: int
    regime_probability_threshold_ppm: int


@dataclass(frozen=True, slots=True)
class EnterSkipDecision:
    """Counterfactual entry decision."""

    action: EntryDecisionAction
    as_of_slot: Slot
    selected_size: CandidateEntrySize | None
    reason_codes: tuple[str, ...]


def select_liquidity_size(
    *,
    candidates: tuple[CandidateEntrySize, ...],
    liquidity_snapshots: tuple[LiquiditySnapshot, ...],
    constraints: SizingConstraints,
) -> LiquiditySizingResult | AbstainResult:
    """Choose the largest candidate that satisfies executable exit constraints."""

    slot_error = _validate_sizing_slots(constraints)
    if slot_error is not None:
        return slot_error
    constraints_error = _validate_sizing_constraints(constraints)
    if constraints_error is not None:
        return constraints_error
    if not candidates:
        return _abstain(
            reason=AbstainReason.MISSING_FEATURE,
            message="candidate entry sizes are required",
            as_of_slot=constraints.as_of_slot,
        )
    candidate_error = _validate_candidates(candidates, constraints.as_of_slot)
    if candidate_error is not None:
        return candidate_error

    liquidity_error = _validate_liquidity_snapshots(
        liquidity_snapshots, candidates, constraints
    )
    if liquidity_error is not None:
        return liquidity_error
    liquidity_by_position = _liquidity_by_position(liquidity_snapshots)
    global_cap = _minimum_quote_cap(constraints)
    viable = tuple(
        candidate
        for candidate in candidates
        if _candidate_is_viable(
            candidate=candidate,
            global_cap=global_cap,
            liquidity=liquidity_by_position[candidate.expected_position_base_units],
            constraints=constraints,
        )
    )
    selected = _largest_viable_candidate(viable)
    reason_codes = (
        ("selected_liquidity_size",)
        if selected is not None
        else (
            _no_liquidity_selection_reason(
                candidates, liquidity_by_position, constraints, global_cap
            ),
        )
    )
    selected_liquidity = (
        liquidity_by_position[selected.expected_position_base_units]
        if selected is not None
        else _aggregate_liquidity_snapshot(liquidity_snapshots, constraints)
    )
    selected_cap = _candidate_quote_cap(
        global_cap=global_cap,
        liquidity=selected_liquidity,
        constraints=constraints,
    )

    return LiquiditySizingResult(
        as_of_slot=constraints.as_of_slot,
        selected_size=selected,
        liquidity_data_start_slot=selected_liquidity.data_start_slot,
        liquidity_data_end_slot=selected_liquidity.data_end_slot,
        liquidity_snapshot_version=selected_liquidity.liquidity_snapshot_version,
        liquidity_source_artifact_version=selected_liquidity.source_artifact_version,
        accepted_liquidity_snapshot_versions=(
            constraints.accepted_liquidity_snapshot_versions
        ),
        accepted_liquidity_source_artifact_versions=(
            constraints.accepted_liquidity_source_artifact_versions
        ),
        accepted_quote_engine_versions=constraints.accepted_quote_engine_versions,
        accepted_simulator_versions=constraints.accepted_simulator_versions,
        accepted_market_snapshot_versions=constraints.accepted_market_snapshot_versions,
        accepted_reserve_snapshot_versions=(
            constraints.accepted_reserve_snapshot_versions
        ),
        accepted_fee_config_versions=constraints.accepted_fee_config_versions,
        accepted_volume_classifier_versions=(
            constraints.accepted_volume_classifier_versions
        ),
        selected_liquidity_position_base_units=(
            selected.expected_position_base_units if selected is not None else 0
        ),
        max_entry_quote_base_units=selected_cap,
        fixed_cap_quote_base_units=constraints.fixed_cap_quote_base_units,
        bankroll_risk_cap_quote_base_units=(
            constraints.bankroll_risk_cap_quote_base_units
        ),
        pool_depth_cap_quote_base_units=constraints.pool_depth_cap_quote_base_units,
        stressed_exit_cap_quote_base_units=constraints.stressed_exit_cap_quote_base_units,
        max_one_shot_exit_size_base_units=(
            selected_liquidity.max_one_shot_exit_size_base_units
        ),
        current_full_exit_output_base_units=selected_liquidity.current_full_exit_output_base_units,
        stressed_full_exit_output_base_units=(
            selected_liquidity.stressed_full_exit_output_base_units
        ),
        p_full_exit_failure_ppm=selected_liquidity.p_full_exit_failure_ppm,
        independent_recent_volume_quote_base_units=(
            selected_liquidity.independent_recent_volume_quote_base_units
        ),
        volume_participation_cap_quote_base_units=_volume_participation_cap(
            selected_liquidity,
            constraints,
        ),
        volume_liquidity_mismatch_count=(
            selected_liquidity.volume_liquidity_mismatch_count
        ),
        max_hazard_after_entry_ppm=constraints.max_hazard_after_entry_ppm,
        max_full_exit_failure_ppm=constraints.max_full_exit_failure_ppm,
        max_exit_volume_participation_ppm=(
            constraints.max_exit_volume_participation_ppm
        ),
        max_volume_liquidity_mismatch_count=(
            constraints.max_volume_liquidity_mismatch_count
        ),
        max_immediate_exit_loss_lamports=constraints.max_immediate_exit_loss_lamports,
        quote_engine_version=selected_liquidity.quote_engine_version,
        simulator_version=selected_liquidity.simulator_version,
        market_snapshot_version=selected_liquidity.market_snapshot_version,
        reserve_snapshot_version=selected_liquidity.reserve_snapshot_version,
        fee_config_version=selected_liquidity.fee_config_version,
        volume_classifier_version=selected_liquidity.volume_classifier_version,
        liquidity_evidence_ids=_combined_liquidity_evidence_ids(liquidity_snapshots),
        liquidity_reason_codes=_combined_liquidity_reason_codes(liquidity_snapshots),
        reason_codes=reason_codes,
    )


def _validate_sizing_slots(
    constraints: SizingConstraints,
) -> AbstainResult | None:
    if not _non_negative_int(constraints.as_of_slot):
        return _unsupported(
            "sizing constraints as_of_slot must be non-negative", Slot(-1)
        )
    return None


def simulate_counterfactual_entry_candidates(
    *,
    candidate_inputs: tuple[CounterfactualEntryCandidateInput, ...],
) -> CounterfactualEntrySimulationResult | AbstainResult:
    """Build candidate sizes from explicit state-after-entry quote evidence."""

    validation_error = _validate_counterfactual_inputs(candidate_inputs)
    if validation_error is not None:
        return validation_error
    first_input = candidate_inputs[0]
    return CounterfactualEntrySimulationResult(
        as_of_slot=first_input.as_of_slot,
        simulation_version=first_input.simulation_version,
        market_state_snapshot_version=first_input.market_state_snapshot_version,
        candidates=tuple(
            _build_candidate_from_counterfactual(candidate_input)
            for candidate_input in candidate_inputs
        ),
        reason_codes=("counterfactual_entry_candidates_simulated",),
    )


def evaluate_counterfactual_entry(
    *,
    inputs: EntryGateInputs,
    thresholds: EntryGateThresholds,
) -> EnterSkipDecision | AbstainResult:
    """Evaluate whether a candidate entry remains safe after our own buy."""

    abstention = _validate_entry_inputs(inputs, thresholds)
    if abstention is not None:
        return abstention

    skip_reason = _entry_skip_reason(inputs, thresholds)
    if skip_reason is not None:
        return _skip(inputs, skip_reason)

    return EnterSkipDecision(
        action=EntryDecisionAction.ENTER,
        as_of_slot=inputs.as_of_slot,
        selected_size=inputs.sizing_result.selected_size,
        reason_codes=("counterfactual_entry_passed",),
    )


def evaluate_policy_backed_counterfactual_entry(
    *,
    inputs: PolicyBackedEntryGateInputs,
    thresholds: PolicyBackedEntryThresholds,
) -> EnterSkipDecision | AbstainResult:
    """Evaluate entry from a strict loaded decision snapshot policy."""

    shape_error = _validate_policy_backed_entry_input_shape(inputs)
    if shape_error is not None:
        return shape_error
    bundle_result = validate_decision_snapshot_bundle_with_policy(
        bundle=inputs.decision_bundle,
        policy=inputs.decision_policy,
    )
    if isinstance(bundle_result, AbstainResult):
        return bundle_result
    return _evaluate_policy_backed_entry_for_bundle(
        inputs=inputs,
        thresholds=thresholds,
        bundle=bundle_result,
    )


def _evaluate_policy_backed_entry_for_bundle(
    *,
    inputs: PolicyBackedEntryGateInputs,
    thresholds: PolicyBackedEntryThresholds,
    bundle: DecisionSnapshotBundle,
) -> EnterSkipDecision | AbstainResult:
    provenance_error = _validate_policy_backed_bundle_provenance(
        inputs=inputs,
        bundle=bundle,
    )
    if provenance_error is not None:
        return provenance_error
    if not bundle.selector.is_selected:
        return _selector_skip(bundle.as_of_slot)
    policy_error = _validate_policy_backed_selected_entry_policy(
        inputs=inputs,
        thresholds=thresholds,
        bundle=bundle,
    )
    if policy_error is not None:
        return policy_error
    if inputs.sizing_result is None:
        return _abstain(
            reason=AbstainReason.MISSING_FEATURE,
            message="sizing result is required for selected entry bundle",
            as_of_slot=bundle.as_of_slot,
        )
    q10_result = _entry_q10_from_loaded_sizing(
        sizing_result=inputs.sizing_result,
        artifact_policy=inputs.artifact_policy,
        as_of_slot=bundle.as_of_slot,
    )
    if isinstance(q10_result, AbstainResult):
        return q10_result

    return evaluate_counterfactual_entry(
        inputs=_entry_inputs_from_policy_bundle(
            inputs=inputs,
            bundle=bundle,
            q10_remaining_time_after_entry_ms=q10_result,
        ),
        thresholds=_entry_gate_thresholds_from_policy(thresholds),
    )


def _candidate_is_viable(
    *,
    candidate: CandidateEntrySize,
    global_cap: QuoteBaseUnits,
    liquidity: LiquiditySnapshot,
    constraints: SizingConstraints,
) -> bool:
    return (
        int(candidate.quote_amount_base_units)
        <= int(
            _candidate_quote_cap(
                global_cap=global_cap,
                liquidity=liquidity,
                constraints=constraints,
            )
        )
        and candidate.expected_position_base_units
        <= liquidity.max_one_shot_exit_size_base_units
        and int(candidate.immediate_exit_loss_lamports)
        <= int(constraints.max_immediate_exit_loss_lamports)
        and candidate.hazard_after_entry_ppm <= constraints.max_hazard_after_entry_ppm
        and liquidity.p_full_exit_failure_ppm <= constraints.max_full_exit_failure_ppm
        and liquidity.volume_liquidity_mismatch_count
        <= constraints.max_volume_liquidity_mismatch_count
    )


def _largest_viable_candidate(
    candidates: tuple[CandidateEntrySize, ...],
) -> CandidateEntrySize | None:
    if not candidates:
        return None
    return max(
        candidates,
        key=lambda candidate: (
            int(candidate.quote_amount_base_units),
            -candidate.hazard_after_entry_ppm,
        ),
    )


def _minimum_quote_cap(constraints: SizingConstraints) -> QuoteBaseUnits:
    return QuoteBaseUnits(
        min(
            int(constraints.fixed_cap_quote_base_units),
            int(constraints.bankroll_risk_cap_quote_base_units),
            int(constraints.pool_depth_cap_quote_base_units),
            int(constraints.stressed_exit_cap_quote_base_units),
        )
    )


def _candidate_quote_cap(
    *,
    global_cap: QuoteBaseUnits,
    liquidity: LiquiditySnapshot,
    constraints: SizingConstraints,
) -> QuoteBaseUnits:
    return QuoteBaseUnits(
        min(
            int(global_cap),
            int(_volume_participation_cap(liquidity, constraints)),
        )
    )


def _volume_participation_cap(
    liquidity: LiquiditySnapshot,
    constraints: SizingConstraints,
) -> QuoteBaseUnits:
    return QuoteBaseUnits(
        int(liquidity.independent_recent_volume_quote_base_units)
        * constraints.max_exit_volume_participation_ppm
        // PROBABILITY_PPM_DENOMINATOR
    )


def _no_liquidity_selection_reason(
    candidates: tuple[CandidateEntrySize, ...],
    liquidity_by_position: dict[int, LiquiditySnapshot],
    constraints: SizingConstraints,
    global_cap: QuoteBaseUnits,
) -> str:
    checks = (
        (
            "candidate_quote_amount_above_cap",
            tuple(
                candidate.quote_amount_base_units > global_cap
                for candidate in candidates
            ),
        ),
        (
            "candidate_quote_amount_above_volume_participation_cap",
            tuple(
                candidate.quote_amount_base_units
                > _volume_participation_cap(
                    liquidity_by_position[candidate.expected_position_base_units],
                    constraints,
                )
                for candidate in candidates
            ),
        ),
        (
            "candidate_position_above_one_shot_exit_capacity",
            tuple(
                candidate.expected_position_base_units
                > liquidity_by_position[
                    candidate.expected_position_base_units
                ].max_one_shot_exit_size_base_units
                for candidate in candidates
            ),
        ),
        (
            "candidate_immediate_loss_above_cap",
            tuple(
                candidate.immediate_exit_loss_lamports
                > constraints.max_immediate_exit_loss_lamports
                for candidate in candidates
            ),
        ),
        (
            "candidate_hazard_above_cap",
            tuple(
                candidate.hazard_after_entry_ppm
                > constraints.max_hazard_after_entry_ppm
                for candidate in candidates
            ),
        ),
        (
            "full_exit_failure_above_cap",
            tuple(
                liquidity_by_position[
                    candidate.expected_position_base_units
                ].p_full_exit_failure_ppm
                > constraints.max_full_exit_failure_ppm
                for candidate in candidates
            ),
        ),
        (
            "volume_liquidity_mismatch_above_cap",
            tuple(
                liquidity_by_position[
                    candidate.expected_position_base_units
                ].volume_liquidity_mismatch_count
                > constraints.max_volume_liquidity_mismatch_count
                for candidate in candidates
            ),
        ),
    )
    for reason, matches in checks:
        if any(matches):
            return reason
    return "no_candidate_within_liquidity_caps"


def _aggregate_liquidity_snapshot(
    liquidity_snapshots: tuple[LiquiditySnapshot, ...],
    constraints: SizingConstraints,
) -> LiquiditySnapshot:
    return LiquiditySnapshot(
        as_of_slot=constraints.as_of_slot,
        data_start_slot=min(
            liquidity.data_start_slot for liquidity in liquidity_snapshots
        ),
        data_end_slot=max(liquidity.data_end_slot for liquidity in liquidity_snapshots),
        liquidity_snapshot_version=liquidity_snapshots[0].liquidity_snapshot_version,
        source_artifact_version=liquidity_snapshots[0].source_artifact_version,
        selected_full_position_base_units=0,
        max_one_shot_exit_size_base_units=min(
            liquidity.max_one_shot_exit_size_base_units
            for liquidity in liquidity_snapshots
        ),
        current_full_exit_output_base_units=QuoteBaseUnits(
            min(
                int(liquidity.current_full_exit_output_base_units)
                for liquidity in liquidity_snapshots
            )
        ),
        stressed_full_exit_output_base_units=QuoteBaseUnits(
            min(
                int(liquidity.stressed_full_exit_output_base_units)
                for liquidity in liquidity_snapshots
            )
        ),
        p_full_exit_failure_ppm=max(
            liquidity.p_full_exit_failure_ppm for liquidity in liquidity_snapshots
        ),
        independent_recent_volume_quote_base_units=QuoteBaseUnits(
            min(
                int(liquidity.independent_recent_volume_quote_base_units)
                for liquidity in liquidity_snapshots
            )
        ),
        volume_liquidity_mismatch_count=max(
            liquidity.volume_liquidity_mismatch_count
            for liquidity in liquidity_snapshots
        ),
        quote_engine_version=liquidity_snapshots[0].quote_engine_version,
        simulator_version=liquidity_snapshots[0].simulator_version,
        market_snapshot_version=liquidity_snapshots[0].market_snapshot_version,
        reserve_snapshot_version=liquidity_snapshots[0].reserve_snapshot_version,
        fee_config_version=liquidity_snapshots[0].fee_config_version,
        volume_classifier_version=liquidity_snapshots[0].volume_classifier_version,
        evidence_ids=_combined_liquidity_evidence_ids(liquidity_snapshots),
        reason_codes=_combined_liquidity_reason_codes(liquidity_snapshots),
    )


def _liquidity_by_position(
    liquidity_snapshots: tuple[LiquiditySnapshot, ...],
) -> dict[int, LiquiditySnapshot]:
    return {
        liquidity.selected_full_position_base_units: liquidity
        for liquidity in liquidity_snapshots
    }


def _combined_liquidity_evidence_ids(
    liquidity_snapshots: tuple[LiquiditySnapshot, ...],
) -> tuple[str, ...]:
    evidence_ids: list[str] = []
    for liquidity in liquidity_snapshots:
        evidence_ids.extend(liquidity.evidence_ids)
    return tuple(dict.fromkeys(evidence_ids))


def _combined_liquidity_reason_codes(
    liquidity_snapshots: tuple[LiquiditySnapshot, ...],
) -> tuple[str, ...]:
    reason_codes: list[str] = []
    for liquidity in liquidity_snapshots:
        reason_codes.extend(liquidity.reason_codes)
    return tuple(dict.fromkeys(reason_codes))


def _validate_counterfactual_inputs(
    candidate_inputs: tuple[CounterfactualEntryCandidateInput, ...],
) -> AbstainResult | None:
    if type(candidate_inputs) is not tuple or not candidate_inputs:
        return _abstain(
            reason=AbstainReason.MISSING_FEATURE,
            message="counterfactual candidate inputs are required",
            as_of_slot=Slot(-1),
        )
    first_input = candidate_inputs[0]
    if not isinstance(first_input, CounterfactualEntryCandidateInput):
        return _unsupported("counterfactual candidate input is malformed", Slot(-1))
    metadata_error = _validate_counterfactual_metadata(first_input)
    if metadata_error is not None:
        return metadata_error
    for candidate_input in candidate_inputs:
        input_error = _validate_counterfactual_candidate_input(
            candidate_input,
            first_input,
        )
        if input_error is not None:
            return input_error
    return None


def _validate_counterfactual_metadata(
    candidate_input: CounterfactualEntryCandidateInput,
) -> AbstainResult | None:
    if not _non_negative_int(candidate_input.as_of_slot):
        return _abstain(
            reason=AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
            message="counterfactual as_of_slot must be non-negative",
            as_of_slot=candidate_input.as_of_slot,
        )
    if not _valid_version(candidate_input.simulation_version):
        return _abstain(
            reason=AbstainReason.DECODER_MISMATCH,
            message="simulation_version is required",
            as_of_slot=candidate_input.as_of_slot,
        )
    if not _valid_version(candidate_input.market_state_snapshot_version):
        return _abstain(
            reason=AbstainReason.DECODER_MISMATCH,
            message="market_state_snapshot_version is required",
            as_of_slot=candidate_input.as_of_slot,
        )
    return None


def _validate_counterfactual_candidate_input(
    candidate_input: CounterfactualEntryCandidateInput,
    first_input: CounterfactualEntryCandidateInput,
) -> AbstainResult | None:
    if not isinstance(candidate_input, CounterfactualEntryCandidateInput):
        return _unsupported(
            "counterfactual candidate input is malformed",
            first_input.as_of_slot,
        )
    if not _non_negative_int(candidate_input.as_of_slot):
        return _unsupported(
            "counterfactual input as_of_slot must be non-negative",
            first_input.as_of_slot,
        )
    consistency_error = _validate_counterfactual_consistency(
        candidate_input,
        first_input,
    )
    if consistency_error is not None:
        return consistency_error
    artifact_error = _validate_counterfactual_artifacts(candidate_input)
    if artifact_error is not None:
        return artifact_error
    return _validate_counterfactual_quotes(candidate_input)


def _validate_counterfactual_consistency(
    candidate_input: CounterfactualEntryCandidateInput,
    first_input: CounterfactualEntryCandidateInput,
) -> AbstainResult | None:
    if candidate_input.as_of_slot != first_input.as_of_slot:
        return _abstain(
            reason=AbstainReason.STALE_STATE,
            message="counterfactual inputs use different slots",
            as_of_slot=first_input.as_of_slot,
        )
    if candidate_input.simulation_version != first_input.simulation_version:
        return _abstain(
            reason=AbstainReason.DECODER_MISMATCH,
            message="counterfactual inputs use different simulation versions",
            as_of_slot=first_input.as_of_slot,
        )
    if candidate_input.market_state_snapshot_version != (
        first_input.market_state_snapshot_version
    ):
        return _abstain(
            reason=AbstainReason.DECODER_MISMATCH,
            message="counterfactual inputs use different market-state versions",
            as_of_slot=first_input.as_of_slot,
        )
    return None


def _validate_counterfactual_artifacts(
    candidate_input: CounterfactualEntryCandidateInput,
) -> AbstainResult | None:
    if not _valid_evidence_ids(candidate_input.evidence_ids):
        return _abstain(
            reason=AbstainReason.MISSING_FEATURE,
            message="counterfactual evidence_ids are required",
            as_of_slot=candidate_input.as_of_slot,
        )
    if not _positive_int(candidate_input.proposed_quote_amount_base_units):
        return _abstain(
            reason=AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
            message="proposed quote amount must be positive",
            as_of_slot=candidate_input.as_of_slot,
        )
    timing_error = _validate_timing_after_entry(
        candidate_input.timing_after_entry,
        candidate_input.as_of_slot,
    )
    if timing_error is not None:
        return timing_error
    if candidate_input.timing_after_entry.as_of_slot != candidate_input.as_of_slot:
        return _abstain(
            reason=AbstainReason.STALE_STATE,
            message="timing_after_entry uses a stale slot",
            as_of_slot=candidate_input.as_of_slot,
        )
    return None


def _validate_counterfactual_quotes(
    candidate_input: CounterfactualEntryCandidateInput,
) -> AbstainResult | None:
    entry_quote = candidate_input.entry_quote
    exit_quote = candidate_input.immediate_exit_quote_after_entry
    if isinstance(entry_quote, AbstainResult):
        return _quote_abstention("entry quote abstained", entry_quote)
    if isinstance(exit_quote, AbstainResult):
        return _quote_abstention("immediate exit quote abstained", exit_quote)
    quote_error = _validate_executable_quote(
        quote=entry_quote,
        expected_slot=candidate_input.as_of_slot,
        expected_input=int(candidate_input.proposed_quote_amount_base_units),
        quote_name="entry_quote",
    )
    if quote_error is not None:
        return quote_error
    quote_error = _validate_executable_quote(
        quote=exit_quote,
        expected_slot=candidate_input.as_of_slot,
        expected_input=entry_quote.output_amount_base_units,
        quote_name="immediate_exit_quote_after_entry",
    )
    if quote_error is not None:
        return quote_error
    return _validate_quote_pair(entry_quote, exit_quote)


def _validate_timing_after_entry(
    timing: object,
    as_of_slot: Slot,
) -> AbstainResult | None:
    shape_error = _validate_timing_shape(timing, as_of_slot)
    if shape_error is not None:
        return shape_error
    timing_snapshot = cast("RugTimingSnapshot", timing)
    version_error = _validate_timing_version(timing_snapshot, as_of_slot)
    if version_error is not None:
        return version_error
    probability_error = _validate_timing_probabilities(timing_snapshot)
    if probability_error is not None:
        return probability_error
    quantile_error = _validate_timing_quantile_order(timing_snapshot)
    if quantile_error is not None:
        return quantile_error
    return _validate_timing_quantile_probability_coherence(timing_snapshot)


def _validate_timing_shape(
    timing: object,
    as_of_slot: Slot,
) -> AbstainResult | None:
    if not _has_timing_snapshot_shape(timing):
        return _unsupported("timing_after_entry is malformed", as_of_slot)
    timing_snapshot = cast("RugTimingSnapshot", timing)
    if not _non_negative_int(timing_snapshot.as_of_slot):
        return _unsupported("timing_after_entry slot is invalid", as_of_slot)
    return None


def _validate_timing_version(
    timing: RugTimingSnapshot,
    as_of_slot: Slot,
) -> AbstainResult | None:
    if not _valid_version(timing.timing_model_version):
        return _abstain(
            reason=AbstainReason.DECODER_MISMATCH,
            message="timing_model_version is required",
            as_of_slot=as_of_slot,
        )
    return None


def _validate_timing_probabilities(
    timing: RugTimingSnapshot,
) -> AbstainResult | None:
    probability_error = _validate_probability_fields(
        timing.as_of_slot,
        {
            "p_dump_next_1s_ppm": timing.p_dump_next_1s_ppm,
            "p_dump_next_3s_ppm": timing.p_dump_next_3s_ppm,
            "p_dump_next_5s_ppm": timing.p_dump_next_5s_ppm,
            "p_dump_next_10s_ppm": timing.p_dump_next_10s_ppm,
        },
    )
    if probability_error is not None:
        return probability_error
    if not (
        timing.p_dump_next_1s_ppm
        <= timing.p_dump_next_3s_ppm
        <= timing.p_dump_next_5s_ppm
        <= timing.p_dump_next_10s_ppm
    ):
        return _abstain(
            reason=AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
            message="timing_after_entry probabilities must be non-decreasing",
            as_of_slot=timing.as_of_slot,
        )
    return None


def _validate_timing_quantile_order(
    timing: RugTimingSnapshot,
) -> AbstainResult | None:
    if not _valid_remaining_time_quantiles(timing):
        return _abstain(
            reason=AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
            message="timing_after_entry quantiles must be non-decreasing",
            as_of_slot=timing.as_of_slot,
        )
    return None


def _validate_timing_quantile_probability_coherence(
    timing: RugTimingSnapshot,
) -> AbstainResult | None:
    for quantile_ms, probability_ppm in (
        (timing.q05_remaining_dump_time_ms, Q05_PROBABILITY_PPM),
        (timing.q10_remaining_dump_time_ms, Q10_PROBABILITY_PPM),
        (timing.q50_remaining_dump_time_ms, Q50_PROBABILITY_PPM),
    ):
        quantile_error = _validate_quantile_probability(
            timing=timing,
            quantile_ms=quantile_ms,
            probability_ppm=probability_ppm,
        )
        if quantile_error is not None:
            return quantile_error
    return None


def _validate_quantile_probability(
    *,
    timing: RugTimingSnapshot,
    quantile_ms: int,
    probability_ppm: int,
) -> AbstainResult | None:
    previous_probability: int | None = None
    for horizon_ms, horizon_probability in _timing_horizon_probabilities(timing):
        if quantile_ms <= horizon_ms:
            if horizon_probability < probability_ppm:
                return _unsupported(
                    "timing quantile is not supported by horizon probability",
                    timing.as_of_slot,
                )
            if (
                previous_probability is not None
                and previous_probability >= probability_ppm
            ):
                return _unsupported(
                    "timing quantile is later than its probability crossing",
                    timing.as_of_slot,
                )
            return None
        previous_probability = horizon_probability
    if previous_probability is not None and previous_probability >= probability_ppm:
        return _unsupported(
            "timing quantile exceeds supported horizon after probability crossing",
            timing.as_of_slot,
        )
    return None


def _timing_horizon_probabilities(
    timing: RugTimingSnapshot,
) -> tuple[tuple[int, int], ...]:
    return (
        (1_000, timing.p_dump_next_1s_ppm),
        (3_000, timing.p_dump_next_3s_ppm),
        (5_000, timing.p_dump_next_5s_ppm),
        (10_000, timing.p_dump_next_10s_ppm),
    )


def _validate_executable_quote(
    *,
    quote: object,
    expected_slot: Slot,
    expected_input: int,
    quote_name: str,
) -> AbstainResult | None:
    shape_error = _validate_quote_shape(
        quote=quote,
        expected_slot=expected_slot,
        quote_name=quote_name,
    )
    if shape_error is not None:
        return shape_error
    executable_quote = cast("ExecutableQuote", quote)
    identity_error = _validate_quote_identity(
        quote=executable_quote,
        expected_slot=expected_slot,
        expected_input=expected_input,
        quote_name=quote_name,
    )
    if identity_error is not None:
        return identity_error
    numeric_error = _validate_quote_numeric_fields(executable_quote, quote_name)
    if numeric_error is not None:
        return numeric_error
    return _validate_quote_versions(executable_quote, quote_name)


def _validate_quote_shape(
    *,
    quote: object,
    expected_slot: Slot,
    quote_name: str,
) -> AbstainResult | None:
    if not isinstance(quote, ExecutableQuote):
        return _quote_unsupported(f"{quote_name} is malformed", expected_slot)
    if not isinstance(quote.path, QuotePath):
        return _quote_unsupported(f"{quote_name} path is invalid", expected_slot)
    if not _non_negative_int(quote.as_of_slot):
        return _quote_unsupported(f"{quote_name} slot is invalid", expected_slot)
    return None


def _validate_quote_identity(
    *,
    quote: ExecutableQuote,
    expected_slot: Slot,
    expected_input: int,
    quote_name: str,
) -> AbstainResult | None:
    if quote.as_of_slot != expected_slot:
        return _abstain(
            reason=AbstainReason.STALE_STATE,
            message=f"{quote_name} uses a stale slot",
            as_of_slot=expected_slot,
        )
    if quote.input_amount_base_units != expected_input:
        return _abstain(
            reason=AbstainReason.MISSING_FEATURE,
            message=f"{quote_name} input does not match counterfactual state",
            as_of_slot=expected_slot,
        )
    return None


def _validate_quote_numeric_fields(
    quote: ExecutableQuote,
    quote_name: str,
) -> AbstainResult | None:
    if not _positive_int(quote.input_amount_base_units):
        return _quote_unsupported(
            f"{quote_name} input must be positive",
            quote.as_of_slot,
        )
    if not _positive_int(quote.output_amount_base_units):
        return _quote_unsupported(
            f"{quote_name} output must be positive",
            quote.as_of_slot,
        )
    if not _non_negative_int(quote.fee_amount_base_units):
        return _quote_unsupported(
            f"{quote_name} fee must be non-negative",
            quote.as_of_slot,
        )
    if not _valid_decimals(quote.base_decimals):
        return _quote_unsupported(
            f"{quote_name} base_decimals are unsupported",
            quote.as_of_slot,
        )
    if not _valid_decimals(quote.quote_decimals):
        return _quote_unsupported(
            f"{quote_name} quote_decimals are unsupported",
            quote.as_of_slot,
        )
    return None


def _validate_quote_versions(
    quote: ExecutableQuote,
    quote_name: str,
) -> AbstainResult | None:
    for field_name, value in {
        "fee_config_version": quote.fee_config_version,
        "decoder_version": quote.decoder_version,
        "idl_hash": quote.idl_hash,
        "program_config_version": quote.program_config_version,
    }.items():
        if not _valid_version(value):
            return _abstain(
                reason=AbstainReason.DECODER_MISMATCH,
                message=f"{quote_name} {field_name} is required",
                as_of_slot=quote.as_of_slot,
            )
    return None


def _validate_quote_pair(
    entry_quote: ExecutableQuote,
    exit_quote: ExecutableQuote,
) -> AbstainResult | None:
    comparable_fields = (
        entry_quote.path == exit_quote.path,
        entry_quote.base_decimals == exit_quote.base_decimals,
        entry_quote.quote_decimals == exit_quote.quote_decimals,
        entry_quote.fee_config_version == exit_quote.fee_config_version,
        entry_quote.decoder_version == exit_quote.decoder_version,
        entry_quote.idl_hash == exit_quote.idl_hash,
        entry_quote.program_config_version == exit_quote.program_config_version,
    )
    if not all(comparable_fields):
        return _abstain(
            reason=AbstainReason.DECODER_MISMATCH,
            message="entry and immediate-exit quote provenance mismatch",
            as_of_slot=entry_quote.as_of_slot,
        )
    return None


def _build_candidate_from_counterfactual(
    candidate_input: CounterfactualEntryCandidateInput,
) -> CandidateEntrySize:
    entry_quote = cast("ExecutableQuote", candidate_input.entry_quote)
    exit_quote = cast(
        "ExecutableQuote",
        candidate_input.immediate_exit_quote_after_entry,
    )
    immediate_loss = max(
        0,
        entry_quote.input_amount_base_units - exit_quote.output_amount_base_units,
    )
    return CandidateEntrySize(
        as_of_slot=candidate_input.as_of_slot,
        quote_amount_base_units=candidate_input.proposed_quote_amount_base_units,
        expected_position_base_units=entry_quote.output_amount_base_units,
        hazard_after_entry_ppm=candidate_input.timing_after_entry.p_dump_next_10s_ppm,
        q10_remaining_time_after_entry_ms=(
            candidate_input.timing_after_entry.q10_remaining_dump_time_ms
        ),
        immediate_exit_loss_lamports=Lamports(immediate_loss),
    )


def _validate_sizing_constraints(
    constraints: SizingConstraints,
) -> AbstainResult | None:
    version_error = _validate_constraint_versions(constraints)
    if version_error is not None:
        return version_error
    probability_error = _validate_probability_fields(
        constraints.as_of_slot,
        {
            "max_hazard_after_entry_ppm": constraints.max_hazard_after_entry_ppm,
            "max_full_exit_failure_ppm": constraints.max_full_exit_failure_ppm,
            "max_exit_volume_participation_ppm": (
                constraints.max_exit_volume_participation_ppm
            ),
        },
    )
    if probability_error is not None:
        return probability_error
    return _validate_constraint_amounts(constraints)


def _validate_constraint_versions(
    constraints: SizingConstraints,
) -> AbstainResult | None:
    version_fields = {
        "accepted_liquidity_snapshot_versions": (
            constraints.accepted_liquidity_snapshot_versions
        ),
        "accepted_liquidity_source_artifact_versions": (
            constraints.accepted_liquidity_source_artifact_versions
        ),
        "accepted_quote_engine_versions": constraints.accepted_quote_engine_versions,
        "accepted_simulator_versions": constraints.accepted_simulator_versions,
        "accepted_market_snapshot_versions": (
            constraints.accepted_market_snapshot_versions
        ),
        "accepted_reserve_snapshot_versions": (
            constraints.accepted_reserve_snapshot_versions
        ),
        "accepted_fee_config_versions": constraints.accepted_fee_config_versions,
        "accepted_volume_classifier_versions": (
            constraints.accepted_volume_classifier_versions
        ),
    }
    for field_name, value in version_fields.items():
        if not _valid_str_tuple(value):
            return _abstain(
                reason=AbstainReason.DECODER_MISMATCH,
                message=f"{field_name} is required",
                as_of_slot=constraints.as_of_slot,
            )
    return None


def _validate_constraint_amounts(
    constraints: SizingConstraints,
) -> AbstainResult | None:
    positive_fields = {
        "fixed_cap_quote_base_units": constraints.fixed_cap_quote_base_units,
        "bankroll_risk_cap_quote_base_units": (
            constraints.bankroll_risk_cap_quote_base_units
        ),
        "pool_depth_cap_quote_base_units": constraints.pool_depth_cap_quote_base_units,
        "stressed_exit_cap_quote_base_units": (
            constraints.stressed_exit_cap_quote_base_units
        ),
    }
    for field_name, value in positive_fields.items():
        if not _positive_int(value):
            return _unsupported(
                f"{field_name} must be positive", constraints.as_of_slot
            )
    if not _non_negative_int(constraints.max_immediate_exit_loss_lamports):
        return _unsupported(
            "max_immediate_exit_loss_lamports must be non-negative",
            constraints.as_of_slot,
        )
    if not _non_negative_int(constraints.max_volume_liquidity_mismatch_count):
        return _unsupported(
            "max_volume_liquidity_mismatch_count must be non-negative",
            constraints.as_of_slot,
        )
    if _minimum_quote_cap(constraints) <= 0:
        return _unsupported("entry size caps must be positive", constraints.as_of_slot)
    return None


def _validate_liquidity_snapshots(
    liquidity_snapshots: tuple[LiquiditySnapshot, ...],
    candidates: tuple[CandidateEntrySize, ...],
    constraints: SizingConstraints,
) -> AbstainResult | None:
    if type(liquidity_snapshots) is not tuple or not liquidity_snapshots:
        return _abstain(
            reason=AbstainReason.MISSING_FEATURE,
            message="liquidity snapshots are required",
            as_of_slot=constraints.as_of_slot,
        )
    position_counts: dict[int, int] = {}
    for liquidity in liquidity_snapshots:
        snapshot_error = _validate_liquidity_snapshot(liquidity, constraints)
        if snapshot_error is not None:
            return snapshot_error
        position_counts[liquidity.selected_full_position_base_units] = (
            position_counts.get(liquidity.selected_full_position_base_units, 0) + 1
        )
    duplicate_positions = tuple(
        position for position, count in position_counts.items() if count > 1
    )
    if duplicate_positions:
        return _unsupported(
            "duplicate liquidity snapshot position", constraints.as_of_slot
        )
    return _validate_liquidity_candidate_coverage(
        liquidity_snapshots, candidates, constraints
    )


def _validate_liquidity_snapshot(
    liquidity: object,
    constraints: SizingConstraints,
) -> AbstainResult | None:
    if not isinstance(liquidity, LiquiditySnapshot):
        return _unsupported("liquidity snapshot is malformed", constraints.as_of_slot)
    slot_error = _validate_liquidity_snapshot_slots(liquidity, constraints)
    if slot_error is not None:
        return slot_error
    identity_error = _validate_liquidity_snapshot_identity(liquidity, constraints)
    if identity_error is not None:
        return identity_error
    version_error = _validate_liquidity_snapshot_versions(liquidity, constraints)
    if version_error is not None:
        return version_error
    amount_error = _validate_liquidity_snapshot_amounts(liquidity, constraints)
    if amount_error is not None:
        return amount_error
    return _validate_liquidity_snapshot_probabilities(liquidity, constraints)


def _validate_liquidity_snapshot_slots(
    liquidity: LiquiditySnapshot,
    constraints: SizingConstraints,
) -> AbstainResult | None:
    slot_fields = (
        liquidity.as_of_slot,
        liquidity.data_start_slot,
        liquidity.data_end_slot,
    )
    if not all(_non_negative_int(value) for value in slot_fields):
        return _unsupported(
            "liquidity snapshot slot is invalid", constraints.as_of_slot
        )
    if not (
        liquidity.data_start_slot
        <= liquidity.data_end_slot
        <= liquidity.as_of_slot
        == constraints.as_of_slot
    ):
        return _abstain(
            reason=AbstainReason.STALE_STATE,
            message="liquidity snapshot interval is stale",
            as_of_slot=constraints.as_of_slot,
        )
    if liquidity.data_end_slot != liquidity.as_of_slot:
        return _abstain(
            reason=AbstainReason.STALE_STATE,
            message="liquidity snapshot is not decision-current",
            as_of_slot=constraints.as_of_slot,
        )
    return None


def _validate_liquidity_snapshot_identity(
    liquidity: LiquiditySnapshot,
    constraints: SizingConstraints,
) -> AbstainResult | None:
    if not _valid_evidence_ids(liquidity.evidence_ids):
        return _abstain(
            reason=AbstainReason.MISSING_FEATURE,
            message="liquidity snapshot evidence_ids are required",
            as_of_slot=constraints.as_of_slot,
        )
    if not _valid_reason_codes(liquidity.reason_codes):
        return _abstain(
            reason=AbstainReason.MISSING_FEATURE,
            message="liquidity snapshot reason_codes are required",
            as_of_slot=constraints.as_of_slot,
        )
    if not _positive_int(liquidity.selected_full_position_base_units):
        return _unsupported(
            "liquidity selected full position must be positive",
            constraints.as_of_slot,
        )
    return None


def _validate_liquidity_snapshot_versions(
    liquidity: LiquiditySnapshot,
    constraints: SizingConstraints,
) -> AbstainResult | None:
    version_checks = (
        (
            liquidity.liquidity_snapshot_version,
            constraints.accepted_liquidity_snapshot_versions,
            "liquidity snapshot",
        ),
        (
            liquidity.source_artifact_version,
            constraints.accepted_liquidity_source_artifact_versions,
            "liquidity source artifact",
        ),
        (
            liquidity.quote_engine_version,
            constraints.accepted_quote_engine_versions,
            "quote engine",
        ),
        (
            liquidity.simulator_version,
            constraints.accepted_simulator_versions,
            "simulator",
        ),
        (
            liquidity.market_snapshot_version,
            constraints.accepted_market_snapshot_versions,
            "market snapshot",
        ),
        (
            liquidity.reserve_snapshot_version,
            constraints.accepted_reserve_snapshot_versions,
            "reserve snapshot",
        ),
        (liquidity.fee_config_version, constraints.accepted_fee_config_versions, "fee"),
        (
            liquidity.volume_classifier_version,
            constraints.accepted_volume_classifier_versions,
            "volume classifier",
        ),
    )
    for version, accepted_versions, label in version_checks:
        if not _valid_version(version):
            return _abstain(
                reason=AbstainReason.DECODER_MISMATCH,
                message=f"{label} version is required",
                as_of_slot=constraints.as_of_slot,
            )
        if version not in accepted_versions:
            return _abstain(
                reason=AbstainReason.DECODER_MISMATCH,
                message=f"{label} version is unknown",
                as_of_slot=constraints.as_of_slot,
            )
    return None


def _validate_liquidity_snapshot_amounts(
    liquidity: LiquiditySnapshot,
    constraints: SizingConstraints,
) -> AbstainResult | None:
    positive_fields = {
        "max_one_shot_exit_size_base_units": (
            liquidity.max_one_shot_exit_size_base_units
        ),
        "current_full_exit_output_base_units": (
            liquidity.current_full_exit_output_base_units
        ),
        "stressed_full_exit_output_base_units": (
            liquidity.stressed_full_exit_output_base_units
        ),
    }
    for field_name, value in positive_fields.items():
        if not _positive_int(value):
            return _unsupported(
                f"{field_name} must be positive", constraints.as_of_slot
            )
    if not _non_negative_int(liquidity.volume_liquidity_mismatch_count):
        return _unsupported(
            "volume_liquidity_mismatch_count must be non-negative",
            constraints.as_of_slot,
        )
    if not _non_negative_int(liquidity.independent_recent_volume_quote_base_units):
        return _unsupported(
            "independent_recent_volume_quote_base_units must be non-negative",
            constraints.as_of_slot,
        )
    return None


def _validate_liquidity_snapshot_probabilities(
    liquidity: LiquiditySnapshot,
    constraints: SizingConstraints,
) -> AbstainResult | None:
    return _validate_probability_fields(
        constraints.as_of_slot,
        {"p_full_exit_failure_ppm": liquidity.p_full_exit_failure_ppm},
    )


def _validate_liquidity_candidate_coverage(
    liquidity_snapshots: tuple[LiquiditySnapshot, ...],
    candidates: tuple[CandidateEntrySize, ...],
    constraints: SizingConstraints,
) -> AbstainResult | None:
    liquidity_positions = {
        liquidity.selected_full_position_base_units for liquidity in liquidity_snapshots
    }
    candidate_positions = {
        candidate.expected_position_base_units for candidate in candidates
    }
    if liquidity_positions != candidate_positions:
        return _abstain(
            reason=AbstainReason.MISSING_FEATURE,
            message="liquidity snapshots must match candidate positions",
            as_of_slot=constraints.as_of_slot,
        )
    return None


def _validate_candidates(
    candidates: tuple[CandidateEntrySize, ...],
    as_of_slot: Slot,
) -> AbstainResult | None:
    for candidate in candidates:
        candidate_error = _validate_candidate(candidate, as_of_slot)
        if candidate_error is not None:
            return candidate_error
    return None


def _validate_candidate(
    candidate: CandidateEntrySize,
    as_of_slot: Slot,
) -> AbstainResult | None:
    slot_error = _validate_candidate_slot(candidate, as_of_slot)
    if slot_error is not None:
        return slot_error
    if not _valid_probability_ppm(candidate.hazard_after_entry_ppm):
        return _invalid_probability("hazard_after_entry_ppm", as_of_slot)
    return _validate_candidate_numeric_fields(candidate, as_of_slot)


def _validate_candidate_slot(
    candidate: CandidateEntrySize,
    as_of_slot: Slot,
) -> AbstainResult | None:
    if not _non_negative_int(candidate.as_of_slot):
        return _unsupported("candidate as_of_slot must be non-negative", as_of_slot)
    if candidate.as_of_slot != as_of_slot:
        return _abstain(
            reason=AbstainReason.STALE_STATE,
            message="candidate entry size uses a stale slot",
            as_of_slot=as_of_slot,
        )
    return None


def _validate_candidate_numeric_fields(
    candidate: CandidateEntrySize,
    as_of_slot: Slot,
) -> AbstainResult | None:
    if not _positive_int(candidate.quote_amount_base_units):
        return _abstain(
            reason=AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
            message="candidate quote amount must be positive",
            as_of_slot=as_of_slot,
        )
    if not _positive_int(candidate.expected_position_base_units):
        return _abstain(
            reason=AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
            message="candidate expected position must be positive",
            as_of_slot=as_of_slot,
        )
    if not _positive_int(candidate.q10_remaining_time_after_entry_ms):
        return _abstain(
            reason=AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
            message="candidate q10 remaining time must be positive",
            as_of_slot=as_of_slot,
        )
    if not _non_negative_int(candidate.immediate_exit_loss_lamports):
        return _abstain(
            reason=AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
            message="candidate immediate exit loss must be non-negative",
            as_of_slot=as_of_slot,
        )
    return None


def _validate_entry_probabilities(
    inputs: EntryGateInputs,
    thresholds: EntryGateThresholds,
) -> AbstainResult | None:
    probability_fields = {
        "entity_probability_ppm": inputs.entity_probability_ppm,
        "regime_probability_ppm": inputs.regime_probability_ppm,
        "entity_probability_threshold_ppm": thresholds.entity_probability_threshold_ppm,
        "regime_probability_threshold_ppm": thresholds.regime_probability_threshold_ppm,
    }
    for field_name, value in probability_fields.items():
        if not _valid_probability_ppm(value):
            return _invalid_probability(field_name, inputs.as_of_slot)
    return None


def _validate_entry_inputs(
    inputs: EntryGateInputs,
    thresholds: EntryGateThresholds,
) -> AbstainResult | None:
    shape_error = _validate_entry_input_shape(inputs)
    if shape_error is not None:
        return shape_error
    sizing_error = _validate_loaded_sizing_result(inputs.sizing_result)
    if sizing_error is not None:
        return sizing_error
    selected_error = _validate_entry_selected_consistency(inputs)
    if selected_error is not None:
        return selected_error
    probability_error = _validate_entry_probabilities(inputs, thresholds)
    if probability_error is not None:
        return probability_error
    return _validate_entry_numeric_fields(inputs)


def _validate_policy_backed_entry_input_shape(
    inputs: PolicyBackedEntryGateInputs,
) -> AbstainResult | None:
    if not isinstance(inputs, PolicyBackedEntryGateInputs):
        return _unsupported("policy-backed entry inputs are malformed", Slot(-1))
    if not isinstance(inputs.decision_bundle, DecisionSnapshotBundle):
        return _unsupported("decision snapshot bundle is malformed", Slot(-1))
    if not isinstance(inputs.artifact_policy, PolicyBackedEntryArtifactPolicy):
        return _unsupported("entry artifact policy is malformed", Slot(-1))
    return None


def _validate_policy_backed_bundle_provenance(
    *,
    inputs: PolicyBackedEntryGateInputs,
    bundle: DecisionSnapshotBundle,
) -> AbstainResult | None:
    policy_error = _validate_entry_artifact_policy_header(
        artifact_policy=inputs.artifact_policy,
        as_of_slot=bundle.as_of_slot,
    )
    if policy_error is not None:
        return policy_error
    decision_policy_error = _validate_decision_policy_against_artifact_policy(
        inputs=inputs,
        bundle=bundle,
    )
    if decision_policy_error is not None:
        return decision_policy_error
    return _validate_bundle_against_entry_artifact_policy(
        artifact_policy=inputs.artifact_policy,
        bundle=bundle,
    )


def _validate_policy_backed_selected_entry_policy(
    *,
    inputs: PolicyBackedEntryGateInputs,
    thresholds: PolicyBackedEntryThresholds,
    bundle: DecisionSnapshotBundle,
) -> AbstainResult | None:
    if not isinstance(thresholds, PolicyBackedEntryThresholds):
        return _unsupported("entry threshold policy is malformed", bundle.as_of_slot)
    if (
        bundle.selector.is_selected
        and not inputs.decision_policy.require_selected_operator_churn_audit
    ):
        return _unsupported(
            "entry action policy must require selected operator churn audit",
            bundle.as_of_slot,
        )
    return _validate_entry_action_artifacts(
        inputs=inputs,
        thresholds=thresholds,
        artifact_policy=inputs.artifact_policy,
        as_of_slot=bundle.as_of_slot,
    )


def _validate_entry_artifact_policy_header(
    *,
    artifact_policy: PolicyBackedEntryArtifactPolicy,
    as_of_slot: Slot,
) -> AbstainResult | None:
    if not _non_negative_int(artifact_policy.as_of_slot):
        return _unsupported("entry artifact policy as_of_slot is invalid", Slot(-1))
    if artifact_policy.as_of_slot != as_of_slot:
        return _abstain(
            reason=AbstainReason.STALE_STATE,
            message="entry artifact policy uses a different slot",
            as_of_slot=as_of_slot,
        )
    if not _valid_version(artifact_policy.policy_version):
        return _abstain(
            reason=AbstainReason.DECODER_MISMATCH,
            message="entry artifact policy version is required",
            as_of_slot=as_of_slot,
        )
    return _validate_trusted_entry_artifact_policy(
        artifact_policy=artifact_policy,
        as_of_slot=as_of_slot,
    )


def _validate_trusted_entry_artifact_policy(
    *,
    artifact_policy: PolicyBackedEntryArtifactPolicy,
    as_of_slot: Slot,
) -> AbstainResult | None:
    if artifact_policy.policy_version != TRUSTED_ENTRY_ARTIFACT_POLICY_VERSION:
        return _abstain(
            reason=AbstainReason.DECODER_MISMATCH,
            message="entry artifact policy version is not trusted",
            as_of_slot=as_of_slot,
        )
    for (
        field_name,
        expected_versions,
    ) in TRUSTED_ENTRY_ARTIFACT_POLICY_ALLOWLISTS.items():
        if getattr(artifact_policy, field_name) != expected_versions:
            return _abstain(
                reason=AbstainReason.DECODER_MISMATCH,
                message=f"{field_name} does not match trusted entry policy",
                as_of_slot=as_of_slot,
            )
    return None


def _validate_decision_policy_against_artifact_policy(
    *,
    inputs: PolicyBackedEntryGateInputs,
    bundle: DecisionSnapshotBundle,
) -> AbstainResult | None:
    version_error = _validate_versions_against_policy(
        as_of_slot=bundle.as_of_slot,
        version_checks=(
            (
                inputs.decision_policy.policy_version,
                inputs.artifact_policy.accepted_decision_policy_versions,
                "decision snapshot policy",
            ),
        ),
    )
    if version_error is not None:
        return version_error
    selector = bundle.selector
    if selector.operator_churn_snapshot_version is None:
        return None
    return _validate_versions_against_policy(
        as_of_slot=bundle.as_of_slot,
        version_checks=(
            (
                selector.operator_churn_snapshot_version,
                inputs.artifact_policy.accepted_operator_churn_snapshot_versions,
                "operator churn snapshot",
            ),
        ),
    )


def _validate_bundle_against_entry_artifact_policy(
    *,
    artifact_policy: PolicyBackedEntryArtifactPolicy,
    bundle: DecisionSnapshotBundle,
) -> AbstainResult | None:
    version_checks = (
        (
            bundle.snapshot_bundle_version,
            artifact_policy.accepted_snapshot_bundle_versions,
            "snapshot bundle",
        ),
        (
            bundle.feature_snapshot_version,
            artifact_policy.accepted_feature_snapshot_versions,
            "feature snapshot",
        ),
        (
            bundle.market_state_snapshot_version,
            artifact_policy.accepted_market_state_snapshot_versions,
            "market-state snapshot",
        ),
        (
            bundle.matcher.entity_graph_snapshot_version,
            artifact_policy.accepted_entity_graph_snapshot_versions,
            "entity graph snapshot",
        ),
        (
            bundle.matcher.operator_profile_version,
            artifact_policy.accepted_operator_profile_versions,
            "operator profile",
        ),
        (
            bundle.matcher.regime_model_version,
            artifact_policy.accepted_regime_model_versions,
            "regime model",
        ),
        (
            bundle.matcher.matcher_version,
            artifact_policy.accepted_matcher_versions,
            "matcher",
        ),
        (
            bundle.selector.selector_version,
            artifact_policy.accepted_selector_versions,
            "selector",
        ),
        (
            bundle.selector.trigger_generator_version,
            artifact_policy.accepted_trigger_generator_versions,
            "trigger generator",
        ),
        (
            bundle.selector.trigger_feature_schema_version,
            artifact_policy.accepted_trigger_feature_schema_versions,
            "trigger feature schema",
        ),
        (
            bundle.selector.trigger_labeler_version,
            artifact_policy.accepted_trigger_labeler_versions,
            "trigger labeler",
        ),
        (
            bundle.selector.trigger_row_schema_version,
            artifact_policy.accepted_trigger_row_schema_versions,
            "trigger row schema",
        ),
        (
            bundle.timing.timing_model_version,
            artifact_policy.accepted_timing_model_versions,
            "timing model",
        ),
    )
    return _validate_versions_against_policy(
        as_of_slot=bundle.as_of_slot,
        version_checks=version_checks,
    )


def _validate_entry_action_artifacts(
    *,
    inputs: PolicyBackedEntryGateInputs,
    thresholds: PolicyBackedEntryThresholds,
    artifact_policy: PolicyBackedEntryArtifactPolicy,
    as_of_slot: Slot,
) -> AbstainResult | None:
    shape_error = _validate_selected_entry_action_artifact_shape(inputs, as_of_slot)
    if shape_error is not None:
        return shape_error
    slot_error = _validate_entry_action_artifact_slots(
        inputs=inputs,
        thresholds=thresholds,
        as_of_slot=as_of_slot,
    )
    if slot_error is not None:
        return slot_error
    version_error = _validate_versions_against_policy(
        as_of_slot=as_of_slot,
        version_checks=(
            (
                inputs.latency_snapshot.latency_snapshot_version,
                artifact_policy.accepted_latency_snapshot_versions,
                "entry latency snapshot",
            ),
            (
                inputs.edge_snapshot.edge_model_version,
                artifact_policy.accepted_edge_model_versions,
                "entry edge model",
            ),
            (
                thresholds.threshold_policy_version,
                artifact_policy.accepted_threshold_policy_versions,
                "entry threshold policy",
            ),
        ),
    )
    if version_error is not None:
        return version_error
    return _validate_entry_action_artifact_values(
        inputs=inputs,
        thresholds=thresholds,
        as_of_slot=as_of_slot,
    )


def _validate_selected_entry_action_artifact_shape(
    inputs: PolicyBackedEntryGateInputs,
    as_of_slot: Slot,
) -> AbstainResult | None:
    if not isinstance(inputs.latency_snapshot, EntryLatencySnapshot):
        return _unsupported("entry latency snapshot is malformed", as_of_slot)
    if not isinstance(inputs.edge_snapshot, EntryEdgeSnapshot):
        return _unsupported("entry edge snapshot is malformed", as_of_slot)
    return None


def _validate_entry_action_artifact_slots(
    *,
    inputs: PolicyBackedEntryGateInputs,
    thresholds: PolicyBackedEntryThresholds,
    as_of_slot: Slot,
) -> AbstainResult | None:
    slot_fields = {
        "entry latency snapshot": inputs.latency_snapshot.as_of_slot,
        "entry edge snapshot": inputs.edge_snapshot.as_of_slot,
        "entry threshold policy": thresholds.as_of_slot,
    }
    for label, slot in slot_fields.items():
        if not _non_negative_int(slot):
            return _unsupported(f"{label} as_of_slot is invalid", as_of_slot)
        if slot != as_of_slot:
            return _abstain(
                reason=AbstainReason.STALE_STATE,
                message=f"{label} uses a different slot",
                as_of_slot=as_of_slot,
            )
    return None


def _validate_entry_action_artifact_values(
    *,
    inputs: PolicyBackedEntryGateInputs,
    thresholds: PolicyBackedEntryThresholds,
    as_of_slot: Slot,
) -> AbstainResult | None:
    latency_values = {
        "p99_entry_latency_ms": inputs.latency_snapshot.p99_entry_latency_ms,
        "p99_exit_latency_ms": inputs.latency_snapshot.p99_exit_latency_ms,
        "safety_margin_ms": inputs.latency_snapshot.safety_margin_ms,
    }
    for field_name, value in latency_values.items():
        if not _non_negative_int(value):
            return _negative_value(field_name, as_of_slot)
    edge_values = {
        "expected_net_pnl_lcb_lamports": (
            inputs.edge_snapshot.expected_net_pnl_lcb_lamports
        ),
        "minimum_required_edge_lamports": (
            inputs.edge_snapshot.minimum_required_edge_lamports
        ),
    }
    for field_name, value in edge_values.items():
        if not _non_negative_int(value):
            return _negative_value(field_name, as_of_slot)
    probability_error = _validate_probability_fields(
        as_of_slot,
        {
            "entity_probability_threshold_ppm": (
                thresholds.entity_probability_threshold_ppm
            ),
            "regime_probability_threshold_ppm": (
                thresholds.regime_probability_threshold_ppm
            ),
        },
    )
    if probability_error is not None:
        return probability_error
    if not _valid_evidence_ids(inputs.latency_snapshot.evidence_ids):
        return _abstain(
            reason=AbstainReason.MISSING_FEATURE,
            message="entry latency evidence_ids are required",
            as_of_slot=as_of_slot,
        )
    if not _valid_evidence_ids(inputs.edge_snapshot.evidence_ids):
        return _abstain(
            reason=AbstainReason.MISSING_FEATURE,
            message="entry edge evidence_ids are required",
            as_of_slot=as_of_slot,
        )
    return None


def _entry_q10_from_loaded_sizing(
    *,
    sizing_result: LiquiditySizingResult,
    artifact_policy: PolicyBackedEntryArtifactPolicy,
    as_of_slot: Slot,
) -> int | AbstainResult:
    slot_error = _validate_entry_sizing_slot(sizing_result, as_of_slot)
    if slot_error is not None:
        return slot_error
    sizing_result = cast("LiquiditySizingResult", sizing_result)
    sizing_error = _validate_loaded_sizing_result(sizing_result)
    if sizing_error is not None:
        return sizing_error
    policy_error = _validate_sizing_against_entry_artifact_policy(
        sizing_result=sizing_result,
        artifact_policy=artifact_policy,
    )
    if policy_error is not None:
        return policy_error
    selected = sizing_result.selected_size
    if selected is None:
        return 0
    return selected.q10_remaining_time_after_entry_ms


def _validate_entry_sizing_slot(
    sizing_result: object,
    as_of_slot: Slot,
) -> AbstainResult | None:
    if not isinstance(sizing_result, LiquiditySizingResult):
        return _unsupported("sizing_result is malformed", as_of_slot)
    if not _non_negative_int(sizing_result.as_of_slot):
        return _unsupported("sizing result as_of_slot must be non-negative", Slot(-1))
    if sizing_result.as_of_slot != as_of_slot:
        return _abstain(
            reason=AbstainReason.STALE_STATE,
            message="decision bundle and sizing result use different slots",
            as_of_slot=as_of_slot,
        )
    return None


def _validate_sizing_against_entry_artifact_policy(
    *,
    sizing_result: LiquiditySizingResult,
    artifact_policy: PolicyBackedEntryArtifactPolicy,
) -> AbstainResult | None:
    version_checks = (
        (
            sizing_result.liquidity_snapshot_version,
            artifact_policy.accepted_liquidity_snapshot_versions,
            "liquidity snapshot",
        ),
        (
            sizing_result.liquidity_source_artifact_version,
            artifact_policy.accepted_liquidity_source_artifact_versions,
            "liquidity source artifact",
        ),
        (
            sizing_result.quote_engine_version,
            artifact_policy.accepted_quote_engine_versions,
            "quote engine",
        ),
        (
            sizing_result.simulator_version,
            artifact_policy.accepted_simulator_versions,
            "simulator",
        ),
        (
            sizing_result.market_snapshot_version,
            artifact_policy.accepted_sizing_market_snapshot_versions,
            "sizing market snapshot",
        ),
        (
            sizing_result.reserve_snapshot_version,
            artifact_policy.accepted_reserve_snapshot_versions,
            "reserve snapshot",
        ),
        (
            sizing_result.fee_config_version,
            artifact_policy.accepted_fee_config_versions,
            "fee config",
        ),
        (
            sizing_result.volume_classifier_version,
            artifact_policy.accepted_volume_classifier_versions,
            "volume classifier",
        ),
    )
    return _validate_versions_against_policy(
        as_of_slot=sizing_result.as_of_slot,
        version_checks=version_checks,
    )


def _validate_versions_against_policy(
    *,
    as_of_slot: Slot,
    version_checks: tuple[tuple[str, tuple[str, ...], str], ...],
) -> AbstainResult | None:
    for version, accepted_versions, label in version_checks:
        if not _valid_str_tuple(accepted_versions):
            return _abstain(
                reason=AbstainReason.DECODER_MISMATCH,
                message=f"accepted {label} versions are required",
                as_of_slot=as_of_slot,
            )
        if not _valid_version(version):
            return _abstain(
                reason=AbstainReason.DECODER_MISMATCH,
                message=f"{label} version is required",
                as_of_slot=as_of_slot,
            )
        if version not in accepted_versions:
            return _abstain(
                reason=AbstainReason.DECODER_MISMATCH,
                message=f"{label} version is not action-policy accepted",
                as_of_slot=as_of_slot,
            )
    return None


def _entry_inputs_from_policy_bundle(
    *,
    inputs: PolicyBackedEntryGateInputs,
    bundle: DecisionSnapshotBundle,
    q10_remaining_time_after_entry_ms: int,
) -> EntryGateInputs:
    latency_snapshot = cast("EntryLatencySnapshot", inputs.latency_snapshot)
    edge_snapshot = cast("EntryEdgeSnapshot", inputs.edge_snapshot)
    return EntryGateInputs(
        as_of_slot=bundle.as_of_slot,
        entity_probability_ppm=bundle.matcher.entity_probability_ppm,
        regime_probability_ppm=bundle.matcher.regime_probability_ppm,
        q10_remaining_time_after_entry_ms=q10_remaining_time_after_entry_ms,
        p99_entry_latency_ms=latency_snapshot.p99_entry_latency_ms,
        p99_exit_latency_ms=latency_snapshot.p99_exit_latency_ms,
        safety_margin_ms=latency_snapshot.safety_margin_ms,
        expected_net_pnl_lcb_lamports=edge_snapshot.expected_net_pnl_lcb_lamports,
        minimum_required_edge_lamports=edge_snapshot.minimum_required_edge_lamports,
        sizing_result=cast("LiquiditySizingResult", inputs.sizing_result),
    )


def _entry_gate_thresholds_from_policy(
    thresholds: PolicyBackedEntryThresholds,
) -> EntryGateThresholds:
    return EntryGateThresholds(
        entity_probability_threshold_ppm=thresholds.entity_probability_threshold_ppm,
        regime_probability_threshold_ppm=thresholds.regime_probability_threshold_ppm,
    )


def _validate_entry_input_shape(inputs: EntryGateInputs) -> AbstainResult | None:
    if not _non_negative_int(inputs.as_of_slot):
        return _unsupported("entry inputs as_of_slot must be non-negative", Slot(-1))
    if not isinstance(inputs.sizing_result, LiquiditySizingResult):
        return _unsupported("sizing_result is malformed", inputs.as_of_slot)
    if inputs.as_of_slot != inputs.sizing_result.as_of_slot:
        return _abstain(
            reason=AbstainReason.STALE_STATE,
            message="entry inputs and sizing result use different slots",
            as_of_slot=inputs.as_of_slot,
        )
    return None


def _validate_entry_selected_consistency(
    inputs: EntryGateInputs,
) -> AbstainResult | None:
    selected = inputs.sizing_result.selected_size
    if selected is None:
        return None
    if selected.as_of_slot != inputs.as_of_slot:
        return _abstain(
            reason=AbstainReason.STALE_STATE,
            message="selected entry size uses a stale slot",
            as_of_slot=inputs.as_of_slot,
        )
    if selected is not None and (
        selected.q10_remaining_time_after_entry_ms
        != inputs.q10_remaining_time_after_entry_ms
    ):
        return _abstain(
            reason=AbstainReason.STALE_STATE,
            message="entry q10 timing does not match selected counterfactual size",
            as_of_slot=inputs.as_of_slot,
        )
    return None


def _validate_loaded_sizing_result(
    sizing_result: LiquiditySizingResult,
) -> AbstainResult | None:
    for validation in (
        _validate_loaded_sizing_slot,
        _validate_loaded_sizing_provenance,
        _validate_loaded_sizing_probabilities,
        _validate_loaded_sizing_amounts,
        _validate_loaded_sizing_reason_codes,
        _validate_loaded_sizing_risk_caps,
    ):
        validation_error = validation(sizing_result)
        if validation_error is not None:
            return validation_error
    selected = sizing_result.selected_size
    if selected is None:
        return None
    if not isinstance(selected, CandidateEntrySize):
        return _unsupported(
            "selected entry size is malformed", sizing_result.as_of_slot
        )
    selected_error = _validate_candidate(selected, sizing_result.as_of_slot)
    if selected_error is not None:
        return selected_error
    return _validate_selected_size_against_sizing_result(selected, sizing_result)


def _validate_loaded_sizing_slot(
    sizing_result: LiquiditySizingResult,
) -> AbstainResult | None:
    if not _non_negative_int(sizing_result.as_of_slot):
        return _unsupported("sizing result as_of_slot must be non-negative", Slot(-1))
    interval_fields = (
        sizing_result.liquidity_data_start_slot,
        sizing_result.liquidity_data_end_slot,
    )
    if not all(_non_negative_int(value) for value in interval_fields):
        return _unsupported(
            "sizing liquidity interval slot is invalid",
            sizing_result.as_of_slot,
        )
    if not (
        sizing_result.liquidity_data_start_slot
        <= sizing_result.liquidity_data_end_slot
        <= sizing_result.as_of_slot
    ):
        return _abstain(
            reason=AbstainReason.STALE_STATE,
            message="sizing liquidity interval is stale",
            as_of_slot=sizing_result.as_of_slot,
        )
    if sizing_result.liquidity_data_end_slot != sizing_result.as_of_slot:
        return _abstain(
            reason=AbstainReason.STALE_STATE,
            message="sizing liquidity is not decision-current",
            as_of_slot=sizing_result.as_of_slot,
        )
    return None


def _validate_loaded_sizing_provenance(
    sizing_result: LiquiditySizingResult,
) -> AbstainResult | None:
    version_checks = (
        (
            sizing_result.liquidity_snapshot_version,
            sizing_result.accepted_liquidity_snapshot_versions,
            "liquidity snapshot",
        ),
        (
            sizing_result.liquidity_source_artifact_version,
            sizing_result.accepted_liquidity_source_artifact_versions,
            "liquidity source artifact",
        ),
        (
            sizing_result.quote_engine_version,
            sizing_result.accepted_quote_engine_versions,
            "quote engine",
        ),
        (
            sizing_result.simulator_version,
            sizing_result.accepted_simulator_versions,
            "simulator",
        ),
        (
            sizing_result.market_snapshot_version,
            sizing_result.accepted_market_snapshot_versions,
            "market snapshot",
        ),
        (
            sizing_result.reserve_snapshot_version,
            sizing_result.accepted_reserve_snapshot_versions,
            "reserve snapshot",
        ),
        (
            sizing_result.fee_config_version,
            sizing_result.accepted_fee_config_versions,
            "fee config",
        ),
        (
            sizing_result.volume_classifier_version,
            sizing_result.accepted_volume_classifier_versions,
            "volume classifier",
        ),
    )
    for version, accepted_versions, label in version_checks:
        if not _valid_str_tuple(accepted_versions):
            return _abstain(
                reason=AbstainReason.DECODER_MISMATCH,
                message=f"accepted {label} versions are required",
                as_of_slot=sizing_result.as_of_slot,
            )
        if not _valid_version(version):
            return _abstain(
                reason=AbstainReason.DECODER_MISMATCH,
                message=f"{label} version is required",
                as_of_slot=sizing_result.as_of_slot,
            )
        if version not in accepted_versions:
            return _abstain(
                reason=AbstainReason.DECODER_MISMATCH,
                message=f"{label} version is unknown",
                as_of_slot=sizing_result.as_of_slot,
            )
    if not _valid_evidence_ids(sizing_result.liquidity_evidence_ids):
        return _abstain(
            reason=AbstainReason.MISSING_FEATURE,
            message="sizing liquidity evidence_ids are required",
            as_of_slot=sizing_result.as_of_slot,
        )
    if not _valid_reason_codes(sizing_result.liquidity_reason_codes):
        return _abstain(
            reason=AbstainReason.MISSING_FEATURE,
            message="sizing liquidity reason_codes are required",
            as_of_slot=sizing_result.as_of_slot,
        )
    return None


def _validate_loaded_sizing_probabilities(
    sizing_result: LiquiditySizingResult,
) -> AbstainResult | None:
    return _validate_probability_fields(
        sizing_result.as_of_slot,
        {
            "p_full_exit_failure_ppm": sizing_result.p_full_exit_failure_ppm,
            "max_hazard_after_entry_ppm": sizing_result.max_hazard_after_entry_ppm,
            "max_full_exit_failure_ppm": sizing_result.max_full_exit_failure_ppm,
            "max_exit_volume_participation_ppm": (
                sizing_result.max_exit_volume_participation_ppm
            ),
        },
    )


def _validate_loaded_sizing_reason_codes(
    sizing_result: LiquiditySizingResult,
) -> AbstainResult | None:
    if not _valid_reason_codes(sizing_result.reason_codes):
        return _abstain(
            reason=AbstainReason.MISSING_FEATURE,
            message="sizing reason_codes are required",
            as_of_slot=sizing_result.as_of_slot,
        )
    return None


def _validate_loaded_sizing_amounts(
    sizing_result: LiquiditySizingResult,
) -> AbstainResult | None:
    positive_amount_fields = {
        "fixed_cap_quote_base_units": sizing_result.fixed_cap_quote_base_units,
        "bankroll_risk_cap_quote_base_units": (
            sizing_result.bankroll_risk_cap_quote_base_units
        ),
        "pool_depth_cap_quote_base_units": (
            sizing_result.pool_depth_cap_quote_base_units
        ),
        "stressed_exit_cap_quote_base_units": (
            sizing_result.stressed_exit_cap_quote_base_units
        ),
        "max_one_shot_exit_size_base_units": (
            sizing_result.max_one_shot_exit_size_base_units
        ),
        "current_full_exit_output_base_units": (
            sizing_result.current_full_exit_output_base_units
        ),
        "stressed_full_exit_output_base_units": (
            sizing_result.stressed_full_exit_output_base_units
        ),
    }
    for field_name, value in positive_amount_fields.items():
        if not _positive_int(value):
            return _unsupported(
                f"{field_name} must be positive", sizing_result.as_of_slot
            )
    non_negative_amount_fields = {
        "max_entry_quote_base_units": sizing_result.max_entry_quote_base_units,
        "independent_recent_volume_quote_base_units": (
            sizing_result.independent_recent_volume_quote_base_units
        ),
        "volume_participation_cap_quote_base_units": (
            sizing_result.volume_participation_cap_quote_base_units
        ),
        "max_immediate_exit_loss_lamports": (
            sizing_result.max_immediate_exit_loss_lamports
        ),
        "volume_liquidity_mismatch_count": (
            sizing_result.volume_liquidity_mismatch_count
        ),
        "max_volume_liquidity_mismatch_count": (
            sizing_result.max_volume_liquidity_mismatch_count
        ),
        "selected liquidity position": (
            sizing_result.selected_liquidity_position_base_units
        ),
    }
    for field_name, value in non_negative_amount_fields.items():
        if not _non_negative_int(value):
            return _unsupported(
                f"{field_name} must be non-negative", sizing_result.as_of_slot
            )
    return None


def _validate_loaded_sizing_risk_caps(
    sizing_result: LiquiditySizingResult,
) -> AbstainResult | None:
    if sizing_result.p_full_exit_failure_ppm > sizing_result.max_full_exit_failure_ppm:
        return _unsupported(
            "sizing result exceeds full-exit failure cap",
            sizing_result.as_of_slot,
        )
    if (
        sizing_result.volume_liquidity_mismatch_count
        > sizing_result.max_volume_liquidity_mismatch_count
    ):
        return _unsupported(
            "sizing result exceeds volume-liquidity mismatch cap",
            sizing_result.as_of_slot,
        )
    if sizing_result.max_entry_quote_base_units != _loaded_minimum_quote_cap(
        sizing_result
    ):
        return _unsupported(
            "sizing result max entry quote does not match cap components",
            sizing_result.as_of_slot,
        )
    if (
        sizing_result.volume_participation_cap_quote_base_units
        != _loaded_volume_participation_cap(sizing_result)
    ):
        return _unsupported(
            "sizing result volume participation cap is inconsistent",
            sizing_result.as_of_slot,
        )
    return None


def _validate_selected_size_against_sizing_result(
    selected: CandidateEntrySize,
    sizing_result: LiquiditySizingResult,
) -> AbstainResult | None:
    checks = (
        (
            selected.quote_amount_base_units > sizing_result.max_entry_quote_base_units,
            "selected size exceeds sizing max entry quote",
        ),
        (
            selected.expected_position_base_units
            != sizing_result.selected_liquidity_position_base_units,
            "selected size does not match audited liquidity position",
        ),
        (
            selected.expected_position_base_units
            > sizing_result.max_one_shot_exit_size_base_units,
            "selected size exceeds one-shot exit capacity",
        ),
        (
            selected.hazard_after_entry_ppm > sizing_result.max_hazard_after_entry_ppm,
            "selected size exceeds hazard cap",
        ),
        (
            selected.immediate_exit_loss_lamports
            > sizing_result.max_immediate_exit_loss_lamports,
            "selected size exceeds immediate-loss cap",
        ),
        (
            selected.quote_amount_base_units
            > sizing_result.volume_participation_cap_quote_base_units,
            "selected size exceeds volume participation cap",
        ),
    )
    for failed, message in checks:
        if failed:
            return _unsupported(
                message,
                sizing_result.as_of_slot,
            )
    return None


def _loaded_minimum_quote_cap(
    sizing_result: LiquiditySizingResult,
) -> QuoteBaseUnits:
    return QuoteBaseUnits(
        min(
            int(sizing_result.fixed_cap_quote_base_units),
            int(sizing_result.bankroll_risk_cap_quote_base_units),
            int(sizing_result.pool_depth_cap_quote_base_units),
            int(sizing_result.stressed_exit_cap_quote_base_units),
            int(sizing_result.volume_participation_cap_quote_base_units),
        )
    )


def _loaded_volume_participation_cap(
    sizing_result: LiquiditySizingResult,
) -> QuoteBaseUnits:
    return QuoteBaseUnits(
        int(sizing_result.independent_recent_volume_quote_base_units)
        * sizing_result.max_exit_volume_participation_ppm
        // PROBABILITY_PPM_DENOMINATOR
    )


def _validate_entry_numeric_fields(inputs: EntryGateInputs) -> AbstainResult | None:
    numeric_fields: dict[str, object] = {
        "q10_remaining_time_after_entry_ms": inputs.q10_remaining_time_after_entry_ms,
        "p99_entry_latency_ms": inputs.p99_entry_latency_ms,
        "p99_exit_latency_ms": inputs.p99_exit_latency_ms,
        "safety_margin_ms": inputs.safety_margin_ms,
        "expected_net_pnl_lcb_lamports": inputs.expected_net_pnl_lcb_lamports,
        "minimum_required_edge_lamports": inputs.minimum_required_edge_lamports,
    }
    for field_name, value in numeric_fields.items():
        if not _non_negative_int(value):
            return _negative_value(field_name, inputs.as_of_slot)
    return None


def _entry_skip_reason(
    inputs: EntryGateInputs,
    thresholds: EntryGateThresholds,
) -> str | None:
    if inputs.sizing_result.selected_size is None:
        return "no_liquidity_size_selected"
    if inputs.entity_probability_ppm < thresholds.entity_probability_threshold_ppm:
        return "entity_probability_below_threshold"
    if inputs.regime_probability_ppm < thresholds.regime_probability_threshold_ppm:
        return "regime_probability_below_threshold"
    if inputs.q10_remaining_time_after_entry_ms <= _entry_latency_budget_ms(inputs):
        return "q10_remaining_time_inside_latency_budget"
    if int(inputs.expected_net_pnl_lcb_lamports) <= int(
        inputs.minimum_required_edge_lamports
    ):
        return "net_pnl_lcb_below_required_edge"
    return None


def _entry_latency_budget_ms(inputs: EntryGateInputs) -> int:
    return (
        inputs.p99_entry_latency_ms
        + inputs.p99_exit_latency_ms
        + inputs.safety_margin_ms
    )


def _validate_probability_fields(
    as_of_slot: Slot,
    fields: dict[str, object],
) -> AbstainResult | None:
    for field_name, value in fields.items():
        if not _valid_probability_ppm(value):
            return _invalid_probability(field_name, as_of_slot)
    return None


def _valid_probability_ppm(value: object) -> bool:
    return _non_negative_int(value) and value <= PROBABILITY_PPM_DENOMINATOR


def _valid_remaining_time_quantiles(timing: RugTimingSnapshot) -> bool:
    return (
        _non_negative_int(timing.q05_remaining_dump_time_ms)
        and _non_negative_int(timing.q10_remaining_dump_time_ms)
        and _non_negative_int(timing.q50_remaining_dump_time_ms)
        and timing.q05_remaining_dump_time_ms
        <= timing.q10_remaining_dump_time_ms
        <= timing.q50_remaining_dump_time_ms
    )


def _has_timing_snapshot_shape(value: object) -> bool:
    return all(
        hasattr(value, field_name)
        for field_name in (
            "as_of_slot",
            "timing_model_version",
            "p_dump_next_1s_ppm",
            "p_dump_next_3s_ppm",
            "p_dump_next_5s_ppm",
            "p_dump_next_10s_ppm",
            "q05_remaining_dump_time_ms",
            "q10_remaining_dump_time_ms",
            "q50_remaining_dump_time_ms",
        )
    )


def _valid_decimals(value: object) -> bool:
    return type(value) is int and 0 <= value <= MAX_SUPPORTED_DECIMALS


def _valid_version(value: object) -> bool:
    return type(value) is str and bool(value.strip())


def _valid_str_tuple(value: object) -> bool:
    return (
        type(value) is tuple
        and bool(value)
        and all(type(item) is str and bool(item.strip()) for item in value)
    )


def _valid_evidence_ids(evidence_ids: object) -> bool:
    return (
        type(evidence_ids) is tuple
        and bool(evidence_ids)
        and all(
            type(evidence_id) is str and evidence_id for evidence_id in evidence_ids
        )
    )


def _valid_reason_codes(reason_codes: object) -> bool:
    return (
        type(reason_codes) is tuple
        and bool(reason_codes)
        and all(
            type(reason_code) is str and reason_code for reason_code in reason_codes
        )
    )


def _non_negative_int(value: object) -> bool:
    return type(value) is int and value >= 0


def _positive_int(value: object) -> bool:
    return type(value) is int and value > 0


def _invalid_probability(field_name: str, as_of_slot: Slot) -> AbstainResult:
    return _abstain(
        reason=AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
        message=f"{field_name} must be in probability ppm range",
        as_of_slot=as_of_slot,
    )


def _negative_value(field_name: str, as_of_slot: Slot) -> AbstainResult:
    return _abstain(
        reason=AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
        message=f"{field_name} must be non-negative",
        as_of_slot=as_of_slot,
    )


def _quote_abstention(message: str, quote_result: AbstainResult) -> AbstainResult:
    return _abstain(
        reason=quote_result.reason,
        message=message,
        as_of_slot=Slot(quote_result.as_of_slot),
    )


def _quote_unsupported(message: str, as_of_slot: Slot) -> AbstainResult:
    return _unsupported(message, as_of_slot)


def _unsupported(message: str, as_of_slot: Slot) -> AbstainResult:
    return _abstain(
        reason=AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
        message=message,
        as_of_slot=as_of_slot,
    )


def _skip(inputs: EntryGateInputs, reason_code: str) -> EnterSkipDecision:
    return EnterSkipDecision(
        action=EntryDecisionAction.SKIP,
        as_of_slot=inputs.as_of_slot,
        selected_size=inputs.sizing_result.selected_size,
        reason_codes=(reason_code,),
    )


def _selector_skip(as_of_slot: Slot) -> EnterSkipDecision:
    return EnterSkipDecision(
        action=EntryDecisionAction.SKIP,
        as_of_slot=as_of_slot,
        selected_size=None,
        reason_codes=("selector_not_selected",),
    )


def _abstain(
    *,
    reason: AbstainReason,
    message: str,
    as_of_slot: Slot,
) -> AbstainResult:
    return AbstainResult(
        reason=reason,
        message=message,
        as_of_slot=_abstain_slot(as_of_slot),
    )


def _abstain_slot(as_of_slot: object) -> int:
    if type(as_of_slot) is int:
        return as_of_slot
    return -1
