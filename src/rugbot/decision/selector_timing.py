"""Pure selector and timing snapshot builders."""

from dataclasses import dataclass
from enum import Enum

from rugbot.decision.snapshots import (
    LaunchMatcherSnapshot,
    RuggerSelectorSnapshot,
    RugTimingSnapshot,
)
from rugbot.domain.amounts import Slot
from rugbot.domain.decisions import AbstainReason, AbstainResult
from rugbot.graph.entity_resolution import AddressRole
from rugbot.graph.point_in_time import PROBABILITY_PPM_DENOMINATOR
from rugbot.graph.wallet_churn import (
    HIGH_RISK_CHURN_ROLES,
    OperatorWalletChurnSnapshot,
    WalletChurnAddress,
    WalletChurnStatus,
)
from rugbot.models.rule_hypotheses import (
    ObservedTriggerEvaluation,
    OperatorAction,
    RuleExpressionKind,
    RuleHypothesisMatch,
    TriggerMatchStatus,
)

TIMING_MAX_HORIZON_MS = 10_000
REQUIRED_TIMING_HORIZONS_MS = (1_000, 3_000, 5_000, TIMING_MAX_HORIZON_MS)
Q50_PROBABILITY_PPM = 500_000


class SelectorDecisionReason(Enum):
    """Selector pass/fail reason codes."""

    SELECTOR_PASSED = "selector_passed"
    ENTITY_PROBABILITY_BELOW_THRESHOLD = "entity_probability_below_threshold"
    REGIME_PROBABILITY_BELOW_THRESHOLD = "regime_probability_below_threshold"
    HISTORICAL_SUPPORT_BELOW_THRESHOLD = "historical_support_below_threshold"
    TRIGGER_RISK_BELOW_THRESHOLD = "trigger_risk_below_threshold"
    OPERATOR_CHURN_NEW_HIGH_RISK_ROLES_ABOVE_CAP = (
        "operator_churn_new_high_risk_roles_above_cap"
    )
    OPERATOR_CHURN_ADDRESS_TURNOVER_ABOVE_CAP = (
        "operator_churn_address_turnover_above_cap"
    )
    OPERATOR_CHURN_RETAINED_ROLE_CHANGES_ABOVE_CAP = (
        "operator_churn_retained_role_changes_above_cap"
    )


@dataclass(frozen=True, slots=True)
class SelectorSupportEvidence:
    """Point-in-time selector support for one matched entity/regime."""

    as_of_slot: Slot
    entity_id: str
    regime_id: str
    historical_launch_count: int
    support_snapshot_version: str
    operator_profile_version: str
    regime_model_version: str
    evidence_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class RuggerSelectorConfig:
    """Thresholds and versions for selecting a known operator launch."""

    as_of_slot: Slot
    selector_version: str
    target_action: OperatorAction
    min_entity_probability_ppm: int
    min_regime_probability_ppm: int
    min_historical_launches: int
    min_trigger_risk_ppm: int


@dataclass(frozen=True, slots=True)
class OperatorChurnSelectorPolicy:
    """Caps for treating wallet churn as a known-operator uncertainty gate."""

    require_churn_snapshot: bool
    accepted_churn_snapshot_versions: tuple[str, ...]
    max_new_high_risk_roles: int
    max_address_turnover_ppm: int
    max_retained_role_changes: int


@dataclass(frozen=True, slots=True)
class OperatorChurnSelectorGate:
    """Point-in-time churn input and policy for one selector evaluation."""

    operator_churn: OperatorWalletChurnSnapshot | None
    policy: OperatorChurnSelectorPolicy


@dataclass(frozen=True, slots=True)
class DiscreteHazardBin:
    """One integer discrete-time dump hazard bin."""

    as_of_slot: Slot
    horizon_ms: int
    hazard_ppm: int


@dataclass(frozen=True, slots=True)
class DumpHazardForecast:
    """Precomputed point-in-time discrete hazard forecast."""

    as_of_slot: Slot
    timing_model_version: str
    forecast_snapshot_version: str
    bins: tuple[DiscreteHazardBin, ...]
    evidence_ids: tuple[str, ...]


def build_rugger_selector_snapshot(
    *,
    matcher: LaunchMatcherSnapshot,
    trigger: ObservedTriggerEvaluation,
    support: SelectorSupportEvidence,
    config: RuggerSelectorConfig,
    operator_churn_gate: OperatorChurnSelectorGate | None = None,
) -> RuggerSelectorSnapshot | AbstainResult:
    """Build a selector snapshot from validated matcher and trigger artifacts."""

    validation_error = _validate_selector_inputs(
        matcher=matcher,
        trigger=trigger,
        support=support,
        config=config,
        operator_churn_gate=operator_churn_gate,
    )
    if validation_error is not None:
        return validation_error

    reason = _selector_reason(
        matcher=matcher,
        trigger=trigger,
        support=support,
        config=config,
        operator_churn_gate=operator_churn_gate,
    )
    operator_churn = (
        operator_churn_gate.operator_churn if operator_churn_gate is not None else None
    )
    churn_policy = (
        operator_churn_gate.policy if operator_churn_gate is not None else None
    )
    churn_audit_enabled = operator_churn is not None and churn_policy is not None
    return RuggerSelectorSnapshot(
        as_of_slot=config.as_of_slot,
        selector_version=config.selector_version,
        is_selected=reason is SelectorDecisionReason.SELECTOR_PASSED,
        min_entity_probability_ppm=config.min_entity_probability_ppm,
        min_regime_probability_ppm=config.min_regime_probability_ppm,
        min_trigger_risk_ppm=config.min_trigger_risk_ppm,
        max_trigger_risk_ppm=trigger.max_trigger_risk_ppm,
        min_historical_launches=config.min_historical_launches,
        historical_launch_count=support.historical_launch_count,
        trigger_generator_version=trigger.generator_version,
        trigger_feature_schema_version=trigger.feature_schema_version,
        trigger_labeler_version=trigger.labeler_version,
        trigger_row_schema_version=trigger.row_schema_version,
        trigger_market_state_snapshot_version=trigger.market_state_snapshot_version,
        trigger_operator_profile_version=trigger.operator_profile_version,
        trigger_regime_model_version=trigger.regime_model_version,
        reason_codes=(reason.value,),
        operator_churn_snapshot_version=(
            operator_churn.churn_snapshot_version if churn_audit_enabled else None
        ),
        max_operator_churn_new_high_risk_roles=(
            churn_policy.max_new_high_risk_roles if churn_audit_enabled else None
        ),
        observed_operator_churn_new_high_risk_roles=(
            operator_churn.new_high_risk_role_count if churn_audit_enabled else None
        ),
        max_operator_churn_address_turnover_ppm=(
            churn_policy.max_address_turnover_ppm if churn_audit_enabled else None
        ),
        observed_operator_churn_address_turnover_ppm=(
            operator_churn.address_turnover_ppm if churn_audit_enabled else None
        ),
        max_operator_churn_retained_role_changes=(
            churn_policy.max_retained_role_changes if churn_audit_enabled else None
        ),
        observed_operator_churn_retained_role_changes=(
            operator_churn.retained_role_change_count if churn_audit_enabled else None
        ),
    )


