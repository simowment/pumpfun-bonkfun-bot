"""Persistence contract protocol for the deterministic tracker."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from rugbot.tracker.models import (
        AlertOutboxRecord,
        FunderRecord,
        LaunchRecord,
        TargetExecutionPolicy,
        TransferRecord,
        WalletRecord,
    )


class TrackerRepository(Protocol):
    """Abstract storage contract for the deterministic funding tracker."""

    # --- Funders ---
    def save_funder(self, funder: FunderRecord) -> None:
        """Insert or update a root funder."""

    def get_funders(self, *, enabled_only: bool = False) -> tuple[FunderRecord, ...]:
        """Fetch all registered root funders."""

    def get_funder(self, address: str) -> FunderRecord | None:
        """Fetch one root funder by address."""

    def enable_funder(self, address: str, *, enabled: bool) -> None:
        """Enable or disable tracking for a root funder."""

    def delete_funder(self, address: str) -> None:
        """Completely remove a root funder and its execution policy."""

    def save_target_execution_policy(self, policy: TargetExecutionPolicy) -> None:
        """Persist one execution policy owned by a tracked funder."""

    def get_target_execution_policy(
        self, funder_address: str
    ) -> TargetExecutionPolicy | None:
        """Fetch the execution policy for one tracked funder."""

    # --- Wallets ---
    def save_wallet(self, wallet: WalletRecord) -> None:
        """Insert or update a tracked wallet node."""

    def get_wallet(self, address: str) -> WalletRecord | None:
        """Fetch a tracked wallet by address."""

    def get_wallets(self) -> tuple[WalletRecord, ...]:
        """Fetch all tracked wallets."""

    def get_active_wallets(
        self, now_iso: str | None = None
    ) -> tuple[WalletRecord, ...]:
        """Fetch all active, non-expired wallets."""

    def get_descendants(self, root_funder: str) -> tuple[WalletRecord, ...]:
        """Fetch all active and non-expired descendants of a root funder."""

    def expire_wallets(self, now_iso: str) -> tuple[str, ...]:
        """Mark all expired wallets in the database and return their addresses."""

    # --- Transfers ---
    def save_transfer(self, transfer: TransferRecord) -> bool:
        """Insert a verified SOL transfer. Returns True if inserted, False if duplicate."""

    def get_transfers(self, limit: int = 100) -> tuple[TransferRecord, ...]:
        """Fetch latest verified transfers."""

    def get_parent_transfer(self, wallet_address: str) -> TransferRecord | None:
        """Find the earliest incoming transfer that funded this wallet."""

    # --- Launches ---
    def save_launch(self, launch: LaunchRecord) -> bool:
        """Insert a verified Pump.fun launch event. Returns True if new."""

    def get_launches(self, limit: int = 100) -> tuple[LaunchRecord, ...]:
        """Fetch latest verified launches."""

    def get_launch(self, mint: str) -> LaunchRecord | None:
        """Fetch a launch record by token mint address."""

    def get_launches_for_funder(self, root_funder: str) -> tuple[LaunchRecord, ...]:
        """Fetch all launches associated with a root funder."""

    # --- Alert Outbox ---
    def enqueue_launch_alerts(
        self,
        launch: LaunchRecord,
        consumers: tuple[str, ...],
        creator_wallet: WalletRecord,
    ) -> bool:
        """Insert a launch, its creator-wallet CREATOR update, and outbox rows atomically."""

    def get_undelivered_alerts(self, consumer: str) -> tuple[AlertOutboxRecord, ...]:
        """Fetch undelivered alert outbox rows for one consumer."""

    def mark_alerts_delivered(self, consumer: str, mints: tuple[str, ...]) -> None:
        """Mark the given outbox rows as delivered for one consumer."""

    # --- Launch Activation Cursors ---
    def set_launch_activation(self, address: str, activation_slot: int) -> None:
        """Persist the per-address finalized-slot activation cursor."""

    def get_launch_activation(self, address: str) -> int | None:
        """Fetch the persisted activation cursor for one address, or None."""

    # --- Stats & Search ---
    def get_summary_stats(self) -> dict[str, int]:
        """Return aggregate metrics for dashboard displays."""

    def search(self, query: str) -> dict[str, tuple[Any, ...]]:
        """Multi-entity search across funders, wallets, transfers, and launches."""
