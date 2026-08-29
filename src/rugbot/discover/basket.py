"""Resumable cross-token wallet basket discovery."""

# ruff: noqa: C901, TRY003

from __future__ import annotations

import asyncio
from pathlib import Path

from rugbot.backtest.trajectory.finalized_trade_builder import (
    PumpTradeEventProof,
    decode_pump_trade_event_proofs,
)
from rugbot.discover.store import (
    ensure_discover_schema,
    fetch_wallet_basket_scan,
    save_mint_transaction_candidates,
    save_wallet_basket_scan,
    upsert_wallet_launch_participation,
)
from rugbot.domain.decisions import AbstainResult
from rugbot.ingest.rpc_observer import (
    observe_address,
    supports_full_transaction_history,
)
from rugbot.integrations.solscan import SolscanClient
from rugbot.intelligence.token_resolver import PUMP_PROGRAM_ID
from rugbot.runtime.config import load_provider_settings, resolve_dotenv
from rugbot.storage.database import DatabaseManager
from rugbot.storage.jsonl_observation_store import JsonlObservationStore

MAX_LAUNCH_WINDOW_SLOTS = 300
MAX_LAUNCH_WINDOWS = 15
WINDOW_SCAN_CONCURRENCY = 2


def scan_wallet_basket(
    wallet: str,
    *,
    entity_mints: frozenset[str],
    state_dir: Path = Path(".state/discover"),
    max_pages: int = 5,
) -> dict[str, object]:
    """Advance one cached Solscan wallet basket scan by a bounded page count."""

    resolve_dotenv()
    providers = load_provider_settings()
    if providers.solscan_api_key is None:
        raise ValueError("SOLSCAN_API_KEY is required for wallet basket discovery")
    database = DatabaseManager(state_dir / "rugbot.db")
    ensure_discover_schema(database)
    checkpoint = fetch_wallet_basket_scan(database, wallet)
    if checkpoint is not None and int(checkpoint["complete"]) == 1:
        return _scan_report(wallet, checkpoint, status="complete")
    cursor = checkpoint["cursor"] if checkpoint is not None else None
    if cursor is not None and not isinstance(cursor, str):
        raise RuntimeError("persisted Solscan basket cursor is malformed")
    discovery = SolscanClient(providers.solscan_api_key).mint_transaction_candidates(
        wallet,
        program=str(PUMP_PROGRAM_ID),
        mints=entity_mints,
        max_pages=max_pages,
        cursor=cursor,
    )
    save_mint_transaction_candidates(
        database,
        wallet=wallet,
        candidates=discovery.candidates,
    )
    previous_pages = int(checkpoint["pages_scanned"]) if checkpoint is not None else 0
    total_candidates_row = database.connection.execute(
        "SELECT COUNT(*) AS count "
        "FROM discover_mint_transaction_candidates WHERE wallet = ?",
        (wallet,),
    ).fetchone()
    total_candidates = int(total_candidates_row["count"])
    cumulative_pages = previous_pages + discovery.pages_scanned
    save_wallet_basket_scan(
        database,
        wallet=wallet,
        cursor=discovery.next_cursor,
        pages_scanned=cumulative_pages,
        total_candidates=total_candidates,
        complete=discovery.complete,
        warning=discovery.warning,
    )
    return {
        "status": "complete" if discovery.complete else "partial",
        "wallet": wallet,
        "pages_scanned": cumulative_pages,
        "candidate_count": total_candidates,
        "new_candidate_count": len(discovery.candidates),
        "complete": discovery.complete,
        "warning": discovery.warning,
        "cursor": discovery.next_cursor,
    }


def _scan_report(
    wallet: str,
    checkpoint: dict[str, object],
    *,
    status: str,
) -> dict[str, object]:
    return {
        "status": status,
        "wallet": wallet,
        "pages_scanned": int(checkpoint["pages_scanned"]),
        "candidate_count": int(checkpoint["total_candidates"]),
        "new_candidate_count": 0,
        "complete": bool(checkpoint["complete"]),
        "warning": checkpoint["warning"],
        "cursor": checkpoint["cursor"],
    }


