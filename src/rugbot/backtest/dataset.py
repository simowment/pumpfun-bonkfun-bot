"""Finalized observation dataset construction and OOS backtest execution.

This module is the boundary between immutable finalized observations and the
typed copy-trade/backtest contracts.  It deliberately does not decode a new
protocol format: launches use the existing pinned observation decoder, while
trade rows are accepted only when a finalized fill producer has already
provided their typed amounts and provenance.
"""

# These validators are intentionally explicit and fail closed at the dataset
# boundary rather than hiding malformed evidence behind a generic adapter.
# ruff: noqa: PLR0911, TC001

from __future__ import annotations

from dataclasses import dataclass, replace

from rugbot.backtest.copytrade import (
    CopyTradeConfig,
    CopyTradeHistorySample,
    CopyTradeLaunchCase,
    evaluate_copy_trade_launches,
)
from rugbot.backtest.evaluation import (
    BacktestConfig,
    BacktestLaunchResult,
    BacktestReport,
    FrozenModelManifest,
    build_backtest_report,
)
from rugbot.domain.amounts import QuoteBaseUnits, Slot, TokenBaseUnits
from rugbot.domain.decisions import AbstainReason, AbstainResult
from rugbot.domain.launches import LaunchCreatedV2
from rugbot.domain.observations import RawChainObservation
from rugbot.domain.trades import TradeSide
from rugbot.graph.point_in_time import PROBABILITY_PPM_DENOMINATOR
from rugbot.ingest.pump_create_observation import (
    decode_pump_create_v2_observation,
)
from rugbot.models.outcome_labels import (
    HorizonOutcomeLabel,
    LaunchOutcomeLabels,
    OutcomeObservationPoint,
)
from rugbot.storage.jsonl_observation_store import observation_identity


@dataclass(frozen=True, slots=True)
class FinalizedTrade:
    """One typed, executed trade joined to its immutable raw observation.

    The row is intentionally a fill contract, not an instruction contract.
    The existing protocol trade decoder proves instruction arguments only; it
    cannot prove executed amounts without finalized transaction metadata and
    account state.  A producer must therefore supply those exact amounts here.
    """

    as_of_slot: Slot
    launch_id: str
    token_mint: str
    wallet: str
    side: TradeSide
    slot: Slot
    transaction_index: int
    signature: bytes
    base_amount_base_units: TokenBaseUnits
    quote_amount_base_units: QuoteBaseUnits
    execution_cost_quote_base_units: QuoteBaseUnits
    evidence_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class FinalizedBacktestDataset:
    """Immutable launches, trades, cases, and source observations."""

    as_of_slot: Slot
    observations: tuple[RawChainObservation, ...]
    launches: tuple[LaunchCreatedV2, ...]
    trades: tuple[FinalizedTrade, ...]
    cases: tuple[CopyTradeLaunchCase, ...]
    evidence_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class FullExitStressConfig:
    """Integer full-exit stress applied to every historical trajectory point."""

    as_of_slot: Slot
    output_haircut_ppm: int
    additional_execution_cost_quote_base_units: QuoteBaseUnits


@dataclass(frozen=True, slots=True)
class FinalizedBacktestResult:
    """OOS report plus the stressed per-launch results used to build it."""

    dataset: FinalizedBacktestDataset
    evaluated_launches: tuple[BacktestLaunchResult, ...]
    report: BacktestReport
    full_exit_stress_applied: bool
    stressed_point_count: int