def build_rug_timing_snapshot(
    *,
    forecast: DumpHazardForecast,
) -> RugTimingSnapshot | AbstainResult:
    """Build a timing snapshot from an already computed hazard forecast."""

    validation_error = _validate_timing_forecast(forecast)
    if validation_error is not None:
        return validation_error
    return RugTimingSnapshot(
        as_of_slot=forecast.as_of_slot,
        timing_model_version=forecast.timing_model_version,
        p_dump_next_1s_ppm=_cumulative_dump_probability(forecast.bins, 1_000),
        p_dump_next_3s_ppm=_cumulative_dump_probability(forecast.bins, 3_000),
        p_dump_next_5s_ppm=_cumulative_dump_probability(forecast.bins, 5_000),
        p_dump_next_10s_ppm=_cumulative_dump_probability(forecast.bins, 10_000),
        q05_remaining_dump_time_ms=_quantile_time_ms(forecast.bins, 50_000),
        q10_remaining_dump_time_ms=_quantile_time_ms(forecast.bins, 100_000),
        q50_remaining_dump_time_ms=_quantile_time_ms(forecast.bins, 500_000),
    )


def _validate_selector_inputs(
    *,
    matcher: LaunchMatcherSnapshot,
    trigger: ObservedTriggerEvaluation,
    support: SelectorSupportEvidence,
    config: RuggerSelectorConfig,
    operator_churn_gate: OperatorChurnSelectorGate | None,
) -> AbstainResult | None:
    config_error = _validate_selector_config(config)
    if config_error is not None:
        return config_error
    matcher_error = _validate_matcher(matcher, config)
    if matcher_error is not None:
        return matcher_error
    support_error = _validate_support(support, matcher, config)
    if support_error is not None:
        return support_error
    trigger_error = _validate_trigger(trigger, matcher, config)
    if trigger_error is not None:
        return trigger_error
    return _validate_operator_churn(
        operator_churn_gate=operator_churn_gate,
        matcher=matcher,
        config=config,
    )


def _validate_selector_config(config: RuggerSelectorConfig) -> AbstainResult | None:
    if not _non_negative_int(config.as_of_slot):
        return _unsupported(
            "selector as_of_slot must be non-negative", config.as_of_slot
        )
    if not isinstance(config.selector_version, str) or not config.selector_version:
        return _decoder_mismatch("selector_version is required", config.as_of_slot)
    if not isinstance(config.target_action, OperatorAction):
        return _unsupported("selector target_action is invalid", config.as_of_slot)
    if not _non_negative_int(config.min_historical_launches):
        return _unsupported(
            "min_historical_launches must be non-negative", config.as_of_slot
        )
    return _validate_probability_fields(
        config.as_of_slot,
        {
            "min_entity_probability_ppm": config.min_entity_probability_ppm,
            "min_regime_probability_ppm": config.min_regime_probability_ppm,
            "min_trigger_risk_ppm": config.min_trigger_risk_ppm,
        },
    )


def _validate_matcher(
    matcher: LaunchMatcherSnapshot,
    config: RuggerSelectorConfig,
) -> AbstainResult | None:
    if matcher.as_of_slot != config.as_of_slot:
        return _stale("matcher snapshot uses a different as_of_slot", config.as_of_slot)
    missing_error = _require_features(
        config.as_of_slot,
        {
            "entity_id": matcher.entity_id,
            "regime_id": matcher.regime_id,
        },
    )
    if missing_error is not None:
        return missing_error
    version_error = _require_versions(
        config.as_of_slot,
        {
            "entity_graph_snapshot_version": matcher.entity_graph_snapshot_version,
            "operator_profile_version": matcher.operator_profile_version,
            "regime_model_version": matcher.regime_model_version,
            "matcher_version": matcher.matcher_version,
        },
    )
    if version_error is not None:
        return version_error
    return _validate_probability_fields(
        config.as_of_slot,
        {
            "entity_probability_ppm": matcher.entity_probability_ppm,
            "regime_probability_ppm": matcher.regime_probability_ppm,
        },
    )


def _validate_support(
    support: SelectorSupportEvidence,
    matcher: LaunchMatcherSnapshot,
    config: RuggerSelectorConfig,
) -> AbstainResult | None:
    identity_error = _validate_support_identity(support, matcher, config)
    if identity_error is not None:
        return identity_error
    version_error = _validate_support_versions(support, matcher, config)
    if version_error is not None:
        return version_error
    return _validate_support_provenance(support, config)


