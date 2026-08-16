"""Immutable point-in-time snapshots for decision-layer model inputs."""

from dataclasses import dataclass

from rugbot.domain.amounts import Slot
from rugbot.domain.decisions import AbstainReason, AbstainResult
from rugbot.graph.point_in_time import PROBABILITY_PPM_DENOMINATOR


@dataclass(frozen=True, slots=True)
class LaunchMatcherSnapshot:
    """Known-operator launch match result at one slot boundary."""

    as_of_slot: Slot
    entity_id: str
    regime_id: str
    entity_probability_ppm: int
    regime_probability_ppm: int
    entity_graph_snapshot_version: str
    operator_profile_version: str
    regime_model_version: str
    matcher_version: str


@dataclass(frozen=True, slots=True)
class RuggerSelectorSnapshot:
    """Selector output derived from a matcher snapshot and support rules."""

    as_of_slot: Slot
    selector_version: str
    is_selected: bool
    min_entity_probability_ppm: int
    min_regime_probability_ppm: int
    min_trigger_risk_ppm: int
    max_trigger_risk_ppm: int
    min_historical_launches: int
    historical_launch_count: int
    trigger_generator_version: str
    trigger_feature_schema_version: str
    trigger_labeler_version: str
    trigger_row_schema_version: str
    trigger_market_state_snapshot_version: str
    trigger_operator_profile_version: str
    trigger_regime_model_version: str
    reason_codes: tuple[str, ...]
    operator_churn_snapshot_version: str | None = None
    max_operator_churn_new_high_risk_roles: int | None = None
    observed_operator_churn_new_high_risk_roles: int | None = None
    max_operator_churn_address_turnover_ppm: int | None = None
    observed_operator_churn_address_turnover_ppm: int | None = None
    max_operator_churn_retained_role_changes: int | None = None
    observed_operator_churn_retained_role_changes: int | None = None


@dataclass(frozen=True, slots=True)
class RugTimingSnapshot:
    """Discrete-time dump-hazard and remaining-time snapshot."""

    as_of_slot: Slot
    timing_model_version: str
    p_dump_next_1s_ppm: int
    p_dump_next_3s_ppm: int
    p_dump_next_5s_ppm: int
    p_dump_next_10s_ppm: int
    q05_remaining_dump_time_ms: int
    q10_remaining_dump_time_ms: int
    q50_remaining_dump_time_ms: int


@dataclass(frozen=True, slots=True)
class DecisionSnapshotBundle:
    """Complete model-input bundle for selector, entry, and exit decisions."""

    as_of_slot: Slot
    snapshot_bundle_version: str
    feature_snapshot_version: str
    market_state_snapshot_version: str
    matcher: LaunchMatcherSnapshot
    selector: RuggerSelectorSnapshot
    timing: RugTimingSnapshot


@dataclass(frozen=True, slots=True)
class DecisionSnapshotPolicy:
    """Strict policy for deciding whether a loaded bundle is actionable."""

    as_of_slot: Slot
    policy_version: str
    require_selected_operator_churn_audit: bool
    accepted_operator_churn_snapshot_versions: tuple[str, ...]


def validate_decision_snapshot_bundle(
    bundle: DecisionSnapshotBundle,
) -> DecisionSnapshotBundle | AbstainResult:
    """Validate a complete immutable decision snapshot bundle.

    The validator is intentionally pure. It does not fetch missing evidence or
    reinterpret model outputs. Missing versions, stale slots, invalid
    probabilities, and incoherent timing curves abstain before downstream
    sizing or exit logic can use the bundle.
    """

    shape_error = _validate_bundle_shape(bundle)
    if shape_error is not None:
        return shape_error
    slot_error = _validate_bundle_slot(bundle)
    if slot_error is not None:
        return slot_error

    for validation in (
        _validate_bundle_versions,
        _validate_matcher_snapshot,
        _validate_selector_snapshot,
        _validate_timing_snapshot,
        _validate_selector_match_consistency,
    ):
        validation_error = validation(bundle)
        if validation_error is not None:
            return validation_error
    return bundle


