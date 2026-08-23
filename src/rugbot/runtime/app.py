"""Unified RugbotApp application composition root and facade."""

# ruff: noqa: BLE001, TRY003

from __future__ import annotations

import inspect
import os
from datetime import UTC, datetime
from typing import TYPE_CHECKING
from urllib.parse import urlsplit, urlunsplit

from rugbot.application.commands import COMMAND_REGISTRY, BotCommand, CommandResult
from rugbot.domain.entities import TargetRecord
from rugbot.ingest.pump.pump_stream import PumpPortalLaunchStream
from rugbot.runtime.config import SniperConfigError, load_sniper_config
from rugbot.runtime.event_bus import EventBus
from rugbot.runtime.workers.tracked_launch_observation import (
    TrackedLaunchObservationProducer,
)
from rugbot.storage.database import DatabaseManager
from rugbot.storage.tracker import SQLiteTrackerRepository
from rugbot.tracker.clock import SystemClock
from rugbot.tracker.cluster_graph_model import (
    ClusterIntelligenceModel,
    build_cluster_intelligence_model,
)
from rugbot.tracker.engine import TrackerEngine
from rugbot.tracker.funder_discovery import discover_funder
from rugbot.tracker.models import (
    FunderRecord,
    LaunchRecord,
    TargetExecutionMode,
    TargetExecutionPolicy,
    TransferRecord,
    WalletRecord,
)
from rugbot.tracker.screener import ScreenerService
from rugbot.tracker.service import TrackerService

SYSTEM_PROGRAM = "11111111111111111111111111111111"

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from rugbot.execution.position_runtime import PaperPositionState
    from rugbot.ingest.rpc_observer import RpcHttpTransport
    from rugbot.runtime.sniper_runtime import SniperRuntime
    from rugbot.runtime.workers.sniper_daemon import (
        SniperDaemonService,
        SniperDaemonSnapshot,
    )
    from rugbot.tracker.events import TrackerEvent


