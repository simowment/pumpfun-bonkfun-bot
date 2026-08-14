"""Assemble bounded copy-trade cases from finalized typed artifacts."""

# This module is deliberately a join boundary.  It does not fetch, decode, or
# infer market data; incomplete joins abstain instead of producing partial cases.
# ruff: noqa: C901, PLR0911, PLR0912, PLR0913

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from rugbot.backtest.copytrade import CopyTradeHistorySample, CopyTradeLaunchCase
from rugbot.decision.operator_qualification import (
    CompletedLaunchOutcome,
    WalletEntityEvidence,
)
from rugbot.domain.amounts import QuoteBaseUnits, Slot
from rugbot.domain.decisions import AbstainReason, AbstainResult
from rugbot.domain.launches import LaunchCreatedV2
from rugbot.domain.trades import TradeSide
from rugbot.graph.point_in_time import PROBABILITY_PPM_DENOMINATOR
from rugbot.models.outcome_labels import (
    HorizonOutcomeLabel,
    LaunchOutcomeLabels,
    OutcomeObservationPoint,
)

if TYPE_CHECKING:
    from rugbot.backtest.dataset import FinalizedTrade


@dataclass(frozen=True, slots=True)
class CopyTradeTrajectoryArtifact:
    """Frozen trajectory and entry facts for one launch.

    The trajectory builder owns quote provenance and ordering.  This artifact
    carries only the additional launch-level facts that are not present in an
    ``OutcomeObservationPoint`` or ``LaunchOutcomeLabels``.
    """

    as_of_slot: Slot
    launch_id: str
    token_mint: str
    launch_time_ms: int
    entry_market_cap_quote_base_units: QuoteBaseUnits
    wallet_buy_elapsed_ms: int
    holding_time_ms: int
    trajectory: tuple[OutcomeObservationPoint, ...]
    evidence_ids: tuple[str, ...]


CaseAssemblyResult = tuple[CopyTradeLaunchCase, ...] | AbstainResult


