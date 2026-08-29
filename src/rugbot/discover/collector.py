"""Headless rug_discover collect daemon."""

# ruff: noqa: C901, PLR0912, PLR0913, PLR0915, BLE001, TRY003, TC001, TC003

from __future__ import annotations

import asyncio
import datetime as dt
import json
import os
import signal
import time
from dataclasses import asdict, dataclass
from pathlib import Path

import base58
from sol_trade_sdk.solana.provider_pool import RpcHttpTransport, RpcProviderPool

from rugbot.backtest.trajectory.finalized_trade_builder import (
    decode_pump_trade_event_proofs,
)
from rugbot.discover.store import (
    append_observation,
    ensure_discover_schema,
    update_launch_metrics,
    upsert_launch,
    upsert_trade,
)
from rugbot.domain.decisions import AbstainReason, AbstainResult
from rugbot.domain.observations import RawChainObservation
from rugbot.ingest.pump.pump_create_observation import decode_pump_create_v2_observation
from rugbot.ingest.pump.pump_stream import PumpPortalLaunchStream
from rugbot.ingest.rpc_observer import observe_address, observe_finalized_transaction
from rugbot.runtime.config import load_provider_settings, resolve_dotenv
from rugbot.storage.database import DatabaseManager
from rugbot.utils.logger import get_logger

logger = get_logger(__name__)

HEARTBEAT_SECONDS = 60
POLL_TRADES_SECONDS = 30
RECONNECT_MIN_SECONDS = 1.0
RECONNECT_MAX_SECONDS = 30.0
STALE_RETRY_SECONDS = 0.5
STALE_TIMEOUT_SECONDS = 60
SEMAPHORE_LIMIT = 4
RPC_MINIMUM_INTERVAL_SECONDS = 0.25
TRADE_HISTORY_LIMIT = 10
TRADE_MONITOR_SECONDS = 15 * 60
# Trade polling cost: ~2 req per mint per POLL_TRADES_SECONDS while active.
# Gated behind RUGBOT_DISCOVER_TRADE_POLL_ENABLED to avoid idle RPC burn.
PENDING_FINALIZED_TRANSACTION_MESSAGE = (
    "getTransaction returned no complete finalized transaction"
)
INCONSISTENT_FINALIZED_SLOT_MESSAGE = (
    "getTransaction returned an inconsistent finalized slot"
)


@dataclass(slots=True)
class CollectStats:
    launches: int = 0
    trades: int = 0
    errors: int = 0


def _payload_result(observation: RawChainObservation) -> dict[str, object] | None:
    """Narrow the persisted JSON-RPC response to its result object."""

    if observation.raw_source_payload is None:
        return None
    payload = json.loads(observation.raw_source_payload)
    if not isinstance(payload, dict):
        return None
    result = payload.get("result")
    return result if isinstance(result, dict) else None


def _transaction_actors(
    observation: RawChainObservation,
) -> tuple[str | None, tuple[str, ...]]:
    """Return fee payer and required signers proven by the RPC transaction."""

    result = _payload_result(observation)
    transaction = result.get("transaction") if result is not None else None
    if not isinstance(transaction, dict):
        return None, ()
    message = transaction.get("message")
    if not isinstance(message, dict):
        return None, ()
    header = message.get("header")
    account_keys = message.get("accountKeys")
    if not isinstance(account_keys, list):
        return None, ()
    pubkeys: list[str] = []
    if isinstance(header, dict):
        required = header.get("numRequiredSignatures")
        if not isinstance(required, int) or required < 1:
            return None, ()
        for item in account_keys[:required]:
            if isinstance(item, str):
                pubkeys.append(item)
            elif isinstance(item, dict) and isinstance(item.get("pubkey"), str):
                pubkeys.append(item["pubkey"])
            else:
                return None, ()
    else:
        for item in account_keys:
            if (
                isinstance(item, dict)
                and item.get("signer") is True
                and isinstance(item.get("pubkey"), str)
            ):
                pubkeys.append(item["pubkey"])
    return (pubkeys[0] if pubkeys else None), tuple(pubkeys)


def _created_at(observation: RawChainObservation) -> str | None:
    """Return finalized block time as UTC ISO-8601 when the RPC supplied it."""

    result = _payload_result(observation)
    block_time = result.get("blockTime") if result is not None else None
    if not isinstance(block_time, int):
        return None
    return dt.datetime.fromtimestamp(block_time, tz=dt.UTC).isoformat()


def _trade_event_json(event: object) -> str:
    """Serialize decoded proof fields without duplicating binary event bytes."""

    fields = asdict(event)
    fields.pop("encoded_event", None)
    return json.dumps(fields, sort_keys=True)


