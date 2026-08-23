"""Pure adverse insider sell detection and attribution contracts."""

from dataclasses import dataclass

from rugbot.domain.amounts import (
    PROBABILITY_PPM_DENOMINATOR,
    QuoteBaseUnits,
    Slot,
    TokenBaseUnits,
)
from rugbot.domain.decisions import AbstainReason, AbstainResult


@dataclass(frozen=True, slots=True)
class MarketTrajectoryPoint:
    """Point-in-time market observation used for collapse detection."""

    as_of_slot: Slot
    slot: Slot
    event_index: int
    elapsed_ms: int
    price_quote_base_units_per_token_base_unit_ppm: int
    real_quote_reserves_base_units: QuoteBaseUnits | None
    curve_progress_ppm: int | None


@dataclass(frozen=True, slots=True)
class AdverseEventDetectionConfig:
    """Thresholds for material adverse-event detection."""

    as_of_slot: Slot
    token_mint: str
    detector_version: str
    min_peak_price_ppm: int
    min_drawdown_ppm: int
    recovery_window_ms: int


@dataclass(frozen=True, slots=True)
class AdverseEvent:
    """Detected material price-collapse event before blame assignment."""

    as_of_slot: Slot
    token_mint: str
    collapse_start_slot: Slot
    collapse_start_elapsed_ms: int
    peak_slot: Slot
    peak_elapsed_ms: int
    peak_price_ppm: int
    trough_slot: Slot
    trough_elapsed_ms: int
    trough_price_ppm: int
    drawdown_ppm: int
    recovery_ppm: int
    detector_version: str
    source_point_count: int


@dataclass(frozen=True, slots=True)
class AdverseEventDetection:
    """Adverse-event detector output."""

    as_of_slot: Slot
    event: AdverseEvent | None
    reason_codes: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class _PeakTroughCollapse:
    peak: MarketTrajectoryPoint
    trough: MarketTrajectoryPoint
    drawdown_ppm: int
    recovery_ppm: int


@dataclass(frozen=True, slots=True)
class CandidateDumpSell:
    """Candidate sell that may have contributed to an adverse event."""

    as_of_slot: Slot
    slot: Slot
    transaction_index: int
    signature: bytes
    elapsed_ms: int
    seller_wallet: str
    base_amount_base_units: TokenBaseUnits
    quote_amount_base_units: QuoteBaseUnits
    price_impact_ppm: int
    same_controller_probability_ppm: int
    cooperating_wallet_probability_ppm: int
    evidence_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class DumpAttributionConfig:
    """Thresholds for probabilistic dump-sell attribution."""

    as_of_slot: Slot
    attribution_version: str
    pre_collapse_window_ms: int
    post_collapse_window_ms: int
    min_cluster_probability_ppm: int


@dataclass(frozen=True, slots=True)
class ResponsibleSell:
    """Probabilistically attributed sell evidence."""

    as_of_slot: Slot
    slot: Slot
    transaction_index: int
    signature: bytes
    elapsed_ms: int
    seller_wallet: str
    base_amount_base_units: TokenBaseUnits
    quote_amount_base_units: QuoteBaseUnits
    price_impact_ppm: int
    cluster_probability_ppm: int
    evidence_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class DumpAttribution:
    """Point-in-time attribution result for a detected adverse event."""

    as_of_slot: Slot
    token_mint: str
    attribution_version: str
    responsible_sells: tuple[ResponsibleSell, ...]
    probable_dump_wallets: tuple[str, ...]
    attributed_sell_base_units: TokenBaseUnits
    max_sell_price_impact_ppm: int
    attribution_confidence_ppm: int
    reason_codes: tuple[str, ...]