def assemble_copy_trade_cases(
    *,
    launches: tuple[LaunchCreatedV2, ...],
    fills: tuple[FinalizedTrade, ...],
    entity_evidence: tuple[WalletEntityEvidence, ...],
    trajectories: tuple[CopyTradeTrajectoryArtifact, ...],
    outcomes: tuple[CompletedLaunchOutcome, ...],
    outcome_labels: tuple[LaunchOutcomeLabels, ...],
    as_of_slot: Slot,
    entity_id: str,
    regime_id: str,
    min_entity_probability_ppm: int = 500_000,
    max_entry_transaction_index: int = 1,
) -> CaseAssemblyResult:
    """Build one copy-trade case per supplied launch.

    Every supplied launch is treated as an intended target.  Earlier launches
    become history only when a completed outcome is available before the
    target decision boundary.  No artifact discovered after that boundary is
    used for the history sample.  Missing, ambiguous, or mismatched evidence
    returns ``AbstainResult``.
    """

    cutoff = _safe_slot(as_of_slot)
    request_error = _validate_request(
        launches=launches,
        fills=fills,
        entity_evidence=entity_evidence,
        trajectories=trajectories,
        outcomes=outcomes,
        outcome_labels=outcome_labels,
        as_of_slot=as_of_slot,
        entity_id=entity_id,
        regime_id=regime_id,
        min_entity_probability_ppm=min_entity_probability_ppm,
        max_entry_transaction_index=max_entry_transaction_index,
    )
    if request_error is not None:
        return request_error

    launch_by_id = {launch.launch_id: launch for launch in launches}
    if len(launch_by_id) != len(launches):
        return _abstain(
            AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
            "finalized launches contain duplicate launch IDs",
            cutoff,
        )
    launch_error = _validate_launches(launches, cutoff)
    if launch_error is not None:
        return launch_error

    artifact_error = _validate_artifacts(
        launches=launches,
        trajectories=trajectories,
        outcome_labels=outcome_labels,
        outcomes=outcomes,
        cutoff=cutoff,
        entity_id=entity_id,
    )
    if artifact_error is not None:
        return artifact_error
    fill_error = _validate_fills(fills, cutoff)
    if fill_error is not None:
        return fill_error
    entity_error = _validate_entity_evidence(entity_evidence, cutoff, entity_id)
    if entity_error is not None:
        return entity_error

    trajectory_by_id = {artifact.launch_id: artifact for artifact in trajectories}
    label_by_id = {label.launch_id: label for label in outcome_labels}
    outcome_by_id = _outcomes_by_launch(outcomes)
    if isinstance(outcome_by_id, AbstainResult):
        return outcome_by_id
    if len(trajectory_by_id) != len(trajectories) or len(label_by_id) != len(
        outcome_labels
    ):
        return _abstain(
            AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
            "case artifacts contain duplicate launch IDs",
            cutoff,
        )

    targets = tuple(
        sorted(
            (
                launch
                for launch in launches
                if label_by_id.get(launch.launch_id) is not None
                and label_by_id[launch.launch_id].as_of_slot == cutoff
            ),
            key=lambda item: (item.as_of_slot, item.launch_id),
        )
    )
    if not targets:
        return _abstain(
            AbstainReason.MISSING_FEATURE,
            "no target launch has an outcome label at the requested boundary",
            cutoff,
        )

    cases: list[CopyTradeLaunchCase] = []
    for target in targets:
        target_error = _validate_target_boundary(target, cutoff)
        if target_error is not None:
            return target_error
        target_entity = _resolve_entity(
            evidence=entity_evidence,
            launch_id=target.launch_id,
            entity_id=entity_id,
            boundary=target.as_of_slot,
            min_probability_ppm=min_entity_probability_ppm,
        )
        if isinstance(target_entity, AbstainResult):
            return target_entity
        target_fill = _first_buy(
            fills=fills,
            launch=target,
            wallet=target_entity[0],
            boundary=target.as_of_slot,
            max_transaction_index=max_entry_transaction_index,
        )
        if isinstance(target_fill, AbstainResult):
            return target_fill
        target_trajectory = trajectory_by_id.get(target.launch_id)
        target_labels = label_by_id.get(target.launch_id)
        if target_trajectory is None or target_labels is None:
            return _abstain(
                AbstainReason.MISSING_FEATURE,
                "target trajectory and outcome label are required",
                cutoff,
            )
        history = _build_history(
            launches=launches,
            target=target,
            entity_evidence=entity_evidence,
            fills=fills,
            trajectories=trajectory_by_id,
            labels=label_by_id,
            outcomes=outcome_by_id,
            entity_id=entity_id,
            min_probability_ppm=min_entity_probability_ppm,
            max_transaction_index=max_entry_transaction_index,
        )
        if isinstance(history, AbstainResult):
            return history
        target_artifact_error = _validate_target_artifacts(
            artifact=target_trajectory,
            labels=target_labels,
            launch=target,
            cutoff=cutoff,
        )
        if target_artifact_error is not None:
            return target_artifact_error
        evidence_ids = _unique_ids(
            (
                f"launch:{target.launch_id}",
                *target_entity[1],
                *target_fill.evidence_ids,
                *target_trajectory.evidence_ids,
                *target_labels.evidence_ids,
            )
        )
        if evidence_ids is None:
            return _abstain(
                AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
                "target evidence IDs are malformed or duplicated",
                cutoff,
            )
        cases.append(
            CopyTradeLaunchCase(
                as_of_slot=Slot(cutoff),
                launch_id=target.launch_id,
                decision_id=f"copy-trade:{target.launch_id}",
                token_mint=target.mint_pubkey,
                entity_id=entity_id,
                regime_id=regime_id,
                decision_slot=Slot(target.as_of_slot),
                decision_index=target_fill.transaction_index,
                wallet=target_entity[0],
                launch_time_ms=target_trajectory.launch_time_ms,
                wallet_buy_transaction_index=target_fill.transaction_index,
                wallet_buy_elapsed_ms=target_trajectory.wallet_buy_elapsed_ms,
                entry_market_cap_quote_base_units=(
                    target_trajectory.entry_market_cap_quote_base_units
                ),
                history=history,
                trajectory=target_trajectory.trajectory,
                outcome=target_labels,
                evidence_ids=evidence_ids,
            )
        )
    return tuple(cases)