class RugbotApp:
    """Unified application facade exposing tracker, screener, and sniper services to any interface."""

    def __init__(  # noqa: PLR0913
        self,
        *,
        engine: TrackerEngine,
        repository: SQLiteTrackerRepository,
        event_bus: EventBus,
        service: TrackerService,
        database: DatabaseManager,
        screener: ScreenerService | None = None,
        launch_observation: TrackedLaunchObservationProducer | None = None,
        sniper_runtime: SniperRuntime | None = None,
        sniper_daemon: SniperDaemonService | None = None,
        owns_sniper: bool = False,
    ) -> None:
        self._engine = engine
        self._repository = repository
        self._event_bus = event_bus
        self._service = service
        self._database = database
        self._screener = screener or ScreenerService(tracker_service=service)
        if self._screener.tracker_service is None:
            self._screener.tracker_service = service
        self._launch_observation = launch_observation
        self._sniper_runtime = sniper_runtime
        self._sniper_daemon = sniper_daemon
        self._owns_sniper = owns_sniper
        self._closed = False

    @property
    def screener(self) -> ScreenerService:
        """Return the real-time token and developer cluster screener."""
        return self._screener

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
        """Return the attached sniper daemon service."""
        return self._sniper_daemon

    async def start(self) -> None:
        """Start background producers, observers, and the sniper daemon."""
        if self._launch_observation is not None:
            await self._launch_observation.start()
        if self._sniper_daemon is not None and self._owns_sniper:
            await self._sniper_daemon.start()

    async def close(self) -> None:
        """Release background tasks, close the observation producer, and shut down sniper."""
        if self._closed:
            return
        self._closed = True
        if self._launch_observation is not None:
            await self._launch_observation.stop()
        if self._sniper_daemon is not None and self._owns_sniper:
            await self._sniper_daemon.stop()
        self._database.close()

    async def stop(self) -> None:
        """Alias for close() on application teardown."""
        await self.close()

    def subscribe(
        self,
        event_type: str | Callable[[TrackerEvent], None],
        handler: Callable[[TrackerEvent], None] | None = None,
    ) -> Callable[[], None]:
        """Subscribe a callback to the unified event stream."""
        return self._event_bus.subscribe(event_type, handler)

    def execute(self, cmd: BotCommand) -> CommandResult:
        """Route a synchronous BotCommand through the universal COMMAND_REGISTRY."""
        handler = COMMAND_REGISTRY.get(cmd.name)
        if handler is None:
            return CommandResult(ok=False, message=f"unknown command: {cmd.name}")
        res = handler(self, cmd)
        if inspect.isawaitable(res):
            raise RuntimeError(f"command {cmd.name} requires async execution")
        return res

    async def aexecute(self, cmd: BotCommand) -> CommandResult:
        """Route an async or sync BotCommand through the universal COMMAND_REGISTRY."""
        handler = COMMAND_REGISTRY.get(cmd.name)
        if handler is None:
            return CommandResult(ok=False, message=f"unknown command: {cmd.name}")
        res = handler(self, cmd)
        if inspect.isawaitable(res):
            return await res
        return res

    def watch(self, funder_address: str, label: str = "") -> CommandResult:
        """Register a new root funder for descendant tracking."""
        try:
            self._service.add_funder(funder_address, label=label)
            return CommandResult(ok=True, message=f"watching {funder_address}")
        except Exception as exc:
            return CommandResult(ok=False, message=str(exc))

    def unwatch(self, funder_address: str) -> CommandResult:
        """Disable descendant tracking for a root funder."""
        try:
            self._service.remove_funder(funder_address)
            return CommandResult(ok=True, message=f"unwatched {funder_address}")
        except Exception as exc:
            return CommandResult(ok=False, message=str(exc))

    def get_funder(self, funder_address: str) -> FunderRecord | None:
        """Return a registered funder record by address."""
        return self._repository.get_funder(funder_address)

    def get_funders(self) -> list[FunderRecord]:
        """Return all registered root funders."""
        return self._repository.get_funders()

    def funders(self) -> list[FunderRecord]:
        """Alias for get_funders."""
        return self._repository.get_funders()

    def get_wallet(self, address: str) -> WalletRecord | None:
        """Return a tracked wallet node by address."""
        return self._repository.get_wallet(address)

    def get_wallets(self) -> list[WalletRecord]:
        """Return all tracked descendant wallet nodes."""
        return self._repository.get_wallets()

    def wallets(self) -> list[WalletRecord]:
        """Alias for get_wallets."""
        return self._repository.get_wallets()

    def get_descendant_wallets(self, funder_address: str) -> list[WalletRecord]:
        """Return all descendant wallets funded by a root funder."""
        return self._repository.get_wallets_by_root_funder(funder_address)

    def get_launch(self, mint: str) -> LaunchRecord | None:
        """Return a tracked launch record by mint address."""
        return self._repository.get_launch(mint)

    def get_launches(self) -> list[LaunchRecord]:
        """Return all tracked token creation events."""
        return self._repository.get_launches()

    def launches(self) -> list[LaunchRecord]:
        """Alias for get_launches."""
        return self._repository.get_launches()

    def get_launches_for_funder(self, funder_address: str) -> list[LaunchRecord]:
        """Return all launches attributed to a root funder."""
        return self._repository.get_launches_by_root_funder(funder_address)

    def get_transfers(self) -> list[TransferRecord]:
        """Return all observed funding transfer edges."""
        return self._repository.get_transfers()

    def transfers(self) -> list[TransferRecord]:
        """Alias for get_transfers."""
        return self._repository.get_transfers()

    def get_transfers_for_funder(self, funder_address: str) -> list[TransferRecord]:
        """Return all transfers belonging to a root funder tree."""
        return self._repository.get_transfers_by_root_funder(funder_address)

    def get_summary_stats(self) -> dict[str, int]:
        """Return counts of tracked funders, wallets, launches, and transfers."""
        return {
            "funders_count": len(self._repository.get_funders()),
            "wallets_count": len(self._repository.get_wallets()),
            "launches_count": len(self._repository.get_launches()),
            "transfers_count": len(self._repository.get_transfers()),
        }

    def targets(self) -> list[TargetRecord]:
        """Return all target entities with their execution policy and performance."""
        funders = self._repository.get_funders()
        records: list[TargetRecord] = []
        for f in funders:
            policy = self._repository.get_target_execution_policy(f.address)
            launches = self._repository.get_launches_by_root_funder(f.address)
            records.append(
                TargetRecord(
                    address=f.address,
                    label=f.label or "Target Dev",
                    policy=policy,
                    launches_count=len(launches),
                )
            )
        return records

    def get_target_execution_policy(
        self, funder_address: str
    ) -> TargetExecutionPolicy | None:
        """Return the target execution policy assigned to a funder."""
        return self._repository.get_target_execution_policy(funder_address)

    def save_target_execution_policy(self, policy: TargetExecutionPolicy) -> None:
        """Persist or update a target execution policy."""
        self._repository.save_target_execution_policy(policy)

    def set_target_mode(
        self, funder_address: str, mode: TargetExecutionMode
    ) -> CommandResult:
        """Set the target execution mode (OFF, SIMULATED, LIVE)."""
        policy = self._repository.get_target_execution_policy(funder_address)
        if policy is None:
            return CommandResult(ok=False, message=f"target {funder_address} not found")
        updated = TargetExecutionPolicy(
            funder_address=policy.funder_address,
            monitoring_enabled=policy.monitoring_enabled
            if mode != TargetExecutionMode.OFF
            else False,
            execution_mode=mode,
            quote_size_lamports=policy.quote_size_lamports,
            take_profit_pnl_ppm=policy.take_profit_pnl_ppm,
            stop_loss_pnl_ppm=policy.stop_loss_pnl_ppm,
            max_slippage_bps=policy.max_slippage_bps,
            priority_fee_microlamports=policy.priority_fee_microlamports,
            jito_tip_lamports=policy.jito_tip_lamports,
            updated_at=datetime.now(UTC).isoformat(),
        )
        self._repository.save_target_execution_policy(updated)
        return CommandResult(ok=True, message=f"target mode set to {mode.value}")

    def toggle_kill_switch(self) -> CommandResult:
        """Toggle the daemon safety kill switch."""
        if self._sniper_daemon is None:
            return CommandResult(ok=False, message="sniper daemon not attached")
        active = self._sniper_daemon.toggle_kill_switch()
        return CommandResult(ok=True, message=f"kill switch active={active}")

    def snapshot(self) -> SniperDaemonSnapshot | None:
        """Return point-in-time snapshot of the sniper daemon."""
        if self._sniper_daemon is None:
            return None
        return self._sniper_daemon.snapshot()

    def positions(self) -> tuple[PaperPositionState, ...]:
        """Return all open positions."""
        if self._sniper_daemon is None:
            return ()
        return self._sniper_daemon.open_positions

    async def sell(self, market_id: str, exit_ppm: int) -> CommandResult:
        """Execute a manual position exit."""
        if self._sniper_daemon is None:
            return CommandResult(ok=False, message="sniper daemon not attached")
        try:
            await self._sniper_daemon.exit_position_manual(market_id, exit_ppm)
            return CommandResult(ok=True, message=f"exited position {market_id}")
        except Exception as exc:
            return CommandResult(ok=False, message=str(exc))

    async def discover(self, mint_or_wallet: str) -> CommandResult:
        """Discover the root funder behind a mint or wallet."""
        try:
            result = await discover_funder(mint_or_wallet)
            return CommandResult(
                ok=True, message=f"funder: {result.root_funder}", data=result
            )
        except Exception as exc:
            return CommandResult(ok=False, message=str(exc))

    def get_cluster_intel(
        self, root_funder: str, root_label: str | None = None
    ) -> ClusterIntelligenceModel:
        """Build a complete cluster intelligence model."""
        return build_cluster_intelligence_model(
            self._repository, root_funder, root_label=root_label
        )

    def get_cluster_intelligence(
        self, root_funder: str, root_label: str | None = None
    ) -> ClusterIntelligenceModel:
        """Build a complete cluster intelligence model (alias)."""
        return build_cluster_intelligence_model(
            self._repository, root_funder, root_label=root_label
        )