def detect_adverse_event(
    *,
    points: tuple[MarketTrajectoryPoint, ...],
    config: AdverseEventDetectionConfig,
) -> AdverseEventDetection | AbstainResult:
    """Detect a material collapse from a point-in-time market trajectory."""

    validation_error = _validate_detection_inputs(points, config)
    if validation_error is not None:
        return validation_error

    ordered_points = tuple(
        sorted(points, key=lambda point: (int(point.slot), point.event_index))
    )
    peak = ordered_points[0]
    strongest_event: AdverseEvent | None = None
    for point in ordered_points[1:]:
        if _is_new_peak(point, peak):
            peak = point
            continue
        if (
            peak.price_quote_base_units_per_token_base_unit_ppm
            < config.min_peak_price_ppm
        ):
            continue

        drawdown_ppm = _drawdown_ppm(
            peak.price_quote_base_units_per_token_base_unit_ppm,
            point.price_quote_base_units_per_token_base_unit_ppm,
        )
        if drawdown_ppm < config.min_drawdown_ppm:
            continue

        recovery_ppm = _recovery_ppm(
            trough=point,
            future_points=_points_in_recovery_window(
                ordered_points,
                trough=point,
                recovery_window_ms=config.recovery_window_ms,
            ),
        )
        event = _event_from_peak_trough(
            collapse=_PeakTroughCollapse(
                peak=peak,
                trough=point,
                drawdown_ppm=drawdown_ppm,
                recovery_ppm=recovery_ppm,
            ),
            point_count=len(ordered_points),
            config=config,
        )
        if strongest_event is None or event.drawdown_ppm > strongest_event.drawdown_ppm:
            strongest_event = event

    if strongest_event is None:
        return AdverseEventDetection(
            as_of_slot=config.as_of_slot,
            event=None,
            reason_codes=("no_material_adverse_event",),
        )
    return AdverseEventDetection(
        as_of_slot=config.as_of_slot,
        event=strongest_event,
        reason_codes=("material_adverse_event_detected",),
    )


def attribute_dump_sells(
    *,
    event: AdverseEvent,
    candidates: tuple[CandidateDumpSell, ...],
    config: DumpAttributionConfig,
) -> DumpAttribution | AbstainResult:
    """Attribute candidate sells around a detected adverse event."""

    validation_error = _validate_attribution_inputs(event, candidates, config)
    if validation_error is not None:
        return validation_error

    responsible_sells = tuple(
        _responsible_sell(candidate)
        for candidate in sorted(
            candidates,
            key=lambda sell: (int(sell.slot), sell.transaction_index, sell.signature),
        )
        if _candidate_in_attribution_window(candidate, event, config)
        and _cluster_probability_ppm(candidate) >= config.min_cluster_probability_ppm
    )

    if not responsible_sells:
        return DumpAttribution(
            as_of_slot=config.as_of_slot,
            token_mint=event.token_mint,
            attribution_version=config.attribution_version,
            responsible_sells=(),
            probable_dump_wallets=(),
            attributed_sell_base_units=TokenBaseUnits(0),
            max_sell_price_impact_ppm=0,
            attribution_confidence_ppm=0,
            reason_codes=("no_candidate_above_attribution_threshold",),
        )

    return DumpAttribution(
        as_of_slot=config.as_of_slot,
        token_mint=event.token_mint,
        attribution_version=config.attribution_version,
        responsible_sells=responsible_sells,
        probable_dump_wallets=_unique_wallets(responsible_sells),
        attributed_sell_base_units=TokenBaseUnits(
            sum(int(sell.base_amount_base_units) for sell in responsible_sells)
        ),
        max_sell_price_impact_ppm=max(
            sell.price_impact_ppm for sell in responsible_sells
        ),
        attribution_confidence_ppm=max(
            sell.cluster_probability_ppm for sell in responsible_sells
        ),
        reason_codes=("dump_sells_attributed",),
    )


def _validate_detection_inputs(
    points: tuple[MarketTrajectoryPoint, ...],
    config: AdverseEventDetectionConfig,
) -> AbstainResult | None:
    config_error = _validate_detection_config(config)
    if config_error is not None:
        return config_error
    if not points:
        return _missing("market trajectory points are required", config.as_of_slot)
    for point in points:
        point_error = _validate_trajectory_point(point, config)
        if point_error is not None:
            return point_error
    return None


