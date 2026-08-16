"""Pure leakage-safe backtest evaluation contracts."""

from dataclasses import dataclass
from enum import Enum

from rugbot.domain.amounts import Slot
from rugbot.domain.decisions import AbstainReason, AbstainResult
from rugbot.graph.point_in_time import PROBABILITY_PPM_DENOMINATOR
from rugbot.models.outcome_labels import (
    HorizonOutcomeLabel,
    LaunchOutcomeLabels,
)


class BacktestAction(Enum):
    """Backtest decision action."""

    ENTER = "enter"
    SKIP = "skip"
    ABSTAIN = "abstain"


class BacktestFillStatus(Enum):
    """Frozen paper/replay fill outcome for an attempted entry."""

    FILLED = "filled"
    FAILED = "failed"
    EXPIRED = "expired"
    UNFILLED = "unfilled"
    NOT_ATTEMPTED = "not_attempted"


class BacktestSplit(Enum):
    """Leakage-safe backtest split."""

    TRAIN = "train"
    VALIDATION = "validation"
    TEST = "test"
    STRESS = "stress"


class OrderingScenario(Enum):
    """Execution ordering scenario used by the frozen replay result."""

    OBSERVED_ORDER = "observed_order"
    ADVERSE_SAME_SLOT = "adverse_same_slot"


@dataclass(frozen=True, slots=True)
class FrozenModelManifest:
    """Point-in-time artifact versions used for a backtest run."""

    as_of_slot: Slot
    model_freeze_slot: Slot
    decision_version: str
    model_version: str
    outcome_labeler_version: str
    profile_snapshot_version: str
    graph_snapshot_version: str
    feature_snapshot_version: str
    market_snapshot_version: str
    latency_model_version: str
    fee_config_version: str


@dataclass(frozen=True, slots=True)
class BacktestLaunchResult:
    """Frozen per-launch decision, replay result, and outcome artifact."""

    as_of_slot: Slot
    launch_id: str
    decision_id: str
    token_mint: str
    entity_id: str
    regime_id: str
    decision_slot: Slot
    decision_index: int
    action: BacktestAction
    fill_status: BacktestFillStatus
    ordering_scenario: OrderingScenario | None
    net_pnl_quote_base_units: int | None
    gross_profit_quote_base_units: int | None
    execution_cost_quote_base_units: int | None
    selected_size_quote_base_units: int | None
    outcome: LaunchOutcomeLabels
    manifest: FrozenModelManifest
    reason_codes: tuple[str, ...]
    evidence_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class BacktestConfig:
    """Configuration for a leakage-safe backtest report."""

    as_of_slot: Slot
    evaluation_version: str
    manifest: FrozenModelManifest
    train_end_slot: Slot
    test_start_slot: Slot
    test_end_slot: Slot
    train_entity_ids: tuple[str, ...]
    stress_entity_ids: tuple[str, ...]
    expected_shortfall_tail_ppm: int


@dataclass(frozen=True, slots=True)
class BacktestSplitMetrics:
    """Aggregated metrics for one split."""

    as_of_slot: Slot
    split: BacktestSplit
    observed_launch_count: int
    attempted_trade_count: int
    filled_trade_count: int
    failed_trade_count: int
    expired_trade_count: int
    unfilled_trade_count: int
    skipped_launch_count: int
    abstained_launch_count: int
    censored_outcome_count: int
    adverse_order_attempt_count: int
    net_pnl_observed_quote_base_units: int
    net_pnl_attempted_quote_base_units: int
    net_pnl_filled_quote_base_units: int
    coverage_ppm: int
    fill_failure_ppm: int | None
    cost_to_gross_profit_ppm: int | None
    expected_shortfall_quote_base_units: int | None
    maximum_drawdown_quote_base_units: int
    profit_capture_ppm: int | None
    profitable_launches_incorrectly_skipped_count: int
    adverse_launches_incorrectly_entered_count: int
    reason_codes: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class BacktestReport:
    """Versioned leakage-safe backtest metrics artifact."""

    as_of_slot: Slot
    evaluation_version: str
    manifest: FrozenModelManifest
    split_metrics: tuple[BacktestSplitMetrics, ...]
    source_launch_count: int
    evidence_ids: tuple[str, ...]
    reason_codes: tuple[str, ...]