def _validate_support_identity(
    support: SelectorSupportEvidence,
    matcher: LaunchMatcherSnapshot,
    config: RuggerSelectorConfig,
) -> AbstainResult | None:
    if support.as_of_slot != config.as_of_slot:
        return _stale("selector support uses a different as_of_slot", config.as_of_slot)
    if support.entity_id != matcher.entity_id:
        return _unsupported("selector support entity_id mismatch", config.as_of_slot)
    if support.regime_id != matcher.regime_id:
        return _unsupported("selector support regime_id mismatch", config.as_of_slot)
    if not _non_negative_int(support.historical_launch_count):
        return _unsupported(
            "historical_launch_count must be non-negative", config.as_of_slot
        )
    return None


def _validate_support_versions(
    support: SelectorSupportEvidence,
    matcher: LaunchMatcherSnapshot,
    config: RuggerSelectorConfig,
) -> AbstainResult | None:
    version_error = _require_versions(
        config.as_of_slot,
        {
            "support_snapshot_version": support.support_snapshot_version,
            "operator_profile_version": support.operator_profile_version,
            "regime_model_version": support.regime_model_version,
        },
    )
    if version_error is not None:
        return version_error
    if support.operator_profile_version != matcher.operator_profile_version:
        return _decoder_mismatch(
            "selector support profile version mismatch", config.as_of_slot
        )
    if support.regime_model_version != matcher.regime_model_version:
        return _decoder_mismatch(
            "selector support regime version mismatch", config.as_of_slot
        )
    return None


def _validate_support_provenance(
    support: SelectorSupportEvidence,
    config: RuggerSelectorConfig,
) -> AbstainResult | None:
    if not _valid_evidence_ids(support.evidence_ids):
        return _missing("selector support evidence_ids are required", config.as_of_slot)
    return None


def _validate_trigger(
    trigger: ObservedTriggerEvaluation,
    matcher: LaunchMatcherSnapshot,
    config: RuggerSelectorConfig,
) -> AbstainResult | None:
    for validation in (
        _validate_trigger_identity,
        _validate_trigger_versions,
        _validate_trigger_metrics,
        _validate_trigger_matches,
    ):
        validation_error = validation(trigger, matcher, config)
        if validation_error is not None:
            return validation_error
    return None


def _validate_trigger_identity(
    trigger: ObservedTriggerEvaluation,
    matcher: LaunchMatcherSnapshot,
    config: RuggerSelectorConfig,
) -> AbstainResult | None:
    if trigger.as_of_slot != config.as_of_slot:
        return _stale(
            "trigger evaluation uses a different as_of_slot", config.as_of_slot
        )
    if trigger.entity_id != matcher.entity_id:
        return _unsupported("trigger entity_id mismatch", config.as_of_slot)
    if trigger.regime_id != matcher.regime_id:
        return _unsupported("trigger regime_id mismatch", config.as_of_slot)
    if trigger.target_action is not config.target_action:
        return _unsupported("trigger target_action mismatch", config.as_of_slot)
    return None


def _validate_trigger_versions(
    trigger: ObservedTriggerEvaluation,
    matcher: LaunchMatcherSnapshot,
    config: RuggerSelectorConfig,
) -> AbstainResult | None:
    version_error = _require_versions(
        config.as_of_slot,
        {
            "generator_version": trigger.generator_version,
            "feature_schema_version": trigger.feature_schema_version,
            "labeler_version": trigger.labeler_version,
            "row_schema_version": trigger.row_schema_version,
            "market_state_snapshot_version": trigger.market_state_snapshot_version,
            "operator_profile_version": trigger.operator_profile_version,
            "regime_model_version": trigger.regime_model_version,
        },
    )
    if version_error is not None:
        return version_error
    if trigger.operator_profile_version != matcher.operator_profile_version:
        return _decoder_mismatch("trigger profile version mismatch", config.as_of_slot)
    if trigger.regime_model_version != matcher.regime_model_version:
        return _decoder_mismatch("trigger regime version mismatch", config.as_of_slot)
    return None


def _validate_trigger_metrics(
    trigger: ObservedTriggerEvaluation,
    matcher: LaunchMatcherSnapshot,
    config: RuggerSelectorConfig,
) -> AbstainResult | None:
    del matcher
    if not _valid_probability_ppm(trigger.max_trigger_risk_ppm):
        return _unsupported("max_trigger_risk_ppm is invalid", config.as_of_slot)
    if not _valid_evidence_ids(trigger.reason_codes):
        return _missing("trigger reason_codes are required", config.as_of_slot)
    return None


def _validate_trigger_matches(
    trigger: ObservedTriggerEvaluation,
    matcher: LaunchMatcherSnapshot,
    config: RuggerSelectorConfig,
) -> AbstainResult | None:
    if type(trigger.matches) is not tuple or not trigger.matches:
        return _missing("trigger matches are required", config.as_of_slot)
    for match in trigger.matches:
        match_error = _validate_trigger_match(match, trigger, matcher, config)
        if match_error is not None:
            return match_error
    max_match_risk = max(match.trigger_risk_ppm for match in trigger.matches)
    if trigger.max_trigger_risk_ppm != max_match_risk:
        return _unsupported(
            "trigger max risk does not match trigger matches", config.as_of_slot
        )
    return None


def _validate_trigger_match(
    match: RuleHypothesisMatch,
    trigger: ObservedTriggerEvaluation,
    matcher: LaunchMatcherSnapshot,
    config: RuggerSelectorConfig,
) -> AbstainResult | None:
    identity_error = _validate_trigger_match_identity(match, trigger, config)
    if identity_error is not None:
        return identity_error
    version_error = _validate_trigger_match_versions(match, matcher, trigger, config)
    if version_error is not None:
        return version_error
    return _validate_trigger_match_metrics(match, config)