def build_finalized_dataset(
    *,
    observations: tuple[RawChainObservation, ...],
    cases: tuple[CopyTradeLaunchCase, ...],
    trades: tuple[FinalizedTrade, ...],
    as_of_slot: Slot,
) -> FinalizedBacktestDataset | AbstainResult:
    """Build a typed dataset from finalized immutable observations.

    Launches are decoded from the observations with the canonical pinned Pump
    decoder.  Cases provide the already-derived point-in-time features and
    executable outcome trajectories; this function validates their temporal
    join without deriving future features from labels.  Trade rows are joined
    by canonical transaction evidence rather than the ingestion UUID.
    """

    validation = _validate_dataset_inputs(
        observations=observations,
        cases=cases,
        trades=trades,
        as_of_slot=as_of_slot,
    )
    if validation is not None:
        return validation

    ordered_observations = _ordered_observations(observations)
    decoded_launches: list[LaunchCreatedV2] = []
    for observation in ordered_observations:
        decoded = decode_pump_create_v2_observation(observation)
        if isinstance(decoded, AbstainResult):
            return decoded
        if decoded is not None:
            decoded_launches.append(decoded)

    launches = tuple(decoded_launches)
    launch_error = _validate_launches(launches, as_of_slot)
    if launch_error is not None:
        return launch_error
    case_error = _validate_cases_against_launches(cases, launches, as_of_slot)
    if case_error is not None:
        return case_error
    trade_error = _validate_trades_against_observations(
        trades=trades,
        launches=launches,
        observations=ordered_observations,
        as_of_slot=as_of_slot,
    )
    if trade_error is not None:
        return trade_error

    return FinalizedBacktestDataset(
        as_of_slot=as_of_slot,
        observations=ordered_observations,
        launches=launches,
        trades=tuple(trades),
        cases=tuple(cases),
        evidence_ids=_dataset_evidence_ids(
            observations=ordered_observations,
            launches=launches,
            trades=trades,
            cases=cases,
        ),
    )


def run_finalized_backtest(
    *,
    dataset: FinalizedBacktestDataset,
    strategy: CopyTradeConfig,
    manifest: FrozenModelManifest,
    backtest_config: BacktestConfig,
    stress: FullExitStressConfig,
) -> FinalizedBacktestResult | AbstainResult:
    """Run the shared strategy and produce a leakage-safe OOS report.

    Stress is applied to target trajectories, historical calibration
    trajectories, and their labels before the strategy selects an exit
    threshold.  Consequently the report's TEST and STRESS metrics are based
    on the same stressed full-exit assumptions rather than a post-hoc haircut.
    """

    validation = _validate_run_inputs(
        dataset=dataset,
        strategy=strategy,
        manifest=manifest,
        backtest_config=backtest_config,
        stress=stress,
    )
    if validation is not None:
        return validation

    stressed_cases: list[CopyTradeLaunchCase] = []
    stressed_point_count = 0
    for case in dataset.cases:
        history_error = _validate_point_in_time_history(
            case,
            dataset.as_of_slot,
        )
        if history_error is not None:
            return history_error
        stressed = stress_copy_trade_case(
            case=case,
            entry_cost_quote_base_units=strategy.fixed_entry_quote_base_units,
            config=stress,
        )
        if isinstance(stressed, AbstainResult):
            return stressed
        stressed_cases.append(stressed)
        stressed_point_count += len(stressed.trajectory)

    evaluated = evaluate_copy_trade_launches(
        cases=tuple(stressed_cases),
        config=strategy,
        manifest=manifest,
    )
    if isinstance(evaluated, AbstainResult):
        return evaluated
    report = build_backtest_report(
        launches=evaluated,
        config=backtest_config,
    )
    if isinstance(report, AbstainResult):
        return report
    return FinalizedBacktestResult(
        dataset=dataset,
        evaluated_launches=evaluated,
        report=report,
        full_exit_stress_applied=True,
        stressed_point_count=stressed_point_count,
    )


