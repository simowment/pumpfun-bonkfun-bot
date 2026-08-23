"""Canonical command registry and application command handlers."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from rugbot.domain.entities import TargetExecutionMode

MANUAL_HALF_EXIT_PPM = 500_000
MANUAL_FULL_EXIT_PPM = 1_000_000

if TYPE_CHECKING:
    from rugbot.runtime.app import RugbotApp


@dataclass(frozen=True, slots=True)
class BotCommand:
    """One UI-agnostic operator command."""

    name: str
    args: tuple[str, ...] = ()
    source: str = ""


@dataclass(frozen=True, slots=True)
class CommandResult:
    """Outcome of executing one BotCommand."""

    ok: bool
    message: str
    data: object | None = None


CommandHandler = Callable[
    ["RugbotApp", BotCommand], CommandResult | Awaitable[CommandResult]
]


def _watch(core: RugbotApp, cmd: BotCommand) -> CommandResult:
    """Map the watch command to core funder registration."""
    if not cmd.args:
        return CommandResult(ok=False, message="watch requires a wallet address")
    label = cmd.args[1] if len(cmd.args) > 1 else ""
    return core.watch(cmd.args[0], label=label)


def _unwatch(core: RugbotApp, cmd: BotCommand) -> CommandResult:
    """Map the unwatch command to core funder removal."""
    if not cmd.args:
        return CommandResult(ok=False, message="unwatch requires a wallet address")
    return core.unwatch(cmd.args[0])


def _status(core: RugbotApp, _cmd: BotCommand) -> CommandResult:
    """Project a compact operator status summary from the core."""
    snapshot = core.snapshot()
    if snapshot is None:
        stats = core.repository.get_summary_stats()
        message = (
            f"funders={stats['funders_count']} "
            f"wallets={stats['wallets_count']} "
            f"launches={stats['launches_count']}"
        )
        return CommandResult(ok=True, message=message, data=stats)
    message = (
        f"stage={snapshot.stage.value} "
        f"kill_switch={snapshot.kill_switch_active} "
        f"open_positions={len(snapshot.open_positions)} "
        f"message={snapshot.message}"
    )
    return CommandResult(ok=True, message=message, data=snapshot)


def _positions(core: RugbotApp, _cmd: BotCommand) -> CommandResult:
    """Map the positions command to the core open-positions query."""
    positions = core.positions()
    return CommandResult(
        ok=True,
        message=f"{len(positions)} open positions",
        data=positions,
    )


def _pause(core: RugbotApp, cmd: BotCommand) -> CommandResult:
    """Map the pause command to target mode OFF."""
    if not cmd.args:
        return CommandResult(ok=False, message="pause requires a target id")
    return core.set_target_mode(cmd.args[0], TargetExecutionMode.OFF)


def _resume(core: RugbotApp, cmd: BotCommand) -> CommandResult:
    """Map the resume command to target mode SIMULATED."""
    if not cmd.args:
        return CommandResult(ok=False, message="resume requires a target id")
    return core.set_target_mode(cmd.args[0], TargetExecutionMode.SIMULATED)


def _kill(core: RugbotApp, _cmd: BotCommand) -> CommandResult:
    """Map the kill command to the daemon kill switch."""
    return core.toggle_kill_switch()


async def _sell(core: RugbotApp, cmd: BotCommand) -> CommandResult:
    """Map the sell command to a full manual exit."""
    if not cmd.args:
        return CommandResult(ok=False, message="sell requires a market id")
    return await core.sell(cmd.args[0], MANUAL_FULL_EXIT_PPM)


async def _sell_half(core: RugbotApp, cmd: BotCommand) -> CommandResult:
    """Map the sell_half command to a half manual exit."""
    if not cmd.args:
        return CommandResult(ok=False, message="sell_half requires a market id")
    return await core.sell(cmd.args[0], MANUAL_HALF_EXIT_PPM)


async def _discover(core: RugbotApp, cmd: BotCommand) -> CommandResult:
    """Map the discover command to funder discovery."""
    if not cmd.args:
        return CommandResult(
            ok=False, message="discover requires a token mint or wallet address"
        )
    return await core.discover(cmd.args[0])


COMMAND_REGISTRY: dict[str, CommandHandler] = {
    "watch": _watch,
    "unwatch": _unwatch,
    "status": _status,
    "positions": _positions,
    "pause": _pause,
    "resume": _resume,
    "kill": _kill,
    "sell": _sell,
    "sell_half": _sell_half,
    "discover": _discover,
}

__all__ = [
    "COMMAND_REGISTRY",
    "BotCommand",
    "CommandHandler",
    "CommandResult",
]
