"""Pure launch-level producer for finalized Pump trade outcomes.

The producer composes typed finalized trade proofs with explicit launch,
protocol, and mint metadata.  It performs no RPC or storage access, uses only
integer arithmetic through the canonical quote and label builders, and
abstains when any point-in-time proof is missing or contradictory.
"""

# The validation boundary is intentionally explicit and fail-closed.
# ruff: noqa: PLR0911, PLR0912, C901, TC001

from __future__ import annotations

from dataclasses import dataclass, replace

from rugbot.backtest.finalized_trade_builder import PumpTradeEventProof
from rugbot.backtest.outcome_builder import (
    FinalizedOutcomePointInput,
    build_outcome_observation_point,
)
from rugbot.backtest.trade_event_trajectory import (
    PumpTradeEventProtocolProof,
    TradeEventTrajectoryMetadataProof,
    TradeEventTrajectorySource,
    build_trade_event_trajectory_point,
)
from rugbot.domain.amounts import Slot, TokenBaseUnits
from rugbot.domain.decisions import AbstainReason, AbstainResult
from rugbot.domain.observations import RawChainObservation
from rugbot.domain.quotes import QuotePath
from rugbot.models.adverse_event import (
    AdverseEvent,
    AdverseEventDetection,
    AdverseEventDetectionConfig,
    detect_adverse_event,
)
from rugbot.models.outcome_labels import (
    LaunchOutcomeLabels,
    OutcomeLabelConfig,
    OutcomeObservationPoint,
    build_launch_outcome_labels,
)
from rugbot.protocol.pump.create_state_adapter import PumpCreateMintMetadataProof
from rugbot.protocol.pump.version_registry import PumpProtocolVersionSnapshot


@dataclass(frozen=True, slots=True)
class LaunchTrajectoryMetadata:
    """Point-in-time launch proof and position policy for one token."""

    launch_id: str
    token_mint: str
    launch_slot: Slot
    launch_timestamp: int
    full_exit_base_amount_base_units: TokenBaseUnits
    evidence_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class FinalizedPumpTradePoint:
    """One finalized Pump event plus all metadata needed to quote it."""

    observation: RawChainObservation
    event: PumpTradeEventProof
    event_index: int
    protocol_snapshot: PumpProtocolVersionSnapshot | PumpTradeEventProtocolProof | None
    mint_metadata: PumpCreateMintMetadataProof | None
    curve_completed: bool
    migration_observed: bool
    evidence_ids: tuple[str, ...]
    quote_path: QuotePath = QuotePath.PUMP_BONDING_CURVE


@dataclass(frozen=True, slots=True)
class LaunchOutcomeProduction:
    """Produced immutable trajectory, adverse event, and labels."""

    launch: LaunchTrajectoryMetadata
    trajectory: tuple[OutcomeObservationPoint, ...]
    adverse_detection: AdverseEventDetection
    adverse_event: AdverseEvent | None
    labels: LaunchOutcomeLabels
    evidence_ids: tuple[str, ...]


LaunchOutcomeProductionResult = LaunchOutcomeProduction | AbstainResult