def _validate_request(
    *,
    launches: object,
    fills: object,
    entity_evidence: object,
    trajectories: object,
    outcomes: object,
    outcome_labels: object,
    as_of_slot: object,
    entity_id: object,
    regime_id: object,
    min_entity_probability_ppm: object,
    max_entry_transaction_index: object,
) -> AbstainResult | None:
    cutoff = _safe_slot(as_of_slot)
    if type(as_of_slot) is not int or as_of_slot < 0:
        return _abstain(
            AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
            "case cutoff must be a non-negative integer",
            cutoff,
        )
    if not all(
        type(value) is tuple and bool(value)
        for value in (
            launches,
            fills,
            entity_evidence,
            trajectories,
            outcomes,
            outcome_labels,
        )
    ):
        return _abstain(
            AbstainReason.MISSING_FEATURE,
            "all finalized case artifact tuples are required",
            cutoff,
        )
    if not all(isinstance(value, str) and value for value in (entity_id, regime_id)):
        return _abstain(
            AbstainReason.MISSING_FEATURE,
            "entity and regime identities are required",
            cutoff,
        )
    if (
        type(min_entity_probability_ppm) is not int
        or not 0 <= min_entity_probability_ppm <= PROBABILITY_PPM_DENOMINATOR
        or type(max_entry_transaction_index) is not int
        or max_entry_transaction_index < 0
    ):
        return _abstain(
            AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
            "case thresholds are malformed",
            cutoff,
        )
    return None


def _validate_launches(
    launches: tuple[LaunchCreatedV2, ...], cutoff: int
) -> AbstainResult | None:
    for launch in launches:
        if not isinstance(launch, LaunchCreatedV2):
            return _abstain(
                AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
                "launches must use finalized LaunchCreatedV2 artifacts",
                cutoff,
            )
        if (
            type(launch.as_of_slot) is not int
            or launch.as_of_slot < 0
            or launch.as_of_slot > cutoff
            or not isinstance(launch.launch_id, str)
            or not launch.launch_id
            or not isinstance(launch.account_pubkeys, tuple)
            or type(launch.mint_account_index) is not int
            or not 0 <= launch.mint_account_index < len(launch.account_pubkeys)
            or launch.mint_pubkey != launch.account_pubkeys[launch.mint_account_index]
        ):
            return _abstain(
                AbstainReason.MISSING_FEATURE,
                "launch identity or finalized boundary is incomplete",
                cutoff,
            )
    return None


