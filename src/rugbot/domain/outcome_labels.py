"""Pure multi-horizon outcome label contracts."""

from dataclasses import dataclass

from rugbot.domain.adverse_event import AdverseEvent
from rugbot.domain.amounts import PROBABILITY_PPM_DENOMINATOR, QuoteBaseUnits, Slot
from rugbot.domain.decisions import AbstainReason, AbstainResult


@dataclass(frozen=True, slots=True)
class OutcomeObservationPoint:
    """Finalized point-in-time evidence used for outcome labels."""

    as_of_slot: Slot
    slot: Slot
    event_index: int
    elapsed_ms: int
    price_quote_base_units_per_token_base_unit_ppm: int
    full_exit_output_quote_base_units: QuoteBaseUnits
    full_exit_execution_cost_quote_base_units: QuoteBaseUnits
    curve_progress_ppm: int | None
    curve_completed: bool
    migration_observed: bool
    evidence_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class OutcomeLabelConfig:
    """Configuration for a leakage-safe launch outcome label artifact."""

    as_of_slot: Slot
    launch_id: str
    token_mint: str
    labeler_version: str
    horizon_ms: tuple[int, ...]
    entry_total_cost_quote_base_units: QuoteBaseUnits


@dataclass(frozen=True, slots=True)
class HorizonOutcomeLabel:
    """Per-horizon launch outcome label."""

    as_of_slot: Slot
    launch_id: str
    token_mint: str
    horizon_ms: int
    censored: bool
    last_observed_slot: Slot | None
    last_observed_elapsed_ms: int | None
    adverse_event_observed: bool
    curve_completed: bool
    migration_observed: bool
    drawdown_ppm: int | None
    recovery_ppm: int | None
    full_exit_net_pnl_quote_base_units: int | None
    labeler_version: str
    evidence_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class LaunchOutcomeLabels:
    """Versioned multi-horizon labels for one finalized launch trajectory."""

    as_of_slot: Slot
    launch_id: str
    token_mint: str
    labeler_version: str
    first_material_adverse_event_slot: Slot | None
    first_material_adverse_event_elapsed_ms: int | None
    max_executable_full_position_net_profit_before_adverse_event: int | None
    horizon_labels: tuple[HorizonOutcomeLabel, ...]
    source_point_count: int
    evidence_ids: tuple[str, ...]
    reason_codes: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class _DrawdownRecovery:
    drawdown_ppm: int
    recovery_ppm: int


def build_launch_outcome_labels(
    *,
    points: tuple[OutcomeObservationPoint, ...],
    config: OutcomeLabelConfig,
    adverse_event: AdverseEvent | None,
) -> LaunchOutcomeLabels | AbstainResult:
    """Build leakage-safe per-horizon labels from finalized observations."""

    validation_error = _validate_inputs(
        points=points,
        config=config,
        adverse_event=adverse_event,
    )
    if validation_error is not None:
        return validation_error

    ordered_points = _ordered_points(points)
    horizon_labels = tuple(
        _horizon_label(
            points=ordered_points,
            config=config,
            adverse_event=adverse_event,
            horizon_ms=horizon_ms,
        )
        for horizon_ms in config.horizon_ms
    )
    return LaunchOutcomeLabels(
        as_of_slot=config.as_of_slot,
        launch_id=config.launch_id,
        token_mint=config.token_mint,
        labeler_version=config.labeler_version,
        first_material_adverse_event_slot=(
            adverse_event.collapse_start_slot if adverse_event is not None else None
        ),
        first_material_adverse_event_elapsed_ms=(
            adverse_event.collapse_start_elapsed_ms
            if adverse_event is not None
            else None
        ),
        max_executable_full_position_net_profit_before_adverse_event=(
            _max_net_pnl_before_adverse_event(
                points=ordered_points,
                config=config,
                adverse_event=adverse_event,
            )
        ),
        horizon_labels=horizon_labels,
        source_point_count=len(points),
        evidence_ids=_combined_evidence_ids(ordered_points, adverse_event),
        reason_codes=("multi_horizon_outcome_labels_built",),
    )


