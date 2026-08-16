"""Pure observed-rule hypothesis contracts for adverse operator behavior."""

from dataclasses import dataclass
from enum import Enum

from rugbot.domain.amounts import Slot
from rugbot.domain.decisions import AbstainReason, AbstainResult
from rugbot.graph.point_in_time import PROBABILITY_PPM_DENOMINATOR


class OperatorAction(Enum):
    """Supported labeled operator actions."""

    HOLD = "hold"
    BUY_WITH_LINKED_WALLET = "buy_with_linked_wallet"
    TRANSFER = "transfer"
    PARTIAL_SELL = "partial_sell"
    FULL_DUMP = "full_dump"


class RuleExpressionKind(Enum):
    """Structured predictive-equivalence expression kinds."""

    ELAPSED_MS_AT_OR_ABOVE = "elapsed_ms_at_or_above"
    QUOTE_RESERVE_AT_OR_ABOVE = "quote_reserve_at_or_above"
    CURVE_PROGRESS_AT_OR_ABOVE = "curve_progress_at_or_above"
    OPERATOR_PNL_AT_OR_ABOVE = "operator_pnl_at_or_above"
    INDEPENDENT_BUYER_COUNT_AT_OR_ABOVE = "independent_buyer_count_at_or_above"
    TIME_SINCE_LAST_INDEPENDENT_BUY_AT_OR_ABOVE = (
        "time_since_last_independent_buy_at_or_above"
    )


class TriggerMatchStatus(Enum):
    """Relationship between live state and an observed trigger band."""

    BELOW_OBSERVED_RANGE = "below_observed_range"
    APPROACHING_OBSERVED_TRIGGER = "approaching_observed_trigger"
    INSIDE_OBSERVED_BAND = "inside_observed_band"
    BEYOND_OBSERVED_BAND = "beyond_observed_band"


@dataclass(frozen=True, slots=True)
class TriggerFeatureSnapshot:
    """Pre-action feature state for one launch and entity/regime."""

    as_of_slot: Slot
    entity_id: str
    campaign_id: str
    regime_id: str
    launch_id: str
    elapsed_ms: int
    quote_reserve_base_units: int
    curve_progress_ppm: int
    operator_pnl_lamports: int
    independent_buyer_count: int
    time_since_last_independent_buy_ms: int
    feature_schema_version: str
    market_state_snapshot_version: str
    operator_profile_version: str
    evidence_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class OperatorActionLabel:
    """Finalized label for the next observed operator action."""

    as_of_slot: Slot
    entity_id: str
    campaign_id: str
    regime_id: str
    launch_id: str
    action: OperatorAction
    action_slot: Slot
    action_index: int
    labeler_version: str
    evidence_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class StateActionTrainingRow:
    """Leakage-checked state(t - 1) to operator_action(t) training row."""

    as_of_slot: Slot
    feature: TriggerFeatureSnapshot
    label: OperatorActionLabel
    row_schema_version: str
    evidence_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class RuleHypothesisConfig:
    """Configuration for a rule-hypothesis artifact build."""

    as_of_slot: Slot
    entity_id: str
    campaign_id: str
    regime_id: str
    target_action: OperatorAction
    generator_version: str
    feature_schema_version: str
    labeler_version: str
    row_schema_version: str
    operator_profile_version: str
    regime_model_version: str
    min_distinct_launch_support: int
    min_precision_ppm: int
    min_confidence_ppm: int


@dataclass(frozen=True, slots=True)
class RuleHypothesis:
    """Predictive-equivalence hypothesis over observed state/action rows."""

    as_of_slot: Slot
    entity_id: str
    campaign_id: str
    regime_id: str
    expression_kind: RuleExpressionKind
    target_action: OperatorAction
    threshold_q10_value: int
    threshold_q50_value: int
    threshold_q90_value: int
    support_row_count: int
    distinct_launch_support: int
    source_row_count: int
    source_distinct_launch_count: int
    precision_ppm: int
    dispersion_ppm: int
    confidence_ppm: int
    median_trigger_error: int
    first_seen_slot: Slot
    last_seen_slot: Slot
    evidence_ids: tuple[str, ...]
    generator_version: str
    feature_schema_version: str
    labeler_version: str
    row_schema_version: str
    operator_profile_version: str
    regime_model_version: str


@dataclass(frozen=True, slots=True)
class RuleHypothesisArtifact:
    """Versioned accepted hypothesis artifact for one entity/campaign/regime."""

    as_of_slot: Slot
    entity_id: str
    campaign_id: str
    regime_id: str
    target_action: OperatorAction
    hypotheses: tuple[RuleHypothesis, ...]
    source_row_count: int
    source_distinct_launch_count: int
    accepted_hypothesis_count: int
    skipped_expression_count: int
    min_distinct_launch_support: int
    min_precision_ppm: int
    min_confidence_ppm: int
    generator_version: str
    feature_schema_version: str
    labeler_version: str
    row_schema_version: str
    operator_profile_version: str
    regime_model_version: str
    reason_codes: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ObservedTriggerEvaluationThresholds:
    """Thresholds for evaluating live features against hypotheses."""

    as_of_slot: Slot
    min_confidence_ppm: int
    min_trigger_risk_ppm: int