def validate_decision_snapshot_bundle_with_policy(
    *,
    bundle: DecisionSnapshotBundle,
    policy: DecisionSnapshotPolicy,
) -> DecisionSnapshotBundle | AbstainResult:
    """Validate a decision snapshot bundle under a stricter action policy."""

    bundle_result = validate_decision_snapshot_bundle(bundle)
    if isinstance(bundle_result, AbstainResult):
        return bundle_result
    policy_error = _validate_decision_snapshot_policy(policy, bundle)
    if policy_error is not None:
        return policy_error
    return _validate_selector_churn_policy(bundle, policy)


def _validate_bundle_shape(bundle: DecisionSnapshotBundle) -> AbstainResult | None:
    if not isinstance(bundle, DecisionSnapshotBundle):
        return _unsupported("decision snapshot bundle is malformed", Slot(-1))
    if not isinstance(bundle.matcher, LaunchMatcherSnapshot):
        return _unsupported(
            "decision snapshot matcher is malformed",
            _shape_as_of_slot(bundle),
        )
    if not isinstance(bundle.selector, RuggerSelectorSnapshot):
        return _unsupported(
            "decision snapshot selector is malformed",
            _shape_as_of_slot(bundle),
        )
    if not isinstance(bundle.timing, RugTimingSnapshot):
        return _unsupported(
            "decision snapshot timing is malformed",
            _shape_as_of_slot(bundle),
        )
    return None


def _validate_decision_snapshot_policy(
    policy: DecisionSnapshotPolicy,
    bundle: DecisionSnapshotBundle,
) -> AbstainResult | None:
    if not isinstance(policy, DecisionSnapshotPolicy):
        return _unsupported("decision snapshot policy is malformed", bundle.as_of_slot)
    slot_error = _validate_decision_snapshot_policy_slot(policy, bundle)
    if slot_error is not None:
        return slot_error
    shape_error = _validate_decision_snapshot_policy_shape(policy, bundle)
    if shape_error is not None:
        return shape_error
    return _validate_decision_snapshot_policy_versions(policy, bundle)


def _validate_decision_snapshot_policy_slot(
    policy: DecisionSnapshotPolicy,
    bundle: DecisionSnapshotBundle,
) -> AbstainResult | None:
    if not _non_negative_int(policy.as_of_slot):
        return _unsupported(
            "decision snapshot policy as_of_slot is invalid", policy.as_of_slot
        )
    if policy.as_of_slot != bundle.as_of_slot:
        return _stale("decision snapshot policy uses a different slot", bundle)
    return None


def _validate_decision_snapshot_policy_shape(
    policy: DecisionSnapshotPolicy,
    bundle: DecisionSnapshotBundle,
) -> AbstainResult | None:
    if not _non_empty_str(policy.policy_version):
        return _decoder_mismatch(
            "decision snapshot policy version is required",
            bundle.as_of_slot,
        )
    if type(policy.require_selected_operator_churn_audit) is not bool:
        return _unsupported(
            "require_selected_operator_churn_audit must be boolean",
            bundle.as_of_slot,
        )
    return None


def _validate_decision_snapshot_policy_versions(
    policy: DecisionSnapshotPolicy,
    bundle: DecisionSnapshotBundle,
) -> AbstainResult | None:
    if not _valid_str_tuple(policy.accepted_operator_churn_snapshot_versions):
        return _decoder_mismatch(
            "accepted operator churn snapshot versions are required",
            bundle.as_of_slot,
        )
    return None


def _validate_selector_churn_policy(
    bundle: DecisionSnapshotBundle,
    policy: DecisionSnapshotPolicy,
) -> DecisionSnapshotBundle | AbstainResult:
    selector = bundle.selector
    if (
        policy.require_selected_operator_churn_audit
        and selector.is_selected
        and _selector_churn_audit_absent(selector)
    ):
        return _missing(
            "selected selector requires operator churn audit",
            bundle.as_of_slot,
        )
    if (
        not _selector_churn_audit_absent(selector)
        and selector.operator_churn_snapshot_version
        not in policy.accepted_operator_churn_snapshot_versions
    ):
        return _decoder_mismatch(
            "selector operator churn snapshot version is not accepted",
            bundle.as_of_slot,
        )
    return bundle