def _validate_trigger_match_identity(
    match: RuleHypothesisMatch,
    trigger: ObservedTriggerEvaluation,
    config: RuggerSelectorConfig,
) -> AbstainResult | None:
    slot_error = _validate_trigger_match_slot(match, config)
    if slot_error is not None:
        return slot_error
    identity_error = _validate_trigger_match_ids(match, trigger, config)
    if identity_error is not None:
        return identity_error
    return _validate_trigger_match_kind(match, config)


def _validate_trigger_match_slot(
    match: RuleHypothesisMatch,
    config: RuggerSelectorConfig,
) -> AbstainResult | None:
    if match.as_of_slot != config.as_of_slot:
        return _stale("trigger match uses a different as_of_slot", config.as_of_slot)
    return None


def _validate_trigger_match_ids(
    match: RuleHypothesisMatch,
    trigger: ObservedTriggerEvaluation,
    config: RuggerSelectorConfig,
) -> AbstainResult | None:
    if match.entity_id != trigger.entity_id:
        return _unsupported("trigger match entity_id mismatch", config.as_of_slot)
    if match.regime_id != trigger.regime_id:
        return _unsupported("trigger match regime_id mismatch", config.as_of_slot)
    if match.target_action is not trigger.target_action:
        return _unsupported("trigger match target_action mismatch", config.as_of_slot)
    return None


def _validate_trigger_match_kind(
    match: RuleHypothesisMatch,
    config: RuggerSelectorConfig,
) -> AbstainResult | None:
    if not isinstance(match.expression_kind, RuleExpressionKind):
        return _unsupported(
            "trigger match expression_kind is invalid", config.as_of_slot
        )
    if not isinstance(match.status, TriggerMatchStatus):
        return _unsupported("trigger match status is invalid", config.as_of_slot)
    return None


def _validate_trigger_match_versions(
    match: RuleHypothesisMatch,
    matcher: LaunchMatcherSnapshot,
    trigger: ObservedTriggerEvaluation,
    config: RuggerSelectorConfig,
) -> AbstainResult | None:
    del matcher
    version_error = _require_versions(
        config.as_of_slot,
        {
            "generator_version": match.generator_version,
            "feature_schema_version": match.feature_schema_version,
            "labeler_version": match.labeler_version,
            "row_schema_version": match.row_schema_version,
            "operator_profile_version": match.operator_profile_version,
            "regime_model_version": match.regime_model_version,
        },
    )
    if version_error is not None:
        return version_error
    if _match_versions(match) != _trigger_versions(trigger):
        return _decoder_mismatch("trigger match version mismatch", config.as_of_slot)
    return None


def _validate_trigger_match_metrics(
    match: RuleHypothesisMatch,
    config: RuggerSelectorConfig,
) -> AbstainResult | None:
    if not _non_negative_int(match.observed_value):
        return _unsupported(
            "trigger match observed_value must be non-negative", config.as_of_slot
        )
    if not _non_negative_int(match.threshold_q50_value):
        return _unsupported(
            "trigger match threshold must be non-negative", config.as_of_slot
        )
    return _validate_probability_fields(
        config.as_of_slot,
        {
            "proximity_ppm": match.proximity_ppm,
            "trigger_risk_ppm": match.trigger_risk_ppm,
            "confidence_ppm": match.confidence_ppm,
            "precision_ppm": match.precision_ppm,
        },
    )


def _validate_operator_churn(
    *,
    operator_churn_gate: OperatorChurnSelectorGate | None,
    matcher: LaunchMatcherSnapshot,
    config: RuggerSelectorConfig,
) -> AbstainResult | None:
    if operator_churn_gate is None:
        return None
    if not isinstance(operator_churn_gate, OperatorChurnSelectorGate):
        return _unsupported("operator churn gate is malformed", config.as_of_slot)
    churn_policy = operator_churn_gate.policy
    operator_churn = operator_churn_gate.operator_churn
    policy_error = _validate_operator_churn_policy(churn_policy, config)
    if policy_error is not None:
        return policy_error
    if operator_churn is None:
        if churn_policy.require_churn_snapshot:
            return _missing(
                "operator churn snapshot is required",
                config.as_of_slot,
            )
        return None
    return _validate_operator_churn_snapshot(
        operator_churn=operator_churn,
        churn_policy=churn_policy,
        matcher=matcher,
        config=config,
    )


def _validate_operator_churn_policy(
    churn_policy: OperatorChurnSelectorPolicy,
    config: RuggerSelectorConfig,
) -> AbstainResult | None:
    if not isinstance(churn_policy, OperatorChurnSelectorPolicy):
        return _unsupported("operator churn policy is malformed", config.as_of_slot)
    shape_error = _validate_operator_churn_policy_shape(churn_policy, config)
    if shape_error is not None:
        return shape_error
    return _validate_operator_churn_policy_caps(churn_policy, config)


def _validate_operator_churn_policy_shape(
    churn_policy: OperatorChurnSelectorPolicy,
    config: RuggerSelectorConfig,
) -> AbstainResult | None:
    if type(churn_policy.require_churn_snapshot) is not bool:
        return _unsupported(
            "operator churn require flag must be boolean",
            config.as_of_slot,
        )
    if not _valid_str_tuple(churn_policy.accepted_churn_snapshot_versions):
        return _decoder_mismatch(
            "accepted operator churn snapshot versions are required",
            config.as_of_slot,
        )
    return None