def stress_copy_trade_case(
    *,
    case: CopyTradeLaunchCase,
    entry_cost_quote_base_units: QuoteBaseUnits,
    config: FullExitStressConfig,
) -> CopyTradeLaunchCase | AbstainResult:
    """Return a case whose executable full-exit points are integer-stressed."""

    validation = _validate_stress_config(config)
    if validation is not None:
        return validation
    if not isinstance(case, CopyTradeLaunchCase):
        return _abstain(
            AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
            "copy-trade case is malformed",
            config.as_of_slot,
        )
    if case.as_of_slot != config.as_of_slot:
        return _abstain(
            AbstainReason.STALE_STATE,
            "copy-trade case and full-exit stress use different cutoffs",
            config.as_of_slot,
        )
    trajectory = _stress_points(case.trajectory, config)
    if isinstance(trajectory, AbstainResult):
        return trajectory
    history: list[CopyTradeHistorySample] = []
    for sample in case.history:
        sample_trajectory = _stress_points(sample.trajectory, config)
        if isinstance(sample_trajectory, AbstainResult):
            return sample_trajectory
        realized = _historical_realized_pnl(
            points=sample_trajectory,
            adverse_event_elapsed_ms=sample.adverse_event_elapsed_ms,
            entry_cost_quote_base_units=sample.entry_cost_quote_base_units,
        )
        history.append(
            replace(
                sample,
                trajectory=sample_trajectory,
                realized_net_pnl_quote_base_units=realized,
            )
        )
    outcome = _stress_outcome(
        outcome=case.outcome,
        points=trajectory,
        entry_cost_quote_base_units=entry_cost_quote_base_units,
    )
    if isinstance(outcome, AbstainResult):
        return outcome
    return replace(case, history=tuple(history), trajectory=trajectory, outcome=outcome)


def _validate_dataset_inputs(
    *,
    observations: object,
    cases: object,
    trades: object,
    as_of_slot: object,
) -> AbstainResult | None:
    if type(as_of_slot) is not int or as_of_slot < 0:
        return _abstain(
            AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
            "dataset as_of_slot must be a non-negative integer",
            -1,
        )
    if type(observations) is not tuple or type(cases) is not tuple:
        return _abstain(
            AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
            "dataset observations and cases must be tuples",
            as_of_slot,
        )
    if type(trades) is not tuple:
        return _abstain(
            AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
            "dataset trades must be a tuple",
            as_of_slot,
        )
    if any(type(item) is not RawChainObservation for item in observations):
        return _abstain(
            AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
            "dataset contains malformed raw observation",
            as_of_slot,
        )
    if any(type(item) is not CopyTradeLaunchCase for item in cases):
        return _abstain(
            AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
            "dataset contains malformed copy-trade case",
            as_of_slot,
        )
    if any(type(item) is not FinalizedTrade for item in trades):
        return _abstain(
            AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
            "dataset contains malformed finalized trade",
            as_of_slot,
        )
    identities = tuple(observation_identity(item) for item in observations)
    if len(set(identities)) != len(identities):
        return _abstain(
            AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
            "dataset contains duplicate raw evidence",
            as_of_slot,
        )
    for observation in observations:
        if (
            observation.commitment != "finalized"
            or observation.canonical_status != "canonical"
            or observation.source_update_kind != "transaction"
            or observation.slot > as_of_slot
        ):
            return _abstain(
                AbstainReason.STALE_STATE,
                "dataset requires finalized canonical transaction evidence",
                as_of_slot,
            )
    return None


def _ordered_observations(
    observations: tuple[RawChainObservation, ...],
) -> tuple[RawChainObservation, ...]:
    return tuple(
        sorted(
            observations,
            key=lambda item: (
                item.slot,
                item.transaction_index if item.transaction_index is not None else -1,
                item.event_ordinal if item.event_ordinal is not None else -1,
                item.receive_sequence,
            ),
        )
    )


def _validate_launches(
    launches: tuple[LaunchCreatedV2, ...],
    as_of_slot: Slot,
) -> AbstainResult | None:
    launch_ids = tuple(launch.launch_id for launch in launches)
    if len(set(launch_ids)) != len(launch_ids):
        return _abstain(
            AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
            "decoded launch IDs are not unique",
            as_of_slot,
        )
    if any(launch.as_of_slot > as_of_slot for launch in launches):
        return _abstain(
            AbstainReason.STALE_STATE,
            "decoded launch is newer than the dataset cutoff",
            as_of_slot,
        )
    return None


