"""Replay finalized observations with explicitly typed case proof inputs.

The repository has a canonical JSONL contract for ``RawChainObservation`` but
does not have a serialization contract for ``FinalizedLaunchCaseProof`` or its
dependent point-in-time artifacts.  This boundary therefore reads only the
existing observation store and accepts case metadata as the existing typed
objects.  It delegates replay and strategy execution to the shared finalized
pipeline so offline replay cannot acquire different evidence or use a second
strategy implementation.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from rugbot.backtest.online_pipeline import (
    FinalizedBacktestRunArtifacts,
    FinalizedPipelineResult,
    run_production_backtest_pipeline,
)
from rugbot.domain.decisions import AbstainReason, AbstainResult
from rugbot.storage.jsonl_observation_store import JsonlObservationStore

if TYPE_CHECKING:
    from rugbot.backtest.dataset import FinalizedTrade
    from rugbot.backtest.production_case_adapter import FinalizedLaunchCaseProof
    from rugbot.decision.operator_qualification import WalletEntityEvidence
    from rugbot.domain.amounts import Slot
    from rugbot.domain.launches import LaunchCreatedV2


def run_case_proof_replay(  # noqa: PLR0913
    *,
    observation_path: Path,
    as_of_slot: Slot,
    launches: tuple[LaunchCreatedV2, ...],
    trades: tuple[FinalizedTrade, ...],
    entity_evidence: tuple[WalletEntityEvidence, ...],
    proofs: tuple[FinalizedLaunchCaseProof, ...],
    entity_id: str,
    regime_id: str,
    run: FinalizedBacktestRunArtifacts | None = None,
    min_entity_probability_ppm: int = 500_000,
    max_entry_transaction_index: int = 1,
) -> FinalizedPipelineResult:
    """Replay one canonical observation store through the shared pipeline.

    ``launches``, ``trades``, ``entity_evidence``, and ``proofs`` are required
    to be repository-defined immutable typed artifacts.  They are deliberately
    not loaded from JSON here because no safe serialization contract exists for
    those proof types.  When ``run`` is supplied, the same finalized strategy
    path used by online callers is executed; otherwise the typed dataset is
    assembled for inspection.

    Args:
        observation_path: Existing canonical raw-observation JSONL path.
        as_of_slot: Inclusive finalized replay cutoff.
        launches: Pinned launch decodes joined to the observations.
        trades: Explicit finalized fill artifacts.
        entity_evidence: Point-in-time wallet/entity evidence.
        proofs: Complete finalized launch trajectory proof bundles.
        entity_id: Entity evaluated by the copy-trade strategy.
        regime_id: Regime identifier for the same strategy universe.
        run: Optional frozen strategy/evaluation artifacts.
        min_entity_probability_ppm: Minimum entity evidence probability.
        max_entry_transaction_index: Highest accepted copied entry position.

    Returns:
        The shared finalized pipeline result, or an explicit abstention when
        the observation store cannot be decoded.
    """

    if type(as_of_slot) is not int or as_of_slot < 0:
        return _abstain(
            AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
            "case-proof replay cutoff must be a non-negative integer",
            _safe_slot(as_of_slot),
        )
    if not isinstance(observation_path, Path):
        return _abstain(
            AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
            "case-proof replay observation path must be a Path",
            as_of_slot,
        )

    try:
        observations = tuple(JsonlObservationStore(observation_path).read_all())
    except (OSError, UnicodeError, ValueError) as error:
        return _abstain(
            AbstainReason.UNKNOWN_PROTOCOL_STATE,
            f"finalized observation replay failed: {type(error).__name__}",
            as_of_slot,
        )

    return run_production_backtest_pipeline(
        observations=observations,
        launches=launches,
        trades=trades,
        entity_evidence=entity_evidence,
        as_of_slot=as_of_slot,
        entity_id=entity_id,
        regime_id=regime_id,
        proofs=proofs,
        run=run,
        min_entity_probability_ppm=min_entity_probability_ppm,
        max_entry_transaction_index=max_entry_transaction_index,
    )


def _safe_slot(value: object) -> int:
    return value if type(value) is int else -1


def _abstain(
    reason: AbstainReason,
    message: str,
    as_of_slot: int,
) -> AbstainResult:
    return AbstainResult(reason=reason, message=message, as_of_slot=as_of_slot)


__all__ = ["run_case_proof_replay"]
