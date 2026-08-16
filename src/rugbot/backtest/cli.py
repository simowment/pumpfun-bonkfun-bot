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

from rugbot.backtest.dataset import (
    FinalizedBacktestDataset,
    FinalizedBacktestResult,
    build_finalized_dataset,
)
from rugbot.backtest.finalized_trade_builder import (
    build_finalized_trades_from_observations,
)
from rugbot.backtest.io import load_backtest_document
from rugbot.backtest.observation_trade_join import derive_finalized_trade_joins
from rugbot.backtest.online_pipeline import (
    FinalizedBacktestMetadata,
    run_finalized_backtest_pipeline,
)
from rugbot.backtest.qualified_run import QualifiedRunResult
from rugbot.backtest.rpc_case_acquisition import (
    acquire_finalized_rpc_case_observations,
)
from rugbot.domain.amounts import Slot
from rugbot.domain.decisions import AbstainReason, AbstainResult
from rugbot.storage.jsonl_observation_store import JsonlObservationStore

MAX_RPC_TRANSACTIONS = 1000

if TYPE_CHECKING:
    from collections.abc import Sequence

    from rugbot.backtest.evaluation import BacktestReport


def run_backtest_file(path: Path) -> BacktestReport | AbstainResult:
    """Validate one fixed-shape document and reject unqualified evaluation."""

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
    return AbstainResult(
        reason=AbstainReason.MISSING_FEATURE,
        message=(
            "fixed-shape backtest document lacks typed qualification evidence; "
            "completed outcomes and wallet entity evidence are required"
        ),
        as_of_slot=document.config.as_of_slot,
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


async def run_rpc_dataset(
    *,
    operator_wallet: str,
    endpoint: str,
    start_slot: Slot,
    end_slot: Slot,
    max_transactions: int,
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
    dataset = build_finalized_dataset(
        observations=bounded_observations,
        cases=(),
        trades=trades,
        as_of_slot=end_slot,
    )
    if isinstance(dataset, AbstainResult):
        return dataset
    return run_finalized_backtest_pipeline(
        observations=dataset.observations,
        metadata=FinalizedBacktestMetadata(
            as_of_slot=end_slot,
            trades=dataset.trades,
            cases=(),
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
            "status": "abstain",
            "reason": AbstainReason.MISSING_FEATURE.value,
            "message": (
                "finalized backtest result is missing operator qualification; "
                "typed completed outcomes and wallet entity evidence are required"
            ),
            "as_of_slot": result.dataset.as_of_slot,
            "result": _dataset_summary(result),
        }
        exit_code = 1
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