def _validate_cases_against_launches(
    cases: tuple[CopyTradeLaunchCase, ...],
    launches: tuple[LaunchCreatedV2, ...],
    as_of_slot: Slot,
) -> AbstainResult | None:
    launch_by_id = {launch.launch_id: launch for launch in launches}
    for case in cases:
        launch = launch_by_id.get(case.launch_id)
        if launch is None:
            return _abstain(
                AbstainReason.MISSING_FEATURE,
                "copy-trade case has no decoded finalized launch",
                as_of_slot,
            )
        if case.as_of_slot != as_of_slot or case.decision_slot != launch.as_of_slot:
            return _abstain(
                AbstainReason.STALE_STATE,
                "copy-trade case is not aligned with decoded launch evidence",
                as_of_slot,
            )
        if case.token_mint != launch.mint_pubkey:
            return _abstain(
                AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
                "copy-trade case mint does not match decoded launch",
                as_of_slot,
            )
        if launch.signature is None or launch.transaction_index is None:
            return _abstain(
                AbstainReason.MISSING_FEATURE,
                "decoded launch lacks canonical transaction identity",
                as_of_slot,
            )
        history_error = _validate_point_in_time_history(case, as_of_slot)
        if history_error is not None:
            return history_error
    return None


def _validate_point_in_time_history(
    case: CopyTradeLaunchCase,
    as_of_slot: Slot,
) -> AbstainResult | None:
    for sample in case.history:
        if (
            sample.launch_slot >= case.decision_slot
            or sample.as_of_slot > case.decision_slot
        ):
            return _abstain(
                AbstainReason.STALE_STATE,
                "historical feature uses evidence after the target decision",
                as_of_slot,
            )
        if any(point.as_of_slot > case.decision_slot for point in sample.trajectory):
            return _abstain(
                AbstainReason.STALE_STATE,
                "historical trajectory uses a future feature boundary",
                as_of_slot,
            )
    return None


def _validate_trades_against_observations(
    *,
    trades: tuple[FinalizedTrade, ...],
    launches: tuple[LaunchCreatedV2, ...],
    observations: tuple[RawChainObservation, ...],
    as_of_slot: Slot,
) -> AbstainResult | None:
    launch_ids = {launch.launch_id for launch in launches}
    observation_keys = {
        _observation_key(observation)
        for observation in observations
        if observation.signature is not None
        and observation.transaction_index is not None
    }
    seen_trade_keys: set[tuple[int, int, bytes, str]] = set()
    for trade in trades:
        error = _validate_trade(trade, as_of_slot, launch_ids)
        if error is not None:
            return error
        key = (trade.slot, trade.transaction_index, trade.signature, trade.launch_id)
        if key in seen_trade_keys:
            return _abstain(
                AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
                "finalized trade rows are duplicated",
                as_of_slot,
            )
        seen_trade_keys.add(key)
        if (trade.slot, trade.transaction_index, trade.signature) not in {
            (item[0], item[1], item[2]) for item in observation_keys
        }:
            return _abstain(
                AbstainReason.MISSING_FEATURE,
                "finalized trade has no matching raw observation",
                as_of_slot,
            )
    return None


def _validate_trade(
    trade: FinalizedTrade,
    as_of_slot: Slot,
    launch_ids: set[str],
) -> AbstainResult | None:
    if (
        trade.as_of_slot < trade.slot
        or trade.as_of_slot > as_of_slot
        or trade.slot > as_of_slot
    ):
        return _abstain(
            AbstainReason.STALE_STATE,
            "finalized trade is outside the dataset cutoff",
            as_of_slot,
        )
    if trade.launch_id not in launch_ids or not trade.token_mint or not trade.wallet:
        return _abstain(
            AbstainReason.MISSING_FEATURE,
            "finalized trade identity is incomplete",
            as_of_slot,
        )
    if (
        type(trade.transaction_index) is not int
        or trade.transaction_index < 0
        or not isinstance(trade.signature, bytes)
        or not trade.signature
        or trade.side not in (TradeSide.BUY, TradeSide.SELL)
    ):
        return _abstain(
            AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
            "finalized trade transaction identity is malformed",
            as_of_slot,
        )
    if (
        any(
            type(amount) is not int or amount < 0
            for amount in (
                trade.base_amount_base_units,
                trade.quote_amount_base_units,
                trade.execution_cost_quote_base_units,
            )
        )
        or not trade.base_amount_base_units
    ):
        return _abstain(
            AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
            "finalized trade amounts must be integer and positive in base units",
            as_of_slot,
        )
    if not _valid_ids(trade.evidence_ids):
        return _abstain(
            AbstainReason.MISSING_FEATURE,
            "finalized trade evidence IDs are required",
            as_of_slot,
        )
    return None


