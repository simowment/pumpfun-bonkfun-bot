"""Adapt finalized launch productions into canonical copy-trade cases."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from rugbot.backtest.cases.case_builder import (
    CaseAssemblyResult,
    CopyTradeTrajectoryArtifact,
    assemble_copy_trade_cases,
)
from rugbot.backtest.trajectory.trajectory_producer import (
    FinalizedPumpTradePoint,
    LaunchOutcomeProduction,
    LaunchTrajectoryMetadata,
    build_launch_outcome,
)
from rugbot.decision.operator_qualification import (
    CompletedLaunchOutcome,
    WalletEntityEvidence,
)
from rugbot.domain.amounts import QuoteBaseUnits, Slot
from rugbot.domain.decisions import AbstainReason, AbstainResult
from rugbot.domain.observations import RawChainObservation
from rugbot.domain.outcome_labels import (
    HorizonOutcomeLabel,
    LaunchOutcomeLabels,
)
from rugbot.storage.jsonl_observation_store import observation_identity

if TYPE_CHECKING:
    from rugbot.backtest.dataset import FinalizedTrade
    from rugbot.domain.adverse_event import AdverseEventDetectionConfig
    from rugbot.domain.launches import LaunchCreatedV2
    from rugbot.domain.outcome_labels import OutcomeLabelConfig


@dataclass(frozen=True, slots=True)
class ProductionEntryFacts:
    """Finalized entry facts not carried by ``LaunchOutcomeProduction``."""

    as_of_slot: Slot
    launch_id: str
    entry_market_cap_quote_base_units: QuoteBaseUnits
    wallet_buy_elapsed_ms: int
    evidence_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class FinalizedLaunchCaseProof:
    """All point-in-time proofs needed to produce one launch case.

    The bundle deliberately contains decoded finalized trade events rather than
    raw payload fragments.  RPC acquisition may supply the observations, but
    protocol, mint, state, outcome, adverse-event, and entry proofs must still
    be explicit before a case can be assembled.
    """

    launch: LaunchTrajectoryMetadata
    points: tuple[FinalizedPumpTradePoint, ...]
    outcome_config: OutcomeLabelConfig
    adverse_config: AdverseEventDetectionConfig
    entry_facts: ProductionEntryFacts


ConvertedProduction = tuple[
    CopyTradeTrajectoryArtifact,
    CompletedLaunchOutcome,
    LaunchOutcomeLabels,
]


def assemble_observation_copy_trade_cases(  # noqa: PLR0911, PLR0913
    *,
    launches: tuple[LaunchCreatedV2, ...],
    fills: tuple[FinalizedTrade, ...],
    entity_evidence: tuple[WalletEntityEvidence, ...],
    observations: tuple[RawChainObservation, ...],
    proofs: tuple[FinalizedLaunchCaseProof, ...],
    as_of_slot: Slot,
    entity_id: str,
    regime_id: str,
    min_entity_probability_ppm: int = 500_000,
    max_entry_transaction_index: int = 1,
) -> CaseAssemblyResult:
    """Produce cases directly from complete finalized Pump proof bundles.

    This is the typed handoff for RPC and replay callers.  It performs no I/O,
    and every trajectory/outcome is produced by the existing pure producer.
    Missing protocol, mint, executable-quote, entry, or temporal evidence is
    returned unchanged as an abstention.
    """

    cutoff = _safe_slot(as_of_slot)
    if type(as_of_slot) is not int or as_of_slot < 0:
        return _abstain(
            AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
            "observation case cutoff must be a non-negative integer",
            cutoff,
        )
    if type(proofs) is not tuple or not proofs:
        return _abstain(
            AbstainReason.MISSING_FEATURE,
            (
                "finalized launch case proofs are required: launch metadata, "
                "decoded Pump TradeEvents, point-in-time protocol/mint/state "
                "proofs, outcome/adverse configs, and entry facts"
            ),
            cutoff,
        )
    observation_error = _validate_proof_observations(observations, proofs, cutoff)
    if observation_error is not None:
        return observation_error

    productions: list[LaunchOutcomeProduction] = []
    entry_facts: list[ProductionEntryFacts] = []
    seen_launch_ids: set[str] = set()
    for proof in proofs:
        if (
            type(proof) is not FinalizedLaunchCaseProof
            or type(proof.launch) is not LaunchTrajectoryMetadata
            or type(proof.entry_facts) is not ProductionEntryFacts
        ):
            return _abstain(
                AbstainReason.MISSING_FEATURE,
                "finalized launch case proof is malformed",
                cutoff,
            )
        launch_id = proof.launch.launch_id
        if launch_id in seen_launch_ids:
            return _abstain(
                AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
                "finalized launch case proofs contain duplicate launch IDs",
                cutoff,
            )
        seen_launch_ids.add(launch_id)
        production = build_launch_outcome(
            launch=proof.launch,
            points=proof.points,
            outcome_config=proof.outcome_config,
            adverse_config=proof.adverse_config,
        )
        if isinstance(production, AbstainResult):
            return production
        productions.append(production)
        entry_facts.append(proof.entry_facts)

    return assemble_production_copy_trade_cases(
        launches=launches,
        fills=fills,
        entity_evidence=entity_evidence,
        productions=tuple(productions),
        entry_facts=tuple(entry_facts),
        as_of_slot=as_of_slot,
        entity_id=entity_id,
        regime_id=regime_id,
        min_entity_probability_ppm=min_entity_probability_ppm,
        max_entry_transaction_index=max_entry_transaction_index,
    )


def assemble_production_copy_trade_cases(  # noqa: PLR0911, PLR0913
    *,
    launches: tuple[LaunchCreatedV2, ...],
    fills: tuple[FinalizedTrade, ...],
    entity_evidence: tuple[WalletEntityEvidence, ...],
    productions: tuple[LaunchOutcomeProduction, ...],
    entry_facts: tuple[ProductionEntryFacts, ...],
    as_of_slot: Slot,
    entity_id: str,
    regime_id: str,
    min_entity_probability_ppm: int = 500_000,
    max_entry_transaction_index: int = 1,
) -> CaseAssemblyResult:
    """Convert complete productions and delegate canonical case assembly."""

    cutoff = _safe_slot(as_of_slot)
    if type(as_of_slot) is not int or as_of_slot < 0:
        return _abstain(
            AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
            "production case cutoff must be a non-negative integer",
            cutoff,
        )
    facts_by_launch = _index_entry_facts(entry_facts, cutoff)
    if isinstance(facts_by_launch, AbstainResult):
        return facts_by_launch
    if type(productions) is not tuple or not productions:
        return _abstain(
            AbstainReason.MISSING_FEATURE,
            "launch outcome productions are required",
            cutoff,
        )

    converted: list[ConvertedProduction] = []
    seen_launch_ids: set[str] = set()
    for production in productions:
        launch_id = _production_launch_id(production)
        if launch_id is None:
            return _abstain(
                AbstainReason.MISSING_FEATURE,
                "typed launch outcome production is required",
                cutoff,
            )
        if launch_id in seen_launch_ids:
            return _abstain(
                AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
                "launch outcome productions contain duplicate launch IDs",
                cutoff,
            )
        seen_launch_ids.add(launch_id)
        facts = facts_by_launch.get(launch_id)
        if facts is None:
            return _abstain(
                AbstainReason.MISSING_FEATURE,
                "production lacks finalized entry facts",
                cutoff,
            )
        result = _convert_production(production, facts, entity_id, cutoff)
        if isinstance(result, AbstainResult):
            return result
        converted.append(result)

    if set(facts_by_launch) != seen_launch_ids:
        return _abstain(
            AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
            "production entry facts contain an unjoined launch",
            cutoff,
        )

    return assemble_copy_trade_cases(
        launches=launches,
        fills=fills,
        entity_evidence=entity_evidence,
        trajectories=tuple(item[0] for item in converted),
        outcomes=tuple(item[1] for item in converted),
        outcome_labels=tuple(item[2] for item in converted),
        as_of_slot=as_of_slot,
        entity_id=entity_id,
        regime_id=regime_id,
        min_entity_probability_ppm=min_entity_probability_ppm,
        max_entry_transaction_index=max_entry_transaction_index,
    )


def _index_entry_facts(
    facts: object, cutoff: int
) -> dict[str, ProductionEntryFacts] | AbstainResult:
    if type(facts) is not tuple or not facts:
        return _abstain(
            AbstainReason.MISSING_FEATURE,
            "finalized production entry facts are required",
            cutoff,
        )
    indexed: dict[str, ProductionEntryFacts] = {}
    for item in facts:
        if type(item) is not ProductionEntryFacts or not _valid_entry_facts(item):
            return _abstain(
                AbstainReason.MISSING_FEATURE,
                "production entry facts are incomplete",
                cutoff,
            )
        if item.as_of_slot > cutoff:
            return _abstain(
                AbstainReason.STALE_STATE,
                "production entry facts are newer than the case cutoff",
                cutoff,
            )
        if item.launch_id in indexed:
            return _abstain(
                AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
                "production entry facts contain duplicate launch IDs",
                cutoff,
            )
        indexed[item.launch_id] = item
    return indexed


def _convert_production(
    production: LaunchOutcomeProduction,
    facts: ProductionEntryFacts,
    entity_id: str,
    cutoff: int,
) -> ConvertedProduction | AbstainResult:
    launch = production.launch
    labels = production.labels
    if (
        type(launch) is not LaunchTrajectoryMetadata
        or type(labels) is not LaunchOutcomeLabels
        or type(production.trajectory) is not tuple
        or not production.trajectory
        or labels.launch_id != launch.launch_id
        or labels.token_mint != launch.token_mint
        or type(labels.as_of_slot) is not int
        or labels.as_of_slot < launch.launch_slot
        or labels.as_of_slot > cutoff
        or labels.source_point_count != len(production.trajectory)
        or type(labels.max_executable_full_position_net_profit_before_adverse_event)
        is not int
    ):
        return _abstain(
            AbstainReason.MISSING_FEATURE,
            "production trajectory or labels are incomplete or mismatched",
            cutoff,
        )
    if facts.as_of_slot < launch.launch_slot or facts.as_of_slot > labels.as_of_slot:
        return _abstain(
            AbstainReason.STALE_STATE,
            "entry facts are outside the production boundary",
            cutoff,
        )
    terminal = _terminal_horizon(labels, cutoff)
    if isinstance(terminal, AbstainResult):
        return terminal
    holding_time_ms = terminal.last_observed_elapsed_ms - facts.wallet_buy_elapsed_ms
    if holding_time_ms < 0:
        return _abstain(
            AbstainReason.STALE_STATE,
            "wallet entry occurs after the completed outcome",
            cutoff,
        )

    artifact = CopyTradeTrajectoryArtifact(
        as_of_slot=labels.as_of_slot,
        launch_id=launch.launch_id,
        token_mint=launch.token_mint,
        launch_time_ms=launch.launch_timestamp * 1_000,
        entry_market_cap_quote_base_units=facts.entry_market_cap_quote_base_units,
        wallet_buy_elapsed_ms=facts.wallet_buy_elapsed_ms,
        holding_time_ms=holding_time_ms,
        trajectory=production.trajectory,
        evidence_ids=(
            f"production-entry:{launch.launch_id}:{int(labels.as_of_slot)}",
            *facts.evidence_ids,
        ),
    )
    outcome = CompletedLaunchOutcome(
        as_of_slot=labels.as_of_slot,
        entity_id=entity_id,
        launch_id=launch.launch_id,
        launch_slot=launch.launch_slot,
        completed_slot=terminal.last_observed_slot,
        completed=True,
        realized_net_pnl_quote_base_units=QuoteBaseUnits(
            terminal.full_exit_net_pnl_quote_base_units
        ),
        peak_net_pnl_quote_base_units=QuoteBaseUnits(
            labels.max_executable_full_position_net_profit_before_adverse_event
        ),
        adverse_event_observed=terminal.adverse_event_observed,
        evidence_ids=(
            f"production-outcome:{launch.launch_id}:"
            f"{int(terminal.last_observed_slot)}:{int(labels.as_of_slot)}",
        ),
    )
    return artifact, outcome, labels


def _terminal_horizon(
    labels: LaunchOutcomeLabels, cutoff: int
) -> HorizonOutcomeLabel | AbstainResult:
    complete = tuple(
        horizon
        for horizon in labels.horizon_labels
        if type(horizon) is HorizonOutcomeLabel and horizon.censored is False
    )
    if not complete:
        return _abstain(
            AbstainReason.MISSING_FEATURE,
            "production has no completed outcome horizon",
            cutoff,
        )
    terminal = max(complete, key=lambda item: item.horizon_ms)
    if (
        terminal.launch_id != labels.launch_id
        or terminal.token_mint != labels.token_mint
        or terminal.as_of_slot != labels.as_of_slot
        or type(terminal.last_observed_slot) is not int
        or terminal.last_observed_slot > terminal.as_of_slot
        or type(terminal.last_observed_elapsed_ms) is not int
        or terminal.last_observed_elapsed_ms < 0
        or type(terminal.full_exit_net_pnl_quote_base_units) is not int
    ):
        return _abstain(
            AbstainReason.MISSING_FEATURE,
            "completed production horizon lacks terminal evidence",
            cutoff,
        )
    return terminal


def _production_launch_id(production: object) -> str | None:
    if type(production) is not LaunchOutcomeProduction:
        return None
    launch = production.launch
    if type(launch) is not LaunchTrajectoryMetadata:
        return None
    return (
        launch.launch_id if type(launch.launch_id) is str and launch.launch_id else None
    )


def _valid_entry_facts(facts: ProductionEntryFacts) -> bool:
    return (
        type(facts.as_of_slot) is int
        and facts.as_of_slot >= 0
        and type(facts.launch_id) is str
        and bool(facts.launch_id)
        and type(facts.entry_market_cap_quote_base_units) is int
        and facts.entry_market_cap_quote_base_units > 0
        and type(facts.wallet_buy_elapsed_ms) is int
        and facts.wallet_buy_elapsed_ms >= 0
        and type(facts.evidence_ids) is tuple
        and bool(facts.evidence_ids)
        and len(set(facts.evidence_ids)) == len(facts.evidence_ids)
        and all(type(value) is str and bool(value) for value in facts.evidence_ids)
    )


def _validate_proof_observations(
    observations: object,
    proofs: tuple[FinalizedLaunchCaseProof, ...],
    cutoff: int,
) -> AbstainResult | None:
    if type(observations) is not tuple or any(
        type(item) is not RawChainObservation for item in observations
    ):
        return _abstain(
            AbstainReason.MISSING_FEATURE,
            "fetched finalized observations are required for case proofs",
            cutoff,
        )
    available = {observation_identity(item) for item in observations}
    for proof in proofs:
        if type(proof) is not FinalizedLaunchCaseProof:
            continue
        if type(proof.points) is not tuple:
            return _abstain(
                AbstainReason.MISSING_FEATURE,
                "finalized launch case proof points must be an immutable tuple",
                cutoff,
            )
        for point in proof.points:
            if type(point) is not FinalizedPumpTradePoint:
                continue
            if type(point.observation) is not RawChainObservation:
                continue
            if observation_identity(point.observation) not in available:
                return _abstain(
                    AbstainReason.MISSING_FEATURE,
                    "trajectory point has no matching fetched finalized observation",
                    cutoff,
                )
    return None


def _safe_slot(value: object) -> int:
    return value if type(value) is int else -1


def _abstain(reason: AbstainReason, message: str, as_of_slot: int) -> AbstainResult:
    return AbstainResult(reason=reason, message=message, as_of_slot=as_of_slot)


__all__ = [
    "FinalizedLaunchCaseProof",
    "ProductionEntryFacts",
    "assemble_observation_copy_trade_cases",
    "assemble_production_copy_trade_cases",
]