@dataclass(frozen=True, slots=True)
class RuleHypothesisMatch:
    """Evaluation of one live feature against one rule hypothesis."""

    as_of_slot: Slot
    entity_id: str
    campaign_id: str
    regime_id: str
    expression_kind: RuleExpressionKind
    target_action: OperatorAction
    status: TriggerMatchStatus
    observed_value: int
    threshold_q50_value: int
    proximity_ppm: int
    trigger_risk_ppm: int
    confidence_ppm: int
    precision_ppm: int
    generator_version: str
    feature_schema_version: str
    labeler_version: str
    row_schema_version: str
    operator_profile_version: str
    regime_model_version: str


@dataclass(frozen=True, slots=True)
class ObservedTriggerEvaluation:
    """Pure point-in-time evaluation of observed trigger hypotheses."""

    as_of_slot: Slot
    entity_id: str
    campaign_id: str
    regime_id: str
    target_action: OperatorAction
    matches: tuple[RuleHypothesisMatch, ...]
    max_trigger_risk_ppm: int
    generator_version: str
    feature_schema_version: str
    labeler_version: str
    row_schema_version: str
    market_state_snapshot_version: str
    operator_profile_version: str
    regime_model_version: str
    reason_codes: tuple[str, ...]


def generate_rule_hypotheses(
    *,
    rows: tuple[StateActionTrainingRow, ...],
    config: RuleHypothesisConfig,
) -> RuleHypothesisArtifact | AbstainResult:
    """Generate observed rule hypotheses from finalized state/action rows."""

    validation_error = _validate_generation_inputs(rows, config)
    if validation_error is not None:
        return validation_error

    target_rows = tuple(row for row in rows if row.label.action is config.target_action)
    if _has_duplicate_launches(target_rows):
        return _unsupported("target action rows must be unique by launch", config)
    target_launch_count = _distinct_launch_count(target_rows)
    if target_launch_count < config.min_distinct_launch_support:
        return _missing(
            "target action has insufficient distinct launch support", config
        )

    hypotheses = tuple(
        hypothesis
        for hypothesis in (
            _build_expression_hypothesis(
                expression_kind=expression_kind,
                rows=rows,
                target_rows=target_rows,
                config=config,
            )
            for expression_kind in RuleExpressionKind
        )
        if hypothesis is not None
    )
    accepted = tuple(
        hypothesis
        for hypothesis in hypotheses
        if hypothesis.precision_ppm >= config.min_precision_ppm
        and hypothesis.confidence_ppm >= config.min_confidence_ppm
    )
    if not accepted:
        return _missing("no rule hypothesis met precision and confidence", config)

    return RuleHypothesisArtifact(
        as_of_slot=config.as_of_slot,
        entity_id=config.entity_id,
        campaign_id=config.campaign_id,
        regime_id=config.regime_id,
        target_action=config.target_action,
        hypotheses=tuple(
            sorted(accepted, key=lambda hypothesis: hypothesis.expression_kind.value)
        ),
        source_row_count=len(rows),
        source_distinct_launch_count=_distinct_launch_count(rows),
        accepted_hypothesis_count=len(accepted),
        skipped_expression_count=len(tuple(RuleExpressionKind)) - len(accepted),
        min_distinct_launch_support=config.min_distinct_launch_support,
        min_precision_ppm=config.min_precision_ppm,
        min_confidence_ppm=config.min_confidence_ppm,
        generator_version=config.generator_version,
        feature_schema_version=config.feature_schema_version,
        labeler_version=config.labeler_version,
        row_schema_version=config.row_schema_version,
        operator_profile_version=config.operator_profile_version,
        regime_model_version=config.regime_model_version,
        reason_codes=("observed_rule_hypotheses_generated",),
    )


def evaluate_observed_trigger_hypotheses(
    *,
    feature: TriggerFeatureSnapshot,
    artifact: RuleHypothesisArtifact,
    thresholds: ObservedTriggerEvaluationThresholds,
) -> ObservedTriggerEvaluation | AbstainResult:
    """Evaluate live pre-action features against observed hypotheses."""

    validation_error = _validate_evaluation_inputs(
        feature=feature,
        artifact=artifact,
        thresholds=thresholds,
    )
    if validation_error is not None:
        return validation_error

    eligible = tuple(
        hypothesis
        for hypothesis in artifact.hypotheses
        if hypothesis.confidence_ppm >= thresholds.min_confidence_ppm
    )
    if not eligible:
        return _missing("no rule hypotheses met evaluation confidence", artifact)

    matches = tuple(
        _match_hypothesis(feature=feature, hypothesis=hypothesis)
        for hypothesis in eligible
    )
    max_risk = max(match.trigger_risk_ppm for match in matches)
    risk_reason = (
        "trigger_risk_threshold_crossed"
        if max_risk >= thresholds.min_trigger_risk_ppm
        else "trigger_risk_below_threshold"
    )
    return ObservedTriggerEvaluation(
        as_of_slot=feature.as_of_slot,
        entity_id=feature.entity_id,
        campaign_id=feature.campaign_id,
        regime_id=feature.regime_id,
        target_action=artifact.target_action,
        matches=tuple(sorted(matches, key=lambda match: match.expression_kind.value)),
        max_trigger_risk_ppm=max_risk,
        generator_version=artifact.generator_version,
        feature_schema_version=feature.feature_schema_version,
        labeler_version=artifact.labeler_version,
        row_schema_version=artifact.row_schema_version,
        market_state_snapshot_version=feature.market_state_snapshot_version,
        operator_profile_version=feature.operator_profile_version,
        regime_model_version=artifact.regime_model_version,
        reason_codes=("observed_trigger_hypotheses_evaluated", risk_reason),
    )