def _observation_key(
    observation: RawChainObservation,
) -> tuple[int, int, bytes]:
    if observation.signature is None or observation.transaction_index is None:
        raise ValueError
    return observation.slot, observation.transaction_index, observation.signature


def _dataset_evidence_ids(
    *,
    observations: tuple[RawChainObservation, ...],
    launches: tuple[LaunchCreatedV2, ...],
    trades: tuple[FinalizedTrade, ...],
    cases: tuple[CopyTradeLaunchCase, ...],
) -> tuple[str, ...]:
    identifiers: list[str] = [
        _observation_evidence_id(observation) for observation in observations
    ]
    identifiers.extend(launch.launch_id for launch in launches)
    identifiers.extend(trade.evidence_ids for trade in trades)
    identifiers.extend(case.evidence_ids for case in cases)
    return tuple(dict.fromkeys(identifiers))


def _observation_evidence_id(observation: RawChainObservation) -> str:
    """Build a stable evidence label without using the ingestion UUID."""

    signature = (
        observation.signature.hex() if observation.signature is not None else "none"
    )
    transaction_index = (
        str(observation.transaction_index)
        if observation.transaction_index is not None
        else "none"
    )
    return (
        f"observation:{observation.source_id}:{observation.slot}:"
        f"{transaction_index}:{signature}"
    )


def _stress_points(
    points: tuple[OutcomeObservationPoint, ...],
    config: FullExitStressConfig,
) -> tuple[OutcomeObservationPoint, ...] | AbstainResult:
    if type(points) is not tuple or not points:
        return _abstain(
            AbstainReason.MISSING_FEATURE,
            "full-exit stress requires market trajectory points",
            config.as_of_slot,
        )
    stressed: list[OutcomeObservationPoint] = []
    for point in points:
        if (
            type(point) is not OutcomeObservationPoint
            or point.as_of_slot > config.as_of_slot
            or type(point.full_exit_output_quote_base_units) is not int
            or type(point.full_exit_execution_cost_quote_base_units) is not int
        ):
            return _abstain(
                AbstainReason.MISSING_FEATURE,
                "full-exit stress lacks executable integer output data",
                config.as_of_slot,
            )
        available_ppm = PROBABILITY_PPM_DENOMINATOR - config.output_haircut_ppm
        output = int(point.full_exit_output_quote_base_units) * available_ppm
        output //= PROBABILITY_PPM_DENOMINATOR
        cost = int(point.full_exit_execution_cost_quote_base_units) + int(
            config.additional_execution_cost_quote_base_units
        )
        stressed.append(
            replace(
                point,
                full_exit_output_quote_base_units=QuoteBaseUnits(output),
                full_exit_execution_cost_quote_base_units=QuoteBaseUnits(cost),
            )
        )
    return tuple(stressed)


def _historical_realized_pnl(
    *,
    points: tuple[OutcomeObservationPoint, ...],
    adverse_event_elapsed_ms: int | None,
    entry_cost_quote_base_units: QuoteBaseUnits,
) -> int:
    eligible = tuple(
        point
        for point in points
        if adverse_event_elapsed_ms is None
        or point.elapsed_ms < adverse_event_elapsed_ms
    )
    if not eligible:
        return 0
    return max(
        int(point.full_exit_output_quote_base_units)
        - int(point.full_exit_execution_cost_quote_base_units)
        - int(entry_cost_quote_base_units)
        for point in eligible
    )