def _horizon_label(
    *,
    points: tuple[OutcomeObservationPoint, ...],
    config: OutcomeLabelConfig,
    adverse_event: AdverseEvent | None,
    horizon_ms: int,
) -> HorizonOutcomeLabel:
    horizon_points = tuple(point for point in points if point.elapsed_ms <= horizon_ms)
    if not horizon_points or points[-1].elapsed_ms < horizon_ms:
        return HorizonOutcomeLabel(
            as_of_slot=config.as_of_slot,
            launch_id=config.launch_id,
            token_mint=config.token_mint,
            horizon_ms=horizon_ms,
            censored=True,
            last_observed_slot=points[-1].slot,
            last_observed_elapsed_ms=points[-1].elapsed_ms,
            adverse_event_observed=_event_observed_by(adverse_event, horizon_ms),
            curve_completed=any(point.curve_completed for point in horizon_points),
            migration_observed=any(
                point.migration_observed for point in horizon_points
            ),
            drawdown_ppm=None,
            recovery_ppm=None,
            full_exit_net_pnl_quote_base_units=None,
            labeler_version=config.labeler_version,
            evidence_ids=_censored_evidence_ids(
                points=points,
                horizon_points=horizon_points,
            ),
        )

    drawdown = _max_drawdown_and_recovery(horizon_points)
    last_point = horizon_points[-1]
    return HorizonOutcomeLabel(
        as_of_slot=config.as_of_slot,
        launch_id=config.launch_id,
        token_mint=config.token_mint,
        horizon_ms=horizon_ms,
        censored=False,
        last_observed_slot=last_point.slot,
        last_observed_elapsed_ms=last_point.elapsed_ms,
        adverse_event_observed=_event_observed_by(adverse_event, horizon_ms),
        curve_completed=any(point.curve_completed for point in horizon_points),
        migration_observed=any(point.migration_observed for point in horizon_points),
        drawdown_ppm=drawdown.drawdown_ppm,
        recovery_ppm=drawdown.recovery_ppm,
        full_exit_net_pnl_quote_base_units=_net_pnl(last_point, config),
        labeler_version=config.labeler_version,
        evidence_ids=_evidence_ids_for_points(horizon_points),
    )


def _validate_inputs(
    *,
    points: tuple[OutcomeObservationPoint, ...],
    config: OutcomeLabelConfig,
    adverse_event: AdverseEvent | None,
) -> AbstainResult | None:
    config_error = _validate_config_artifact(config)
    if config_error is not None:
        return config_error
    point_error = _validate_point_artifacts(points, config)
    if point_error is not None:
        return point_error
    if adverse_event is None:
        return None
    return _validate_adverse_event(adverse_event, config)


def _validate_config_artifact(config: object) -> AbstainResult | None:
    if not isinstance(config, OutcomeLabelConfig):
        return _unsupported("outcome label config is malformed", Slot(-1))
    return _validate_config(config)


def _validate_point_artifacts(
    points: object,
    config: OutcomeLabelConfig,
) -> AbstainResult | None:
    if type(points) is not tuple:
        return _unsupported(
            "outcome observation points must be a tuple", config.as_of_slot
        )
    if not points:
        return _missing("outcome observation points are required", config.as_of_slot)
    return _validate_points(points, config)


def _validate_config(config: OutcomeLabelConfig) -> AbstainResult | None:
    if not _non_negative_int(config.as_of_slot):
        return _unsupported("as_of_slot must be non-negative", config.as_of_slot)
    if not _valid_non_empty_str(config.launch_id):
        return _missing("launch_id is required", config.as_of_slot)
    if not _valid_non_empty_str(config.token_mint):
        return _missing("token_mint is required", config.as_of_slot)
    if not _valid_non_empty_str(config.labeler_version):
        return _decoder_mismatch("labeler_version is required", config.as_of_slot)
    if not _non_negative_int(config.entry_total_cost_quote_base_units):
        return _unsupported(
            "entry_total_cost_quote_base_units must be non-negative",
            config.as_of_slot,
        )
    return _validate_horizons(config)


def _validate_horizons(config: OutcomeLabelConfig) -> AbstainResult | None:
    if type(config.horizon_ms) is not tuple or not config.horizon_ms:
        return _missing("horizon_ms is required", config.as_of_slot)
    previous_horizon = 0
    for horizon_ms in config.horizon_ms:
        if not _positive_int(horizon_ms):
            return _unsupported(
                "horizon_ms values must be positive integers",
                config.as_of_slot,
            )
        if horizon_ms <= previous_horizon:
            return _unsupported(
                "horizon_ms values must be strictly increasing",
                config.as_of_slot,
            )
        previous_horizon = horizon_ms
    return None