def _validate_operator_churn_policy_caps(
    churn_policy: OperatorChurnSelectorPolicy,
    config: RuggerSelectorConfig,
) -> AbstainResult | None:
    if not _valid_probability_ppm(churn_policy.max_address_turnover_ppm):
        return _unsupported(
            "operator churn address turnover cap is invalid",
            config.as_of_slot,
        )
    for field_name, value in {
        "max_new_high_risk_roles": churn_policy.max_new_high_risk_roles,
        "max_retained_role_changes": churn_policy.max_retained_role_changes,
    }.items():
        if not _non_negative_int(value):
            return _unsupported(
                f"operator churn {field_name} must be non-negative",
                config.as_of_slot,
            )
    return None


def _validate_operator_churn_snapshot(
    *,
    operator_churn: OperatorWalletChurnSnapshot,
    churn_policy: OperatorChurnSelectorPolicy,
    matcher: LaunchMatcherSnapshot,
    config: RuggerSelectorConfig,
) -> AbstainResult | None:
    if not isinstance(operator_churn, OperatorWalletChurnSnapshot):
        return _unsupported("operator churn snapshot is malformed", config.as_of_slot)
    for validation in (
        _validate_operator_churn_identity,
        _validate_operator_churn_versions,
        _validate_operator_churn_addresses,
        _validate_operator_churn_counts,
        _validate_operator_churn_provenance,
    ):
        validation_error = validation(
            operator_churn=operator_churn,
            churn_policy=churn_policy,
            matcher=matcher,
            config=config,
        )
        if validation_error is not None:
            return validation_error
    return None


def _validate_operator_churn_identity(
    *,
    operator_churn: OperatorWalletChurnSnapshot,
    churn_policy: OperatorChurnSelectorPolicy,
    matcher: LaunchMatcherSnapshot,
    config: RuggerSelectorConfig,
) -> AbstainResult | None:
    del churn_policy
    if operator_churn.as_of_slot != config.as_of_slot:
        return _stale(
            "operator churn snapshot uses a different as_of_slot",
            config.as_of_slot,
        )
    if operator_churn.entity_id != matcher.entity_id:
        return _unsupported("operator churn entity_id mismatch", config.as_of_slot)
    if not _non_negative_int(operator_churn.previous_as_of_slot):
        return _unsupported(
            "operator churn previous_as_of_slot is invalid",
            config.as_of_slot,
        )
    if operator_churn.previous_as_of_slot >= operator_churn.as_of_slot:
        return _stale(
            "operator churn previous_as_of_slot must be older",
            config.as_of_slot,
        )
    return None


def _validate_operator_churn_versions(
    *,
    operator_churn: OperatorWalletChurnSnapshot,
    churn_policy: OperatorChurnSelectorPolicy,
    matcher: LaunchMatcherSnapshot,
    config: RuggerSelectorConfig,
) -> AbstainResult | None:
    version_error = _require_versions(
        config.as_of_slot,
        {
            "churn_snapshot_version": operator_churn.churn_snapshot_version,
            "current_profile_version": operator_churn.current_profile_version,
            "previous_profile_version": operator_churn.previous_profile_version,
        },
    )
    if version_error is not None:
        return version_error
    if operator_churn.churn_snapshot_version not in (
        churn_policy.accepted_churn_snapshot_versions
    ):
        return _decoder_mismatch(
            "operator churn snapshot version is not accepted",
            config.as_of_slot,
        )
    if operator_churn.current_profile_version != matcher.operator_profile_version:
        return _decoder_mismatch(
            "operator churn current profile version mismatch",
            config.as_of_slot,
        )
    return None


def _validate_operator_churn_counts(
    *,
    operator_churn: OperatorWalletChurnSnapshot,
    churn_policy: OperatorChurnSelectorPolicy,
    matcher: LaunchMatcherSnapshot,
    config: RuggerSelectorConfig,
) -> AbstainResult | None:
    del churn_policy, matcher
    count_fields = {
        "current_active_address_count": operator_churn.current_active_address_count,
        "previous_active_address_count": operator_churn.previous_active_address_count,
        "new_address_count": operator_churn.new_address_count,
        "retained_address_count": operator_churn.retained_address_count,
        "retired_address_count": operator_churn.retired_address_count,
        "new_high_risk_role_count": operator_churn.new_high_risk_role_count,
        "retained_role_change_count": operator_churn.retained_role_change_count,
    }
    for field_name, value in count_fields.items():
        if not _non_negative_int(value):
            return _unsupported(
                f"operator churn {field_name} must be non-negative",
                config.as_of_slot,
            )
    if not _valid_probability_ppm(operator_churn.address_turnover_ppm):
        return _unsupported(
            "operator churn address_turnover_ppm is invalid",
            config.as_of_slot,
        )
    return _validate_operator_churn_count_consistency(operator_churn, config)


def _validate_operator_churn_count_consistency(
    operator_churn: OperatorWalletChurnSnapshot,
    config: RuggerSelectorConfig,
) -> AbstainResult | None:
    if (
        operator_churn.new_address_count != len(operator_churn.new_addresses)
        or operator_churn.retained_address_count
        != len(operator_churn.retained_addresses)
        or operator_churn.retired_address_count != len(operator_churn.retired_addresses)
    ):
        return _unsupported(
            "operator churn address counts do not match address records",
            config.as_of_slot,
        )
    if operator_churn.current_active_address_count != (
        operator_churn.new_address_count + operator_churn.retained_address_count
    ):
        return _unsupported(
            "operator churn current active count is inconsistent",
            config.as_of_slot,
        )
    if operator_churn.previous_active_address_count != (
        operator_churn.retired_address_count + operator_churn.retained_address_count
    ):
        return _unsupported(
            "operator churn previous active count is inconsistent",
            config.as_of_slot,
        )
    expected_turnover_ppm = _address_turnover_ppm(
        new_count=operator_churn.new_address_count,
        retired_count=operator_churn.retired_address_count,
        previous_count=operator_churn.previous_active_address_count,
        current_count=operator_churn.current_active_address_count,
    )
    if operator_churn.address_turnover_ppm != expected_turnover_ppm:
        return _unsupported(
            "operator churn address turnover is inconsistent",
            config.as_of_slot,
        )
    if operator_churn.retained_role_change_count > (
        operator_churn.retained_address_count
    ):
        return _unsupported(
            "operator churn retained role changes exceed retained addresses",
            config.as_of_slot,
        )
    return None


