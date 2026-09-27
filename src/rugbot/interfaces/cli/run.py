"""rug_run — paper-trade every enabled paper tracker from one Pump log stream.

Opens one ``logsSubscribe`` on the Pump program (confirmed commitment) and
feeds it to the paper desk. Trackers are re-read every few seconds, so
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
from rugbot.integrations.rpc_access import resolve_websocket_endpoint
from rugbot.integrations.solana_logs_stream import SolanaLogsStream
from rugbot.runtime.config import (
    ExecutionMode,
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

    async def tick() -> None:
        next_reload = time.monotonic() + TRACKER_RELOAD_SECONDS
        while True:
            await asyncio.sleep(TICK_SECONDS)
            _say(desk.tick())
            if time.monotonic() >= next_reload:
                desk.set_trackers(_paper_trackers(store))
                next_reload = time.monotonic() + TRACKER_RELOAD_SECONDS

    async def consume() -> None:
        while True:
            notification = await stream.next_notification()
            _say(desk.handle_logs(notification.slot, notification.logs))

    ticker = asyncio.create_task(tick())
    try:
        await asyncio.wait_for(consume(), timeout=seconds)
    except TimeoutError:
        pass
    finally:
        ticker.cancel()
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