def build_backtest_report(
    *,
    launches: tuple[BacktestLaunchResult, ...],
    config: BacktestConfig,
) -> BacktestReport | AbstainResult:
    """Build a leakage-safe backtest metrics report."""

    validation_error = _validate_inputs(launches=launches, config=config)
    if validation_error is not None:
        return validation_error

    ordered_launches = _ordered_launches(launches)
    assigned = tuple(
        (_split_for_launch(launch, config), launch) for launch in ordered_launches
    )
    return BacktestReport(
        as_of_slot=config.as_of_slot,
        evaluation_version=config.evaluation_version,
        manifest=config.manifest,
        split_metrics=tuple(
            _metrics_for_split(
                split=split,
                launches=tuple(
                    launch for launch_split, launch in assigned if launch_split is split
                ),
                config=config,
            )
            for split in BacktestSplit
        ),
        source_launch_count=len(launches),
        evidence_ids=_combined_evidence_ids(ordered_launches),
        reason_codes=("leakage_safe_backtest_report_built",),
    )


def _metrics_for_split(
    *,
    split: BacktestSplit,
    launches: tuple[BacktestLaunchResult, ...],
    config: BacktestConfig,
) -> BacktestSplitMetrics:
    attempted = tuple(launch for launch in launches if _attempted(launch))
    filled = tuple(
        launch for launch in launches if launch.fill_status is BacktestFillStatus.FILLED
    )
    return BacktestSplitMetrics(
        as_of_slot=config.as_of_slot,
        split=split,
        observed_launch_count=len(launches),
        attempted_trade_count=len(attempted),
        filled_trade_count=len(filled),
        failed_trade_count=_status_count(launches, BacktestFillStatus.FAILED),
        expired_trade_count=_status_count(launches, BacktestFillStatus.EXPIRED),
        unfilled_trade_count=_status_count(launches, BacktestFillStatus.UNFILLED),
        skipped_launch_count=_action_count(launches, BacktestAction.SKIP),
        abstained_launch_count=_action_count(launches, BacktestAction.ABSTAIN),
        censored_outcome_count=_censored_outcome_count(launches),
        adverse_order_attempt_count=_adverse_order_attempt_count(launches),
        net_pnl_observed_quote_base_units=_sum_net_pnl(launches),
        net_pnl_attempted_quote_base_units=_sum_net_pnl(attempted),
        net_pnl_filled_quote_base_units=_sum_net_pnl(filled),
        coverage_ppm=_bounded_ratio_ppm(len(attempted), len(launches)),
        fill_failure_ppm=_optional_bounded_ratio_ppm(
            len(attempted) - len(filled), len(attempted)
        ),
        cost_to_gross_profit_ppm=_cost_to_gross_profit_ppm(attempted),
        expected_shortfall_quote_base_units=_expected_shortfall(
            attempted,
            config.expected_shortfall_tail_ppm,
        ),
        maximum_drawdown_quote_base_units=_maximum_drawdown(launches),
        profit_capture_ppm=_profit_capture_ppm(launches),
        profitable_launches_incorrectly_skipped_count=(
            _profitable_skipped_count(launches)
        ),
        adverse_launches_incorrectly_entered_count=_adverse_entered_count(launches),
        reason_codes=(
            ("split_has_launches",) if launches else ("split_has_no_launches",)
        ),
    )


def _validate_inputs(
    *,
    launches: tuple[BacktestLaunchResult, ...],
    config: BacktestConfig,
) -> AbstainResult | None:
    config_error = _validate_config_artifact(config)
    if config_error is not None:
        return config_error
    launch_error = _validate_launch_artifacts(launches, config)
    if launch_error is not None:
        return launch_error
    return _validate_unique_launches(launches, config)


def _validate_config_artifact(config: object) -> AbstainResult | None:
    if not isinstance(config, BacktestConfig):
        return _unsupported("backtest config is malformed", Slot(-1))
    return _validate_config(config)


def _validate_config(config: BacktestConfig) -> AbstainResult | None:
    if not _non_negative_int(config.as_of_slot):
        return _unsupported("as_of_slot must be non-negative", Slot(-1))
    version_error = _validate_config_version(config)
    if version_error is not None:
        return version_error
    split_error = _validate_config_splits(config)
    if split_error is not None:
        return split_error
    entity_error = _validate_config_tail_and_entities(config)
    if entity_error is not None:
        return entity_error
    return _validate_manifest(config.manifest, config)


