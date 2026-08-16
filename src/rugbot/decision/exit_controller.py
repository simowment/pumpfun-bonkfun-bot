"""Pure dynamic exit-controller logic."""

from dataclasses import dataclass
from enum import Enum

from rugbot.decision.sizing import (
    MAX_SUPPORTED_DECIMALS,
    PROBABILITY_PPM_DENOMINATOR,
)
from rugbot.domain.amounts import Lamports, Slot
from rugbot.domain.decisions import AbstainReason, AbstainResult
from rugbot.domain.quotes import ExecutableQuote, QuotePath

ONE_SECOND_MS = 1_000
THREE_SECONDS_MS = 3_000
FIVE_SECONDS_MS = 5_000
TEN_SECONDS_MS = 10_000
DUMP_Q10_PROBABILITY_PPM = PROBABILITY_PPM_DENOMINATOR // 10


class ExitAction(Enum):
    """Dynamic exit action."""

    HOLD = "hold"
    SELL = "sell"


@dataclass(frozen=True, slots=True)
class ExitModelSnapshot:
    """Timing and value snapshot used for a dynamic exit decision."""

    as_of_slot: Slot
    data_start_slot: Slot
    data_end_slot: Slot
    exit_snapshot_version: str
    source_artifact_version: str
    timing_model_version: str
    value_model_version: str
    latency_model_version: str
    liquidity_snapshot_version: str
    liquidity_source_artifact_version: str
    quote_engine_version: str
    simulator_version: str
    market_snapshot_version: str
    reserve_snapshot_version: str
    fee_config_version: str
    volume_classifier_version: str
    q10_remaining_dump_time_ms: int
    p_dump_next_1s_ppm: int
    p_dump_next_3s_ppm: int
    p_dump_next_5s_ppm: int
    p_dump_next_10s_ppm: int
    p_dump_before_exit_ppm: int
    expected_extra_profit_lamports: Lamports
    expected_dump_loss_lamports: Lamports
    execution_cost_lamports: Lamports
    uncertainty_penalty_lamports: Lamports
    p99_exit_latency_ms: int
    safety_margin_ms: int
    full_position_base_units: int
    liquidity_data_start_slot: Slot
    liquidity_data_end_slot: Slot
    liquidity_selected_full_position_base_units: int
    max_one_shot_exit_size_base_units: int
    current_full_exit_output_base_units: int
    stressed_full_exit_output_base_units: int
    p_full_exit_failure_ppm: int
    volume_liquidity_mismatch_count: int
    full_position_sell_quote: ExecutableQuote | AbstainResult
    evidence_ids: tuple[str, ...]
    timing_evidence_ids: tuple[str, ...]
    value_evidence_ids: tuple[str, ...]
    latency_evidence_ids: tuple[str, ...]
    liquidity_evidence_ids: tuple[str, ...]
    reason_codes: tuple[str, ...]
    deterministic_sell_reasons: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ExitSnapshotPolicy:
    """Trusted policy for validating loaded exit snapshots."""

    as_of_slot: Slot
    accepted_exit_snapshot_versions: tuple[str, ...]
    accepted_source_artifact_versions: tuple[str, ...]
    accepted_timing_model_versions: tuple[str, ...]
    accepted_value_model_versions: tuple[str, ...]
    accepted_latency_model_versions: tuple[str, ...]
    accepted_liquidity_snapshot_versions: tuple[str, ...]
    accepted_liquidity_source_artifact_versions: tuple[str, ...]
    accepted_quote_engine_versions: tuple[str, ...]
    accepted_simulator_versions: tuple[str, ...]
    accepted_market_snapshot_versions: tuple[str, ...]
    accepted_reserve_snapshot_versions: tuple[str, ...]
    accepted_fee_config_versions: tuple[str, ...]
    accepted_volume_classifier_versions: tuple[str, ...]
    accepted_quote_decoder_versions: tuple[str, ...]
    accepted_quote_idl_hashes: tuple[str, ...]
    accepted_quote_program_config_versions: tuple[str, ...]
    max_full_exit_failure_ppm: int
    max_volume_liquidity_mismatch_count: int


@dataclass(frozen=True, slots=True)
class ExitDecision:
    """Exit-controller decision."""

    action: ExitAction
    as_of_slot: Slot
    hold_value_lamports: Lamports
    full_position_output_base_units: int | None
    exit_snapshot_version: str
    source_artifact_version: str
    timing_model_version: str
    value_model_version: str
    latency_model_version: str
    liquidity_snapshot_version: str
    liquidity_source_artifact_version: str
    p_full_exit_failure_ppm: int
    volume_liquidity_mismatch_count: int
    evidence_ids: tuple[str, ...]
    reason_codes: tuple[str, ...]


def decide_dynamic_exit(
    snapshot: ExitModelSnapshot,
    policy: ExitSnapshotPolicy,
) -> ExitDecision | AbstainResult:
    """Decide whether to hold or sell using latency and hold-value rules."""

    quote = _validated_quote_only(snapshot, policy)
    if isinstance(quote, AbstainResult):
        return quote

    minimal_error = _validate_minimal_snapshot_shape(snapshot, policy)
    if minimal_error is not None:
        return minimal_error

    sell_reason = _deterministic_sell_reason(snapshot)
    if sell_reason is not None:
        return _sell(snapshot, quote, sell_reason, Lamports(0))

    hold_safety_error = _validate_hold_safety_snapshot(snapshot, policy, quote)
    if hold_safety_error is not None:
        return _sell(snapshot, quote, hold_safety_error, Lamports(0))

    hold_value = _hold_value_lamports(snapshot)
    if int(hold_value) <= 0:
        return _sell(snapshot, quote, "hold_value_not_positive", hold_value)

    return ExitDecision(
        action=ExitAction.HOLD,
        as_of_slot=snapshot.as_of_slot,
        hold_value_lamports=hold_value,
        full_position_output_base_units=quote.output_amount_base_units,
        exit_snapshot_version=snapshot.exit_snapshot_version,
        source_artifact_version=snapshot.source_artifact_version,
        timing_model_version=snapshot.timing_model_version,
        value_model_version=snapshot.value_model_version,
        latency_model_version=snapshot.latency_model_version,
        liquidity_snapshot_version=snapshot.liquidity_snapshot_version,
        liquidity_source_artifact_version=snapshot.liquidity_source_artifact_version,
        p_full_exit_failure_ppm=snapshot.p_full_exit_failure_ppm,
        volume_liquidity_mismatch_count=snapshot.volume_liquidity_mismatch_count,
        evidence_ids=_decision_evidence_ids(snapshot),
        reason_codes=("hold_value_positive",),
    )


def _hold_value_lamports(snapshot: ExitModelSnapshot) -> Lamports:
    p_dump = snapshot.p_dump_before_exit_ppm
    p_no_dump = PROBABILITY_PPM_DENOMINATOR - p_dump
    expected_profit = (
        p_no_dump
        * int(snapshot.expected_extra_profit_lamports)
        // PROBABILITY_PPM_DENOMINATOR
    )
    expected_loss = _ceil_probability_amount(
        int(snapshot.expected_dump_loss_lamports),
        p_dump,
    )
    return Lamports(
        expected_profit
        - expected_loss
        - int(snapshot.execution_cost_lamports)
        - int(snapshot.uncertainty_penalty_lamports)
    )