def _validate_generation_inputs(
    rows: tuple[StateActionTrainingRow, ...],
    config: RuleHypothesisConfig,
) -> AbstainResult | None:
    config_error = _validate_config(config)
    if config_error is not None:
        return config_error
    if type(rows) is not tuple:
        return _unsupported("training rows must be an immutable tuple", config)
    if not rows:
        return _missing("state/action training rows are required", config)
    for row in rows:
        row_error = _validate_row(row, config)
        if row_error is not None:
            return row_error
    return None


def _validate_config(config: RuleHypothesisConfig) -> AbstainResult | None:
    if not _non_negative_int(config.as_of_slot):
        return _unsupported("as_of_slot must be non-negative", config)
    if not isinstance(config.target_action, OperatorAction):
        return _unsupported("target_action is invalid", config)
    missing_id = _require_strings(
        config,
        {
            "entity_id": config.entity_id,
            "campaign_id": config.campaign_id,
            "regime_id": config.regime_id,
        },
        AbstainReason.MISSING_FEATURE,
    )
    if missing_id is not None:
        return missing_id
    version_error = _require_strings(
        config,
        _config_version_fields(config),
        AbstainReason.DECODER_MISMATCH,
    )
    if version_error is not None:
        return version_error
    if not _positive_int(config.min_distinct_launch_support):
        return _unsupported("min_distinct_launch_support must be positive", config)
    return _validate_probability_fields(
        config,
        {
            "min_precision_ppm": config.min_precision_ppm,
            "min_confidence_ppm": config.min_confidence_ppm,
        },
    )


def _validate_row(
    row: StateActionTrainingRow,
    config: RuleHypothesisConfig,
) -> AbstainResult | None:
    for validation in (
        _validate_row_metadata,
        _validate_row_feature,
        _validate_row_label,
        _validate_row_temporal_order,
    ):
        validation_error = validation(row, config)
        if validation_error is not None:
            return validation_error
    return None


def _validate_row_metadata(
    row: StateActionTrainingRow,
    config: RuleHypothesisConfig,
) -> AbstainResult | None:
    if not _non_negative_int(row.as_of_slot):
        return _unsupported("row as_of_slot must be non-negative", config)
    if row.as_of_slot > config.as_of_slot:
        return _stale("row evidence is newer than artifact as_of_slot", config)
    if row.row_schema_version != config.row_schema_version:
        return _decoder_mismatch("row schema version mismatch", config)
    if not _valid_evidence_ids(row.evidence_ids):
        return _missing("row evidence_ids are required", config)
    return None


def _validate_row_feature(
    row: StateActionTrainingRow,
    config: RuleHypothesisConfig,
) -> AbstainResult | None:
    return _validate_feature(row.feature, config)


def _validate_row_label(
    row: StateActionTrainingRow,
    config: RuleHypothesisConfig,
) -> AbstainResult | None:
    return _validate_label(row.label, config)


def _validate_feature(
    feature: TriggerFeatureSnapshot,
    config: RuleHypothesisConfig,
) -> AbstainResult | None:
    for validation in (
        _validate_feature_values,
        _validate_feature_identity,
        _validate_feature_versions,
    ):
        validation_error = validation(feature, config)
        if validation_error is not None:
            return validation_error
    return None


def _validate_feature_values(
    feature: TriggerFeatureSnapshot,
    config: RuleHypothesisConfig,
) -> AbstainResult | None:
    if feature.as_of_slot > config.as_of_slot:
        return _stale("feature snapshot is newer than artifact as_of_slot", config)
    if any(not _non_negative_int(value) for value in _feature_numeric_values(feature)):
        return _unsupported("feature values must be non-negative integers", config)
    if not _valid_probability_ppm(feature.curve_progress_ppm):
        return _unsupported(
            "curve_progress_ppm must be in probability ppm range", config
        )
    return None


def _validate_feature_versions(
    feature: TriggerFeatureSnapshot,
    config: RuleHypothesisConfig,
) -> AbstainResult | None:
    if feature.feature_schema_version != config.feature_schema_version:
        return _decoder_mismatch("feature schema version mismatch", config)
    if feature.operator_profile_version != config.operator_profile_version:
        return _decoder_mismatch("operator profile version mismatch", config)
    if (
        not isinstance(feature.market_state_snapshot_version, str)
        or not feature.market_state_snapshot_version
    ):
        return _decoder_mismatch("market_state_snapshot_version is required", config)
    if not _valid_evidence_ids(feature.evidence_ids):
        return _missing("feature evidence_ids are required", config)
    return None


def _validate_feature_identity(
    feature: TriggerFeatureSnapshot,
    config: RuleHypothesisConfig,
) -> AbstainResult | None:
    mismatches = {
        "entity_id": feature.entity_id != config.entity_id,
        "campaign_id": feature.campaign_id != config.campaign_id,
        "regime_id": feature.regime_id != config.regime_id,
    }
    for field_name, mismatch in mismatches.items():
        if mismatch:
            return _unsupported(f"feature {field_name} mismatch", config)
    if not isinstance(feature.launch_id, str) or not feature.launch_id:
        return _missing("feature launch_id is required", config)
    return None


