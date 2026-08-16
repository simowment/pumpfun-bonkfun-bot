"""Immutable, non-signing paper position lifecycle."""

from __future__ import annotations

from dataclasses import dataclass, field, replace

from rugbot.decision.playbook_rules import (
    PROBABILITY_PPM_DENOMINATOR,
    ExitRuleAction,
    ExitRuleInput,
    ExitRuleState,
    PlaybookRules,
    SellLevel,
    evaluate_exit_rules,
)
from rugbot.domain.amounts import QuoteBaseUnits, Slot, TokenBaseUnits
from rugbot.domain.decisions import AbstainReason, AbstainResult
from rugbot.execution.ports import MAX_SLIPPAGE_BPS, ExecutionIntent


@dataclass(frozen=True, slots=True)
class CalibratedExitEvidence:
    """Point-in-time CopyTrade exit calibration for one open market."""

    as_of_slot: Slot
    market_id: str
    take_profit_pnl_ppm: int
    adverse_event_slot: Slot | None = None


@dataclass(frozen=True, slots=True)
class PositionMarketEvidence:
    """Immutable point-in-time market and PnL evidence for one position."""

    as_of_slot: Slot
    market_id: str
    current_pnl_ppm: int
    idle_ms: int
    executable_exit_capacity_base_units: TokenBaseUnits | None
    current_market_cap_quote_base_units: QuoteBaseUnits | None = None
    calibrated_exit_evidence: CalibratedExitEvidence | None = None


@dataclass(frozen=True, slots=True)
class PaperPositionState:
    """State carried between paper position evaluations."""

    as_of_slot: Slot
    market_id: str
    original_position_base_units: TokenBaseUnits
    current_position_base_units: TokenBaseUnits
    peak_pnl_ppm: int = 0
    exit_rule_state: ExitRuleState = field(default_factory=ExitRuleState)
    emitted_sell_intent_count: int = 0


@dataclass(frozen=True, slots=True)
class PositionRuntimeDecision:
    """A hold or paper sell decision and its next immutable state."""

    action: ExitRuleAction
    as_of_slot: Slot
    sell_intent: ExecutionIntent | None
    reason_codes: tuple[str, ...]
    next_state: PaperPositionState


PositionRuntimeOutcome = PositionRuntimeDecision | AbstainResult


def advance_paper_position(  # noqa: PLR0913
    *,
    rules: PlaybookRules,
    evidence: PositionMarketEvidence,
    state: PaperPositionState,
    max_slippage_bps: int,
    require_full_exit_capacity: bool = True,
    require_calibrated_exit: bool = False,
) -> PositionRuntimeOutcome:
    """Advance one paper position without I/O or transaction submission.

    The function delegates TP, SL, trailing-stop, and inactivity behavior to
    the existing pure exit evaluator. It only translates a validated sell
    decision into an execution intent; submitting or signing is outside this
    module.
    """

    validation_error = _validate_inputs(
        evidence=evidence,
        state=state,
        max_slippage_bps=max_slippage_bps,
        require_full_exit_capacity=require_full_exit_capacity,
        require_calibrated_exit=require_calibrated_exit,
    )
    if validation_error is not None:
        return validation_error

    capacity_error = _required_full_exit_capacity_error(
        evidence=evidence,
        state=state,
        required=require_full_exit_capacity,
    )
    if capacity_error is not None:
        return capacity_error

    calibration = evidence.calibrated_exit_evidence
    effective_rules = _rules_for_calibration(rules, calibration)
    peak_pnl_ppm = max(state.peak_pnl_ppm, evidence.current_pnl_ppm)
    decision = evaluate_exit_rules(
        rules=effective_rules,
        evidence=ExitRuleInput(
            as_of_slot=evidence.as_of_slot,
            current_pnl_ppm=evidence.current_pnl_ppm,
            peak_pnl_ppm=peak_pnl_ppm,
            current_market_cap_quote_base_units=(
                evidence.current_market_cap_quote_base_units
            ),
            idle_ms=(
                1
                if calibration is not None
                and calibration.adverse_event_slot is not None
                else evidence.idle_ms
            ),
        ),
        state=state.exit_rule_state,
        current_position_base_units=state.current_position_base_units,
        original_position_base_units=state.original_position_base_units,
    )
    if isinstance(decision, AbstainResult):
        return decision
    if calibration is not None and calibration.adverse_event_slot is not None:
        decision = replace(decision, reason_codes=("calibrated_adverse_event",))

    observed_state = replace(
        state,
        as_of_slot=evidence.as_of_slot,
        peak_pnl_ppm=peak_pnl_ppm,
        exit_rule_state=decision.next_state,
    )
    if decision.action is ExitRuleAction.HOLD:
        return PositionRuntimeDecision(
            action=decision.action,
            as_of_slot=evidence.as_of_slot,
            sell_intent=None,
            reason_codes=decision.reason_codes,
            next_state=observed_state,
        )

    sell_capacity_error = _sell_capacity_error(
        evidence=evidence,
        sell_amount_base_units=decision.sell_amount_base_units,
    )
    if sell_capacity_error is not None:
        return sell_capacity_error

    sell_intent = ExecutionIntent(
        intent_id=(
            f"{state.market_id}:{evidence.as_of_slot}:paper-sell:"
            f"{state.emitted_sell_intent_count}"
        ),
        as_of_slot=evidence.as_of_slot,
        market_id=state.market_id,
        side="sell",
        quote_amount_base_units=None,
        base_amount_base_units=decision.sell_amount_base_units,
        max_slippage_bps=max_slippage_bps,
        reason_codes=decision.reason_codes,
    )
    next_state = replace(
        observed_state,
        current_position_base_units=TokenBaseUnits(
            state.current_position_base_units - decision.sell_amount_base_units
        ),
        emitted_sell_intent_count=state.emitted_sell_intent_count + 1,
    )
    return PositionRuntimeDecision(
        action=decision.action,
        as_of_slot=evidence.as_of_slot,
        sell_intent=sell_intent,
        reason_codes=decision.reason_codes,
        next_state=next_state,
    )