def _sell(
    snapshot: ExitModelSnapshot,
    quote: ExecutableQuote,
    reason_code: str,
    hold_value_lamports: Lamports | None = None,
) -> ExitDecision:
    deterministic_reasons = (
        snapshot.deterministic_sell_reasons
        if _valid_reason_codes(snapshot.deterministic_sell_reasons)
        else ()
    )
    hold_value = (
        _hold_value_lamports(snapshot)
        if hold_value_lamports is None
        and _valid_probability_ppm(snapshot.p_dump_before_exit_ppm)
        else hold_value_lamports
    )
    return ExitDecision(
        action=ExitAction.SELL,
        as_of_slot=snapshot.as_of_slot,
        hold_value_lamports=hold_value if hold_value is not None else Lamports(0),
        full_position_output_base_units=quote.output_amount_base_units,
        exit_snapshot_version=snapshot.exit_snapshot_version,
        source_artifact_version=snapshot.source_artifact_version,
        timing_model_version=snapshot.timing_model_version,
        value_model_version=snapshot.value_model_version,
        latency_model_version=snapshot.latency_model_version,
        liquidity_snapshot_version=snapshot.liquidity_snapshot_version,
        liquidity_source_artifact_version=snapshot.liquidity_source_artifact_version,
        p_full_exit_failure_ppm=snapshot.p_full_exit_failure_ppm,
        volume_liquidity_mismatch_count=snapshot.volume_liquidity_mismatch_count,
        evidence_ids=_decision_evidence_ids(snapshot),
        reason_codes=(reason_code, *deterministic_reasons),
    )


def _validate_minimal_snapshot_shape(
    snapshot: object,
    policy: ExitSnapshotPolicy,
) -> AbstainResult | None:
    if not isinstance(policy, ExitSnapshotPolicy):
        return _unsupported("exit policy is malformed", Slot(-1))
    if not _non_negative_int(policy.as_of_slot):
        return _unsupported("exit policy as_of_slot must be non-negative", Slot(-1))
    if not isinstance(snapshot, ExitModelSnapshot):
        return _unsupported("exit snapshot is malformed", _policy_slot(policy))
    if not _non_negative_int(snapshot.as_of_slot):
        return _unsupported("exit snapshot as_of_slot must be non-negative", Slot(-1))
    if snapshot.as_of_slot != policy.as_of_slot:
        return _abstain(
            reason=AbstainReason.STALE_STATE,
            message="exit snapshot and policy use different slots",
            as_of_slot=policy.as_of_slot,
        )
    return None


def _validated_quote_only(  # noqa: PLR0911
    snapshot: object,
    policy: ExitSnapshotPolicy,
) -> ExecutableQuote | AbstainResult:
    shape_error = _validate_minimal_snapshot_shape(snapshot, policy)
    if shape_error is not None:
        return shape_error
    quote = snapshot.full_position_sell_quote
    if isinstance(quote, AbstainResult):
        return _abstain(
            reason=quote.reason,
            message="full-position sell quote abstained",
            as_of_slot=snapshot.as_of_slot,
        )
    if not isinstance(quote, ExecutableQuote):
        return _unsupported(
            "full-position sell quote is malformed",
            snapshot.as_of_slot,
        )
    quote_error = _validate_full_position_quote_fields(quote, snapshot, policy)
    if quote_error is not None:
        return quote_error
    if quote.as_of_slot != snapshot.as_of_slot:
        return _abstain(
            reason=AbstainReason.STALE_STATE,
            message="full-position sell quote uses a stale slot",
            as_of_slot=snapshot.as_of_slot,
        )
    if not _positive_int(snapshot.full_position_base_units):
        return _unsupported(
            "full_position_base_units must be positive",
            snapshot.as_of_slot,
        )
    if quote.input_amount_base_units != snapshot.full_position_base_units:
        return _abstain(
            reason=AbstainReason.MISSING_FEATURE,
            message="full-position sell quote input does not match position size",
            as_of_slot=snapshot.as_of_slot,
        )
    return quote


def _validate_full_position_quote_fields(
    quote: ExecutableQuote,
    snapshot: ExitModelSnapshot,
    policy: ExitSnapshotPolicy,
) -> AbstainResult | None:
    shape_error = _validate_full_position_quote_shape(quote, snapshot)
    if shape_error is not None:
        return shape_error
    numeric_error = _validate_full_position_quote_numeric_fields(quote, snapshot)
    if numeric_error is not None:
        return numeric_error
    return _validate_full_position_quote_versions(quote, snapshot, policy)


