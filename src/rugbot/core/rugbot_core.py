"""UI-agnostic RugbotCore facade over the composed tracker and sniper services."""

from __future__ import annotations

import inspect
from typing import TYPE_CHECKING

from rugbot.core.commands import COMMAND_REGISTRY, BotCommand, CommandResult
from rugbot.runtime.sniper_daemon import SniperDaemonError

if TYPE_CHECKING:
    from collections.abc import Callable

    from rugbot.execution.position_runtime import PaperPositionState
    from rugbot.runtime.event_bus import EventBus
    from rugbot.runtime.sniper_daemon import SniperDaemonService, SniperDaemonSnapshot
    from rugbot.runtime.sniper_runtime import SniperRuntime
    from rugbot.runtime.tracker_service import TrackerService
    from rugbot.storage.tracker import SQLiteTrackerRepository
    from rugbot.tracker.engine import TrackerEngine
    from rugbot.tracker.events import TrackerEvent
    from rugbot.tracker.models import (
        FunderRecord,
        LaunchRecord,
        TargetExecutionMode,
        TargetExecutionPolicy,
        WalletRecord,
    )


class RugbotCore:
    """Facade exposing one shared core to any UI adapter."""

    def __init__(  # noqa: PLR0913
        self,
        *,
        engine: TrackerEngine,
        repository: SQLiteTrackerRepository,
        event_bus: EventBus,
        service: TrackerService,
        sniper_runtime: SniperRuntime | None = None,
        sniper_daemon: SniperDaemonService | None = None,
    ) -> None:
        self._engine = engine
        self._repository = repository
        self._event_bus = event_bus
        self._service = service
        self._sniper_runtime = sniper_runtime
        self._sniper_daemon = sniper_daemon

    @property
    def engine(self) -> TrackerEngine:
        """Return the in-memory tracker engine."""
        return self._engine

    @property
    def repository(self) -> SQLiteTrackerRepository:
        """Return the canonical tracker repository."""
        return self._repository

    @property
    def event_bus(self) -> EventBus:
        """Return the shared event bus."""
        return self._event_bus

    @property
    def service(self) -> TrackerService:
        """Return the tracker service owning engine mutations and persistence."""
        return self._service

    @property
    def sniper_runtime(self) -> SniperRuntime | None:
        """Return the optional sniper runtime composition container."""
        return self._sniper_runtime

    @property
    def sniper_daemon(self) -> SniperDaemonService | None:
        """Return the optional sniper daemon service."""
        return self._sniper_daemon

    async def execute_command(self, cmd: BotCommand) -> CommandResult:
        """Dispatch one command through the registry, awaiting async handlers."""
        handler = COMMAND_REGISTRY.get(cmd.name)
        if handler is None:
            return CommandResult(ok=False, message=f"unknown command: {cmd.name}")
        result = handler(self, cmd)
        if inspect.isawaitable(result):
            result = await result
        return result

    def watch(self, address: str, *, label: str = "") -> CommandResult:
        """Register and persist a root funder for surveillance."""
        if not address:
            return CommandResult(ok=False, message="watch requires a wallet address")
        self._service.add_funder(address, label=label)
        return CommandResult(ok=True, message=f"watching {address}")

    def unwatch(self, address: str) -> CommandResult:
        """Disable tracking for a root funder."""
        if not address:
            return CommandResult(ok=False, message="unwatch requires a wallet address")
        self._service.remove_funder(address)
        return CommandResult(ok=True, message=f"stopped watching {address}")

    def set_target_mode(
        self,
        target_id: str,
        mode: TargetExecutionMode,
    ) -> CommandResult:
        """Persist the selected target's execution mode."""
        if self._sniper_daemon is None:
            return CommandResult(ok=False, message="sniper daemon is not available")
        try:
            policy = self._sniper_daemon.set_target_mode(target_id, mode)
        except SniperDaemonError as error:
            return CommandResult(ok=False, message=str(error))
        return CommandResult(
            ok=True,
            message=f"target {target_id} mode set to {mode.value}",
            data=policy,
        )

    def toggle_kill_switch(self) -> CommandResult:
        """Toggle new-entry blocking without disabling exits."""
        if self._sniper_daemon is None:
            return CommandResult(ok=False, message="sniper daemon is not available")
        active = self._sniper_daemon.toggle_kill_switch()
        return CommandResult(
            ok=True,
            message="kill switch active" if active else "kill switch cleared",
            data=active,
        )

    async def sell(self, market_id: str, fraction_ppm: int) -> CommandResult:
        """Execute an operator-requested manual risk reduction."""
        if self._sniper_daemon is None:
            return CommandResult(ok=False, message="sniper daemon is not available")
        try:
            result = await self._sniper_daemon.manual_sell(
                market_id,
                fraction_ppm=fraction_ppm,
            )
        except SniperDaemonError as error:
            return CommandResult(ok=False, message=str(error))
        return CommandResult(
            ok=True,
            message=result.error or "manual sell accepted",
            data=result,
        )

    def targets(
        self,
    ) -> tuple[tuple[FunderRecord, TargetExecutionPolicy | None], ...]:
        """Return tracked funders joined with their execution policies."""
        return tuple(
            (funder, self._repository.get_target_execution_policy(funder.address))
            for funder in self._repository.get_funders()
        )

    def funders(self) -> tuple[FunderRecord, ...]:
        """Return all registered root funders."""
        return self._repository.get_funders()

    def wallets(self) -> tuple[WalletRecord, ...]:
        """Return all tracked wallets."""
        return self._repository.get_wallets()

    def launches(self) -> tuple[LaunchRecord, ...]:
        """Return the latest verified launches."""
        return self._repository.get_launches()

    def positions(self) -> tuple[PaperPositionState, ...]:
        """Return open positions, or an empty tuple without a daemon."""
        if self._sniper_daemon is None:
            return ()
        return self._sniper_daemon.snapshot().open_positions

    def snapshot(self) -> SniperDaemonSnapshot | None:
        """Return the daemon snapshot, or None without a daemon."""
        if self._sniper_daemon is None:
            return None
        return self._sniper_daemon.snapshot()

    def subscribe(self, handler: Callable[[TrackerEvent], object]) -> None:
        """Subscribe a handler to every tracker event."""
        self._event_bus.subscribe("*", handler)

    def publish(self, event: TrackerEvent) -> None:
        """Publish one tracker event to all subscribers."""
        self._event_bus.publish(event)


__all__ = ["RugbotCore"]