def _validate_artifacts(
    *,
    launches: tuple[LaunchCreatedV2, ...],
    trajectories: tuple[CopyTradeTrajectoryArtifact, ...],
    outcome_labels: tuple[LaunchOutcomeLabels, ...],
    outcomes: tuple[CompletedLaunchOutcome, ...],
    cutoff: int,
    entity_id: str,
) -> AbstainResult | None:
    launch_ids = {launch.launch_id for launch in launches}
    for artifact in trajectories:
        if not isinstance(artifact, CopyTradeTrajectoryArtifact):
            return _abstain(
                AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
                "trajectory artifacts are malformed",
                cutoff,
            )
        launch = next(
            (item for item in launches if item.launch_id == artifact.launch_id), None
        )
        if launch is None:
            return _abstain(
                AbstainReason.MISSING_FEATURE,
                "trajectory is not joined to a finalized launch",
                cutoff,
            )
        if (
            artifact.token_mint != launch.mint_pubkey
            or type(artifact.as_of_slot) is not int
            or artifact.as_of_slot < launch.as_of_slot
            or artifact.as_of_slot > cutoff
            or type(artifact.launch_time_ms) is not int
            or artifact.launch_time_ms < 0
            or type(artifact.entry_market_cap_quote_base_units) is not int
            or artifact.entry_market_cap_quote_base_units <= 0
            or type(artifact.wallet_buy_elapsed_ms) is not int
            or artifact.wallet_buy_elapsed_ms < 0
            or type(artifact.holding_time_ms) is not int
            or artifact.holding_time_ms < 0
            or _validate_ids(artifact.evidence_ids) is not None
        ):
            return _abstain(
                AbstainReason.MISSING_FEATURE,
                "trajectory launch facts are incomplete",
                cutoff,
            )
        trajectory_error = _validate_trajectory(
            artifact.trajectory, artifact.as_of_slot, cutoff
        )
        if trajectory_error is not None:
            return trajectory_error

    for label in outcome_labels:
        if (
            not isinstance(label, LaunchOutcomeLabels)
            or label.launch_id not in launch_ids
            or type(label.as_of_slot) is not int
            or not isinstance(label.horizon_labels, tuple)
            or label.as_of_slot > cutoff
            or _validate_ids(label.evidence_ids) is not None
        ):
            return _abstain(
                AbstainReason.MISSING_FEATURE,
                "outcome labels are malformed or incomplete",
                cutoff,
            )
        launch = next(item for item in launches if item.launch_id == label.launch_id)
        if label.token_mint != launch.mint_pubkey:
            return _abstain(
                AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
                "outcome label mint does not match its launch",
                cutoff,
            )
        if any(
            not isinstance(horizon, HorizonOutcomeLabel)
            or horizon.as_of_slot > cutoff
            or _validate_ids(horizon.evidence_ids) is not None
            for horizon in label.horizon_labels
        ):
            return _abstain(
                AbstainReason.MISSING_FEATURE,
                "outcome horizon evidence is malformed",
                cutoff,
            )

    for outcome in outcomes:
        if (
            not isinstance(outcome, CompletedLaunchOutcome)
            or outcome.entity_id != entity_id
            or outcome.launch_id not in launch_ids
            or type(outcome.as_of_slot) is not int
            or outcome.as_of_slot > cutoff
            or not outcome.completed
            or type(outcome.launch_slot) is not int
            or type(outcome.completed_slot) is not int
            or outcome.launch_slot < 0
            or outcome.completed_slot < outcome.launch_slot
            or _validate_ids(outcome.evidence_ids) is not None
        ):
            return _abstain(
                AbstainReason.MISSING_FEATURE,
                "completed outcome evidence is malformed or incomplete",
                cutoff,
            )
    return None


def _validate_fills(
    fills: tuple[FinalizedTrade, ...], cutoff: int
) -> AbstainResult | None:
    from rugbot.backtest.dataset import FinalizedTrade  # noqa: PLC0415

    for fill in fills:
        if not isinstance(fill, FinalizedTrade):
            return _abstain(
                AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
                "fills must use finalized trade artifacts",
                cutoff,
            )
        if (
            type(fill.as_of_slot) is not int
            or type(fill.slot) is not int
            or fill.slot < 0
            or fill.slot > fill.as_of_slot
            or fill.as_of_slot > cutoff
            or type(fill.transaction_index) is not int
            or fill.transaction_index < 0
            or not isinstance(fill.launch_id, str)
            or not fill.launch_id
            or not isinstance(fill.token_mint, str)
            or not fill.token_mint
            or not isinstance(fill.wallet, str)
            or not fill.wallet
            or not isinstance(fill.side, TradeSide)
            or type(fill.signature) is not bytes
            or not fill.signature
            or type(fill.base_amount_base_units) is not int
            or fill.base_amount_base_units <= 0
            or type(fill.quote_amount_base_units) is not int
            or fill.quote_amount_base_units <= 0
            or type(fill.execution_cost_quote_base_units) is not int
            or fill.execution_cost_quote_base_units < 0
            or _validate_ids(fill.evidence_ids) is not None
        ):
            return _abstain(
                AbstainReason.MISSING_FEATURE,
                "finalized fill evidence is malformed",
                cutoff,
            )
    return None