def _validate_label(
    label: OperatorActionLabel,
    config: RuleHypothesisConfig,
) -> AbstainResult | None:
    if any(
        not _non_negative_int(value)
        for value in (label.as_of_slot, label.action_slot, label.action_index)
    ):
        return _unsupported("label slots and index must be non-negative", config)
    if label.as_of_slot > config.as_of_slot:
        return _stale("label evidence is newer than artifact as_of_slot", config)
    if not isinstance(label.action, OperatorAction):
        return _unsupported("label action is invalid", config)
    if label.labeler_version != config.labeler_version:
        return _decoder_mismatch("labeler version mismatch", config)
    if not _valid_evidence_ids(label.evidence_ids):
        return _missing("label evidence_ids are required", config)
    return _validate_label_identity(label, config)


def _validate_label_identity(
    label: OperatorActionLabel,
    config: RuleHypothesisConfig,
) -> AbstainResult | None:
    mismatches = {
        "entity_id": label.entity_id != config.entity_id,
        "campaign_id": label.campaign_id != config.campaign_id,
        "regime_id": label.regime_id != config.regime_id,
    }
    for field_name, mismatch in mismatches.items():
        if mismatch:
            return _unsupported(f"label {field_name} mismatch", config)
    if not isinstance(label.launch_id, str) or not label.launch_id:
        return _missing("label launch_id is required", config)
    return None


def _validate_row_temporal_order(
    row: StateActionTrainingRow,
    config: RuleHypothesisConfig,
) -> AbstainResult | None:
    if row.feature.launch_id != row.label.launch_id:
        return _unsupported("feature and label launch_id mismatch", config)
    if not (
        row.feature.as_of_slot
        < row.label.action_slot
        <= row.label.as_of_slot
        <= row.as_of_slot
        <= config.as_of_slot
    ):
        return _stale("row slots imply temporal leakage", config)
    return None


def _build_expression_hypothesis(
    *,
    expression_kind: RuleExpressionKind,
    rows: tuple[StateActionTrainingRow, ...],
    target_rows: tuple[StateActionTrainingRow, ...],
    config: RuleHypothesisConfig,
) -> RuleHypothesis | None:
    target_launch_count = _distinct_launch_count(target_rows)
    if target_launch_count < config.min_distinct_launch_support:
        return None

    target_values = tuple(
        _expression_value(row.feature, expression_kind) for row in target_rows
    )
    sorted_target_values = tuple(sorted(target_values))
    threshold_q10 = _quantile_value(sorted_target_values, 100_000)
    threshold_q50 = _quantile_value(sorted_target_values, 500_000)
    threshold_q90 = _quantile_value(sorted_target_values, 900_000)
    matched_rows = tuple(
        row
        for row in rows
        if _expression_value(row.feature, expression_kind) >= threshold_q50
    )
    if not matched_rows:
        return None

    matched_target_rows = tuple(
        row for row in matched_rows if row.label.action is config.target_action
    )
    matched_launch_count = _distinct_launch_count(matched_rows)
    matched_target_launch_count = _distinct_launch_count(matched_target_rows)
    precision = (
        matched_target_launch_count
        * PROBABILITY_PPM_DENOMINATOR
        // matched_launch_count
    )
    dispersion = _dispersion_ppm(
        q10_value=threshold_q10,
        q50_value=threshold_q50,
        q90_value=threshold_q90,
    )
    launch_share = (
        target_launch_count
        * PROBABILITY_PPM_DENOMINATOR
        // _distinct_launch_count(rows)
    )
    confidence = (
        precision
        * _stability_ppm(dispersion)
        // PROBABILITY_PPM_DENOMINATOR
        * launch_share
        // PROBABILITY_PPM_DENOMINATOR
    )
    action_slots = tuple(row.label.action_slot for row in target_rows)
    return RuleHypothesis(
        as_of_slot=config.as_of_slot,
        entity_id=config.entity_id,
        campaign_id=config.campaign_id,
        regime_id=config.regime_id,
        expression_kind=expression_kind,
        target_action=config.target_action,
        threshold_q10_value=threshold_q10,
        threshold_q50_value=threshold_q50,
        threshold_q90_value=threshold_q90,
        support_row_count=len(target_rows),
        distinct_launch_support=target_launch_count,
        source_row_count=len(rows),
        source_distinct_launch_count=_distinct_launch_count(rows),
        precision_ppm=precision,
        dispersion_ppm=dispersion,
        confidence_ppm=confidence,
        median_trigger_error=_median_trigger_error(
            values=sorted_target_values,
            median=threshold_q50,
        ),
        first_seen_slot=Slot(min(action_slots)),
        last_seen_slot=Slot(max(action_slots)),
        evidence_ids=_merged_evidence_ids(target_rows),
        generator_version=config.generator_version,
        feature_schema_version=config.feature_schema_version,
        labeler_version=config.labeler_version,
        row_schema_version=config.row_schema_version,
        operator_profile_version=config.operator_profile_version,
        regime_model_version=config.regime_model_version,
    )


