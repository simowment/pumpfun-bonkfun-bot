"""Leakage evidence: the finalized pipeline rejects post-cutoff evidence.

``run_finalized_backtest_pipeline`` is the single orchestration point shared by
replay and RPC backtests. Its ``as_of_slot`` cutoff is the leakage boundary:
observations newer than the cutoff must abort the run instead of being
silently partitioned, and every run artifact must share the exact cutoff.
"""

from __future__ import annotations

from uuid import UUID, uuid4

from rugbot.backtest.dataset import FullExitStressConfig
from rugbot.backtest.evaluation import BacktestConfig, FrozenModelManifest
from rugbot.backtest.runners.copytrade import CopyTradeConfig
from rugbot.backtest.runners.online_pipeline import (
    FinalizedBacktestMetadata,
    FinalizedBacktestRunArtifacts,
    run_finalized_backtest_pipeline,
)
from rugbot.domain.decisions import AbstainResult
from rugbot.domain.observations import RawChainObservation

CUTOFF = 100


def _observation(slot: int) -> RawChainObservation:
    """Build one canonical finalized observation at ``slot``."""
    return RawChainObservation(
        raw_id=uuid4(),
        source_id="test",
        observer_id="test",
        boot_id=UUID(int=0),
        receive_sequence=slot,
        slot=slot,
        parent_slot=slot - 1,
        blockhash=None,
        signature=None,
        transaction_index=0,
        outer_instruction_index=None,
        inner_instruction_group_index=None,
        inner_instruction_index=None,
        stack_height=None,
        event_ordinal=None,
        commitment="finalized",
        canonical_status="canonical",
        received_wall_ns=0,
        received_monotonic_ns=0,
        program_id=None,
        account_pubkey=None,
        account_owner_program_id=None,
        raw_transaction=None,
        raw_transaction_format=None,
        raw_account_data=None,
        account_write_version=None,
        source_update_kind=None,
        raw_source_status=None,
        raw_source_payload=None,
        decoder_name=None,
        decoder_version=None,
        idl_hash=None,
    )


def _manifest(as_of_slot: int) -> FrozenModelManifest:
    return FrozenModelManifest(
        as_of_slot=as_of_slot,
        model_freeze_slot=as_of_slot - 1,
        decision_version="test-decision",
        model_version="test-model",
        outcome_labeler_version="test-labeler",
        profile_snapshot_version="test-profile",
        graph_snapshot_version="test-graph",
        feature_snapshot_version="test-feature",
        market_snapshot_version="test-market",
        latency_model_version="test-latency",
        fee_config_version="test-fee",
    )


def _run_artifacts(as_of_slot: int) -> FinalizedBacktestRunArtifacts:
    manifest = _manifest(as_of_slot)
    return FinalizedBacktestRunArtifacts(
        strategy=CopyTradeConfig(as_of_slot=as_of_slot),
        manifest=manifest,
        backtest_config=BacktestConfig(
            as_of_slot=as_of_slot,
            evaluation_version="test-evaluation",
            manifest=manifest,
            train_end_slot=as_of_slot - 2,
            test_start_slot=as_of_slot - 1,
            test_end_slot=as_of_slot,
            train_entity_ids=(),
            stress_entity_ids=(),
            expected_shortfall_tail_ppm=50_000,
        ),
        stress=FullExitStressConfig(
            as_of_slot=as_of_slot,
            output_haircut_ppm=0,
            additional_execution_cost_quote_base_units=0,
        ),
    )


def test_pipeline_rejects_observation_newer_than_cutoff() -> None:
    """A post-cutoff observation aborts instead of leaking into the replay."""
    result = run_finalized_backtest_pipeline(
        observations=(_observation(CUTOFF + 1),),
        metadata=FinalizedBacktestMetadata(as_of_slot=CUTOFF, trades=(), cases=()),
    )
    assert isinstance(result, AbstainResult)
    assert "newer than its cutoff" in result.message


def test_pipeline_cutoff_is_inclusive() -> None:
    """Slot == cutoff passes the leakage gate; only strictly-future data aborts."""
    result = run_finalized_backtest_pipeline(
        observations=(_observation(CUTOFF),),
        metadata=FinalizedBacktestMetadata(as_of_slot=CUTOFF, trades=(), cases=()),
    )
    # Downstream evidence assembly may still abstain on empty artifacts, but
    # that abstention must never come from the cutoff boundary itself.
    assert not isinstance(result, AbstainResult) or "cutoff" not in result.message


def test_pipeline_rejects_run_artifacts_from_another_cutoff() -> None:
    """Strategy, manifest, config, and stress must share the metadata cutoff."""
    result = run_finalized_backtest_pipeline(
        observations=(_observation(CUTOFF),),
        metadata=FinalizedBacktestMetadata(
            as_of_slot=CUTOFF,
            trades=(),
            cases=(),
            run=_run_artifacts(CUTOFF - 1),
        ),
    )
    assert isinstance(result, AbstainResult)
    assert "do not share the cutoff" in result.message


def test_pipeline_accepts_artifacts_stamped_at_the_cutoff() -> None:
    """Artifacts at the exact cutoff clear the shared-cutoff gate."""
    result = run_finalized_backtest_pipeline(
        observations=(_observation(CUTOFF - 1),),
        metadata=FinalizedBacktestMetadata(
            as_of_slot=CUTOFF,
            trades=(),
            cases=(),
            run=_run_artifacts(CUTOFF),
        ),
    )
    assert not isinstance(result, AbstainResult) or "cutoff" not in result.message