def _validate_entity_evidence(
    evidence: tuple[WalletEntityEvidence, ...],
    cutoff: int,
    entity_id: str,
) -> AbstainResult | None:
    for item in evidence:
        if (
            not isinstance(item, WalletEntityEvidence)
            or item.entity_id != entity_id
            or type(item.as_of_slot) is not int
            or type(item.observed_slot) is not int
            or item.as_of_slot < 0
            or item.observed_slot < 0
            or item.as_of_slot > cutoff
            or item.observed_slot > item.as_of_slot
            or not isinstance(item.launch_id, str)
            or not item.launch_id
            or not isinstance(item.wallet, str)
            or not item.wallet
            or type(item.entity_probability_ppm) is not int
            or not 0 <= item.entity_probability_ppm <= PROBABILITY_PPM_DENOMINATOR
            or _validate_ids(item.evidence_ids) is not None
        ):
            return _abstain(
                AbstainReason.MISSING_FEATURE,
                "wallet/entity evidence is malformed",
                cutoff,
            )
    return None


def _build_history(
    *,
    launches: tuple[LaunchCreatedV2, ...],
    target: LaunchCreatedV2,
    entity_evidence: tuple[WalletEntityEvidence, ...],
    fills: tuple[FinalizedTrade, ...],
    trajectories: dict[str, CopyTradeTrajectoryArtifact],
    labels: dict[str, LaunchOutcomeLabels],
    outcomes: dict[str, CompletedLaunchOutcome],
    entity_id: str,
    min_probability_ppm: int,
    max_transaction_index: int,
) -> tuple[CopyTradeHistorySample, ...] | AbstainResult:
    history: list[CopyTradeHistorySample] = []
    prior_launches = sorted(
        (launch for launch in launches if launch.as_of_slot < target.as_of_slot),
        key=lambda item: (item.as_of_slot, item.launch_id),
    )
    for launch in prior_launches:
        outcome = outcomes.get(launch.launch_id)
        artifact = trajectories.get(launch.launch_id)
        label = labels.get(launch.launch_id)
        if outcome is None or artifact is None or label is None:
            return _abstain(
                AbstainReason.MISSING_FEATURE,
                "prior launch lacks completed outcome artifacts",
                target.as_of_slot,
            )
        entity = _resolve_entity(
            evidence=entity_evidence,
            launch_id=launch.launch_id,
            entity_id=entity_id,
            boundary=target.as_of_slot,
            min_probability_ppm=min_probability_ppm,
        )
        if isinstance(entity, AbstainResult):
            return entity
        fill = _first_buy(
            fills=fills,
            launch=launch,
            wallet=entity[0],
            boundary=target.as_of_slot,
            max_transaction_index=max_transaction_index,
        )
        if isinstance(fill, AbstainResult):
            return fill
        sample_as_of = max(
            int(outcome.as_of_slot),
            int(entity[2]),
            int(artifact.as_of_slot),
            int(label.as_of_slot),
            int(fill.as_of_slot),
        )
        if sample_as_of > target.as_of_slot:
            return _abstain(
                AbstainReason.STALE_STATE,
                "historical case evidence is newer than its decision boundary",
                target.as_of_slot,
            )
        ids = _unique_ids(
            (
                f"launch:{launch.launch_id}",
                *entity[1],
                *fill.evidence_ids,
                *artifact.evidence_ids,
                *label.evidence_ids,
                *outcome.evidence_ids,
            )
        )
        if ids is None:
            return _abstain(
                AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
                "historical evidence IDs are malformed or duplicated",
                target.as_of_slot,
            )
        history.append(
            CopyTradeHistorySample(
                as_of_slot=Slot(sample_as_of),
                launch_id=launch.launch_id,
                token_mint=launch.mint_pubkey,
                wallet=entity[0],
                launch_slot=Slot(launch.as_of_slot),
                launch_time_ms=artifact.launch_time_ms,
                first_buy_transaction_index=fill.transaction_index,
                entry_market_cap_quote_base_units=(
                    artifact.entry_market_cap_quote_base_units
                ),
                entry_cost_quote_base_units=QuoteBaseUnits(
                    int(fill.quote_amount_base_units)
                    + int(fill.execution_cost_quote_base_units)
                ),
                realized_net_pnl_quote_base_units=int(
                    outcome.realized_net_pnl_quote_base_units
                ),
                holding_time_ms=artifact.holding_time_ms,
                wallet_buy_elapsed_ms=artifact.wallet_buy_elapsed_ms,
                trajectory=artifact.trajectory,
                adverse_event_elapsed_ms=(
                    label.first_material_adverse_event_elapsed_ms
                ),
                evidence_ids=ids,
            )
        )
    return tuple(history)