def _stress_outcome(
    *,
    outcome: LaunchOutcomeLabels,
    points: tuple[OutcomeObservationPoint, ...],
    entry_cost_quote_base_units: QuoteBaseUnits,
) -> LaunchOutcomeLabels | AbstainResult:
    if outcome.as_of_slot != points[0].as_of_slot:
        return _abstain(
            AbstainReason.STALE_STATE,
            "outcome and stressed trajectory use different cutoffs",
            points[0].as_of_slot,
        )
    horizon_labels: list[HorizonOutcomeLabel] = []
    for label in outcome.horizon_labels:
        horizon_points = tuple(
            point for point in points if point.elapsed_ms <= label.horizon_ms
        )
        if label.censored:
            horizon_labels.append(label)
            continue
        if not horizon_points:
            return _abstain(
                AbstainReason.MISSING_FEATURE,
                "uncensored outcome has no stressed full-exit point",
                outcome.as_of_slot,
            )
        point = horizon_points[-1]
        pnl = (
            int(point.full_exit_output_quote_base_units)
            - int(point.full_exit_execution_cost_quote_base_units)
            - int(entry_cost_quote_base_units)
        )
        horizon_labels.append(replace(label, full_exit_net_pnl_quote_base_units=pnl))
    maximum = _historical_realized_pnl(
        points=points,
        adverse_event_elapsed_ms=outcome.first_material_adverse_event_elapsed_ms,
        entry_cost_quote_base_units=entry_cost_quote_base_units,
    )
    return replace(
        outcome,
        max_executable_full_position_net_profit_before_adverse_event=maximum,
        horizon_labels=tuple(horizon_labels),
        reason_codes=tuple(
            dict.fromkeys((*outcome.reason_codes, "full_exit_stress_applied"))
        ),
    )


def _validate_run_inputs(
    *,
    dataset: FinalizedBacktestDataset,
    strategy: CopyTradeConfig,
    manifest: FrozenModelManifest,
    backtest_config: BacktestConfig,
    stress: FullExitStressConfig,
) -> AbstainResult | None:
    if not isinstance(dataset, FinalizedBacktestDataset):
        return _abstain(
            AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
            "backtest dataset is malformed",
            -1,
        )
    if (
        strategy.as_of_slot != dataset.as_of_slot
        or backtest_config.as_of_slot != dataset.as_of_slot
    ):
        return _abstain(
            AbstainReason.STALE_STATE,
            "dataset, strategy, and report cutoff slots must match",
            dataset.as_of_slot,
        )
    if (
        manifest.as_of_slot != dataset.as_of_slot
        or backtest_config.manifest != manifest
    ):
        return _abstain(
            AbstainReason.STALE_STATE,
            "dataset report manifest is not frozen at the dataset cutoff",
            dataset.as_of_slot,
        )
    if not dataset.cases:
        return _abstain(
            AbstainReason.MISSING_FEATURE,
            "backtest dataset contains no launch cases",
            dataset.as_of_slot,
        )
    return _validate_stress_config(stress)


def _validate_stress_config(config: FullExitStressConfig) -> AbstainResult | None:
    if (
        type(config.as_of_slot) is not int
        or config.as_of_slot < 0
        or type(config.output_haircut_ppm) is not int
        or not 0 <= config.output_haircut_ppm <= PROBABILITY_PPM_DENOMINATOR
        or type(config.additional_execution_cost_quote_base_units) is not int
        or config.additional_execution_cost_quote_base_units < 0
    ):
        return _abstain(
            AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
            "full-exit stress configuration is malformed",
            config.as_of_slot if type(config.as_of_slot) is int else -1,
        )
    return None


def _valid_ids(values: object) -> bool:
    return (
        type(values) is tuple
        and bool(values)
        and all(type(value) is str and bool(value) for value in values)
    )


def _abstain(reason: AbstainReason, message: str, as_of_slot: int) -> AbstainResult:
    return AbstainResult(reason=reason, message=message, as_of_slot=as_of_slot)


__all__ = [
    "FinalizedBacktestDataset",
    "FinalizedBacktestResult",
    "FinalizedTrade",
    "FullExitStressConfig",
    "build_finalized_dataset",
    "run_finalized_backtest",
    "stress_copy_trade_case",
]
