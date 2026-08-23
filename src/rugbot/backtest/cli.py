"""Command-line entry point for offline leakage-safe backtests."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from dataclasses import fields, is_dataclass
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING

from rugbot.backtest.cases.rpc_case_acquisition import (
    acquire_finalized_rpc_case_observations,
    build_rpc_case_proofs,
)
from rugbot.backtest.dataset import (
    FinalizedBacktestDataset,
    FinalizedBacktestResult,
    FullExitStressConfig,
)
from rugbot.backtest.evaluation import (
    BacktestConfig,
    FrozenModelManifest,
    build_backtest_report,
)
from rugbot.backtest.io import load_backtest_document
from rugbot.backtest.runners.copytrade import CopyTradeConfig
from rugbot.backtest.runners.online_pipeline import (
    FinalizedBacktestMetadata,
    FinalizedBacktestRunArtifacts,
    run_finalized_backtest_pipeline,
    run_production_backtest_pipeline,
)
from rugbot.backtest.runners.qualified_run import QualifiedRunResult
from rugbot.backtest.trajectory.finalized_trade_builder import (
    build_finalized_trades_from_observations,
)
from rugbot.backtest.trajectory.observation_trade_join import (
    derive_finalized_trade_joins,
)
from rugbot.domain.amounts import Slot
from rugbot.domain.decisions import AbstainReason, AbstainResult
from rugbot.storage.jsonl_observation_store import JsonlObservationStore

MAX_RPC_TRANSACTIONS = 1000
MIN_RPC_LAUNCHES_FOR_SPLIT = 2

if TYPE_CHECKING:
    from collections.abc import Sequence

    from rugbot.backtest.evaluation import BacktestReport


def run_backtest_file(path: Path) -> BacktestReport | AbstainResult:
    """Validate and evaluate one fixed-shape backtest artifact.

    The input contains already-proven, typed launch outcomes. Qualification is
    deliberately not inferred here; RPC acquisition and the qualified pipeline
    remain separate entry points and still abstain when their typed evidence
    is incomplete.
    """

    document = load_backtest_document(path)
    if isinstance(document, AbstainResult):
        return document
    try:
        observations = JsonlObservationStore(
            path.parent / document.raw_observation_path
        ).read_all()
    except (OSError, UnicodeError, ValueError) as error:
        return AbstainResult(
            reason=AbstainReason.UNKNOWN_PROTOCOL_STATE,
            message=f"raw observation replay failed: {type(error).__name__}",
            as_of_slot=-1,
        )

    if len(observations) != len(document.launches):
        return AbstainResult(
            reason=AbstainReason.MISSING_FEATURE,
            message="raw observation count does not match backtest launches",
            as_of_slot=document.config.as_of_slot,
        )
    ordered = sorted(
        observations,
        key=lambda item: (
            item.slot,
            item.transaction_index if item.transaction_index is not None else -1,
            item.event_ordinal if item.event_ordinal is not None else -1,
        ),
    )
    for ordinal, (observation, launch) in enumerate(
        zip(ordered, document.launches, strict=True)
    ):
        if (
            observation.commitment != "finalized"
            or observation.canonical_status != "canonical"
            or observation.event_ordinal != ordinal
            or observation.slot != launch.decision_slot
        ):
            return AbstainResult(
                reason=AbstainReason.UNKNOWN_PROTOCOL_STATE,
                message="raw observation is not canonical backtest evidence",
                as_of_slot=observation.slot,
            )
    return build_backtest_report(
        launches=document.launches,
        config=document.config,
    )


def run_replayed_dataset_file(
    path: Path,
    *,
    as_of_slot: Slot,
) -> FinalizedBacktestDataset | FinalizedBacktestResult | AbstainResult:
    """Replay finalized JSONL observations through the shared pipeline.

    This command intentionally supplies no synthetic trades or cases.  Those
    artifacts must be produced explicitly by finalized decoders/features and
    passed to ``run_finalized_backtest_pipeline`` by an online caller.
    """

    try:
        observations = tuple(JsonlObservationStore(path).read_all())
    except (OSError, UnicodeError, ValueError) as error:
        return AbstainResult(
            reason=AbstainReason.UNKNOWN_PROTOCOL_STATE,
            message=f"finalized observation replay failed: {type(error).__name__}",
            as_of_slot=int(as_of_slot),
        )
    return run_finalized_backtest_pipeline(
        observations=observations,
        metadata=FinalizedBacktestMetadata(
            as_of_slot=as_of_slot,
            trades=(),
            cases=(),
        ),
    )


async def run_rpc_dataset(  # noqa: PLR0913
    *,
    operator_wallet: str,
    endpoint: str,
    start_slot: Slot,
    end_slot: Slot,
    max_transactions: int,
    fixed_entry_quote_base_units: int = 1_000_000,
    horizon_ms: int = 0,
    min_history_launch_count: int = 15,
) -> FinalizedBacktestDataset | FinalizedBacktestResult | AbstainResult:
    """Acquire finalized RPC evidence and pass it through the shared pipeline."""

    acquired = await acquire_finalized_rpc_case_observations(
        operator_wallet=operator_wallet,
        endpoint=endpoint,
        start_slot=start_slot,
        as_of_slot=end_slot,
        max_transactions_per_address=max_transactions,
    )
    if isinstance(acquired, AbstainResult):
        return acquired
    bounded_observations = tuple(
        observation
        for observation in acquired.observations
        if start_slot <= observation.slot <= end_slot
    )
    joins = derive_finalized_trade_joins(
        observations=bounded_observations,
        as_of_slot=end_slot,
        eligible_mints=frozenset(acquired.launch_mints),
    )
    if isinstance(joins, AbstainResult):
        return joins
    _, trade_joins = joins
    trades = build_finalized_trades_from_observations(
        observations=bounded_observations,
        joins=trade_joins,
        as_of_slot=end_slot,
    )
    if isinstance(trades, AbstainResult):
        return trades
    bundle = build_rpc_case_proofs(
        acquisition=acquired,
        trades=trades,
        as_of_slot=end_slot,
        fixed_entry_quote_base_units=fixed_entry_quote_base_units,
        horizon_ms=horizon_ms,
    )
    if isinstance(bundle, AbstainResult):
        return bundle
    if len(acquired.launches) < MIN_RPC_LAUNCHES_FOR_SPLIT:
        return AbstainResult(
            reason=AbstainReason.MISSING_FEATURE,
            message="at least two finalized launches are required for a split",
            as_of_slot=int(end_slot),
        )
    ordered_launches = tuple(
        sorted(acquired.launches, key=lambda item: (item.as_of_slot, item.launch_id))
    )
    test_start_slot = Slot(ordered_launches[-1].as_of_slot)
    train_end_slot = Slot(ordered_launches[-2].as_of_slot)
    manifest = FrozenModelManifest(
        as_of_slot=end_slot,
        model_freeze_slot=train_end_slot,
        decision_version="copy-trade-fixed-entry",
        model_version="rule-based",
        outcome_labeler_version="pump-trade-event-outcome",
        profile_snapshot_version="finalized-rpc",
        graph_snapshot_version="finalized-rpc",
        feature_snapshot_version="finalized-rpc",
        market_snapshot_version="finalized-rpc",
        latency_model_version="zero-delay-paper",
        fee_config_version="pump-trade-event-fees",
    )
    strategy = CopyTradeConfig(
        as_of_slot=end_slot,
        min_history_launch_count=min_history_launch_count,
        max_history_launch_count=20,
        max_entry_transaction_index=1,
        fixed_entry_quote_base_units=fixed_entry_quote_base_units,
    )
    backtest_config = BacktestConfig(
        as_of_slot=end_slot,
        evaluation_version="finalized-rpc-backtest",
        manifest=manifest,
        train_end_slot=train_end_slot,
        test_start_slot=test_start_slot,
        test_end_slot=end_slot,
        train_entity_ids=(operator_wallet,),
        stress_entity_ids=(),
        expected_shortfall_tail_ppm=100_000,
    )
    return run_production_backtest_pipeline(
        observations=bounded_observations,
        launches=acquired.launches,
        trades=trades,
        entity_evidence=bundle.entity_evidence,
        proofs=bundle.proofs,
        as_of_slot=end_slot,
        entity_id=operator_wallet,
        regime_id="pump-curve",
        run=FinalizedBacktestRunArtifacts(
            strategy=strategy,
            manifest=manifest,
            backtest_config=backtest_config,
            stress=FullExitStressConfig(
                as_of_slot=end_slot,
                output_haircut_ppm=1_000_000,
                additional_execution_cost_quote_base_units=0,
            ),
        ),
    )


def build_arg_parser() -> argparse.ArgumentParser:
    """Build the offline backtest CLI parser."""

    parser = argparse.ArgumentParser(
        description="Run a leakage-safe offline backtest from a JSON document."
    )
    parser.add_argument(
        "--input",
        type=Path,
        help="Path to the fixed-shape backtest JSON document.",
    )
    parser.add_argument(
        "--replay",
        type=Path,
        help="Path to a JSONL file containing finalized observations.",
    )
    parser.add_argument(
        "--operator-wallet",
        help="Known operator wallet whose finalized history should be acquired.",
    )
    parser.add_argument(
        "--start-slot",
        type=int,
        help="Inclusive finalized slot lower bound for the operator history window.",
    )
    parser.add_argument(
        "--end-slot",
        type=int,
        help="Inclusive finalized slot upper bound for the operator history window.",
    )
    parser.add_argument(
        "--max-transactions",
        type=int,
        default=20,
        help="Maximum finalized transactions acquired in the operator history window.",
    )
    parser.add_argument(
        "--quote-size-lamports",
        type=int,
        default=1_000_000,
        help="Fixed paper entry size in lamports for the historical case builder.",
    )
    parser.add_argument(
        "--horizon-ms",
        type=int,
        default=0,
        help="Required outcome horizon; zero uses the observed launch duration.",
    )
    parser.add_argument(
        "--min-history-launch-count",
        type=int,
        default=15,
        help="Minimum completed launches required before a copy-trade entry.",
    )
    parser.add_argument(
        "--endpoint",
        help=(
            "HTTP RPC endpoint used with --operator-wallet; "
            "defaults to SOLANA_RPC_HTTP."
        ),
    )
    parser.add_argument(
        "--as-of-slot",
        type=int,
        help="Explicit finalized cutoff used with --replay.",
    )
    parser.add_argument(
        "--pretty",
        action="store_true",
        help="Pretty-print the JSON report.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:  # noqa: C901, PLR0912
    """Run the offline backtest command."""

    parser = build_arg_parser()
    args = parser.parse_args(argv)
    modes = (
        args.input is not None,
        args.replay is not None,
        args.operator_wallet is not None,
    )
    if sum(modes) != 1:
        parser.error("provide exactly one of --input, --replay, or --operator-wallet")
    if args.operator_wallet is not None:
        if (
            args.start_slot is None
            or args.end_slot is None
            or args.start_slot < 0
            or args.end_slot < args.start_slot
        ):
            parser.error("--operator-wallet requires a valid --start-slot/--end-slot")
        if not 1 <= args.max_transactions <= MAX_RPC_TRANSACTIONS:
            parser.error("--max-transactions must be between 1 and 1000")
        if args.quote_size_lamports <= 0:
            parser.error("--quote-size-lamports must be positive")
        if args.horizon_ms < 0:
            parser.error("--horizon-ms must be non-negative")
        if args.min_history_launch_count < 1:
            parser.error("--min-history-launch-count must be positive")
        endpoint = args.endpoint or os.environ.get("SOLANA_RPC_HTTP")
        if not endpoint:
            parser.error("--operator-wallet requires --endpoint or SOLANA_RPC_HTTP")
        result = asyncio.run(
            run_rpc_dataset(
                operator_wallet=args.operator_wallet,
                endpoint=endpoint,
                start_slot=Slot(args.start_slot),
                end_slot=Slot(args.end_slot),
                max_transactions=args.max_transactions,
                fixed_entry_quote_base_units=args.quote_size_lamports,
                horizon_ms=args.horizon_ms,
                min_history_launch_count=args.min_history_launch_count,
            )
        )
    elif args.replay is not None:
        if args.as_of_slot is None or args.as_of_slot < 0:
            parser.error("--replay requires a non-negative --as-of-slot")
        result = run_replayed_dataset_file(
            args.replay,
            as_of_slot=Slot(args.as_of_slot),
        )
    else:
        result = run_backtest_file(args.input)
    if isinstance(result, AbstainResult):
        payload: object = {
            "status": "abstain",
            "reason": result.reason.value,
            "message": result.message,
            "as_of_slot": result.as_of_slot,
        }
        exit_code = 1
    elif isinstance(result, QualifiedRunResult):
        payload, exit_code = _qualified_run_payload(result)
    elif isinstance(result, FinalizedBacktestDataset):
        summary = _dataset_summary(result)
        if not result.cases:
            payload = {
                "status": "abstain",
                "reason": AbstainReason.MISSING_FEATURE.value,
                "message": (
                    "finalized observations produced no executable copy-trade "
                    "cases; operator qualification, point-in-time entity evidence, "
                    "and completed outcome proofs are required"
                ),
                "as_of_slot": result.as_of_slot,
                "dataset": summary,
            }
            exit_code = 1
        else:
            payload = {
                "status": "dataset",
                "dataset": summary,
            }
            exit_code = 0
    elif isinstance(result, FinalizedBacktestResult):
        payload = {
            "status": "ok",
            "as_of_slot": result.dataset.as_of_slot,
            "result": _dataset_summary(result),
            "report": _jsonable(result.report),
        }
        exit_code = 0
    else:
        payload = {"status": "ok", "report": _jsonable(result)}
        exit_code = 0
    print(
        json.dumps(
            payload,
            indent=2 if args.pretty else None,
            sort_keys=True,
        )
    )
    return exit_code


def _qualified_run_payload(result: QualifiedRunResult) -> tuple[object, int]:
    """Render qualification metrics without treating an incomplete run as success."""

    qualification = _jsonable(result.qualification)
    if not result.is_qualified:
        return (
            {
                "status": "abstain",
                "reason": "operator_not_qualified",
                "message": result.qualification.message
                or "operator qualification did not produce a qualified OOS result",
                "as_of_slot": result.qualification.as_of_slot,
                "qualification": qualification,
            },
            1,
        )
    return (
        {
            "status": "ok",
            "as_of_slot": result.backtest.dataset.as_of_slot,
            "qualification": qualification,
            "result": _dataset_summary(result.backtest),
        },
        0,
    )


def _jsonable(value: object) -> object:
    if isinstance(value, Enum):
        return value.value
    if is_dataclass(value):
        return {
            field.name: _jsonable(getattr(value, field.name)) for field in fields(value)
        }
    if isinstance(value, tuple):
        return [_jsonable(item) for item in value]
    if isinstance(value, list):
        return [_jsonable(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    return value


def _dataset_summary(
    dataset: FinalizedBacktestDataset | FinalizedBacktestResult,
) -> object:
    """Return a JSON-safe summary without serializing raw UUID-bearing rows."""

    if isinstance(dataset, FinalizedBacktestResult):
        return {
            "as_of_slot": dataset.dataset.as_of_slot,
            "observation_count": len(dataset.dataset.observations),
            "launch_count": len(dataset.dataset.launches),
            "trade_count": len(dataset.dataset.trades),
            "case_count": len(dataset.dataset.cases),
            "result": _jsonable(dataset.report),
        }
    return {
        "as_of_slot": dataset.as_of_slot,
        "observation_count": len(dataset.observations),
        "launch_count": len(dataset.launches),
        "trade_count": len(dataset.trades),
        "case_count": len(dataset.cases),
        "evidence_ids": list(dataset.evidence_ids),
    }


if __name__ == "__main__":
    raise SystemExit(main())
