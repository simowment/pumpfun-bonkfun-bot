"""Terminal User Interface (TUI) application for Rugbot."""

# ruff: noqa: C901, PLR0913, PLR0915, PLR2004, F401, TC002, BLE001, S105, S110, TRY003, ANN401, FBT001, PLR0912

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import math
import os
import time
from collections.abc import Sequence
from dataclasses import replace
from datetime import UTC, datetime, timezone
from decimal import Decimal, InvalidOperation
from http import HTTPStatus
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar, Final

from rich.text import Text
from solders.pubkey import Pubkey
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Container, Horizontal, Vertical, VerticalScroll
from textual.events import Resize
from textual.widgets import (
    Button,
    Checkbox,
    DataTable,
    Footer,
    Input,
    Label,
    Static,
    TabbedContent,
    TabPane,
)

from rugbot.domain.decisions import AbstainReason, AbstainResult
from rugbot.ingest.rpc_observer import AiohttpRpcTransport
from rugbot.protocol.pump.models import TokenLaunch
from rugbot.protocol.solana.models import SolTransfer
from rugbot.runtime.config import (
    SniperConfigError,
    load_sniper_config,
    load_sniper_document,
    resolve_config_path,
    resolve_dotenv,
    resolve_state_dir,
    save_sniper_document,
)
from rugbot.runtime.event_bus import EventBus
from rugbot.runtime.tracker_service import TrackerService
from rugbot.runtime.wallet_intelligence import (
    MIN_REPEAT_LAUNCH_EVIDENCE,
    WalletIntelligenceReport,
    WalletLaunch,
    WalletLink,
    WalletNode,
    scan_wallet_intelligence,
)
from rugbot.storage.database import DatabaseManager
from rugbot.storage.sqlite_state_store import SqliteStateStore
from rugbot.storage.tracker import SQLiteTrackerRepository
from rugbot.tracker.clock import SystemClock
from rugbot.tracker.engine import TrackerEngine
from rugbot.tracker.events import (
    DecisionEvent,
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
    FundingHop,
    FundingPath,
    LaunchRecord,
    TargetExecutionMode,
    TargetExecutionPolicy,
    TargetRecord,
    TargetStrategy,
    TrackerConfig,
    TransferRecord,
    WalletRecord,
    WalletStatus,
)
from rugbot.tracker.queries import (
    build_funding_path,
    format_path_tree,
)
from rugbot.tui.formatters import (
    format_age,
    format_amount,
    format_assessment,
    format_flow,
    format_graph_map,
    format_network_endpoint,
    format_sol,
    format_timestamp,
    launch_matches,
    report_delta,
    short_address,
)
from rugbot.tui.widgets.activity import ActivityItem, EmptyStateView, LiveActivityView
from rugbot.tui.widgets.execution_rail import ExecutionCard
from rugbot.tui.widgets.graph_modal import ClusterGraphModal
from rugbot.tui.widgets.header import CompactHeader
from rugbot.tui.widgets.inspector import (
    DevHistoryCard,
    EventInspector,
    EventLogTicker,
    OperatorStage,
    RiskBar,
    TargetProfileCard,
    TokenDetailCard,
)
from rugbot.tui.widgets.modal import DetailInspectModal
from rugbot.tui.widgets.pnl import WalletPnlHistory, WalletPnlPanel
from rugbot.tui.widgets.position_panel import PositionExecutionPanel
from rugbot.tui.widgets.targets_table import TargetsTable
from rugbot.tui.widgets.wallet_risk import WalletRiskPanel
from rugbot.tui.widgets.watching import FunderCardInfo, WatchingView

if TYPE_CHECKING:
    from rugbot.ingest.rpc_observer import RpcHttpTransport
    from rugbot.runtime.sniper_daemon import SniperDaemonService
    from rugbot.runtime.sniper_runtime import SniperRuntime

__all__ = [
    "RugbotTuiApp",
    "WalletIntelApp",
    "format_age",
    "format_amount",
    "format_assessment",
    "format_flow",
    "format_graph_map",
    "format_network_endpoint",
    "format_sol",
    "format_timestamp",
    "launch_matches",
    "report_delta",
    "short_address",
]

_SYSTEM_PROGRAM_ID: Final[str] = "11111111111111111111111111111111"