def _validate_bundle_slot(bundle: DecisionSnapshotBundle) -> AbstainResult | None:
    if not _non_negative_int(bundle.as_of_slot):
        return _unsupported("as_of_slot must be non-negative", bundle.as_of_slot)
    if (
        bundle.matcher.as_of_slot != bundle.as_of_slot
        or bundle.selector.as_of_slot != bundle.as_of_slot
        or bundle.timing.as_of_slot != bundle.as_of_slot
    ):
        return _stale("decision snapshot components use different slots", bundle)
    return None


def _validate_bundle_versions(
    bundle: DecisionSnapshotBundle,
) -> AbstainResult | None:
    return _require_versions(
        bundle.as_of_slot,
        {
            "snapshot_bundle_version": bundle.snapshot_bundle_version,
            "feature_snapshot_version": bundle.feature_snapshot_version,
            "market_state_snapshot_version": bundle.market_state_snapshot_version,
        },
    )


def _validate_matcher_snapshot(
    bundle: DecisionSnapshotBundle,
) -> AbstainResult | None:
    matcher = bundle.matcher
    missing_id = _require_features(
        bundle.as_of_slot,
        {
            "entity_id": matcher.entity_id,
            "regime_id": matcher.regime_id,
        },
    )
    if missing_id is not None:
        return missing_id

    version_error = _require_versions(
        bundle.as_of_slot,
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
        bundle.as_of_slot,
        {
            "entity_probability_ppm": matcher.entity_probability_ppm,
            "regime_probability_ppm": matcher.regime_probability_ppm,
        },
    )


def _validate_selector_snapshot(
    bundle: DecisionSnapshotBundle,
) -> AbstainResult | None:
    for validation in (
        _validate_selector_versions,
        _validate_selector_probabilities,
        _validate_selector_state,
        _validate_selector_churn_audit,
        _validate_selector_selected_consistency,
    ):
        validation_error = validation(bundle)
        if validation_error is not None:
            return validation_error
    return None


def _validate_selector_versions(
    bundle: DecisionSnapshotBundle,
) -> AbstainResult | None:
    selector = bundle.selector
    version_error = _require_versions(
        bundle.as_of_slot,
        {"selector_version": selector.selector_version},
    )
    if version_error is not None:
        return version_error

    trigger_version_error = _require_versions(
        bundle.as_of_slot,
        {
            "trigger_generator_version": selector.trigger_generator_version,
            "trigger_feature_schema_version": (selector.trigger_feature_schema_version),
            "trigger_labeler_version": selector.trigger_labeler_version,
            "trigger_row_schema_version": selector.trigger_row_schema_version,
            "trigger_market_state_snapshot_version": (
                selector.trigger_market_state_snapshot_version
            ),
            "trigger_operator_profile_version": (
                selector.trigger_operator_profile_version
            ),
            "trigger_regime_model_version": selector.trigger_regime_model_version,
        },
    )
    if trigger_version_error is not None:
        return trigger_version_error

    if selector.trigger_market_state_snapshot_version != (
        bundle.market_state_snapshot_version
    ):
        return _decoder_mismatch(
            "selector trigger market-state version mismatch",
            bundle.as_of_slot,
        )
    return None


def _validate_selector_probabilities(
    bundle: DecisionSnapshotBundle,
) -> AbstainResult | None:
    selector = bundle.selector
    return _validate_probability_fields(
        bundle.as_of_slot,
        {
            "min_entity_probability_ppm": selector.min_entity_probability_ppm,
            "min_regime_probability_ppm": selector.min_regime_probability_ppm,
            "min_trigger_risk_ppm": selector.min_trigger_risk_ppm,
            "max_trigger_risk_ppm": selector.max_trigger_risk_ppm,
        },
    )


def _validate_selector_state(
    bundle: DecisionSnapshotBundle,
) -> AbstainResult | None:
    selector = bundle.selector
    if type(selector.is_selected) is not bool:
        return _unsupported("selector is_selected must be boolean", bundle.as_of_slot)
    if not _non_negative_int(selector.min_historical_launches) or not _non_negative_int(
        selector.historical_launch_count
    ):
        return _unsupported(
            "selector historical support counts must be non-negative",
            bundle.as_of_slot,
        )
    if not _valid_reason_codes(selector.reason_codes):
        return _missing("selector reason_codes are required", bundle.as_of_slot)
    return None