def _validate_full_position_quote_shape(
    quote: ExecutableQuote,
    snapshot: ExitModelSnapshot,
) -> AbstainResult | None:
    if not isinstance(quote.path, QuotePath):
        return _unsupported("full-position quote path is invalid", snapshot.as_of_slot)
    if not _non_negative_int(quote.as_of_slot):
        return _unsupported("full-position quote slot is invalid", snapshot.as_of_slot)
    return None


def _validate_full_position_quote_numeric_fields(
    quote: ExecutableQuote,
    snapshot: ExitModelSnapshot,
) -> AbstainResult | None:
    if not _positive_int(quote.input_amount_base_units):
        return _unsupported(
            "full-position sell quote input must be positive",
            snapshot.as_of_slot,
        )
    if not _positive_int(quote.output_amount_base_units):
        return _unsupported(
            "full-position sell quote has zero output",
            snapshot.as_of_slot,
        )
    if not _non_negative_int(quote.fee_amount_base_units):
        return _unsupported(
            "full-position sell quote fee must be non-negative",
            snapshot.as_of_slot,
        )
    if not _valid_decimals(quote.base_decimals):
        return _unsupported(
            "full-position sell quote base_decimals are unsupported",
            snapshot.as_of_slot,
        )
    if not _valid_decimals(quote.quote_decimals):
        return _unsupported(
            "full-position sell quote quote_decimals are unsupported",
            snapshot.as_of_slot,
        )
    return None


def _validate_full_position_quote_versions(
    quote: ExecutableQuote,
    snapshot: ExitModelSnapshot,
    policy: ExitSnapshotPolicy,
) -> AbstainResult | None:
    checks = (
        (
            quote.fee_config_version,
            policy.accepted_fee_config_versions,
            "fee_config_version",
        ),
        (
            quote.decoder_version,
            policy.accepted_quote_decoder_versions,
            "decoder_version",
        ),
        (quote.idl_hash, policy.accepted_quote_idl_hashes, "idl_hash"),
        (
            quote.program_config_version,
            policy.accepted_quote_program_config_versions,
            "program_config_version",
        ),
    )
    for value, accepted_versions, field_name in checks:
        if not _valid_str_tuple(accepted_versions):
            return _abstain(
                reason=AbstainReason.DECODER_MISMATCH,
                message=f"accepted quote {field_name} versions are required",
                as_of_slot=snapshot.as_of_slot,
            )
        if not _valid_version(value):
            return _abstain(
                reason=AbstainReason.DECODER_MISMATCH,
                message=f"full-position sell quote {field_name} is required",
                as_of_slot=snapshot.as_of_slot,
            )
        if value not in accepted_versions:
            return _abstain(
                reason=AbstainReason.DECODER_MISMATCH,
                message=f"full-position sell quote {field_name} is unknown",
                as_of_slot=snapshot.as_of_slot,
            )
    return None


def _validate_hold_safety_snapshot(
    snapshot: ExitModelSnapshot,
    policy: ExitSnapshotPolicy,
    quote: ExecutableQuote,
) -> str | None:
    for validator in (
        _validate_hold_safety_versions,
        _validate_hold_safety_evidence,
        _validate_hold_safety_intervals,
        _validate_hold_safety_numeric_fields,
        _validate_hold_safety_probabilities,
    ):
        reason = validator(snapshot, policy)
        if reason is not None:
            return reason
    return _hold_safety_sell_reason(snapshot, policy, quote)