def _validate_operator_churn_addresses(
    *,
    operator_churn: OperatorWalletChurnSnapshot,
    churn_policy: OperatorChurnSelectorPolicy,
    matcher: LaunchMatcherSnapshot,
    config: RuggerSelectorConfig,
) -> AbstainResult | None:
    del churn_policy, matcher
    grouped_addresses = (
        (operator_churn.new_addresses, WalletChurnStatus.NEW),
        (operator_churn.retained_addresses, WalletChurnStatus.RETAINED),
        (operator_churn.retired_addresses, WalletChurnStatus.RETIRED),
    )
    seen_addresses: set[str] = set()
    new_high_risk_role_count = 0
    for addresses, expected_status in grouped_addresses:
        if type(addresses) is not tuple:
            return _unsupported(
                "operator churn address records must be tuples",
                config.as_of_slot,
            )
        for address in addresses:
            address_error = _validate_operator_churn_address(
                address=address,
                expected_status=expected_status,
                operator_churn=operator_churn,
                config=config,
            )
            if address_error is not None:
                return address_error
            if address.address in seen_addresses:
                return _unsupported(
                    "operator churn address records contain duplicates",
                    config.as_of_slot,
                )
            seen_addresses.add(address.address)
            if expected_status is WalletChurnStatus.NEW:
                new_high_risk_role_count += address.high_risk_role_count
    if operator_churn.new_high_risk_role_count != new_high_risk_role_count:
        return _unsupported(
            "operator churn new high-risk role count is inconsistent",
            config.as_of_slot,
        )
    return None


def _validate_operator_churn_address(
    *,
    address: WalletChurnAddress,
    expected_status: WalletChurnStatus,
    operator_churn: OperatorWalletChurnSnapshot,
    config: RuggerSelectorConfig,
) -> AbstainResult | None:
    for validation in (
        _validate_operator_churn_address_identity,
        _validate_operator_churn_address_probabilities,
        _validate_operator_churn_address_roles,
        _validate_operator_churn_address_provenance,
    ):
        validation_error = validation(
            address=address,
            expected_status=expected_status,
            operator_churn=operator_churn,
            config=config,
        )
        if validation_error is not None:
            return validation_error
    return None


def _validate_operator_churn_address_identity(
    *,
    address: WalletChurnAddress,
    expected_status: WalletChurnStatus,
    operator_churn: OperatorWalletChurnSnapshot,
    config: RuggerSelectorConfig,
) -> AbstainResult | None:
    if not isinstance(address, WalletChurnAddress):
        return _unsupported("operator churn address is malformed", config.as_of_slot)
    if address.as_of_slot != operator_churn.as_of_slot:
        return _stale(
            "operator churn address uses a different as_of_slot",
            config.as_of_slot,
        )
    if address.entity_id != operator_churn.entity_id:
        return _unsupported("operator churn address entity mismatch", config.as_of_slot)
    if not _non_empty_str(address.address):
        return _missing("operator churn address is required", config.as_of_slot)
    if address.status is not expected_status:
        return _unsupported("operator churn address status mismatch", config.as_of_slot)
    return None


def _validate_operator_churn_address_probabilities(
    *,
    address: WalletChurnAddress,
    expected_status: WalletChurnStatus,
    operator_churn: OperatorWalletChurnSnapshot,
    config: RuggerSelectorConfig,
) -> AbstainResult | None:
    del expected_status, operator_churn
    return _validate_probability_fields(
        config.as_of_slot,
        {
            "membership_probability_ppm": address.membership_probability_ppm,
            "same_controller_probability_ppm": (
                address.same_controller_probability_ppm
            ),
            "cooperating_probability_ppm": address.cooperating_probability_ppm,
        },
    )


def _validate_operator_churn_address_roles(
    *,
    address: WalletChurnAddress,
    expected_status: WalletChurnStatus,
    operator_churn: OperatorWalletChurnSnapshot,
    config: RuggerSelectorConfig,
) -> AbstainResult | None:
    del expected_status, operator_churn
    if type(address.roles) is not tuple:
        return _unsupported(
            "operator churn address roles must be tuple", config.as_of_slot
        )
    if any(not isinstance(role, AddressRole) for role in address.roles):
        return _unsupported("operator churn address role is invalid", config.as_of_slot)
    if not _non_negative_int(address.high_risk_role_count):
        return _unsupported(
            "operator churn address high-risk role count is invalid",
            config.as_of_slot,
        )
    if address.high_risk_role_count != sum(
        1 for role in address.roles if role in HIGH_RISK_CHURN_ROLES
    ):
        return _unsupported(
            "operator churn address high-risk role count is inconsistent",
            config.as_of_slot,
        )
    return None


def _validate_operator_churn_address_provenance(
    *,
    address: WalletChurnAddress,
    expected_status: WalletChurnStatus,
    operator_churn: OperatorWalletChurnSnapshot,
    config: RuggerSelectorConfig,
) -> AbstainResult | None:
    del expected_status, operator_churn
    if not _valid_evidence_ids(address.evidence_ids):
        return _missing(
            "operator churn address evidence_ids are required",
            config.as_of_slot,
        )
    if not _non_empty_str(address.model_version):
        return _decoder_mismatch(
            "operator churn address model_version is required",
            config.as_of_slot,
        )
    return None