def _validate_manifest(
    manifest: object,
    config: BacktestConfig,
) -> AbstainResult | None:
    if not isinstance(manifest, FrozenModelManifest):
        return _unsupported("frozen model manifest is malformed", config)
    if not _non_negative_int(manifest.as_of_slot) or not _non_negative_int(
        manifest.model_freeze_slot
    ):
        return _unsupported("manifest slots must be non-negative", config)
    if manifest.as_of_slot != config.as_of_slot:
        return _stale("manifest uses a stale as_of_slot", config)
    if manifest.model_freeze_slot > config.test_start_slot:
        return _stale("model freeze slot is newer than test start", config)
    return _validate_manifest_versions(manifest, config)


def _validate_manifest_versions(
    manifest: FrozenModelManifest,
    config: BacktestConfig,
) -> AbstainResult | None:
    versions = (
        manifest.decision_version,
        manifest.model_version,
        manifest.outcome_labeler_version,
        manifest.profile_snapshot_version,
        manifest.graph_snapshot_version,
        manifest.feature_snapshot_version,
        manifest.market_snapshot_version,
        manifest.latency_model_version,
        manifest.fee_config_version,
    )
    if any(not _valid_non_empty_str(version) for version in versions):
        return _decoder_mismatch("manifest versions are required", config)
    return None


def _validate_config_version(config: BacktestConfig) -> AbstainResult | None:
    if not _valid_non_empty_str(config.evaluation_version):
        return _decoder_mismatch("evaluation_version is required", config)
    return None


def _validate_config_splits(config: BacktestConfig) -> AbstainResult | None:
    split_slots = (config.train_end_slot, config.test_start_slot, config.test_end_slot)
    if any(not _non_negative_int(slot) for slot in split_slots):
        return _unsupported("backtest split slots must be non-negative", Slot(-1))
    if not (config.train_end_slot < config.test_start_slot <= config.test_end_slot):
        return _unsupported("backtest split slots are incoherent", config)
    if config.test_end_slot > config.as_of_slot:
        return _stale("backtest test_end_slot is newer than as_of_slot", config)
    return None


def _validate_config_tail_and_entities(config: BacktestConfig) -> AbstainResult | None:
    if not _positive_probability_ppm(config.expected_shortfall_tail_ppm):
        return _unsupported(
            "expected_shortfall_tail_ppm must be in (0, 1000000]",
            config,
        )
    train_error = _validate_entity_id_tuple(config.train_entity_ids, config)
    if train_error is not None:
        return train_error
    stress_error = _validate_entity_id_tuple(config.stress_entity_ids, config)
    if stress_error is not None:
        return stress_error
    if set(config.train_entity_ids) & set(config.stress_entity_ids):
        return _unsupported("train and stress entity IDs must be disjoint", config)
    return None


def _validate_entity_id_tuple(
    entity_ids: object,
    config: BacktestConfig,
) -> AbstainResult | None:
    if type(entity_ids) is not tuple:
        return _unsupported("split entity IDs must be tuples", config)
    if any(not _valid_non_empty_str(entity_id) for entity_id in entity_ids):
        return _missing("split entity IDs must contain non-empty strings", config)
    if len(set(entity_ids)) != len(entity_ids):
        return _unsupported("split entity IDs must be unique", config)
    return None


def _validate_launch_artifacts(
    launches: object,
    config: BacktestConfig,
) -> AbstainResult | None:
    if type(launches) is not tuple:
        return _unsupported("backtest launches must be a tuple", config)
    if not launches:
        return _missing("backtest launches are required", config)
    for launch in launches:
        if not isinstance(launch, BacktestLaunchResult):
            return _unsupported("backtest launch result is malformed", config)
        launch_error = _validate_launch(launch, config)
        if launch_error is not None:
            return launch_error
    return None


def _validate_launch(
    launch: BacktestLaunchResult,
    config: BacktestConfig,
) -> AbstainResult | None:
    for validation in (
        _validate_launch_identity,
        _validate_launch_manifest,
        _validate_launch_outcome,
        _validate_launch_action_and_pnl,
        _validate_launch_evidence,
    ):
        validation_error = validation(launch, config)
        if validation_error is not None:
            return validation_error
    return None