def _expression_value(
    feature: TriggerFeatureSnapshot,
    expression_kind: RuleExpressionKind,
) -> int:
    values = {
        RuleExpressionKind.ELAPSED_MS_AT_OR_ABOVE: feature.elapsed_ms,
        RuleExpressionKind.QUOTE_RESERVE_AT_OR_ABOVE: (
            feature.quote_reserve_base_units
        ),
        RuleExpressionKind.CURVE_PROGRESS_AT_OR_ABOVE: feature.curve_progress_ppm,
        RuleExpressionKind.OPERATOR_PNL_AT_OR_ABOVE: feature.operator_pnl_lamports,
        RuleExpressionKind.INDEPENDENT_BUYER_COUNT_AT_OR_ABOVE: (
            feature.independent_buyer_count
        ),
        RuleExpressionKind.TIME_SINCE_LAST_INDEPENDENT_BUY_AT_OR_ABOVE: (
            feature.time_since_last_independent_buy_ms
        ),
    }
    return values[expression_kind]


def _validate_evaluation_inputs(
    *,
    feature: TriggerFeatureSnapshot,
    artifact: RuleHypothesisArtifact,
    thresholds: ObservedTriggerEvaluationThresholds,
) -> AbstainResult | None:
    artifact_error = _validate_artifact(artifact)
    if artifact_error is not None:
        return artifact_error
    threshold_error = _validate_thresholds(thresholds, artifact)
    if threshold_error is not None:
        return threshold_error
    feature_config = _config_from_artifact(artifact)
    feature_error = _validate_feature(feature, feature_config)
    if feature_error is not None:
        return feature_error
    if feature.as_of_slot != artifact.as_of_slot:
        return _stale("live feature uses a different as_of_slot", artifact)
    return None


def _validate_artifact(artifact: RuleHypothesisArtifact) -> AbstainResult | None:
    for validation in (
        _validate_artifact_metadata,
        _validate_artifact_counts,
        _validate_artifact_hypotheses,
    ):
        validation_error = validation(artifact)
        if validation_error is not None:
            return validation_error
    return None


def _validate_artifact_metadata(
    artifact: RuleHypothesisArtifact,
) -> AbstainResult | None:
    if not _non_negative_int(artifact.as_of_slot):
        return _unsupported("artifact as_of_slot must be non-negative", artifact)
    if not isinstance(artifact.target_action, OperatorAction):
        return _unsupported("artifact target_action is invalid", artifact)
    missing_id = _require_strings(
        artifact,
        {
            "entity_id": artifact.entity_id,
            "campaign_id": artifact.campaign_id,
            "regime_id": artifact.regime_id,
        },
        AbstainReason.MISSING_FEATURE,
    )
    if missing_id is not None:
        return missing_id
    version_error = _require_strings(
        artifact,
        _artifact_version_fields(artifact),
        AbstainReason.DECODER_MISMATCH,
    )
    if version_error is not None:
        return version_error
    return None


def _validate_artifact_hypotheses(
    artifact: RuleHypothesisArtifact,
) -> AbstainResult | None:
    for hypothesis in artifact.hypotheses:
        hypothesis_error = _validate_hypothesis(hypothesis, artifact)
        if hypothesis_error is not None:
            return hypothesis_error
    return None


def _validate_artifact_counts(
    artifact: RuleHypothesisArtifact,
) -> AbstainResult | None:
    counts = (
        artifact.source_row_count,
        artifact.source_distinct_launch_count,
        artifact.accepted_hypothesis_count,
        artifact.skipped_expression_count,
        artifact.min_distinct_launch_support,
    )
    if any(not _non_negative_int(count) for count in counts):
        return _unsupported("artifact counts must be non-negative", artifact)
    if not _positive_int(artifact.min_distinct_launch_support):
        return _unsupported(
            "artifact min_distinct_launch_support must be positive", artifact
        )
    if type(artifact.hypotheses) is not tuple or not artifact.hypotheses:
        return _missing("artifact hypotheses are required", artifact)
    if artifact.accepted_hypothesis_count != len(artifact.hypotheses):
        return _unsupported("accepted_hypothesis_count mismatch", artifact)
    if not _valid_evidence_ids(artifact.reason_codes):
        return _missing("artifact reason_codes are required", artifact)
    return _validate_probability_fields(
        artifact,
        {
            "min_precision_ppm": artifact.min_precision_ppm,
            "min_confidence_ppm": artifact.min_confidence_ppm,
        },
    )


def _validate_hypothesis(
    hypothesis: RuleHypothesis,
    artifact: RuleHypothesisArtifact,
) -> AbstainResult | None:
    for validation in (
        _validate_hypothesis_identity,
        _validate_hypothesis_kind,
        _validate_hypothesis_metrics,
        _validate_hypothesis_versions,
    ):
        validation_error = validation(hypothesis, artifact)
        if validation_error is not None:
            return validation_error
    return None


def _validate_hypothesis_identity(
    hypothesis: RuleHypothesis,
    artifact: RuleHypothesisArtifact,
) -> AbstainResult | None:
    if hypothesis.as_of_slot != artifact.as_of_slot:
        return _stale("hypothesis uses a different as_of_slot", artifact)
    if hypothesis.entity_id != artifact.entity_id:
        return _unsupported("hypothesis entity_id mismatch", artifact)
    if hypothesis.campaign_id != artifact.campaign_id:
        return _unsupported("hypothesis campaign_id mismatch", artifact)
    if hypothesis.regime_id != artifact.regime_id:
        return _unsupported("hypothesis regime_id mismatch", artifact)
    return None


