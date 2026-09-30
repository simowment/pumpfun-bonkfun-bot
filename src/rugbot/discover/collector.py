"""Headless rug_discover collect daemon: one finalized Pump program log stream.

Every successful Pump transaction arrives on one ``logsSubscribe``. Create
events become launches (their logs are stored so readers decode the create
event, reserves and Mayhem flag exactly as for a fetched transaction); every
trade of a launch is recorded for ``TRADE_RECORD_WINDOW_SECONDS`` after its
create. No per-launch RPC calls are made.

The Pump program executes in essentially every slot, so a notification slot
jumping past ``STREAM_GAP_SLOTS`` means the socket dropped events: the gap is
persisted so datasets can exclude launches whose trades may be incomplete.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import os
import signal
import time
from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING

from solders.pubkey import Pubkey

from rugbot.discover.store import (
    ensure_discover_schema,
    record_stream_gap,
    upsert_launch,
    upsert_trade,
)
from rugbot.domain.amounts import Slot
from rugbot.domain.decisions import AbstainResult
from rugbot.ingest.pump.create_decoder import PUMP_PROGRAM_ID
from rugbot.ingest.pump.create_event_decoder import (
    PumpCreateEvent,
    decode_pump_create_event_logs,
)
from rugbot.ingest.pump.trade_event_decoder import (
    decode_pump_trade_event,
    pump_trade_payloads,
)
from rugbot.integrations.rpc_access import resolve_websocket_endpoint
from rugbot.integrations.solana_logs_stream import (
    SolanaLogsStream,
    WalletLogNotification,
)
from rugbot.runtime.config import load_provider_settings, resolve_dotenv
from rugbot.storage.database import DatabaseManager
from rugbot.utils.logger import get_logger

if TYPE_CHECKING:
    from pathlib import Path

logger = get_logger(__name__)

HEARTBEAT_SECONDS = 60
STREAM_COMMITMENT = "finalized"
# Trades of each launch are recorded for this long after its create.
TRADE_RECORD_WINDOW_SECONDS = 2 * 3600
# More slots than this between two Pump notifications means lost events.
STREAM_GAP_SLOTS = 20
LAUNCH_SOURCE = "pump_logs"
# discover_trades side for a launch whose trade events are outside the modeled
# fee set (holder rewards); scans skip such launches instead of using a gap.
UNMODELED_SIDE = "unmodeled"
PUMP_TRADE_MINT_OFFSET = 8
PUBKEY_BYTES = 32


@dataclass(slots=True)
class CollectStats:
    notifications: int = 0
    launches: int = 0
    trades: int = 0
    gaps: int = 0
    errors: int = 0


def _record_create(
    db: DatabaseManager, notification: WalletLogNotification, create: PumpCreateEvent
) -> None:
    upsert_launch(
        db,
        mint=create.mint_pubkey,
        creator=create.creator_pubkey,
        created_signature=notification.signature,
        created_slot=notification.slot,
        symbol=create.symbol,
        name=create.name,
        created_at=dt.datetime.fromtimestamp(create.timestamp, tz=dt.UTC).isoformat(),
        bonding_curve=create.bonding_curve_pubkey,
        source=LAUNCH_SOURCE,
        raw_json=json.dumps({"meta": {"logMessages": list(notification.logs)}}),
    )


def _record_trades(
    db: DatabaseManager,
    notification: WalletLogNotification,
    tracked_until: dict[str, float],
    now: float,
) -> int:
    """Persist every Pump TradeEvent of a tracked mint; return how many."""
    recorded = 0
    for event_index, payload in pump_trade_payloads(notification.logs):
        mint = str(
            Pubkey.from_bytes(
                payload[PUMP_TRADE_MINT_OFFSET : PUMP_TRADE_MINT_OFFSET + PUBKEY_BYTES]
            )
        )
        if tracked_until.get(mint, 0.0) < now:
            continue
        event = decode_pump_trade_event(payload, notification.slot)
        if isinstance(event, AbstainResult):
            # Marks the launch as not replayable instead of leaving a gap.
            upsert_trade(
                db,
                mint=mint,
                signature=notification.signature,
                event_index=event_index,
                slot=notification.slot,
                side=UNMODELED_SIDE,
                quote_amount_base_units=0,
                raw_json=json.dumps({"reason": event.message}),
            )
            continue
        upsert_trade(
            db,
            mint=mint,
            signature=notification.signature,
            event_index=event_index,
            slot=notification.slot,
            side="buy" if event.is_buy else "sell",
            wallet=event.user,
            quote_amount_base_units=event.sol_amount_base_units,
            base_amount=event.token_amount_base_units,
            raw_json=json.dumps(
                {
                    "timestamp": event.timestamp,
                    "virtual_sol_reserves": event.virtual_sol_reserves_base_units,
                    "virtual_token_reserves": event.virtual_token_reserves_base_units,
                }
            ),
        )
        recorded += 1
    return recorded


def _process(
    db: DatabaseManager,
    notification: WalletLogNotification,
    tracked_until: dict[str, float],
    stats: CollectStats,
    last_slot: int | None,
) -> int:
    """Record one notification's create and trades; return the newest slot."""
    stats.notifications += 1
    if last_slot is not None and notification.slot - last_slot > STREAM_GAP_SLOTS:
        record_stream_gap(db, from_slot=last_slot, to_slot=notification.slot)
        stats.gaps += 1
        logger.warning("stream gap %d..%d", last_slot, notification.slot)
    now = time.monotonic()
    create = decode_pump_create_event_logs(
        notification.logs, as_of_slot=Slot(notification.slot)
    )
    if isinstance(create, PumpCreateEvent):
        _record_create(db, notification, create)
        tracked_until[create.mint_pubkey] = now + TRADE_RECORD_WINDOW_SECONDS
        stats.launches += 1
    stats.trades += _record_trades(db, notification, tracked_until, now)
    return max(last_slot or 0, notification.slot)


