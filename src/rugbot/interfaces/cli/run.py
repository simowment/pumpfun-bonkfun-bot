"""rug_run — paper-trade every enabled paper tracker from one Pump log stream.

Opens one ``logsSubscribe`` on the Pump program (confirmed commitment) and
feeds it to the paper desk. Funding-source trackers
(``funded_wallet_creations``) add one ``logsSubscribe`` per source: each of
its transactions is fetched, and every wallet it funds from a zero balance,
within the tracker's amount range and with no other history, is armed. Trackers are re-read every few seconds, so
``rug_tracker add/set/disable`` takes effect without a restart. Never signs or
submits a transaction. ``--report`` prints per-tracker results from the journal.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time
from pathlib import Path
from typing import TYPE_CHECKING

from rugbot.domain.amounts import LAMPORTS_PER_SOL
from rugbot.ingest.pump.create_decoder import PUMP_PROGRAM_ID
from rugbot.integrations.rpc_access import (
    RpcAccessError,
    resolve_websocket_endpoint,
    sync_rpc_result,
)
from rugbot.integrations.solana_logs_stream import SolanaLogsStream
from rugbot.runtime.config import (
    ExecutionMode,
    TrackingMode,
    load_provider_settings,
    resolve_dotenv,
    resolve_state_dir,
)
from rugbot.runtime.workers.paper_desk import (
    BUY,
    DEFAULT_LANDING_SLOTS,
    SELL,
    PaperDesk,
)
from rugbot.storage.config_store import ConfigStore
from rugbot.storage.paper_journal import PaperJournal
from rugbot.tracker.funding_chain import (
    FundingChainError,
    fresh_funded_wallets,
    has_earlier_history,
)

if TYPE_CHECKING:
    from rugbot.storage.config_store import Tracker

STREAM_COMMITMENT = "confirmed"
TICK_SECONDS = 0.5
TRACKER_RELOAD_SECONDS = 10.0
JOURNAL_FILENAME = "paper_journal.sqlite3"
POSITIONS_DIRNAME = "paper_positions"


def _paper_trackers(store: ConfigStore) -> list[Tracker]:
    return [
        tracker
        for tracker in store.list_trackers()
        if tracker.enabled and tracker.config.execution.mode is ExecutionMode.PAPER
    ]


def _funding_sources(trackers: list[Tracker]) -> dict[str, Tracker]:
    return {
        tracker.wallet: tracker
        for tracker in trackers
        if tracker.config.tracking_mode is TrackingMode.FUNDED_WALLET_CREATIONS
    }


def _funded_fresh_wallets(
    source: Tracker, signature: str
) -> list[tuple[str, int]] | str:
    """Fresh wallets ``source`` funded in range in one transaction, or an error."""
    funding = source.config.funding
    try:
        result = sync_rpc_result(
            "getTransaction",
            [
                signature,
                {
                    "encoding": "jsonParsed",
                    "maxSupportedTransactionVersion": 1,
                    "commitment": STREAM_COMMITMENT,
                },
            ],
        )
        if result is None:
            return f"{signature} not yet visible at {STREAM_COMMITMENT}"
        return [
            (wallet, lamports)
            for wallet, lamports in fresh_funded_wallets(result, source=source.wallet)
            if funding.min_lamports <= lamports <= funding.max_lamports
            and not has_earlier_history(wallet, funding_signature=signature)
        ]
    except (RpcAccessError, FundingChainError) as error:
        return f"{signature}: {error}"


async def _watch_funding(
    stream: SolanaLogsStream, sources: dict[str, Tracker], desk: PaperDesk
) -> None:
    """Arm every fresh wallet a watched funding source pays within range."""
    while True:
        notification = await stream.next_notification()
        source = sources.get(notification.wallet)
        if source is None:
            continue
        funded = await asyncio.to_thread(
            _funded_fresh_wallets, source, notification.signature
        )
        if isinstance(funded, str):
            print(f"funding watch: {funded}", flush=True)
            continue
        for wallet, lamports in funded:
            _say(desk.arm(source.wallet, wallet, lamports))


def _say(lines: list[str]) -> None:
    for line in lines:
        print(line, flush=True)


async def _run(state_dir: Path, *, landing_slots: int, seconds: float | None) -> int:
    resolve_dotenv()
    websocket = resolve_websocket_endpoint(load_provider_settings().rpc_http)
    if websocket is None:
        print("error: SOLANA_RPC_HTTP or SOLANA_RPC_WEBSOCKET is required")
        return 1
    store = ConfigStore(state_dir=state_dir)
    trackers = _paper_trackers(store)
    if not trackers:
        print("error: no enabled paper trackers (rug_tracker add ...)")
        return 1
    journal = PaperJournal(state_dir / JOURNAL_FILENAME)
    desk = PaperDesk(
        trackers,
        positions_dir=state_dir / POSITIONS_DIRNAME,
        journal=journal,
        landing_slots=landing_slots,
    )
    print(
        f"paper desk: {len(trackers)} trackers, landing +{landing_slots} slots, "
        f"{len(desk.open_positions)} open positions restored",
        flush=True,
    )
    stream = SolanaLogsStream(websocket, commitment=STREAM_COMMITMENT)
    await stream.reconcile([PUMP_PROGRAM_ID])
    sources = _funding_sources(trackers)
    funding_stream = SolanaLogsStream(websocket, commitment=STREAM_COMMITMENT)
    await funding_stream.reconcile(sources)
    if sources:
        print(f"watching {len(sources)} funding sources", flush=True)

    async def tick() -> None:
        next_reload = time.monotonic() + TRACKER_RELOAD_SECONDS
        while True:
            await asyncio.sleep(TICK_SECONDS)
            _say(desk.tick())
            if time.monotonic() >= next_reload:
                current = _paper_trackers(store)
                desk.set_trackers(current)
                sources.clear()
                sources.update(_funding_sources(current))
                await funding_stream.reconcile(sources)
                next_reload = time.monotonic() + TRACKER_RELOAD_SECONDS

    async def consume() -> None:
        while True:
            notification = await stream.next_notification()
            _say(desk.handle_logs(notification.slot, notification.logs))

    ticker = asyncio.create_task(tick())
    funding = asyncio.create_task(_watch_funding(funding_stream, sources, desk))
    try:
        await asyncio.wait_for(consume(), timeout=seconds)
    except TimeoutError:
        pass
    finally:
        ticker.cancel()
        funding.cancel()
        await funding_stream.close()
        await stream.close()
        desk.close()
        journal.close()
    return 0


def _report(state_dir: Path) -> str:
    journal = PaperJournal(state_dir / JOURNAL_FILENAME)
    fills = journal.fills()
    journal.close()
    rows = []
    for tracker in dict.fromkeys(fill.tracker for fill in fills):
        mine = [fill for fill in fills if fill.tracker == tracker]
        position_pnl: dict[str, int] = {}
        closed: list[int] = []
        for fill in mine:
            if fill.side == SELL and fill.pnl_lamports is not None:
                position_pnl[fill.mint] = (
                    position_pnl.get(fill.mint, 0) + fill.pnl_lamports
                )
                if fill.position_closed:
                    closed.append(position_pnl.pop(fill.mint))
        buys = sum(fill.side == BUY for fill in mine)
        wins = sum(pnl > 0 for pnl in closed)
        fees = sum(fill.curve_fee_lamports + fill.tx_cost_lamports for fill in mine)
        net = sum(closed)
        rows.append(
            f"{tracker}  closed {len(closed):3}  "
            f"win {100 * wins / len(closed) if closed else 0:5.1f}%  "
            f"net {net / LAMPORTS_PER_SOL:+.4f} SOL  "
            f"fees {fees / LAMPORTS_PER_SOL:.4f} SOL  "
            f"open {buys - len(closed)}"
        )
    return "\n".join(rows) or "no paper fills yet"


def main(argv: list[str] | None = None) -> int:
    """Run the paper desk, or print its report."""

    parser = argparse.ArgumentParser(prog="rug_run", description=__doc__)
    parser.add_argument("--state-dir", type=Path, default=None)
    parser.add_argument(
        "--landing-slots",
        type=int,
        default=DEFAULT_LANDING_SLOTS,
        help="slots between a decision and its fill (0 = same block)",
    )
    parser.add_argument("--seconds", type=float, help="stop after this long")
    parser.add_argument("--report", action="store_true", help="print results")
    args = parser.parse_args(argv)
    state_dir = resolve_state_dir(args.state_dir)
    if args.report:
        print(_report(state_dir))
        return 0
    try:
        return asyncio.run(
            _run(state_dir, landing_slots=args.landing_slots, seconds=args.seconds)
        )
    except KeyboardInterrupt:
        print("stopped", file=sys.stderr)
        return 0