def _validate_launch_identity(
    launch: BacktestLaunchResult,
    config: BacktestConfig,
) -> AbstainResult | None:
    if not _non_negative_int(launch.as_of_slot):
        return _unsupported("launch as_of_slot must be non-negative", config)
    if launch.as_of_slot != config.as_of_slot:
        return _stale("launch result uses a stale as_of_slot", config)
    for value in (
        launch.launch_id,
        launch.decision_id,
        launch.token_mint,
        launch.entity_id,
        launch.regime_id,
    ):
        if not _valid_non_empty_str(value):
            return _missing("launch identity fields are required", config)
    return _validate_launch_decision_position(launch, config)


def _validate_launch_decision_position(
    launch: BacktestLaunchResult,
    config: BacktestConfig,
) -> AbstainResult | None:
    if not _non_negative_int(launch.decision_slot) or not _non_negative_int(
        launch.decision_index
    ):
        return _unsupported("launch decision position is invalid", config)
    if launch.decision_slot > config.test_end_slot:
        return _stale("launch decision is outside configured test window", config)
    if (
        launch.entity_id in config.train_entity_ids
        and launch.decision_slot >= config.test_start_slot
    ):
        return _unsupported("train entity appears in test window", config)
    return None


def _validate_launch_manifest(
    launch: BacktestLaunchResult,
    config: BacktestConfig,
) -> AbstainResult | None:
    manifest = launch.manifest
    if not isinstance(manifest, FrozenModelManifest):
        return _unsupported("launch manifest is malformed", config)
    if manifest != config.manifest:
        return _decoder_mismatch("launch manifest mismatch", config)
    return None


def _validate_launch_outcome(
    launch: BacktestLaunchResult,
    config: BacktestConfig,
) -> AbstainResult | None:
    outcome = launch.outcome
    if not isinstance(outcome, LaunchOutcomeLabels):
        return _unsupported("launch outcome is malformed", config)
    identity_error = _validate_launch_outcome_identity(launch, outcome, config)
    if identity_error is not None:
        return identity_error
    version_error = _validate_launch_outcome_version(outcome, config)
    if version_error is not None:
        return version_error
    evidence_error = _validate_launch_outcome_evidence(outcome, config)
    if evidence_error is not None:
        return evidence_error
    return _validate_launch_outcome_fields(outcome, config)


def _validate_launch_outcome_identity(
    launch: BacktestLaunchResult,
    outcome: LaunchOutcomeLabels,
    config: BacktestConfig,
) -> AbstainResult | None:
    if not _non_negative_int(outcome.as_of_slot):
        return _unsupported("launch outcome as_of_slot is invalid", config)
    if outcome.as_of_slot != config.as_of_slot:
        return _stale("launch outcome uses a stale as_of_slot", config)
    if outcome.launch_id != launch.launch_id or outcome.token_mint != launch.token_mint:
        return _unsupported("launch outcome identity mismatch", config)
    return None


def _validate_launch_outcome_version(
    outcome: LaunchOutcomeLabels,
    config: BacktestConfig,
) -> AbstainResult | None:
    if outcome.labeler_version != config.manifest.outcome_labeler_version:
        return _decoder_mismatch("launch outcome labeler_version mismatch", config)
    return None


def _validate_launch_outcome_evidence(
    outcome: LaunchOutcomeLabels,
    config: BacktestConfig,
) -> AbstainResult | None:
    if not _valid_evidence_ids(outcome.evidence_ids):
        return _missing("launch outcome evidence_ids are required", config)
    return None


def _validate_launch_outcome_fields(
    outcome: LaunchOutcomeLabels,
    config: BacktestConfig,
) -> AbstainResult | None:
    event_error = _validate_launch_outcome_event_fields(outcome, config)
    if event_error is not None:
        return event_error
    opportunity_error = _validate_launch_outcome_opportunity(outcome, config)
    if opportunity_error is not None:
        return opportunity_error
    return _validate_launch_outcome_horizons(outcome, config)