def _validate_detection_config(
    config: AdverseEventDetectionConfig,
) -> AbstainResult | None:
    if not _non_negative_int(config.as_of_slot):
        return _unsupported("as_of_slot must be non-negative", config.as_of_slot)
    if not isinstance(config.token_mint, str) or not config.token_mint:
        return _missing("token_mint is required", config.as_of_slot)
    if not isinstance(config.detector_version, str) or not config.detector_version:
        return _decoder_mismatch("detector_version is required", config.as_of_slot)
    return _validate_detection_thresholds(config)


def _validate_detection_thresholds(
    config: AdverseEventDetectionConfig,
) -> AbstainResult | None:
    if not _positive_int(config.min_peak_price_ppm):
        return _unsupported("min_peak_price_ppm must be positive", config.as_of_slot)
    if not _valid_probability_ppm(config.min_drawdown_ppm):
        return _unsupported(
            "min_drawdown_ppm must be in probability ppm range",
            config.as_of_slot,
        )
    if not _non_negative_int(config.recovery_window_ms):
        return _unsupported(
            "recovery_window_ms must be non-negative",
            config.as_of_slot,
        )
    return None


def _validate_trajectory_point(
    point: MarketTrajectoryPoint,
    config: AdverseEventDetectionConfig,
) -> AbstainResult | None:
    for validation in (
        _validate_point_slot,
        _validate_point_ordering,
        _validate_point_price,
        _validate_point_curve_progress,
        _validate_point_reserves,
    ):
        validation_error = validation(point, config)
        if validation_error is not None:
            return validation_error
    return None


def _validate_point_slot(
    point: MarketTrajectoryPoint,
    config: AdverseEventDetectionConfig,
) -> AbstainResult | None:
    if not _non_negative_int(point.as_of_slot) or not _non_negative_int(point.slot):
        return _unsupported(
            "market trajectory point slot fields must be non-negative integers",
            config.as_of_slot,
        )
    if point.as_of_slot != config.as_of_slot:
        return _stale("market trajectory point uses a stale as_of_slot", config)
    if point.slot > config.as_of_slot:
        return _stale("market trajectory point is newer than as_of_slot", config)
    return None


def _validate_point_ordering(
    point: MarketTrajectoryPoint,
    config: AdverseEventDetectionConfig,
) -> AbstainResult | None:
    if not _non_negative_int(point.event_index) or not _non_negative_int(
        point.elapsed_ms
    ):
        return _unsupported(
            "market trajectory point ordering fields must be non-negative",
            config.as_of_slot,
        )
    return None


def _validate_point_price(
    point: MarketTrajectoryPoint,
    config: AdverseEventDetectionConfig,
) -> AbstainResult | None:
    if not _positive_int(point.price_quote_base_units_per_token_base_unit_ppm):
        return _unsupported(
            "market trajectory price must be positive",
            config.as_of_slot,
        )
    return None


def _validate_point_curve_progress(
    point: MarketTrajectoryPoint,
    config: AdverseEventDetectionConfig,
) -> AbstainResult | None:
    if point.curve_progress_ppm is not None and not _valid_probability_ppm(
        point.curve_progress_ppm
    ):
        return _unsupported(
            "curve_progress_ppm must be in probability ppm range",
            config.as_of_slot,
        )
    return None


def _validate_point_reserves(
    point: MarketTrajectoryPoint,
    config: AdverseEventDetectionConfig,
) -> AbstainResult | None:
    if point.real_quote_reserves_base_units is not None and not _non_negative_int(
        point.real_quote_reserves_base_units
    ):
        return _unsupported(
            "real_quote_reserves_base_units must be non-negative",
            config.as_of_slot,
        )
    return None


def _validate_attribution_inputs(
    event: AdverseEvent,
    candidates: tuple[CandidateDumpSell, ...],
    config: DumpAttributionConfig,
) -> AbstainResult | None:
    config_error = _validate_attribution_config(event, config)
    if config_error is not None:
        return config_error
    if not candidates:
        return _missing("candidate dump-sell evidence is required", config.as_of_slot)

    event_error = _validate_event_for_attribution(event)
    if event_error is not None:
        return event_error

    for candidate in candidates:
        candidate_error = _validate_candidate_sell(candidate, config)
        if candidate_error is not None:
            return candidate_error
    return None