def _resolve_entity(
    *,
    evidence: tuple[WalletEntityEvidence, ...],
    launch_id: str,
    entity_id: str,
    boundary: int,
    min_probability_ppm: int,
) -> tuple[str, tuple[str, ...], int] | AbstainResult:
    matches = tuple(
        item
        for item in evidence
        if item.launch_id == launch_id
        and item.entity_id == entity_id
        and item.as_of_slot <= boundary
        and item.observed_slot <= boundary
        and item.entity_probability_ppm >= min_probability_ppm
    )
    if not matches:
        return _abstain(
            AbstainReason.MISSING_FEATURE,
            "point-in-time entity evidence is missing",
            boundary,
        )
    best_probability = max(item.entity_probability_ppm for item in matches)
    best = tuple(
        item for item in matches if item.entity_probability_ppm == best_probability
    )
    wallets = {item.wallet for item in best}
    if len(wallets) != 1 or any(
        _validate_ids(item.evidence_ids) is not None for item in best
    ):
        return _abstain(
            AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
            "point-in-time entity evidence is ambiguous",
            boundary,
        )
    ids = _unique_ids(
        tuple(identifier for item in best for identifier in item.evidence_ids)
    )
    if ids is None:
        return _abstain(
            AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
            "entity evidence IDs are malformed or duplicated",
            boundary,
        )
    return next(iter(wallets)), ids, max(item.as_of_slot for item in best)


def _first_buy(
    *,
    fills: tuple[FinalizedTrade, ...],
    launch: LaunchCreatedV2,
    wallet: str,
    boundary: int,
    max_transaction_index: int,
) -> FinalizedTrade | AbstainResult:
    candidates = tuple(
        fill
        for fill in fills
        if fill.launch_id == launch.launch_id
        and fill.token_mint == launch.mint_pubkey
        and fill.wallet == wallet
        and fill.side is TradeSide.BUY
        and fill.slot <= boundary
        and fill.as_of_slot <= boundary
        and fill.transaction_index <= max_transaction_index
    )
    if not candidates:
        return _abstain(
            AbstainReason.MISSING_FEATURE,
            "finalized wallet buy fill is missing at the decision boundary",
            boundary,
        )
    ordered = tuple(
        sorted(candidates, key=lambda fill: (fill.slot, fill.transaction_index))
    )
    first = ordered[0]
    if (
        type(first.transaction_index) is not int
        or first.transaction_index < 0
        or not first.evidence_ids
        or any(
            type(identifier) is not str or not identifier
            for identifier in first.evidence_ids
        )
        or int(first.quote_amount_base_units) <= 0
        or int(first.base_amount_base_units) <= 0
    ):
        return _abstain(
            AbstainReason.MISSING_FEATURE,
            "finalized wallet buy fill is incomplete",
            boundary,
        )
    return first


