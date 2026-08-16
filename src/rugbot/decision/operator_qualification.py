"""Pure point-in-time qualification of a known adverse operator."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from rugbot.domain.amounts import QuoteBaseUnits, Slot
from rugbot.domain.decisions import AbstainReason
from rugbot.graph.point_in_time import PROBABILITY_PPM_DENOMINATOR


class QualificationStatus(Enum):
    """Terminal status returned by operator qualification."""

    QUALIFIED = "QUALIFIED"
    ABSTAIN = "ABSTAIN"


@dataclass(frozen=True, slots=True)
class CompletedLaunchOutcome:
    """Completed historical outcome attributed to one resolved entity."""

    as_of_slot: Slot
    entity_id: str
    launch_id: str
    launch_slot: Slot
    completed_slot: Slot
    completed: bool
    realized_net_pnl_quote_base_units: QuoteBaseUnits
    peak_net_pnl_quote_base_units: QuoteBaseUnits
    adverse_event_observed: bool
    evidence_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class WalletEntityEvidence:
    """Point-in-time wallet-to-entity evidence for one historical launch."""

    as_of_slot: Slot
    observed_slot: Slot
    entity_id: str
    launch_id: str
    wallet: str
    entity_probability_ppm: int
    evidence_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class OperatorQualificationConfig:
    """Integer thresholds for one point-in-time qualification decision."""

    as_of_slot: Slot
    entity_id: str
    min_sample_count: int = 3
    min_win_rate_ppm: int = 500_000
    min_expectancy_quote_base_units: int = 0
    min_peak_pnl_quote_base_units: int = 0
    min_adverse_launch_count: int = 2
    min_adverse_rate_ppm: int = 500_000
    min_entity_probability_ppm: int = 500_000


@dataclass(frozen=True, slots=True)
class OperatorQualification:
    """Immutable qualification metrics and reason codes at one slot boundary."""

    status: QualificationStatus
    as_of_slot: Slot
    entity_id: str
    sample_count: int
    win_count: int
    win_rate_ppm: int
    expectancy_quote_base_units: QuoteBaseUnits
    average_peak_pnl_quote_base_units: QuoteBaseUnits
    adverse_launch_count: int
    adverse_rate_ppm: int
    repeated_adverse_behavior: bool
    matched_wallet_count: int
    reason_codes: tuple[str, ...]
    evidence_ids: tuple[str, ...]
    message: str = ""
    abstain_reason: AbstainReason | None = None

    @property
    def peak_pnl_quote_base_units(self) -> QuoteBaseUnits:
        """Return the integer historical average peak used for qualification."""

        return self.average_peak_pnl_quote_base_units


def qualify_operator(
    *,
    outcomes: tuple[CompletedLaunchOutcome, ...],
    entity_evidence: tuple[WalletEntityEvidence, ...],
    config: OperatorQualificationConfig,
) -> OperatorQualification:
    """Qualify an operator using only evidence available at ``config.as_of_slot``.

    The function does not fetch, mutate, or infer evidence.  A threshold miss
    produces ``ABSTAIN`` with computed metrics; malformed, missing, or future
    evidence also produces ``ABSTAIN`` and an explicit reason code.
    """

    config_error = _validate_config(config)
    if config_error is not None:
        return config_error
    input_error = _validate_input_shapes(
        outcomes=outcomes,
        entity_evidence=entity_evidence,
        config=config,
    )
    if input_error is not None:
        return input_error

    outcome_error = _validate_outcomes(outcomes=outcomes, config=config)
    if outcome_error is not None:
        return outcome_error
    evidence_error = _validate_entity_evidence(
        outcomes=outcomes,
        entity_evidence=entity_evidence,
        config=config,
    )
    if evidence_error is not None:
        return evidence_error

    matched_evidence = _matched_evidence(
        outcomes=outcomes,
        entity_evidence=entity_evidence,
        config=config,
    )
    if isinstance(matched_evidence, OperatorQualification):
        return matched_evidence
    return _build_result(
        outcomes=outcomes,
        matched_evidence=matched_evidence,
        config=config,
    )


def _build_result(
    *,
    outcomes: tuple[CompletedLaunchOutcome, ...],
    matched_evidence: tuple[WalletEntityEvidence, ...],
    config: OperatorQualificationConfig,
) -> OperatorQualification:
    sample_count = len(outcomes)
    win_count = sum(
        outcome.realized_net_pnl_quote_base_units > 0 for outcome in outcomes
    )
    adverse_launch_count = sum(outcome.adverse_event_observed for outcome in outcomes)
    win_rate_ppm = win_count * PROBABILITY_PPM_DENOMINATOR // sample_count
    adverse_rate_ppm = (
        adverse_launch_count * PROBABILITY_PPM_DENOMINATOR // sample_count
    )
    expectancy = (
        sum(outcome.realized_net_pnl_quote_base_units for outcome in outcomes)
        // sample_count
    )
    average_peak = (
        sum(outcome.peak_net_pnl_quote_base_units for outcome in outcomes)
        // sample_count
    )
    repeated_adverse = (
        adverse_launch_count >= config.min_adverse_launch_count
        and adverse_rate_ppm >= config.min_adverse_rate_ppm
    )

    reasons = [
        (
            "minimum_sample_met"
            if sample_count >= config.min_sample_count
            else "insufficient_sample"
        ),
        (
            "win_rate_threshold_met"
            if win_rate_ppm >= config.min_win_rate_ppm
            else "win_rate_below_threshold"
        ),
        (
            "expectancy_threshold_met"
            if expectancy >= config.min_expectancy_quote_base_units
            else "expectancy_below_threshold"
        ),
        (
            "peak_threshold_met"
            if average_peak >= config.min_peak_pnl_quote_base_units
            else "peak_below_threshold"
        ),
        (
            "repeated_adverse_behavior_confirmed"
            if repeated_adverse
            else "repeated_adverse_behavior_not_confirmed"
        ),
        "entity_evidence_confirmed",
    ]
    qualifies = (
        sample_count >= config.min_sample_count
        and win_rate_ppm >= config.min_win_rate_ppm
        and expectancy >= config.min_expectancy_quote_base_units
        and average_peak >= config.min_peak_pnl_quote_base_units
        and repeated_adverse
    )
    if qualifies:
        reasons.append("operator_qualified")
        status = QualificationStatus.QUALIFIED
        abstain_reason = None
    else:
        reasons.append("operator_qualification_abstained")
        status = QualificationStatus.ABSTAIN
        abstain_reason = AbstainReason.MISSING_FEATURE

    return OperatorQualification(
        status=status,
        as_of_slot=config.as_of_slot,
        entity_id=config.entity_id,
        sample_count=sample_count,
        win_count=win_count,
        win_rate_ppm=win_rate_ppm,
        expectancy_quote_base_units=expectancy,
        average_peak_pnl_quote_base_units=average_peak,
        adverse_launch_count=adverse_launch_count,
        adverse_rate_ppm=adverse_rate_ppm,
        repeated_adverse_behavior=repeated_adverse,
        matched_wallet_count=len({item.wallet for item in matched_evidence}),
        reason_codes=tuple(reasons),
        evidence_ids=_combined_evidence_ids(outcomes, matched_evidence),
        message="",
        abstain_reason=abstain_reason,
    )


def _matched_evidence(
    *,
    outcomes: tuple[CompletedLaunchOutcome, ...],
    entity_evidence: tuple[WalletEntityEvidence, ...],
    config: OperatorQualificationConfig,
) -> tuple[WalletEntityEvidence, ...] | OperatorQualification:
    outcome_ids = {outcome.launch_id for outcome in outcomes}
    matched = tuple(
        item
        for item in entity_evidence
        if item.entity_id == config.entity_id
        and item.launch_id in outcome_ids
        and item.entity_probability_ppm >= config.min_entity_probability_ppm
    )
    covered_launches = {item.launch_id for item in matched}
    if covered_launches == outcome_ids:
        return matched
    missing = sorted(outcome_ids - covered_launches)
    return _abstain(
        config=config,
        reason=AbstainReason.MISSING_FEATURE,
        reason_code="missing_entity_evidence",
        message=f"missing entity evidence for launches: {','.join(missing)}",
    )


def _validate_config(config: object) -> OperatorQualification | None:
    if not isinstance(config, OperatorQualificationConfig):
        return _abstain(
            config=None,
            reason=AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
            reason_code="malformed_qualification_config",
            message="qualification config is malformed",
        )
    integer_fields = (
        config.as_of_slot,
        config.min_sample_count,
        config.min_win_rate_ppm,
        config.min_expectancy_quote_base_units,
        config.min_peak_pnl_quote_base_units,
        config.min_adverse_launch_count,
        config.min_adverse_rate_ppm,
        config.min_entity_probability_ppm,
    )
    if any(type(value) is not int for value in integer_fields):
        return _abstain(
            config=config,
            reason=AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
            reason_code="malformed_qualification_threshold",
            message="qualification thresholds must be integers",
        )
    if config.as_of_slot < 0 or not config.entity_id:
        return _abstain(
            config=config,
            reason=AbstainReason.MISSING_FEATURE,
            reason_code="missing_qualification_identity",
            message="qualification slot and entity are required",
        )
    if config.min_sample_count <= 0 or config.min_adverse_launch_count <= 0:
        return _abstain(
            config=config,
            reason=AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
            reason_code="invalid_sample_threshold",
            message="sample thresholds must be positive",
        )
    if any(
        value < 0 or value > PROBABILITY_PPM_DENOMINATOR
        for value in (
            config.min_win_rate_ppm,
            config.min_adverse_rate_ppm,
            config.min_entity_probability_ppm,
        )
    ):
        return _abstain(
            config=config,
            reason=AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
            reason_code="invalid_probability_threshold",
            message="probability thresholds are outside ppm range",
        )
    return None


def _validate_input_shapes(
    *,
    outcomes: object,
    entity_evidence: object,
    config: OperatorQualificationConfig,
) -> OperatorQualification | None:
    if type(outcomes) is not tuple or not outcomes:
        return _abstain(
            config=config,
            reason=AbstainReason.MISSING_FEATURE,
            reason_code="historical_outcomes_required",
            message="completed historical outcomes are required",
        )
    if type(entity_evidence) is not tuple or not entity_evidence:
        return _abstain(
            config=config,
            reason=AbstainReason.MISSING_FEATURE,
            reason_code="entity_evidence_required",
            message="wallet/entity evidence is required",
        )
    return None


def _validate_outcomes(  # noqa: C901, PLR0911
    *,
    outcomes: tuple[CompletedLaunchOutcome, ...],
    config: OperatorQualificationConfig,
) -> OperatorQualification | None:
    seen_launches: set[str] = set()
    for outcome in outcomes:
        if not isinstance(outcome, CompletedLaunchOutcome):
            return _abstain(
                config=config,
                reason=AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
                reason_code="malformed_historical_outcome",
                message="historical outcome is malformed",
            )
        if outcome.launch_id in seen_launches:
            return _abstain(
                config=config,
                reason=AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
                reason_code="duplicate_historical_outcome",
                message="launch IDs must be unique",
            )
        seen_launches.add(outcome.launch_id)
        if outcome.entity_id != config.entity_id:
            return _abstain(
                config=config,
                reason=AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
                reason_code="outcome_entity_mismatch",
                message="historical outcome belongs to another entity",
            )
        if not _valid_outcome_identity(outcome):
            return _abstain(
                config=config,
                reason=AbstainReason.MISSING_FEATURE,
                reason_code="missing_historical_outcome_identity",
                message="historical outcome identity is required",
            )
        stale = _future_outcome_field(outcome, config.as_of_slot)
        if stale:
            return _abstain(
                config=config,
                reason=AbstainReason.STALE_STATE,
                reason_code="future_outcome_evidence",
                message=f"historical outcome field is after slot {config.as_of_slot}",
            )
        if not outcome.completed:
            return _abstain(
                config=config,
                reason=AbstainReason.MISSING_FEATURE,
                reason_code="incomplete_historical_outcome",
                message="qualification requires completed outcomes",
            )
        if outcome.completed_slot < outcome.launch_slot:
            return _abstain(
                config=config,
                reason=AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
                reason_code="invalid_outcome_order",
                message="completion must follow launch",
            )
        if not _valid_evidence_ids(outcome.evidence_ids):
            return _abstain(
                config=config,
                reason=AbstainReason.MISSING_FEATURE,
                reason_code="outcome_evidence_ids_required",
                message="historical outcome provenance is required",
            )
        if type(outcome.adverse_event_observed) is not bool:
            return _abstain(
                config=config,
                reason=AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
                reason_code="malformed_adverse_event_flag",
                message="adverse event flag must be boolean",
            )
    return None


def _validate_entity_evidence(  # noqa: C901, PLR0911
    *,
    outcomes: tuple[CompletedLaunchOutcome, ...],
    entity_evidence: tuple[WalletEntityEvidence, ...],
    config: OperatorQualificationConfig,
) -> OperatorQualification | None:
    outcome_ids = {outcome.launch_id for outcome in outcomes}
    seen_pairs: set[tuple[str, str]] = set()
    for item in entity_evidence:
        if not isinstance(item, WalletEntityEvidence):
            return _abstain(
                config=config,
                reason=AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
                reason_code="malformed_entity_evidence",
                message="wallet/entity evidence is malformed",
            )
        if item.entity_id != config.entity_id:
            return _abstain(
                config=config,
                reason=AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
                reason_code="evidence_entity_mismatch",
                message="wallet/entity evidence belongs to another entity",
            )
        if item.launch_id not in outcome_ids:
            return _abstain(
                config=config,
                reason=AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
                reason_code="evidence_launch_mismatch",
                message="wallet/entity evidence references an unknown launch",
            )
        pair = (item.launch_id, item.wallet)
        if pair in seen_pairs:
            return _abstain(
                config=config,
                reason=AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
                reason_code="duplicate_entity_evidence",
                message="wallet/entity evidence pairs must be unique",
            )
        seen_pairs.add(pair)
        if not _valid_evidence_identity(item):
            return _abstain(
                config=config,
                reason=AbstainReason.MISSING_FEATURE,
                reason_code="missing_entity_evidence_identity",
                message="wallet/entity evidence identity is required",
            )
        if (
            item.as_of_slot > config.as_of_slot
            or item.observed_slot > config.as_of_slot
        ):
            return _abstain(
                config=config,
                reason=AbstainReason.STALE_STATE,
                reason_code="future_entity_evidence",
                message=f"entity evidence is after slot {config.as_of_slot}",
            )
        if item.observed_slot > item.as_of_slot:
            return _abstain(
                config=config,
                reason=AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
                reason_code="invalid_entity_evidence_order",
                message="evidence observation follows its snapshot slot",
            )
        if type(item.entity_probability_ppm) is not int or not (
            0 <= item.entity_probability_ppm <= PROBABILITY_PPM_DENOMINATOR
        ):
            return _abstain(
                config=config,
                reason=AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
                reason_code="invalid_entity_probability",
                message="entity probability is outside ppm range",
            )
        if not _valid_evidence_ids(item.evidence_ids):
            return _abstain(
                config=config,
                reason=AbstainReason.MISSING_FEATURE,
                reason_code="entity_evidence_ids_required",
                message="wallet/entity provenance is required",
            )
    return None


def _valid_outcome_identity(outcome: CompletedLaunchOutcome) -> bool:
    return (
        type(outcome.as_of_slot) is int
        and type(outcome.launch_slot) is int
        and type(outcome.completed_slot) is int
        and type(outcome.realized_net_pnl_quote_base_units) is int
        and type(outcome.peak_net_pnl_quote_base_units) is int
        and all(
            isinstance(value, str) and bool(value)
            for value in (outcome.entity_id, outcome.launch_id)
        )
        and all(
            value >= 0
            for value in (
                outcome.as_of_slot,
                outcome.launch_slot,
                outcome.completed_slot,
            )
        )
    )


def _future_outcome_field(
    outcome: CompletedLaunchOutcome,
    as_of_slot: Slot,
) -> bool:
    return any(
        value > as_of_slot
        for value in (outcome.as_of_slot, outcome.launch_slot, outcome.completed_slot)
    )


def _valid_evidence_identity(item: WalletEntityEvidence) -> bool:
    return (
        type(item.as_of_slot) is int
        and type(item.observed_slot) is int
        and all(
            isinstance(value, str) and bool(value)
            for value in (item.entity_id, item.launch_id, item.wallet)
        )
        and item.as_of_slot >= 0
        and item.observed_slot >= 0
    )


def _valid_evidence_ids(evidence_ids: object) -> bool:
    return (
        type(evidence_ids) is tuple
        and bool(evidence_ids)
        and len(set(evidence_ids)) == len(evidence_ids)
        and all(isinstance(value, str) and bool(value) for value in evidence_ids)
    )


def _combined_evidence_ids(
    outcomes: tuple[CompletedLaunchOutcome, ...],
    entity_evidence: tuple[WalletEntityEvidence, ...],
) -> tuple[str, ...]:
    return tuple(
        sorted(
            {evidence_id for item in outcomes for evidence_id in item.evidence_ids}
            | {
                evidence_id
                for item in entity_evidence
                for evidence_id in item.evidence_ids
            }
        )
    )


def _abstain(
    *,
    config: OperatorQualificationConfig | None,
    reason: AbstainReason,
    reason_code: str,
    message: str,
) -> OperatorQualification:
    if config is None or type(config.as_of_slot) is not int:
        as_of_slot = Slot(-1)
        entity_id = ""
    else:
        as_of_slot = config.as_of_slot
        entity_id = config.entity_id if isinstance(config.entity_id, str) else ""
    return OperatorQualification(
        status=QualificationStatus.ABSTAIN,
        as_of_slot=as_of_slot,
        entity_id=entity_id,
        sample_count=0,
        win_count=0,
        win_rate_ppm=0,
        expectancy_quote_base_units=0,
        average_peak_pnl_quote_base_units=0,
        adverse_launch_count=0,
        adverse_rate_ppm=0,
        repeated_adverse_behavior=False,
        matched_wallet_count=0,
        reason_codes=(reason_code,),
        evidence_ids=(),
        message=message,
        abstain_reason=reason,
    )


__all__ = [
    "CompletedLaunchOutcome",
    "OperatorQualification",
    "OperatorQualificationConfig",
    "QualificationStatus",
    "WalletEntityEvidence",
    "qualify_operator",
]