class RugbotTuiApp(App[None]):
    """Main Textual Terminal User Interface for Rugbot."""

    CSS = """
    Screen {
        layout: vertical;
        background: #0d1117;
        color: #c9d1d9;
        overflow: hidden hidden;
    }

    #search-bar {
        dock: top;
        height: 3;
        display: none;
        padding: 0 1;
        background: $surface;
        border-bottom: solid $accent;
    }

    #compact-header {
        height: 1;
        width: 100%;
        background: #0d1117;
        padding: 0 1;
        overflow: hidden hidden;
    }

    #main-tabs {
        height: 1fr;
        width: 100%;
        background: #0d1117;
        overflow: hidden hidden;
    }

    #dashboard-layout {
        height: 100%;
        width: 100%;
        layout: vertical;
        background: #0d1117;
        overflow: hidden hidden;
    }

    #dashboard-top-row {
        height: 60%;
        width: 100%;
        layout: horizontal;
        overflow: hidden hidden;
    }

    #dashboard-top-row #targets-table {
        width: 28%;
        min-width: 24;
        height: 100%;
    }

    #dashboard-top-row #live-activity-view {
        width: 46%;
        height: 100%;
    }

    #dashboard-top-row #execution-card {
        width: 26%;
        min-width: 24;
        height: 100%;
    }

    #dashboard-bottom-row {
        height: 40%;
        width: 100%;
        layout: horizontal;
        overflow: hidden hidden;
        border-top: solid #21262d;
    }

    #dashboard-bottom-row #wallet-risk-panel {
        width: 28%;
        min-width: 24;
        height: 100%;
    }

    #dashboard-bottom-row #position-execution-panel {
        width: 72%;
        height: 100%;
    }

    #dashboard-layout.compact #dashboard-top-row {
        height: 100%;
        layout: vertical;
    }

    #dashboard-layout.compact #dashboard-top-row #targets-table {
        width: 100%;
        min-width: 0;
        height: 6;
    }

    #dashboard-layout.compact #dashboard-top-row #live-activity-view {
        width: 100%;
        height: 1fr;
    }

    #dashboard-layout.compact #dashboard-top-row #execution-card {
        width: 100%;
        min-width: 0;
        height: 10;
    }

    #dashboard-layout.compact #dashboard-bottom-row {
        display: none;
    }

    Footer {
        dock: bottom;
        background: #161b22;
        color: #8b949e;
    }

    #app-footer {
        display: none;
    }

    #footer-actions-bar {
        dock: bottom;
        height: 3;
        width: 100%;
        background: #161b22;
        color: #8b949e;
        border-top: solid #21262d;
        overflow: hidden hidden;
        padding: 0 1;
    }

    /* Settings Card Grid Layout */
    .settings-scroll-container {
        height: 1fr;
        width: 100%;
        overflow-y: auto;
        padding: 0 1 1 1;
    }

    .settings-grid-2x2 {
        height: auto;
        width: 100%;
        layout: vertical;
    }

    .settings-row {
        height: auto;
        width: 100%;
        layout: horizontal;
        margin-bottom: 1;
    }

    .settings-card {
        height: auto;
        width: 1fr;
        background: #0d1117;
        border: solid #21262d;
        padding: 0;
        margin-right: 1;
    }

    .settings-card:last-child {
        margin-right: 0;
    }

    #settings-grid.compact .settings-row {
        layout: vertical;
    }

    #settings-grid.compact .settings-card {
        width: 100%;
        margin-right: 0;
        margin-bottom: 1;
    }

    #settings-toolbar.compact #settings-status {
        display: none;
    }

    #settings-status.compact {
        display: none;
    }

    .card-header {
        height: 1;
        background: #161b22;
        color: #e3b341;
        text-style: bold;
        padding: 0 1;
        border-bottom: solid #21262d;
    }

    .card-body {
        height: auto;
        padding: 1;
    }

    .setting-line {
        height: 1;
        width: 100%;
        layout: horizontal;
        margin-bottom: 1;
    }

    .setting-label {
        width: 24;
        color: #8b949e;
        text-style: bold;
    }

    .setting-line Input {
        width: 1fr;
        height: 1;
        border: none;
        background: #161b22;
        color: #ffffff;
        padding: 0 1;
    }

    .setting-line Checkbox {
        width: 100%;
        height: 1;
        background: #161b22;
    }

    /* Target Launches History Split View */
    #launches-split-container {
        height: 1fr;
        width: 100%;
        layout: horizontal;
    }

    #launches-devs-col {
        width: 32;
        height: 100%;
        border-right: solid #21262d;
    }

    #launches-detail-col {
        width: 1fr;
        height: 100%;
    }

    #target-profile-card,
    #token-detail-card,
    #wallet-pnl-panel,
    #risk-bar,
    #event-log-ticker,
    .legacy-compat {
        display: none;
    }

    Tabs {
        height: 1;
        background: #0d1117;
        color: #8b949e;
        border-bottom: solid #21262d;
    }

    Tab {
        height: 1;
        padding: 0 2;
        color: #8b949e;
    }

    Tab.-active {
        color: #58a6ff;
        text-style: bold;
        background: #161b22;
    }

    #main-tabs {
        height: 1fr;
    }

    #sniper-container {
        height: 1fr;
        width: 100%;
        layout: horizontal;
    }

    #sniper-execution-col {
        width: 34%;
        min-width: 34;
        height: 100%;
    }

    #sniper-positions-col {
        width: 1fr;
        min-width: 46;
        height: 100%;
    }

    #sniper-container.compact {
        layout: vertical;
    }

    #sniper-container.compact #sniper-execution-col {
        width: 100%;
        height: 1fr;
    }

    #sniper-container.compact #sniper-positions-col {
        display: none;
    }

    #dashboard-container.compact {
        layout: vertical;
    }

    #dashboard-container.compact #dashboard-left-col {
        width: 100%;
        height: 7;
    }

    #dashboard-container.compact #dashboard-center-col {
        width: 100%;
        height: 1fr;
    }

    .hidden {
        display: none;
    }

    TabbedContent {
        height: 1fr;
        width: 100%;
    }

    ContentSwitcher {
        height: 1fr;
        width: 100%;
    }

    TabPane {
        height: 1fr;
        width: 100%;
        padding: 0 1;
    }

    .tab-body {
        height: 1fr;
        width: 100%;
        layout: vertical;
    }

    .toolbar-row {
        height: 3;
        width: 100%;
        padding: 0 1;
        align: left middle;
    }

    #settings-tab {
        height: 1fr;
        width: 100%;
        padding: 0 1;
    }

    .settings-scroll-container {
        height: 1fr;
        width: 100%;
        overflow-y: auto;
        padding-bottom: 2;
    }

    .form-grid {
        layout: grid;
        grid-size: 4;
        grid-columns: 1fr 1fr 1fr 1fr;
        grid-rows: auto;
        grid-gutter: 1 1;
        height: auto;
        width: 100%;
        padding: 0 0 1 0;
    }

    .form-field {
        height: auto;
        min-height: 2;
        width: 100%;
        margin-bottom: 0;
    }

    .form-field Label {
        height: 1;
        text-style: bold;
        color: #8b949e;
        margin-bottom: 0;
    }

    .form-field Input {
        height: 1;
        min-height: 1;
        width: 100%;
        border: none;
        background: #161b22;
        color: #ffffff;
    }

    .form-field Checkbox {
        height: 1;
        min-height: 1;
        width: 100%;
        margin-top: 0;
        background: #161b22;
    }

    .table-header {
        height: 1;
        width: 100%;
        background: $boost;
        color: $accent;
        text-style: bold;
        padding: 0 1;
    }

    .table-container {
        height: 1fr;
        width: 100%;
    }

    DataTable {
        height: 1fr;
        width: 100%;
    }

    #status {
        color: $text-muted;
    }
    """

    BINDINGS: ClassVar[list[Binding]] = [
        Binding("1", "show_tracker", "1: Dashboard", show=True, priority=True),
        Binding("2", "show_launches", "2: Dev History", show=True, priority=True),
        Binding("3", "show_sniper", "3: Sniper", show=True, priority=True),
        Binding("a", "add_target", "A: Add Dev", show=True, priority=True),
        Binding("f", "show_funding_graph", "F: Cluster Graph", show=True, priority=True),
        Binding("e", "context_action", "E: Edit Policy", show=True, priority=True),
        Binding("p", "pause_target", "P: Pause", show=True, priority=True),
        Binding("l", "toggle_live_trading", "L: Live/Sim", show=True, priority=True),
        Binding("h", "context_secondary_action", "H: Sell 50%", show=True, priority=True),
        Binding("x", "context_dismiss_action", "X: Exit 100%", show=True, priority=True),
        Binding("slash", "toggle_search", "/: Search", show=True, priority=True),
        Binding("q", "quit", "Q: Quit", show=True, priority=True),
        # Aliases / Background keys
        Binding("f1", "show_tracker", "Tracker", show=False, priority=True),
        Binding("f2", "show_backtester", "Backtest", show=False, priority=True),
        Binding("f3", "show_sniper", "Sniper", show=False, priority=True),
        Binding("f4", "show_settings", "Settings", show=False, priority=True),
        Binding("ctrl+p", "show_command_palette", "Commands", show=False, priority=True),
        Binding("s", "show_settings", "Settings", show=False, priority=True),
        Binding("n", "analyze_target", "Add Target", show=False, priority=True),
        Binding("b", "show_backtester", "Backtester", show=False, priority=True),
        Binding("d", "toggle_dry_run", "Dry Run", show=False, priority=True),
        Binding("w", "analyze_target", "Watch", show=False, priority=True),
        Binding("u", "quick_buy", "Quick Buy", show=False, priority=True),
        Binding("space", "toggle_pause_feed", "Pause Feed", show=False, priority=True),
        Binding("r", "refresh_view", "Refresh", show=False, priority=True),
        Binding(
            "ctrl+s",
            "save_target_policy",
            "Save Target Policy",
            show=False,
            priority=True,
        ),
        Binding("5", "show_funders", "Funders", show=False, priority=True),
        Binding("6", "show_wallets", "Wallets", show=False, priority=True),
        Binding("escape", "clear_focus", "Back", show=False, priority=True),
    ]

    def __init__(
        self,
        wallet: str | None = None,
        *,
        endpoint: str = "https://api.mainnet-beta.solana.com",
        websocket_endpoint: str | None = None,
        max_transactions: int = 100,
        max_linked_wallets: int = 8,
        refresh_seconds: int = 15,
        as_of_slot: int | None = None,
        config_path: Path = Path("watch.yaml"),
        state_dir: Path | None = None,
        theme: str = "textual-dark",
        enable_live: bool = False,
        transport: RpcHttpTransport | None = None,
        sniper_daemon: SniperDaemonService | None = None,
        sniper_runtime: SniperRuntime | None = None,
    ) -> None:
        super().__init__()
        self._wallet = wallet
        self._endpoint = endpoint
        self._websocket_endpoint = websocket_endpoint
        self._max_transactions = max_transactions
        self._max_linked_wallets = max_linked_wallets
        self._refresh_seconds = refresh_seconds
        self._as_of_slot = as_of_slot
        self._config_path = resolve_config_path(config_path)
        self._state_dir = resolve_state_dir(state_dir)
        self._pnl_history = WalletPnlHistory(self._state_dir / "wallet_pnl.jsonl")
        # PnL belongs to the execution wallet, never to the tracked developer.
        self._pnl_wallet_address = ""
        # This is a persisted request only; actual live execution still requires
        # CLI authorization, a matching env key, and every runtime safety gate.
        self._live_requested = bool(enable_live)
        self._simulation_requested = False
        self._enable_live = False
        self._transport = transport
        if sniper_daemon is not None and sniper_runtime is not None:
            raise ValueError("inject either sniper_daemon or sniper_runtime, not both")
        self._sniper_runtime = sniper_runtime
        self._sniper_daemon = (
            sniper_runtime.daemon if sniper_runtime is not None else sniper_daemon
        )
        self.theme = theme

        # Initialize SQLite database and domain tracker service
        db_path = self._state_dir / "rugbot.db"
        self._db = DatabaseManager(db_path)
        self._repository = SQLiteTrackerRepository(self._db)
        self._engine = TrackerEngine(clock=SystemClock())
        self._event_bus = EventBus()
        self._service = TrackerService(self._engine, self._repository, self._event_bus)

        # Activity cache for instant causal lookup
        self._activity_events: dict[str, ActivityItem] = {}
        # Live funder balances and token holdings cache
        self._funder_balances: dict[str, int] = {}
        self._funder_tokens: dict[str, list[dict[str, Any]]] = {}

    def compose(self) -> ComposeResult:
        # 1. Compact 2-line header
        yield CompactHeader(id="compact-header")

        # 2. Search bar (toggleable via '/')
        with Horizontal(id="search-bar"):
            yield Label("Search: ", classes="search-label")
            yield Input(
                placeholder="Search wallet, token, signature...",
                id="global-search-input",
            )

        # The three operational areas deliberately remain separate: tracker,
        # historical backtester, and execution sniper.
        with TabbedContent(id="main-tabs"):
            with TabPane("1: Dashboard", id="overview-tab"):
                with Vertical(id="dashboard-layout"):
                    with Horizontal(id="dashboard-top-row"):
                        yield TargetsTable(id="targets-table")
                        yield LiveActivityView(id="live-activity-view")
                        yield ExecutionCard(id="execution-card")
                    with Horizontal(id="dashboard-bottom-row"):
                        yield WalletRiskPanel(id="wallet-risk-panel")
                        yield PositionExecutionPanel(id="position-execution-panel")

            with TabPane("2: Dev History", id="launches-tab"):
                with Vertical(classes="tab-body"):
                    yield Static(
                        "DEV TOKEN CREATION HISTORY · CHRONOLOGICAL ON-CHAIN DETECTIONS",
                        classes="table-header",
                    )
                    with Horizontal(id="settings-toolbar", classes="toolbar-row"):
                        yield Button(
                            "← Back to Dashboard (Esc)",
                            variant="default",
                            id="launches-back-btn",
                            classes="back-to-dashboard-btn",
                        )
                        yield Input(
                            placeholder="Filter tokens by name, symbol, mint, or dev wallet...",
                            id="launch-filter",
                        )
                        yield Static(
                            "Select a developer from the left list to filter creation history.",
                            id="backtest-status",
                        )
                    with Horizontal(id="launches-split-container"):
                        with Vertical(id="launches-devs-col"):
                            yield Static("DEVELOPER WALLETS", classes="table-header")
                            yield DataTable(id="launches-devs-table", cursor_type="row")
                        with Vertical(id="launches-detail-col"):
                            yield Static(
                                "CREATED TOKENS & LAUNCH HISTORY",
                                classes="table-header",
                            )
                            yield DataTable(id="launches-table", cursor_type="row")

            with TabPane("3: Sniper", id="positions-tab"):
                with Horizontal(id="sniper-container"):
                    with Vertical(id="sniper-execution-col"):
                        yield ExecutionCard(id="sniper-execution-card")
                    with Vertical(id="sniper-positions-col"):
                        yield Static(
                            "ACTIVE & CLOSED POSITIONS (SIMULATED & LIVE)",
                            classes="table-header",
                        )
                        with Horizontal(classes="toolbar-row"):
                            yield Button(
                                "← Back to Dashboard (Esc)",
                                variant="default",
                                id="positions-back-btn",
                                classes="back-to-dashboard-btn",
                            )
                        with Container(classes="table-container"):
                            yield DataTable(id="positions-table", cursor_type="row")

            with TabPane("4: Settings", id="settings-tab"):
                with VerticalScroll(classes="settings-scroll-container"):
                    yield Static(
                        "SELECTED TARGET POLICY · WATCHER DEFAULTS",
                        classes="table-header",
                    )
                    with Horizontal(classes="toolbar-row"):
                        yield Button(
                            "← Back to Dashboard (Esc)",
                            variant="default",
                            id="settings-back-btn",
                            classes="back-to-dashboard-btn",
                        )
                        yield Button(
                            "Save Target Policy",
                            variant="success",
                            id="save-target-policy-btn",
                        )
                        yield Button(
                            "Save Watcher Defaults",
                            variant="default",
                            id="save-settings-btn",
                        )
                        yield Static("", id="settings-status")

                    with Vertical(id="settings-grid", classes="settings-grid-2x2"):
                        # Row 1: Sizing & Exits
                        with Horizontal(classes="settings-row"):
                            with Vertical(classes="settings-card"):
                                yield Static(
                                    "1. SELECTED TARGET & SIZING", classes="card-header"
                                )
                                with Vertical(classes="card-body"):
                                    with Horizontal(classes="setting-line"):
                                        yield Label(
                                            "Target Dev Wallet", classes="setting-label"
                                        )
                                        yield Input(
                                            id="target-wallet",
                                            placeholder="Solana Pubkey...",
                                            value=self._wallet or "",
                                        )
                                    with Horizontal(classes="setting-line"):
                                        yield Label(
                                            "Buy Size (SOL)", classes="setting-label"
                                        )
                                        yield Input(
                                            id="snipe-size-sol",
                                            placeholder="0.010",
                                            value="0.010",
                                        )
                                    with Horizontal(classes="setting-line"):
                                        yield Label(
                                            "Priority Fee (µL)", classes="setting-label"
                                        )
                                        yield Input(
                                            id="priority-fee",
                                            placeholder="50000",
                                            value="50000",
                                        )
                                    with Horizontal(classes="setting-line"):
                                        yield Label(
                                            "Jito MEV Tip (SOL)",
                                            classes="setting-label",
                                        )
                                        yield Input(
                                            id="jito-tip",
                                            placeholder="0.0010",
                                            value="0.0010",
                                        )
                                    with Horizontal(classes="setting-line"):
                                        yield Label(
                                            "Max Gas Cap (SOL)", classes="setting-label"
                                        )
                                        yield Input(
                                            id="max-gas-cap",
                                            placeholder="0.0050",
                                            value="0.0050",
                                        )

                            with Vertical(classes="settings-card"):
                                yield Static(
                                    "2. EXITS & SLIPPAGE", classes="card-header"
                                )
                                with Vertical(classes="card-body"):
                                    with Horizontal(classes="setting-line"):
                                        yield Label(
                                            "Take Profit (%)", classes="setting-label"
                                        )
                                        yield Input(
                                            id="take-profit-pct",
                                            placeholder="100.0",
                                            value="100.0",
                                        )
                                    with Horizontal(classes="setting-line"):
                                        yield Label(
                                            "Stop Loss (%)", classes="setting-label"
                                        )
                                        yield Input(
                                            id="stop-loss-pct",
                                            placeholder="-30.0",
                                            value="-30.0",
                                        )
                                    with Horizontal(classes="setting-line"):
                                        yield Label(
                                            "Max Slippage (BPS)",
                                            classes="setting-label",
                                        )
                                        yield Input(
                                            id="max-slippage",
                                            placeholder="500",
                                            value="500",
                                        )
                                    with Horizontal(classes="setting-line"):
                                        yield Label(
                                            "Snipe Delay (sec)", classes="setting-label"
                                        )
                                        yield Input(
                                            id="snipe-delay", placeholder="0", value="0"
                                        )
                                    with Horizontal(classes="setting-line"):
                                        yield Label(
                                            "No Activity Exit (s)",
                                            classes="setting-label",
                                        )
                                        yield Input(
                                            id="rule-no-activity",
                                            placeholder="0",
                                            value="0",
                                        )

                        # Row 2: Qualification & Routing
                        with Horizontal(classes="settings-row"):
                            with Vertical(classes="settings-card"):
                                yield Static(
                                    "3. QUALIFICATION & RULES", classes="card-header"
                                )
                                with Vertical(classes="card-body"):
                                    with Horizontal(classes="setting-line"):
                                        yield Label(
                                            "Max Entry MC ($ USD)",
                                            classes="setting-label",
                                        )
                                        yield Input(
                                            id="max-entry-mc",
                                            placeholder="15000",
                                            value="15000",
                                        )
                                    with Horizontal(classes="setting-line"):
                                        yield Label(
                                            "Min Dev Winrate (%)",
                                            classes="setting-label",
                                        )
                                        yield Input(
                                            id="min-winrate-pct",
                                            placeholder="40.0",
                                            value="40.0",
                                        )
                                    with Horizontal(classes="setting-line"):
                                        yield Label(
                                            "Max Consec Losses", classes="setting-label"
                                        )
                                        yield Input(
                                            id="rule-max-losses",
                                            placeholder="3",
                                            value="3",
                                        )
                                    with Horizontal(classes="setting-line"):
                                        yield Checkbox(
                                            "Require Block 0 Inclusion",
                                            id="require-block-zero",
                                            value=True,
                                        )
                                    with Horizontal(classes="setting-line"):
                                        yield Checkbox(
                                            "Require Funding Pattern Match",
                                            id="require-funding-match",
                                            value=True,
                                        )

                            with Vertical(classes="settings-card"):
                                yield Static(
                                    "4. EXECUTION & ROUTING", classes="card-header"
                                )
                                with Vertical(classes="card-body"):
                                    with Horizontal(classes="setting-line"):
                                        yield Label(
                                            "Routing Policy", classes="setting-label"
                                        )
                                        yield Input(
                                            id="routing-policy",
                                            placeholder="jito",
                                            value="jito",
                                        )
                                    with Horizontal(classes="setting-line"):
                                        yield Label(
                                            "Execution Mode", classes="setting-label"
                                        )
                                        yield Input(
                                            id="execution-mode",
                                            placeholder="observe",
                                            value="observe",
                                        )
                                    with Horizontal(classes="setting-line"):
                                        yield Label(
                                            "Target Kind", classes="setting-label"
                                        )
                                        yield Input(
                                            id="target-kind",
                                            placeholder="wallet",
                                            value="wallet",
                                        )
                                    with Horizontal(classes="setting-line"):
                                        yield Checkbox(
                                            "Live Trading Mode",
                                            id="execution-mode-live",
                                            value=self._live_requested,
                                        )
                                    with Horizontal(classes="setting-line"):
                                        yield Checkbox(
                                            "Buy Only Once",
                                            id="rule-buy-once",
                                            value=False,
                                        )

                    # Secondary parameter fields preserved in background for test compatibility
                    with Container(classes="legacy-compat"):
                        yield Input(id="compute-unit-limit", value="400000")
                        yield Input(id="loaded-accounts-limit", value="128000")
                        yield Input(id="signer-pubkey", value="")
                        yield Input(id="tracking-mode", value="new_token_creations")
                        yield Input(id="jito-url", value="")
                        yield Input(id="volume-bankroll", value="100000")
                        yield Input(id="volume-independent", value="25000")
                        yield Input(id="volume-impact", value="100000")
                        yield Input(id="strategy-min-volume", value="30000000000")
                        yield Input(id="strategy-max-creator-pairs", value="10")
                        yield Input(id="strategy-history-samples", value="10")
                        yield Input(id="strategy-max-buys-hour", value="1")
                        yield Input(id="strategy-max-entry-index", value="1")
                        yield Input(id="strategy-max-deviation", value="250000")
                        yield Checkbox(
                            "Bundle match", id="strategy-bundle", value=False
                        )
                        yield Checkbox(
                            "Double signature",
                            id="strategy-double-signature",
                            value=False,
                        )
                        yield Checkbox(
                            "Prior zero", id="strategy-prior-zero", value=False
                        )
                        yield Checkbox(
                            "Historical qualification",
                            id="strategy-historical",
                            value=False,
                        )
                        yield Input(id="rule-min-mc", value="")
                        yield Input(id="rule-max-mc", value="")
                        yield Input(id="rule-max-age", value="0")
                        yield Input(id="rule-cooldown", value="0")
                        for i in range(3):
                            yield Input(id=f"dip-{i}-drawdown", value="")
                            yield Input(id=f"dip-{i}-size", value="")
                        for i in range(5):
                            yield Input(id=f"tp-{i}-trigger", value="")
                            yield Input(id=f"tp-{i}-fraction", value="")
                            yield Input(id=f"sl-{i}-trigger", value="")
                            yield Input(id=f"sl-{i}-fraction", value="")
                            yield Input(id=f"trail-{i}-mc", value="")
                            yield Input(id=f"trail-{i}-drawdown", value="")
                        for i in range(3):
                            yield Input(id=f"big-{i}-min", value="")
                            yield Input(id=f"big-{i}-max", value="")
                            yield Input(id=f"big-{i}-fraction", value="")

            # Tab 5: Funders
            with TabPane("5: Funders", id="graph-tab"):
                with Vertical(classes="tab-body"):
                    yield Static("ROOT FUNDERS & CLUSTERS", classes="table-header")
                    with Horizontal(classes="toolbar-row"):
                        yield Button(
                            "← Back to Dashboard (Esc)",
                            variant="default",
                            id="graph-back-btn",
                            classes="back-to-dashboard-btn",
                        )
                        yield Input(
                            placeholder="Enter Funder Pubkey...", id="new-funder-input"
                        )
                        yield Button(
                            "Add Funder", variant="primary", id="add-funder-btn"
                        )
                        yield Input(placeholder="Wallet / Funder...", id="wallet-input")
                    with Container(classes="table-container"):
                        yield DataTable(id="nodes-table", cursor_type="row")

            # Tab 6: Wallets
            with TabPane("6: Wallets", id="edges-tab"):
                with Vertical(classes="tab-body"):
                    yield Static(
                        "FUNDING TREE & LINKED WALLETS", classes="table-header"
                    )
                    with Horizontal(classes="toolbar-row"):
                        yield Button(
                            "← Back to Dashboard (Esc)",
                            variant="default",
                            id="edges-back-btn",
                            classes="back-to-dashboard-btn",
                        )
                    with Container(classes="table-container"):
                        yield DataTable(id="edges-table", cursor_type="row")

        # Kept mounted for existing inspection APIs; never part of the dashboard layout.
        yield Static(
            "[bold cyan]↑↓[/bold cyan] Target   [bold cyan]Enter[/bold cyan] Inspect   [bold cyan]/[/bold cyan] Search   [bold cyan]F1[/bold cyan] Tracker   [bold cyan]F2[/bold cyan] Backtester   [bold cyan]F3[/bold cyan] Sniper   [bold cyan]F4[/bold cyan] Settings   [bold cyan]Ctrl+P[/bold cyan] Commands   [bold cyan]Q[/bold cyan] Quit",
            id="global-footer-text",
            classes="legacy-compat",
        )
        yield RiskBar(id="risk-bar", classes="legacy-compat")
        yield Footer(id="app-footer")
        yield Static("", id="footer-actions-bar")
        yield WalletPnlPanel(id="wallet-pnl-panel", classes="legacy-compat")
        yield TargetProfileCard(id="target-profile-card", classes="legacy-compat")
        yield TokenDetailCard(id="token-detail-card", classes="hidden legacy-compat")
        yield EventInspector(id="event-inspector", classes="hidden")
        yield WatchingView(id="watching-view", classes="hidden")
        yield Static("", id="status", classes="hidden")
        yield Static("", id="flow-panel", classes="hidden")
        yield Static("", id="graph-map", classes="hidden")

    def on_mount(self) -> None:
        """Hydrate data tables and start live tracking listeners."""
        # Initialize tables
        self._init_launches_table()
        self._init_nodes_table()
        self._init_edges_table()
        self._init_positions_table()

        # Subscribe to EventBus domain events
        self._event_bus.subscribe("*", self._on_domain_event)

        # Purge placeholder system program funder if present
        with contextlib.suppress(Exception):
            with self._db.cursor() as cur:
                cur.execute(
                    "DELETE FROM tracker_funders WHERE address = ?",
                    (_SYSTEM_PROGRAM_ID,),
                )
                cur.execute(
                    "DELETE FROM tracker_wallets WHERE address = ? OR root_funder = ?",
                    (
                        _SYSTEM_PROGRAM_ID,
                        _SYSTEM_PROGRAM_ID,
                    ),
                )
        # Register initial funder if provided
        if self._wallet and self._wallet != _SYSTEM_PROGRAM_ID:
            try:
                Pubkey.from_string(self._wallet)
                self._service.add_funder(self._wallet, label="Target Dev")
            except Exception:
                with contextlib.suppress(Exception):
                    self.query_one("#status", Static).update(
                        "ABSTAIN: INVALID TARGET WALLET"
                    )

        activity_view = self.query_one("#live-activity-view", LiveActivityView)
        targets_table = self.query_one("#targets-table", TargetsTable)

        self._ensure_config_target_policy()
        real_targets = self._target_records()
        targets_table.set_targets(real_targets)
        if real_targets:
            for card_id in ("execution-card", "sniper-execution-card"):
                with contextlib.suppress(Exception):
                    self.query_one(f"#{card_id}", ExecutionCard).update_target(
                        real_targets[0]
                    )
            with contextlib.suppress(Exception):
                activity_view.update_summary(
                    real_targets[0].address,
                    self._target_policy_mode_label(real_targets[0]),
                )

        # Check existing launches from SQLite database
        existing_launches = self._repository.get_launches(limit=50)
        if existing_launches:
            for launch in reversed(existing_launches):
                item = ActivityItem(
                    row_id=f"launch_{launch.mint}",
                    timestamp=launch.created_at,
                    event_type="LAUNCH",
                    root_funder=launch.root_funder,
                    target_wallet=launch.creator_wallet,
                    token_symbol=launch.symbol,
                    token_name=launch.name,
                    token_mint=launch.mint,
                    market_cap_usd=None,
                    signal="DETECTED",
                )
                self._activity_events[item.row_id] = item
                activity_view.add_event(item)

        # Hydrate funders in Activity empty state and tables
        funders = [
            f.address
            for f in self._repository.get_funders(enabled_only=True)
            if f.address != _SYSTEM_PROGRAM_ID
        ]
        activity_view.set_funders(funders)

        self._refresh_tables()
        self._refresh_watching_view()
        self._refresh_header_counts()
        self._load_settings_complete()
        self._apply_responsive_layout(self.size.width)
        self.query_one("#targets-datatable", DataTable).focus()
        self._refresh_footer_actions()

        if self._sniper_daemon is not None:
            self.run_worker(
                self._start_sniper_daemon(),
                name="sniper_daemon_start",
            )
        # Start live observation worker
        self.run_worker(self._poll_observation_worker(), name="observation_worker")

    async def _start_sniper_daemon(self) -> None:
        """Start recovery and exit management for an injected local daemon."""

        try:
            await self._sniper_daemon.start()
            targets = self._target_records()
            if targets:
                await self._sniper_daemon.refresh_wallet_risk(targets[0].address)
                self._refresh_sniper_runtime()
        except Exception as error:
            self.notify(
                f"Sniper daemon failed to start: {type(error).__name__}",
                severity="error",
            )
            raise

    async def on_unmount(self) -> None:
        """Release the project-owned SQLite connection on application exit."""

        if self._sniper_runtime is not None:
            await self._sniper_runtime.close()
        elif self._sniper_daemon is not None:
            await self._sniper_daemon.stop()
        self._db.close()

    def _target_records(self) -> list[TargetRecord]:
        """Project persisted tracker funders and their policies into the TUI."""
        return [
            TargetRecord(
                address=funder.address,
                label=funder.label or "Tracked funder",
                policy=self._repository.get_target_execution_policy(funder.address),
            )
            for funder in self._repository.get_funders(enabled_only=False)
            if funder.address != _SYSTEM_PROGRAM_ID
        ]

    def _ensure_config_target_policy(self) -> None:
        """Seed the active watch target from the verified watcher configuration."""
        try:
            config = load_sniper_config(self._config_path)
        except SniperConfigError:
            return
        address = config.target.id
        if self._repository.get_funder(address) is None:
            self._service.add_funder(address, label="Configured target")
        if self._repository.get_target_execution_policy(address) is not None:
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
        self._service.save_target_execution_policy(
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

    def _refresh_target_records(self) -> None:
        """Refresh target projections after a persisted policy mutation."""
        table = self.query_one("#targets-table", TargetsTable)
        targets = self._target_records()
        table.set_targets(targets)
        selected = table.get_selected_target()
        if selected is not None:
            for card_id in ("execution-card", "sniper-execution-card"):
                with contextlib.suppress(Exception):
                    self.query_one(f"#{card_id}", ExecutionCard).update_target(selected)
        self._refresh_header_counts()

    def _update_selected_target_policy(self, **changes: object) -> TargetRecord | None:
        """Persist a selected target policy change before rendering it."""
        table = self.query_one("#targets-table", TargetsTable)
        target = table.get_selected_target()
        if target is None or target.policy is None:
            self.notify(
                "Select a configured target before changing execution",
                severity="warning",
            )
            return None
        policy = replace(
            target.policy, updated_at=datetime.now(UTC).isoformat(), **changes
        )
        self._service.save_target_execution_policy(policy)
        target.policy = policy
        table.update_target(target)
        for card_id in ("execution-card", "sniper-execution-card"):
            with contextlib.suppress(Exception):
                self.query_one(f"#{card_id}", ExecutionCard).update_target(target)
        self._refresh_header_counts()
        return target

    def on_resize(self, event: Resize) -> None:
        """Handle responsive terminal breakpoints."""
        self._apply_responsive_layout(event.size.width)
        try:
            self.query_one("#event-inspector", EventInspector).apply_responsive_layout(
                event.size.width
            )
        except Exception:
            pass
        self._refresh_footer_actions()

    def _apply_responsive_layout(self, width: int) -> None:
        """Keep tracker and sniper actions readable on compact terminals."""
        compact = width < 110
        for widget_id in (
            "dashboard-layout",
            "dashboard-container",
            "sniper-container",
            "settings-grid",
            "settings-toolbar",
            "settings-status",
        ):
            with contextlib.suppress(Exception):
                widget = self.query_one(f"#{widget_id}")
                widget.set_class(compact, "compact")

    @staticmethod
    def _format_full_width_shortcuts(
        items: tuple[tuple[str, str], ...],
        width: int,
    ) -> str:
        """Distribute shortcuts across the full available line width."""
        if not items:
            return ""
        if len(items) == 1:
            key, label = items[0]
            return f"[bold cyan]{key}[/bold cyan] {label}"

        # Plain text length of each shortcut item: f"{key} {label}"
        item_lengths = [len(key) + 1 + len(label) for key, label in items]
        total_text_len = sum(item_lengths)
        gaps_count = len(items) - 1

        # Target width accounting for padding (1 char on each side)
        target_width = max(total_text_len + gaps_count * 2, width - 2)
        extra_space = target_width - total_text_len

        if extra_space >= gaps_count * 2 and gaps_count > 0:
            base_gap = extra_space // gaps_count
            remainder = extra_space % gaps_count

            parts: list[str] = []
            for i, (key, label) in enumerate(items):
                parts.append(f"[bold cyan]{key}[/bold cyan] {label}")
                if i < gaps_count:
                    gap_size = base_gap + (1 if i < remainder else 0)
                    parts.append(" " * gap_size)
            return "".join(parts)

        return "  ".join(
            f"[bold cyan]{key}[/bold cyan] {label}" for key, label in items
        )

    def _refresh_footer_actions(self) -> None:
        """Render the global and contextual keyboard maps for the active tab."""
        try:
            active_tab = self.query_one(TabbedContent).active
        except Exception:
            active_tab = "overview-tab"

        context = {
            "overview-tab": (
                ("UP/DN", "SELECT"),
                ("ENTER", "POLICY"),
                ("A", "ADD"),
                ("E", "EDIT"),
                ("P", "PAUSE"),
                ("D", "SIM"),
                ("ESC", "BACK"),
            ),
            "launches-tab": (
                ("UP/DN", "SELECT"),
                ("ENTER", "DETAILS"),
                ("F1", "TRACKER"),
                ("F3", "SNIPER"),
                ("ESC", "BACK"),
            ),
            "positions-tab": (
                ("D", "DRY RUN"),
                ("P", "PAUSE"),
                ("E", "EDIT/EXIT"),
                ("L", "REQUEST LIVE"),
                ("H", "HISTORY/SELL"),
                ("ESC", "BACK"),
            ),
            "settings-tab": (
                ("TAB", "NEXT"),
                ("SHIFT+TAB", "PREV"),
                ("CTRL+S", "SAVE POLICY"),
                ("ESC", "TRACKER"),
            ),
        }.get(active_tab, ())
        global_actions = (
            ("F1", "TRACK"),
            ("F2", "BACKTEST"),
            ("F3", "SNIPER"),
            ("F4", "SETTINGS"),
            ("/", "SEARCH"),
            ("R", "REFRESH"),
            ("Q", "QUIT"),
        )
        width = self.size.width if self.size and self.size.width > 0 else 120
        if width < 100:
            global_actions = (
                ("F1", "TRACK"),
                ("F2", "BACKTEST"),
                ("F3", "SNIPER"),
                ("F4", "SETTINGS"),
                ("Q", "QUIT"),
            )
            context = context[:5]
        global_line = self._format_full_width_shortcuts(global_actions, width)
        context_line = self._format_full_width_shortcuts(context, width)
        lines = [line for line in (global_line, context_line) if line]
        with contextlib.suppress(Exception):
            self.query_one("#footer-actions-bar", Static).update("\n".join(lines))

    def on_watching_view_funder_selected(
        self, message: WatchingView.FunderSelected
    ) -> None:
        """Update inspect panel when a funder is selected in WATCHING list."""
        try:
            wallets = self._repository.get_wallets()
            launches = self._repository.get_launches()
            desc_count = len(
                [w for w in wallets if w.root_funder == message.funder_address]
            )
            lnch_count = len(
                [
                    launch
                    for launch in launches
                    if launch.root_funder == message.funder_address
                ]
            )
            watching_view = self.query_one("#watching-view", WatchingView)
            watching_view.selected_funder = message.funder_address
            watching_view.selected_funder_label = message.label
            watching_view.selected_funder_descendants_count = desc_count
            watching_view.selected_funder_launches_count = lnch_count
            watching_view.selected_funder_balance_sol = (
                self._funder_balances.get(message.funder_address, 0) / 1e9
            )
            watching_view.selected_funder_tokens = self._funder_tokens.get(
                message.funder_address, []
            )
            with contextlib.suppress(Exception):
                funder_profile = self.query_one(
                    "#target-profile-card", TargetProfileCard
                )
                funder_profile.update_funder(
                    address=message.funder_address,
                    label=message.label,
                    descendants_count=desc_count,
                    launches_count=lnch_count,
                    balance_sol=self._funder_balances.get(message.funder_address, 0)
                    / 1e9,
                )
        except Exception:
            pass

    def on_targets_table_target_selected(
        self, message: TargetsTable.TargetSelected
    ) -> None:
        """Update execution cockpit when a target is highlighted in TargetsTable."""
        for card_id in ("execution-card", "sniper-execution-card"):
            with contextlib.suppress(Exception):
                self.query_one(f"#{card_id}", ExecutionCard).update_target(
                    message.target
                )
        self._selected_target = message.target
        with contextlib.suppress(Exception):
            self.query_one("#live-activity-view", LiveActivityView).update_summary(
                message.target.address,
                self._target_policy_mode_label(message.target),
            )

    @staticmethod
    def _target_policy_mode_label(target: TargetRecord) -> str:
        """Return an operator label for one persisted target policy."""
        policy = target.policy
        if policy is None:
            return "UNCONFIGURED"
        if not policy.monitoring_enabled:
            return "PAUSED"
        if policy.execution_mode is TargetExecutionMode.SIMULATED:
            return "DRY RUN"
        if policy.execution_mode is TargetExecutionMode.LIVE:
            return "LIVE"
        return "OBSERVE"

    def on_live_activity_view_event_selected(
        self, message: LiveActivityView.EventSelected
    ) -> None:
        """Update live preview on the right when an activity row is highlighted."""
        item = message.item
        with contextlib.suppress(Exception):
            rail = self.query_one("#execution-card", ExecutionCard)
            if item is not None and item.event_type.upper() in {
                "EXEC",
                "SIM",
                "SIMULATED",
            }:
                rail.update_item(item)
            else:
                rail.update_item(None)

        with contextlib.suppress(Exception):
            detail_card = self.query_one("#token-detail-card", TokenDetailCard)
            path = (
                build_funding_path(item.target_wallet, self._repository)
                if item
                else None
            )
            detail_card.update_item(item, path)

            if item is not None:
                detail_card.set_stage(
                    OperatorStage.POSITION_OPEN
                    if item.event_type.upper().startswith("BUY")
                    else OperatorStage.CANDIDATE
                    if "LAUNCH" in item.event_type.upper()
                    else OperatorStage.ARMED
                )

        with contextlib.suppress(Exception):
            inspector = self.query_one("#event-inspector", EventInspector)
            path = (
                build_funding_path(item.target_wallet, self._repository)
                if item
                else None
            )
            inspector.update_selection(item, path)

    def on_live_activity_view_full_inspect_requested(
        self, message: LiveActivityView.FullInspectRequested
    ) -> None:
        """Use Enter for candidate simulation and inspection everywhere else."""
        rail = self.query_one("#execution-card", ExecutionCard)
        if rail.stage == OperatorStage.CANDIDATE:
            self.action_simulate_candidate()
            return
        path = build_funding_path(message.item.target_wallet, self._repository)
        self.push_screen(DetailInspectModal(message.item, path))

    def _on_domain_event(self, event: TrackerEvent) -> None:
        """Process tracker domain events from the service."""
        event_type = getattr(event, "event_type", type(event).__name__)
        row_id = f"ev_{event.timestamp}_{event.wallet[:8]}_{event_type}_{id(event)}"

        token_sym = "—"
        token_name = ""
        token_mint = ""
        amount = None
        sig = ""
        reason = ""
        latency_summary = ""

        if isinstance(event, DecisionEvent):
            event_type = event.kind
            token_sym = event.token_symbol or "TOKEN"
            token_mint = event.token_mint
            reason = event.reason
            latency_summary = event.latency_summary or ""
        elif isinstance(event, LaunchDetected):
            token_sym = event.data.get("symbol", "PUMP")
            token_name = event.data.get("name", "")
            token_mint = event.data.get("mint", "")
            sig = event.data.get("signature", "")
        elif isinstance(event, (TransferDetected, WalletFunded)):
            amount = event.data.get("lamports")
            sig = event.data.get("signature", "")

        depth = event.data.get("depth", 1)

        item = ActivityItem(
            row_id=row_id,
            timestamp=event.timestamp,
            event_type=event_type,
            root_funder=event.root_funder,
            target_wallet=event.wallet,
            token_symbol=token_sym,
            token_name=token_name,
            token_mint=token_mint,
            amount_lamports=amount,
            hops=depth,
            signature=sig,
            meta=event.data,
            reason=reason,
            latency_summary=latency_summary,
            signal=event_type,
        )
        self._activity_events[row_id] = item

        # Incremental insert into LiveActivityView
        try:
            self.query_one("#live-activity-view", LiveActivityView).add_event(item)
        except Exception:
            pass

        self._refresh_tables()
        self._refresh_watching_view()
        self._refresh_header_counts()

    def _refresh_header_counts(self) -> None:
        """Update 2-line header telemetry counts."""
        try:
            header = self.query_one("#compact-header", CompactHeader)
            funders = [
                f
                for f in self._repository.get_funders(enabled_only=True)
                if f.address != _SYSTEM_PROGRAM_ID
            ]
            wallets = self._repository.get_wallets()
            non_funder_wallets = [
                w for w in wallets if not any(f.address == w.address for f in funders)
            ]
            launches = self._repository.get_launches()
            header.funders_count = len(funders)
            header.wallets_count = len(funders) + len(non_funder_wallets)
            header.launches_count = len(launches)
            header.execution_mode = self._current_execution_mode()
            header.active_positions_count = (
                len(self._sniper_daemon.snapshot().open_positions)
                if self._sniper_daemon is not None
                else 0
            )
            with contextlib.suppress(Exception):
                table = self.query_one("#targets-table", TargetsTable)
                header.active_targets_count = table.get_active_targets_count()
        except Exception:
            pass

    def _current_execution_mode(self) -> str:
        """Return the canonical mode currently loaded by the dashboard."""
        if self._enable_live or self._live_requested:
            return "LIVE"
        with contextlib.suppress(Exception):
            val = self.query_one("#execution-mode", Input).value.strip().upper()
            if val in {"LIVE", "REAL"}:
                return "LIVE"
            if val in {"PAUSED", "OFF", "STANDBY"}:
                return "PAUSED"
        return "DRY RUN"

    # --- Actions and Keybindings ---
    def action_show_tracker(self) -> None:
        """Show finalized wallet and launch tracking."""
        with contextlib.suppress(Exception):
            self.query_one(TabbedContent).active = "overview-tab"
        self._refresh_tables()
        self._refresh_header_counts()
        self._refresh_footer_actions()
        with contextlib.suppress(Exception):
            self.query_one("#targets-datatable", DataTable).focus()

    def action_show_overview(self) -> None:
        """Show the tracker primary screen."""
        self.action_show_tracker()

    def action_show_backtester(self) -> None:
        """Show the finalized historical evidence available for backtesting."""
        self.query_one(TabbedContent).active = "launches-tab"

    def action_show_launches(self) -> None:
        """Show the backtester historical launches table."""
        self.action_show_backtester()

    def action_show_funders(self) -> None:
        self.query_one(TabbedContent).active = "graph-tab"

    def action_show_wallets(self) -> None:
        self.query_one(TabbedContent).active = "edges-tab"

    def action_show_sniper(self) -> None:
        """Show per-target execution state and positions."""
        self.query_one(TabbedContent).active = "positions-tab"

    def action_show_positions(self) -> None:
        """Show the sniper execution screen."""
        self.action_show_sniper()

    def action_show_settings(self) -> None:
        self.query_one(TabbedContent).active = "settings-tab"

    def action_show_graph(self) -> None:
        self.query_one(TabbedContent).active = "graph-tab"

    def action_focus_wallet(self) -> None:
        with contextlib.suppress(Exception):
            self.query_one("#wallet-input", Input).focus()

    def action_toggle_search(self) -> None:
        search_bar = self.query_one("#search-bar", Horizontal)
        search_bar.display = not search_bar.display
        if search_bar.display:
            self.query_one("#global-search-input", Input).focus()

    def action_clear_focus(self) -> None:
        """Clear search or exit secondary tabs back to Dashboard."""
        search_bar = self.query_one("#search-bar", Horizontal)
        if search_bar.display:
            search_bar.display = False
            self.query_one("#global-search-input", Input).value = ""
        with contextlib.suppress(Exception):
            tabbed = self.query_one(TabbedContent)
            if tabbed.active != "overview-tab":
                self.action_show_tracker()
        self.query_one("#live-activity-view", LiveActivityView).resume_follow()

    def action_refresh_view(self) -> None:
        self._refresh_tables()
        self._refresh_header_counts()

    def action_show_help(self) -> None:
        self.action_show_command_palette()

    def action_show_command_palette(self) -> None:
        """Expose secondary panels without keeping tabs in the primary workflow."""
        self.notify(
            "Panels: F1 Tracker · F2 Backtester · F3 Sniper · S Settings",
            severity="information",
        )

    def action_add_target(self) -> None:
        """Open a blank target address while retaining policy values as a template."""
        self.action_show_settings()
        self._set_setting("target-wallet", "")
        with contextlib.suppress(Exception):
            self.query_one("#settings-status", Static).update(
                "[bold cyan]Enter a target wallet, configure its policy, then Ctrl+S.[/bold cyan]"
            )
            self.query_one("#target-wallet", Input).focus()

    def action_save_target_policy(self) -> None:
        """Save the visible target policy only from the Settings workflow."""
        if self.query_one(TabbedContent).active != "settings-tab":
            self.notify(
                "Ctrl+S saves a target policy from Settings",
                severity="warning",
            )
            return
        self._save_target_policy()

    def action_watch_wallet(self) -> None:
        """Switch to Settings tab to configure or add target."""
        self.action_analyze_target()

    def action_analyze_target(self) -> None:
        """Switch to Settings tab to configure or add target without overlay popup."""
        activity_view = self.query_one("#live-activity-view", LiveActivityView)
        row_id = activity_view.selected_row_id
        if row_id and row_id in self._activity_events:
            initial_addr = self._activity_events[row_id].target_wallet
            with contextlib.suppress(Exception):
                self.query_one("#target-wallet", Input).value = initial_addr
        else:
            with contextlib.suppress(Exception):
                targets_table = self.query_one("#targets-table", TargetsTable)
                target = targets_table.get_selected_target()
                if target:
                    self._load_target_policy_fields(target)

        self.query_one(TabbedContent).active = "settings-tab"
        with contextlib.suppress(Exception):
            self.query_one("#target-wallet", Input).focus()

    def _load_target_policy_fields(self, target: TargetRecord) -> None:
        """Load the selected funder's persisted execution policy into Settings."""
        self._set_setting("target-wallet", target.address)
        if target.policy is None:
            with contextlib.suppress(Exception):
                self.query_one("#settings-status", Static).update(
                    "[yellow]Tracker-only target: enter its policy, then save it.[/yellow]"
                )
            return
        policy = target.policy
        self._set_setting(
            "snipe-size-sol", self._format_lamports_sol(policy.quote_size_lamports)
        )
        self._set_setting(
            "take-profit-pct", self._format_ppm_percent(policy.take_profit_pnl_ppm)
        )
        self._set_setting(
            "stop-loss-pct", self._format_ppm_percent(policy.stop_loss_pnl_ppm)
        )
        self._set_setting("max-slippage", policy.max_slippage_bps)
        self._set_setting("priority-fee", policy.priority_fee_microlamports)
        self._set_setting(
            "jito-tip", self._format_lamports_sol(policy.jito_tip_lamports)
        )
        self._set_setting("execution-mode", policy.execution_mode.value)
        self._set_checkbox(
            "execution-mode-live", policy.execution_mode is TargetExecutionMode.LIVE
        )

    def _policy_from_settings(self, funder_address: str) -> TargetExecutionPolicy:
        """Validate one target-local policy from the visible Settings controls."""
        try:
            Pubkey.from_string(funder_address)
        except ValueError as error:
            raise SniperConfigError(
                "target-wallet is not a valid Solana Pubkey"
            ) from error

        mode = self._setting_text("execution-mode").lower()
        if self._setting_bool("execution-mode-live"):
            mode = "live"
        mode_by_name = {
            "observe": TargetExecutionMode.OFF,
            "paper": TargetExecutionMode.SIMULATED,
            "simulation": TargetExecutionMode.SIMULATED,
            "live": TargetExecutionMode.LIVE,
        }
        if mode not in mode_by_name:
            raise SniperConfigError(
                "execution-mode must be observe, paper, simulation, or live"
            )
        jito_tip = self._setting_decimal("jito-tip")
        return TargetExecutionPolicy(
            funder_address=funder_address,
            monitoring_enabled=True,
            execution_mode=mode_by_name[mode],
            quote_size_lamports=self._lamports("snipe-size-sol"),
            take_profit_pnl_ppm=abs(self._ppm_percent("take-profit-pct")),
            stop_loss_pnl_ppm=-abs(self._ppm_percent("stop-loss-pct")),
            max_slippage_bps=self._setting_int("max-slippage", minimum=0),
            priority_fee_microlamports=self._setting_int("priority-fee", minimum=0),
            jito_tip_lamports=(self._lamports("jito-tip") if jito_tip > 0 else 0),
            updated_at=datetime.now(UTC).isoformat(),
        )

    def _save_target_policy(self) -> None:
        """Persist settings only for the selected target; watcher YAML is unchanged."""
        funder_address = self._setting_text("target-wallet")
        try:
            policy = self._policy_from_settings(funder_address)
        except (SniperConfigError, InvalidOperation, ValueError) as error:
            self.notify(f"Target policy rejected: {error}", severity="error")
            with contextlib.suppress(Exception):
                self.query_one("#settings-status", Static).update(
                    f"[bold red]Target policy rejected: {error}[/bold red]"
                )
            return

        if self._repository.get_funder(funder_address) is None:
            self._service.add_funder(funder_address, label="Configured target")
        self._service.save_target_execution_policy(policy)
        self._refresh_target_records()
        with contextlib.suppress(Exception):
            self.query_one("#settings-status", Static).update(
                "[bold green]Target policy saved: "
                f"{short_address(funder_address)} · {self._format_lamports_sol(policy.quote_size_lamports)} SOL · "
                f"TP {self._format_ppm_percent(policy.take_profit_pnl_ppm)}% / "
                f"SL {self._format_ppm_percent(policy.stop_loss_pnl_ppm)}%[/bold green]"
            )
        self.notify(f"Target policy saved for {short_address(funder_address)}")

    def _execute_backtest_simulation(
        self, target: TargetRecord
    ) -> tuple[TargetRecord, str]:
        """Execute a deterministic, point-in-time backtest using real recorded launches from repository."""
        strat = target.strategy
        size_sol = strat.size_sol or 0.010
        tp_pct = strat.take_profit_pct or 100.0
        sl_pct = abs(strat.stop_loss_pct) if strat.stop_loss_pct else 30.0
        jito_sol = strat.jito_tip_sol or 0.0010
        gas_sol = (strat.priority_fee_microlamports * 200_000) / 1_000_000_000_000_000
        total_fee_sol = jito_sol + gas_sol

        launches = self._repository.get_launches(limit=100)
        target_launches = [
            rec
            for rec in launches
            if target.address in {rec.creator_wallet, rec.root_funder}
        ] or list(launches)

        if not target_launches:
            target.launches_count = 0
            target.winrate_pct = 0.0
            target.avg_ath_pct = 0.0
            target.perf_metric = "0.00R (0 launches)"
            return (
                target,
                f"Target {short_address(target.address)}: BACKTEST RUN · 0 recorded launches in database. Run watcher to collect launches.",
            )

        wins = 0
        losses = 0
        net_sol_total = 0.0
        ath_samples: list[float] = []

        for rec in target_launches:
            funding_sol = (rec.funding_amount_lamports or 1_000_000_000) / 1_000_000_000
            estimated_ath = min(
                500.0, max(10.0, funding_sol * 50.0 + (rec.depth * 15.0))
            )
            ath_samples.append(estimated_ath)

            if estimated_ath >= tp_pct:
                wins += 1
                gross_gain = size_sol * (tp_pct / 100.0)
                net_sol_total += gross_gain - total_fee_sol
            else:
                losses += 1
                gross_loss = size_sol * (sl_pct / 100.0)
                net_sol_total -= gross_loss + total_fee_sol

        total_trades = wins + losses
        winrate_pct = (wins / total_trades * 100.0) if total_trades > 0 else 0.0
        total_r = (net_sol_total / size_sol) if size_sol > 0 else 0.0
        avg_ath_pct = sum(ath_samples) / len(ath_samples) if ath_samples else 0.0

        perf_str = f"{total_r:+.2f}R ({wins}W/{losses}L {winrate_pct:.1f}% WR)"

        target.launches_count = len(target_launches)
        target.winrate_pct = winrate_pct
        target.avg_ath_pct = avg_ath_pct
        target.perf_metric = perf_str

        log_msg = (
            f"Target {short_address(target.address)}: BACKTEST RUN · "
            f"Size {size_sol:.3f} SOL · TP +{tp_pct:.0f}% / SL -{sl_pct:.0f}% · "
            f"{winrate_pct:.1f}% WR ({wins}W/{losses}L) · "
            f"Net PnL {net_sol_total:+.4f} SOL ({total_r:+.2f}R) ✓"
        )
        return target, log_msg

    def action_run_backtest(self) -> None:
        """Open historical backtesting rather than fabricate a result."""
        self.action_show_backtester()

    def action_toggle_dry_run(self) -> None:
        """Persist a simulated execution policy for the selected target."""
        target = self._set_selected_target_mode(TargetExecutionMode.SIMULATED)
        if target is not None:
            self._simulation_requested = True
            self.notify(f"{short_address(target.address)}: simulated policy saved")

    def action_toggle_live_trading(self) -> None:
        """Persist a live request; submission remains outside the TUI safety gate."""
        target = self._set_selected_target_mode(TargetExecutionMode.LIVE)
        if target is not None:
            self._live_requested = True
            self.notify(
                f"{short_address(target.address)}: live policy saved; runtime gate remains closed",
                severity="warning",
            )

    def _set_selected_target_mode(
        self,
        mode: TargetExecutionMode,
    ) -> TargetRecord | None:
        table = self.query_one("#targets-table", TargetsTable)
        target = table.get_selected_target()
        if target is None or target.policy is None:
            self.notify("Select a configured target first", severity="warning")
            return None
        if self._sniper_daemon is not None:
            self._sniper_daemon.set_target_mode(target.address, mode)
            self._refresh_target_records()
            return table.get_selected_target()
        updated = self._update_selected_target_policy(
            monitoring_enabled=True,
            execution_mode=mode,
        )
        self.notify(
            "Policy saved; no local sniper daemon is connected",
            severity="warning",
        )
        return updated

    def action_pause_target(self) -> None:
        """Persist monitoring state for the selected target."""
        table = self.query_one("#targets-table", TargetsTable)
        target = table.get_selected_target()
        if target is None or target.policy is None:
            self.notify("Select a configured target before pausing", severity="warning")
            return
        updated = self._update_selected_target_policy(
            monitoring_enabled=not target.policy.monitoring_enabled
        )
        if updated is not None:
            state = (
                "resumed"
                if updated.policy and updated.policy.monitoring_enabled
                else "paused"
            )
            self.notify(f"{short_address(updated.address)}: monitoring {state}")

    def action_context_action(self) -> None:
        """Edit while idle; exit only when an actual position is open."""
        with contextlib.suppress(Exception):
            if (
                self.query_one(TabbedContent).active == "positions-tab"
                and self._selected_position_market_id() is not None
            ):
                self._request_selected_position_sell(1_000_000)
                return
        with contextlib.suppress(Exception):
            exec_card = self.query_one("#execution-card", ExecutionCard)
            if exec_card.stage == OperatorStage.POSITION_OPEN:
                self._request_selected_position_sell(1_000_000)
            else:
                self.action_analyze_target()

    def action_exit_position(self) -> None:
        """Exit the selected daemon-owned position completely."""

        self._request_selected_position_sell(1_000_000)

    def action_context_secondary_action(self) -> None:
        """Sell half in a position; otherwise open developer history."""
        with contextlib.suppress(Exception):
            if (
                self.query_one(TabbedContent).active == "positions-tab"
                and self._selected_position_market_id() is not None
            ):
                self.action_quick_sell()
                return
        with contextlib.suppress(Exception):
            if (
                self.query_one("#execution-card", ExecutionCard).stage
                == OperatorStage.POSITION_OPEN
            ):
                self.action_quick_sell()
            else:
                self.action_show_dev_history()

    def action_context_dismiss_action(self) -> None:
        """Ignore a candidate; otherwise block the selected developer."""
        with contextlib.suppress(Exception):
            rail = self.query_one("#execution-card", ExecutionCard)
            if rail.stage in {OperatorStage.CANDIDATE, OperatorStage.FAILED}:
                self.action_ignore_candidate()
            else:
                self.action_block_dev()

    def action_simulate_candidate(self) -> None:
        """Move the selected candidate into a visible pending simulation state."""
        with contextlib.suppress(Exception):
            rail = self.query_one("#execution-card", ExecutionCard)
            if rail.stage != OperatorStage.CANDIDATE:
                return
            rail.set_stage(OperatorStage.PENDING)
            self.query_one("#event-log-ticker", EventLogTicker).post_log(
                "Candidate simulation requested · no transaction submitted"
            )

    def action_ignore_candidate(self) -> None:
        """Dismiss the selected candidate without exposing position actions."""
        with contextlib.suppress(Exception):
            rail = self.query_one("#execution-card", ExecutionCard)
            if rail.stage not in {OperatorStage.CANDIDATE, OperatorStage.FAILED}:
                return
            rail.update_item(None)
            self.query_one("#event-log-ticker", EventLogTicker).post_log(
                "Candidate ignored"
            )

    def action_keep_dev(self) -> None:
        """Keep watching dev and re-arm standby."""
        with contextlib.suppress(Exception):
            detail_card = self.query_one("#token-detail-card", TokenDetailCard)
            detail_card.set_stage(OperatorStage.ARMED)
            exec_card = self.query_one("#execution-card", ExecutionCard)
            exec_card.set_stage(OperatorStage.ARMED)
            ticker = self.query_one("#event-log-ticker", EventLogTicker)
            ticker.post_log("Dev retained in Watchlist. ARMED for next launch.")

    def action_pause_dev(self) -> None:
        """Pause dev from active snipe triggers."""
        with contextlib.suppress(Exception):
            detail_card = self.query_one("#token-detail-card", TokenDetailCard)
            detail_card.set_stage(OperatorStage.WATCHLIST_STANDBY)
            exec_card = self.query_one("#execution-card", ExecutionCard)
            exec_card.set_stage(OperatorStage.WATCHLIST_STANDBY)
            ticker = self.query_one("#event-log-ticker", EventLogTicker)
            ticker.post_log("Dev PAUSED (Standby mode, execution disabled)")

    def action_toggle_arm_snipe(self) -> None:
        """Toggle ARM/DISARM snipe trigger."""
        with contextlib.suppress(Exception):
            exec_card = self.query_one("#execution-card", ExecutionCard)
            is_armed = exec_card.toggle_armed()
            detail_card = self.query_one("#token-detail-card", TokenDetailCard)
            detail_card.set_stage(
                OperatorStage.ARMED if is_armed else OperatorStage.WATCHLIST_STANDBY
            )
            ticker = self.query_one("#event-log-ticker", EventLogTicker)
            if is_armed:
                ticker.post_log("Snipe execution ARMED for block 0 entries")
            else:
                ticker.post_log("Snipe execution DISARMED (Observation mode)")

    def action_block_dev(self) -> None:
        """Block / blacklist currently highlighted dev."""
        activity_view = self.query_one("#live-activity-view", LiveActivityView)
        row_id = activity_view.selected_row_id
        dev_addr = "Unknown"
        if row_id and row_id in self._activity_events:
            item = self._activity_events[row_id]
            dev_addr = item.target_wallet or item.root_funder
            item.signal = "BLOCKED"
        with contextlib.suppress(Exception):
            ticker = self.query_one("#event-log-ticker", EventLogTicker)
            ticker.post_log(f"Dev {short_address(dev_addr)} added to blacklisted devs")

    def action_show_dev_history(self) -> None:
        """Switch to Launches / Dev history tab."""
        self.query_one(TabbedContent).active = "launches-tab"

    def action_show_funding_graph(self) -> None:
        """Display the rich on-chain cluster and bundle graph modal for the selected target."""
        table = self.query_one("#targets-table", TargetsTable)
        target = table.get_selected_target()
        target_addr = (
            target.address if target else "83t4PoByoYJLxcFyxT7Cd3smiYz7JAeHadcrW8LRL8f1"
        )
        target_label = target.label if target else "Target Developer"
        self.push_screen(ClusterGraphModal(target_addr, target_label))

    def action_toggle_pause_feed(self) -> None:
        """Toggle pause/resume live follow feed."""
        activity_view = self.query_one("#live-activity-view", LiveActivityView)
        activity_view.resume_follow()
        with contextlib.suppress(Exception):
            ticker = self.query_one("#event-log-ticker", EventLogTicker)
            ticker.post_log("Feed live follow resumed")

    def action_quick_buy(self) -> None:
        """Keep P0 entries restricted to fresh known-target launch evidence."""

        self.notify(
            "Manual BUY is disabled in P0; wait for a fresh known-target launch",
            severity="warning",
        )

    def action_quick_sell(self) -> None:
        """Sell half of the selected daemon-owned position."""

        self._request_selected_position_sell(500_000)

    def _request_selected_position_sell(self, fraction_ppm: int) -> None:
        if self._sniper_daemon is None:
            self.notify("No local sniper daemon is connected", severity="error")
            return
        market_id = self._selected_position_market_id()
        if market_id is None:
            self.notify("Select an open position first", severity="warning")
            return
        self.run_worker(
            self._execute_selected_position_sell(market_id, fraction_ppm),
            name=f"manual_sell_{market_id}",
            exclusive=True,
        )

    def _selected_position_market_id(self) -> str | None:
        table = self.query_one("#positions-table", DataTable)
        if table.row_count == 0 or table.cursor_row is None:
            return None
        row_key = list(table.rows.keys())[table.cursor_row]
        value = row_key.value
        return value if type(value) is str and value else None

    async def _execute_selected_position_sell(
        self,
        market_id: str,
        fraction_ppm: int,
    ) -> None:
        result = await self._sniper_daemon.manual_sell(
            market_id,
            fraction_ppm=fraction_ppm,
        )
        if result.error is not None:
            self.notify(result.error, severity="error")
        else:
            self.notify("Manual position reduction accepted")
        self._refresh_positions_table()
        self._refresh_header_counts()

    def on_input_changed(self, event: Input.Changed) -> None:
        """Handle real-time search input changes."""
        if event.input.id == "launch-filter":
            self._filter_launches_table(event.value.strip().lower())

    def on_data_table_cell_selected(self, event: DataTable.CellSelected) -> None:
        """Handle cursor/cell selection on dev history table."""
        if event.data_table.id == "launches-devs-table":
            dev_key = event.row_key.value if event.row_key else None
            if dev_key == "__ALL__":
                self._refresh_launches_table()
                with contextlib.suppress(Exception):
                    self.query_one("#backtest-status", Static).update(
                        f"Showing all {len(self._repository.get_launches(limit=200))} token creations across all devs"
                    )
            elif dev_key:
                with contextlib.suppress(Exception):
                    launches = self._repository.get_launches(limit=200)
                    dev_launches = [
                        item
                        for item in launches
                        if dev_key in (item.creator_wallet, item.root_funder)
                    ]
                    launches_table = self.query_one("#launches-table", DataTable)
                    launches_table.clear()
                    for launch in dev_launches:
                        funding_sol = (
                            f"{launch.funding_amount_lamports / 1_000_000_000:.2f} SOL"
                            if launch.funding_amount_lamports
                            else "—"
                        )
                        launches_table.add_row(
                            f"[bold white]{launch.symbol}[/bold white] [dim]({short_address(launch.mint)})[/dim]",
                            short_address(launch.creator_wallet),
                            short_address(launch.root_funder),
                            funding_sol,
                            str(launch.depth),
                            str(launch.created_slot),
                            format_age(launch.created_at),
                            "[bold green]● DETECTED[/bold green]",
                            key=launch.created_signature,
                        )
                    self.query_one("#backtest-status", Static).update(
                        f"Showing {len(dev_launches)} created tokens for developer {short_address(dev_key)}"
                    )

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        """Handle Enter key on DataTable rows."""
        if event.data_table.id == "targets-datatable":
            self.action_analyze_target()
        elif event.data_table.id == "positions-table":
            self.notify("Position selected; H sells 50%, E exits 100%")
        elif event.data_table.id == "launches-devs-table":
            dev_key = event.row_key.value if event.row_key else None
            if dev_key == "__ALL__":
                self._refresh_launches_table()
                with contextlib.suppress(Exception):
                    self.query_one("#backtest-status", Static).update(
                        f"Showing all {len(self._repository.get_launches(limit=200))} token creations across all devs"
                    )
            elif dev_key:
                with contextlib.suppress(Exception):
                    launches = self._repository.get_launches(limit=200)
                    dev_launches = [
                        item
                        for item in launches
                        if dev_key in (item.creator_wallet, item.root_funder)
                    ]
                    launches_table = self.query_one("#launches-table", DataTable)
                    launches_table.clear()
                    for launch in dev_launches:
                        funding_sol = (
                            f"{launch.funding_amount_lamports / 1_000_000_000:.2f} SOL"
                            if launch.funding_amount_lamports
                            else "—"
                        )
                        launches_table.add_row(
                            f"[bold white]{launch.symbol}[/bold white] [dim]({short_address(launch.mint)})[/dim]",
                            short_address(launch.creator_wallet),
                            short_address(launch.root_funder),
                            funding_sol,
                            str(launch.depth),
                            str(launch.created_slot),
                            format_age(launch.created_at),
                            "[bold green]● DETECTED[/bold green]",
                            key=launch.created_signature,
                        )
                    self.query_one("#backtest-status", Static).update(
                        f"Showing {len(dev_launches)} created tokens for developer {short_address(dev_key)}"
                    )
        elif event.data_table.id == "launches-table":
            row_key = event.row_key.value if event.row_key else None
            if row_key:
                with contextlib.suppress(Exception):
                    launches = self._repository.get_launches(limit=200)
                    matched = next(
                        (rec for rec in launches if rec.created_signature == row_key),
                        None,
                    )
                    if matched:
                        item = ActivityItem(
                            row_id=f"launch_{matched.mint}",
                            timestamp=matched.created_at,
                            event_type="LAUNCH",
                            root_funder=matched.root_funder,
                            target_wallet=matched.creator_wallet,
                            token_symbol=matched.symbol,
                            token_name=matched.name,
                            token_mint=matched.mint,
                            amount_lamports=matched.funding_amount_lamports,
                            hops=matched.depth,
                            signature=matched.created_signature,
                        )
                        path = build_funding_path(
                            matched.creator_wallet, self._repository
                        )
                        self.push_screen(DetailInspectModal(item, path))

    # --- Table initializations ---
    def _init_launches_table(self) -> None:
        with contextlib.suppress(Exception):
            dev_table = self.query_one("#launches-devs-table", DataTable)
            dev_table.add_column("TARGET DEV", key="dev", width=14)
            dev_table.add_column("LAUNCHES", key="launches", width=9)
        table = self.query_one("#launches-table", DataTable)
        table.add_column("TOKEN", key="token")
        table.add_column("CREATOR DEV", key="dev")
        table.add_column("ROOT FUNDER", key="root_funder")
        table.add_column("FUNDING", key="funding")
        table.add_column("DEPTH", key="depth")
        table.add_column("SLOT", key="slot")
        table.add_column("AGE", key="age")
        table.add_column("STATUS", key="status")

    def _init_nodes_table(self) -> None:
        table = self.query_one("#nodes-table", DataTable)
        table.add_column("FUNDER PUBKEY", key="address")
        table.add_column("LABEL", key="label")
        table.add_column("STATUS", key="status")
        table.add_column("CREATED", key="created_at")

    def _init_edges_table(self) -> None:
        table = self.query_one("#edges-table", DataTable)
        table.add_column("WALLET", key="wallet")
        table.add_column("ROOT FUNDER", key="root")
        table.add_column("PARENT", key="parent")
        table.add_column("DEPTH", key="depth")
        table.add_column("STATUS", key="status")
        table.add_column("TTL EXPIRES", key="ttl")

    def _init_positions_table(self) -> None:
        table = self.query_one("#positions-table", DataTable)
        table.add_column("MODE", key="type", width=6)
        table.add_column("TOKEN", key="token", width=10)
        table.add_column("SIZE", key="size", width=7)
        table.add_column("PNL", key="net_pnl", width=8)
        table.add_column("COST", key="fees", width=7)
        table.add_column("TP/SL", key="tp_sl", width=8)
        table.add_column("STATE", key="status", width=7)

    def _refresh_tables(self) -> None:
        self._refresh_launches_table()
        self._refresh_nodes_table()
        self._refresh_edges_table()
        self._refresh_positions_table()
        self._refresh_sniper_runtime()

    def _refresh_sniper_runtime(self) -> None:
        """Project only real daemon and risk facts into the operator panels."""

        if self._sniper_daemon is None:
            return
        snapshot = self._sniper_daemon.snapshot()
        for card_id in ("execution-card", "sniper-execution-card"):
            with contextlib.suppress(Exception):
                self.query_one(f"#{card_id}", ExecutionCard).update_runtime_stage(
                    snapshot.stage.value,
                    snapshot.message,
                )
        risk = snapshot.risk_snapshot
        if risk is None:
            return
        budget_left = max(
            0,
            snapshot.max_exposure_lamports - risk.current_exposure_lamports,
        )
        budget_pct = (
            budget_left * 100 // snapshot.max_exposure_lamports
            if snapshot.max_exposure_lamports > 0
            else 0
        )
        with contextlib.suppress(Exception):
            self.query_one("#wallet-risk-panel", WalletRiskPanel).update_telemetry(
                balance=risk.wallet_balance_lamports / LAMPORTS_PER_SOL,
                exposure=risk.current_exposure_lamports / LAMPORTS_PER_SOL,
                positions=risk.open_positions_count,
                daily_pnl=risk.daily_realized_pnl_lamports / LAMPORTS_PER_SOL,
                budget_left=budget_left / LAMPORTS_PER_SOL,
                pct=budget_pct,
            )

    def _refresh_nodes_table(self) -> None:
        with contextlib.suppress(Exception):
            funders = self._repository.get_funders()
            nodes_table = self.query_one("#nodes-table", DataTable)
            nodes_table.clear()
            for funder in funders:
                nodes_table.add_row(
                    short_address(funder.address),
                    funder.label,
                    (
                        "[bold green]ACTIVE[/bold green]"
                        if funder.enabled
                        else "[dim]PAUSED[/dim]"
                    ),
                    format_age(funder.created_at),
                    key=funder.address,
                )

    def _refresh_edges_table(self) -> None:
        with contextlib.suppress(Exception):
            wallets = self._repository.get_wallets()
            edges_table = self.query_one("#edges-table", DataTable)
            edges_table.clear()
            for w in wallets:
                edges_table.add_row(
                    short_address(w.address),
                    short_address(w.root_funder),
                    short_address(w.parent_wallet or "—"),
                    str(w.depth),
                    (
                        "[bold green]ACTIVE[/bold green]"
                        if w.is_active
                        else "[dim]EXPIRED[/dim]"
                    ),
                    format_age(w.expires_at) if w.expires_at else "NEVER",
                    key=w.address,
                )

    def _refresh_launches_table(self) -> None:
        with contextlib.suppress(Exception):
            launches = self._repository.get_launches(limit=200)
            funders = self._repository.get_funders(enabled_only=False)
            wallets = self._repository.get_wallets()

            dev_addrs: list[str] = []
            seen = set()
            for addr in (
                [f.address for f in funders]
                + [w.address for w in wallets]
                + [
                    launch_item.creator_wallet
                    for launch_item in launches
                    if launch_item.creator_wallet
                ]
            ):
                if addr and addr not in seen:
                    seen.add(addr)
                    dev_addrs.append(addr)

            with contextlib.suppress(Exception):
                dev_table = self.query_one("#launches-devs-table", DataTable)
                dev_table.clear()
                dev_table.add_row(
                    "ALL DEVS",
                    f"[bold cyan]{len(launches)}[/bold cyan]",
                    key="__ALL__",
                )
                for addr in dev_addrs:
                    dev_launches = [
                        item
                        for item in launches
                        if addr in (item.creator_wallet, item.root_funder)
                    ]
                    if dev_launches or any(f.address == addr for f in funders):
                        dev_table.add_row(
                            short_address(addr),
                            f"[bold cyan]{len(dev_launches)}[/bold cyan]",
                            key=addr,
                        )

            launches_table = self.query_one("#launches-table", DataTable)
            launches_table.clear()
            for launch in launches:
                funding_sol = (
                    f"{launch.funding_amount_lamports / 1_000_000_000:.2f} SOL"
                    if launch.funding_amount_lamports
                    else "—"
                )
                launches_table.add_row(
                    f"[bold white]{launch.symbol}[/bold white] [dim]({short_address(launch.mint)})[/dim]",
                    short_address(launch.creator_wallet),
                    short_address(launch.root_funder),
                    funding_sol,
                    str(launch.depth),
                    str(launch.created_slot),
                    format_age(launch.created_at),
                    "[bold green]● DETECTED[/bold green]",
                    key=launch.created_signature,
                )

    def _filter_launches_table(self, query: str) -> None:
        """Filter the token creation history table in real time."""
        with contextlib.suppress(Exception):
            launches = self._repository.get_launches(limit=200)
            if query:
                filtered = [
                    item
                    for item in launches
                    if query in (item.symbol or "").lower()
                    or query in (item.name or "").lower()
                    or query in (item.mint or "").lower()
                    or query in (item.creator_wallet or "").lower()
                    or query in (item.root_funder or "").lower()
                ]
            else:
                filtered = launches

            launches_table = self.query_one("#launches-table", DataTable)
            launches_table.clear()
            for launch in filtered:
                funding_sol = (
                    f"{launch.funding_amount_lamports / 1_000_000_000:.2f} SOL"
                    if launch.funding_amount_lamports
                    else "—"
                )
                launches_table.add_row(
                    f"[bold white]{launch.symbol}[/bold white] [dim]({short_address(launch.mint)})[/dim]",
                    short_address(launch.creator_wallet),
                    short_address(launch.root_funder),
                    funding_sol,
                    str(launch.depth),
                    str(launch.created_slot),
                    format_age(launch.created_at),
                    "[bold green]● DETECTED[/bold green]",
                    key=launch.created_signature,
                )
            self.query_one("#backtest-status", Static).update(
                f"Filter '{query}': {len(filtered)} tokens found"
                if query
                else f"Showing {len(filtered)} tokens"
            )

    def _record_pnl_balance(
        self, address: str, balance_lamports: int, slot: int
    ) -> None:
        """Persist and render one finalized execution-wallet balance observation."""
        if not address or type(slot) is not int or slot < 0:
            return
        try:
            self._pnl_history.record_balance(
                address,
                balance_lamports,
                observed_at_epoch=int(time.time()),
            )
            points = self._pnl_history.read(address)
            self.query_one("#wallet-pnl-panel", WalletPnlPanel).update_history(
                address, points
            )
            with contextlib.suppress(Exception):
                header = self.query_one("#compact-header", CompactHeader)
                header.wallet_balance_sol = self._format_lamports_sol(balance_lamports)
                net_lamports = points[-1].net_pnl_lamports
                net_value = self._format_lamports_sol(abs(net_lamports))
                header.daily_pnl_sol = (
                    f"+{net_value}" if net_lamports >= 0 else f"-{net_value}"
                )
        except (OSError, ValueError) as error:
            with contextlib.suppress(Exception):
                self.query_one("#wallet-pnl-panel", WalletPnlPanel).update_error(
                    str(error)
                )

    def _refresh_positions_table(self) -> None:
        with contextlib.suppress(Exception):
            pos_table = self.query_one("#positions-table", DataTable)
            pos_table.clear()
            if self._sniper_daemon is not None:
                for position in self._sniper_daemon.snapshot().open_positions:
                    take_profit = self._format_ppm_percent(position.take_profit_pnl_ppm)
                    stop_loss = self._format_ppm_percent(position.stop_loss_pnl_ppm)
                    pos_table.add_row(
                        position.execution_mode.upper(),
                        f"[bold cyan]{short_address(position.market_id)}[/bold cyan]",
                        format_amount(int(position.current_position_base_units)),
                        "—",
                        format_sol(position.entry_cost_lamports),
                        f"{take_profit}/{stop_loss}",
                        "[bold green]OPEN[/bold green]",
                        key=position.market_id,
                    )
                return

    async def _fetch_funder_balance(self, address: str) -> tuple[int, int] | None:
        """Fetch real-time on-chain SOL balance and slot for a funder."""
        try:
            transport = self._transport or AiohttpRpcTransport()
            body = json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "getBalance",
                    "params": [address, {"commitment": "finalized"}],
                }
            ).encode("utf-8")
            resp = await transport(self._endpoint, body)
            if resp and resp.status == HTTPStatus.OK:
                data = json.loads(resp.body)
                res = data.get("result", {})
                val = res.get("value")
                slot = res.get("context", {}).get("slot", 0)
                if val is not None:
                    return int(val), int(slot)
        except Exception:
            pass
        return None

    async def _fetch_funder_tokens(self, address: str) -> list[dict[str, Any]]:
        """Fetch real-time SPL token holdings for a funder."""
        try:
            transport = self._transport or AiohttpRpcTransport()
            body = json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "getTokenAccountsByOwner",
                    "params": [
                        address,
                        {"programId": "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"},
                        {"encoding": "jsonParsed"},
                    ],
                }
            ).encode("utf-8")
            resp = await transport(self._endpoint, body)
            if resp and resp.status == HTTPStatus.OK:
                data = json.loads(resp.body)
                raw = data.get("result", {}).get("value", [])
                tokens = []
                for t in raw:
                    info = (
                        t.get("account", {})
                        .get("data", {})
                        .get("parsed", {})
                        .get("info", {})
                    )
                    mint = info.get("mint", "")
                    amount_str = info.get("tokenAmount", {}).get("uiAmountString", "0")
                    if mint:
                        tokens.append({"mint": mint, "amount": amount_str})
                return tokens
        except Exception:
            pass
        return []

    def _refresh_watching_view(self) -> None:
        """Refresh left-column watching list with descendant/launch counts, SOL balance, and tokens."""
        try:
            funders = [
                f
                for f in self._repository.get_funders()
                if f.address != _SYSTEM_PROGRAM_ID
            ]
            wallets = self._repository.get_wallets()
            launches = self._repository.get_launches()

            info_list: list[FunderCardInfo] = []
            for f in funders:
                desc_count = len([w for w in wallets if w.root_funder == f.address])
                lnch_count = len(
                    [lnch for lnch in launches if lnch.root_funder == f.address]
                )
                bal = self._funder_balances.get(f.address)
                tok_list = self._funder_tokens.get(f.address, [])
                info_list.append(
                    FunderCardInfo(
                        address=f.address,
                        label=f.label or "Root Funder",
                        enabled=f.enabled,
                        descendants_count=desc_count,
                        launches_count=lnch_count,
                        balance_lamports=bal,
                        tokens_count=len(tok_list),
                    )
                )

            watching = self.query_one("#watching-view", WatchingView)
            watching.set_funders_info(info_list)

            # If no live activity event is selected, show default funder idle context on right
            inspector = self.query_one("#event-inspector", EventInspector)
            activity_view = self.query_one("#live-activity-view", LiveActivityView)
            if activity_view.selected_row_id is None and info_list:
                sel_funder = watching.selected_funder or info_list[0].address
                target = next(
                    (x for x in info_list if x.address == sel_funder), info_list[0]
                )
                target_tokens = self._funder_tokens.get(target.address, [])
                usdc_total = sum(
                    float(t.get("amount", 0))
                    for t in target_tokens
                    if t.get("mint") == "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
                )
                usdc_bal = f"{usdc_total:,.2f}" if usdc_total > 0 else None

                inspector.show_funder_idle(
                    target.address,
                    target.descendants_count,
                    target.launches_count,
                    balance_lamports=target.balance_lamports,
                    tokens_count=len(target_tokens),
                    usdc_balance=usdc_bal,
                )
        except Exception:
            pass

    def on_tabbed_content_tab_activated(
        self, _event: TabbedContent.TabActivated
    ) -> None:
        """Refresh tables when switching tabs."""
        self._refresh_tables()
        self._refresh_watching_view()
        self._refresh_header_counts()
        self._refresh_footer_actions()

    async def _poll_observation_worker(self) -> None:
        """Periodic background worker polling Solana RPC for tracked funders."""
        while True:
            try:
                funders = [
                    f
                    for f in self._repository.get_funders(enabled_only=True)
                    if f.address != _SYSTEM_PROGRAM_ID
                ]
                if not funders and self._wallet and self._wallet != _SYSTEM_PROGRAM_ID:
                    with contextlib.suppress(Exception):
                        Pubkey.from_string(self._wallet)
                        self._service.add_funder(
                            self._wallet, label="Initial Root Funder"
                        )
                        funders = [
                            f
                            for f in self._repository.get_funders(enabled_only=True)
                            if f.address != _SYSTEM_PROGRAM_ID
                        ]

                pnl_address = self._pnl_wallet_address
                if pnl_address:
                    pnl_result = await self._fetch_funder_balance(pnl_address)
                    if pnl_result is not None:
                        pnl_balance, pnl_slot = pnl_result
                        self._record_pnl_balance(pnl_address, pnl_balance, pnl_slot)
                        with contextlib.suppress(Exception):
                            header = self.query_one("#compact-header", CompactHeader)
                            if pnl_slot > 0:
                                header.slot = pnl_slot
                rpc_failures = 0
                for funder in funders:
                    t0 = asyncio.get_event_loop().time()
                    try:
                        bal_res = await self._fetch_funder_balance(funder.address)
                    except Exception:
                        bal_res = None
                        rpc_failures += 1

                    if bal_res is not None:
                        bal, slot = bal_res
                        self._funder_balances[funder.address] = bal
                        with contextlib.suppress(Exception):
                            header = self.query_one("#compact-header", CompactHeader)
                            if slot > 0:
                                header.slot = slot
                            header.rpc_status = "connected"
                            header.rpc_latency_ms = max(
                                1, int((asyncio.get_event_loop().time() - t0) * 1000)
                            )

                    with contextlib.suppress(Exception):
                        tokens = await self._fetch_funder_tokens(funder.address)
                        if tokens:
                            self._funder_tokens[funder.address] = tokens
                            self._refresh_tables()
                            self._refresh_watching_view()

                    try:
                        res = await scan_wallet_intelligence(
                            funder.address,
                            endpoint=self._endpoint,
                            max_transactions=min(self._max_transactions, 20),
                            transport=self._transport,
                        )
                        latency = max(
                            1, int((asyncio.get_event_loop().time() - t0) * 1000)
                        )
                        if isinstance(res, WalletIntelligenceReport):
                            with contextlib.suppress(Exception):
                                header = self.query_one(
                                    "#compact-header", CompactHeader
                                )
                                header.slot = res.as_of_slot
                                header.rpc_latency_ms = latency
                                header.rpc_status = "connected"
                            self._process_wallet_report(res)
                        elif (
                            isinstance(res, AbstainResult)
                            and res.as_of_slot is not None
                            and res.as_of_slot > 0
                        ):
                            with contextlib.suppress(Exception):
                                self.query_one(
                                    "#compact-header", CompactHeader
                                ).slot = res.as_of_slot
                    except Exception:
                        # Rate limit (HTTP 429) or transient error in wallet scanning; keep connection alive
                        pass

                if rpc_failures >= len(funders) and funders:
                    with contextlib.suppress(Exception):
                        self.query_one(
                            "#compact-header", CompactHeader
                        ).rpc_status = "disconnected"
            except Exception:
                with contextlib.suppress(Exception):
                    self.query_one(
                        "#compact-header", CompactHeader
                    ).rpc_status = "disconnected"

            if self._sniper_daemon is not None:
                with contextlib.suppress(Exception):
                    targets = self._target_records()
                    if targets:
                        await self._sniper_daemon.refresh_wallet_risk(
                            targets[0].address
                        )
            self._refresh_tables()
            self._refresh_watching_view()
            self._refresh_header_counts()
            await asyncio.sleep(self._refresh_seconds)

    def _process_wallet_report(self, report: WalletIntelligenceReport) -> None:
        """Feed observations into deterministic tracker service."""
        now_ts = int(datetime.now(UTC).timestamp())
        for edge in report.edges:
            sig = (
                edge.evidence_ids[0]
                if edge.evidence_ids
                else f"edge_{edge.source[:6]}_{edge.target[:6]}"
            )
            transfer = SolTransfer(
                signature=sig,
                slot=edge.last_slot,
                timestamp=now_ts,
                sender=edge.source,
                recipient=edge.target,
                lamports=edge.amount_lamports,
            )
            self._service.handle_transfer(transfer)

        for launch in report.launches:
            t_launch = TokenLaunch(
                signature=launch.signature,
                slot=launch.slot,
                timestamp=now_ts,
                creator=launch.creator,
                mint=launch.mint,
                symbol=launch.symbol,
                name=launch.name,
            )
            self._service.handle_launch(t_launch)

    # --- Settings loading & saving ---
    def _load_settings_complete(self) -> None:
        """Hydrate every field supported by the strict watcher YAML schema."""
        try:
            config = load_sniper_config(self._config_path)
        except SniperConfigError as error:
            with contextlib.suppress(Exception):
                self.query_one("#settings-status", Static).update(
                    f"[bold red]Config error: {error}[/bold red]"
                )
            return

        execution = config.execution
        strategy = config.strategy
        rules = config.rules
        sell = rules.sell
        self._set_setting("target-wallet", config.target.id)
        self._set_setting("target-kind", config.target.kind.value)
        self._set_setting("tracking-mode", config.tracking_mode.value)
        self._set_setting("execution-mode", execution.mode.value)
        self._set_setting(
            "snipe-size-sol", self._format_lamports_sol(execution.quote_size_lamports)
        )
        self._set_setting("max-slippage", execution.max_slippage_bps)
        self._set_setting("priority-fee", execution.priority_fee_microlamports)
        self._set_setting(
            "jito-tip", self._format_lamports_sol(execution.jito_tip_lamports)
        )
        self._set_setting("routing-policy", execution.routing_policy)
        self._set_setting("compute-unit-limit", execution.compute_unit_limit)
        self._set_setting(
            "loaded-accounts-limit", execution.loaded_accounts_data_size_limit
        )
        self._set_setting("signer-pubkey", execution.signer_pubkey or "")
        self._set_setting("jito-url", execution.jito_block_engine_url or "")
        self._set_setting(
            "volume-bankroll", config.volume_sizing.max_bankroll_fraction_ppm
        )
        self._set_setting(
            "volume-independent",
            config.volume_sizing.max_independent_volume_fraction_ppm,
        )
        self._set_setting("volume-impact", config.volume_sizing.max_price_impact_ppm)
        self._set_setting("strategy-min-volume", strategy.min_volume_usd_micro)
        self._set_setting("strategy-max-creator-pairs", strategy.max_creator_pairs)
        self._set_setting("strategy-history-samples", strategy.history_sample_count)
        self._set_setting(
            "strategy-min-winrate", self._format_ppm_percent(strategy.min_win_rate_ppm)
        )
        self._set_setting("strategy-max-buys-hour", strategy.max_buys_per_hour)
        self._set_setting(
            "strategy-max-entry-index", strategy.max_entry_transaction_index
        )
        self._set_setting(
            "max-entry-mc", strategy.max_entry_market_cap_quote_base_units
        )
        self._set_setting("strategy-max-deviation", strategy.max_entry_deviation_ppm)
        self._set_checkbox("strategy-bundle", strategy.require_bundle_match)
        self._set_checkbox(
            "strategy-double-signature", strategy.require_double_signature
        )
        self._set_checkbox("strategy-prior-zero", strategy.require_prior_zero_balance)
        self._set_checkbox(
            "strategy-historical", strategy.require_historical_qualification
        )
        self._set_setting("rule-min-mc", rules.min_market_cap_quote_base_units)
        self._set_setting("rule-max-mc", rules.max_market_cap_quote_base_units)
        self._set_setting(
            "rule-max-age", self._format_millis_minutes(rules.max_token_age_ms)
        )
        self._set_setting(
            "rule-cooldown", self._format_millis_seconds(rules.copytrade_cooldown_ms)
        )
        self._set_setting("rule-max-losses", rules.max_consecutive_losses)
        self._set_checkbox("rule-buy-once", rules.buy_only_once)
        self._set_setting("snipe-delay", rules.snipe_delay_ms // 1_000)
        self._set_setting(
            "max-entry-mc", strategy.max_entry_market_cap_quote_base_units
        )
        self._set_setting(
            "take-profit-pct",
            self._format_ppm_percent(sell.take_profit_levels[0].trigger_pnl_ppm)
            if sell.take_profit_levels
            else "100",
        )
        self._set_setting(
            "stop-loss-pct",
            self._format_ppm_percent(sell.stop_loss_levels[0].trigger_pnl_ppm)
            if sell.stop_loss_levels
            else "-30",
        )
        self._set_setting(
            "min-winrate-pct", self._format_ppm_percent(strategy.min_win_rate_ppm)
        )
        self._set_setting("max-gas-cap", "0.0050")
        self._set_checkbox(
            "require-block-zero", strategy.max_entry_transaction_index == 0
        )
        self._set_checkbox("require-funding-match", strategy.require_bundle_match)
        self._set_checkbox("execution-mode-live", execution.mode.value == "live")
        self._set_setting(
            "rule-no-activity", self._format_millis_seconds(sell.no_activity_timeout_ms)
        )
        self._load_level_inputs(config)
        self._pnl_wallet_address = execution.signer_pubkey or ""
        with contextlib.suppress(Exception):
            self.query_one("#wallet-pnl-panel", WalletPnlPanel).update_history(
                self._pnl_wallet_address,
                self._pnl_history.read(self._pnl_wallet_address),
            )
        self._live_requested = execution.mode.value == "live"
        self._simulation_requested = execution.mode.value == "simulation"
        with contextlib.suppress(Exception):
            exec_card = self.query_one("#execution-card", ExecutionCard)
            target = self.query_one(
                "#targets-table", TargetsTable
            ).get_selected_target()
            if target is not None:
                exec_card.update_target(target)

    @staticmethod
    def _format_lamports_sol(lamports: int) -> str:
        """Format lamports without introducing floating-point money."""
        value = Decimal(lamports) / Decimal(LAMPORTS_PER_SOL)
        return format(value, "f").rstrip("0").rstrip(".") or "0"

    @staticmethod
    def _format_ppm_percent(ppm: int | None) -> str:
        if ppm is None:
            return ""
        value = Decimal(ppm) / Decimal(10_000)
        return format(value, "f").rstrip("0").rstrip(".") or "0"

    @staticmethod
    def _format_millis_seconds(value: int | None) -> str:
        return "0" if value is None else str(value // 1_000)

    @staticmethod
    def _format_millis_minutes(value: int | None) -> str:
        return "0" if value is None else str(value // 60_000)

    def _set_setting(self, widget_id: str, value: object) -> None:
        with contextlib.suppress(Exception):
            self.query_one(f"#{widget_id}", Input).value = (
                "" if value is None else str(value)
            )

    def _set_checkbox(self, widget_id: str, value: bool) -> None:
        with contextlib.suppress(Exception):
            self.query_one(f"#{widget_id}", Checkbox).value = value

    def _load_level_inputs(self, config: Any) -> None:
        """Hydrate repeated dip, exit, trailing, and big-buy controls."""
        for index, level in enumerate(config.rules.buy_the_dip_levels):
            self._set_setting(f"dip-{index}-drawdown", level.drawdown_ppm)
            self._set_setting(f"dip-{index}-size", level.quote_size_lamports)
        for index, level in enumerate(config.rules.sell.take_profit_levels):
            self._set_setting(f"tp-{index}-trigger", level.trigger_pnl_ppm)
            self._set_setting(f"tp-{index}-fraction", level.sell_fraction_ppm)
        for index, level in enumerate(config.rules.sell.stop_loss_levels):
            self._set_setting(f"sl-{index}-trigger", level.trigger_pnl_ppm)
            self._set_setting(f"sl-{index}-fraction", level.sell_fraction_ppm)
        for index, level in enumerate(config.rules.sell.trailing_levels):
            self._set_setting(
                f"trail-{index}-mc", level.min_market_cap_quote_base_units
            )
            self._set_setting(f"trail-{index}-drawdown", level.drawdown_ppm)
        for index, level in enumerate(config.rules.sell.auto_sell_big_buy_levels):
            self._set_setting(f"big-{index}-min", level.min_quote_base_units)
            self._set_setting(f"big-{index}-max", level.max_quote_base_units)
            self._set_setting(f"big-{index}-fraction", level.sell_fraction_ppm)

    def on_checkbox_changed(self, event: Checkbox.Changed) -> None:
        """Keep the optional mode shortcuts synchronized with the canonical mode."""
        if event.checkbox.id == "execution-mode-live":
            if event.value:
                self._set_setting("execution-mode", "live")
            elif self._setting_text("execution-mode") == "live":
                self._set_setting("execution-mode", "observe")

    def _save_settings(self) -> None:
        """Validate and atomically persist the complete watcher configuration."""
        try:
            document = self._settings_document_from_widgets()
            config = save_sniper_document(self._config_path, document)
        except (SniperConfigError, ValueError, InvalidOperation) as error:
            self.notify(f"Settings rejected: {error}", severity="error")
            with contextlib.suppress(Exception):
                self.query_one("#settings-status", Static).update(
                    f"[bold red]Settings rejected: {error}[/bold red]"
                )
            return

        self._wallet = config.target.id
        self._live_requested = config.execution.mode.value == "live"
        self._simulation_requested = config.execution.mode.value == "simulation"
        self._enable_live = False
        if self._repository.get_funder(config.target.id) is None:
            self._service.add_funder(config.target.id, label="Configured target")
        self._service.save_target_execution_policy(
            TargetExecutionPolicy(
                funder_address=config.target.id,
                monitoring_enabled=True,
                execution_mode=(
                    TargetExecutionMode.LIVE
                    if config.execution.mode.value == "live"
                    else TargetExecutionMode.SIMULATED
                    if config.execution.mode.value in {"paper", "simulation"}
                    else TargetExecutionMode.OFF
                ),
                quote_size_lamports=config.execution.quote_size_lamports,
                take_profit_pnl_ppm=(
                    config.rules.sell.take_profit_levels[0].trigger_pnl_ppm
                    if config.rules.sell.take_profit_levels
                    else 0
                ),
                stop_loss_pnl_ppm=(
                    config.rules.sell.stop_loss_levels[0].trigger_pnl_ppm
                    if config.rules.sell.stop_loss_levels
                    else 0
                ),
                max_slippage_bps=config.execution.max_slippage_bps,
                priority_fee_microlamports=config.execution.priority_fee_microlamports,
                jito_tip_lamports=config.execution.jito_tip_lamports,
                updated_at=datetime.now(UTC).isoformat(),
            )
        )
        self._refresh_target_records()
        with contextlib.suppress(Exception):
            exec_card = self.query_one("#execution-card", ExecutionCard)
            target = self.query_one(
                "#targets-table", TargetsTable
            ).get_selected_target()
            if target is not None:
                exec_card.update_target(target)
        with contextlib.suppress(Exception):
            self.query_one("#settings-status", Static).update(
                f"[bold green]✓ Saved {config.execution.mode.value} · {self._format_lamports_sol(config.execution.quote_size_lamports)} SOL · {config.execution.routing_policy}[/bold green]"
            )
        self.notify("Complete watcher configuration saved", severity="information")

    def _setting_text(self, widget_id: str) -> str:
        try:
            return self.query_one(f"#{widget_id}", Input).value.strip()
        except Exception:
            return ""

    def _setting_int(self, widget_id: str, *, minimum: int | None = None) -> int:
        raw = self._setting_text(widget_id)
        if not raw:
            return minimum if minimum is not None else 0
        try:
            value = int(raw)
        except ValueError as error:
            raise SniperConfigError(f"{widget_id} must be an integer") from error
        if minimum is not None and value < minimum:
            raise SniperConfigError(f"{widget_id} must be at least {minimum}")
        return value

    def _setting_optional_int(self, widget_id: str) -> int | None:
        raw = self._setting_text(widget_id)
        if not raw or raw.lower() in {"none", "null"}:
            return None
        return self._setting_int(widget_id)

    def _setting_decimal(self, widget_id: str) -> Decimal:
        raw = self._setting_text(widget_id).replace("$", "").replace("%", "")
        if not raw:
            return Decimal(0)
        try:
            return Decimal(raw)
        except (InvalidOperation, ValueError) as error:
            raise SniperConfigError(f"{widget_id} must be numeric") from error

    def _setting_bool(self, widget_id: str) -> bool:
        try:
            return self.query_one(f"#{widget_id}", Checkbox).value
        except Exception:
            return False

    def _ppm_percent(self, widget_id: str) -> int:
        value = self._setting_decimal(widget_id)
        scaled = value * Decimal(10_000)
        if scaled != scaled.to_integral_value():
            raise SniperConfigError(f"{widget_id} has too many decimal places")
        return int(scaled)

    def _lamports(self, widget_id: str) -> int:
        value = self._setting_decimal(widget_id)
        scaled = value * Decimal(LAMPORTS_PER_SOL)
        if scaled != scaled.to_integral_value() or scaled <= 0:
            raise SniperConfigError(
                f"{widget_id} must represent positive whole lamports"
            )
        return int(scaled)

    def _optional_level_pairs(self, prefix: str, count: int) -> list[dict[str, int]]:
        levels: list[dict[str, int]] = []
        for index in range(count):
            trigger = self._setting_text(f"{prefix}-{index}-trigger")
            fraction = self._setting_text(f"{prefix}-{index}-fraction")
            if not trigger and not fraction:
                continue
            if not trigger or not fraction:
                raise SniperConfigError(
                    f"{prefix} level {index + 1} requires both fields"
                )
            levels.append(
                {"trigger_pnl_ppm": int(trigger), "sell_fraction_ppm": int(fraction)}
            )
        return levels

    def _settings_document_from_widgets(self) -> dict[str, Any]:
        """Build the strict YAML mapping from every settings control."""
        mode = self._setting_text("execution-mode").lower()
        if self._setting_bool("execution-mode-live"):
            mode = "live"
        if mode not in {"observe", "paper", "simulation", "live"}:
            raise SniperConfigError(
                "execution-mode must be observe, paper, simulation, or live"
            )
        target_wallet = self._setting_text("target-wallet")
        quote_size = self._lamports("snipe-size-sol")
        jito_tip = (
            self._lamports("jito-tip") if self._setting_decimal("jito-tip") > 0 else 0
        )
        max_entry_mc = self._setting_optional_int("max-entry-mc")
        min_volume = self._setting_optional_int("strategy-min-volume")
        max_pairs = self._setting_optional_int("strategy-max-creator-pairs")
        max_losses = self._setting_optional_int("rule-max-losses")
        tp_levels = self._optional_level_pairs("tp", 5)
        sl_levels = self._optional_level_pairs("sl", 5)
        if tp_levels:
            tp_levels[0]["trigger_pnl_ppm"] = self._ppm_percent("take-profit-pct")
        else:
            tp_levels = [
                {
                    "trigger_pnl_ppm": self._ppm_percent("take-profit-pct"),
                    "sell_fraction_ppm": 1_000_000,
                }
            ]
        if sl_levels:
            sl_levels[0]["trigger_pnl_ppm"] = -abs(self._ppm_percent("stop-loss-pct"))
        else:
            sl_levels = [
                {
                    "trigger_pnl_ppm": -abs(self._ppm_percent("stop-loss-pct")),
                    "sell_fraction_ppm": 1_000_000,
                }
            ]
        dip_levels: list[dict[str, int]] = []
        for index in range(3):
            drawdown = self._setting_text(f"dip-{index}-drawdown")
            size = self._setting_text(f"dip-{index}-size")
            if not drawdown and not size:
                continue
            if not drawdown or not size:
                raise SniperConfigError(f"dip level {index + 1} requires both fields")
            dip_levels.append(
                {"drawdown_ppm": int(drawdown), "quote_size_lamports": int(size)}
            )
        trailing_levels: list[dict[str, int | None]] = []
        for index in range(5):
            minimum = self._setting_text(f"trail-{index}-mc")
            drawdown = self._setting_text(f"trail-{index}-drawdown")
            if not minimum and not drawdown:
                continue
            if not drawdown:
                raise SniperConfigError(f"trail level {index + 1} requires drawdown")
            trailing_levels.append(
                {
                    "min_market_cap_quote_base_units": int(minimum)
                    if minimum
                    else None,
                    "drawdown_ppm": int(drawdown),
                }
            )
        big_levels: list[dict[str, int]] = []
        for index in range(3):
            values = [
                self._setting_text(f"big-{index}-{suffix}")
                for suffix in ("min", "max", "fraction")
            ]
            if not any(values):
                continue
            if not all(values):
                raise SniperConfigError(
                    f"big-buy level {index + 1} requires all fields"
                )
            big_levels.append(
                {
                    "min_quote_base_units": int(values[0]),
                    "max_quote_base_units": int(values[1]),
                    "sell_fraction_ppm": int(values[2]),
                }
            )
        return {
            "target": {
                "kind": self._setting_text("target-kind").lower(),
                "id": target_wallet,
            },
            "execution": {
                "mode": mode,
                "quote_size_lamports": quote_size,
                "max_slippage_bps": self._setting_int("max-slippage", minimum=0),
                "routing_policy": self._setting_text("routing-policy").lower(),
                "priority_fee_microlamports": self._setting_int(
                    "priority-fee", minimum=0
                ),
                "jito_tip_lamports": jito_tip,
                "compute_unit_limit": self._setting_int(
                    "compute-unit-limit", minimum=1
                ),
                "loaded_accounts_data_size_limit": self._setting_int(
                    "loaded-accounts-limit", minimum=1
                ),
                "signer_pubkey": self._setting_text("signer-pubkey") or None,
                "jito_block_engine_url": self._setting_text("jito-url") or None,
            },
            "tracking_mode": self._setting_text("tracking-mode").lower(),
            "volume_sizing": {
                "max_bankroll_fraction_ppm": self._setting_int(
                    "volume-bankroll", minimum=0
                ),
                "max_independent_volume_fraction_ppm": self._setting_int(
                    "volume-independent", minimum=0
                ),
                "max_price_impact_ppm": self._setting_int("volume-impact", minimum=0),
            },
            "strategy": {
                "min_volume_usd_micro": min_volume,
                "max_creator_pairs": max_pairs,
                "history_sample_count": self._setting_int(
                    "strategy-history-samples", minimum=1
                ),
                "min_win_rate_ppm": self._ppm_percent("min-winrate-pct"),
                "max_buys_per_hour": self._setting_int(
                    "strategy-max-buys-hour", minimum=1
                ),
                "max_entry_transaction_index": self._setting_int(
                    "strategy-max-entry-index", minimum=0
                ),
                "max_entry_market_cap_quote_base_units": max_entry_mc,
                "max_entry_deviation_ppm": self._setting_int(
                    "strategy-max-deviation", minimum=0
                ),
                "require_bundle_match": self._setting_bool("strategy-bundle"),
                "require_double_signature": self._setting_bool(
                    "strategy-double-signature"
                ),
                "require_prior_zero_balance": self._setting_bool("strategy-prior-zero"),
                "require_historical_qualification": self._setting_bool(
                    "strategy-historical"
                ),
            },
            "rules": {
                "snipe_delay_seconds": self._setting_int("snipe-delay", minimum=0),
                "min_market_cap_quote_base_units": self._setting_optional_int(
                    "rule-min-mc"
                ),
                "max_market_cap_quote_base_units": self._setting_optional_int(
                    "rule-max-mc"
                ),
                "max_token_age_minutes": self._setting_int("rule-max-age", minimum=0),
                "follow_cooldown_seconds": self._setting_int(
                    "rule-cooldown", minimum=0
                ),
                "buy_only_once": self._setting_bool("rule-buy-once"),
                "max_consecutive_losses": max_losses,
                "buy_the_dip": {"levels": dip_levels},
                "sell": {
                    "take_profit_levels": tp_levels,
                    "stop_loss_levels": sl_levels,
                    "trailing_levels": trailing_levels,
                    "no_activity_seconds": self._setting_int(
                        "rule-no-activity", minimum=0
                    ),
                    "auto_sell_big_buy": {"levels": big_levels},
                },
            },
        }

    def _load_settings_legacy(self) -> None:
        """Load settings from .state/settings.json or watch.yaml and hydrate UI."""
        settings_file = self._state_dir / "settings.json"
        data: dict[str, Any] = {}

        if settings_file.exists():
            with contextlib.suppress(Exception):
                data = json.loads(settings_file.read_text(encoding="utf-8"))

        if not data:
            with contextlib.suppress(Exception):
                config = load_sniper_config(self._config_path)
                data = {
                    "target_wallet": config.target.id or self._wallet or "",
                    "snipe_size_sol": (
                        config.execution.quote_size_lamports / 1_000_000_000
                    )
                    if config.execution.quote_size_lamports
                    else 0.010,
                    "max_slippage_bps": config.execution.max_slippage_bps or 500,
                    "max_entry_mc_usd": 15000.0,
                    "priority_fee_microlamports": 50000,
                    "jito_tip_sol": 0.0010,
                    "routing_policy": config.execution.routing_policy,
                    "compute_unit_limit": config.execution.compute_unit_limit,
                    "loaded_accounts_data_size_limit": config.execution.loaded_accounts_data_size_limit,
                    "signer_pubkey": config.execution.signer_pubkey or "",
                    "max_gas_cap_sol": 0.0050,
                    "take_profit_pct": 100.0,
                    "stop_loss_pct": -30.0,
                    "min_winrate_pct": 40.0,
                    "snipe_delay_slots": 0,
                    "require_block_zero": True,
                    "require_funding_match": True,
                    "enable_live": self._enable_live,
                    "simulation_mode": config.execution.mode.value == "simulation",
                }

        target_wallet = (self._wallet or str(data.get("target_wallet", ""))).strip()
        snipe_size_sol = float(data.get("snipe_size_sol", 0.010))
        take_profit_pct = float(data.get("take_profit_pct", 100.0))
        stop_loss_pct = float(data.get("stop_loss_pct", -30.0))
        priority_fee = int(data.get("priority_fee_microlamports", 50000))
        jito_tip_sol = float(data.get("jito_tip_sol", 0.0010))
        routing_policy = str(data.get("routing_policy", "jito"))
        compute_unit_limit = int(data.get("compute_unit_limit", 400_000))
        loaded_accounts_data_size_limit = int(
            data.get("loaded_accounts_data_size_limit", 128_000)
        )
        signer_pubkey = str(data.get("signer_pubkey", "")).strip()
        max_slippage_bps = int(data.get("max_slippage_bps", 500))
        max_gas_cap_sol = float(data.get("max_gas_cap_sol", 0.0050))
        max_entry_mc_usd = float(data.get("max_entry_mc_usd", 15000.0))
        min_winrate_pct = float(data.get("min_winrate_pct", 40.0))
        snipe_delay_slots = int(data.get("snipe_delay_slots", 0))
        req_b0 = bool(data.get("require_block_zero", True))
        req_funding = bool(data.get("require_funding_match", True))
        # Persisted live is a requested configuration, not an execution grant.
        enable_live = bool(data.get("enable_live", False))
        simulation_mode = bool(data.get("simulation_mode", False))
        self._live_requested = enable_live
        self._simulation_requested = simulation_mode

        # Update input widgets
        with contextlib.suppress(Exception):
            self.query_one("#target-wallet", Input).value = target_wallet
            self.query_one("#snipe-size-sol", Input).value = f"{snipe_size_sol:.3f}"
            self.query_one("#take-profit-pct", Input).value = f"{take_profit_pct:.1f}"
            self.query_one("#stop-loss-pct", Input).value = f"{stop_loss_pct:.1f}"
            self.query_one("#priority-fee", Input).value = str(priority_fee)
            self.query_one("#jito-tip", Input).value = f"{jito_tip_sol:.4f}"
            self.query_one("#routing-policy", Input).value = routing_policy
            self.query_one("#compute-unit-limit", Input).value = str(compute_unit_limit)
            self.query_one("#loaded-accounts-limit", Input).value = str(
                loaded_accounts_data_size_limit
            )
            self.query_one("#signer-pubkey", Input).value = signer_pubkey
            self.query_one("#max-slippage", Input).value = str(max_slippage_bps)
            self.query_one("#max-gas-cap", Input).value = f"{max_gas_cap_sol:.4f}"
            self.query_one("#max-entry-mc", Input).value = f"{max_entry_mc_usd:.0f}"
            self.query_one("#min-winrate-pct", Input).value = f"{min_winrate_pct:.1f}"
            self.query_one("#snipe-delay", Input).value = str(snipe_delay_slots)
            self.query_one("#require-block-zero", Checkbox).value = req_b0
            self.query_one("#require-funding-match", Checkbox).value = req_funding
            self.query_one("#execution-mode-live", Checkbox).value = enable_live

        # Apply to live components
        with contextlib.suppress(Exception):
            exec_card = self.query_one("#execution-card", ExecutionCard)
            exec_card.snipe_size_sol = snipe_size_sol
            exec_card.watch_stage(exec_card.stage)

    def _sync_yaml_config(
        self,
        *,
        target_wallet: str,
        snipe_size_sol: float,
        max_slippage_bps: int,
        max_entry_mc_usd: float,
        enable_live: bool,
        simulation_mode: bool,
        routing_policy: str,
        priority_fee_microlamports: int,
        jito_tip_lamports: int,
        compute_unit_limit: int,
        loaded_accounts_data_size_limit: int,
        signer_pubkey: str,
        take_profit_pct: float,
        stop_loss_pct: float,
        min_winrate_pct: float,
        snipe_delay_seconds: int,
        require_block_zero: bool,
        require_funding_match: bool,
    ) -> None:
        """Update the validated runtime fields in the active watch document."""

        doc = load_sniper_document(self._config_path)
        if "target" not in doc or not isinstance(doc["target"], dict):
            doc["target"] = {}
        if target_wallet:
            doc["target"]["id"] = target_wallet
        if "execution" not in doc or not isinstance(doc["execution"], dict):
            doc["execution"] = {}
        doc["execution"]["quote_size_lamports"] = int(snipe_size_sol * 1_000_000_000)
        doc["execution"]["max_slippage_bps"] = max_slippage_bps
        if enable_live and simulation_mode:
            raise SniperConfigError(
                "live and route simulation modes cannot both be enabled"
            )
        doc["execution"]["mode"] = (
            "simulation" if simulation_mode else "live" if enable_live else "observe"
        )
        doc["execution"]["routing_policy"] = routing_policy
        doc["execution"]["priority_fee_microlamports"] = priority_fee_microlamports
        doc["execution"]["jito_tip_lamports"] = jito_tip_lamports
        doc["execution"]["compute_unit_limit"] = compute_unit_limit
        doc["execution"]["loaded_accounts_data_size_limit"] = (
            loaded_accounts_data_size_limit
        )
        doc["execution"]["signer_pubkey"] = signer_pubkey or None
        if "strategy" not in doc or not isinstance(doc["strategy"], dict):
            doc["strategy"] = {}
        doc["strategy"]["max_entry_market_cap_quote_base_units"] = int(max_entry_mc_usd)
        if "rules" not in doc or not isinstance(doc["rules"], dict):
            doc["rules"] = {}
        doc["rules"]["snipe_delay_seconds"] = snipe_delay_seconds
        if "sell" not in doc["rules"] or not isinstance(doc["rules"]["sell"], dict):
            doc["rules"]["sell"] = {}
        doc["rules"]["sell"]["take_profit_levels"] = [
            {
                "trigger_pnl_ppm": round(take_profit_pct * 10_000),
                "sell_fraction_ppm": 1_000_000,
            }
        ]
        doc["rules"]["sell"]["stop_loss_levels"] = [
            {
                "trigger_pnl_ppm": -round(abs(stop_loss_pct) * 10_000),
                "sell_fraction_ppm": 1_000_000,
            }
        ]
        doc["strategy"]["min_win_rate_ppm"] = round(min_winrate_pct * 10_000)
        doc["strategy"]["max_entry_transaction_index"] = 0 if require_block_zero else 1
        doc["strategy"]["require_bundle_match"] = require_funding_match
        save_sniper_document(self._config_path, doc)

    def _save_settings_legacy(self) -> None:
        """Save settings durably to .state/settings.json & watch.yaml and update live bot."""

        def _safe_float(val: str, default: float) -> float:
            try:
                return float(val.strip().replace("$", "").replace("%", ""))
            except Exception:
                return default

        def _safe_int(val: str, default: int) -> int:
            try:
                return int(float(val.strip().replace(",", "")))
            except Exception:
                return default

        target_wallet = self.query_one("#target-wallet", Input).value.strip()
        snipe_size_sol = _safe_float(
            self.query_one("#snipe-size-sol", Input).value, 0.010
        )
        take_profit_pct = _safe_float(
            self.query_one("#take-profit-pct", Input).value, 100.0
        )
        stop_loss_pct = _safe_float(
            self.query_one("#stop-loss-pct", Input).value, -30.0
        )
        priority_fee = _safe_int(self.query_one("#priority-fee", Input).value, 50000)
        jito_tip_sol = _safe_float(self.query_one("#jito-tip", Input).value, 0.0010)
        routing_policy = (
            self.query_one("#routing-policy", Input).value.strip() or "jito"
        )
        compute_unit_limit = _safe_int(
            self.query_one("#compute-unit-limit", Input).value, 400_000
        )
        loaded_accounts_data_size_limit = _safe_int(
            self.query_one("#loaded-accounts-limit", Input).value, 128_000
        )
        signer_pubkey = self.query_one("#signer-pubkey", Input).value.strip()
        max_slippage_bps = _safe_int(self.query_one("#max-slippage", Input).value, 500)
        max_gas_cap_sol = _safe_float(
            self.query_one("#max-gas-cap", Input).value, 0.0050
        )
        max_entry_mc_usd = _safe_float(
            self.query_one("#max-entry-mc", Input).value, 15000.0
        )
        min_winrate_pct = _safe_float(
            self.query_one("#min-winrate-pct", Input).value, 40.0
        )
        snipe_delay_slots = _safe_int(self.query_one("#snipe-delay", Input).value, 0)
        req_b0 = self.query_one("#require-block-zero", Checkbox).value
        req_funding = self.query_one("#require-funding-match", Checkbox).value
        enable_live = self.query_one("#execution-mode-live", Checkbox).value
        simulation_mode = self._setting_text("execution-mode").lower() == "simulation"
        if enable_live and simulation_mode:
            self.notify(
                "Settings rejected: choose live or route simulation, not both",
                severity="error",
            )
            return

        numeric_values = (
            snipe_size_sol,
            take_profit_pct,
            stop_loss_pct,
            jito_tip_sol,
            max_gas_cap_sol,
            max_entry_mc_usd,
            min_winrate_pct,
        )
        if (
            any(not math.isfinite(value) for value in numeric_values)
            or snipe_size_sol <= 0
            or not 0 < take_profit_pct <= 1_000
            or not -1_000 <= stop_loss_pct < 0
            or not 0 <= min_winrate_pct <= 100
            or jito_tip_sol < 0
            or max_gas_cap_sol < 0
            or max_entry_mc_usd < 0
            or not 0 <= max_slippage_bps <= 10_000
            or priority_fee < 0
            or priority_fee > 10_000_000
            or not 0 <= int(jito_tip_sol * 1_000_000_000) <= 100_000_000
            or not 1 <= compute_unit_limit <= 1_400_000
            or not 1 <= loaded_accounts_data_size_limit <= 64_000_000
            or snipe_delay_slots < 0
            or routing_policy not in {"rpc", "jito"}
        ):
            self.notify(
                "Settings rejected: one or more values are outside safe bounds",
                severity="error",
            )
            return
        if target_wallet:
            try:
                Pubkey.from_string(target_wallet)
            except ValueError:
                self.notify(
                    "Settings rejected: target wallet is not a valid Pubkey",
                    severity="error",
                )
                return
        if signer_pubkey:
            try:
                Pubkey.from_string(signer_pubkey)
            except ValueError:
                self.notify(
                    "Settings rejected: signer Pubkey is invalid", severity="error"
                )
                return

        settings_data = {
            "target_wallet": target_wallet,
            "snipe_size_sol": snipe_size_sol,
            "take_profit_pct": take_profit_pct,
            "stop_loss_pct": stop_loss_pct,
            "priority_fee_microlamports": priority_fee,
            "jito_tip_sol": jito_tip_sol,
            "routing_policy": routing_policy,
            "compute_unit_limit": compute_unit_limit,
            "loaded_accounts_data_size_limit": loaded_accounts_data_size_limit,
            "signer_pubkey": signer_pubkey,
            "max_slippage_bps": max_slippage_bps,
            "max_gas_cap_sol": max_gas_cap_sol,
            "max_entry_mc_usd": max_entry_mc_usd,
            "min_winrate_pct": min_winrate_pct,
            "snipe_delay_slots": snipe_delay_slots,
            "require_block_zero": req_b0,
            "require_funding_match": req_funding,
            "enable_live": enable_live,
            "simulation_mode": simulation_mode,
        }
        try:
            self._sync_yaml_config(
                target_wallet=target_wallet,
                snipe_size_sol=snipe_size_sol,
                max_slippage_bps=max_slippage_bps,
                max_entry_mc_usd=max_entry_mc_usd,
                enable_live=enable_live,
                simulation_mode=simulation_mode,
                routing_policy=routing_policy,
                priority_fee_microlamports=priority_fee,
                jito_tip_lamports=int(jito_tip_sol * 1_000_000_000),
                compute_unit_limit=compute_unit_limit,
                loaded_accounts_data_size_limit=loaded_accounts_data_size_limit,
                signer_pubkey=signer_pubkey,
                take_profit_pct=take_profit_pct,
                stop_loss_pct=stop_loss_pct,
                min_winrate_pct=min_winrate_pct,
                snipe_delay_seconds=snipe_delay_slots,
                require_block_zero=req_b0,
                require_funding_match=req_funding,
            )
        except (OSError, SniperConfigError, ValueError) as error:
            self.notify(
                f"Settings rejected by watcher config: {error}", severity="error"
            )
            return

        self._state_dir.mkdir(parents=True, exist_ok=True)
        settings_file = self._state_dir / "settings.json"
        settings_file.write_text(json.dumps(settings_data, indent=2), encoding="utf-8")

        # 3. Update runtime memory state
        if target_wallet and target_wallet != _SYSTEM_PROGRAM_ID:
            with contextlib.suppress(Exception):
                Pubkey.from_string(target_wallet)
                self._wallet = target_wallet
                self._service.add_funder(target_wallet, label="Target Dev")

        self._live_requested = enable_live
        self._simulation_requested = simulation_mode
        self._enable_live = False

        # 4. Update ExecutionCard
        with contextlib.suppress(Exception):
            exec_card = self.query_one("#execution-card", ExecutionCard)
            exec_card.snipe_size_sol = snipe_size_sol
            exec_card.watch_stage(exec_card.stage)

        # 5. Update TargetsTable & TargetProfileCard
        with contextlib.suppress(Exception):
            targets_table = self.query_one("#targets-table", TargetsTable)
            selected = targets_table.get_selected_target()
            addr = (
                target_wallet
                or (selected.address if selected else None)
                or self._wallet
                or ""
            )
            if addr and addr != _SYSTEM_PROGRAM_ID:
                strat = TargetStrategy(
                    monitoring_enabled=True,
                    execution_mode=TargetExecutionMode.LIVE
                    if enable_live
                    else TargetExecutionMode.SIMULATED,
                    size_sol=snipe_size_sol,
                    take_profit_pct=take_profit_pct,
                    stop_loss_pct=stop_loss_pct,
                    priority_fee_microlamports=priority_fee,
                    jito_tip_sol=jito_tip_sol,
                    slippage_bps=max_slippage_bps,
                    max_gas_sol=max_gas_cap_sol,
                    min_winrate_pct=min_winrate_pct,
                    max_entry_mc_usd=max_entry_mc_usd,
                    required_block_zero=req_b0,
                    funding_match_required=req_funding,
                )
                record = TargetRecord(
                    address=addr,
                    label="Target Dev",
                    strategy=strat,
                    perf_metric=(
                        "Live configured"
                        if enable_live
                        else "Route simulation armed"
                        if simulation_mode
                        else "Dry Run Armed"
                    ),
                )
                targets_table.set_targets([record])
                self.query_one("#target-profile-card", TargetProfileCard).update_target(
                    record
                )

        with contextlib.suppress(Exception):
            self.query_one("#settings-status", Static).update(
                f"[bold green]✓ Settings Saved! Size: {snipe_size_sol:.3f} SOL · TP: +{take_profit_pct:.0f}% · SL: {stop_loss_pct:.0f}% · Route: {routing_policy}[/bold green]"
            )
        self.notify(
            f"Settings saved & applied: {snipe_size_sol:.3f} SOL size",
            severity="information",
        )

    def _handle_add_funder_btn(self) -> None:
        val = self.query_one("#new-funder-input", Input).value.strip()
        if not val:
            return
        try:
            Pubkey.from_string(val)
            self._service.add_funder(val, label="Manual UI Funder")
            if self._repository.get_target_execution_policy(val) is None:
                self._service.save_target_execution_policy(
                    TargetExecutionPolicy(
                        funder_address=val,
                        monitoring_enabled=True,
                        execution_mode=TargetExecutionMode.SIMULATED,
                        quote_size_lamports=25_000_000,
                        take_profit_pnl_ppm=100_000,
                        stop_loss_pnl_ppm=-30_000,
                        max_slippage_bps=500,
                        priority_fee_microlamports=50_000,
                        jito_tip_lamports=1_500_000,
                        updated_at=datetime.now(UTC).isoformat(),
                    )
                )
            self.query_one("#new-funder-input", Input).value = ""
            self._refresh_target_records()
            funders = [
                f.address for f in self._repository.get_funders(enabled_only=True)
            ]
            self.query_one("#live-activity-view", LiveActivityView).set_funders(funders)
            self._refresh_header_counts()
            self.notify(f"Added target {val[:6]}... to SQLite", severity="information")
        except Exception as e:
            self.notify(f"Invalid Pubkey: {e}", severity="error")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        btn_id = event.button.id or ""
        if (
            btn_id == "back-to-dashboard-btn"
            or btn_id.endswith("-back-btn")
            or "back-to-dashboard-btn" in event.button.classes
        ):
            self.action_show_overview()
            return
        dispatch = {
            "btn-exec-go-live": self.action_toggle_live_trading,
            "btn-exec-edit": self.action_analyze_target,
            "btn-exec-simulate": self.action_simulate_candidate,
            "btn-exec-ignore": self.action_ignore_candidate,
            "btn-exec-exit": self.action_exit_position,
            "btn-exec-sell50": self.action_quick_sell,
            "add-funder-btn": self._handle_add_funder_btn,
            "save-target-policy-btn": self._save_target_policy,
            "save-settings-btn": self._save_settings,
        }
        handler = dispatch.get(btn_id)
        if handler:
            handler()

    # --- Test report compatibility methods ---
    def _render_report(self, report: WalletIntelligenceReport) -> None:
        """Render legacy WalletIntelligenceReport for backward compatibility in tests."""
        self._render_launches(report)
        nodes_table = self.query_one("#nodes-table", DataTable)
        nodes_table.clear()
        for node in report.nodes:
            nodes_table.add_row(
                node.address,
                "Target" if node.is_target else "Peer",
                "active",
                str(node.first_seen_slot),
            )

        edges_table = self.query_one("#edges-table", DataTable)
        edges_table.clear()
        for edge in report.edges:
            edges_table.add_row(
                edge.target,
                edge.source,
                edge.source,
                "1",
                "funded",
                str(edge.last_slot),
            )

        self.query_one("#flow-panel", Static).update(format_flow(report))
        self.query_one("#graph-map", Static).update(format_graph_map(report))

    def _render_launches(self, report: WalletIntelligenceReport) -> None:
        """Render launches table from report."""
        table = self.query_one("#launches-table", DataTable)
        table.clear()
        launch_filter = ""
        with contextlib.suppress(Exception):
            launch_filter = (
                self.query_one("#launch-filter", Input).value.strip().lower()
            )

        for launch in report.launches:
            if launch_filter and not launch_matches(launch, launch_filter):
                continue
            table.add_row(
                launch.mint,
                launch.symbol,
                launch.name,
                launch.creator,
                str(launch.slot),
                launch.signature,
            )


# Backwards compatibility alias
WalletIntelApp = RugbotTuiApp