def _validate_attribution_config(
    event: AdverseEvent,
    config: DumpAttributionConfig,
) -> AbstainResult | None:
    slot_error = _validate_attribution_slots(event, config)
    if slot_error is not None:
        return slot_error
    if (
        not isinstance(config.attribution_version, str)
        or not config.attribution_version
    ):
        return _decoder_mismatch("attribution_version is required", config.as_of_slot)
    return _validate_attribution_thresholds(config)


def _validate_attribution_slots(
    event: AdverseEvent,
    config: DumpAttributionConfig,
) -> AbstainResult | None:
    if not _non_negative_int(config.as_of_slot):
        return _unsupported("as_of_slot must be non-negative", config.as_of_slot)
    if not _non_negative_int(event.as_of_slot):
        return _unsupported("event as_of_slot must be non-negative", config.as_of_slot)
    if config.as_of_slot != event.as_of_slot:
        return _stale_attribution(
            "event and attribution config use different slots",
            event,
        )
    return None


def _validate_attribution_thresholds(
    config: DumpAttributionConfig,
) -> AbstainResult | None:
    if not _non_negative_int(config.pre_collapse_window_ms) or not _non_negative_int(
        config.post_collapse_window_ms
    ):
        return _unsupported(
            "attribution windows must be non-negative",
            config.as_of_slot,
        )
    if not _valid_probability_ppm(config.min_cluster_probability_ppm):
        return _unsupported(
            "min_cluster_probability_ppm must be in probability ppm range",
            config.as_of_slot,
        )
    return None


def _validate_event_for_attribution(event: AdverseEvent) -> AbstainResult | None:
    if not isinstance(event.token_mint, str) or not event.token_mint:
        return _missing("event token_mint is required", event.as_of_slot)
    if not isinstance(event.detector_version, str) or not event.detector_version:
        return _decoder_mismatch("event detector_version is required", event.as_of_slot)
    event_slot_error = _validate_event_slots(event)
    if event_slot_error is not None:
        return event_slot_error
    event_numeric_error = _validate_event_numeric_fields(event)
    if event_numeric_error is not None:
        return event_numeric_error
    if not _valid_probability_ppm(event.drawdown_ppm) or not _valid_probability_ppm(
        event.recovery_ppm
    ):
        return _unsupported(
            "event drawdown and recovery must be in probability ppm range",
            event.as_of_slot,
        )
    return None


def _validate_event_slots(event: AdverseEvent) -> AbstainResult | None:
    slot_fields = (
        event.as_of_slot,
        event.collapse_start_slot,
        event.peak_slot,
        event.trough_slot,
    )
    if any(not _non_negative_int(slot) for slot in slot_fields):
        return _unsupported(
            "event slot fields must be non-negative integers",
            event.as_of_slot,
        )
    if (
        event.collapse_start_slot > event.as_of_slot
        or event.peak_slot > event.as_of_slot
        or event.trough_slot > event.as_of_slot
    ):
        return _stale_attribution("event slot is newer than as_of_slot", event)
    return None


def _validate_event_numeric_fields(event: AdverseEvent) -> AbstainResult | None:
    if not _non_negative_int(event.collapse_start_elapsed_ms):
        return _unsupported(
            "event collapse elapsed time must be non-negative",
            event.as_of_slot,
        )
    if not _non_negative_int(event.peak_elapsed_ms) or not _non_negative_int(
        event.trough_elapsed_ms
    ):
        return _unsupported(
            "event elapsed time fields must be non-negative",
            event.as_of_slot,
        )
    if not _positive_int(event.peak_price_ppm) or not _positive_int(
        event.trough_price_ppm
    ):
        return _unsupported(
            "event price fields must be positive",
            event.as_of_slot,
        )
    if not _positive_int(event.source_point_count):
        return _unsupported(
            "event source_point_count must be positive",
            event.as_of_slot,
        )
    return None