def build_launch_outcome(
    *,
    launch: LaunchTrajectoryMetadata,
    points: tuple[FinalizedPumpTradePoint, ...],
    outcome_config: OutcomeLabelConfig,
    adverse_config: AdverseEventDetectionConfig,
) -> LaunchOutcomeProductionResult:
    """Build a leakage-safe launch trajectory and its adverse-event labels.

    Each input point supplies its own protocol and mint snapshot.  Those
    snapshots must be at the observation slot; no current-state metadata is
    inferred for historical points.  Same-slot events are retained when their
    explicit event indexes differ.
    """

    cutoff = _safe_slot(getattr(outcome_config, "as_of_slot", -1))
    validation = _validate_request(
        launch=launch,
        points=points,
        outcome_config=outcome_config,
        adverse_config=adverse_config,
        cutoff=cutoff,
    )
    if validation is not None:
        return validation

    finalized_inputs: list[FinalizedOutcomePointInput] = []
    seen_positions: set[tuple[int, int]] = set()
    seen_transactions: set[tuple[bytes, int, int]] = set()
    seen_evidence_ids = set(launch.evidence_ids)

    for point in points:
        if type(point) is not FinalizedPumpTradePoint:
            return _abstain(
                AbstainReason.MISSING_FEATURE,
                "finalized Pump trade point is malformed",
                cutoff,
            )
        if type(point.observation) is not RawChainObservation:
            return _abstain(
                AbstainReason.MISSING_FEATURE,
                "finalized raw observation is required",
                cutoff,
            )
        if type(point.event) is not PumpTradeEventProof:
            return _abstain(
                AbstainReason.MISSING_FEATURE,
                "decoded Pump TradeEvent proof is required",
                cutoff,
            )
        if point.event.mint != launch.token_mint:
            return _abstain(
                AbstainReason.STALE_STATE,
                "TradeEvent mint does not match launch metadata",
                cutoff,
            )
        if (
            type(point.observation.slot) is not int
            or point.observation.slot < launch.launch_slot
            or point.observation.slot > cutoff
        ):
            return _abstain(
                AbstainReason.STALE_STATE,
                "finalized Pump trade point is outside the launch cutoff",
                cutoff,
            )
        if (
            type(point.event_index) is not int
            or point.event_index < 0
            or type(point.observation.slot) is not int
            or type(point.observation.signature) is not bytes
            or not point.observation.signature
            or type(point.observation.transaction_index) is not int
            or point.observation.transaction_index < 0
            or type(point.curve_completed) is not bool
            or type(point.migration_observed) is not bool
            or _invalid_evidence_ids(point.evidence_ids)
        ):
            return _abstain(
                AbstainReason.MISSING_FEATURE,
                "finalized Pump trade point provenance is incomplete",
                cutoff,
            )
        position = (point.observation.slot, point.event_index)
        if position in seen_positions:
            return _abstain(
                AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
                "launch trajectory positions must be unique",
                cutoff,
            )
        seen_positions.add(position)

        transaction_key = (
            point.observation.signature,
            point.observation.transaction_index,
            point.event_index,
        )
        if transaction_key in seen_transactions:
            return _abstain(
                AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
                "finalized Pump trade points are duplicated",
                cutoff,
            )
        seen_transactions.add(transaction_key)

        if seen_evidence_ids.intersection(point.evidence_ids):
            return _abstain(
                AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
                "launch trajectory evidence IDs must be unique",
                cutoff,
            )
        seen_evidence_ids.update(point.evidence_ids)

        source = TradeEventTrajectorySource(
            observation=point.observation,
            event=point.event,
            metadata=TradeEventTrajectoryMetadataProof(
                as_of_slot=Slot(point.observation.slot),
                event_index=point.event_index,
                trajectory_start_timestamp=launch.launch_timestamp,
                curve_completed=point.curve_completed,
                migration_observed=point.migration_observed,
                full_exit_base_amount_base_units=(
                    launch.full_exit_base_amount_base_units
                ),
                protocol_snapshot=point.protocol_snapshot,
                mint_metadata=point.mint_metadata,
                evidence_ids=point.evidence_ids,
                quote_path=point.quote_path,
            ),
        )
        finalized_point = build_trade_event_trajectory_point(
            source=source,
            as_of_slot=Slot(cutoff),
        )
        if isinstance(finalized_point, AbstainResult):
            return finalized_point
        finalized_inputs.append(finalized_point)

    finalized_inputs.sort(
        key=lambda item: (
            int(item.market_state.slot),
            item.market_state.event_index,
        )
    )
    previous_elapsed_ms: int | None = None
    for finalized_point in finalized_inputs:
        elapsed_ms = finalized_point.market_state.elapsed_ms
        if previous_elapsed_ms is not None and elapsed_ms < previous_elapsed_ms:
            return _abstain(
                AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
                "finalized Pump trajectory time moves backwards",
                cutoff,
            )
        previous_elapsed_ms = elapsed_ms

    outcome_points: list[OutcomeObservationPoint] = []
    for finalized_point in finalized_inputs:
        outcome_point = build_outcome_observation_point(
            point=finalized_point,
            as_of_slot=Slot(cutoff),
        )
        if isinstance(outcome_point, AbstainResult):
            return outcome_point
        outcome_points.append(outcome_point)

    trajectory = tuple(outcome_points)
    market_states = tuple(
        replace(point.market_state, as_of_slot=Slot(cutoff))
        for point in finalized_inputs
    )
    adverse_detection = detect_adverse_event(
        points=market_states,
        config=adverse_config,
    )
    if isinstance(adverse_detection, AbstainResult):
        return adverse_detection

    labels = build_launch_outcome_labels(
        points=trajectory,
        config=outcome_config,
        adverse_event=adverse_detection.event,
    )
    if isinstance(labels, AbstainResult):
        return labels

    evidence_ids = tuple(dict.fromkeys((*launch.evidence_ids, *labels.evidence_ids)))
    return LaunchOutcomeProduction(
        launch=launch,
        trajectory=trajectory,
        adverse_detection=adverse_detection,
        adverse_event=adverse_detection.event,
        labels=labels,
        evidence_ids=evidence_ids,
    )