async def scan_wallet_launch_windows(
    wallet: str,
    *,
    launch_windows: tuple[tuple[str, int], ...],
    state_dir: Path = Path(".state/discover"),
    offset_slots: int = 120,
) -> dict[str, object]:
    """Decode one wallet's finalized trades after known entity launches."""

    if not launch_windows or len(launch_windows) > MAX_LAUNCH_WINDOWS:
        raise ValueError("launch window scan requires 1 to 15 finalized mints")
    if offset_slots < 1 or offset_slots > MAX_LAUNCH_WINDOW_SLOTS:
        raise ValueError("launch window offset must be between 1 and 300 slots")
    resolve_dotenv()
    providers = load_provider_settings()
    endpoints = tuple(
        endpoint
        for endpoint in (providers.rpc_http, *providers.rpc_http_fallbacks)
        if endpoint is not None and supports_full_transaction_history(endpoint)
    )
    if not endpoints:
        raise ValueError(
            "a Helius or Alchemy RPC endpoint is required for launch-window scans"
        )
    database = DatabaseManager(state_dir / "rugbot.db")
    ensure_discover_schema(database)
    semaphore = asyncio.Semaphore(WINDOW_SCAN_CONCURRENCY)

    async def scan(mint: str, creation_slot: int) -> dict[str, object]:
        window_end_slot = creation_slot + offset_slots
        stored = database.connection.execute(
            "SELECT * FROM discover_wallet_launch_participation "
            "WHERE wallet = ? AND mint = ? AND window_end_slot = ? "
            "AND complete = 1",
            (wallet, mint, window_end_slot),
        ).fetchone()
        if stored is not None:
            return dict(stored)
        cache = JsonlObservationStore(
            state_dir / "basket_windows" / f"{wallet}-{mint}.jsonl"
        )
        observations = cache.read_all()
        warning: str | None = None
        if not observations:
            async with semaphore:
                for endpoint in endpoints:
                    result = await observe_address(
                        wallet,
                        endpoint=endpoint,
                        source_id=f"basket-window:{wallet}:{mint}",
                        observer_id="rug_discover",
                        max_signatures=100,
                        max_transactions=1_000,
                        max_pages=10,
                        start_slot=creation_slot,
                        end_slot=window_end_slot,
                        observation_store=cache,
                    )
                    if isinstance(result, AbstainResult):
                        warning = result.message
                        continue
                    observations = list(result)
                    warning = None
                    break
        complete = bool(observations)
        buys: list[PumpTradeEventProof] = []
        sells: list[PumpTradeEventProof] = []
        buy_slots: list[int] = []
        sell_slots: list[int] = []
        for observation in observations:
            decoded = decode_pump_trade_event_proofs(observation)
            if isinstance(decoded, AbstainResult):
                continue
            for _, event in decoded:
                if event.mint != mint or event.user != wallet:
                    continue
                if event.is_buy:
                    buys.append(event)
                    buy_slots.append(observation.slot)
                else:
                    sells.append(event)
                    sell_slots.append(observation.slot)
        buy_quote_lamports = sum(
            event.quote_amount_base_units or event.sol_amount_base_units
            for event in buys
        )
        sell_quote_lamports = sum(
            event.quote_amount_base_units or event.sol_amount_base_units
            for event in sells
        )
        upsert_wallet_launch_participation(
            database,
            wallet=wallet,
            mint=mint,
            creation_slot=creation_slot,
            window_end_slot=window_end_slot,
            transactions_cached=len(observations),
            buy_count=len(buys),
            sell_count=len(sells),
            first_buy_slot=min(buy_slots, default=None),
            last_sell_slot=max(sell_slots, default=None),
            buy_quote_lamports=buy_quote_lamports,
            sell_quote_lamports=sell_quote_lamports,
            complete=complete,
            warning=warning,
        )
        return {
            "wallet": wallet,
            "mint": mint,
            "creation_slot": creation_slot,
            "window_end_slot": window_end_slot,
            "transactions_cached": len(observations),
            "buy_count": len(buys),
            "sell_count": len(sells),
            "first_buy_slot": min(buy_slots, default=None),
            "first_buy_offset_slots": (
                min(buy_slots) - creation_slot if buy_slots else None
            ),
            "last_sell_slot": max(sell_slots, default=None),
            "buy_quote_lamports": buy_quote_lamports,
            "sell_quote_lamports": sell_quote_lamports,
            "complete": complete,
            "warning": warning,
        }

    rows = await asyncio.gather(
        *(scan(mint, creation_slot) for mint, creation_slot in launch_windows)
    )
    participating = tuple(row for row in rows if int(row.get("buy_count", 0)) > 0)
    return {
        "wallet": wallet,
        "window_count": len(rows),
        "complete_window_count": sum(bool(row.get("complete")) for row in rows),
        "participating_token_count": len(participating),
        "rows": rows,
    }
