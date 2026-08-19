"""Service coordinating the tracker engine, persistence, and event delivery."""

from __future__ import annotations

from typing import TYPE_CHECKING

from rugbot.tracker.models import (
    FunderRecord,
    LaunchRecord,
    TargetExecutionPolicy,
    TransferRecord,
)

if TYPE_CHECKING:
    from rugbot.protocol.pump.models import TokenLaunch
    from rugbot.protocol.solana.models import SolTransfer
    from rugbot.runtime.event_bus import EventBus
    from rugbot.tracker.engine import TrackerEngine
    from rugbot.tracker.events import TrackerEvent
    from rugbot.tracker.repository import TrackerRepository


UNKNOWN_FUNDER_POLICY_ERROR = "execution policy requires an existing tracked funder"


class TrackerService:
    """Orchestrate deterministic mutations, persistence, and event delivery."""

    def __init__(
        self,
        engine: TrackerEngine,
        repository: TrackerRepository,
        event_bus: EventBus | None = None,
    ) -> None:
        self._engine = engine
        self._repository = repository
        self._event_bus = event_bus
        self._restore_state()

    @property
    def engine(self) -> TrackerEngine:
        """Return the in-memory tracker engine."""
        return self._engine

    @property
    def repository(self) -> TrackerRepository:
        """Return the canonical tracker repository."""
        return self._repository

    @property
    def event_bus(self) -> EventBus | None:
        """Return the optional event bus."""
        return self._event_bus

    def _restore_state(self) -> None:
        """Hydrate the in-memory engine from persisted state on cold start."""
        for funder in self._repository.get_funders(enabled_only=True):
            self._engine.add_funder(funder.address, label=funder.label)

        active_wallets = self._repository.get_active_wallets(
            self._engine.clock.now().isoformat()
        )
        for wallet in active_wallets:
            if wallet.address not in self._engine.tracked_wallets:
                self._engine.restore_wallet(wallet)

    def add_funder(self, address: str, label: str = "") -> list[TrackerEvent]:
        """Register and persist a root funder."""
        events = self._engine.add_funder(address, label=label)
        now_iso = self._engine.clock.now().isoformat()
        self._repository.save_funder(
            FunderRecord(
                id=None,
                address=address,
                label=label,
                enabled=True,
                created_at=now_iso,
                last_seen_at=now_iso,
            )
        )
        self._dispatch_events(events)
        return events

    def remove_funder(self, address: str) -> list[TrackerEvent]:
        """Disable a root funder."""
        events = self._engine.remove_funder(address)
        self._repository.enable_funder(address, enabled=False)
        self._dispatch_events(events)
        return events

    def save_target_execution_policy(self, policy: TargetExecutionPolicy) -> None:
        """Persist an execution policy for a known tracked funder."""
        if self._repository.get_funder(policy.funder_address) is None:
            raise ValueError(UNKNOWN_FUNDER_POLICY_ERROR)
        self._repository.save_target_execution_policy(policy)

    def handle_transfer(self, transfer: SolTransfer) -> list[TrackerEvent]:
        """Persist one normalized SOL transfer and its tracker mutations."""
        events = self._engine.handle_transfer(transfer)
        if not events:
            return []

        child = self._engine.get_tracked_wallet(transfer.recipient)
        if child is not None:
            self._repository.save_wallet(child)
            self._repository.save_transfer(
                TransferRecord(
                    signature=transfer.signature,
                    instruction_index=transfer.instruction_index,
                    slot=transfer.slot,
                    timestamp=transfer.timestamp,
                    from_wallet=transfer.sender,
                    to_wallet=transfer.recipient,
                    amount_lamports=transfer.lamports,
                    amount_sol=transfer.sol_amount,
                    root_funder=child.root_funder,
                    depth=child.depth,
                )
            )

        self._dispatch_events(events)
        return events

    def handle_launch(self, launch: TokenLaunch) -> list[TrackerEvent]:
        """Persist a proven launch without inventing a qualification decision."""
        events = self._engine.handle_launch(launch)
        if not events:
            return []

        creator_wallet = self._engine.get_tracked_wallet(launch.creator)
        if creator_wallet is not None:
            self._repository.save_wallet(creator_wallet)
            parent_transfer = self._repository.get_parent_transfer(launch.creator)
            self._repository.save_launch(
                LaunchRecord(
                    mint=launch.mint,
                    creator_wallet=launch.creator,
                    root_funder=creator_wallet.root_funder,
                    symbol=launch.symbol,
                    name=launch.name,
                    created_signature=launch.signature,
                    created_slot=launch.slot,
                    created_at=launch.timestamp,
                    depth=creator_wallet.depth,
                    funding_signature=(
                        parent_transfer.signature if parent_transfer else None
                    ),
                    funding_amount_lamports=(
                        parent_transfer.amount_lamports if parent_transfer else None
                    ),
                    funding_timestamp=(
                        parent_transfer.timestamp if parent_transfer else None
                    ),
                )
            )

        self._dispatch_events(events)
        return events

    def expire_wallets(self) -> list[TrackerEvent]:
        """Expire out-of-TTL wallets in memory and persistence."""
        events = self._engine.expire_wallets()
        self._repository.expire_wallets(self._engine.clock.now().isoformat())
        self._dispatch_events(events)
        return events

    def _dispatch_events(self, events: list[TrackerEvent]) -> None:
        """Publish events only after their state mutations are persisted."""
        if self._event_bus is None:
            return
        for event in events:
            self._event_bus.publish(event)