def _validate_operator_churn_provenance(
    *,
    operator_churn: OperatorWalletChurnSnapshot,
    churn_policy: OperatorChurnSelectorPolicy,
    matcher: LaunchMatcherSnapshot,
    config: RuggerSelectorConfig,
) -> AbstainResult | None:
    del churn_policy, matcher
    if not _valid_evidence_ids(operator_churn.evidence_ids):
        return _missing("operator churn evidence_ids are required", config.as_of_slot)
    if not _valid_evidence_ids(operator_churn.reason_codes):
        return _missing("operator churn reason_codes are required", config.as_of_slot)
    return None


def _selector_reason(
    *,
    matcher: LaunchMatcherSnapshot,
    trigger: ObservedTriggerEvaluation,
    support: SelectorSupportEvidence,
    config: RuggerSelectorConfig,
    operator_churn_gate: OperatorChurnSelectorGate | None,
) -> SelectorDecisionReason:
    threshold_reason = _selector_threshold_reason(
        matcher=matcher,
        trigger=trigger,
        support=support,
        config=config,
    )
    if threshold_reason is not None:
        return threshold_reason
    churn_reason = _selector_churn_reason(operator_churn_gate)
    if churn_reason is not None:
        return churn_reason
    return SelectorDecisionReason.SELECTOR_PASSED


def _selector_threshold_reason(
    *,
    matcher: LaunchMatcherSnapshot,
    trigger: ObservedTriggerEvaluation,
    support: SelectorSupportEvidence,
    config: RuggerSelectorConfig,
) -> SelectorDecisionReason | None:
    if matcher.entity_probability_ppm < config.min_entity_probability_ppm:
        return SelectorDecisionReason.ENTITY_PROBABILITY_BELOW_THRESHOLD
    if matcher.regime_probability_ppm < config.min_regime_probability_ppm:
        return SelectorDecisionReason.REGIME_PROBABILITY_BELOW_THRESHOLD
    if support.historical_launch_count < config.min_historical_launches:
        return SelectorDecisionReason.HISTORICAL_SUPPORT_BELOW_THRESHOLD
    if trigger.max_trigger_risk_ppm < config.min_trigger_risk_ppm:
        return SelectorDecisionReason.TRIGGER_RISK_BELOW_THRESHOLD
    return None


def _selector_churn_reason(
    operator_churn_gate: OperatorChurnSelectorGate | None,
) -> SelectorDecisionReason | None:
    if operator_churn_gate is None or operator_churn_gate.operator_churn is None:
        return None
    operator_churn = operator_churn_gate.operator_churn
    churn_policy = operator_churn_gate.policy
    if operator_churn.new_high_risk_role_count > churn_policy.max_new_high_risk_roles:
        return SelectorDecisionReason.OPERATOR_CHURN_NEW_HIGH_RISK_ROLES_ABOVE_CAP
    if operator_churn.address_turnover_ppm > churn_policy.max_address_turnover_ppm:
        return SelectorDecisionReason.OPERATOR_CHURN_ADDRESS_TURNOVER_ABOVE_CAP
    if operator_churn.retained_role_change_count > (
        churn_policy.max_retained_role_changes
    ):
        return SelectorDecisionReason.OPERATOR_CHURN_RETAINED_ROLE_CHANGES_ABOVE_CAP
    return None


def _validate_timing_forecast(forecast: DumpHazardForecast) -> AbstainResult | None:
    metadata_error = _validate_timing_metadata(forecast)
    if metadata_error is not None:
        return metadata_error
    bin_error = _validate_timing_bins(forecast)
    if bin_error is not None:
        return bin_error
    return _validate_timing_quantile_reach(forecast)


def _validate_timing_metadata(forecast: DumpHazardForecast) -> AbstainResult | None:
    if not _non_negative_int(forecast.as_of_slot):
        return _unsupported(
            "timing as_of_slot must be non-negative", forecast.as_of_slot
        )
    version_error = _require_versions(
        forecast.as_of_slot,
        {
            "timing_model_version": forecast.timing_model_version,
            "forecast_snapshot_version": forecast.forecast_snapshot_version,
        },
    )
    if version_error is not None:
        return version_error
    if not _valid_evidence_ids(forecast.evidence_ids):
        return _missing("timing evidence_ids are required", forecast.as_of_slot)
    return None


def _validate_timing_bins(forecast: DumpHazardForecast) -> AbstainResult | None:
    if type(forecast.bins) is not tuple or not forecast.bins:
        return _missing("timing hazard bins are required", forecast.as_of_slot)
    previous_horizon = -1
    for hazard_bin in forecast.bins:
        bin_error = _validate_timing_bin(
            hazard_bin=hazard_bin,
            previous_horizon_ms=previous_horizon,
            forecast=forecast,
        )
        if bin_error is not None:
            return bin_error
        previous_horizon = hazard_bin.horizon_ms
    if previous_horizon < TIMING_MAX_HORIZON_MS:
        return _missing(
            "timing forecast must cover the next 10 seconds", forecast.as_of_slot
        )
    if not _has_required_timing_horizons(forecast.bins):
        return _missing(
            "timing forecast must include fixed decision horizons",
            forecast.as_of_slot,
        )
    return None


def _validate_timing_bin(
    *,
    hazard_bin: DiscreteHazardBin,
    previous_horizon_ms: int,
    forecast: DumpHazardForecast,
) -> AbstainResult | None:
    if hazard_bin.as_of_slot != forecast.as_of_slot:
        return _stale("hazard bin uses a different as_of_slot", forecast.as_of_slot)
    if not _positive_int(hazard_bin.horizon_ms):
        return _unsupported(
            "hazard bin horizon_ms must be positive", forecast.as_of_slot
        )
    if hazard_bin.horizon_ms <= previous_horizon_ms:
        return _unsupported(
            "hazard bin horizons must be strictly increasing",
            forecast.as_of_slot,
        )
    if not _valid_probability_ppm(hazard_bin.hazard_ppm):
        return _unsupported(
            "hazard_ppm must be in probability ppm range", forecast.as_of_slot
        )
    return None


