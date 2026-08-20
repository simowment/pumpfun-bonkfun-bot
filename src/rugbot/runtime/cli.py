"""Runnable finalized HTTP wallet watcher."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from time import sleep
from typing import TYPE_CHECKING

from rugbot.domain.decisions import AbstainReason, AbstainResult
from rugbot.execution.observe import ObserveExecutionPort
from rugbot.ingest.observation_pipeline import DurableObservationIngestor
from rugbot.ingest.pump_create_observation import (
    decode_pump_create_v2_observation,
)
from rugbot.ingest.pump_stream import PumpCreateStreamSource
from rugbot.runtime.config import (
    CoreSniperConfig,
    ExecutionMode,
    ListenerKind,
    SniperConfigError,
    SniperTarget,
    TargetKind,
    load_sniper_config,
    load_wallet_portfolio,
    resolve_config_path,
    resolve_dotenv,
    resolve_state_dir,
)
from rugbot.runtime.execution_factory import build_execution_port as _execution_port
from rugbot.runtime.observation_loop import (
    ObservationCycleReport,
    ObservationSource,
    RpcAddressObservationSource,
    SharedObservationLoop,
)
from rugbot.runtime.pump_market import PumpOnlineMarket
from rugbot.runtime.sniper_runtime import build_sniper_runtime
from rugbot.runtime.wallet_intelligence import (
    WalletIntelligenceReport,
    abstention_to_json,
    report_to_json,
    scan_wallet_intelligence,
)
from rugbot.runtime.watch import (
    ExecutionPortResolver,
    PositionEvidenceResolver,
    WatchSnipeCandidate,
    WatchSnipeHandler,
)
from rugbot.storage.jsonl_observation_store import JsonlObservationStore
from rugbot.storage.paper_position_store import (
    PaperPositionStoreError,
)
from rugbot.storage.sqlite_state_store import SqliteStateStore, SqliteStateStoreError

if TYPE_CHECKING:
    from collections.abc import Sequence

    from rugbot.decision.operator_qualification import (
        OperatorQualification,
        WalletEntityEvidence,
    )
    from rugbot.execution.ports import ExecutionPort, ExecutionReceipt
    from rugbot.ingest.rpc_observer import RpcHttpTransport

MAX_WATCH_TRANSACTIONS = 20


@dataclass(frozen=True, slots=True)
class WatchCycleResult:
    """One durable wallet polling result."""

    report: ObservationCycleReport
    candidates: tuple[WatchSnipeCandidate, ...]
    receipts: tuple[ExecutionReceipt, ...]


async def run_watch_cycle(  # noqa: PLR0913
    config: CoreSniperConfig,
    *,
    endpoint: str,
    state_dir: Path,
    max_transactions: int = MAX_WATCH_TRANSACTIONS,
    transport: RpcHttpTransport | None = None,
    execution_port: ExecutionPort | None = None,
    execution_port_resolver: ExecutionPortResolver | None = None,
    position_evidence_resolver: PositionEvidenceResolver | None = None,
    qualification: OperatorQualification | None = None,
    entity_evidence: tuple[WalletEntityEvidence, ...] | None = None,
    market: PumpOnlineMarket | None = None,
    source: ObservationSource | None = None,
) -> WatchCycleResult | AbstainResult:
    """Run one finalized wallet poll through the shared observation path.

    A caller must inject a deterministic paper port or per-candidate resolver
    built from exact finalized market state. Paper mode never creates a
    simulator-less fallback.
    """

    if (
        type(max_transactions) is not int
        or not 1 <= max_transactions <= MAX_WATCH_TRANSACTIONS
    ):
        return AbstainResult(
            reason=AbstainReason.MISSING_FEATURE,
            message="max_transactions must be an integer from 1 through 20",
            as_of_slot=-1,
        )
    if config.target.kind is not TargetKind.WALLET:
        return AbstainResult(
            reason=AbstainReason.MISSING_FEATURE,
            message="watch mode requires a wallet target",
            as_of_slot=-1,
        )
    if (
        config.execution.mode
        in (ExecutionMode.PAPER, ExecutionMode.SIMULATION, ExecutionMode.LIVE)
        and execution_port is None
        and execution_port_resolver is None
    ):
        return AbstainResult(
            reason=AbstainReason.MISSING_FEATURE,
            message=(
                "exact finalized paper execution context is required"
                if config.execution.mode is ExecutionMode.PAPER
                else "exact finalized route simulation context is required"
            ),
            as_of_slot=-1,
        )

    raw_path = state_dir / "observations.jsonl"
    try:
        state_store = SqliteStateStore(state_dir / "state.sqlite3")
    except SqliteStateStoreError:
        return AbstainResult(
            reason=AbstainReason.UNKNOWN_PROTOCOL_STATE,
            message="SQLite watcher state could not be opened",
            as_of_slot=-1,
        )
    try:
        observation_source = source or RpcAddressObservationSource(
            address=config.target.id,
            endpoint=endpoint,
            raw_observation_path=raw_path,
            handled_ledger=state_store,
            max_signatures=MAX_WATCH_TRANSACTIONS,
            max_transactions=max_transactions,
            transport=transport,
        )
        resolved_execution_port = execution_port_resolver or (
            market.execution_port
            if market is not None and config.execution.mode is ExecutionMode.PAPER
            else None
        )
        handler = WatchSnipeHandler(
            config=config,
            resolver=decode_pump_create_v2_observation,
            execution_port=(
                execution_port
                if execution_port is not None
                else _execution_port(config.execution.mode, endpoint)
            ),
            qualification=qualification,
            entity_evidence=entity_evidence,
            execution_port_resolver=resolved_execution_port,
            entry_evidence_resolver=(market.entry_evidence if market else None),
            position_evidence_resolver=(
                position_evidence_resolver
                or (
                    lambda observation, position: (
                        market.position_evidence(
                            observation,
                            position,
                            entry_quote_lamports=config.execution.quote_size_lamports,
                        )
                        if market is not None
                        else None
                    )
                )
            ),
            position_store=(
                state_store
                if config.execution.mode
                in (ExecutionMode.PAPER, ExecutionMode.SIMULATION, ExecutionMode.LIVE)
                else None
            ),
        )
        loop = SharedObservationLoop(
            DurableObservationIngestor(
                observation_store=JsonlObservationStore(raw_path),
                checkpoint_writer=state_store,
            ),
            state_store,
        )
        report = await loop.run_once(source=observation_source, handler=handler)
        if (
            report.abstention is None
            and market is not None
            and config.execution.mode
            in (ExecutionMode.PAPER, ExecutionMode.SIMULATION, ExecutionMode.LIVE)
        ):
            poll_slot = await market.finalized_slot()
            if isinstance(poll_slot, AbstainResult):
                report = replace(report, abstention=poll_slot)
            else:
                poll_error = await handler.poll_open_positions(
                    lambda position, as_of_slot: market.position_evidence_at_slot(
                        position,
                        as_of_slot=as_of_slot,
                        entry_quote_lamports=config.execution.quote_size_lamports,
                    ),
                    as_of_slot=poll_slot,
                )
                if poll_error is not None:
                    report = replace(report, abstention=poll_error)
        return WatchCycleResult(
            report=report,
            candidates=tuple(handler.candidates),
            receipts=tuple(handler.receipts),
        )
    except (PaperPositionStoreError, ValueError):
        return AbstainResult(
            reason=AbstainReason.UNKNOWN_PROTOCOL_STATE,
            message="paper position state is malformed",
            as_of_slot=-1,
        )
    finally:
        state_store.close()


async def run_wallet_intelligence_cycle(  # noqa: PLR0913
    wallet: str,
    *,
    endpoint: str,
    max_transactions: int = 50,
    max_history_pages: int = 10,
    max_linked_wallets: int = 8,
    max_hops: int = 3,
    as_of_slot: int | None = None,
    transport: RpcHttpTransport | None = None,
) -> WalletIntelligenceReport | AbstainResult:
    """Run the bounded finalized wallet/operator report without execution."""

    return await scan_wallet_intelligence(
        wallet,
        endpoint=endpoint,
        max_transactions=max_transactions,
        max_history_pages=max_history_pages,
        max_linked_wallets=max_linked_wallets,
        max_hops=max_hops,
        as_of_slot=as_of_slot,
        transport=transport,
    )


def build_arg_parser() -> argparse.ArgumentParser:
    """Build the wallet watcher command parser."""

    parser = argparse.ArgumentParser(
        description=(
            "Watch Pump.fun creator wallets using finalized HTTP evidence, "
            "optionally triggered by PumpPortal real-time events."
        )
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=Path("watch.yaml"),
    )
    parser.add_argument(
        "--state-dir",
        type=Path,
        default=Path(".state/watch"),
    )
    parser.add_argument(
        "--wallet",
        help="Solana creator wallet to watch; overrides target.id in the config",
    )
    parser.add_argument(
        "--portfolio",
        type=Path,
        help=("strict YAML wallet portfolio; each wallet gets isolated durable state"),
    )
    parser.add_argument(
        "--intelligence",
        action="store_true",
        help="print finalized linked-wallet and launch intelligence instead of watching",
    )
    parser.add_argument(
        "--mode",
        choices=(
            ExecutionMode.OBSERVE.value,
            ExecutionMode.PAPER.value,
            ExecutionMode.SIMULATION.value,
            ExecutionMode.LIVE.value,
        ),
        help="override execution.mode for this run",
    )
    parser.add_argument("--once", action="store_true")
    parser.add_argument(
        "--stream",
        action="store_true",
        help="use the persistent Pump.fun WebSocket trigger with finalized hydration",
    )
    parser.add_argument("--interval-seconds", type=int, default=2)
    parser.add_argument(
        "--max-transactions",
        type=int,
        default=MAX_WATCH_TRANSACTIONS,
        help="maximum finalized signatures hydrated per wallet (default: 20)",
    )
    parser.add_argument("--max-history-pages", type=int, default=10)
    parser.add_argument("--max-linked-wallets", type=int, default=8)
    parser.add_argument("--max-hops", type=int, default=3)
    parser.add_argument("--as-of-slot", type=int)
    parser.add_argument("--pretty", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:  # noqa: C901, PLR0911, PLR0912, PLR0915
    """Run the finalized wallet watcher."""

    args = build_arg_parser().parse_args(argv)
    resolve_dotenv()
    endpoint = os.environ.get("SOLANA_RPC_HTTP") or os.environ.get(
        "SOLANA_NODE_RPC_ENDPOINT"
    )
    if not endpoint:
        print(
            json.dumps(
                {
                    "status": "abstain",
                    "reason": AbstainReason.MISSING_FEATURE.value,
                    "message": (
                        "SOLANA_RPC_HTTP or SOLANA_NODE_RPC_ENDPOINT is required"
                    ),
                },
                sort_keys=True,
            )
        )
        return 1
    if args.wallet is not None and args.portfolio is not None:
        _print_json(
            {
                "status": "abstain",
                "reason": AbstainReason.MISSING_FEATURE.value,
                "message": "--wallet and --portfolio cannot be combined",
                "as_of_slot": -1,
            },
            pretty=args.pretty,
        )
        return 1
    if args.intelligence:
        try:
            portfolio = (
                load_wallet_portfolio(args.portfolio)
                if args.portfolio is not None
                else None
            )
        except SniperConfigError as error:
            _print_json(
                {
                    "status": "abstain",
                    "reason": AbstainReason.UNKNOWN_PROTOCOL_STATE.value,
                    "message": str(error),
                    "as_of_slot": -1,
                },
                pretty=args.pretty,
            )
            return 1
        intelligence_wallets = (
            portfolio.wallets
            if portfolio is not None
            else ((args.wallet,) if args.wallet else ())
        )
        if not intelligence_wallets:
            _print_json(
                {
                    "status": "abstain",
                    "reason": AbstainReason.MISSING_FEATURE.value,
                    "message": "--wallet or --portfolio is required with --intelligence",
                    "as_of_slot": -1,
                },
                pretty=args.pretty,
            )
            return 1
        intelligence_results: dict[str, dict[str, object]] = {}
        for wallet in intelligence_wallets:
            result = asyncio.run(
                run_wallet_intelligence_cycle(
                    wallet,
                    endpoint=endpoint,
                    max_transactions=args.max_transactions,
                    max_history_pages=args.max_history_pages,
                    max_linked_wallets=args.max_linked_wallets,
                    max_hops=args.max_hops,
                    as_of_slot=args.as_of_slot,
                )
            )
            intelligence_results[wallet] = (
                report_to_json(result)
                if isinstance(result, WalletIntelligenceReport)
                else abstention_to_json(result)
            )
        if portfolio is None:
            payload = intelligence_results[intelligence_wallets[0]]
        else:
            payload = _portfolio_json(intelligence_results)
        _print_json(payload, pretty=args.pretty)
        return (
            0
            if all(item["status"] == "ok" for item in intelligence_results.values())
            else 1
        )
    try:
        config_path = resolve_config_path(args.config)
        config = load_sniper_config(config_path)
    except SniperConfigError as error:
        print(
            json.dumps(
                {
                    "status": "abstain",
                    "reason": AbstainReason.UNKNOWN_PROTOCOL_STATE.value,
                    "message": str(error),
                },
                sort_keys=True,
            )
        )
        return 1
    portfolio = None
    if args.portfolio is not None:
        try:
            portfolio = load_wallet_portfolio(args.portfolio)
        except SniperConfigError as error:
            _print_json(
                {
                    "status": "abstain",
                    "reason": AbstainReason.UNKNOWN_PROTOCOL_STATE.value,
                    "message": str(error),
                    "as_of_slot": -1,
                },
                pretty=args.pretty,
            )
            return 1
    if args.wallet is not None:
        config = replace(
            config,
            target=SniperTarget(kind=TargetKind.WALLET, id=args.wallet),
        )
    if args.mode is not None:
        config = replace(
            config,
            execution=replace(config.execution, mode=ExecutionMode(args.mode)),
        )
    if (
        args.interval_seconds <= 0
        or not 1 <= args.max_transactions <= MAX_WATCH_TRANSACTIONS
    ):
        print(
            json.dumps(
                {
                    "status": "abstain",
                    "reason": AbstainReason.MISSING_FEATURE.value,
                    "message": (
                        "interval must be positive and max-transactions must be 1-20"
                    ),
                },
                sort_keys=True,
            )
        )
        return 1

    portfolio_watch = portfolio is not None
    if portfolio is not None:
        watch_wallets = portfolio.wallets
        wallet_configs = {
            wallet: replace(
                config,
                target=SniperTarget(kind=TargetKind.WALLET, id=wallet),
            )
            for wallet in watch_wallets
        }
    else:
        watch_wallets = (config.target.id,)
        wallet_configs = {watch_wallets[0]: config}

    state_dir = resolve_state_dir(args.state_dir)
    if config.execution.mode is ExecutionMode.LIVE:
        resolve_dotenv(include_signing=True)
    try:
        execution_port = _execution_port(
            config.execution.mode,
            endpoint,
            expected_signer_pubkey=config.execution.signer_pubkey,
            execution=config.execution,
            transaction_state_path=state_dir / "transactions.sqlite3",
        )
    except (OSError, ValueError) as error:
        _print_json(
            {
                "status": "abstain",
                "reason": AbstainReason.MISSING_FEATURE.value,
                "message": str(error),
                "as_of_slot": -1,
            },
            pretty=args.pretty,
        )
        return 1
    if args.stream or config.listener is ListenerKind.PUMPPORTAL:
        if portfolio is not None:
            _print_json(
                {
                    "status": "abstain",
                    "reason": AbstainReason.MISSING_FEATURE.value,
                    "message": "the pumpportal listener requires one wallet target",
                    "as_of_slot": -1,
                },
                pretty=args.pretty,
            )
            return 1
        return asyncio.run(
            _run_stream_watch(
                config=config,
                endpoint=endpoint,
                state_dir=state_dir,
                max_transactions=args.max_transactions,
                execution_port=execution_port,
                once=args.once,
                pretty=args.pretty,
            )
        )
    while True:
        results: dict[str, dict[str, object]] = {}
        for wallet, wallet_config in wallet_configs.items():
            wallet_state_dir = (
                state_dir / "wallets" / wallet if portfolio_watch else state_dir
            )
            result = asyncio.run(
                _run_watch_once(
                    config=wallet_config,
                    endpoint=endpoint,
                    state_dir=wallet_state_dir,
                    max_transactions=args.max_transactions,
                    execution_port=execution_port,
                )
            )
            results[wallet] = _json_result(result)
        payload = (
            _portfolio_json(results) if portfolio_watch else results[watch_wallets[0]]
        )
        _print_json(payload, pretty=args.pretty)
        if not portfolio_watch and payload["status"] == "abstain":
            return 1
        if args.once:
            return 0
        sleep(args.interval_seconds)


def _portfolio_json(results: dict[str, dict[str, object]]) -> dict[str, object]:
    """Serialize one polling result per wallet without merging their state."""

    abstain_count = sum(result["status"] == "abstain" for result in results.values())
    candidate_count = sum(
        len(result.get("candidates", [])) for result in results.values()
    )
    receipt_count = sum(len(result.get("receipts", [])) for result in results.values())
    return {
        "status": "ok" if abstain_count == 0 else "abstain",
        "wallet_count": len(results),
        "summary": {
            "abstain_count": abstain_count,
            "candidate_count": candidate_count,
            "receipt_count": receipt_count,
        },
        "wallets": results,
    }


async def _run_watch_once(
    *,
    config: CoreSniperConfig,
    endpoint: str,
    state_dir: Path,
    max_transactions: int,
    execution_port: ExecutionPort,
) -> WatchCycleResult | AbstainResult:
    """Run one watch cycle and close its read-only market client."""

    market = PumpOnlineMarket(endpoint)
    try:
        return await run_watch_cycle(
            config,
            endpoint=endpoint,
            state_dir=state_dir,
            max_transactions=max_transactions,
            execution_port=execution_port,
            market=market,
        )
    finally:
        await market.close()


async def _run_stream_watch(  # noqa: PLR0913
    *,
    config: CoreSniperConfig,
    endpoint: str,
    state_dir: Path,
    max_transactions: int,
    execution_port: ExecutionPort,
    once: bool,
    pretty: bool,
) -> int:
    """Run one-wallet stream-triggered watching on the shared handler path."""

    try:
        stream_state = SqliteStateStore(state_dir / "state.sqlite3")
    except SqliteStateStoreError:
        _print_json(
            {
                "status": "abstain",
                "reason": AbstainReason.UNKNOWN_PROTOCOL_STATE.value,
                "message": "SQLite stream state could not be opened",
                "as_of_slot": -1,
            },
            pretty=pretty,
        )
        return 1
    sniper_runtime = build_sniper_runtime(
        config=config,
        endpoint=endpoint,
        state_dir=state_dir,
        execution_port_override=execution_port,
    )
    if sniper_runtime is not None:
        await sniper_runtime.daemon.start()
    source = PumpCreateStreamSource(
        wallet=config.target.id,
        endpoint=endpoint,
        raw_observation_path=state_dir / "observations.jsonl",
        handled_ledger=stream_state,
        max_catchup_transactions=max_transactions,
        processed_handler=(
            sniper_runtime.handle_processed_create
            if sniper_runtime is not None
            else None
        ),
    )
    market = PumpOnlineMarket(endpoint)
    watch_config = (
        replace(
            config,
            execution=replace(config.execution, mode=ExecutionMode.OBSERVE),
        )
        if sniper_runtime is not None
        else config
    )
    watch_execution_port = (
        ObserveExecutionPort() if sniper_runtime is not None else execution_port
    )
    try:
        while True:
            result = await run_watch_cycle(
                watch_config,
                endpoint=endpoint,
                state_dir=state_dir,
                max_transactions=max_transactions,
                execution_port=watch_execution_port,
                market=market,
                source=source,
            )
            payload = _json_result(result)
            _print_json(payload, pretty=pretty)
            if isinstance(result, AbstainResult):
                return 1
            if once:
                return 0
    finally:
        await source.close()
        await market.close()
        if sniper_runtime is not None:
            await sniper_runtime.close()
        stream_state.close()


def _print_json(payload: dict[str, object], *, pretty: bool) -> None:
    print(json.dumps(payload, indent=2 if pretty else None, sort_keys=True))


def _json_result(result: WatchCycleResult | AbstainResult) -> dict[str, object]:
    if isinstance(result, AbstainResult):
        return {
            "status": "abstain",
            "reason": result.reason.value,
            "message": result.message,
            "as_of_slot": result.as_of_slot,
        }
    report = asdict(result.report)
    abstention = result.report.abstention
    report["abstention"] = (
        None
        if abstention is None
        else {
            "reason": abstention.reason.value,
            "message": abstention.message,
            "as_of_slot": abstention.as_of_slot,
        }
    )
    return {
        "status": "ok" if abstention is None else "abstain",
        "report": report,
        "candidates": [asdict(candidate) for candidate in result.candidates],
        "receipts": [
            {
                **asdict(receipt),
                "mode": receipt.mode.value,
            }
            for receipt in result.receipts
        ],
    }


if __name__ == "__main__":
    raise SystemExit(main())
