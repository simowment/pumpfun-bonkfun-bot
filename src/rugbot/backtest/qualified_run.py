"""Composition of point-in-time qualification and finalized OOS backtesting."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from rugbot.backtest.copytrade import CopyTradeConfig, CopyTradeLaunchCase
from rugbot.backtest.dataset import (
    FinalizedBacktestResult,
    FinalizedTrade,
    FullExitStressConfig,
    build_finalized_dataset,
    run_finalized_backtest,
)
from rugbot.backtest.evaluation import BacktestConfig, FrozenModelManifest
from rugbot.decision.operator_qualification import (
    CompletedLaunchOutcome,
    OperatorQualification,
    OperatorQualificationConfig,
    QualificationStatus,
    WalletEntityEvidence,
    qualify_operator,
)
from rugbot.domain.decisions import AbstainReason, AbstainResult

if TYPE_CHECKING:
    from rugbot.domain.observations import RawChainObservation


@dataclass(frozen=True, slots=True)
class QualifiedRunResult:
    """Qualification metrics and, only when qualified, the OOS result.

    A result with ``backtest=None`` is an explicit qualification abstention;
    callers must not present it as a successful backtest.
    """

    qualification: OperatorQualification
    backtest: FinalizedBacktestResult | None

    @property
    def is_qualified(self) -> bool:
        """Return whether qualification and the OOS result are both present."""

        return (
            self.qualification.status is QualificationStatus.QUALIFIED
            and self.backtest is not None
        )


def run_qualified_finalized_backtest(  # noqa: PLR0911, PLR0913
    *,
    observations: tuple[RawChainObservation, ...],
    cases: tuple[CopyTradeLaunchCase, ...],
    trades: tuple[FinalizedTrade, ...],
    outcomes: tuple[CompletedLaunchOutcome, ...],
    entity_evidence: tuple[WalletEntityEvidence, ...],
    qualification_config: OperatorQualificationConfig,
    strategy: CopyTradeConfig,
    manifest: FrozenModelManifest,
    backtest_config: BacktestConfig,
    stress: FullExitStressConfig,
) -> QualifiedRunResult | AbstainResult:
    """Qualify an operator, then run the existing leakage-safe OOS pipeline.

    The required typed fills and completed outcomes are checked before
    qualification so incomplete historical evidence is an explicit
    abstention.  A threshold miss returns the computed qualification metrics
    and never constructs or evaluates a backtest dataset.
    """

    cutoff_error = _validate_cutoff(qualification_config)
    if cutoff_error is not None:
        return cutoff_error
    cutoff = qualification_config.as_of_slot

    evidence_error = _validate_required_typed_evidence(
        outcomes=outcomes,
        trades=trades,
        as_of_slot=cutoff,
    )
    if evidence_error is not None:
        return evidence_error

    qualification = qualify_operator(
        outcomes=outcomes,
        entity_evidence=entity_evidence,
        config=qualification_config,
    )
    if qualification.status is not QualificationStatus.QUALIFIED:
        return QualifiedRunResult(qualification=qualification, backtest=None)

    cutoff_error = _validate_pipeline_cutoffs(
        cutoff=cutoff,
        strategy=strategy,
        manifest=manifest,
        backtest_config=backtest_config,
        stress=stress,
    )
    if cutoff_error is not None:
        return cutoff_error

    dataset = build_finalized_dataset(
        observations=observations,
        cases=cases,
        trades=trades,
        as_of_slot=cutoff,
    )
    if isinstance(dataset, AbstainResult):
        return dataset

    backtest = run_finalized_backtest(
        dataset=dataset,
        strategy=strategy,
        manifest=manifest,
        backtest_config=backtest_config,
        stress=stress,
    )
    if isinstance(backtest, AbstainResult):
        return backtest
    return QualifiedRunResult(qualification=qualification, backtest=backtest)


def _validate_cutoff(
    config: object,
) -> AbstainResult | None:
    if not isinstance(config, OperatorQualificationConfig):
        return AbstainResult(
            reason=AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
            message="qualification config is malformed",
            as_of_slot=-1,
        )
    if type(config.as_of_slot) is not int or config.as_of_slot < 0:
        return AbstainResult(
            reason=AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
            message="qualification cutoff must be a non-negative integer",
            as_of_slot=-1,
        )
    return None


def _validate_required_typed_evidence(
    *,
    outcomes: object,
    trades: object,
    as_of_slot: int,
) -> AbstainResult | None:
    if not isinstance(outcomes, tuple) or not outcomes:
        return AbstainResult(
            reason=AbstainReason.MISSING_FEATURE,
            message="typed completed launch outcomes are required",
            as_of_slot=as_of_slot,
        )
    if any(not isinstance(item, CompletedLaunchOutcome) for item in outcomes):
        return AbstainResult(
            reason=AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
            message="historical outcomes must use CompletedLaunchOutcome",
            as_of_slot=as_of_slot,
        )
    if not isinstance(trades, tuple) or not trades:
        return AbstainResult(
            reason=AbstainReason.MISSING_FEATURE,
            message="typed finalized fills are required",
            as_of_slot=as_of_slot,
        )
    if any(not isinstance(item, FinalizedTrade) for item in trades):
        return AbstainResult(
            reason=AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
            message="fills must use FinalizedTrade",
            as_of_slot=as_of_slot,
        )
    return None


def _validate_pipeline_cutoffs(
    *,
    cutoff: int,
    strategy: object,
    manifest: object,
    backtest_config: object,
    stress: object,
) -> AbstainResult | None:
    if not isinstance(strategy, CopyTradeConfig):
        return AbstainResult(
            reason=AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
            message="copy-trade strategy is malformed",
            as_of_slot=cutoff,
        )
    if not isinstance(manifest, FrozenModelManifest):
        return AbstainResult(
            reason=AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
            message="backtest manifest is malformed",
            as_of_slot=cutoff,
        )
    if not isinstance(backtest_config, BacktestConfig):
        return AbstainResult(
            reason=AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
            message="backtest config is malformed",
            as_of_slot=cutoff,
        )
    if not isinstance(stress, FullExitStressConfig):
        return AbstainResult(
            reason=AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
            message="full-exit stress config is malformed",
            as_of_slot=cutoff,
        )
    if any(
        value != cutoff
        for value in (
            strategy.as_of_slot,
            manifest.as_of_slot,
            manifest.model_freeze_slot,
            backtest_config.as_of_slot,
            stress.as_of_slot,
        )
    ):
        return AbstainResult(
            reason=AbstainReason.STALE_STATE,
            message="qualification and backtest cutoffs must match",
            as_of_slot=cutoff,
        )
    return None


__all__ = ["QualifiedRunResult", "run_qualified_finalized_backtest"]