def _validate_timing_quantile_reach(
    forecast: DumpHazardForecast,
) -> AbstainResult | None:
    if (
        _cumulative_dump_probability(forecast.bins, TIMING_MAX_HORIZON_MS)
        < Q50_PROBABILITY_PPM
    ):
        return _missing(
            "timing forecast does not reach q50 within the supported horizon",
            forecast.as_of_slot,
        )
    quantiles = (
        _quantile_time_ms(forecast.bins, 50_000),
        _quantile_time_ms(forecast.bins, 100_000),
        _quantile_time_ms(forecast.bins, 500_000),
    )
    if not _non_decreasing(quantiles):
        return _unsupported(
            "timing remaining-time quantiles must be non-decreasing",
            forecast.as_of_slot,
        )
    return None


def _match_versions(match: RuleHypothesisMatch) -> tuple[str, ...]:
    return (
        match.generator_version,
        match.feature_schema_version,
        match.labeler_version,
        match.row_schema_version,
        match.operator_profile_version,
        match.regime_model_version,
    )


def _trigger_versions(trigger: ObservedTriggerEvaluation) -> tuple[str, ...]:
    return (
        trigger.generator_version,
        trigger.feature_schema_version,
        trigger.labeler_version,
        trigger.row_schema_version,
        trigger.operator_profile_version,
        trigger.regime_model_version,
    )


def _cumulative_dump_probability(
    bins: tuple[DiscreteHazardBin, ...],
    horizon_ms: int,
) -> int:
    survival_ppm = PROBABILITY_PPM_DENOMINATOR
    for hazard_bin in bins:
        if hazard_bin.horizon_ms > horizon_ms:
            break
        survival_ppm = (
            survival_ppm
            * (PROBABILITY_PPM_DENOMINATOR - hazard_bin.hazard_ppm)
            // PROBABILITY_PPM_DENOMINATOR
        )
    return PROBABILITY_PPM_DENOMINATOR - survival_ppm


def _quantile_time_ms(
    bins: tuple[DiscreteHazardBin, ...],
    probability_ppm: int,
) -> int:
    for hazard_bin in bins:
        if _cumulative_dump_probability(bins, hazard_bin.horizon_ms) >= probability_ppm:
            return hazard_bin.horizon_ms
    return bins[-1].horizon_ms


def _address_turnover_ppm(
    *,
    new_count: int,
    retired_count: int,
    previous_count: int,
    current_count: int,
) -> int:
    denominator = previous_count + current_count
    if denominator == 0:
        return 0
    return (new_count + retired_count) * PROBABILITY_PPM_DENOMINATOR // denominator


def _require_versions(
    as_of_slot: Slot,
    fields: dict[str, str],
) -> AbstainResult | None:
    for field_name, value in fields.items():
        if not _non_empty_str(value):
            return _decoder_mismatch(f"{field_name} is required", as_of_slot)
    return None


def _require_features(
    as_of_slot: Slot,
    fields: dict[str, str],
) -> AbstainResult | None:
    for field_name, value in fields.items():
        if not _non_empty_str(value):
            return _missing(f"{field_name} is required", as_of_slot)
    return None


def _validate_probability_fields(
    as_of_slot: Slot,
    fields: dict[str, int],
) -> AbstainResult | None:
    for field_name, value in fields.items():
        if not _valid_probability_ppm(value):
            return _unsupported(
                f"{field_name} must be in probability ppm range",
                as_of_slot,
            )
    return None


def _valid_probability_ppm(value: object) -> bool:
    return _non_negative_int(value) and value <= PROBABILITY_PPM_DENOMINATOR


def _valid_evidence_ids(evidence_ids: object) -> bool:
    return _valid_str_tuple(evidence_ids)


def _valid_str_tuple(value: object) -> bool:
    return (
        type(value) is tuple
        and bool(value)
        and all(_non_empty_str(item) for item in value)
    )


def _non_empty_str(value: object) -> bool:
    return type(value) is str and bool(value.strip())


def _non_negative_int(value: object) -> bool:
    return type(value) is int and value >= 0


def _positive_int(value: object) -> bool:
    return type(value) is int and value > 0


def _non_decreasing(values: tuple[int, ...]) -> bool:
    return all(values[index - 1] <= values[index] for index in range(1, len(values)))


def _has_required_timing_horizons(bins: tuple[DiscreteHazardBin, ...]) -> bool:
    available_horizons = {hazard_bin.horizon_ms for hazard_bin in bins}
    return all(
        required_horizon in available_horizons
        for required_horizon in REQUIRED_TIMING_HORIZONS_MS
    )


def _missing(message: str, as_of_slot: Slot) -> AbstainResult:
    return AbstainResult(
        reason=AbstainReason.MISSING_FEATURE,
        message=message,
        as_of_slot=_abstain_slot(as_of_slot),
    )


def _decoder_mismatch(message: str, as_of_slot: Slot) -> AbstainResult:
    return AbstainResult(
        reason=AbstainReason.DECODER_MISMATCH,
        message=message,
        as_of_slot=_abstain_slot(as_of_slot),
    )


def _stale(message: str, as_of_slot: Slot) -> AbstainResult:
    return AbstainResult(
        reason=AbstainReason.STALE_STATE,
        message=message,
        as_of_slot=_abstain_slot(as_of_slot),
    )


def _unsupported(message: str, as_of_slot: Slot) -> AbstainResult:
    return AbstainResult(
        reason=AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
        message=message,
        as_of_slot=_abstain_slot(as_of_slot),
    )


def _abstain_slot(as_of_slot: object) -> int:
    if type(as_of_slot) is int:
        return as_of_slot
    return -1