def _validate_launch_outcome_event_fields(
    outcome: LaunchOutcomeLabels,
    config: BacktestConfig,
) -> AbstainResult | None:
    event_slot = outcome.first_material_adverse_event_slot
    event_elapsed = outcome.first_material_adverse_event_elapsed_ms
    if (event_slot is None) != (event_elapsed is None):
        return _unsupported(
            "launch outcome adverse event fields are incomplete", config
        )
    if event_slot is not None and not _non_negative_int(event_slot):
        return _unsupported("launch outcome adverse event slot is invalid", config)
    if event_slot is not None and event_slot > config.as_of_slot:
        return _stale("launch outcome adverse event is newer than as_of_slot", config)
    if event_elapsed is not None and not _non_negative_int(event_elapsed):
        return _unsupported("launch outcome adverse event elapsed is invalid", config)
    return None


def _validate_launch_outcome_opportunity(
    outcome: LaunchOutcomeLabels,
    config: BacktestConfig,
) -> AbstainResult | None:
    opportunity = outcome.max_executable_full_position_net_profit_before_adverse_event
    if opportunity is not None and not _strict_int(opportunity):
        return _unsupported("launch outcome opportunity must be an integer", config)
    if not _positive_int(outcome.source_point_count):
        return _unsupported(
            "launch outcome source_point_count must be positive", config
        )
    if not _valid_reason_codes(outcome.reason_codes):
        return _missing("launch outcome reason_codes are required", config)
    return None


def _validate_launch_outcome_horizons(
    outcome: LaunchOutcomeLabels,
    config: BacktestConfig,
) -> AbstainResult | None:
    if type(outcome.horizon_labels) is not tuple or not outcome.horizon_labels:
        return _missing("launch outcome horizon labels are required", config)
    previous_horizon = 0
    for label in outcome.horizon_labels:
        if not isinstance(label, HorizonOutcomeLabel):
            return _unsupported("launch outcome horizon label is malformed", config)
        label_error = _validate_horizon_label(
            label=label,
            outcome=outcome,
            config=config,
            previous_horizon=previous_horizon,
        )
        if label_error is not None:
            return label_error
        previous_horizon = label.horizon_ms
    return None


def _validate_horizon_label(
    *,
    label: HorizonOutcomeLabel,
    outcome: LaunchOutcomeLabels,
    config: BacktestConfig,
    previous_horizon: int,
) -> AbstainResult | None:
    identity_error = _validate_horizon_label_identity(label, outcome, config)
    if identity_error is not None:
        return identity_error
    if not _positive_int(label.horizon_ms) or label.horizon_ms <= previous_horizon:
        return _unsupported("horizon labels must be strictly increasing", config)
    if type(label.censored) is not bool:
        return _unsupported("horizon label censored flag must be boolean", config)
    observation_error = _validate_horizon_label_observation(label, config)
    if observation_error is not None:
        return observation_error
    if not _valid_evidence_ids(label.evidence_ids):
        return _missing("horizon label evidence_ids are required", config)
    return _validate_horizon_label_values(label, config)


def _validate_horizon_label_identity(
    label: HorizonOutcomeLabel,
    outcome: LaunchOutcomeLabels,
    config: BacktestConfig,
) -> AbstainResult | None:
    if not _non_negative_int(label.as_of_slot):
        return _unsupported("horizon label as_of_slot is invalid", config)
    if label.as_of_slot != config.as_of_slot:
        return _stale("horizon label uses a stale as_of_slot", config)
    if label.launch_id != outcome.launch_id or label.token_mint != outcome.token_mint:
        return _unsupported("horizon label identity mismatch", config)
    if label.labeler_version != outcome.labeler_version:
        return _decoder_mismatch("horizon label labeler_version mismatch", config)
    return None


def _validate_horizon_label_observation(
    label: HorizonOutcomeLabel,
    config: BacktestConfig,
) -> AbstainResult | None:
    if label.last_observed_slot is None or label.last_observed_elapsed_ms is None:
        return _unsupported("horizon label observation position is incomplete", config)
    position_error = _validate_horizon_label_observation_position(label, config)
    if position_error is not None:
        return position_error
    for flag_name, flag_value in {
        "adverse_event_observed": label.adverse_event_observed,
        "curve_completed": label.curve_completed,
        "migration_observed": label.migration_observed,
    }.items():
        if type(flag_value) is not bool:
            return _unsupported(f"horizon label {flag_name} must be boolean", config)
    return None