def _validate_hold_safety_versions(
    snapshot: ExitModelSnapshot,
    policy: ExitSnapshotPolicy,
) -> str | None:
    checks = (
        (
            snapshot.exit_snapshot_version,
            policy.accepted_exit_snapshot_versions,
        ),
        (snapshot.source_artifact_version, policy.accepted_source_artifact_versions),
        (snapshot.timing_model_version, policy.accepted_timing_model_versions),
        (snapshot.value_model_version, policy.accepted_value_model_versions),
        (snapshot.latency_model_version, policy.accepted_latency_model_versions),
        (
            snapshot.liquidity_snapshot_version,
            policy.accepted_liquidity_snapshot_versions,
        ),
        (
            snapshot.liquidity_source_artifact_version,
            policy.accepted_liquidity_source_artifact_versions,
        ),
        (snapshot.quote_engine_version, policy.accepted_quote_engine_versions),
        (snapshot.simulator_version, policy.accepted_simulator_versions),
        (snapshot.market_snapshot_version, policy.accepted_market_snapshot_versions),
        (snapshot.reserve_snapshot_version, policy.accepted_reserve_snapshot_versions),
        (snapshot.fee_config_version, policy.accepted_fee_config_versions),
        (
            snapshot.volume_classifier_version,
            policy.accepted_volume_classifier_versions,
        ),
    )
    if any(not _valid_str_tuple(accepted) for _, accepted in checks):
        return "missing_exit_accepted_version_policy"
    if any(not _valid_version(version) for version, _ in checks):
        return "missing_exit_evidence_version"
    if any(version not in accepted for version, accepted in checks):
        return "unknown_exit_evidence_version"
    return None


def _validate_hold_safety_evidence(
    snapshot: ExitModelSnapshot,
    policy: ExitSnapshotPolicy,
) -> str | None:
    del policy
    evidence_groups = (
        snapshot.evidence_ids,
        snapshot.timing_evidence_ids,
        snapshot.value_evidence_ids,
        snapshot.latency_evidence_ids,
        snapshot.liquidity_evidence_ids,
        snapshot.reason_codes,
    )
    if any(not _valid_non_empty_str_tuple(group) for group in evidence_groups):
        return "missing_exit_evidence"
    if not _valid_reason_codes(snapshot.deterministic_sell_reasons):
        return "missing_exit_evidence"
    return None


def _validate_hold_safety_intervals(
    snapshot: ExitModelSnapshot,
    policy: ExitSnapshotPolicy,
) -> str | None:
    del policy
    slot_fields = (
        snapshot.data_start_slot,
        snapshot.data_end_slot,
        snapshot.liquidity_data_start_slot,
        snapshot.liquidity_data_end_slot,
    )
    if any(not _non_negative_int(value) for value in slot_fields):
        return "invalid_exit_evidence_slot"
    if not (
        snapshot.data_start_slot <= snapshot.data_end_slot == snapshot.as_of_slot
        and snapshot.liquidity_data_start_slot
        <= snapshot.liquidity_data_end_slot
        == snapshot.as_of_slot
    ):
        return "stale_exit_evidence"
    return None