def _validate_points(
    points: tuple[OutcomeObservationPoint, ...],
    config: OutcomeLabelConfig,
) -> AbstainResult | None:
    seen_positions: set[tuple[int, int]] = set()
    for point in points:
        if not isinstance(point, OutcomeObservationPoint):
            return _unsupported(
                "outcome observation point is malformed",
                config.as_of_slot,
            )
        point_error = _validate_point(point, config)
        if point_error is not None:
            return point_error
        position = (point.slot, point.event_index)
        if position in seen_positions:
            return _unsupported(
                "outcome observation point positions must be unique",
                config.as_of_slot,
            )
        seen_positions.add(position)
    return None


def _validate_point(
    point: OutcomeObservationPoint,
    config: OutcomeLabelConfig,
) -> AbstainResult | None:
    for validation in (
        _validate_point_slots,
        _validate_point_ordering,
        _validate_point_price,
        _validate_point_exit_value,
        _validate_point_progress,
        _validate_point_flags,
        _validate_point_evidence,
    ):
        validation_error = validation(point, config)
        if validation_error is not None:
            return validation_error
    return None


def _validate_point_slots(
    point: OutcomeObservationPoint,
    config: OutcomeLabelConfig,
) -> AbstainResult | None:
    if not _non_negative_int(point.as_of_slot) or not _non_negative_int(point.slot):
        return _unsupported(
            "outcome observation point slot fields must be non-negative integers",
            config.as_of_slot,
        )
    if point.as_of_slot != config.as_of_slot:
        return _stale("outcome observation point uses a stale as_of_slot", config)
    if point.slot > config.as_of_slot:
        return _stale("outcome observation point is newer than as_of_slot", config)
    return None


def _validate_point_ordering(
    point: OutcomeObservationPoint,
    config: OutcomeLabelConfig,
) -> AbstainResult | None:
    if not _non_negative_int(point.event_index) or not _non_negative_int(
        point.elapsed_ms
    ):
        return _unsupported(
            "outcome observation point ordering fields must be non-negative",
            config.as_of_slot,
        )
    return None


def _validate_point_price(
    point: OutcomeObservationPoint,
    config: OutcomeLabelConfig,
) -> AbstainResult | None:
    if not _positive_int(point.price_quote_base_units_per_token_base_unit_ppm):
        return _unsupported(
            "outcome observation point price must be positive",
            config.as_of_slot,
        )
    return None


def _validate_point_exit_value(
    point: OutcomeObservationPoint,
    config: OutcomeLabelConfig,
) -> AbstainResult | None:
    if not _non_negative_int(point.full_exit_output_quote_base_units):
        return _unsupported(
            "full_exit_output_quote_base_units must be non-negative",
            config.as_of_slot,
        )
    if not _non_negative_int(point.full_exit_execution_cost_quote_base_units):
        return _unsupported(
            "full_exit_execution_cost_quote_base_units must be non-negative",
            config.as_of_slot,
        )
    return None


def _validate_point_progress(
    point: OutcomeObservationPoint,
    config: OutcomeLabelConfig,
) -> AbstainResult | None:
    if point.curve_progress_ppm is not None and not _valid_probability_ppm(
        point.curve_progress_ppm
    ):
        return _unsupported(
            "curve_progress_ppm must be in probability ppm range",
            config.as_of_slot,
        )
    return None


def _validate_point_flags(
    point: OutcomeObservationPoint,
    config: OutcomeLabelConfig,
) -> AbstainResult | None:
    if type(point.curve_completed) is not bool:
        return _unsupported("curve_completed must be boolean", config.as_of_slot)
    if type(point.migration_observed) is not bool:
        return _unsupported("migration_observed must be boolean", config.as_of_slot)
    return None


def _validate_point_evidence(
    point: OutcomeObservationPoint,
    config: OutcomeLabelConfig,
) -> AbstainResult | None:
    if not _valid_evidence_ids(point.evidence_ids):
        return _missing(
            "outcome observation point evidence_ids are required",
            config.as_of_slot,
        )
    return None


def _validate_adverse_event(
    event: AdverseEvent,
    config: OutcomeLabelConfig,
) -> AbstainResult | None:
    if not isinstance(event, AdverseEvent):
        return _unsupported("adverse event is malformed", config.as_of_slot)
    identity_error = _validate_adverse_event_identity(event, config)
    if identity_error is not None:
        return identity_error
    position_error = _validate_adverse_event_position(event, config)
    if position_error is not None:
        return position_error
    return _validate_adverse_event_probabilities(event, config)


