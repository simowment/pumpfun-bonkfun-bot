"""SQLite + JSONL persistence for rug_discover collect."""

# ruff: noqa: PLR0913, S608, TRY300, TC001

from __future__ import annotations

import datetime as dt
import json
import sqlite3
from pathlib import Path
from typing import TYPE_CHECKING

from rugbot.domain.observations import RawChainObservation
from rugbot.storage.database import DatabaseManager
from rugbot.storage.jsonl_observation_store import JsonlObservationStore

if TYPE_CHECKING:
    from rugbot.integrations.solscan import SolscanMintTransactionCandidate
    from rugbot.intelligence.entity_mint_index import FinalizedEntityMint


def ensure_discover_schema(db: DatabaseManager) -> None:
    """Create discover_launches and discover_trades tables if missing."""

    conn = db.connection
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS discover_launches (
            mint TEXT PRIMARY KEY,
            creator TEXT NOT NULL,
            symbol TEXT,
            name TEXT,
            created_signature TEXT NOT NULL,
            created_slot INTEGER NOT NULL,
            created_at TEXT,
            bonding_curve TEXT,
            source TEXT NOT NULL,
            bundle_json TEXT,
            mc_1s_lamports INTEGER,
            volume_lamports INTEGER,
            ath_quote_lamports INTEGER,
            dev_sell_slot INTEGER,
            bundler_sell_count INTEGER DEFAULT 0,
            dump_slot INTEGER,
            sweep_slot INTEGER,
            inactive_seconds INTEGER,
            enriched_at TEXT,
            creation_transaction_index INTEGER,
            raw_json TEXT
        )
        """
    )
    # Migration: add creation_transaction_index if table pre-existed without it
    try:
        cols = {
            row["name"]
            for row in conn.execute("PRAGMA table_info(discover_launches)").fetchall()
        }
        if "creation_transaction_index" not in cols:
            conn.execute(
                "ALTER TABLE discover_launches ADD COLUMN creation_transaction_index INTEGER"
            )
    except Exception:  # noqa: BLE001, S110
        pass
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS discover_trades (
            mint TEXT NOT NULL,
            signature TEXT NOT NULL,
            event_index INTEGER NOT NULL,
            slot INTEGER NOT NULL,
            tx_index INTEGER,
            wallet TEXT,
            side TEXT NOT NULL,
            quote_amount_base_units INTEGER NOT NULL,
            quote_mint TEXT,
            base_amount INTEGER,
            fee_payer TEXT,
            signers_json TEXT NOT NULL,
            price_ppm INTEGER,
            raw_json TEXT,
            PRIMARY KEY (signature, event_index)
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_discover_trades_mint ON discover_trades(mint)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_discover_trades_mint_slot ON discover_trades(mint, slot)"
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS discover_candidates (
            wallet TEXT PRIMARY KEY,
            first_seen_slot INTEGER,
            launch_count INTEGER NOT NULL DEFAULT 0,
            winrate REAL,
            best_tp INTEGER,
            updated_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS discover_dossier (
            wallet TEXT PRIMARY KEY,
            report_json TEXT NOT NULL,
            enriched_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS discover_wallet_basket_scans (
            wallet TEXT PRIMARY KEY,
            cursor TEXT,
            pages_scanned INTEGER NOT NULL,
            total_candidates INTEGER NOT NULL,
            complete INTEGER NOT NULL,
            warning TEXT,
            updated_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS discover_mint_transaction_candidates (
            wallet TEXT NOT NULL,
            signature TEXT NOT NULL,
            slot INTEGER NOT NULL,
            tx_index INTEGER,
            block_time INTEGER NOT NULL,
            matched_mints_json TEXT NOT NULL,
            confirmed INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (wallet, signature)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS discover_entity_mints (
            mint TEXT PRIMARY KEY,
            creator TEXT NOT NULL,
            name TEXT NOT NULL,
            symbol TEXT NOT NULL,
            created_timestamp INTEGER NOT NULL,
            creation_slot INTEGER NOT NULL,
            creation_signature TEXT NOT NULL,
            creation_transaction_index INTEGER,
            bonding_curve TEXT NOT NULL,
            relation TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS discover_wallet_launch_participation (
            wallet TEXT NOT NULL,
            mint TEXT NOT NULL,
            creation_slot INTEGER NOT NULL,
            window_end_slot INTEGER NOT NULL,
            transactions_cached INTEGER NOT NULL,
            buy_count INTEGER NOT NULL,
            sell_count INTEGER NOT NULL,
            first_buy_slot INTEGER,
            last_sell_slot INTEGER,
            buy_quote_lamports INTEGER NOT NULL,
            sell_quote_lamports INTEGER NOT NULL,
            complete INTEGER NOT NULL,
            warning TEXT,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (wallet, mint)
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_discover_launches_creator ON discover_launches(creator)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_discover_launches_created_at ON discover_launches(created_at)"
    )
    conn.commit()


def upsert_launch(
    db: DatabaseManager,
    *,
    mint: str,
    creator: str,
    created_signature: str,
    created_slot: int,
    symbol: str | None = None,
    name: str | None = None,
    created_at: str | None = None,
    bonding_curve: str | None = None,
    source: str = "pumpportal",
    bundle_json: str | None = None,
    raw_json: str | None = None,
) -> None:
    """Insert or replace a launch row inside an IMMEDIATE transaction."""

    conn = db.connection
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            """
            INSERT INTO discover_launches
                (mint, creator, symbol, name, created_signature, created_slot,
                 created_at, bonding_curve, source, bundle_json, raw_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(mint) DO UPDATE SET
                creator=excluded.creator,
                symbol=COALESCE(excluded.symbol, discover_launches.symbol),
                name=COALESCE(excluded.name, discover_launches.name),
                created_signature=excluded.created_signature,
                created_slot=excluded.created_slot,
                created_at=COALESCE(excluded.created_at, discover_launches.created_at),
                bonding_curve=COALESCE(excluded.bonding_curve, discover_launches.bonding_curve),
                source=excluded.source,
                bundle_json=COALESCE(excluded.bundle_json, discover_launches.bundle_json),
                raw_json=COALESCE(excluded.raw_json, discover_launches.raw_json)
            """,
            (
                mint,
                creator,
                symbol,
                name,
                created_signature,
                created_slot,
                created_at,
                bonding_curve,
                source,
                bundle_json,
                raw_json,
            ),
        )
        conn.execute("COMMIT")
    except sqlite3.Error:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise


def upsert_trade(
    db: DatabaseManager,
    *,
    mint: str,
    signature: str,
    event_index: int,
    slot: int,
    side: str,
    tx_index: int | None = None,
    wallet: str | None = None,
    quote_amount_base_units: int,
    quote_mint: str | None = None,
    base_amount: int | None = None,
    fee_payer: str | None = None,
    signers_json: str = "[]",
    price_ppm: int | None = None,
    raw_json: str | None = None,
) -> bool:
    """Insert trade row idempotently."""

    conn = db.connection
    try:
        conn.execute("BEGIN IMMEDIATE")
        cursor = conn.execute(
            """
            INSERT INTO discover_trades
                (mint, signature, event_index, slot, tx_index, wallet, side,
                 quote_amount_base_units, quote_mint, base_amount, fee_payer,
                 signers_json, price_ppm, raw_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(signature, event_index) DO NOTHING
            """,
            (
                mint,
                signature,
                event_index,
                slot,
                tx_index,
                wallet,
                side,
                quote_amount_base_units,
                quote_mint,
                base_amount,
                fee_payer,
                signers_json,
                price_ppm,
                raw_json,
            ),
        )
        conn.execute("COMMIT")
        return cursor.rowcount == 1
    except sqlite3.Error:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise


def update_launch_metrics(
    db: DatabaseManager,
    mint: str,
    *,
    mc_1s_lamports: int | None = None,
    volume_lamports: int | None = None,
    ath_quote_lamports: int | None = None,
    dev_sell_slot: int | None = None,
    bundler_sell_count: int | None = None,
    dump_slot: int | None = None,
    sweep_slot: int | None = None,
    inactive_seconds: int | None = None,
    bundle_json: str | None = None,
) -> None:
    """Patch computed metrics for a launch."""

    fields: list[str] = []
    values: list[object] = []
    mapping = {
        "mc_1s_lamports": mc_1s_lamports,
        "volume_lamports": volume_lamports,
        "ath_quote_lamports": ath_quote_lamports,
        "dev_sell_slot": dev_sell_slot,
        "bundler_sell_count": bundler_sell_count,
        "dump_slot": dump_slot,
        "sweep_slot": sweep_slot,
        "inactive_seconds": inactive_seconds,
        "bundle_json": bundle_json,
    }
    for key, value in mapping.items():
        if value is not None:
            fields.append(f"{key} = ?")
            values.append(value)
    if not fields:
        return
    values.append(mint)
    conn = db.connection
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            f"UPDATE discover_launches SET {', '.join(fields)} WHERE mint = ?",
            tuple(values),
        )
        conn.execute("COMMIT")
    except sqlite3.Error:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise


def jsonl_path_for_mint(state_dir: Path, mint: str) -> Path:
    """Return JSONL path for one mint."""

    return state_dir / "observations" / f"{mint}.jsonl"


def append_observation(
    state_dir: Path,
    observation: RawChainObservation,
    *,
    mint: str | None = None,
) -> bool:
    """Append observation to per-mint JSONL via JsonlObservationStore."""

    path = (
        jsonl_path_for_mint(state_dir, mint)
        if mint
        else state_dir / "observations" / "unknown.jsonl"
    )
    store = JsonlObservationStore(path)
    return store.append(observation)


def upsert_candidate(
    db: DatabaseManager,
    *,
    wallet: str,
    first_seen_slot: int | None = None,
    launch_count: int = 0,
    winrate: float | None = None,
    best_tp: int | None = None,
) -> None:
    """Insert or replace a candidate row."""

    updated_at = dt.datetime.now(dt.UTC).isoformat()
    conn = db.connection
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            """
            INSERT INTO discover_candidates
                (wallet, first_seen_slot, launch_count, winrate, best_tp, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(wallet) DO UPDATE SET
                first_seen_slot=COALESCE(excluded.first_seen_slot, discover_candidates.first_seen_slot),
                launch_count=excluded.launch_count,
                winrate=excluded.winrate,
                best_tp=excluded.best_tp,
                updated_at=excluded.updated_at
            """,
            (wallet, first_seen_slot, launch_count, winrate, best_tp, updated_at),
        )
        conn.execute("COMMIT")
    except sqlite3.Error:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise


def upsert_dossier(
    db: DatabaseManager,
    *,
    wallet: str,
    report_json: str,
) -> None:
    """Persist full dossier JSON for a wallet."""

    enriched_at = dt.datetime.now(dt.UTC).isoformat()
    conn = db.connection
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            """
            INSERT INTO discover_dossier (wallet, report_json, enriched_at)
            VALUES (?, ?, ?)
            ON CONFLICT(wallet) DO UPDATE SET
                report_json=excluded.report_json,
                enriched_at=excluded.enriched_at
            """,
            (wallet, report_json, enriched_at),
        )
        conn.execute("COMMIT")
    except sqlite3.Error:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise


def save_wallet_basket_scan(
    db: DatabaseManager,
    *,
    wallet: str,
    cursor: str | None,
    pages_scanned: int,
    total_candidates: int,
    complete: bool,
    warning: str | None,
) -> None:
    """Persist the resumable Solscan basket checkpoint for one wallet."""

    db.connection.execute(
        """
        INSERT INTO discover_wallet_basket_scans
            (wallet, cursor, pages_scanned, total_candidates, complete,
             warning, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(wallet) DO UPDATE SET
            cursor=excluded.cursor,
            pages_scanned=excluded.pages_scanned,
            total_candidates=excluded.total_candidates,
            complete=excluded.complete,
            warning=excluded.warning,
            updated_at=excluded.updated_at
        """,
        (
            wallet,
            cursor,
            pages_scanned,
            total_candidates,
            int(complete),
            warning,
            dt.datetime.now(dt.UTC).isoformat(),
        ),
    )
    db.connection.commit()


def fetch_wallet_basket_scan(
    db: DatabaseManager,
    wallet: str,
) -> dict[str, object] | None:
    """Return the persisted basket checkpoint for one wallet."""

    row = db.connection.execute(
        "SELECT * FROM discover_wallet_basket_scans WHERE wallet = ?",
        (wallet,),
    ).fetchone()
    return dict(row) if row is not None else None


def save_mint_transaction_candidates(
    db: DatabaseManager,
    *,
    wallet: str,
    candidates: tuple[SolscanMintTransactionCandidate, ...],
) -> None:
    """Persist indexed nominations separately from finalized trade evidence."""

    db.connection.executemany(
        """
        INSERT OR IGNORE INTO discover_mint_transaction_candidates
            (wallet, signature, slot, tx_index, block_time,
             matched_mints_json, confirmed)
        VALUES (?, ?, ?, ?, ?, ?, 0)
        """,
        [
            (
                wallet,
                candidate.signature,
                candidate.slot,
                candidate.transaction_index,
                candidate.block_time,
                json.dumps(candidate.matched_mints),
            )
            for candidate in candidates
        ],
    )
    db.connection.commit()


def save_entity_mints(
    db: DatabaseManager,
    mints: tuple[FinalizedEntityMint, ...],
) -> None:
    """Cache finalized entity-mint identities for later wallet-window scans."""

    updated_at = dt.datetime.now(dt.UTC).isoformat()
    db.connection.executemany(
        """
        INSERT INTO discover_entity_mints
            (mint, creator, name, symbol, created_timestamp, creation_slot,
             creation_signature, creation_transaction_index, bonding_curve,
             relation, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(mint) DO UPDATE SET
            creator=excluded.creator,
            name=excluded.name,
            symbol=excluded.symbol,
            created_timestamp=excluded.created_timestamp,
            creation_slot=excluded.creation_slot,
            creation_signature=excluded.creation_signature,
            creation_transaction_index=excluded.creation_transaction_index,
            bonding_curve=excluded.bonding_curve,
            relation=excluded.relation,
            updated_at=excluded.updated_at
        """,
        [
            (
                mint.mint,
                mint.creator,
                mint.name,
                mint.symbol,
                mint.created_timestamp,
                mint.creation_slot,
                mint.creation_signature,
                mint.creation_transaction_index,
                mint.bonding_curve,
                mint.relation,
                updated_at,
            )
            for mint in mints
        ],
    )
    db.connection.commit()


def fetch_entity_mint_windows(
    db: DatabaseManager,
    creator: str,
) -> tuple[tuple[str, int], ...]:
    """Return cached finalized mint and creation-slot pairs for one creator."""

    rows = db.connection.execute(
        "SELECT mint, creation_slot FROM discover_entity_mints "
        "WHERE creator = ? ORDER BY creation_slot",
        (creator,),
    ).fetchall()
    return tuple((str(row["mint"]), int(row["creation_slot"])) for row in rows)


def upsert_wallet_launch_participation(
    db: DatabaseManager,
    *,
    wallet: str,
    mint: str,
    creation_slot: int,
    window_end_slot: int,
    transactions_cached: int,
    buy_count: int,
    sell_count: int,
    first_buy_slot: int | None,
    last_sell_slot: int | None,
    buy_quote_lamports: int,
    sell_quote_lamports: int,
    complete: bool,
    warning: str | None,
) -> None:
    """Persist one finalized wallet participation window."""

    db.connection.execute(
        """
        INSERT INTO discover_wallet_launch_participation
            (wallet, mint, creation_slot, window_end_slot,
             transactions_cached, buy_count, sell_count, first_buy_slot,
             last_sell_slot, buy_quote_lamports, sell_quote_lamports,
             complete, warning, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(wallet, mint) DO UPDATE SET
            creation_slot=excluded.creation_slot,
            window_end_slot=excluded.window_end_slot,
            transactions_cached=excluded.transactions_cached,
            buy_count=excluded.buy_count,
            sell_count=excluded.sell_count,
            first_buy_slot=excluded.first_buy_slot,
            last_sell_slot=excluded.last_sell_slot,
            buy_quote_lamports=excluded.buy_quote_lamports,
            sell_quote_lamports=excluded.sell_quote_lamports,
            complete=excluded.complete,
            warning=excluded.warning,
            updated_at=excluded.updated_at
        """,
        (
            wallet,
            mint,
            creation_slot,
            window_end_slot,
            transactions_cached,
            buy_count,
            sell_count,
            first_buy_slot,
            last_sell_slot,
            buy_quote_lamports,
            sell_quote_lamports,
            int(complete),
            warning,
            dt.datetime.now(dt.UTC).isoformat(),
        ),
    )
    db.connection.commit()


def fetch_candidates(
    db: DatabaseManager,
    *,
    since_iso: str | None = None,
    limit: int = 50,
) -> list[dict[str, object]]:
    """Return candidates joined with recent launch stats; empty if none."""

    conn = db.connection
    rows = conn.execute(
        "SELECT * FROM discover_candidates ORDER BY updated_at DESC LIMIT ?", (limit,)
    ).fetchall()
    result: list[dict[str, object]] = []
    for r in rows:
        result.append(dict(r))
    # if no candidates yet but launches exist, synthesize from launches for presentation
    if not result and since_iso is not None:
        launches = conn.execute(
            "SELECT creator, COUNT(*) as cnt, MIN(created_slot) as first_slot FROM discover_launches WHERE (? IS NULL OR created_at >= ?) GROUP BY creator ORDER BY cnt DESC LIMIT ?",
            (since_iso, since_iso, limit),
        ).fetchall()
        for row in launches:
            result.append(
                {
                    "wallet": row["creator"],
                    "first_seen_slot": row["first_slot"],
                    "launch_count": row["cnt"],
                    "winrate": None,
                    "best_tp": None,
                    "updated_at": since_iso,
                }
            )
    return result


def fetch_dossier(
    db: DatabaseManager,
    wallet: str,
) -> dict[str, object] | None:
    """Return dossier row for a wallet."""

    conn = db.connection
    row = conn.execute(
        "SELECT * FROM discover_dossier WHERE wallet = ?", (wallet,)
    ).fetchone()
    if row is None:
        return None
    return dict(row)


def fetch_launches_for_wallet(
    db: DatabaseManager,
    wallet: str,
    limit: int = 100,
) -> list[dict[str, object]]:
    """Return launches for a creator wallet."""

    conn = db.connection
    rows = conn.execute(
        "SELECT * FROM discover_launches WHERE creator = ? ORDER BY created_at DESC LIMIT ?",
        (wallet, limit),
    ).fetchall()
    return [dict(r) for r in rows]


def fetch_trades_for_wallet_launches(
    db: DatabaseManager,
    wallet: str,
) -> list[dict[str, object]]:
    """Return trades for all launches of a wallet."""

    conn = db.connection
    rows = conn.execute(
        """
        SELECT t.* FROM discover_trades t
        JOIN discover_launches l ON l.mint = t.mint
        WHERE l.creator = ?
        ORDER BY t.slot ASC
        """,
        (wallet,),
    ).fetchall()
    return [dict(r) for r in rows]


def get_discover_state_dir(state_dir: Path | None = None) -> Path:
    """Return canonical discover state dir."""

    if state_dir is not None:
        return state_dir
    return Path(".state/discover")