def build_ui_runtime(  # noqa: PLR0913
    *,
    state_dir: Path,
    wallet: str | None = None,
    config_path: Path | None = None,
    sniper_runtime: SniperRuntime | None = None,
    sniper_daemon: SniperDaemonService | None = None,
    endpoint: str | None = None,
    websocket_endpoint: str | None = None,
    transport: RpcHttpTransport | None = None,
) -> RugbotApp:
    """Build the unified RugbotApp runtime."""
    if sniper_daemon is not None and sniper_runtime is not None:
        raise ValueError("inject either sniper_daemon or sniper_runtime, not both")
    daemon = sniper_runtime.daemon if sniper_runtime is not None else sniper_daemon

    db = DatabaseManager(state_dir / "rugbot.db")
    repository = SQLiteTrackerRepository(db)
    engine = TrackerEngine(clock=SystemClock())
    event_bus = EventBus()
    service = TrackerService(engine, repository, event_bus)
    resolved_endpoint = endpoint or _resolve_endpoint()
    screener = ScreenerService(
        tracker_service=service,
        endpoint=resolved_endpoint,
    )
    resolved_websocket_endpoint = websocket_endpoint or _resolve_websocket_endpoint(
        resolved_endpoint
    )
    launch_observation = None
    if resolved_endpoint:
        launch_observation = TrackedLaunchObservationProducer(
            service=service,
            repository=repository,
            endpoint=resolved_endpoint,
            websocket_endpoint=resolved_websocket_endpoint,
            pumpportal_stream=PumpPortalLaunchStream(),
            global_launch_handler=screener.nominate_live_launch,
            transport=transport,
        )

    app = RugbotApp(
        engine=engine,
        repository=repository,
        event_bus=event_bus,
        service=service,
        database=db,
        screener=screener,
        launch_observation=launch_observation,
        sniper_runtime=sniper_runtime,
        sniper_daemon=daemon,
        owns_sniper=False,
    )
    if config_path is not None:
        _seed_configured_target(repository, service, config_path)
    elif wallet is not None and repository.get_funder(wallet) is None:
        service.add_funder(wallet, label="Configured target")
    return app