def _validate_adverse_event_identity(
    event: AdverseEvent,
    config: OutcomeLabelConfig,
) -> AbstainResult | None:
    if not _non_negative_int(event.as_of_slot):
        return _unsupported("adverse event as_of_slot is invalid", config.as_of_slot)
    if event.as_of_slot != config.as_of_slot:
        return _stale("adverse event uses a stale as_of_slot", config)
    if event.token_mint != config.token_mint:
        return _unsupported("adverse event token_mint mismatch", config.as_of_slot)
    if not _valid_non_empty_str(event.detector_version):
        return _decoder_mismatch(
            "adverse event detector_version is required",
            config.as_of_slot,
        )
    return None


def _validate_adverse_event_position(
    event: AdverseEvent,
    config: OutcomeLabelConfig,
) -> AbstainResult | None:
    slot_fields = (
        event.collapse_start_slot,
        event.peak_slot,
        event.trough_slot,
    )
    if any(not _non_negative_int(slot) for slot in slot_fields):
        return _unsupported(
            "adverse event slot fields must be non-negative integers",
            config.as_of_slot,
        )
    if any(slot > config.as_of_slot for slot in slot_fields):
        return _stale("adverse event is newer than as_of_slot", config)
    elapsed_fields = (
        event.collapse_start_elapsed_ms,
        event.peak_elapsed_ms,
        event.trough_elapsed_ms,
    )
    if any(not _non_negative_int(elapsed_ms) for elapsed_ms in elapsed_fields):
        return _unsupported(
            "adverse event elapsed time fields must be non-negative integers",
            config.as_of_slot,
        )
    if event.peak_elapsed_ms > event.trough_elapsed_ms:
        return _unsupported(
            "adverse event peak must not follow trough",
            config.as_of_slot,
        )
    if (
        event.collapse_start_slot != event.trough_slot
        or event.collapse_start_elapsed_ms != event.trough_elapsed_ms
    ):
        return _unsupported(
            "adverse event collapse start must match trough position",
            config.as_of_slot,
        )
    return None


def _validate_adverse_event_probabilities(
    event: AdverseEvent,
    config: OutcomeLabelConfig,
) -> AbstainResult | None:
    price_error = _validate_adverse_event_prices(event, config)
    if price_error is not None:
        return price_error
    if not _valid_probability_ppm(event.drawdown_ppm) or not _valid_probability_ppm(
        event.recovery_ppm
    ):
        return _unsupported(
            "adverse event drawdown and recovery must be probability ppm",
            config.as_of_slot,
        )
    return None


def _validate_adverse_event_prices(
    event: AdverseEvent,
    config: OutcomeLabelConfig,
) -> AbstainResult | None:
    if not _positive_int(event.peak_price_ppm) or not _positive_int(
        event.trough_price_ppm
    ):
        return _unsupported(
            "adverse event price fields must be positive",
            config.as_of_slot,
        )
    if event.peak_price_ppm < event.trough_price_ppm:
        return _unsupported(
            "adverse event peak price must not be below trough price",
            config.as_of_slot,
        )
    if not _positive_int(event.source_point_count):
        return _unsupported(
            "adverse event source_point_count must be positive",
            config.as_of_slot,
        )
    return None


def _ordered_points(
    points: tuple[OutcomeObservationPoint, ...],
) -> tuple[OutcomeObservationPoint, ...]:
    return tuple(
        sorted(
            points,
            key=lambda point: (point.elapsed_ms, int(point.slot), point.event_index),
        )
    )


def _max_drawdown_and_recovery(
    points: tuple[OutcomeObservationPoint, ...],
) -> _DrawdownRecovery:
    peak = points[0]
    max_drawdown_ppm = 0
    recovery_ppm = 0
    trough_index = 0
    for index, point in enumerate(points[1:], start=1):
        if point.price_quote_base_units_per_token_base_unit_ppm > (
            peak.price_quote_base_units_per_token_base_unit_ppm
        ):
            peak = point
            continue
        drawdown_ppm = _drawdown_ppm(
            peak.price_quote_base_units_per_token_base_unit_ppm,
            point.price_quote_base_units_per_token_base_unit_ppm,
        )
        if drawdown_ppm > max_drawdown_ppm:
            max_drawdown_ppm = drawdown_ppm
            trough_index = index
            recovery_ppm = 0

    if max_drawdown_ppm > 0:
        trough = points[trough_index]
        recovery_ppm = _recovery_ppm(
            trough=trough,
            future_points=points[trough_index + 1 :],
        )
    return _DrawdownRecovery(
        drawdown_ppm=max_drawdown_ppm,
        recovery_ppm=recovery_ppm,
    )