def _validate_request(
    *,
    launch: LaunchTrajectoryMetadata,
    points: tuple[FinalizedPumpTradePoint, ...],
    outcome_config: OutcomeLabelConfig,
    adverse_config: AdverseEventDetectionConfig,
    cutoff: int,
) -> AbstainResult | None:
    if type(cutoff) is not int or cutoff < 0:
        return _abstain(
            AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
            "outcome cutoff slot must be a non-negative integer",
            cutoff,
        )
    if type(launch) is not LaunchTrajectoryMetadata:
        return _abstain(
            AbstainReason.MISSING_FEATURE,
            "typed launch metadata is required",
            cutoff,
        )
    if type(points) is not tuple or not points:
        return _abstain(
            AbstainReason.MISSING_FEATURE,
            "finalized Pump trade points are required",
            cutoff,
        )
    if type(outcome_config) is not OutcomeLabelConfig:
        return _abstain(
            AbstainReason.MISSING_FEATURE,
            "outcome label config is required",
            cutoff,
        )
    if type(adverse_config) is not AdverseEventDetectionConfig:
        return _abstain(
            AbstainReason.MISSING_FEATURE,
            "adverse-event config is required",
            cutoff,
        )
    if (
        type(launch.launch_id) is not str
        or not launch.launch_id
        or type(launch.token_mint) is not str
        or not launch.token_mint
        or type(launch.launch_slot) is not int
        or launch.launch_slot < 0
        or launch.launch_slot > cutoff
        or type(launch.launch_timestamp) is not int
        or launch.launch_timestamp < 0
        or type(launch.full_exit_base_amount_base_units) is not int
        or launch.full_exit_base_amount_base_units <= 0
        or _invalid_evidence_ids(launch.evidence_ids)
    ):
        return _abstain(
            AbstainReason.MISSING_FEATURE,
            "launch metadata proof is incomplete",
            cutoff,
        )
    if outcome_config.as_of_slot != cutoff or adverse_config.as_of_slot != cutoff:
        return _abstain(
            AbstainReason.STALE_STATE,
            "label and adverse configs must use the requested cutoff",
            cutoff,
        )
    if (
        outcome_config.launch_id != launch.launch_id
        or outcome_config.token_mint != launch.token_mint
        or adverse_config.token_mint != launch.token_mint
    ):
        return _abstain(
            AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
            "outcome configuration does not match launch metadata",
            cutoff,
        )
    return None


def _invalid_evidence_ids(evidence_ids: object) -> bool:
    return (
        type(evidence_ids) is not tuple
        or not evidence_ids
        or any(type(value) is not str or not value for value in evidence_ids)
        or len(set(evidence_ids)) != len(evidence_ids)
    )


def _safe_slot(value: object) -> int:
    return value if type(value) is int else -1


def _abstain(reason: AbstainReason, message: str, as_of_slot: int) -> AbstainResult:
    return AbstainResult(reason=reason, message=message, as_of_slot=as_of_slot)


__all__ = [
    "FinalizedPumpTradePoint",
    "LaunchOutcomeProduction",
    "LaunchOutcomeProductionResult",
    "LaunchTrajectoryMetadata",
    "build_launch_outcome",
]