def _validate_selector_churn_audit(
    bundle: DecisionSnapshotBundle,
) -> AbstainResult | None:
    presence_error = _validate_selector_churn_audit_presence(bundle)
    if presence_error is not None:
        return presence_error
    if _selector_churn_audit_absent(bundle.selector):
        return None
    value_error = _validate_selector_churn_audit_values(bundle)
    if value_error is not None:
        return value_error
    return _validate_selector_churn_selected_consistency(bundle)


def _validate_selector_churn_audit_presence(
    bundle: DecisionSnapshotBundle,
) -> AbstainResult | None:
    selector = bundle.selector
    if _selector_churn_audit_absent(selector):
        return None
    if any(value is None for value in _selector_churn_audit_fields(selector)):
        return _missing(
            "selector operator churn audit fields are incomplete", bundle.as_of_slot
        )
    return None


def _validate_selector_churn_audit_values(
    bundle: DecisionSnapshotBundle,
) -> AbstainResult | None:
    selector = bundle.selector
    if not _non_empty_str(selector.operator_churn_snapshot_version):
        return _decoder_mismatch(
            "selector operator churn snapshot version is required",
            bundle.as_of_slot,
        )

    count_fields = {
        "max_operator_churn_new_high_risk_roles": (
            selector.max_operator_churn_new_high_risk_roles
        ),
        "observed_operator_churn_new_high_risk_roles": (
            selector.observed_operator_churn_new_high_risk_roles
        ),
        "max_operator_churn_retained_role_changes": (
            selector.max_operator_churn_retained_role_changes
        ),
        "observed_operator_churn_retained_role_changes": (
            selector.observed_operator_churn_retained_role_changes
        ),
    }
    for field_name, value in count_fields.items():
        if not _non_negative_int(value):
            return _unsupported(f"{field_name} must be non-negative", bundle.as_of_slot)

    turnover_error = _validate_probability_fields(
        bundle.as_of_slot,
        {
            "max_operator_churn_address_turnover_ppm": (
                selector.max_operator_churn_address_turnover_ppm
            ),
            "observed_operator_churn_address_turnover_ppm": (
                selector.observed_operator_churn_address_turnover_ppm
            ),
        },
    )
    if turnover_error is not None:
        return turnover_error
    return None


def _validate_selector_churn_selected_consistency(
    bundle: DecisionSnapshotBundle,
) -> AbstainResult | None:
    selector = bundle.selector
    if selector.is_selected and (
        selector.observed_operator_churn_new_high_risk_roles
        > selector.max_operator_churn_new_high_risk_roles
        or selector.observed_operator_churn_address_turnover_ppm
        > selector.max_operator_churn_address_turnover_ppm
        or selector.observed_operator_churn_retained_role_changes
        > selector.max_operator_churn_retained_role_changes
    ):
        return _unsupported(
            "selector selected above operator churn caps",
            bundle.as_of_slot,
        )
    return None


def _selector_churn_audit_absent(selector: RuggerSelectorSnapshot) -> bool:
    return all(value is None for value in _selector_churn_audit_fields(selector))


def _selector_churn_audit_fields(
    selector: RuggerSelectorSnapshot,
) -> tuple[object, ...]:
    return (
        selector.operator_churn_snapshot_version,
        selector.max_operator_churn_new_high_risk_roles,
        selector.observed_operator_churn_new_high_risk_roles,
        selector.max_operator_churn_address_turnover_ppm,
        selector.observed_operator_churn_address_turnover_ppm,
        selector.max_operator_churn_retained_role_changes,
        selector.observed_operator_churn_retained_role_changes,
    )


def _validate_selector_selected_consistency(
    bundle: DecisionSnapshotBundle,
) -> AbstainResult | None:
    selector = bundle.selector
    if selector.is_selected and (
        selector.historical_launch_count < selector.min_historical_launches
    ):
        return _unsupported(
            "selector selected without required historical support",
            bundle.as_of_slot,
        )
    if selector.is_selected and (
        selector.max_trigger_risk_ppm < selector.min_trigger_risk_ppm
    ):
        return _unsupported(
            "selector selected below trigger risk threshold",
            bundle.as_of_slot,
        )
    return None