async def _observe_finalized_with_retry(
    signature: str,
    *,
    endpoint: str,
    source_id: str,
    semaphore: asyncio.Semaphore,
    transport: RpcHttpTransport | None,
) -> object:
    start = time.monotonic()
    async with semaphore:
        while True:
            result = await observe_finalized_transaction(
                signature,
                expected_slot=None,
                endpoint=endpoint,
                source_id=source_id,
                observer_id="rug_discover",
                receive_sequence=1,
                transport=transport,
            )
            if not hasattr(result, "reason"):
                return result
            # AbstainResult
            is_pending_transaction = (
                getattr(result, "reason", None) is AbstainReason.MISSING_FEATURE
                and getattr(result, "message", None)
                == PENDING_FINALIZED_TRANSACTION_MESSAGE
            )
            is_provider_slot_lag = (
                getattr(result, "reason", None) is AbstainReason.UNKNOWN_PROTOCOL_STATE
                and getattr(result, "message", None)
                == INCONSISTENT_FINALIZED_SLOT_MESSAGE
            )
            if (
                getattr(result, "reason", None) is AbstainReason.STALE_STATE
                or is_pending_transaction
                or is_provider_slot_lag
            ):
                if time.monotonic() - start > STALE_TIMEOUT_SECONDS:
                    return result
                await asyncio.sleep(STALE_RETRY_SECONDS)
                continue
            return result


def _discover_trade_poll_enabled() -> bool:
    """Return True only when bonding-curve trade polling is explicitly enabled."""
    return os.environ.get("RUGBOT_DISCOVER_TRADE_POLL_ENABLED", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _discover_trade_poll_interval_seconds() -> float:
    """Resolve trade poll interval from env RUGBOT_DISCOVER_TRADE_POLL_SECONDS."""
    raw = os.environ.get("RUGBOT_DISCOVER_TRADE_POLL_SECONDS", "").strip()
    if not raw:
        return float(POLL_TRADES_SECONDS)
    try:
        value = float(raw)
    except ValueError as error:
        raise ValueError(
            "RUGBOT_DISCOVER_TRADE_POLL_SECONDS must be a number"
        ) from error
    if value < 5:
        raise ValueError("RUGBOT_DISCOVER_TRADE_POLL_SECONDS must be >= 5")
    return value


async def _poll_trades_for_mint(
    mint: str,
    bonding_curve: str,
    *,
    creator: str,
    quote_mint: str,
    quote_is_sol: bool,
    endpoint: str,
    db: DatabaseManager,
    state_dir: Path,
    stop_event: asyncio.Event,
    stats: CollectStats,
    use_jsonl: bool,
    transport: RpcHttpTransport | None,
    poll_semaphore: asyncio.Semaphore,
) -> None:
    if not _discover_trade_poll_enabled():
        return
    poll_interval = _discover_trade_poll_interval_seconds()
    started_at = time.monotonic()
    while (
        not stop_event.is_set()
        and time.monotonic() - started_at < TRADE_MONITOR_SECONDS
    ):
        try:
            async with poll_semaphore:
                result = await observe_address(
                    bonding_curve,
                    endpoint=endpoint,
                    source_id=f"discover:{mint}",
                    max_signatures=TRADE_HISTORY_LIMIT,
                    max_transactions=TRADE_HISTORY_LIMIT,
                    transport=transport,
                    standard_history_only=True,
                )
            if isinstance(result, AbstainResult):
                await asyncio.sleep(poll_interval)
                continue
            inserted = 0
            for observation in result:
                decoded_events = decode_pump_trade_event_proofs(observation)
                if isinstance(decoded_events, AbstainResult):
                    continue
                matching_events = tuple(
                    (event_index, event)
                    for event_index, event in decoded_events
                    if event.mint == mint
                )
                if not matching_events or observation.signature is None:
                    continue
                if use_jsonl:
                    append_observation(state_dir, observation, mint=mint)
                signature = base58.b58encode(observation.signature).decode("ascii")
                fee_payer, signers = _transaction_actors(observation)
                for event_index, event in matching_events:
                    quote_amount = (
                        event.quote_amount_base_units or event.sol_amount_base_units
                    )
                    quote_reserves = (
                        event.virtual_quote_reserves_base_units
                        or event.virtual_sol_reserves_base_units
                    )
                    price_ppm = None
                    if event.virtual_token_reserves_base_units > 0:
                        price_ppm = (
                            quote_reserves
                            * 1_000_000
                            // event.virtual_token_reserves_base_units
                        )
                    if upsert_trade(
                        db,
                        mint=mint,
                        signature=signature,
                        event_index=event_index,
                        slot=observation.slot,
                        side="buy" if event.is_buy else "sell",
                        tx_index=observation.transaction_index,
                        wallet=event.user,
                        quote_amount_base_units=quote_amount,
                        quote_mint=quote_mint,
                        base_amount=event.token_amount_base_units,
                        fee_payer=fee_payer,
                        signers_json=json.dumps(signers),
                        price_ppm=price_ppm,
                        raw_json=_trade_event_json(event),
                    ):
                        inserted += 1
                        if not event.is_buy and event.user == creator:
                            update_launch_metrics(
                                db,
                                mint,
                                dev_sell_slot=observation.slot,
                            )
            if inserted:
                stats.trades += inserted
                if quote_is_sol:
                    row = db.connection.execute(
                        "SELECT SUM(quote_amount_base_units) AS volume "
                        "FROM discover_trades WHERE mint = ?",
                        (mint,),
                    ).fetchone()
                    if row is not None and isinstance(row["volume"], int):
                        update_launch_metrics(
                            db,
                            mint,
                            volume_lamports=row["volume"],
                        )
        except Exception:
            stats.errors += 1
            logger.warning("poll trades error for %s", mint, exc_info=True)
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=poll_interval)
        except TimeoutError:
            continue