def _validate_hold_safety_numeric_fields(  # noqa: PLR0911
    snapshot: ExitModelSnapshot,
    policy: ExitSnapshotPolicy,
) -> str | None:
    numeric_fields: dict[str, object] = {
        "q10_remaining_dump_time_ms": snapshot.q10_remaining_dump_time_ms,
        "expected_extra_profit_lamports": snapshot.expected_extra_profit_lamports,
        "expected_dump_loss_lamports": snapshot.expected_dump_loss_lamports,
        "execution_cost_lamports": snapshot.execution_cost_lamports,
        "uncertainty_penalty_lamports": snapshot.uncertainty_penalty_lamports,
        "p99_exit_latency_ms": snapshot.p99_exit_latency_ms,
        "safety_margin_ms": snapshot.safety_margin_ms,
        "full_position_base_units": snapshot.full_position_base_units,
        "liquidity_selected_full_position_base_units": (
            snapshot.liquidity_selected_full_position_base_units
        ),
        "max_one_shot_exit_size_base_units": (
            snapshot.max_one_shot_exit_size_base_units
        ),
        "current_full_exit_output_base_units": (
            snapshot.current_full_exit_output_base_units
        ),
        "stressed_full_exit_output_base_units": (
            snapshot.stressed_full_exit_output_base_units
        ),
        "volume_liquidity_mismatch_count": (snapshot.volume_liquidity_mismatch_count),
        "max_volume_liquidity_mismatch_count": (
            policy.max_volume_liquidity_mismatch_count
        ),
    }
    if any(not _non_negative_int(value) for value in numeric_fields.values()):
        return "invalid_exit_model_snapshot"
    if any(
        not _valid_probability_ppm(value)
        for value in (
            snapshot.p_dump_next_1s_ppm,
            snapshot.p_dump_next_3s_ppm,
            snapshot.p_dump_next_5s_ppm,
            snapshot.p_dump_next_10s_ppm,
            snapshot.p_dump_before_exit_ppm,
            snapshot.p_full_exit_failure_ppm,
            policy.max_full_exit_failure_ppm,
        )
    ):
        return "invalid_exit_probability"
    if snapshot.full_position_base_units == 0:
        return "invalid_exit_model_snapshot"
    if snapshot.max_one_shot_exit_size_base_units == 0:
        return "exit_capacity_breach"
    if snapshot.current_full_exit_output_base_units == 0:
        return "liquidity_quote_output_mismatch"
    if snapshot.stressed_full_exit_output_base_units == 0:
        return "stressed_exit_output_missing"
    return None


def _validate_hold_safety_probabilities(
    snapshot: ExitModelSnapshot,
    policy: ExitSnapshotPolicy,
) -> str | None:
    del policy
    if not (
        snapshot.p_dump_next_1s_ppm
        <= snapshot.p_dump_next_3s_ppm
        <= snapshot.p_dump_next_5s_ppm
        <= snapshot.p_dump_next_10s_ppm
    ):
        return "incoherent_exit_probabilities"
    if snapshot.p99_exit_latency_ms + snapshot.safety_margin_ms > TEN_SECONDS_MS:
        return "exit_latency_budget_outside_timing_horizon"
    q10_error = _validate_q10_timing_coherence(snapshot)
    if q10_error is not None:
        return q10_error
    if snapshot.p_dump_before_exit_ppm < _exit_budget_probability(snapshot):
        return "dump_probability_below_exit_latency_horizon"
    return None


def _validate_q10_timing_coherence(snapshot: ExitModelSnapshot) -> str | None:
    q10_ms = snapshot.q10_remaining_dump_time_ms
    q10_ppm = DUMP_Q10_PROBABILITY_PPM
    if q10_ms <= ONE_SECOND_MS:
        coherent = snapshot.p_dump_next_1s_ppm >= q10_ppm
    elif q10_ms <= THREE_SECONDS_MS:
        coherent = (
            snapshot.p_dump_next_1s_ppm < q10_ppm
            and snapshot.p_dump_next_3s_ppm >= q10_ppm
        )
    elif q10_ms <= FIVE_SECONDS_MS:
        coherent = (
            snapshot.p_dump_next_3s_ppm < q10_ppm
            and snapshot.p_dump_next_5s_ppm >= q10_ppm
        )
    elif q10_ms <= TEN_SECONDS_MS:
        coherent = (
            snapshot.p_dump_next_5s_ppm < q10_ppm
            and snapshot.p_dump_next_10s_ppm >= q10_ppm
        )
    else:
        coherent = snapshot.p_dump_next_10s_ppm < q10_ppm
    if not coherent:
        return "incoherent_exit_timing_quantile"
    return None