def _validate_target_artifacts(
    *,
    artifact: CopyTradeTrajectoryArtifact,
    labels: LaunchOutcomeLabels,
    launch: LaunchCreatedV2,
    cutoff: int,
) -> AbstainResult | None:
    if (
        artifact.as_of_slot > cutoff
        or labels.as_of_slot != cutoff
        or labels.launch_id != launch.launch_id
        or labels.token_mint != launch.mint_pubkey
    ):
        return _abstain(
            AbstainReason.STALE_STATE,
            "target labels do not share the requested frozen boundary",
            cutoff,
        )
    return None


def _validate_trajectory(
    points: tuple[OutcomeObservationPoint, ...],
    artifact_as_of: int,
    cutoff: int,
) -> AbstainResult | None:
    if type(points) is not tuple or not points:
        return _abstain(
            AbstainReason.MISSING_FEATURE,
            "trajectory points are required",
            cutoff,
        )
    previous: tuple[int, int, int] | None = None
    seen: set[str] = set()
    for point in points:
        if not isinstance(point, OutcomeObservationPoint):
            return _abstain(
                AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
                "trajectory contains an invalid point",
                cutoff,
            )
        values = (
            point.as_of_slot,
            point.slot,
            point.event_index,
            point.elapsed_ms,
            point.price_quote_base_units_per_token_base_unit_ppm,
            point.full_exit_output_quote_base_units,
            point.full_exit_execution_cost_quote_base_units,
        )
        if any(type(value) is not int or value < 0 for value in values):
            return _abstain(
                AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
                "trajectory point contains malformed integer values",
                cutoff,
            )
        if point.slot > point.as_of_slot or point.as_of_slot > artifact_as_of:
            return _abstain(
                AbstainReason.STALE_STATE,
                "trajectory point is newer than its artifact boundary",
                cutoff,
            )
        position = (point.elapsed_ms, point.event_index, point.slot)
        if previous is not None and position <= previous:
            return _abstain(
                AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
                "trajectory points are not strictly ordered",
                cutoff,
            )
        if _validate_ids(point.evidence_ids) is not None or seen.intersection(
            point.evidence_ids
        ):
            return _abstain(
                AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
                "trajectory evidence IDs are malformed or duplicated",
                cutoff,
            )
        seen.update(point.evidence_ids)
        previous = position
    return None


def _outcomes_by_launch(
    outcomes: tuple[CompletedLaunchOutcome, ...],
) -> dict[str, CompletedLaunchOutcome] | AbstainResult:
    result: dict[str, CompletedLaunchOutcome] = {}
    for outcome in outcomes:
        if outcome.launch_id in result:
            return _abstain(
                AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
                "completed outcomes contain duplicate launch IDs",
                outcome.as_of_slot,
            )
        result[outcome.launch_id] = outcome
    return result


def _validate_target_boundary(
    launch: LaunchCreatedV2, cutoff: int
) -> AbstainResult | None:
    if launch.as_of_slot > cutoff:
        return _abstain(
            AbstainReason.STALE_STATE,
            "target launch is newer than the requested cutoff",
            cutoff,
        )
    return None


def _validate_ids(values: object) -> AbstainResult | None:
    if (
        type(values) is not tuple
        or not values
        or any(type(value) is not str or not value for value in values)
        or len(set(values)) != len(values)
    ):
        return _abstain(
            AbstainReason.MISSING_FEATURE,
            "evidence IDs must be a non-empty unique tuple",
            -1,
        )
    return None


def _unique_ids(values: tuple[str, ...]) -> tuple[str, ...] | None:
    if any(type(value) is not str or not value for value in values):
        return None
    if len(set(values)) != len(values):
        return None
    return values


def _safe_slot(value: object) -> int:
    return value if type(value) is int else -1


def _abstain(reason: AbstainReason, message: str, as_of_slot: int) -> AbstainResult:
    return AbstainResult(reason=reason, message=message, as_of_slot=as_of_slot)


__all__ = [
    "CaseAssemblyResult",
    "CopyTradeTrajectoryArtifact",
    "assemble_copy_trade_cases",
]