def _validate_horizon_label_observation_position(
    label: HorizonOutcomeLabel,
    config: BacktestConfig,
) -> AbstainResult | None:
    if not _non_negative_int(label.last_observed_slot):
        return _unsupported("horizon label last observed slot is invalid", config)
    if label.last_observed_slot > config.as_of_slot:
        return _stale("horizon label observation is newer than as_of_slot", config)
    if not _non_negative_int(label.last_observed_elapsed_ms):
        return _unsupported(
            "horizon label last observed elapsed time is invalid", config
        )
    return None


def _validate_horizon_label_values(
    label: HorizonOutcomeLabel,
    config: BacktestConfig,
) -> AbstainResult | None:
    if label.censored:
        if any(
            value is not None
            for value in (
                label.drawdown_ppm,
                label.recovery_ppm,
                label.full_exit_net_pnl_quote_base_units,
            )
        ):
            return _unsupported(
                "censored horizon label must not carry outcomes", config
            )
        return None
    if not _valid_probability_ppm(label.drawdown_ppm):
        return _unsupported("horizon drawdown_ppm must be probability ppm", config)
    if not _valid_probability_ppm(label.recovery_ppm):
        return _unsupported("horizon recovery_ppm must be probability ppm", config)
    if not _strict_int(label.full_exit_net_pnl_quote_base_units):
        return _unsupported("horizon full-exit PnL must be an integer", config)
    return None


def _validate_launch_action_and_pnl(
    launch: BacktestLaunchResult,
    config: BacktestConfig,
) -> AbstainResult | None:
    if not isinstance(launch.action, BacktestAction):
        return _unsupported("launch action is invalid", config)
    if not isinstance(launch.fill_status, BacktestFillStatus):
        return _unsupported("launch fill_status is invalid", config)
    if launch.action is BacktestAction.ENTER:
        return _validate_attempted_launch(launch, config)
    return _validate_non_attempted_launch(launch, config)


def _validate_non_attempted_launch(
    launch: BacktestLaunchResult,
    config: BacktestConfig,
) -> AbstainResult | None:
    if launch.fill_status is not BacktestFillStatus.NOT_ATTEMPTED:
        return _unsupported("non-entered launch must not have a fill status", config)
    if launch.ordering_scenario is not None:
        return _unsupported(
            "non-entered launch must not carry ordering scenario", config
        )
    if any(
        value is not None
        for value in (
            launch.net_pnl_quote_base_units,
            launch.gross_profit_quote_base_units,
            launch.execution_cost_quote_base_units,
            launch.selected_size_quote_base_units,
        )
    ):
        return _unsupported("non-entered launch must not carry trade fields", config)
    return None


def _validate_attempted_launch(
    launch: BacktestLaunchResult,
    config: BacktestConfig,
) -> AbstainResult | None:
    if launch.fill_status is BacktestFillStatus.NOT_ATTEMPTED:
        return _unsupported("entered launch must have attempted fill status", config)
    if not isinstance(launch.ordering_scenario, OrderingScenario):
        return _unsupported("attempted launch ordering scenario is invalid", config)
    pnl_error = _validate_attempted_launch_pnl(launch, config)
    if pnl_error is not None:
        return pnl_error
    return _validate_attempted_launch_size(launch, config)


def _validate_attempted_launch_pnl(
    launch: BacktestLaunchResult,
    config: BacktestConfig,
) -> AbstainResult | None:
    if not _strict_int(launch.net_pnl_quote_base_units):
        return _unsupported("attempted launch net PnL must be an integer", config)
    if not _non_negative_int(launch.gross_profit_quote_base_units):
        return _unsupported(
            "attempted launch gross profit must be non-negative", config
        )
    if not _non_negative_int(launch.execution_cost_quote_base_units):
        return _unsupported(
            "attempted launch execution cost must be non-negative", config
        )
    return None


def _validate_attempted_launch_size(
    launch: BacktestLaunchResult,
    config: BacktestConfig,
) -> AbstainResult | None:
    if not _positive_int(launch.selected_size_quote_base_units):
        return _unsupported("attempted launch selected size must be positive", config)
    return None


