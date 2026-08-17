"""Shared finalized-observation orchestration for replay and RPC callers.

Acquisition is deliberately outside this module. Replay and RPC callers hand
the same immutable observations and explicit typed artifacts to the pure
orchestrator, so strategy logic has one execution path.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from rugbot.backtest.copytrade import CopyTradeConfig, CopyTradeLaunchCase
from rugbot.backtest.dataset import (
    FinalizedBacktestDataset,
    FinalizedBacktestResult,
    FinalizedTrade,
    FullExitStressConfig,
    build_finalized_dataset,
    run_finalized_backtest,
)
from rugbot.backtest.evaluation import (
    BacktestConfig,
    FrozenModelManifest,
)
from rugbot.backtest.production_case_adapter import (
    FinalizedLaunchCaseProof,
    ProductionEntryFacts,
    assemble_observation_copy_trade_cases,
    assemble_production_copy_trade_cases,
)
from rugbot.domain.decisions import AbstainReason, AbstainResult
from rugbot.domain.observations import RawChainObservation
from rugbot.storage.jsonl_observation_store import observation_identity

if TYPE_CHECKING:
    from rugbot.backtest.trajectory_producer import LaunchOutcomeProduction
    from rugbot.decision.operator_qualification import WalletEntityEvidence
    from rugbot.domain.amounts import Slot
    from rugbot.domain.launches import LaunchCreatedV2


@dataclass(frozen=True, slots=True)
class FinalizedBacktestRunArtifacts:
    """Immutable strategy inputs for one finalized backtest run."""

    strategy: CopyTradeConfig
    manifest: FrozenModelManifest
    backtest_config: BacktestConfig
    stress: FullExitStressConfig


@dataclass(frozen=True, slots=True)
class FinalizedBacktestMetadata:
    """Explicit point-in-time artifacts joined to raw observations."""

    as_of_slot: Slot
    trades: tuple[FinalizedTrade, ...]
    cases: tuple[CopyTradeLaunchCase, ...]
    run: FinalizedBacktestRunArtifacts | None = None


FinalizedPipelineResult = (
    FinalizedBacktestDataset | FinalizedBacktestResult | AbstainResult
)


def run_finalized_backtest_pipeline(
    *,
    observations: tuple[RawChainObservation, ...],
    metadata: FinalizedBacktestMetadata,
) -> FinalizedPipelineResult:
    """Run the common finalized pipeline after replay or RPC acquisition.

    ``FinalizedBacktestMetadata`` builds a dataset and, when its run artifacts
    are present, executes the strategy. No branch performs acquisition,
    database access, signing, or submission.
    """

    validation = _validate_inputs(observations=observations, metadata=metadata)
    if validation is not None:
        return validation
    dataset = build_finalized_dataset(
        observations=observations,
        cases=metadata.cases,
        trades=metadata.trades,
        as_of_slot=metadata.as_of_slot,
    )
    if isinstance(dataset, AbstainResult):
        return dataset
    if metadata.run is None:
        return dataset

    return run_finalized_backtest(
        dataset=dataset,
        strategy=metadata.run.strategy,
        manifest=metadata.run.manifest,
        backtest_config=metadata.run.backtest_config,
        stress=metadata.run.stress,
    )


def run_production_backtest_pipeline(  # noqa: PLR0913
    *,
    observations: tuple[RawChainObservation, ...],
    launches: tuple[LaunchCreatedV2, ...],
    trades: tuple[FinalizedTrade, ...],
    entity_evidence: tuple[WalletEntityEvidence, ...],
    as_of_slot: Slot,
    entity_id: str,
    regime_id: str,
    productions: tuple[LaunchOutcomeProduction, ...] = (),
    entry_facts: tuple[ProductionEntryFacts, ...] = (),
    run: FinalizedBacktestRunArtifacts | None = None,
    min_entity_probability_ppm: int = 500_000,
    max_entry_transaction_index: int = 1,
    proofs: tuple[FinalizedLaunchCaseProof, ...] = (),
) -> FinalizedPipelineResult:
    """Adapt typed launch proofs or produced artifacts into the final pipeline."""

    if proofs and (productions or entry_facts):
        return _abstain(
            AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
            "provide finalized launch proofs or produced cases, not both",
            _safe_slot(as_of_slot),
        )
    if proofs:
        cases = assemble_observation_copy_trade_cases(
            launches=launches,
            fills=trades,
            entity_evidence=entity_evidence,
            observations=observations,
            proofs=proofs,
            as_of_slot=as_of_slot,
            entity_id=entity_id,
            regime_id=regime_id,
            min_entity_probability_ppm=min_entity_probability_ppm,
            max_entry_transaction_index=max_entry_transaction_index,
        )
    else:
        cases = assemble_production_copy_trade_cases(
            launches=launches,
            fills=trades,
            entity_evidence=entity_evidence,
            productions=productions,
            entry_facts=entry_facts,
            as_of_slot=as_of_slot,
            entity_id=entity_id,
            regime_id=regime_id,
            min_entity_probability_ppm=min_entity_probability_ppm,
            max_entry_transaction_index=max_entry_transaction_index,
        )
    if isinstance(cases, AbstainResult):
        return cases
    return run_finalized_backtest_pipeline(
        observations=observations,
        metadata=FinalizedBacktestMetadata(
            as_of_slot=as_of_slot,
            trades=trades,
            cases=cases,
            run=run,
        ),
    )


def _validate_inputs(  # noqa: C901, PLR0911, PLR0912
    *,
    observations: object,
    metadata: object,
) -> AbstainResult | None:
    if type(observations) is not tuple:
        return _abstain(
            AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
            "finalized pipeline observations must be an immutable tuple",
            -1,
        )
    if type(metadata) is not FinalizedBacktestMetadata:
        return _abstain(
            AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
            "finalized pipeline metadata is malformed",
            -1,
        )
    cutoff = _safe_slot(metadata.as_of_slot)
    if cutoff < 0:
        return _abstain(
            AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
            "finalized pipeline cutoff must be a non-negative slot",
            cutoff,
        )
    if any(type(item) is not RawChainObservation for item in observations):
        return _abstain(
            AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
            "finalized pipeline contains a malformed raw observation",
            cutoff,
        )
    identities = tuple(observation_identity(item) for item in observations)
    if len(set(identities)) != len(identities):
        return _abstain(
            AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
            "finalized pipeline contains duplicate raw evidence",
            cutoff,
        )
    for observation in observations:
        if (
            observation.commitment != "finalized"
            or observation.canonical_status != "canonical"
        ):
            return _abstain(
                AbstainReason.STALE_STATE,
                "finalized pipeline requires canonical finalized evidence",
                cutoff,
            )
        if type(observation.slot) is not int or observation.slot < 0:
            return _abstain(
                AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
                "finalized pipeline observation slot is malformed",
                cutoff,
            )
        if observation.slot > cutoff:
            return _abstain(
                AbstainReason.STALE_STATE,
                "finalized pipeline observation is newer than its cutoff",
                cutoff,
            )

    if type(metadata.trades) is not tuple or type(metadata.cases) is not tuple:
        return _abstain(
            AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
            "finalized pipeline artifacts must be immutable tuples",
            cutoff,
        )
    run = metadata.run
    if run is None:
        return None
    if type(run) is not FinalizedBacktestRunArtifacts or not all(
        isinstance(value, expected)
        for value, expected in (
            (run.strategy, CopyTradeConfig),
            (run.manifest, FrozenModelManifest),
            (run.backtest_config, BacktestConfig),
            (run.stress, FullExitStressConfig),
        )
    ):
        return _abstain(
            AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
            "finalized pipeline run artifacts are malformed",
            cutoff,
        )
    if (
        run.strategy.as_of_slot != cutoff
        or run.manifest.as_of_slot != cutoff
        or run.backtest_config.as_of_slot != cutoff
        or run.stress.as_of_slot != cutoff
        or run.backtest_config.manifest != run.manifest
    ):
        return _abstain(
            AbstainReason.STALE_STATE,
            "finalized pipeline run artifacts do not share the cutoff",
            cutoff,
        )
    return None


def _safe_slot(value: object) -> int:
    return value if type(value) is int else -1


def _abstain(reason: AbstainReason, message: str, as_of_slot: int) -> AbstainResult:
    return AbstainResult(reason=reason, message=message, as_of_slot=as_of_slot)


__all__ = [
    "FinalizedBacktestMetadata",
    "FinalizedBacktestRunArtifacts",
    "FinalizedPipelineResult",
    "run_finalized_backtest_pipeline",
    "run_production_backtest_pipeline",
]