def _write_health(path: Path, status: str, stats: CollectStats) -> None:
    payload = {"status": status, **asdict(stats), "timestamp": int(time.time())}
    payload["pid"] = os.getpid()
    try:
        path.write_text(json.dumps(payload), encoding="utf-8")
    except OSError:
        logger.warning("could not write %s", path, exc_info=True)


async def run_collect(
    state_dir: Path,
    *,
    endpoint: str | None = None,
    duration_seconds: float | None = None,
) -> None:
    """Record every Pump launch and its trades from one finalized log stream."""

    resolve_dotenv()
    websocket = resolve_websocket_endpoint(
        endpoint or load_provider_settings().rpc_http
    )
    if websocket is None:
        raise ValueError("SOLANA_RPC_HTTP or SOLANA_RPC_WEBSOCKET is required")  # noqa: TRY003
    if duration_seconds is not None and duration_seconds <= 0:
        raise ValueError("duration_seconds must be positive")  # noqa: TRY003
    state_dir.mkdir(parents=True, exist_ok=True)
    db = DatabaseManager(state_dir / "rugbot.db")
    ensure_discover_schema(db)
    pid_path = state_dir / "rug_discover.pid"
    health_path = state_dir / "health.json"
    pid_path.write_text(str(os.getpid()), encoding="utf-8")

    stream = SolanaLogsStream(websocket, commitment=STREAM_COMMITMENT)
    await stream.reconcile([PUMP_PROGRAM_ID])
    stats = CollectStats()
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop_event.set)
    deadline = None if duration_seconds is None else time.monotonic() + duration_seconds
    tracked_until: dict[str, float] = {}
    last_slot: int | None = None
    next_heartbeat = time.monotonic() + HEARTBEAT_SECONDS
    logger.info("rug_discover collect started state_dir=%s", state_dir)
    try:
        while not stop_event.is_set():
            now = time.monotonic()
            if deadline is not None and now >= deadline:
                break
            if now >= next_heartbeat:
                tracked_until = {m: t for m, t in tracked_until.items() if t > now}
                _write_health(health_path, "ok", stats)
                logger.info("heartbeat %s tracked=%d", stats, len(tracked_until))
                next_heartbeat = now + HEARTBEAT_SECONDS
            try:
                notification = await asyncio.wait_for(
                    stream.next_notification(), timeout=HEARTBEAT_SECONDS
                )
            except TimeoutError:
                continue
            last_slot = _process(db, notification, tracked_until, stats, last_slot)
    finally:
        await stream.close()
        pid_path.unlink(missing_ok=True)
        _write_health(health_path, "stopped", stats)
        logger.info("rug_discover collect stopped %s", stats)