def _validate_candidate_sell(
    candidate: CandidateDumpSell,
    config: DumpAttributionConfig,
) -> AbstainResult | None:
    for validation in (
        _validate_candidate_slot,
        _validate_candidate_ordering,
        _validate_candidate_identity,
        _validate_candidate_amounts,
        _validate_candidate_price_impact,
        _validate_candidate_probabilities,
        _validate_candidate_evidence,
    ):
        validation_error = validation(candidate, config)
        if validation_error is not None:
            return validation_error
    return None


def _validate_candidate_slot(
    candidate: CandidateDumpSell,
    config: DumpAttributionConfig,
) -> AbstainResult | None:
    if not _non_negative_int(candidate.as_of_slot) or not _non_negative_int(
        candidate.slot
    ):
        return _stale_candidate("candidate dump-sell slot is invalid", config)
    if candidate.as_of_slot != config.as_of_slot:
        return _stale_candidate(
            "candidate dump-sell evidence uses a stale as_of_slot",
            config,
        )
    if candidate.slot > config.as_of_slot:
        return _stale_candidate("candidate dump-sell slot is invalid", config)
    return None


def _validate_candidate_ordering(
    candidate: CandidateDumpSell,
    config: DumpAttributionConfig,
) -> AbstainResult | None:
    if not _non_negative_int(candidate.transaction_index) or not _non_negative_int(
        candidate.elapsed_ms
    ):
        return _unsupported(
            "candidate dump-sell ordering fields must be non-negative",
            config.as_of_slot,
        )
    return None


def _validate_candidate_identity(
    candidate: CandidateDumpSell,
    config: DumpAttributionConfig,
) -> AbstainResult | None:
    if not isinstance(candidate.signature, bytes) or not candidate.signature:
        return _missing("candidate dump-sell signature is required", config.as_of_slot)
    if not isinstance(candidate.seller_wallet, str) or not candidate.seller_wallet:
        return _missing("candidate dump-sell wallet is required", config.as_of_slot)
    return None


def _validate_candidate_amounts(
    candidate: CandidateDumpSell,
    config: DumpAttributionConfig,
) -> AbstainResult | None:
    if not _positive_int(candidate.base_amount_base_units):
        return _unsupported(
            "candidate dump-sell base amount must be positive",
            config.as_of_slot,
        )
    if not _non_negative_int(candidate.quote_amount_base_units):
        return _unsupported(
            "candidate dump-sell quote amount must be non-negative",
            config.as_of_slot,
        )
    return None


def _validate_candidate_price_impact(
    candidate: CandidateDumpSell,
    config: DumpAttributionConfig,
) -> AbstainResult | None:
    if not _valid_probability_ppm(candidate.price_impact_ppm):
        return _unsupported(
            "candidate dump-sell price impact must be in probability ppm range",
            config.as_of_slot,
        )
    return None


def _validate_candidate_evidence(
    candidate: CandidateDumpSell,
    config: DumpAttributionConfig,
) -> AbstainResult | None:
    if not candidate.evidence_ids or any(
        not isinstance(evidence_id, str) or not evidence_id
        for evidence_id in candidate.evidence_ids
    ):
        return _missing(
            "candidate dump-sell evidence_ids are required", config.as_of_slot
        )
    return None


def _validate_candidate_probabilities(
    candidate: CandidateDumpSell,
    config: DumpAttributionConfig,
) -> AbstainResult | None:
    probability_fields = {
        "same_controller_probability_ppm": candidate.same_controller_probability_ppm,
        "cooperating_wallet_probability_ppm": (
            candidate.cooperating_wallet_probability_ppm
        ),
    }
    for field_name, value in probability_fields.items():
        if not _valid_probability_ppm(value):
            return _unsupported(
                f"{field_name} must be in probability ppm range",
                config.as_of_slot,
            )
    return None


def _is_new_peak(point: MarketTrajectoryPoint, peak: MarketTrajectoryPoint) -> bool:
    return (
        point.price_quote_base_units_per_token_base_unit_ppm
        > peak.price_quote_base_units_per_token_base_unit_ppm
    )