def _hold_safety_sell_reason(  # noqa: PLR0911
    snapshot: ExitModelSnapshot,
    policy: ExitSnapshotPolicy,
    quote: ExecutableQuote,
) -> str | None:
    if snapshot.full_position_base_units != (
        snapshot.liquidity_selected_full_position_base_units
    ):
        return "liquidity_position_mismatch"
    if snapshot.full_position_base_units > snapshot.max_one_shot_exit_size_base_units:
        return "exit_capacity_breach"
    if quote.output_amount_base_units != snapshot.current_full_exit_output_base_units:
        return "liquidity_quote_output_mismatch"
    if snapshot.p_full_exit_failure_ppm > policy.max_full_exit_failure_ppm:
        return "full_exit_failure_above_cap"
    if (
        snapshot.volume_liquidity_mismatch_count
        > policy.max_volume_liquidity_mismatch_count
    ):
        return "volume_liquidity_mismatch_above_cap"
    if (
        snapshot.q10_remaining_dump_time_ms
        <= snapshot.p99_exit_latency_ms + snapshot.safety_margin_ms
    ):
        return "q10_remaining_time_inside_exit_latency"
    return None


def _exit_budget_probability(snapshot: ExitModelSnapshot) -> int:
    budget_ms = snapshot.p99_exit_latency_ms + snapshot.safety_margin_ms
    if budget_ms <= ONE_SECOND_MS:
        return snapshot.p_dump_next_1s_ppm
    if budget_ms <= THREE_SECONDS_MS:
        return snapshot.p_dump_next_3s_ppm
    if budget_ms <= FIVE_SECONDS_MS:
        return snapshot.p_dump_next_5s_ppm
    return snapshot.p_dump_next_10s_ppm


def _deterministic_sell_reason(snapshot: ExitModelSnapshot) -> str | None:
    if snapshot.deterministic_sell_reasons and _valid_reason_codes(
        snapshot.deterministic_sell_reasons
    ):
        return "deterministic_sell_rule"
    return None


def _decision_evidence_ids(snapshot: ExitModelSnapshot) -> tuple[str, ...]:
    evidence_ids: list[str] = []
    for group in (
        snapshot.evidence_ids,
        snapshot.timing_evidence_ids,
        snapshot.value_evidence_ids,
        snapshot.latency_evidence_ids,
        snapshot.liquidity_evidence_ids,
    ):
        if _valid_non_empty_str_tuple(group):
            evidence_ids.extend(group)
    return tuple(dict.fromkeys(evidence_ids))


def _policy_slot(policy: object) -> Slot:
    if isinstance(policy, ExitSnapshotPolicy) and _non_negative_int(policy.as_of_slot):
        return policy.as_of_slot
    return Slot(-1)


def _abstain(
    *,
    reason: AbstainReason,
    message: str,
    as_of_slot: Slot,
) -> AbstainResult:
    return AbstainResult(reason=reason, message=message, as_of_slot=int(as_of_slot))


def _valid_probability_ppm(value: object) -> bool:
    return _non_negative_int(value) and value <= PROBABILITY_PPM_DENOMINATOR


def _valid_reason_codes(reason_codes: object) -> bool:
    return type(reason_codes) is tuple and all(
        type(reason_code) is str and reason_code for reason_code in reason_codes
    )


def _non_negative_int(value: object) -> bool:
    return type(value) is int and value >= 0


def _positive_int(value: object) -> bool:
    return type(value) is int and value > 0


def _valid_decimals(value: object) -> bool:
    return type(value) is int and 0 <= value <= MAX_SUPPORTED_DECIMALS


def _valid_version(value: object) -> bool:
    return type(value) is str and bool(value)


def _valid_str_tuple(value: object) -> bool:
    return (
        type(value) is tuple
        and bool(value)
        and all(type(item) is str and bool(item) for item in value)
    )


def _valid_non_empty_str_tuple(value: object) -> bool:
    return _valid_str_tuple(value)


def _unsupported(message: str, as_of_slot: Slot) -> AbstainResult:
    return _abstain(
        reason=AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
        message=message,
        as_of_slot=as_of_slot,
    )


def _ceil_probability_amount(amount: int, probability_ppm: int) -> int:
    numerator = amount * probability_ppm
    return (numerator + PROBABILITY_PPM_DENOMINATOR - 1) // (
        PROBABILITY_PPM_DENOMINATOR
    )