def _resolve_endpoint() -> str | None:
    return os.environ.get("SOLANA_RPC_HTTP") or os.environ.get("RPC_ENDPOINT")


def _resolve_websocket_endpoint(http_endpoint: str | None) -> str | None:
    wss_env = os.environ.get("SOLANA_RPC_WSS") or os.environ.get("RPC_WSS_ENDPOINT")
    if wss_env:
        return wss_env
    if not http_endpoint:
        return None
    parsed = urlsplit(http_endpoint)
    scheme = "wss" if parsed.scheme == "https" else "ws"
    return urlunsplit(
        (scheme, parsed.netloc, parsed.path, parsed.query, parsed.fragment)
    )


def _normalize_execution_mode(mode: object) -> TargetExecutionMode:
    if isinstance(mode, TargetExecutionMode):
        return mode
    val = str(mode).lower()
    if val in ("observe", "off"):
        return TargetExecutionMode.OFF
    if val in ("live",):
        return TargetExecutionMode.LIVE
    return TargetExecutionMode.SIMULATED


def _seed_configured_target(
    repository: SQLiteTrackerRepository, service: TrackerService, config_path: Path
) -> None:
    try:
        cfg = load_sniper_config(config_path)
    except SniperConfigError:
        return
    funder = getattr(cfg.target, "funder_address", None) or getattr(
        cfg.target, "id", None
    )
    if not funder or funder == SYSTEM_PROGRAM:
        return
    if repository.get_funder(funder) is None:
        service.add_funder(funder, label="Configured target")
    raw_mode = getattr(cfg.target, "execution_mode", None) or (
        cfg.execution.mode.value if hasattr(cfg, "execution") else "simulated"
    )
    exec_mode = _normalize_execution_mode(raw_mode)
    quote_size = getattr(cfg.target, "quote_size_lamports", None) or (
        cfg.execution.quote_size_lamports if hasattr(cfg, "execution") else 10_000_000
    )
    policy = TargetExecutionPolicy(
        funder_address=funder,
        monitoring_enabled=getattr(cfg.target, "monitoring_enabled", True),
        execution_mode=exec_mode,
        quote_size_lamports=quote_size,
        take_profit_pnl_ppm=getattr(cfg.target, "take_profit_pnl_ppm", 1_000_000),
        stop_loss_pnl_ppm=getattr(cfg.target, "stop_loss_pnl_ppm", -300_000),
        max_slippage_bps=getattr(cfg.target, "max_slippage_bps", 500),
        priority_fee_microlamports=getattr(
            cfg.target, "priority_fee_microlamports", 50_000
        ),
        jito_tip_lamports=getattr(cfg.target, "jito_tip_lamports", 1_000_000),
        updated_at=datetime.now(UTC).isoformat(),
    )
    repository.save_target_execution_policy(policy)


__all__ = [
    "RugbotApp",
    "build_ui_runtime",
]
