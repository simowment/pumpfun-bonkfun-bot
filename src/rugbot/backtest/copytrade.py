"""Video-derived copy-trade policy for the canonical backtest runner.

The policy only consumes frozen, typed artifacts. It qualifies one entity from
completed launches available before the target decision slot, then simulates a
delayed fixed-size entry against executable outcome observation points. The
result is the existing :class:`BacktestLaunchResult` contract, so aggregation
and leakage checks remain owned by ``backtest.evaluation``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from rugbot.backtest.evaluation import (
    BacktestAction,
    BacktestFillStatus,
    BacktestLaunchResult,
    FrozenModelManifest,
    OrderingScenario,
)
from rugbot.domain.decisions import AbstainReason, AbstainResult
from rugbot.graph.point_in_time import PROBABILITY_PPM_DENOMINATOR
from rugbot.models.outcome_labels import (
    LaunchOutcomeLabels,
    OutcomeObservationPoint,
)

if TYPE_CHECKING:
    from rugbot.domain.amounts import QuoteBaseUnits, Slot

_WEEK_MS = 7 * 24 * 60 * 60 * 1_000
_Ppm = int


@dataclass(frozen=True, slots=True)
class CopyTradeConfig:
    """Research defaults extracted from the video, expressed as integers."""

    as_of_slot: Slot
    min_history_launch_count: int = 15
    max_history_launch_count: int = 20
    min_win_rate_ppm: _Ppm = 500_000
    max_weekly_buy_count: int = 300
    max_history_holding_time_ms: int | None = None
    max_entry_transaction_index: int = 1
    max_entry_market_cap_quote_base_units: QuoteBaseUnits = 0
    max_history_entry_deviation_ppm: _Ppm = 250_000
    copy_delay_ms: int = 0
    fixed_entry_quote_base_units: QuoteBaseUnits = 0
    min_exit_win_rate_ppm: _Ppm = 0
    exit_peak_descent_step_ppm: _Ppm = 250_000
    stop_loss_pnl_ppm: _Ppm | None = None


@dataclass(frozen=True, slots=True)
class CopyTradeHistorySample:
    """One completed entity launch used for point-in-time qualification."""

    as_of_slot: Slot
    launch_id: str
    token_mint: str
    wallet: str
    launch_slot: Slot
    launch_time_ms: int
    first_buy_transaction_index: int
    entry_market_cap_quote_base_units: QuoteBaseUnits
    entry_cost_quote_base_units: QuoteBaseUnits
    realized_net_pnl_quote_base_units: int
    holding_time_ms: int
    wallet_buy_elapsed_ms: int
    trajectory: tuple[OutcomeObservationPoint, ...]
    adverse_event_elapsed_ms: int | None
    evidence_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class CopyTradeLaunchCase:
    """Frozen target launch inputs for one delayed copy-trade simulation."""

    as_of_slot: Slot
    launch_id: str
    decision_id: str
    token_mint: str
    entity_id: str
    regime_id: str
    decision_slot: Slot
    decision_index: int
    wallet: str
    launch_time_ms: int
    wallet_buy_transaction_index: int
    wallet_buy_elapsed_ms: int
    entry_market_cap_quote_base_units: QuoteBaseUnits
    history: tuple[CopyTradeHistorySample, ...]
    trajectory: tuple[OutcomeObservationPoint, ...]
    outcome: LaunchOutcomeLabels
    evidence_ids: tuple[str, ...]


def evaluate_copy_trade_launches(
    *,
    cases: tuple[CopyTradeLaunchCase, ...],
    config: CopyTradeConfig,
    manifest: FrozenModelManifest,
) -> tuple[BacktestLaunchResult, ...] | AbstainResult:
    """Build canonical per-launch results from frozen copy-trade cases."""

    validation_error = _validate_inputs(cases=cases, config=config, manifest=manifest)
    if validation_error is not None:
        return validation_error
    results: list[BacktestLaunchResult] = []
    for case in cases:
        case_error = _validate_case(case, config)
        if case_error is not None:
            return case_error
        qualification = _qualify_entity(case=case, config=config)
        if qualification is not None:
            results.append(_skip(case, manifest, qualification))
            continue
        candidate_failure = _candidate_failure(case=case, config=config)
        if candidate_failure is not None:
            results.append(_skip(case, manifest, candidate_failure))
            continue
        exit_selection = _calibrate_exit(case=case, config=config)
        if isinstance(exit_selection, str):
            results.append(_abstain_launch(case, manifest, exit_selection))
            continue
        results.append(
            _simulate(
                case=case,
                config=config,
                manifest=manifest,
                take_profit_pnl_ppm=exit_selection,
            )
        )
    return tuple(results)


def _validate_inputs(  # noqa: PLR0911
    *,
    cases: object,
    config: object,
    manifest: object,
) -> AbstainResult | None:
    if not isinstance(config, CopyTradeConfig):
        return _abstain(AbstainReason.UNSUPPORTED_PROTOCOL_STATE, "invalid config", -1)
    if error := _validate_config(config):
        return error
    if type(cases) is not tuple or not cases:
        return _abstain(
            AbstainReason.MISSING_FEATURE, "cases are required", config.as_of_slot
        )
    if not isinstance(manifest, FrozenModelManifest):
        return _abstain(
            AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
            "invalid manifest",
            config.as_of_slot,
        )
    launch_ids = tuple(getattr(case, "launch_id", None) for case in cases)
    if any(type(value) is not str or not value for value in launch_ids):
        return _abstain(
            AbstainReason.MISSING_FEATURE, "missing launch ID", config.as_of_slot
        )
    if len(set(launch_ids)) != len(launch_ids):
        return _abstain(
            AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
            "duplicate launch ID",
            config.as_of_slot,
        )
    return None


def _validate_config(config: CopyTradeConfig) -> AbstainResult | None:
    integer_fields = (
        config.as_of_slot,
        config.min_history_launch_count,
        config.max_history_launch_count,
        config.min_win_rate_ppm,
        config.max_weekly_buy_count,
        config.max_entry_transaction_index,
        config.max_entry_market_cap_quote_base_units,
        config.max_history_entry_deviation_ppm,
        config.copy_delay_ms,
        config.fixed_entry_quote_base_units,
        config.min_exit_win_rate_ppm,
        config.exit_peak_descent_step_ppm,
    )
    optional_ints = (config.max_history_holding_time_ms, config.stop_loss_pnl_ppm)
    valid = (
        all(type(value) is int for value in integer_fields)
        and all(value is None or type(value) is int for value in optional_ints)
        and config.as_of_slot >= 0
        and 0 < config.min_history_launch_count <= config.max_history_launch_count
        and config.max_weekly_buy_count > 0
        and config.max_entry_transaction_index >= 0
        and config.max_entry_market_cap_quote_base_units > 0
        and config.fixed_entry_quote_base_units > 0
        and config.copy_delay_ms >= 0
        and config.exit_peak_descent_step_ppm > 0
        and all(
            0 <= value <= PROBABILITY_PPM_DENOMINATOR
            for value in (
                config.min_win_rate_ppm,
                config.min_exit_win_rate_ppm,
                config.max_history_entry_deviation_ppm,
            )
        )
        and (
            config.max_history_holding_time_ms is None
            or config.max_history_holding_time_ms >= 0
        )
        and (
            config.stop_loss_pnl_ppm is None
            or -PROBABILITY_PPM_DENOMINATOR <= config.stop_loss_pnl_ppm < 0
        )
    )
    if valid:
        return None
    return _abstain(
        AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
        "invalid copy-trade config",
        config.as_of_slot if type(config.as_of_slot) is int else -1,
    )


def _validate_case(  # noqa: C901, PLR0911
    case: CopyTradeLaunchCase, config: CopyTradeConfig
) -> AbstainResult | None:
    if not isinstance(case, CopyTradeLaunchCase):
        return _abstain(
            AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
            "invalid copy-trade case",
            config.as_of_slot,
        )
    identities = (
        case.launch_id,
        case.decision_id,
        case.token_mint,
        case.entity_id,
        case.regime_id,
        case.wallet,
    )
    positions = (
        case.as_of_slot,
        case.decision_slot,
        case.decision_index,
        case.launch_time_ms,
        case.wallet_buy_transaction_index,
        case.wallet_buy_elapsed_ms,
        case.entry_market_cap_quote_base_units,
    )
    if any(type(value) is not str or not value for value in identities):
        return _abstain(
            AbstainReason.MISSING_FEATURE,
            "missing copy-trade identity",
            config.as_of_slot,
        )
    if any(type(value) is not int or value < 0 for value in positions):
        return _abstain(
            AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
            "invalid copy-trade position",
            config.as_of_slot,
        )
    if case.as_of_slot != config.as_of_slot or case.decision_slot > config.as_of_slot:
        return _abstain(
            AbstainReason.STALE_STATE,
            "copy-trade case is outside its frozen boundary",
            config.as_of_slot,
        )
    if not _valid_ids(case.evidence_ids) or not isinstance(
        case.outcome, LaunchOutcomeLabels
    ):
        return _abstain(
            AbstainReason.MISSING_FEATURE,
            "missing copy-trade evidence",
            config.as_of_slot,
        )
    if (
        case.outcome.as_of_slot != config.as_of_slot
        or case.outcome.launch_id != case.launch_id
        or case.outcome.token_mint != case.token_mint
    ):
        return _abstain(
            AbstainReason.STALE_STATE,
            "copy-trade outcome does not match the frozen case",
            config.as_of_slot,
        )
    if (
        type(case.history) is not tuple
        or type(case.trajectory) is not tuple
        or not case.trajectory
    ):
        return _abstain(
            AbstainReason.MISSING_FEATURE,
            "copy-trade history or trajectory is missing",
            config.as_of_slot,
        )
    for sample in case.history:
        if not isinstance(sample, CopyTradeHistorySample):
            return _abstain(
                AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
                "invalid history sample",
                config.as_of_slot,
            )
        if type(sample.launch_slot) is not int or type(sample.as_of_slot) is not int:
            return _abstain(
                AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
                "invalid history position",
                config.as_of_slot,
            )
        if (
            sample.launch_slot < case.decision_slot
            and sample.as_of_slot <= case.decision_slot
        ):
            if error := _validate_trajectory(sample.trajectory, config):
                return error
    return _validate_trajectory(case.trajectory, config)


def _validate_trajectory(
    points: tuple[OutcomeObservationPoint, ...], config: CopyTradeConfig
) -> AbstainResult | None:
    if type(points) is not tuple or not points:
        return _abstain(
            AbstainReason.MISSING_FEATURE, "missing trajectory", config.as_of_slot
        )
    previous: tuple[int, int, int] | None = None
    for point in points:
        if not isinstance(point, OutcomeObservationPoint):
            return _abstain(
                AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
                "invalid trajectory point",
                config.as_of_slot,
            )
        values = (
            point.as_of_slot,
            point.slot,
            point.event_index,
            point.elapsed_ms,
            point.full_exit_output_quote_base_units,
            point.full_exit_execution_cost_quote_base_units,
        )
        if any(type(value) is not int or value < 0 for value in values):
            return _abstain(
                AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
                "invalid trajectory value",
                config.as_of_slot,
            )
        if point.slot > point.as_of_slot or point.as_of_slot > config.as_of_slot:
            return _abstain(
                AbstainReason.STALE_STATE,
                "trajectory is not finalized at the cutoff",
                config.as_of_slot,
            )
        position = (point.elapsed_ms, point.event_index, point.slot)
        if (previous is not None and position <= previous) or not _valid_ids(
            point.evidence_ids
        ):
            return _abstain(
                AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
                "invalid trajectory order or evidence",
                config.as_of_slot,
            )
        previous = position
    return None


def _qualify_entity(  # noqa: PLR0911
    *, case: CopyTradeLaunchCase, config: CopyTradeConfig
) -> str | None:
    eligible = _eligible_history(case=case)
    if len(eligible) < config.min_history_launch_count:
        return "copy_trade_insufficient_wallet_history"
    if any(not _valid_history_sample(sample, case, config) for sample in eligible):
        return "copy_trade_invalid_wallet_history"
    weekly_count = sum(
        sample.launch_time_ms >= case.launch_time_ms - _WEEK_MS for sample in eligible
    )
    if weekly_count > config.max_weekly_buy_count:
        return "copy_trade_wallet_too_active"
    selected = _recent_history(case=case, limit=config.max_history_launch_count)
    win_rate_ppm = (
        sum(sample.realized_net_pnl_quote_base_units > 0 for sample in selected)
        * PROBABILITY_PPM_DENOMINATOR
        // len(selected)
    )
    if win_rate_ppm < config.min_win_rate_ppm:
        return "copy_trade_wallet_win_rate_below_threshold"
    entry_caps = tuple(
        int(sample.entry_market_cap_quote_base_units) for sample in selected
    )
    minimum = min(entry_caps)
    if (
        max(entry_caps) - minimum
    ) * PROBABILITY_PPM_DENOMINATOR > minimum * config.max_history_entry_deviation_ppm:
        return "copy_trade_entry_market_cap_is_inconsistent"
    if config.max_history_holding_time_ms is not None and any(
        sample.holding_time_ms > config.max_history_holding_time_ms
        for sample in selected
    ):
        return "copy_trade_wallet_holding_time_is_unstable"
    return None


def _eligible_history(
    *, case: CopyTradeLaunchCase
) -> tuple[CopyTradeHistorySample, ...]:
    return tuple(
        sample
        for sample in case.history
        if sample.launch_slot < case.decision_slot
        and sample.as_of_slot <= case.decision_slot
    )


def _recent_history(
    *, case: CopyTradeLaunchCase, limit: int
) -> tuple[CopyTradeHistorySample, ...]:
    return tuple(
        sorted(
            _eligible_history(case=case),
            key=lambda sample: (sample.launch_slot, sample.launch_id),
        )
    )[-limit:]


def _calibrate_exit(  # noqa: C901
    *, case: CopyTradeLaunchCase, config: CopyTradeConfig
) -> int | str:
    selected = _recent_history(case=case, limit=config.max_history_launch_count)
    peak_values: list[int] = []
    for sample in selected:
        peak_pnl_ppm = _historical_peak_pnl_ppm(sample=sample, config=config)
        if peak_pnl_ppm is None:
            return "copy_trade_exit_calibration_missing"
        peak_values.append(peak_pnl_ppm)
    anchor_peak = sum(peak_values) // len(peak_values)
    if anchor_peak <= 0:
        return "copy_trade_non_positive_historical_peak"
    best_threshold: int | None = None
    best_score: tuple[int, int, int] | None = None
    for take_profit_pnl_ppm in _descending_peak_candidates(
        anchor_peak, config.exit_peak_descent_step_ppm
    ):
        pnl_values: list[int] = []
        for sample in selected:
            simulation = _simulate_path(
                points=sample.trajectory,
                wallet_buy_elapsed_ms=sample.wallet_buy_elapsed_ms,
                adverse_event_elapsed_ms=sample.adverse_event_elapsed_ms,
                entry_cost=int(config.fixed_entry_quote_base_units),
                copy_delay_ms=config.copy_delay_ms,
                take_profit_pnl_ppm=take_profit_pnl_ppm,
                stop_loss_pnl_ppm=config.stop_loss_pnl_ppm,
            )
            if simulation is None:
                return "copy_trade_exit_calibration_missing"
            pnl_values.append(simulation[1])
        win_rate_ppm = (
            sum(value > 0 for value in pnl_values)
            * PROBABILITY_PPM_DENOMINATOR
            // len(pnl_values)
        )
        if win_rate_ppm < config.min_exit_win_rate_ppm:
            continue
        mean_pnl = sum(pnl_values) // len(pnl_values)
        score = (mean_pnl, win_rate_ppm, take_profit_pnl_ppm)
        if best_score is None or score > best_score:
            best_threshold = take_profit_pnl_ppm
            best_score = score
    if best_threshold is None or best_score is None:
        return "copy_trade_no_exit_threshold_meets_win_rate"
    if best_score[0] <= 0:
        return "copy_trade_non_positive_historical_exit_pnl"
    return best_threshold


def _historical_peak_pnl_ppm(
    *, sample: CopyTradeHistorySample, config: CopyTradeConfig
) -> int | None:
    sellable = _sellable_points(
        points=sample.trajectory,
        wallet_buy_elapsed_ms=sample.wallet_buy_elapsed_ms,
        adverse_event_elapsed_ms=sample.adverse_event_elapsed_ms,
        copy_delay_ms=config.copy_delay_ms,
    )
    if not sellable:
        return None
    entry_cost = int(config.fixed_entry_quote_base_units)
    return max(
        (
            int(point.full_exit_output_quote_base_units)
            - int(point.full_exit_execution_cost_quote_base_units)
            - entry_cost
        )
        * PROBABILITY_PPM_DENOMINATOR
        // entry_cost
        for point in sellable
    )


def _descending_peak_candidates(anchor_peak: int, step_ppm: int) -> tuple[int, ...]:
    candidates: list[int] = []
    threshold = anchor_peak
    while threshold > 0:
        candidates.append(threshold)
        threshold = max(0, threshold - step_ppm)
    candidates.append(0)
    return tuple(dict.fromkeys(candidates))


def _valid_history_sample(
    sample: CopyTradeHistorySample, case: CopyTradeLaunchCase, config: CopyTradeConfig
) -> bool:
    values = (
        sample.as_of_slot,
        sample.launch_slot,
        sample.launch_time_ms,
        sample.first_buy_transaction_index,
        sample.entry_market_cap_quote_base_units,
        sample.entry_cost_quote_base_units,
        sample.realized_net_pnl_quote_base_units,
        sample.holding_time_ms,
        sample.wallet_buy_elapsed_ms,
    )
    return (
        all(type(value) is int for value in values)
        and sample.as_of_slot >= sample.launch_slot
        and sample.launch_slot < case.decision_slot
        and sample.launch_time_ms < case.launch_time_ms
        and isinstance(sample.wallet, str)
        and bool(sample.wallet)
        and sample.first_buy_transaction_index >= 0
        and sample.entry_market_cap_quote_base_units > 0
        and sample.entry_cost_quote_base_units == config.fixed_entry_quote_base_units
        and sample.holding_time_ms >= 0
        and sample.wallet_buy_elapsed_ms >= 0
        and type(sample.trajectory) is tuple
        and bool(sample.trajectory)
        and all(
            isinstance(point, OutcomeObservationPoint) for point in sample.trajectory
        )
        and (
            sample.adverse_event_elapsed_ms is None
            or (
                type(sample.adverse_event_elapsed_ms) is int
                and sample.adverse_event_elapsed_ms >= 0
            )
        )
        and _valid_ids(sample.evidence_ids)
        and sample.token_mint not in (case.token_mint, "")
        and sample.launch_id not in (case.launch_id, "")
        and sample.first_buy_transaction_index <= config.max_entry_transaction_index
    )


def _candidate_failure(
    *, case: CopyTradeLaunchCase, config: CopyTradeConfig
) -> str | None:
    if case.wallet_buy_transaction_index > config.max_entry_transaction_index:
        return "copy_trade_missed_block_0_or_1"
    if (
        case.entry_market_cap_quote_base_units
        > config.max_entry_market_cap_quote_base_units
    ):
        return "copy_trade_entry_market_cap_above_limit"
    return None


def _skip(
    case: CopyTradeLaunchCase, manifest: FrozenModelManifest, reason: str
) -> BacktestLaunchResult:
    return BacktestLaunchResult(
        as_of_slot=case.as_of_slot,
        launch_id=case.launch_id,
        decision_id=case.decision_id,
        token_mint=case.token_mint,
        entity_id=case.entity_id,
        regime_id=case.regime_id,
        decision_slot=case.decision_slot,
        decision_index=case.decision_index,
        action=BacktestAction.SKIP,
        fill_status=BacktestFillStatus.NOT_ATTEMPTED,
        ordering_scenario=None,
        net_pnl_quote_base_units=None,
        gross_profit_quote_base_units=None,
        execution_cost_quote_base_units=None,
        selected_size_quote_base_units=None,
        outcome=case.outcome,
        manifest=manifest,
        reason_codes=(reason,),
        evidence_ids=_combined_ids(case.evidence_ids, case.outcome.evidence_ids),
    )


def _abstain_launch(
    case: CopyTradeLaunchCase,
    manifest: FrozenModelManifest,
    reason: str,
) -> BacktestLaunchResult:
    return BacktestLaunchResult(
        as_of_slot=case.as_of_slot,
        launch_id=case.launch_id,
        decision_id=case.decision_id,
        token_mint=case.token_mint,
        entity_id=case.entity_id,
        regime_id=case.regime_id,
        decision_slot=case.decision_slot,
        decision_index=case.decision_index,
        action=BacktestAction.ABSTAIN,
        fill_status=BacktestFillStatus.NOT_ATTEMPTED,
        ordering_scenario=None,
        net_pnl_quote_base_units=None,
        gross_profit_quote_base_units=None,
        execution_cost_quote_base_units=None,
        selected_size_quote_base_units=None,
        outcome=case.outcome,
        manifest=manifest,
        reason_codes=(reason,),
        evidence_ids=_combined_ids(case.evidence_ids, case.outcome.evidence_ids),
    )


def _simulate(
    *,
    case: CopyTradeLaunchCase,
    config: CopyTradeConfig,
    manifest: FrozenModelManifest,
    take_profit_pnl_ppm: int,
) -> BacktestLaunchResult:
    simulation = _simulate_path(
        points=case.trajectory,
        wallet_buy_elapsed_ms=case.wallet_buy_elapsed_ms,
        adverse_event_elapsed_ms=case.outcome.first_material_adverse_event_elapsed_ms,
        entry_cost=int(config.fixed_entry_quote_base_units),
        copy_delay_ms=config.copy_delay_ms,
        take_profit_pnl_ppm=take_profit_pnl_ppm,
        stop_loss_pnl_ppm=config.stop_loss_pnl_ppm,
    )
    if simulation is None:
        return _unfilled(
            case, config, manifest, "copy_trade_no_observation_after_copy_delay"
        )
    entry_cost = int(config.fixed_entry_quote_base_units)
    exit_point, net_pnl = simulation
    output = int(exit_point.full_exit_output_quote_base_units)
    execution_cost = int(exit_point.full_exit_execution_cost_quote_base_units)
    gross_profit = max(0, output - entry_cost)
    return BacktestLaunchResult(
        as_of_slot=case.as_of_slot,
        launch_id=case.launch_id,
        decision_id=case.decision_id,
        token_mint=case.token_mint,
        entity_id=case.entity_id,
        regime_id=case.regime_id,
        decision_slot=case.decision_slot,
        decision_index=case.decision_index,
        action=BacktestAction.ENTER,
        fill_status=BacktestFillStatus.FILLED,
        ordering_scenario=OrderingScenario.OBSERVED_ORDER,
        net_pnl_quote_base_units=net_pnl,
        gross_profit_quote_base_units=gross_profit,
        execution_cost_quote_base_units=execution_cost,
        selected_size_quote_base_units=entry_cost,
        outcome=case.outcome,
        manifest=manifest,
        reason_codes=(
            "copy_trade_wallet_qualified",
            "copy_trade_entry_delayed",
            "copy_trade_exit_calibrated",
            f"copy_trade_tp_{take_profit_pnl_ppm}_ppm",
        ),
        evidence_ids=_combined_ids(
            case.evidence_ids, case.outcome.evidence_ids, exit_point.evidence_ids
        ),
    )


def _simulate_path(  # noqa: PLR0913
    *,
    points: tuple[OutcomeObservationPoint, ...],
    wallet_buy_elapsed_ms: int,
    adverse_event_elapsed_ms: int | None,
    entry_cost: int,
    copy_delay_ms: int,
    take_profit_pnl_ppm: int,
    stop_loss_pnl_ppm: int | None,
) -> tuple[OutcomeObservationPoint, int] | None:
    sellable = _sellable_points(
        points=points,
        wallet_buy_elapsed_ms=wallet_buy_elapsed_ms,
        adverse_event_elapsed_ms=adverse_event_elapsed_ms,
        copy_delay_ms=copy_delay_ms,
    )
    if not sellable:
        return None
    exit_point = _select_exit(
        sellable,
        entry_cost,
        take_profit_pnl_ppm,
        stop_loss_pnl_ppm,
    )
    net_pnl = (
        int(exit_point.full_exit_output_quote_base_units)
        - int(exit_point.full_exit_execution_cost_quote_base_units)
        - entry_cost
    )
    return exit_point, net_pnl


def _sellable_points(
    *,
    points: tuple[OutcomeObservationPoint, ...],
    wallet_buy_elapsed_ms: int,
    adverse_event_elapsed_ms: int | None,
    copy_delay_ms: int,
) -> tuple[OutcomeObservationPoint, ...] | None:
    entry_deadline = wallet_buy_elapsed_ms + copy_delay_ms
    entry_position = next(
        (
            index
            for index, point in enumerate(points)
            if point.elapsed_ms >= entry_deadline
        ),
        None,
    )
    if entry_position is None:
        return None
    sellable = tuple(
        point
        for point in points[entry_position:]
        if adverse_event_elapsed_ms is None
        or point.elapsed_ms < adverse_event_elapsed_ms
    )
    return sellable or None


def _select_exit(
    points: tuple[OutcomeObservationPoint, ...],
    entry_cost: int,
    take_profit_pnl_ppm: int,
    stop_loss_pnl_ppm: int | None,
) -> OutcomeObservationPoint:
    take_profit = _ceil_div(
        entry_cost * take_profit_pnl_ppm, PROBABILITY_PPM_DENOMINATOR
    )
    stop_loss = (
        None
        if stop_loss_pnl_ppm is None
        else -_ceil_div(
            entry_cost * abs(stop_loss_pnl_ppm), PROBABILITY_PPM_DENOMINATOR
        )
    )
    for point in points:
        pnl = (
            int(point.full_exit_output_quote_base_units)
            - int(point.full_exit_execution_cost_quote_base_units)
            - entry_cost
        )
        if pnl >= take_profit or (stop_loss is not None and pnl <= stop_loss):
            return point
    return points[-1]


def _unfilled(
    case: CopyTradeLaunchCase,
    config: CopyTradeConfig,
    manifest: FrozenModelManifest,
    reason: str,
) -> BacktestLaunchResult:
    return BacktestLaunchResult(
        as_of_slot=case.as_of_slot,
        launch_id=case.launch_id,
        decision_id=case.decision_id,
        token_mint=case.token_mint,
        entity_id=case.entity_id,
        regime_id=case.regime_id,
        decision_slot=case.decision_slot,
        decision_index=case.decision_index,
        action=BacktestAction.ENTER,
        fill_status=BacktestFillStatus.UNFILLED,
        ordering_scenario=OrderingScenario.OBSERVED_ORDER,
        net_pnl_quote_base_units=0,
        gross_profit_quote_base_units=0,
        execution_cost_quote_base_units=0,
        selected_size_quote_base_units=int(config.fixed_entry_quote_base_units),
        outcome=case.outcome,
        manifest=manifest,
        reason_codes=("copy_trade_wallet_qualified", reason),
        evidence_ids=_combined_ids(case.evidence_ids, case.outcome.evidence_ids),
    )


def _combined_ids(*groups: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(identifier for group in groups for identifier in group))


def _valid_ids(values: tuple[str, ...]) -> bool:
    return (
        type(values) is tuple
        and bool(values)
        and all(type(value) is str and bool(value) for value in values)
    )


def _ceil_div(numerator: int, denominator: int) -> int:
    return (numerator + denominator - 1) // denominator


def _abstain(reason: AbstainReason, message: str, as_of_slot: int) -> AbstainResult:
    return AbstainResult(reason=reason, message=message, as_of_slot=as_of_slot)


__all__ = [
    "CopyTradeConfig",
    "CopyTradeHistorySample",
    "CopyTradeLaunchCase",
    "evaluate_copy_trade_launches",
]