def _validate_inputs(  # noqa: PLR0911
    *,
    evidence: object,
    state: object,
    max_slippage_bps: object,
    require_full_exit_capacity: object,
    require_calibrated_exit: object,
) -> AbstainResult | None:
    if not isinstance(evidence, PositionMarketEvidence):
        return _abstain(
            AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
            "position evidence is malformed",
            -1,
        )
    if not isinstance(state, PaperPositionState):
        return _abstain(
            AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
            "paper position state is malformed",
            evidence.as_of_slot,
        )
    if (
        not _non_negative_int(evidence.as_of_slot)
        or type(evidence.market_id) is not str
        or not evidence.market_id
    ):
        return _abstain(
            AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
            "position evidence identity is malformed",
            evidence.as_of_slot,
        )
    if evidence.market_id != state.market_id:
        return _abstain(
            AbstainReason.DECODER_MISMATCH,
            "position evidence does not match the open market",
            evidence.as_of_slot,
        )
    if not _valid_state(state):
        return _abstain(
            AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
            "paper position state values are malformed",
            evidence.as_of_slot,
        )
    if evidence.as_of_slot <= state.as_of_slot:
        return _abstain(
            AbstainReason.STALE_STATE,
            "position evidence does not advance the state slot",
            evidence.as_of_slot,
        )
    if not _valid_evidence_values(evidence):
        return _abstain(
            AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
            "position market or PnL evidence is malformed",
            evidence.as_of_slot,
        )
    calibration_error = _validate_calibration(
        evidence=evidence,
        state=state,
        require=require_calibrated_exit,
    )
    if calibration_error is not None:
        return calibration_error
    if (
        type(max_slippage_bps) is not int
        or not 0 <= max_slippage_bps <= MAX_SLIPPAGE_BPS
        or type(require_full_exit_capacity) is not bool
        or type(require_calibrated_exit) is not bool
    ):
        return _abstain(
            AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
            "paper position execution controls are malformed",
            evidence.as_of_slot,
        )
    return None


def _valid_state(state: PaperPositionState) -> bool:
    return (
        _non_negative_int(state.as_of_slot)
        and type(state.market_id) is str
        and bool(state.market_id)
        and _positive_int(state.original_position_base_units)
        and _positive_int(state.current_position_base_units)
        and state.current_position_base_units <= state.original_position_base_units
        and type(state.peak_pnl_ppm) is int
        and isinstance(state.exit_rule_state, ExitRuleState)
        and _non_negative_int(state.emitted_sell_intent_count)
    )