def _drawdown_ppm(peak_price_ppm: int, trough_price_ppm: int) -> int:
    if trough_price_ppm >= peak_price_ppm:
        return 0
    return (
        (peak_price_ppm - trough_price_ppm)
        * PROBABILITY_PPM_DENOMINATOR
        // peak_price_ppm
    )


def _recovery_ppm(
    *,
    trough: OutcomeObservationPoint,
    future_points: tuple[OutcomeObservationPoint, ...],
) -> int:
    if not future_points:
        return 0
    trough_price = trough.price_quote_base_units_per_token_base_unit_ppm
    best_recovery_price = max(
        point.price_quote_base_units_per_token_base_unit_ppm for point in future_points
    )
    if best_recovery_price <= trough_price:
        return 0
    return min(
        PROBABILITY_PPM_DENOMINATOR,
        (best_recovery_price - trough_price)
        * PROBABILITY_PPM_DENOMINATOR
        // trough_price,
    )


def _max_net_pnl_before_adverse_event(
    *,
    points: tuple[OutcomeObservationPoint, ...],
    config: OutcomeLabelConfig,
    adverse_event: AdverseEvent | None,
) -> int | None:
    eligible_points = tuple(
        point
        for point in points
        if adverse_event is None
        or point.elapsed_ms < adverse_event.collapse_start_elapsed_ms
    )
    if not eligible_points:
        return None
    return max(_net_pnl(point, config) for point in eligible_points)


def _net_pnl(point: OutcomeObservationPoint, config: OutcomeLabelConfig) -> int:
    return (
        int(point.full_exit_output_quote_base_units)
        - int(point.full_exit_execution_cost_quote_base_units)
        - int(config.entry_total_cost_quote_base_units)
    )


def _event_observed_by(
    adverse_event: AdverseEvent | None,
    horizon_ms: int,
) -> bool:
    return (
        adverse_event is not None
        and adverse_event.collapse_start_elapsed_ms <= horizon_ms
    )


def _combined_evidence_ids(
    points: tuple[OutcomeObservationPoint, ...],
    adverse_event: AdverseEvent | None,
) -> tuple[str, ...]:
    evidence_ids = list(_evidence_ids_for_points(points))
    if adverse_event is not None:
        evidence_ids.append(
            f"adverse-event:{adverse_event.detector_version}:"
            f"{int(adverse_event.collapse_start_slot)}"
        )
    return tuple(dict.fromkeys(evidence_ids))


def _evidence_ids_for_points(
    points: tuple[OutcomeObservationPoint, ...],
) -> tuple[str, ...]:
    evidence_ids: list[str] = []
    for point in points:
        evidence_ids.extend(point.evidence_ids)
    return tuple(dict.fromkeys(evidence_ids))


def _censored_evidence_ids(
    *,
    points: tuple[OutcomeObservationPoint, ...],
    horizon_points: tuple[OutcomeObservationPoint, ...],
) -> tuple[str, ...]:
    if horizon_points:
        return _evidence_ids_for_points(horizon_points)
    return _evidence_ids_for_points(points[:1])


def _valid_evidence_ids(evidence_ids: object) -> bool:
    return (
        type(evidence_ids) is tuple
        and bool(evidence_ids)
        and all(
            type(evidence_id) is str and evidence_id for evidence_id in evidence_ids
        )
    )


def _valid_non_empty_str(value: object) -> bool:
    return type(value) is str and bool(value)


def _valid_probability_ppm(value: object) -> bool:
    return _non_negative_int(value) and value <= PROBABILITY_PPM_DENOMINATOR


def _non_negative_int(value: object) -> bool:
    return type(value) is int and value >= 0


def _positive_int(value: object) -> bool:
    return type(value) is int and value > 0


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


def _stale(message: str, config: OutcomeLabelConfig) -> AbstainResult:
    return AbstainResult(
        reason=AbstainReason.STALE_STATE,
        message=message,
        as_of_slot=_abstain_slot(config.as_of_slot),
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
