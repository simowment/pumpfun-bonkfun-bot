"""Pure deterministic state engine executing the funding-tree mutation rules."""

from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING

from rugbot.tracker.clock import Clock, SystemClock
from rugbot.tracker.events import (
    FunderAdded,
    LaunchDetected,
    PathDepthLimitReached,
    PathStopped,
    TrackerEvent,
    TransferDetected,
    WalletExpired,
    WalletFunded,
)
from rugbot.tracker.models import (
    LAMPORTS_PER_SOL,
    FunderRecord,
    TrackerConfig,
    WalletRecord,
    WalletStatus,
)

if TYPE_CHECKING:
    from rugbot.domain.transfers import SolTransfer
    from rugbot.ingest.pump.models import TokenLaunch


class TrackerEngine:
    """Pure deterministic in-memory tracker executing the 10-step funding tree algorithm.

    This engine does no I/O and does not publish to external event buses. It consumes
    normalized protocol events and returns a list of resulting TrackerEvents.
    """

    def __init__(
        self,
        config: TrackerConfig | None = None,
        clock: Clock | None = None,
    ) -> None:
        self._config = config or TrackerConfig()
        self._clock: Clock = clock or SystemClock()
        self._tracked_wallets: dict[str, WalletRecord] = {}
        self._funder_labels: dict[str, str] = {}
        self._active_funders: set[str] = set()
        self._processed_transfers: set[tuple[str, int]] = set()
        self._processed_launches: set[str] = set()
        self._metrics: dict[str, int] = {
            "transfers_parsed": 0,
            "transfers_matched": 0,
            "launches_detected": 0,
            "wallets_expired": 0,
        }

    @property
    def config(self) -> TrackerConfig:
        return self._config

    def set_config(self, config: TrackerConfig) -> None:
        self._config = config

    @property
    def clock(self) -> Clock:
        return self._clock

    @property
    def tracked_wallets(self) -> dict[str, WalletRecord]:
        return dict(self._tracked_wallets)

    def get_tracked_wallet(self, address: str) -> WalletRecord | None:
        return self._tracked_wallets.get(address)

    def restore_wallet(self, wallet: WalletRecord) -> None:
        """Hydrate an active wallet record from persistent storage on startup."""
        self._tracked_wallets[wallet.address] = wallet

    def is_tracked(self, address: str) -> bool:
        return address in self._tracked_wallets

    def is_active_funder(self, address: str) -> bool:
        """Return whether the address is currently an active root funder."""
        return address in self._active_funders

    def reconcile_state(
        self,
        enabled_funders: tuple[FunderRecord, ...],
        active_wallets: tuple[WalletRecord, ...],
    ) -> None:
        """Reconcile in-memory state to the canonical repository state.

        Resets the engine to exactly the enabled funders and the active,
        non-expired descendant wallets of those funders. Funders that were
        disabled and wallets that expired or were removed externally are dropped;
        descendants of disabled funders are never restored. This is a silent
        hydration primitive used on cold start and refresh and emits no events.
        """
        enabled = {funder.address for funder in enabled_funders}
        self._active_funders = set(enabled)
        self._funder_labels = {
            funder.address: funder.label for funder in enabled_funders
        }

        rebuilt: dict[str, WalletRecord] = {}
        for funder in enabled_funders:
            rebuilt[funder.address] = WalletRecord(
                address=funder.address,
                root_funder=funder.address,
                parent_wallet=None,
                depth=0,
                status=WalletStatus.FUNDER,
                discovered_at=funder.created_at,
                expires_at=None,
                last_active_at=funder.last_seen_at,
            )
        for wallet in active_wallets:
            if wallet.root_funder in enabled:
                rebuilt[wallet.address] = wallet
        self._tracked_wallets = rebuilt

    @property
    def metrics(self) -> dict[str, int]:
        return dict(self._metrics)

    def add_funder(self, address: str, label: str = "") -> list[TrackerEvent]:
        """Register a new root funder and activate tracking."""
        now_dt = self._clock.now()
        now_iso = now_dt.isoformat()
        self._funder_labels[address] = label
        self._active_funders.add(address)

        root_wallet = WalletRecord(
            address=address,
            root_funder=address,
            parent_wallet=None,
            depth=0,
            status=WalletStatus.FUNDER,
            discovered_at=now_iso,
            expires_at=None,
            last_active_at=now_iso,
        )
        self._tracked_wallets[address] = root_wallet

        event = FunderAdded(
            root_funder=address,
            wallet=address,
            timestamp=self._clock.timestamp(),
            data={"address": address, "label": label, "created_at": now_iso},
        )
        return [event]

    def remove_funder(self, address: str) -> list[TrackerEvent]:
        """Disable a root funder."""
        self._active_funders.discard(address)
        self._tracked_wallets.pop(address, None)
        return []

    def handle_transfer(self, transfer: SolTransfer) -> list[TrackerEvent]:
        """Process one normalized SOL transfer against the active funding tree."""
        self._metrics["transfers_parsed"] += 1

        parent = self._tracked_wallets.get(transfer.sender)
        if parent is None:
            return []

        if transfer.lamports < self._config.min_transfer_lamports:
            return []

        # Deduplication key: (signature, instruction_index)
        tx_key = (transfer.signature, transfer.instruction_index)
        if tx_key in self._processed_transfers:
            return []
        self._processed_transfers.add(tx_key)

        now_dt = self._clock.now()
        now_iso = now_dt.isoformat()
        parent.last_active_at = now_iso

        events: list[TrackerEvent] = []
        root_funder = parent.root_funder
        next_depth = parent.depth + 1
        amount_sol = transfer.lamports / LAMPORTS_PER_SOL

        # 1. Depth limit check
        if next_depth > self._config.max_depth:
            events.append(
                PathDepthLimitReached(
                    root_funder=root_funder,
                    wallet=transfer.recipient,
                    timestamp=self._clock.timestamp(),
                    data={
                        "from": transfer.sender,
                        "to": transfer.recipient,
                        "depth": next_depth,
                        "max_depth": self._config.max_depth,
                        "amount_lamports": transfer.lamports,
                        "amount_sol": amount_sol,
                        "signature": transfer.signature,
                    },
                )
            )
            return events

        # 2. Blocked intermediary check
        if transfer.recipient in self._config.blocked_intermediaries:
            events.append(
                PathStopped(
                    root_funder=root_funder,
                    wallet=transfer.recipient,
                    timestamp=self._clock.timestamp(),
                    data={
                        "reason": "blocked_intermediary",
                        "from": transfer.sender,
                        "to": transfer.recipient,
                        "signature": transfer.signature,
                    },
                )
            )
            return events

        # 3. Add or update recipient node in memory
        expires_at_dt = now_dt + timedelta(seconds=self._config.descendant_ttl_seconds)
        expires_at_iso = expires_at_dt.isoformat()

        child = self._tracked_wallets.get(transfer.recipient)
        is_new = child is None
        if child is None:
            child = WalletRecord(
                address=transfer.recipient,
                root_funder=root_funder,
                parent_wallet=transfer.sender,
                depth=next_depth,
                status=WalletStatus.FUNDED,
                discovered_at=now_iso,
                expires_at=expires_at_iso,
                last_active_at=now_iso,
            )
            self._tracked_wallets[transfer.recipient] = child
        else:
            # Reactivate if expired
            if child.status == WalletStatus.EXPIRED:
                child.status = WalletStatus.FUNDED
            child.expires_at = expires_at_iso
            child.last_active_at = now_iso

        self._metrics["transfers_matched"] += 1

        # Emit transfer detected
        events.append(
            TransferDetected(
                root_funder=root_funder,
                wallet=transfer.recipient,
                timestamp=self._clock.timestamp(),
                data={
                    "from": transfer.sender,
                    "to": transfer.recipient,
                    "amount_lamports": transfer.lamports,
                    "amount_sol": amount_sol,
                    "signature": transfer.signature,
                    "depth": next_depth,
                },
            )
        )

        if is_new:
            events.append(
                WalletFunded(
                    root_funder=root_funder,
                    wallet=transfer.recipient,
                    timestamp=self._clock.timestamp(),
                    data={
                        "parent": transfer.sender,
                        "depth": next_depth,
                        "amount_lamports": transfer.lamports,
                        "amount_sol": amount_sol,
                        "signature": transfer.signature,
                        "expires_at": expires_at_iso,
                    },
                )
            )

        return events

    def is_launch_eligible(self, launch: TokenLaunch) -> bool:
        """Return whether a launch can be committed without mutating engine state.

        A launch is eligible when its mint has not already been processed and its
        creator is a tracked, non-expired wallet. This is a pure check used by the
        service before persisting; it never mutates state.
        """
        if launch.mint in self._processed_launches:
            return False
        creator_wallet = self._tracked_wallets.get(launch.creator)
        if creator_wallet is None or creator_wallet.status == WalletStatus.EXPIRED:
            return False
        return True

    def commit_launch(self, launch: TokenLaunch) -> list[TrackerEvent]:
        """Apply the launch mutation and emit a LaunchDetected event.

        Called by the service only after the launch, its creator-wallet CREATOR
        update, and its outbox rows have been durably persisted, so the engine
        mutation is never left dangling on a persistence failure. Re-checks
        eligibility defensively and returns an empty list when the launch is no
        longer committable.
        """
        if not self.is_launch_eligible(launch):
            return []
        creator_wallet = self._tracked_wallets[launch.creator]
        self._processed_launches.add(launch.mint)
        now_dt = self._clock.now()
        now_iso = now_dt.isoformat()

        creator_wallet.status = WalletStatus.CREATOR
        creator_wallet.last_active_at = now_iso

        self._metrics["launches_detected"] += 1

        event = LaunchDetected(
            root_funder=creator_wallet.root_funder,
            wallet=launch.creator,
            timestamp=self._clock.timestamp(),
            data={
                "mint": launch.mint,
                "symbol": launch.symbol,
                "name": launch.name,
                "creator": launch.creator,
                "root_funder": creator_wallet.root_funder,
                "depth": creator_wallet.depth,
                "signature": launch.signature,
                "slot": launch.slot,
                "created_at": launch.timestamp,
            },
        )
        return [event]

    def expire_wallets(self) -> list[TrackerEvent]:
        """Check all descendant nodes against their TTL and transition expired nodes."""
        now_dt = self._clock.now()
        now_iso = now_dt.isoformat()
        events: list[TrackerEvent] = []

        for address, wallet in list(self._tracked_wallets.items()):
            if wallet.status in (
                WalletStatus.FUNDER,
                WalletStatus.EXPIRED,
                WalletStatus.IGNORED,
            ):
                continue

            if wallet.expires_at and wallet.expires_at < now_iso:
                wallet.status = WalletStatus.EXPIRED
                self._metrics["wallets_expired"] += 1
                events.append(
                    WalletExpired(
                        root_funder=wallet.root_funder,
                        wallet=address,
                        timestamp=self._clock.timestamp(),
                        data={
                            "depth": wallet.depth,
                            "last_active_at": wallet.last_active_at,
                            "expired_at": now_iso,
                        },
                    )
                )

        return events