def _validate_timing_snapshot(
    bundle: DecisionSnapshotBundle,
) -> AbstainResult | None:
    timing = bundle.timing
    version_error = _require_versions(
        bundle.as_of_slot,
        {"timing_model_version": timing.timing_model_version},
    )
    if version_error is not None:
        return version_error

    probability_error = _validate_probability_fields(
        bundle.as_of_slot,
        {
            "p_dump_next_1s_ppm": timing.p_dump_next_1s_ppm,
            "p_dump_next_3s_ppm": timing.p_dump_next_3s_ppm,
            "p_dump_next_5s_ppm": timing.p_dump_next_5s_ppm,
            "p_dump_next_10s_ppm": timing.p_dump_next_10s_ppm,
        },
    )
    if probability_error is not None:
        return probability_error

    if not _non_decreasing(
        (
            timing.p_dump_next_1s_ppm,
            timing.p_dump_next_3s_ppm,
            timing.p_dump_next_5s_ppm,
            timing.p_dump_next_10s_ppm,
        )
    ):
        return _unsupported(
            "timing horizon dump probabilities must be non-decreasing",
            bundle.as_of_slot,
        )

    quantiles = (
        timing.q05_remaining_dump_time_ms,
        timing.q10_remaining_dump_time_ms,
        timing.q50_remaining_dump_time_ms,
    )
    if any(not _non_negative_int(value) for value in quantiles):
        return _unsupported(
            "remaining dump-time quantiles must be non-negative",
            bundle.as_of_slot,
        )
    if not _non_decreasing(quantiles):
        return _unsupported(
            "remaining dump-time quantiles must be non-decreasing",
            bundle.as_of_slot,
        )
    return None


def _validate_selector_match_consistency(
    bundle: DecisionSnapshotBundle,
) -> AbstainResult | None:
    matcher = bundle.matcher
    selector = bundle.selector
    version_error = _validate_selector_match_versions(bundle)
    if version_error is not None:
        return version_error
    if selector.is_selected and (
        matcher.entity_probability_ppm < selector.min_entity_probability_ppm
        or matcher.regime_probability_ppm < selector.min_regime_probability_ppm
    ):
        return _unsupported(
            "selector selected below matcher probability thresholds",
            bundle.as_of_slot,
        )
    return None


def _validate_selector_match_versions(
    bundle: DecisionSnapshotBundle,
) -> AbstainResult | None:
    matcher = bundle.matcher
    selector = bundle.selector
    if selector.trigger_operator_profile_version != matcher.operator_profile_version:
        return _decoder_mismatch(
            "selector trigger profile version mismatch",
            bundle.as_of_slot,
        )
    if selector.trigger_regime_model_version != matcher.regime_model_version:
        return _decoder_mismatch(
            "selector trigger regime version mismatch",
            bundle.as_of_slot,
        )
    return None


def _require_versions(
    as_of_slot: Slot,
    fields: dict[str, object],
) -> AbstainResult | None:
    for field_name, value in fields.items():
        if type(value) is not str or not value:
            return _decoder_mismatch(f"{field_name} is required", as_of_slot)
    return None


def _require_features(
    as_of_slot: Slot,
    fields: dict[str, object],
) -> AbstainResult | None:
    for field_name, value in fields.items():
        if type(value) is not str or not value:
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


def _valid_probability_ppm(value: int) -> bool:
    return _non_negative_int(value) and value <= PROBABILITY_PPM_DENOMINATOR


def _non_negative_int(value: object) -> bool:
    return type(value) is int and value >= 0


def _non_decreasing(values: tuple[int, ...]) -> bool:
    return all(values[index - 1] <= values[index] for index in range(1, len(values)))


def _valid_reason_codes(reason_codes: object) -> bool:
    return _valid_str_tuple(reason_codes)


def _valid_str_tuple(value: object) -> bool:
    return (
        type(value) is tuple
        and bool(value)
        and all(_non_empty_str(item) for item in value)
    )


def _non_empty_str(value: object) -> bool:
    return type(value) is str and bool(value.strip())


def _stale(message: str, bundle: DecisionSnapshotBundle) -> AbstainResult:
    return AbstainResult(
        reason=AbstainReason.STALE_STATE,
        message=message,
        as_of_slot=_abstain_slot(bundle.as_of_slot),
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


def _shape_as_of_slot(bundle: DecisionSnapshotBundle) -> Slot:
    if type(bundle.as_of_slot) is int:
        return bundle.as_of_slot
    return Slot(-1)