def _drawdown_ppm(peak_price_ppm: int, trough_price_ppm: int) -> int:
    if trough_price_ppm >= peak_price_ppm:
        return 0
    return (
        (peak_price_ppm - trough_price_ppm)
        * PROBABILITY_PPM_DENOMINATOR
        // (peak_price_ppm)
    )


def _recovery_ppm(
    *,
    trough: MarketTrajectoryPoint,
    future_points: tuple[MarketTrajectoryPoint, ...],
) -> int:
    if not future_points:
        return 0
    trough_price = trough.price_quote_base_units_per_token_base_unit_ppm
    if trough_price <= 0:
        return 0
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


def _points_in_recovery_window(
    points: tuple[MarketTrajectoryPoint, ...],
    *,
    trough: MarketTrajectoryPoint,
    recovery_window_ms: int,
) -> tuple[MarketTrajectoryPoint, ...]:
    recovery_deadline_ms = trough.elapsed_ms + recovery_window_ms
    return tuple(
        point
        for point in points
        if point.elapsed_ms > trough.elapsed_ms
        and point.elapsed_ms <= recovery_deadline_ms
    )


def _event_from_peak_trough(
    *,
    collapse: _PeakTroughCollapse,
    point_count: int,
    config: AdverseEventDetectionConfig,
) -> AdverseEvent:
    peak = collapse.peak
    trough = collapse.trough
    return AdverseEvent(
        as_of_slot=config.as_of_slot,
        token_mint=config.token_mint,
        collapse_start_slot=trough.slot,
        collapse_start_elapsed_ms=trough.elapsed_ms,
        peak_slot=peak.slot,
        peak_elapsed_ms=peak.elapsed_ms,
        peak_price_ppm=peak.price_quote_base_units_per_token_base_unit_ppm,
        trough_slot=trough.slot,
        trough_elapsed_ms=trough.elapsed_ms,
        trough_price_ppm=trough.price_quote_base_units_per_token_base_unit_ppm,
        drawdown_ppm=collapse.drawdown_ppm,
        recovery_ppm=collapse.recovery_ppm,
        detector_version=config.detector_version,
        source_point_count=point_count,
    )


def _candidate_in_attribution_window(
    candidate: CandidateDumpSell,
    event: AdverseEvent,
    config: DumpAttributionConfig,
) -> bool:
    earliest_ms = event.collapse_start_elapsed_ms - config.pre_collapse_window_ms
    latest_ms = event.collapse_start_elapsed_ms + config.post_collapse_window_ms
    return earliest_ms <= candidate.elapsed_ms <= latest_ms


def _cluster_probability_ppm(candidate: CandidateDumpSell) -> int:
    return max(
        candidate.same_controller_probability_ppm,
        candidate.cooperating_wallet_probability_ppm,
    )


def _responsible_sell(candidate: CandidateDumpSell) -> ResponsibleSell:
    return ResponsibleSell(
        as_of_slot=candidate.as_of_slot,
        slot=candidate.slot,
        transaction_index=candidate.transaction_index,
        signature=candidate.signature,
        elapsed_ms=candidate.elapsed_ms,
        seller_wallet=candidate.seller_wallet,
        base_amount_base_units=candidate.base_amount_base_units,
        quote_amount_base_units=candidate.quote_amount_base_units,
        price_impact_ppm=candidate.price_impact_ppm,
        cluster_probability_ppm=_cluster_probability_ppm(candidate),
        evidence_ids=candidate.evidence_ids,
    )


def _unique_wallets(sells: tuple[ResponsibleSell, ...]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(sell.seller_wallet for sell in sells))


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


def _stale(
    message: str,
    config: AdverseEventDetectionConfig,
) -> AbstainResult:
    return AbstainResult(
        reason=AbstainReason.STALE_STATE,
        message=message,
        as_of_slot=_abstain_slot(config.as_of_slot),
    )


def _stale_attribution(message: str, event: AdverseEvent) -> AbstainResult:
    return AbstainResult(
        reason=AbstainReason.STALE_STATE,
        message=message,
        as_of_slot=_abstain_slot(event.as_of_slot),
    )


def _stale_candidate(
    message: str,
    config: DumpAttributionConfig,
) -> AbstainResult:
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