async def run_collect(
    state_dir: Path,
    *,
    use_jsonl: bool = False,
    endpoint: str | None = None,
    pumpportal_url: str | None = None,
    duration_seconds: float | None = None,
) -> None:
    """Daemon loop: PumpPortal -> finalized hydration -> SQLite/JSONL -> trade polling."""

    resolve_dotenv()
    providers = load_provider_settings()
    rpc_endpoint = endpoint or providers.rpc_http
    if not rpc_endpoint:
        raise ValueError("SOLANA_RPC_HTTP is required for rug_discover collect")
    if duration_seconds is not None and duration_seconds <= 0:
        raise ValueError("duration_seconds must be positive")

    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "observations").mkdir(parents=True, exist_ok=True)
    db = DatabaseManager(state_dir / "rugbot.db")
    ensure_discover_schema(db)

    pid_path = state_dir / "rug_discover.pid"
    health_path = state_dir / "health.json"
    try:
        pid_path.write_text(str(os.getpid()), encoding="utf-8")
    except OSError:
        logger.warning("could not write pid file", exc_info=True)

    stream = PumpPortalLaunchStream(
        websocket_endpoint=pumpportal_url or "wss://pumpportal.fun/api/data",
        api_key=providers.pumpportal_api_key,
    )
    semaphore = asyncio.Semaphore(SEMAPHORE_LIMIT)
    trade_poll_semaphore = asyncio.Semaphore(1)
    rpc_transport = (
        RpcProviderPool(
            (rpc_endpoint, *providers.rpc_http_fallbacks),
            minimum_interval_seconds=RPC_MINIMUM_INTERVAL_SECONDS,
        )
        if endpoint is None and providers.rpc_http_fallbacks
        else None
    )
    stats = CollectStats()
    stop_event = asyncio.Event()
    trade_tasks: dict[str, asyncio.Task[None]] = {}

    def _handle_signal(*_args: object) -> None:
        stop_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _handle_signal)
        except (NotImplementedError, ValueError, RuntimeError):
            pass

    last_heartbeat = time.monotonic()
    deadline = (
        time.monotonic() + duration_seconds if duration_seconds is not None else None
    )

    logger.info(
        "rug_discover collect started state_dir=%s endpoint=%s", state_dir, rpc_endpoint
    )

    try:
        while not stop_event.is_set():
            remaining = deadline - time.monotonic() if deadline is not None else None
            if remaining is not None and remaining <= 0:
                break
            # heartbeat
            if time.monotonic() - last_heartbeat >= HEARTBEAT_SECONDS:
                health = {
                    "status": "ok",
                    "launches": stats.launches,
                    "trades": stats.trades,
                    "errors": stats.errors,
                    "timestamp": int(time.time()),
                    "pid": os.getpid(),
                }
                try:
                    health_path.write_text(json.dumps(health), encoding="utf-8")
                except OSError:
                    pass
                logger.info(
                    "heartbeat launches=%d trades=%d errors=%d",
                    stats.launches,
                    stats.trades,
                    stats.errors,
                )
                last_heartbeat = time.monotonic()

            # next_global_notification already handles reconnect exponential 1->30s internally
            try:
                notification = await asyncio.wait_for(
                    stream.next_global_notification(),
                    timeout=min(HEARTBEAT_SECONDS, remaining)
                    if remaining is not None
                    else HEARTBEAT_SECONDS,
                )
            except TimeoutError:
                continue
            except Exception as exc:
                stats.errors += 1
                logger.warning("pumpportal stream error: %s", exc, exc_info=True)
                await asyncio.sleep(RECONNECT_MIN_SECONDS)
                continue

            # hydrate finalized
            hydration_remaining = (
                deadline - time.monotonic() if deadline is not None else None
            )
            if hydration_remaining is not None and hydration_remaining <= 0:
                break
            try:
                result = await asyncio.wait_for(
                    _observe_finalized_with_retry(
                        notification.signature,
                        endpoint=rpc_endpoint,
                        source_id="rug_discover",
                        semaphore=semaphore,
                        transport=rpc_transport,
                    ),
                    timeout=hydration_remaining,
                )
            except TimeoutError:
                break
            if result is None or hasattr(result, "reason"):
                stats.errors += 1
                logger.warning(
                    "finalized hydration abstained for %s: %s: %s",
                    notification.signature,
                    getattr(result, "reason", "unknown"),
                    getattr(result, "message", "no detail"),
                )
                continue

            obs = result  # RawChainObservation
            # persist observation
            if use_jsonl:
                try:
                    append_observation(
                        state_dir,
                        obs,  # type: ignore[arg-type]
                        mint=notification.mint_pubkey,
                    )
                except Exception:
                    logger.warning("jsonl append failed", exc_info=True)

            # decode create_v2
            try:
                decoded = decode_pump_create_v2_observation(obs)  # type: ignore[arg-type]
            except Exception as exc:
                logger.warning(
                    "decode failed for %s: %s",
                    notification.signature,
                    exc,
                    exc_info=True,
                )
                continue
            if decoded is None or hasattr(decoded, "reason"):
                # not a create_v2 or abstained
                continue

            mint = decoded.mint_pubkey  # type: ignore[union-attr]
            creator = (
                decoded.creator_pubkey
                if hasattr(decoded, "creator_pubkey")
                else notification.creator_pubkey
            )  # type: ignore[union-attr]
            bonding_curve = decoded.bonding_curve_pubkey  # type: ignore[union-attr]

            raw_json = None
            try:
                raw_json = (
                    obs.raw_source_payload.decode("utf-8")
                    if obs.raw_source_payload
                    else None
                )  # type: ignore[union-attr]
            except Exception:
                raw_json = None

            try:
                upsert_launch(
                    db,
                    mint=mint,
                    creator=creator,
                    created_signature=notification.signature,
                    created_slot=obs.slot,  # type: ignore[union-attr]
                    symbol=decoded.symbol,  # type: ignore[union-attr]
                    name=decoded.name,  # type: ignore[union-attr]
                    created_at=_created_at(obs),  # type: ignore[arg-type]
                    bonding_curve=bonding_curve,
                    source="pumpportal",
                    raw_json=raw_json,
                )
            except Exception as exc:
                logger.warning(
                    "upsert launch failed for %s: %s", mint, exc, exc_info=True
                )
                continue

            stats.launches += 1
            logger.info("launch %s creator %s slot %s", mint, creator, obs.slot)  # type: ignore[union-attr]

            # subscribe trades for bonding_curve if available (gated by env flag)
            if (
                bonding_curve
                and mint not in trade_tasks
                and _discover_trade_poll_enabled()
            ):
                task = asyncio.create_task(
                    _poll_trades_for_mint(
                        mint,
                        bonding_curve,
                        creator=creator,
                        quote_mint=decoded.quote_mint_pubkey,  # type: ignore[union-attr]
                        quote_is_sol=decoded.quote_asset == "SOL",  # type: ignore[union-attr]
                        endpoint=rpc_endpoint,
                        db=db,
                        state_dir=state_dir,
                        stop_event=stop_event,
                        stats=stats,
                        use_jsonl=use_jsonl,
                        transport=rpc_transport,
                        poll_semaphore=trade_poll_semaphore,
                    )
                )
                trade_tasks[mint] = task
                task.add_done_callback(
                    lambda _task, tracked_mint=mint: trade_tasks.pop(tracked_mint, None)
                )
    finally:
        stop_event.set()
        for task in list(trade_tasks.values()):
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        try:
            await stream.close()
        except Exception:
            logger.warning("could not close PumpPortal stream", exc_info=True)
        # cleanup pid
        try:
            if pid_path.exists():
                pid_path.unlink()
        except OSError:
            pass
        health = {
            "status": "stopped",
            "launches": stats.launches,
            "trades": stats.trades,
            "errors": stats.errors,
            "timestamp": int(time.time()),
            "pid": os.getpid(),
        }
        try:
            health_path.write_text(json.dumps(health), encoding="utf-8")
        except OSError:
            logger.warning("could not write final health", exc_info=True)
        logger.info("rug_discover collect stopped")