def _validate_hypothesis_kind(
    hypothesis: RuleHypothesis,
    artifact: RuleHypothesisArtifact,
) -> AbstainResult | None:
    if not isinstance(hypothesis.expression_kind, RuleExpressionKind):
        return _unsupported("hypothesis expression_kind is invalid", artifact)
    if hypothesis.target_action is not artifact.target_action:
        return _unsupported("hypothesis target_action mismatch", artifact)
    return None


def _validate_hypothesis_metrics(
    hypothesis: RuleHypothesis,
    artifact: RuleHypothesisArtifact,
) -> AbstainResult | None:
    threshold_error = _validate_hypothesis_thresholds(hypothesis, artifact)
    if threshold_error is not None:
        return threshold_error
    support_error = _validate_hypothesis_support(hypothesis, artifact)
    if support_error is not None:
        return support_error
    probability_error = _validate_probability_fields(
        artifact,
        {
            "precision_ppm": hypothesis.precision_ppm,
            "dispersion_ppm": hypothesis.dispersion_ppm,
            "confidence_ppm": hypothesis.confidence_ppm,
        },
    )
    if probability_error is not None:
        return probability_error
    gate_error = _validate_hypothesis_publication_gates(hypothesis, artifact)
    if gate_error is not None:
        return gate_error
    return None


def _validate_hypothesis_publication_gates(
    hypothesis: RuleHypothesis,
    artifact: RuleHypothesisArtifact,
) -> AbstainResult | None:
    if hypothesis.precision_ppm < artifact.min_precision_ppm:
        return _unsupported("hypothesis below precision publication gate", artifact)
    if hypothesis.confidence_ppm < artifact.min_confidence_ppm:
        return _unsupported("hypothesis below confidence publication gate", artifact)
    return None


def _validate_hypothesis_thresholds(
    hypothesis: RuleHypothesis,
    artifact: RuleHypothesisArtifact,
) -> AbstainResult | None:
    threshold_values = (
        hypothesis.threshold_q10_value,
        hypothesis.threshold_q50_value,
        hypothesis.threshold_q90_value,
    )
    if any(not _non_negative_int(value) for value in threshold_values):
        return _unsupported("hypothesis thresholds must be non-negative", artifact)
    if not (
        hypothesis.threshold_q10_value
        <= hypothesis.threshold_q50_value
        <= hypothesis.threshold_q90_value
    ):
        return _unsupported("hypothesis thresholds must be monotonic", artifact)
    return None


def _validate_hypothesis_support(
    hypothesis: RuleHypothesis,
    artifact: RuleHypothesisArtifact,
) -> AbstainResult | None:
    for validation in (
        _validate_hypothesis_support_numbers,
        _validate_hypothesis_source_counts,
        _validate_hypothesis_seen_slots,
        _validate_hypothesis_support_gate,
    ):
        validation_error = validation(hypothesis, artifact)
        if validation_error is not None:
            return validation_error
    return None


def _validate_hypothesis_support_numbers(
    hypothesis: RuleHypothesis,
    artifact: RuleHypothesisArtifact,
) -> AbstainResult | None:
    counts = (
        hypothesis.support_row_count,
        hypothesis.distinct_launch_support,
        hypothesis.source_row_count,
        hypothesis.source_distinct_launch_count,
        hypothesis.median_trigger_error,
        hypothesis.first_seen_slot,
        hypothesis.last_seen_slot,
    )
    if any(not _non_negative_int(value) for value in counts):
        return _unsupported("hypothesis counts must be non-negative", artifact)
    return None


def _validate_hypothesis_source_counts(
    hypothesis: RuleHypothesis,
    artifact: RuleHypothesisArtifact,
) -> AbstainResult | None:
    if hypothesis.distinct_launch_support > hypothesis.source_distinct_launch_count:
        return _unsupported("distinct launch support exceeds source count", artifact)
    if hypothesis.distinct_launch_support > hypothesis.support_row_count:
        return _unsupported(
            "distinct launch support exceeds support row count",
            artifact,
        )
    if hypothesis.support_row_count > hypothesis.source_row_count:
        return _unsupported("support row count exceeds source row count", artifact)
    if hypothesis.source_row_count != artifact.source_row_count:
        return _unsupported("hypothesis source_row_count mismatch", artifact)
    if hypothesis.source_distinct_launch_count != artifact.source_distinct_launch_count:
        return _unsupported(
            "hypothesis source_distinct_launch_count mismatch", artifact
        )
    return None


def _validate_hypothesis_seen_slots(
    hypothesis: RuleHypothesis,
    artifact: RuleHypothesisArtifact,
) -> AbstainResult | None:
    if hypothesis.first_seen_slot > hypothesis.last_seen_slot:
        return _unsupported("hypothesis seen slot range is invalid", artifact)
    if hypothesis.last_seen_slot > artifact.as_of_slot:
        return _stale("hypothesis evidence is newer than artifact as_of_slot", artifact)
    return None


def _validate_hypothesis_support_gate(
    hypothesis: RuleHypothesis,
    artifact: RuleHypothesisArtifact,
) -> AbstainResult | None:
    if hypothesis.distinct_launch_support < artifact.min_distinct_launch_support:
        return _unsupported("hypothesis below distinct launch support gate", artifact)
    return None