def _validate_launch_evidence(
    launch: BacktestLaunchResult,
    config: BacktestConfig,
) -> AbstainResult | None:
    if not _valid_reason_codes(launch.reason_codes):
        return _missing("launch reason_codes are required", config)
    if not _valid_evidence_ids(launch.evidence_ids):
        return _missing("launch evidence_ids are required", config)
    return None


def _validate_unique_launches(
    launches: tuple[BacktestLaunchResult, ...],
    config: BacktestConfig,
) -> AbstainResult | None:
    launch_ids = tuple(launch.launch_id for launch in launches)
    if len(set(launch_ids)) != len(launch_ids):
        return _unsupported("backtest launch IDs must be unique", config)
    decision_ids = tuple(launch.decision_id for launch in launches)
    if len(set(decision_ids)) != len(decision_ids):
        return _unsupported("backtest decision IDs must be unique", config)
    return None


def _split_for_launch(
    launch: BacktestLaunchResult,
    config: BacktestConfig,
) -> BacktestSplit:
    if launch.entity_id in config.stress_entity_ids:
        return BacktestSplit.STRESS
    if launch.decision_slot <= config.train_end_slot:
        return BacktestSplit.TRAIN
    if launch.decision_slot < config.test_start_slot:
        return BacktestSplit.VALIDATION
    return BacktestSplit.TEST


def _ordered_launches(
    launches: tuple[BacktestLaunchResult, ...],
) -> tuple[BacktestLaunchResult, ...]:
    return tuple(
        sorted(
            launches,
            key=lambda launch: (
                int(launch.decision_slot),
                launch.decision_index,
                launch.launch_id,
            ),
        )
    )


def _attempted(launch: BacktestLaunchResult) -> bool:
    return launch.action is BacktestAction.ENTER


def _status_count(
    launches: tuple[BacktestLaunchResult, ...],
    status: BacktestFillStatus,
) -> int:
    return sum(1 for launch in launches if launch.fill_status is status)


def _action_count(
    launches: tuple[BacktestLaunchResult, ...],
    action: BacktestAction,
) -> int:
    return sum(1 for launch in launches if launch.action is action)


def _censored_outcome_count(launches: tuple[BacktestLaunchResult, ...]) -> int:
    return sum(
        1
        for launch in launches
        if any(label.censored for label in launch.outcome.horizon_labels)
    )


def _adverse_order_attempt_count(launches: tuple[BacktestLaunchResult, ...]) -> int:
    return sum(
        1
        for launch in launches
        if launch.ordering_scenario is OrderingScenario.ADVERSE_SAME_SLOT
    )


def _sum_net_pnl(launches: tuple[BacktestLaunchResult, ...]) -> int:
    return sum(
        int(launch.net_pnl_quote_base_units)
        for launch in launches
        if launch.net_pnl_quote_base_units is not None
    )


def _cost_to_gross_profit_ppm(
    launches: tuple[BacktestLaunchResult, ...],
) -> int | None:
    gross_profit = sum(
        int(launch.gross_profit_quote_base_units)
        for launch in launches
        if launch.gross_profit_quote_base_units is not None
    )
    if gross_profit <= 0:
        return None
    execution_cost = sum(
        int(launch.execution_cost_quote_base_units)
        for launch in launches
        if launch.execution_cost_quote_base_units is not None
    )
    return execution_cost * PROBABILITY_PPM_DENOMINATOR // gross_profit


def _expected_shortfall(
    launches: tuple[BacktestLaunchResult, ...],
    tail_ppm: int,
) -> int | None:
    if not launches:
        return None
    pnl_values = sorted(int(launch.net_pnl_quote_base_units) for launch in launches)
    tail_count = max(
        1,
        (len(pnl_values) * tail_ppm + PROBABILITY_PPM_DENOMINATOR - 1)
        // PROBABILITY_PPM_DENOMINATOR,
    )
    tail_values = pnl_values[:tail_count]
    return sum(tail_values) // len(tail_values)


def _maximum_drawdown(launches: tuple[BacktestLaunchResult, ...]) -> int:
    cumulative = 0
    peak = 0
    max_drawdown = 0
    for launch in launches:
        if launch.net_pnl_quote_base_units is not None:
            cumulative += int(launch.net_pnl_quote_base_units)
        peak = max(peak, cumulative)
        drawdown = peak - cumulative
        max_drawdown = max(max_drawdown, drawdown)
    return max_drawdown


