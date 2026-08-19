"""SQLite implementation of the TrackerRepository protocol."""

from __future__ import annotations

import sqlite3
from typing import TYPE_CHECKING, Any

from rugbot.tracker.models import (
    FunderRecord,
    LaunchRecord,
    TargetExecutionMode,
    TargetExecutionPolicy,
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
            """
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
        """Fetch all launches associated with a root funder."""
        conn = self._db.connection
        cursor = conn.execute(
            "SELECT * FROM tracker_launches WHERE root_funder = ? ORDER BY created_at DESC",
            (root_funder,),
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