def _validate_hypothesis_versions(
    hypothesis: RuleHypothesis,
    artifact: RuleHypothesisArtifact,
) -> AbstainResult | None:
    if not _valid_evidence_ids(hypothesis.evidence_ids):
        return _missing("hypothesis evidence_ids are required", artifact)
    version_error = _require_strings(
        artifact,
        {
            "generator_version": hypothesis.generator_version,
            "feature_schema_version": hypothesis.feature_schema_version,
            "labeler_version": hypothesis.labeler_version,
            "row_schema_version": hypothesis.row_schema_version,
            "operator_profile_version": hypothesis.operator_profile_version,
            "regime_model_version": hypothesis.regime_model_version,
        },
        AbstainReason.DECODER_MISMATCH,
    )
    if version_error is not None:
        return version_error
    if _hypothesis_versions(hypothesis) != _artifact_versions(artifact):
        return _decoder_mismatch("hypothesis version metadata mismatch", artifact)
    return None


def _validate_thresholds(
    thresholds: ObservedTriggerEvaluationThresholds,
    artifact: RuleHypothesisArtifact,
) -> AbstainResult | None:
    if thresholds.as_of_slot != artifact.as_of_slot:
        return _stale("evaluation thresholds use a different as_of_slot", artifact)
    return _validate_probability_fields(
        artifact,
        {
            "min_confidence_ppm": thresholds.min_confidence_ppm,
            "min_trigger_risk_ppm": thresholds.min_trigger_risk_ppm,
        },
    )


def _match_hypothesis(
    *,
    feature: TriggerFeatureSnapshot,
    hypothesis: RuleHypothesis,
) -> RuleHypothesisMatch:
    observed_value = _expression_value(feature, hypothesis.expression_kind)
    proximity = _proximity_ppm(observed_value, hypothesis)
    risk = hypothesis.confidence_ppm * proximity // PROBABILITY_PPM_DENOMINATOR
    return RuleHypothesisMatch(
        as_of_slot=feature.as_of_slot,
        entity_id=feature.entity_id,
        campaign_id=feature.campaign_id,
        regime_id=feature.regime_id,
        expression_kind=hypothesis.expression_kind,
        target_action=hypothesis.target_action,
        status=_match_status(observed_value, hypothesis),
        observed_value=observed_value,
        threshold_q50_value=hypothesis.threshold_q50_value,
        proximity_ppm=proximity,
        trigger_risk_ppm=risk,
        confidence_ppm=hypothesis.confidence_ppm,
        precision_ppm=hypothesis.precision_ppm,
        generator_version=hypothesis.generator_version,
        feature_schema_version=hypothesis.feature_schema_version,
        labeler_version=hypothesis.labeler_version,
        row_schema_version=hypothesis.row_schema_version,
        operator_profile_version=hypothesis.operator_profile_version,
        regime_model_version=hypothesis.regime_model_version,
    )


def _match_status(
    observed_value: int,
    hypothesis: RuleHypothesis,
) -> TriggerMatchStatus:
    if observed_value < hypothesis.threshold_q10_value:
        return TriggerMatchStatus.BELOW_OBSERVED_RANGE
    if observed_value < hypothesis.threshold_q50_value:
        return TriggerMatchStatus.APPROACHING_OBSERVED_TRIGGER
    if observed_value <= hypothesis.threshold_q90_value:
        return TriggerMatchStatus.INSIDE_OBSERVED_BAND
    return TriggerMatchStatus.BEYOND_OBSERVED_BAND


def _proximity_ppm(observed_value: int, hypothesis: RuleHypothesis) -> int:
    if observed_value < hypothesis.threshold_q10_value:
        return min(
            499_999,
            observed_value * 500_000 // max(1, hypothesis.threshold_q10_value),
        )
    if observed_value < hypothesis.threshold_q50_value:
        return 500_000 + (
            (observed_value - hypothesis.threshold_q10_value)
            * 500_000
            // max(1, hypothesis.threshold_q50_value - hypothesis.threshold_q10_value)
        )
    return PROBABILITY_PPM_DENOMINATOR


def _feature_numeric_values(feature: TriggerFeatureSnapshot) -> tuple[int, ...]:
    return (
        feature.as_of_slot,
        feature.elapsed_ms,
        feature.quote_reserve_base_units,
        feature.curve_progress_ppm,
        feature.operator_pnl_lamports,
        feature.independent_buyer_count,
        feature.time_since_last_independent_buy_ms,
    )


def _quantile_value(values: tuple[int, ...], quantile_ppm: int) -> int:
    index = (
        (len(values) - 1) * quantile_ppm + PROBABILITY_PPM_DENOMINATOR // 2
    ) // PROBABILITY_PPM_DENOMINATOR
    return values[index]


def _median_trigger_error(*, values: tuple[int, ...], median: int) -> int:
    errors = tuple(sorted(abs(value - median) for value in values))
    return _quantile_value(errors, 500_000)


def _dispersion_ppm(*, q10_value: int, q50_value: int, q90_value: int) -> int:
    width = q90_value - q10_value
    denominator = max(1, q50_value)
    return min(
        PROBABILITY_PPM_DENOMINATOR,
        width * PROBABILITY_PPM_DENOMINATOR // denominator,
    )


def _stability_ppm(dispersion_ppm: int) -> int:
    return max(0, PROBABILITY_PPM_DENOMINATOR - dispersion_ppm)


def _distinct_launch_count(rows: tuple[StateActionTrainingRow, ...]) -> int:
    return len({row.feature.launch_id for row in rows})


def _has_duplicate_launches(rows: tuple[StateActionTrainingRow, ...]) -> bool:
    return _distinct_launch_count(rows) != len(rows)


def _merged_evidence_ids(rows: tuple[StateActionTrainingRow, ...]) -> tuple[str, ...]:
    return tuple(
        sorted(
            {
                evidence_id
                for row in rows
                for evidence_id in (
                    row.evidence_ids + row.feature.evidence_ids + row.label.evidence_ids
                )
            }
        )
    )


def _config_version_fields(config: RuleHypothesisConfig) -> dict[str, str]:
    return {
        "generator_version": config.generator_version,
        "feature_schema_version": config.feature_schema_version,
        "labeler_version": config.labeler_version,
        "row_schema_version": config.row_schema_version,
        "operator_profile_version": config.operator_profile_version,
        "regime_model_version": config.regime_model_version,
    }


def _artifact_version_fields(artifact: RuleHypothesisArtifact) -> dict[str, str]:
    return {
        "generator_version": artifact.generator_version,
        "feature_schema_version": artifact.feature_schema_version,
        "labeler_version": artifact.labeler_version,
        "row_schema_version": artifact.row_schema_version,
        "operator_profile_version": artifact.operator_profile_version,
        "regime_model_version": artifact.regime_model_version,
    }


def _hypothesis_versions(hypothesis: RuleHypothesis) -> tuple[str, ...]:
    return (
        hypothesis.generator_version,
        hypothesis.feature_schema_version,
        hypothesis.labeler_version,
        hypothesis.row_schema_version,
        hypothesis.operator_profile_version,
        hypothesis.regime_model_version,
    )


def _artifact_versions(artifact: RuleHypothesisArtifact) -> tuple[str, ...]:
    return (
        artifact.generator_version,
        artifact.feature_schema_version,
        artifact.labeler_version,
        artifact.row_schema_version,
        artifact.operator_profile_version,
        artifact.regime_model_version,
    )


def _config_from_artifact(artifact: RuleHypothesisArtifact) -> RuleHypothesisConfig:
    return RuleHypothesisConfig(
        as_of_slot=artifact.as_of_slot,
        entity_id=artifact.entity_id,
        campaign_id=artifact.campaign_id,
        regime_id=artifact.regime_id,
        target_action=artifact.target_action,
        generator_version=artifact.generator_version,
        feature_schema_version=artifact.feature_schema_version,
        labeler_version=artifact.labeler_version,
        row_schema_version=artifact.row_schema_version,
        operator_profile_version=artifact.operator_profile_version,
        regime_model_version=artifact.regime_model_version,
        min_distinct_launch_support=1,
        min_precision_ppm=0,
        min_confidence_ppm=0,
    )


def _require_strings(
    context: RuleHypothesisConfig | RuleHypothesisArtifact,
    fields: dict[str, str],
    reason: AbstainReason,
) -> AbstainResult | None:
    for field_name, value in fields.items():
        if not isinstance(value, str) or not value:
            return _abstain(
                reason=reason,
                message=f"{field_name} is required",
                as_of_slot=context.as_of_slot,
            )
    return None


def _validate_probability_fields(
    context: RuleHypothesisConfig | RuleHypothesisArtifact,
    fields: dict[str, int],
) -> AbstainResult | None:
    for field_name, value in fields.items():
        if not _valid_probability_ppm(value):
            return _unsupported(
                f"{field_name} must be in probability ppm range",
                context,
            )
    return None


def _valid_probability_ppm(value: object) -> bool:
    return _non_negative_int(value) and value <= PROBABILITY_PPM_DENOMINATOR


def _valid_evidence_ids(evidence_ids: object) -> bool:
    return (
        type(evidence_ids) is tuple
        and bool(evidence_ids)
        and all(
            isinstance(evidence_id, str) and evidence_id for evidence_id in evidence_ids
        )
    )


def _positive_int(value: object) -> bool:
    return type(value) is int and value > 0


def _non_negative_int(value: object) -> bool:
    return type(value) is int and value >= 0


def _missing(
    message: str,
    context: RuleHypothesisConfig | RuleHypothesisArtifact,
) -> AbstainResult:
    return _abstain(
        reason=AbstainReason.MISSING_FEATURE,
        message=message,
        as_of_slot=context.as_of_slot,
    )


def _decoder_mismatch(
    message: str,
    context: RuleHypothesisConfig | RuleHypothesisArtifact,
) -> AbstainResult:
    return _abstain(
        reason=AbstainReason.DECODER_MISMATCH,
        message=message,
        as_of_slot=context.as_of_slot,
    )


def _stale(
    message: str,
    context: RuleHypothesisConfig | RuleHypothesisArtifact,
) -> AbstainResult:
    return _abstain(
        reason=AbstainReason.STALE_STATE,
        message=message,
        as_of_slot=context.as_of_slot,
    )


def _unsupported(
    message: str,
    context: RuleHypothesisConfig | RuleHypothesisArtifact,
) -> AbstainResult:
    return _abstain(
        reason=AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
        message=message,
        as_of_slot=context.as_of_slot,
    )


def _abstain(
    *,
    reason: AbstainReason,
    message: str,
    as_of_slot: object,
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