def _profit_capture_ppm(launches: tuple[BacktestLaunchResult, ...]) -> int | None:
    denominator = sum(_positive_opportunity(launch) for launch in launches)
    if denominator <= 0:
        return None
    captured = sum(
        max(0, int(launch.net_pnl_quote_base_units))
        for launch in launches
        if launch.fill_status is BacktestFillStatus.FILLED
        and launch.net_pnl_quote_base_units is not None
    )
    return captured * PROBABILITY_PPM_DENOMINATOR // denominator


def _profitable_skipped_count(launches: tuple[BacktestLaunchResult, ...]) -> int:
    return sum(
        1
        for launch in launches
        if launch.action in (BacktestAction.SKIP, BacktestAction.ABSTAIN)
        and _positive_opportunity(launch) > 0
    )


def _adverse_entered_count(launches: tuple[BacktestLaunchResult, ...]) -> int:
    return sum(
        1
        for launch in launches
        if launch.action is BacktestAction.ENTER
        and launch.outcome.first_material_adverse_event_slot is not None
    )


def _positive_opportunity(launch: BacktestLaunchResult) -> int:
    opportunity = (
        launch.outcome.max_executable_full_position_net_profit_before_adverse_event
    )
    if opportunity is None or opportunity <= 0:
        return 0
    return opportunity


def _combined_evidence_ids(
    launches: tuple[BacktestLaunchResult, ...],
) -> tuple[str, ...]:
    evidence_ids: list[str] = []
    for launch in launches:
        evidence_ids.extend(launch.evidence_ids)
        evidence_ids.extend(launch.outcome.evidence_ids)
    return tuple(dict.fromkeys(evidence_ids))


def _bounded_ratio_ppm(numerator: int, denominator: int) -> int:
    if denominator <= 0:
        return 0
    return min(
        PROBABILITY_PPM_DENOMINATOR,
        numerator * PROBABILITY_PPM_DENOMINATOR // denominator,
    )


def _optional_bounded_ratio_ppm(numerator: int, denominator: int) -> int | None:
    if denominator <= 0:
        return None
    return _bounded_ratio_ppm(numerator, denominator)


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


def _valid_non_empty_str(value: object) -> bool:
    return type(value) is str and bool(value)


def _positive_probability_ppm(value: object) -> bool:
    return _positive_int(value) and value <= PROBABILITY_PPM_DENOMINATOR


def _valid_probability_ppm(value: object) -> bool:
    return _non_negative_int(value) and value <= PROBABILITY_PPM_DENOMINATOR


def _strict_int(value: object) -> bool:
    return type(value) is int


def _non_negative_int(value: object) -> bool:
    return type(value) is int and value >= 0


def _positive_int(value: object) -> bool:
    return type(value) is int and value > 0


def _missing(message: str, config: BacktestConfig) -> AbstainResult:
    return AbstainResult(
        reason=AbstainReason.MISSING_FEATURE,
        message=message,
        as_of_slot=_abstain_slot(config.as_of_slot),
    )


def _decoder_mismatch(message: str, config: BacktestConfig) -> AbstainResult:
    return AbstainResult(
        reason=AbstainReason.DECODER_MISMATCH,
        message=message,
        as_of_slot=_abstain_slot(config.as_of_slot),
    )


def _stale(message: str, config: BacktestConfig) -> AbstainResult:
    return AbstainResult(
        reason=AbstainReason.STALE_STATE,
        message=message,
        as_of_slot=_abstain_slot(config.as_of_slot),
    )


def _unsupported(message: str, config_or_slot: BacktestConfig | Slot) -> AbstainResult:
    return AbstainResult(
        reason=AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
        message=message,
        as_of_slot=_result_slot(config_or_slot),
    )


def _result_slot(config_or_slot: BacktestConfig | Slot) -> int:
    if isinstance(config_or_slot, BacktestConfig):
        return _abstain_slot(config_or_slot.as_of_slot)
    return _abstain_slot(config_or_slot)


def _abstain_slot(as_of_slot: object) -> int:
    if type(as_of_slot) is int:
        return as_of_slot
    return -1
