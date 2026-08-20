"""Shared UI composition factory building a RugbotCore from local state and config."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

from rugbot.core.rugbot_core import RugbotCore
from rugbot.runtime.config import SniperConfigError, load_sniper_config
from rugbot.runtime.event_bus import EventBus
from rugbot.runtime.tracker_service import TrackerService
from rugbot.storage.database import DatabaseManager
from rugbot.storage.tracker import SQLiteTrackerRepository
from rugbot.tracker.clock import SystemClock
from rugbot.tracker.engine import TrackerEngine
from rugbot.tracker.models import TargetExecutionMode, TargetExecutionPolicy

if TYPE_CHECKING:
    from pathlib import Path

    from rugbot.runtime.sniper_daemon import SniperDaemonService
    from rugbot.runtime.sniper_runtime import SniperRuntime


def build_ui_runtime(
    *,
    state_dir: Path,
    wallet: str | None = None,
    config_path: Path | None = None,
    sniper_runtime: SniperRuntime | None = None,
    sniper_daemon: SniperDaemonService | None = None,
) -> RugbotCore:
    """Build the shared UI runtime: tracker stack plus an optional sniper daemon.

    Mirrors the composition previously owned by ``RugbotTuiApp.__init__`` so the
    TUI and future bot adapters drive the same core. When ``config_path`` is
    provided, the configured watch target and its execution policy are seeded;
    otherwise a directly-provided ``wallet`` is seeded as a tracked funder.
    """
    if sniper_daemon is not None and sniper_runtime is not None:
        raise ValueError(  # noqa: TRY003
            "inject either sniper_daemon or sniper_runtime, not both"
        )
    daemon = sniper_runtime.daemon if sniper_runtime is not None else sniper_daemon

    db = DatabaseManager(state_dir / "rugbot.db")
    repository = SQLiteTrackerRepository(db)
    engine = TrackerEngine(clock=SystemClock())
    event_bus = EventBus()
    service = TrackerService(engine, repository, event_bus)

    core = RugbotCore(
        engine=engine,
        repository=repository,
        event_bus=event_bus,
        service=service,
        sniper_runtime=sniper_runtime,
        sniper_daemon=daemon,
    )
    if config_path is not None:
        _seed_configured_target(repository, service, config_path)
    elif wallet is not None and repository.get_funder(wallet) is None:
        service.add_funder(wallet, label="Configured target")
    return core


def _seed_configured_target(
    repository: SQLiteTrackerRepository,
    service: TrackerService,
    config_path: Path,
) -> None:
    """Seed the configured watch target and its execution policy."""
    try:
        config = load_sniper_config(config_path)
    except SniperConfigError:
        return
    address = config.target.id
    if repository.get_funder(address) is None:
        service.add_funder(address, label="Configured target")
    if repository.get_target_execution_policy(address) is not None:
        return
    take_profit = (
        config.rules.sell.take_profit_levels[0].trigger_pnl_ppm
        if config.rules.sell.take_profit_levels
        else 0
    )
    stop_loss = (
        config.rules.sell.stop_loss_levels[0].trigger_pnl_ppm
        if config.rules.sell.stop_loss_levels
        else 0
    )
    execution_mode = (
        TargetExecutionMode.LIVE
        if config.execution.mode.value == "live"
        else TargetExecutionMode.SIMULATED
        if config.execution.mode.value in {"paper", "simulation"}
        else TargetExecutionMode.OFF
    )
    service.save_target_execution_policy(
        TargetExecutionPolicy(
            funder_address=address,
            monitoring_enabled=True,
            execution_mode=execution_mode,
            quote_size_lamports=config.execution.quote_size_lamports,
            take_profit_pnl_ppm=take_profit,
            stop_loss_pnl_ppm=stop_loss,
            max_slippage_bps=config.execution.max_slippage_bps,
            priority_fee_microlamports=config.execution.priority_fee_microlamports,
            jito_tip_lamports=config.execution.jito_tip_lamports,
            updated_at=datetime.now(UTC).isoformat(),
        )
    )


__all__ = ["build_ui_runtime"]