def _valid_evidence_values(evidence: PositionMarketEvidence) -> bool:
    market_cap = evidence.current_market_cap_quote_base_units
    capacity = evidence.executable_exit_capacity_base_units
    return (
        type(evidence.current_pnl_ppm) is int
        and _non_negative_int(evidence.idle_ms)
        and (market_cap is None or _non_negative_int(market_cap))
        and (capacity is None or _non_negative_int(capacity))
    )


def _validate_calibration(  # noqa: PLR0911
    *,
    evidence: PositionMarketEvidence,
    state: PaperPositionState,
    require: object,
) -> AbstainResult | None:
    calibration = evidence.calibrated_exit_evidence
    if type(require) is not bool:
        return _abstain(
            AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
            "calibrated exit requirement is malformed",
            evidence.as_of_slot,
        )
    if calibration is None:
        if require:
            return _abstain(
                AbstainReason.MISSING_FEATURE,
                "point-in-time calibrated exit evidence is required",
                evidence.as_of_slot,
            )
        return None
    if not isinstance(calibration, CalibratedExitEvidence):
        return _abstain(
            AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
            "calibrated exit evidence is malformed",
            evidence.as_of_slot,
        )
    if calibration.market_id != state.market_id:
        return _abstain(
            AbstainReason.DECODER_MISMATCH,
            "calibrated exit evidence does not match the open market",
            evidence.as_of_slot,
        )
    if not _non_negative_int(calibration.as_of_slot) or not _positive_int(
        calibration.take_profit_pnl_ppm
    ):
        return _abstain(
            AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
            "calibrated exit threshold is malformed",
            evidence.as_of_slot,
        )
    if calibration.as_of_slot > evidence.as_of_slot:
        return _abstain(
            AbstainReason.STALE_STATE,
            "calibrated exit evidence is newer than market evidence",
            evidence.as_of_slot,
        )
    adverse_event_slot = calibration.adverse_event_slot
    if adverse_event_slot is not None and (
        not _non_negative_int(adverse_event_slot)
        or adverse_event_slot > calibration.as_of_slot
    ):
        return _abstain(
            AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
            "calibrated adverse-event evidence is malformed",
            evidence.as_of_slot,
        )
    return None


def _rules_for_calibration(
    rules: PlaybookRules, calibration: CalibratedExitEvidence | None
) -> PlaybookRules:
    if calibration is None:
        return rules
    return replace(
        rules,
        sell=replace(
            rules.sell,
            take_profit_levels=(
                SellLevel(
                    trigger_pnl_ppm=calibration.take_profit_pnl_ppm,
                    sell_fraction_ppm=PROBABILITY_PPM_DENOMINATOR,
                ),
            ),
            stop_loss_levels=(),
            no_activity_timeout_ms=(
                1
                if calibration.adverse_event_slot is not None
                else rules.sell.no_activity_timeout_ms
            ),
        ),
    )


def _required_full_exit_capacity_error(
    *,
    evidence: PositionMarketEvidence,
    state: PaperPositionState,
    required: bool,
) -> AbstainResult | None:
    if not required:
        return None
    capacity = evidence.executable_exit_capacity_base_units
    if capacity is None:
        return _abstain(
            AbstainReason.MISSING_FEATURE,
            "full-exit capacity evidence is required",
            evidence.as_of_slot,
        )
    if capacity < state.current_position_base_units:
        return _abstain(
            AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
            "current position exceeds executable full-exit capacity",
            evidence.as_of_slot,
        )
    return None


def _sell_capacity_error(
    *,
    evidence: PositionMarketEvidence,
    sell_amount_base_units: int,
) -> AbstainResult | None:
    capacity = evidence.executable_exit_capacity_base_units
    if capacity is None:
        return _abstain(
            AbstainReason.MISSING_FEATURE,
            "executable sell capacity evidence is required",
            evidence.as_of_slot,
        )
    if capacity < sell_amount_base_units:
        return _abstain(
            AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
            "sell amount exceeds executable exit capacity",
            evidence.as_of_slot,
        )
    return None


def _abstain(reason: AbstainReason, message: str, as_of_slot: int) -> AbstainResult:
    return AbstainResult(reason=reason, message=message, as_of_slot=as_of_slot)


def _non_negative_int(value: object) -> bool:
    return type(value) is int and value >= 0


def _positive_int(value: object) -> bool:
    return type(value) is int and value > 0
