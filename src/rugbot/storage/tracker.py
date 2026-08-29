"""SQLite implementation of the TrackerRepository protocol."""

from __future__ import annotations

import sqlite3
import time
from typing import TYPE_CHECKING, Any

from rugbot.tracker.models import (
    AlertOutboxRecord,
    BundleParticipationRecord,
    EntityBackfillRecord,
    EntityBackfillStatus,
    FunderRecord,
    LaunchRecord,
    TargetExecutionMode,
    TargetExecutionPolicy,
    TargetScanRecord,
    TransferRecord,
    WalletRecord,
    WalletStatus,
)

if TYPE_CHECKING:
    from rugbot.storage.database import DatabaseManager


class SQLiteTrackerRepository:
    """Concrete SQLite tracker repository storing funding trees, transfers, and launches."""

    def __init__(self, db: DatabaseManager) -> None:
        self._db = db
        self._create_schema()

    def _create_schema(self) -> None:
        conn = self._db.connection
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS tracker_funders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                address TEXT UNIQUE NOT NULL,
                label TEXT,
                enabled INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                last_seen_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS tracker_target_execution_policies (
                funder_address TEXT PRIMARY KEY NOT NULL,
                monitoring_enabled INTEGER NOT NULL,
                execution_mode TEXT NOT NULL,
                quote_size_lamports INTEGER NOT NULL,
                take_profit_pnl_ppm INTEGER NOT NULL,
                stop_loss_pnl_ppm INTEGER NOT NULL,
                max_slippage_bps INTEGER NOT NULL,
                priority_fee_microlamports INTEGER NOT NULL,
                jito_tip_lamports INTEGER NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY (funder_address) REFERENCES tracker_funders(address)
            );

            CREATE TABLE IF NOT EXISTS tracker_target_scans (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                query TEXT NOT NULL,
                tracking_address TEXT,
                token_symbol TEXT,
                token_name TEXT,
                scan_ok INTEGER NOT NULL,
                launch_count INTEGER NOT NULL,
                linked_launch_count INTEGER NOT NULL,
                repeat_bundler_mint_count INTEGER NOT NULL,
                message TEXT NOT NULL,
                first_scanned_at TEXT NOT NULL,
                last_scanned_at TEXT NOT NULL,
                scan_count INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS tracker_entity_backfills (
                query TEXT PRIMARY KEY,
                wallet TEXT NOT NULL,
                requested_transactions INTEGER NOT NULL,
                cached_transactions INTEGER NOT NULL,
                before_signature TEXT,
                status TEXT NOT NULL,
                message TEXT NOT NULL,
                report_json TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_tracker_entity_backfills_status
                ON tracker_entity_backfills (status, updated_at);

            CREATE TABLE IF NOT EXISTS tracker_wallets (
                address TEXT PRIMARY KEY,
                root_funder TEXT NOT NULL,
                parent_wallet TEXT,
                depth INTEGER NOT NULL,
                status TEXT NOT NULL,
                discovered_at TEXT NOT NULL,
                expires_at TEXT,
                last_active_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_tracker_wallets_root ON tracker_wallets (root_funder);
            CREATE INDEX IF NOT EXISTS idx_tracker_wallets_status ON tracker_wallets (status);

            CREATE TABLE IF NOT EXISTS tracker_transfers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                signature TEXT NOT NULL,
                instruction_index INTEGER NOT NULL,
                slot INTEGER NOT NULL,
                timestamp INTEGER NOT NULL,
                from_wallet TEXT NOT NULL,
                to_wallet TEXT NOT NULL,
                amount_lamports INTEGER NOT NULL,
                amount_sol REAL NOT NULL,
                root_funder TEXT NOT NULL,
                depth INTEGER NOT NULL,
                UNIQUE(signature, instruction_index)
            );
            CREATE INDEX IF NOT EXISTS idx_tracker_transfers_from ON tracker_transfers (from_wallet);
            CREATE INDEX IF NOT EXISTS idx_tracker_transfers_to ON tracker_transfers (to_wallet);

            CREATE TABLE IF NOT EXISTS tracker_launches (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                mint TEXT UNIQUE NOT NULL,
                creator_wallet TEXT NOT NULL,
                root_funder TEXT NOT NULL,
                symbol TEXT NOT NULL,
                name TEXT NOT NULL,
                created_signature TEXT NOT NULL,
                created_slot INTEGER NOT NULL,
                created_at INTEGER NOT NULL,
                depth INTEGER NOT NULL,
                funding_signature TEXT,
                funding_amount_lamports INTEGER,
                funding_timestamp INTEGER
            );
            CREATE INDEX IF NOT EXISTS idx_tracker_launches_creator ON tracker_launches (creator_wallet);
            CREATE INDEX IF NOT EXISTS idx_tracker_launches_root ON tracker_launches (root_funder);

            CREATE TABLE IF NOT EXISTS tracker_alert_outbox (
                mint TEXT NOT NULL,
                consumer TEXT NOT NULL,
                delivered INTEGER NOT NULL DEFAULT 0,
                created_at INTEGER NOT NULL,
                PRIMARY KEY (mint, consumer)
            );
            CREATE INDEX IF NOT EXISTS idx_tracker_alert_outbox_consumer
                ON tracker_alert_outbox (consumer, delivered);

            CREATE TABLE IF NOT EXISTS tracker_launch_activation (
                address TEXT PRIMARY KEY,
                activation_slot INTEGER NOT NULL,
                updated_at INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS tracker_bundle_participations (
                bundler_wallet TEXT NOT NULL,
                mint TEXT NOT NULL,
                creator TEXT NOT NULL,
                creation_slot INTEGER NOT NULL,
                buy_signature TEXT NOT NULL,
                transaction_index INTEGER,
                max_sol_cost_lamports INTEGER NOT NULL,
                PRIMARY KEY (bundler_wallet, mint, buy_signature)
            );
            CREATE INDEX IF NOT EXISTS idx_tracker_bundle_participations_wallet
                ON tracker_bundle_participations (bundler_wallet, creator);
            """
        )
        columns = {
            str(row["name"])
            for row in conn.execute("PRAGMA table_info(tracker_target_scans)")
        }
        if columns and ("id" not in columns or "tracking_eligible" in columns):
            conn.executescript(
                """
                BEGIN IMMEDIATE;
                ALTER TABLE tracker_target_scans RENAME TO tracker_target_scans_legacy;
                DROP INDEX IF EXISTS idx_tracker_target_scans_last;
                CREATE TABLE tracker_target_scans (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    query TEXT NOT NULL,
                    tracking_address TEXT,
                    token_symbol TEXT,
                    token_name TEXT,
                    scan_ok INTEGER NOT NULL,
                    launch_count INTEGER NOT NULL,
                    linked_launch_count INTEGER NOT NULL,
                    repeat_bundler_mint_count INTEGER NOT NULL,
                    message TEXT NOT NULL,
                    first_scanned_at TEXT NOT NULL,
                    last_scanned_at TEXT NOT NULL,
                    scan_count INTEGER NOT NULL
                );
                INSERT INTO tracker_target_scans (
                    query, tracking_address, token_symbol, token_name, scan_ok,
                    launch_count, linked_launch_count, repeat_bundler_mint_count,
                    message, first_scanned_at, last_scanned_at, scan_count
                )
                SELECT
                    query, tracking_address, token_symbol, token_name, scan_ok,
                    launch_count, linked_launch_count, repeat_bundler_mint_count,
                    message, first_scanned_at, last_scanned_at, scan_count
                FROM tracker_target_scans_legacy;
                DROP TABLE tracker_target_scans_legacy;
                CREATE INDEX idx_tracker_target_scans_last
                    ON tracker_target_scans (last_scanned_at DESC, id DESC);
                CREATE INDEX idx_tracker_target_scans_entity
                    ON tracker_target_scans (tracking_address, last_scanned_at DESC, id DESC);
                COMMIT;
                """
            )
        if (
            columns
            or conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'tracker_target_scans'"
            ).fetchone()
        ):
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_tracker_target_scans_last "
                "ON tracker_target_scans (last_scanned_at DESC, id DESC)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_tracker_target_scans_entity "
                "ON tracker_target_scans (tracking_address, last_scanned_at DESC, id DESC)"
            )

    # --- Funders ---

    def save_funder(self, funder: FunderRecord) -> None:
        """Insert or update a root funder."""
        conn = self._db.connection
        conn.execute(
            """
            INSERT INTO tracker_funders (address, label, enabled, created_at, last_seen_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(address) DO UPDATE SET
                label = excluded.label,
                enabled = excluded.enabled,
                last_seen_at = excluded.last_seen_at
            """,
            (
                funder.address,
                funder.label,
                1 if funder.enabled else 0,
                funder.created_at,
                funder.last_seen_at,
            ),
        )

    def get_funders(self, *, enabled_only: bool = False) -> tuple[FunderRecord, ...]:
        """Fetch all registered root funders."""
        query = "SELECT * FROM tracker_funders"
        params: tuple[Any, ...] = ()
        if enabled_only:
            query += " WHERE enabled = 1"
        query += " ORDER BY created_at DESC"
        conn = self._db.connection
        cursor = conn.execute(query, params)
        return tuple(
            FunderRecord(
                id=row["id"],
                address=row["address"],
                label=row["label"],
                enabled=bool(row["enabled"]),
                created_at=row["created_at"],
                last_seen_at=row["last_seen_at"],
            )
            for row in cursor.fetchall()
        )

    def get_funder(self, address: str) -> FunderRecord | None:
        """Fetch one root funder by address."""
        conn = self._db.connection
        cursor = conn.execute(
            "SELECT * FROM tracker_funders WHERE address = ?", (address,)
        )
        row = cursor.fetchone()
        if row is None:
            return None
        return FunderRecord(
            id=row["id"],
            address=row["address"],
            label=row["label"],
            enabled=bool(row["enabled"]),
            created_at=row["created_at"],
            last_seen_at=row["last_seen_at"],
        )

    def delete_funder(self, address: str) -> None:
        """Delete a root funder, policy, and associated links."""
        conn = self._db.connection
        conn.execute(
            "DELETE FROM tracker_target_execution_policies WHERE funder_address = ?",
            (address,),
        )
        conn.execute("DELETE FROM tracker_launches WHERE root_funder = ?", (address,))
        conn.execute("DELETE FROM tracker_transfers WHERE root_funder = ?", (address,))
        conn.execute("DELETE FROM tracker_wallets WHERE root_funder = ?", (address,))
        conn.execute("DELETE FROM tracker_funders WHERE address = ?", (address,))

    def clear_all_funders(self) -> None:
        """Delete all root funders, target policies, and tracked nodes."""
        conn = self._db.connection
        conn.execute("DELETE FROM tracker_target_execution_policies")
        conn.execute("DELETE FROM tracker_launches")
        conn.execute("DELETE FROM tracker_transfers")
        conn.execute("DELETE FROM tracker_wallets")
        conn.execute("DELETE FROM tracker_funders")

    def enable_funder(self, address: str, *, enabled: bool) -> None:
        """Enable or disable tracking for a root funder."""
        conn = self._db.connection
        conn.execute(
            "UPDATE tracker_funders SET enabled = ? WHERE address = ?",
            (1 if enabled else 0, address),
        )

    def save_target_execution_policy(self, policy: TargetExecutionPolicy) -> None:
        """Insert or replace the execution policy for an existing funder."""
        self._db.connection.execute(
            """
            INSERT INTO tracker_target_execution_policies (
                funder_address, monitoring_enabled, execution_mode,
                quote_size_lamports, take_profit_pnl_ppm, stop_loss_pnl_ppm,
                max_slippage_bps, priority_fee_microlamports,
                jito_tip_lamports, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(funder_address) DO UPDATE SET
                monitoring_enabled = excluded.monitoring_enabled,
                execution_mode = excluded.execution_mode,
                quote_size_lamports = excluded.quote_size_lamports,
                take_profit_pnl_ppm = excluded.take_profit_pnl_ppm,
                stop_loss_pnl_ppm = excluded.stop_loss_pnl_ppm,
                max_slippage_bps = excluded.max_slippage_bps,
                priority_fee_microlamports = excluded.priority_fee_microlamports,
                jito_tip_lamports = excluded.jito_tip_lamports,
                updated_at = excluded.updated_at
            """,
            (
                policy.funder_address,
                1 if policy.monitoring_enabled else 0,
                policy.execution_mode.value,
                policy.quote_size_lamports,
                policy.take_profit_pnl_ppm,
                policy.stop_loss_pnl_ppm,
                policy.max_slippage_bps,
                policy.priority_fee_microlamports,
                policy.jito_tip_lamports,
                policy.updated_at,
            ),
        )

    def get_target_execution_policy(
        self, funder_address: str
    ) -> TargetExecutionPolicy | None:
        """Fetch the persisted policy for one tracked funder."""
        row = self._db.connection.execute(
            "SELECT * FROM tracker_target_execution_policies WHERE funder_address = ?",
            (funder_address,),
        ).fetchone()
        if row is None:
            return None
        return TargetExecutionPolicy(
            funder_address=row["funder_address"],
            monitoring_enabled=bool(row["monitoring_enabled"]),
            execution_mode=TargetExecutionMode(row["execution_mode"]),
            quote_size_lamports=row["quote_size_lamports"],
            take_profit_pnl_ppm=row["take_profit_pnl_ppm"],
            stop_loss_pnl_ppm=row["stop_loss_pnl_ppm"],
            max_slippage_bps=row["max_slippage_bps"],
            priority_fee_microlamports=row["priority_fee_microlamports"],
            jito_tip_lamports=row["jito_tip_lamports"],
            updated_at=row["updated_at"],
        )

    def save_target_scan(self, scan: TargetScanRecord) -> TargetScanRecord:
        """Append one persistent target scan event."""
        conn = self._db.connection
        occurrence = conn.execute(
            "SELECT COALESCE(MAX(scan_count), 0) + 1 FROM tracker_target_scans WHERE query = ?",
            (scan.query,),
        ).fetchone()[0]
        cursor = conn.execute(
            """
            INSERT INTO tracker_target_scans (
                query, tracking_address, token_symbol, token_name, scan_ok,
                launch_count, linked_launch_count, repeat_bundler_mint_count,
                message, first_scanned_at, last_scanned_at, scan_count
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                scan.query,
                scan.tracking_address,
                scan.token_symbol,
                scan.token_name,
                1 if scan.scan_ok else 0,
                scan.launch_count,
                scan.linked_launch_count,
                scan.repeat_bundler_mint_count,
                scan.message,
                scan.first_scanned_at,
                scan.last_scanned_at,
                occurrence,
            ),
        )
        return TargetScanRecord(
            query=scan.query,
            tracking_address=scan.tracking_address,
            token_symbol=scan.token_symbol,
            token_name=scan.token_name,
            scan_ok=scan.scan_ok,
            launch_count=scan.launch_count,
            linked_launch_count=scan.linked_launch_count,
            repeat_bundler_mint_count=scan.repeat_bundler_mint_count,
            message=scan.message,
            first_scanned_at=scan.first_scanned_at,
            last_scanned_at=scan.last_scanned_at,
            scan_count=occurrence,
            id=int(cursor.lastrowid),
        )

    @staticmethod
    def _target_scan_from_row(row: sqlite3.Row) -> TargetScanRecord:
        """Decode one target scan event row."""
        return TargetScanRecord(
            query=row["query"],
            tracking_address=row["tracking_address"],
            token_symbol=row["token_symbol"],
            token_name=row["token_name"],
            scan_ok=bool(row["scan_ok"]),
            launch_count=row["launch_count"],
            linked_launch_count=row["linked_launch_count"],
            repeat_bundler_mint_count=row["repeat_bundler_mint_count"],
            message=row["message"],
            first_scanned_at=row["first_scanned_at"],
            last_scanned_at=row["last_scanned_at"],
            scan_count=row["scan_count"],
            id=row["id"],
        )

    def get_target_scans(self, limit: int = 100) -> tuple[TargetScanRecord, ...]:
        """Fetch persistent scan events, newest first."""
        rows = self._db.connection.execute(
            """
            SELECT * FROM tracker_target_scans
            ORDER BY last_scanned_at DESC, id DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        return tuple(self._target_scan_from_row(row) for row in rows)

    def get_target_scans_for_entity(
        self, entity_address: str, limit: int = 100
    ) -> tuple[TargetScanRecord, ...]:
        """Fetch persistent scan events for one resolved entity, newest first."""
        rows = self._db.connection.execute(
            """
            SELECT * FROM tracker_target_scans
            WHERE tracking_address = ?
            ORDER BY last_scanned_at DESC, id DESC
            LIMIT ?
            """,
            (entity_address, limit),
        ).fetchall()
        return tuple(self._target_scan_from_row(row) for row in rows)

    def save_entity_backfill(self, backfill: EntityBackfillRecord) -> None:
        """Insert or update one durable entity-history backfill."""

        self._db.connection.execute(
            """
            INSERT INTO tracker_entity_backfills (
                query, wallet, requested_transactions, cached_transactions,
                before_signature, status, message, report_json,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(query) DO UPDATE SET
                wallet = excluded.wallet,
                requested_transactions = excluded.requested_transactions,
                cached_transactions = excluded.cached_transactions,
                before_signature = excluded.before_signature,
                status = excluded.status,
                message = excluded.message,
                report_json = excluded.report_json,
                updated_at = excluded.updated_at
            """,
            (
                backfill.query,
                backfill.wallet,
                backfill.requested_transactions,
                backfill.cached_transactions,
                backfill.before_signature,
                backfill.status.value,
                backfill.message,
                backfill.report_json,
                backfill.created_at,
                backfill.updated_at,
            ),
        )

    def save_bundle_participations(
        self, participations: tuple[BundleParticipationRecord, ...]
    ) -> None:
        """Persist finalized creation-slot buys, ignoring duplicates."""

        self._db.connection.executemany(
            """
            INSERT OR IGNORE INTO tracker_bundle_participations (
                bundler_wallet, mint, creator, creation_slot,
                buy_signature, transaction_index, max_sol_cost_lamports
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    item.bundler_wallet,
                    item.mint,
                    item.creator,
                    item.creation_slot,
                    item.buy_signature,
                    item.transaction_index,
                    item.max_sol_cost_lamports,
                )
                for item in participations
            ],
        )

    def get_bundle_participations(
        self,
        bundler_wallets: tuple[str, ...],
        *,
        exclude_creator: str,
    ) -> tuple[BundleParticipationRecord, ...]:
        """Fetch participations by the wallets for creators other than the excluded one."""

        if not bundler_wallets:
            return ()
        placeholders = ", ".join("?" for _ in bundler_wallets)
        rows = self._db.connection.execute(
            f"""
            SELECT bundler_wallet, mint, creator, creation_slot,
                   buy_signature, transaction_index, max_sol_cost_lamports
            FROM tracker_bundle_participations
            WHERE bundler_wallet IN ({placeholders}) AND creator != ?
            ORDER BY bundler_wallet, creation_slot
            """,  # noqa: S608 - placeholders are generated question marks.
            (*bundler_wallets, exclude_creator),
        ).fetchall()
        return tuple(
            BundleParticipationRecord(
                bundler_wallet=row["bundler_wallet"],
                mint=row["mint"],
                creator=row["creator"],
                creation_slot=row["creation_slot"],
                buy_signature=row["buy_signature"],
                transaction_index=row["transaction_index"],
                max_sol_cost_lamports=row["max_sol_cost_lamports"],
            )
            for row in rows
        )

    def get_entity_backfill(self, query: str) -> EntityBackfillRecord | None:
        """Fetch one entity backfill by its original query or resolved wallet."""

        row = self._db.connection.execute(
            "SELECT * FROM tracker_entity_backfills WHERE query = ? OR wallet = ? ORDER BY updated_at DESC LIMIT 1",
            (query, query),
        ).fetchone()
        return _entity_backfill_from_row(row) if row is not None else None

    def get_incomplete_entity_backfills(self) -> tuple[EntityBackfillRecord, ...]:
        """Fetch entity backfills that can resume after process restart."""

        rows = self._db.connection.execute(
            """
            SELECT * FROM tracker_entity_backfills
            WHERE status IN (?, ?, ?)
            ORDER BY updated_at ASC
            """,
            (
                EntityBackfillStatus.PENDING.value,
                EntityBackfillStatus.RUNNING.value,
                EntityBackfillStatus.RATE_LIMITED.value,
            ),
        ).fetchall()
        return tuple(_entity_backfill_from_row(row) for row in rows)

    # --- Wallets ---

    def save_wallet(self, wallet: WalletRecord) -> None:
        """Insert or update a tracked wallet node."""
        conn = self._db.connection
        conn.execute(
            """
            INSERT INTO tracker_wallets (address, root_funder, parent_wallet, depth, status, discovered_at, expires_at, last_active_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(address) DO UPDATE SET
                status = excluded.status,
                expires_at = excluded.expires_at,
                last_active_at = excluded.last_active_at
            """,
            (
                wallet.address,
                wallet.root_funder,
                wallet.parent_wallet,
                wallet.depth,
                wallet.status.value,
                wallet.discovered_at,
                wallet.expires_at,
                wallet.last_active_at,
            ),
        )

    def get_wallet(self, address: str) -> WalletRecord | None:
        """Fetch a tracked wallet by address."""
        conn = self._db.connection
        cursor = conn.execute(
            "SELECT * FROM tracker_wallets WHERE address = ?", (address,)
        )
        row = cursor.fetchone()
        if row is None:
            return None
        return WalletRecord(
            address=row["address"],
            root_funder=row["root_funder"],
            parent_wallet=row["parent_wallet"],
            depth=row["depth"],
            status=WalletStatus(row["status"]),
            discovered_at=row["discovered_at"],
            expires_at=row["expires_at"],
            last_active_at=row["last_active_at"],
        )

    def get_wallets(self) -> tuple[WalletRecord, ...]:
        """Fetch all tracked wallets."""
        conn = self._db.connection
        cursor = conn.execute(
            "SELECT * FROM tracker_wallets ORDER BY depth ASC, discovered_at DESC"
        )
        return tuple(
            WalletRecord(
                address=row["address"],
                root_funder=row["root_funder"],
                parent_wallet=row["parent_wallet"],
                depth=row["depth"],
                status=WalletStatus(row["status"]),
                discovered_at=row["discovered_at"],
                expires_at=row["expires_at"],
                last_active_at=row["last_active_at"],
            )
            for row in cursor.fetchall()
        )

    def get_active_wallets(
        self, now_iso: str | None = None
    ) -> tuple[WalletRecord, ...]:
        """Fetch all active, non-expired wallets."""
        query = "SELECT * FROM tracker_wallets WHERE status != ?"
        params: list[Any] = [WalletStatus.EXPIRED.value]
        if now_iso:
            query += " AND (expires_at IS NULL OR expires_at > ?)"
            params.append(now_iso)
        conn = self._db.connection
        cursor = conn.execute(query, params)
        return tuple(
            WalletRecord(
                address=row["address"],
                root_funder=row["root_funder"],
                parent_wallet=row["parent_wallet"],
                depth=row["depth"],
                status=WalletStatus(row["status"]),
                discovered_at=row["discovered_at"],
                expires_at=row["expires_at"],
                last_active_at=row["last_active_at"],
            )
            for row in cursor.fetchall()
        )

    def get_descendants(self, root_funder: str) -> tuple[WalletRecord, ...]:
        """Fetch all active and non-expired descendants of a root funder."""
        conn = self._db.connection
        cursor = conn.execute(
            "SELECT * FROM tracker_wallets WHERE root_funder = ? AND address != ? AND status != ? ORDER BY depth ASC",
            (root_funder, root_funder, WalletStatus.EXPIRED.value),
        )
        return tuple(
            WalletRecord(
                address=row["address"],
                root_funder=row["root_funder"],
                parent_wallet=row["parent_wallet"],
                depth=row["depth"],
                status=WalletStatus(row["status"]),
                discovered_at=row["discovered_at"],
                expires_at=row["expires_at"],
                last_active_at=row["last_active_at"],
            )
            for row in cursor.fetchall()
        )

    def get_wallets_by_root_funder(self, root_funder: str) -> tuple[WalletRecord, ...]:
        """Alias for get_descendants."""
        return self.get_descendants(root_funder)

    def expire_wallets(self, now_iso: str) -> tuple[str, ...]:
        """Mark all expired wallets in the database and return their addresses."""
        conn = self._db.connection
        cursor = conn.execute(
            """
            SELECT address FROM tracker_wallets
            WHERE status NOT IN (?, ?)
            AND expires_at IS NOT NULL
            AND expires_at < ?
            """,
            (WalletStatus.FUNDER.value, WalletStatus.EXPIRED.value, now_iso),
        )
        expired_addresses = tuple(row["address"] for row in cursor.fetchall())
        if expired_addresses:
            conn.executemany(
                "UPDATE tracker_wallets SET status = ? WHERE address = ?",
                [(WalletStatus.EXPIRED.value, addr) for addr in expired_addresses],
            )
        return expired_addresses

    # --- Transfers ---

    def save_transfer(self, transfer: TransferRecord) -> bool:
        """Insert a verified SOL transfer. Returns True if inserted, False if duplicate."""
        try:
            conn = self._db.connection
            conn.execute(
                """
                INSERT INTO tracker_transfers (signature, instruction_index, slot, timestamp, from_wallet, to_wallet, amount_lamports, amount_sol, root_funder, depth)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    transfer.signature,
                    transfer.instruction_index,
                    transfer.slot,
                    transfer.timestamp,
                    transfer.from_wallet,
                    transfer.to_wallet,
                    transfer.amount_lamports,
                    transfer.amount_sol,
                    transfer.root_funder,
                    transfer.depth,
                ),
            )
        except sqlite3.IntegrityError:
            return False
        else:
            return True

    def get_transfers(self, limit: int = 100) -> tuple[TransferRecord, ...]:
        """Fetch latest verified transfers."""
        conn = self._db.connection
        cursor = conn.execute(
            "SELECT * FROM tracker_transfers ORDER BY slot DESC, timestamp DESC LIMIT ?",
            (limit,),
        )
        return tuple(
            TransferRecord(
                signature=row["signature"],
                instruction_index=row["instruction_index"],
                slot=row["slot"],
                timestamp=row["timestamp"],
                from_wallet=row["from_wallet"],
                to_wallet=row["to_wallet"],
                amount_lamports=row["amount_lamports"],
                amount_sol=row["amount_sol"],
                root_funder=row["root_funder"],
                depth=row["depth"],
            )
            for row in cursor.fetchall()
        )

    def get_parent_transfer(self, wallet_address: str) -> TransferRecord | None:
        """Find the earliest incoming transfer that funded this wallet."""
        conn = self._db.connection
        cursor = conn.execute(
            "SELECT * FROM tracker_transfers WHERE to_wallet = ? ORDER BY timestamp ASC, slot ASC LIMIT 1",
            (wallet_address,),
        )
        row = cursor.fetchone()
        if row is None:
            return None
        return TransferRecord(
            signature=row["signature"],
            instruction_index=row["instruction_index"],
            slot=row["slot"],
            timestamp=row["timestamp"],
            from_wallet=row["from_wallet"],
            to_wallet=row["to_wallet"],
            amount_lamports=row["amount_lamports"],
            amount_sol=row["amount_sol"],
            root_funder=row["root_funder"],
            depth=row["depth"],
        )

    # --- Launches ---

    def save_launch(self, launch: LaunchRecord) -> bool:
        """Insert a verified Pump.fun launch event. Returns True if new."""
        try:
            conn = self._db.connection
            conn.execute(
                """
                INSERT INTO tracker_launches (mint, creator_wallet, root_funder, symbol, name, created_signature, created_slot, created_at, depth, funding_signature, funding_amount_lamports, funding_timestamp)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    launch.mint,
                    launch.creator_wallet,
                    launch.root_funder,
                    launch.symbol,
                    launch.name,
                    launch.created_signature,
                    launch.created_slot,
                    launch.created_at,
                    launch.depth,
                    launch.funding_signature,
                    launch.funding_amount_lamports,
                    launch.funding_timestamp,
                ),
            )
        except sqlite3.IntegrityError:
            return False
        else:
            return True

    def get_launches(self, limit: int = 100) -> tuple[LaunchRecord, ...]:
        """Fetch latest verified launches."""
        conn = self._db.connection
        cursor = conn.execute(
            "SELECT * FROM tracker_launches ORDER BY created_slot DESC, created_at DESC LIMIT ?",
            (limit,),
        )
        return tuple(
            LaunchRecord(
                mint=row["mint"],
                creator_wallet=row["creator_wallet"],
                root_funder=row["root_funder"],
                symbol=row["symbol"],
                name=row["name"],
                created_signature=row["created_signature"],
                created_slot=row["created_slot"],
                created_at=row["created_at"],
                depth=row["depth"],
                funding_signature=row["funding_signature"],
                funding_amount_lamports=row["funding_amount_lamports"],
                funding_timestamp=row["funding_timestamp"],
            )
            for row in cursor.fetchall()
        )

    def get_launch(self, mint: str) -> LaunchRecord | None:
        """Fetch a launch record by token mint address."""
        conn = self._db.connection
        cursor = conn.execute("SELECT * FROM tracker_launches WHERE mint = ?", (mint,))
        row = cursor.fetchone()
        if row is None:
            return None
        return LaunchRecord(
            mint=row["mint"],
            creator_wallet=row["creator_wallet"],
            root_funder=row["root_funder"],
            symbol=row["symbol"],
            name=row["name"],
            created_signature=row["created_signature"],
            created_slot=row["created_slot"],
            created_at=row["created_at"],
            depth=row["depth"],
            funding_signature=row["funding_signature"],
            funding_amount_lamports=row["funding_amount_lamports"],
            funding_timestamp=row["funding_timestamp"],
        )

    def get_launches_for_funder(self, root_funder: str) -> tuple[LaunchRecord, ...]:
        """Fetch all launches associated with a root funder, creator wallet, or child cluster wallets."""
        conn = self._db.connection
        cursor = conn.execute(
            """
            SELECT * FROM tracker_launches
            WHERE root_funder = ?
               OR creator_wallet = ?
               OR creator_wallet IN (SELECT address FROM tracker_wallets WHERE root_funder = ?)
            ORDER BY created_at DESC
            """,
            (root_funder, root_funder, root_funder),
        )
        return tuple(
            LaunchRecord(
                mint=row["mint"],
                creator_wallet=row["creator_wallet"],
                root_funder=row["root_funder"],
                symbol=row["symbol"],
                name=row["name"],
                created_signature=row["created_signature"],
                created_slot=row["created_slot"],
                created_at=row["created_at"],
                depth=row["depth"],
                funding_signature=row["funding_signature"],
                funding_amount_lamports=row["funding_amount_lamports"],
                funding_timestamp=row["funding_timestamp"],
            )
            for row in cursor.fetchall()
        )

    def get_launches_by_root_funder(self, root_funder: str) -> tuple[LaunchRecord, ...]:
        """Alias for get_launches_for_funder."""
        return self.get_launches_for_funder(root_funder)

    def get_transfers_for_funder(self, root_funder: str) -> tuple[TransferRecord, ...]:
        """Fetch all transfers associated with a root funder."""
        conn = self._db.connection
        cursor = conn.execute(
            """
            SELECT * FROM tracker_transfers
            WHERE root_funder = ?
               OR from_wallet = ?
               OR to_wallet = ?
            ORDER BY slot DESC, timestamp DESC
            """,
            (root_funder, root_funder, root_funder),
        )
        return tuple(
            TransferRecord(
                signature=row["signature"],
                instruction_index=row["instruction_index"],
                slot=row["slot"],
                timestamp=row["timestamp"],
                from_wallet=row["from_wallet"],
                to_wallet=row["to_wallet"],
                amount_lamports=row["amount_lamports"],
                amount_sol=row["amount_sol"],
                root_funder=row["root_funder"],
                depth=row["depth"],
            )
            for row in cursor.fetchall()
        )

    def get_transfers_by_root_funder(
        self, root_funder: str
    ) -> tuple[TransferRecord, ...]:
        """Alias for get_transfers_for_funder."""
        return self.get_transfers_for_funder(root_funder)

    # --- Alert Outbox ---

    def enqueue_launch_alerts(
        self,
        launch: LaunchRecord,
        consumers: tuple[str, ...],
        creator_wallet: WalletRecord,
    ) -> bool:
        """Insert a launch, its creator-wallet CREATOR update, and outbox rows atomically.

        The launch insert, the creator-wallet status update, and the per-consumer
        outbox rows commit in one transaction: either all rows persist or none do.
        Returns True when the launch was newly inserted and False when the mint
        already exists (in which case nothing is written). A persistence failure
        propagates after rolling back the whole transaction so the caller can
        retry without leaving partially persisted CREATOR state.
        """
        conn = self._db.connection
        conn.execute("BEGIN")
        try:
            try:
                conn.execute(
                    """
                    INSERT INTO tracker_launches (mint, creator_wallet, root_funder, symbol, name, created_signature, created_slot, created_at, depth, funding_signature, funding_amount_lamports, funding_timestamp)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        launch.mint,
                        launch.creator_wallet,
                        launch.root_funder,
                        launch.symbol,
                        launch.name,
                        launch.created_signature,
                        launch.created_slot,
                        launch.created_at,
                        launch.depth,
                        launch.funding_signature,
                        launch.funding_amount_lamports,
                        launch.funding_timestamp,
                    ),
                )
            except sqlite3.IntegrityError:
                conn.execute("ROLLBACK")
                return False
            else:
                conn.execute(
                    """
                    INSERT INTO tracker_wallets (address, root_funder, parent_wallet, depth, status, discovered_at, expires_at, last_active_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(address) DO UPDATE SET
                        status = excluded.status,
                        expires_at = excluded.expires_at,
                        last_active_at = excluded.last_active_at
                    """,
                    (
                        creator_wallet.address,
                        creator_wallet.root_funder,
                        creator_wallet.parent_wallet,
                        creator_wallet.depth,
                        creator_wallet.status.value,
                        creator_wallet.discovered_at,
                        creator_wallet.expires_at,
                        creator_wallet.last_active_at,
                    ),
                )
                conn.executemany(
                    """
                    INSERT INTO tracker_alert_outbox (mint, consumer, delivered, created_at)
                    VALUES (?, ?, 0, ?)
                    """,
                    [
                        (launch.mint, consumer, launch.created_at)
                        for consumer in consumers
                    ],
                )
                conn.execute("COMMIT")
                return True
        except Exception:
            conn.execute("ROLLBACK")
            raise

    def get_undelivered_alerts(self, consumer: str) -> tuple[AlertOutboxRecord, ...]:
        """Fetch undelivered alert outbox rows for one consumer, oldest first."""
        conn = self._db.connection
        cursor = conn.execute(
            """
            SELECT mint, consumer, delivered, created_at
            FROM tracker_alert_outbox
            WHERE consumer = ? AND delivered = 0
            ORDER BY created_at ASC
            """,
            (consumer,),
        )
        return tuple(
            AlertOutboxRecord(
                mint=row["mint"],
                consumer=row["consumer"],
                delivered=bool(row["delivered"]),
                created_at=row["created_at"],
            )
            for row in cursor.fetchall()
        )

    def mark_alerts_delivered(self, consumer: str, mints: tuple[str, ...]) -> None:
        """Mark the given outbox rows as delivered for one consumer."""
        if not mints:
            return
        conn = self._db.connection
        conn.executemany(
            """
            UPDATE tracker_alert_outbox SET delivered = 1
            WHERE consumer = ? AND mint = ?
            """,
            [(consumer, mint) for mint in mints],
        )

    # --- Launch Activation Cursors ---

    def set_launch_activation(self, address: str, activation_slot: int) -> None:
        """Persist the per-address finalized-slot activation cursor."""
        conn = self._db.connection
        conn.execute(
            """
            INSERT INTO tracker_launch_activation (address, activation_slot, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(address) DO UPDATE SET
                activation_slot = excluded.activation_slot,
                updated_at = excluded.updated_at
            """,
            (address, activation_slot, int(time.time())),
        )

    def get_launch_activation(self, address: str) -> int | None:
        """Fetch the persisted activation cursor for one address, or None."""
        conn = self._db.connection
        row = conn.execute(
            "SELECT activation_slot FROM tracker_launch_activation WHERE address = ?",
            (address,),
        ).fetchone()
        if row is None:
            return None
        return row["activation_slot"]

    # --- Stats & Search ---

    def get_summary_stats(self) -> dict[str, int]:
        """Return aggregate metrics for dashboard displays."""
        conn = self._db.connection
        f_cnt = conn.execute(
            "SELECT COUNT(*) FROM tracker_funders WHERE enabled = 1"
        ).fetchone()[0]
        w_cnt = conn.execute("SELECT COUNT(*) FROM tracker_wallets").fetchone()[0]
        act_w_cnt = conn.execute(
            "SELECT COUNT(*) FROM tracker_wallets WHERE status != ?",
            (WalletStatus.EXPIRED.value,),
        ).fetchone()[0]
        funded_cnt = conn.execute(
            "SELECT COUNT(*) FROM tracker_wallets WHERE status = ?",
            (WalletStatus.FUNDED.value,),
        ).fetchone()[0]
        creators_cnt = conn.execute(
            "SELECT COUNT(*) FROM tracker_wallets WHERE status = ?",
            (WalletStatus.CREATOR.value,),
        ).fetchone()[0]
        l_cnt = conn.execute("SELECT COUNT(*) FROM tracker_launches").fetchone()[0]

        return {
            "funders_count": f_cnt,
            "wallets_count": w_cnt,
            "active_wallets": act_w_cnt,
            "funded_count": funded_cnt,
            "creators_count": creators_cnt,
            "launches_count": l_cnt,
        }

    def search(self, query: str) -> dict[str, tuple[Any, ...]]:
        """Multi-entity search across funders, wallets, transfers, and launches."""
        q = f"%{query.strip()}%"
        conn = self._db.connection
        funders = [
            FunderRecord(
                id=r["id"],
                address=r["address"],
                label=r["label"],
                enabled=bool(r["enabled"]),
                created_at=r["created_at"],
                last_seen_at=r["last_seen_at"],
            )
            for r in conn.execute(
                "SELECT * FROM tracker_funders WHERE address LIKE ? OR label LIKE ?",
                (q, q),
            ).fetchall()
        ]
        wallets = [
            WalletRecord(
                address=r["address"],
                root_funder=r["root_funder"],
                parent_wallet=r["parent_wallet"],
                depth=r["depth"],
                status=WalletStatus(r["status"]),
                discovered_at=r["discovered_at"],
                expires_at=r["expires_at"],
                last_active_at=r["last_active_at"],
            )
            for r in conn.execute(
                "SELECT * FROM tracker_wallets WHERE address LIKE ? OR root_funder LIKE ?",
                (q, q),
            ).fetchall()
        ]
        transfers = [
            TransferRecord(
                signature=r["signature"],
                instruction_index=r["instruction_index"],
                slot=r["slot"],
                timestamp=r["timestamp"],
                from_wallet=r["from_wallet"],
                to_wallet=r["to_wallet"],
                amount_lamports=r["amount_lamports"],
                amount_sol=r["amount_sol"],
                root_funder=r["root_funder"],
                depth=r["depth"],
            )
            for r in conn.execute(
                "SELECT * FROM tracker_transfers WHERE from_wallet LIKE ? OR to_wallet LIKE ? OR signature LIKE ?",
                (q, q, q),
            ).fetchall()
        ]
        launches = [
            LaunchRecord(
                mint=r["mint"],
                creator_wallet=r["creator_wallet"],
                root_funder=r["root_funder"],
                symbol=r["symbol"],
                name=r["name"],
                created_signature=r["created_signature"],
                created_slot=r["created_slot"],
                created_at=r["created_at"],
                depth=r["depth"],
                funding_signature=r["funding_signature"],
                funding_amount_lamports=r["funding_amount_lamports"],
                funding_timestamp=r["funding_timestamp"],
            )
            for r in conn.execute(
                "SELECT * FROM tracker_launches WHERE mint LIKE ? OR symbol LIKE ? OR name LIKE ? OR creator_wallet LIKE ?",
                (q, q, q, q),
            ).fetchall()
        ]
        return {
            "funders": tuple(funders),
            "wallets": tuple(wallets),
            "transfers": tuple(transfers),
            "launches": tuple(launches),
        }


def _entity_backfill_from_row(row: sqlite3.Row) -> EntityBackfillRecord:
    """Decode one persisted entity backfill row."""

    return EntityBackfillRecord(
        query=row["query"],
        wallet=row["wallet"],
        requested_transactions=row["requested_transactions"],
        cached_transactions=row["cached_transactions"],
        before_signature=row["before_signature"],
        status=EntityBackfillStatus(row["status"]),
        message=row["message"],
        report_json=row["report_json"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )
