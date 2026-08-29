"""Terminal User Interface (TUI) application for Rugbot."""

# ruff: noqa: C901, PLR0913, PLR0915, PLR2004, F401, TC002, BLE001, S105, S110, TRY003, FBT001, PLR0912

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import math
import os
import time
import webbrowser
from collections.abc import Sequence
from dataclasses import replace
from datetime import UTC, datetime, timezone
from decimal import Decimal, InvalidOperation
from http import HTTPStatus
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar, Final

from rich import box
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from sol_trade_sdk.solana.provider_pool import (
    AiohttpRpcTransport,
    RpcHttpTransport,
    RpcProviderPool,
)
from solders.pubkey import Pubkey
from textual import events
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

from rugbot.backtest.runners.cluster_optimizer import (
    HistoricalTokenSample,
    run_cluster_tp_grid_search,
)
from rugbot.domain.decisions import AbstainReason, AbstainResult
from rugbot.domain.transfers import SolTransfer
from rugbot.ingest.pump.models import TokenLaunch
from rugbot.intelligence.token_resolver import resolve_token_or_wallet
from rugbot.intelligence.wallet_intelligence import (
    WalletIntelligenceReport,
    WalletLaunch,
    WalletLink,
    WalletNode,
    scan_wallet_intelligence,
)
from rugbot.interfaces.tui.clipboard import get_system_clipboard
from rugbot.interfaces.tui.formatters import (
    format_age,
    format_amount,
    format_flow,
    format_graph_map,
    format_network_endpoint,
    format_sol,
    format_timestamp,
    launch_matches,
    report_delta,
    short_address,
)
from rugbot.interfaces.tui.settings_spec import (
    boolean,
    build_settings_document,
    config_widget_values,
    decimal_value,
    format_percent_from_ppm,
    format_sol_from_lamports,
    integer,
    lamports_from_sol,
    level_widget_values,
    ppm_from_percent,
    text,
)
from rugbot.interfaces.tui.widgets import (
    ActivityItem,
    BacktestMatrixWidget,
    ClusterGraphModal,
    ClusterGraphWidget,
    CompactHeader,
    DetailInspectModal,
    DevHistoryCard,
    EmptyStateView,
    EventInspector,
    EventLogTicker,
    ExecutionCard,
    FunderCardInfo,
    HelpCheatsheetScreen,
    LiveActivityView,
    OperatorStage,
    PositionExecutionPanel,
    RiskBar,
    TargetProfileCard,
    TargetsTable,
    TokenDetailCard,
    WalletPnlHistory,
    WalletPnlPanel,
    WalletRiskPanel,
    WatchingView,
)
from rugbot.runtime.app import build_ui_runtime
from rugbot.runtime.config import (
    SniperConfigError,
    resolve_dotenv,
    resolve_state_dir,
)
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

if TYPE_CHECKING:
    from rugbot.runtime.app import RugbotApp
    from rugbot.runtime.event_bus import EventBus
    from rugbot.runtime.sniper_runtime import SniperRuntime
    from rugbot.runtime.workers.sniper_daemon import SniperDaemonService
    from rugbot.storage.tracker import SQLiteTrackerRepository
    from rugbot.tracker.service import TrackerService

__all__ = [
    "RugbotTuiApp",
    "format_age",
    "format_amount",
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


def _setting_line(
    label: str,
    widget_id: str,
    *,
    placeholder: str = "",
    value: str = "",
) -> Horizontal:
    """Build one labeled settings input row."""
    return Horizontal(
        Label(label, classes="setting-label"),
        Input(id=widget_id, placeholder=placeholder, value=value),
        classes="setting-line",
    )


def _setting_check(label: str, widget_id: str, value: bool) -> Horizontal:
    """Build one labeled settings checkbox row."""
    return Horizontal(
        Checkbox(label, id=widget_id, value=value),
        classes="setting-line",
    )


def _compose_settings_pane(
    wallet: str | None,
    live_requested: bool,
) -> ComposeResult:
    """Build the four-card settings grid plus background schema fields."""
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
                    yield Static("1. SELECTED TARGET & SIZING", classes="card-header")
                    with Vertical(classes="card-body"):
                        with Horizontal(classes="setting-line"):
                            yield Label("Target Dev / Token", classes="setting-label")
                            yield Input(
                                id="target-wallet",
                                placeholder="Solana Pubkey or Token Mint...",
                                value=wallet or "",
                            )
                            yield Button(
                                "📋 Paste",
                                variant="default",
                                id="btn-paste-target-settings",
                                classes="paste-btn",
                            )
                        yield _setting_line(
                            "Dev Alias / Label",
                            "target-alias",
                            placeholder="e.g. Mega-Bundle Dev...",
                        )
                        yield _setting_line(
                            "Buy Size (SOL)",
                            "snipe-size-sol",
                            placeholder="0.010",
                            value="0.010",
                        )
                        yield _setting_line(
                            "Priority Fee (µL)",
                            "priority-fee",
                            placeholder="50000",
                            value="50000",
                        )
                        yield _setting_line(
                            "Jito MEV Tip (SOL)",
                            "jito-tip",
                            placeholder="0.0010",
                            value="0.0010",
                        )
                        yield _setting_line(
                            "Max Gas Cap (SOL)",
                            "max-gas-cap",
                            placeholder="0.0050",
                            value="0.0050",
                        )
                with Vertical(classes="settings-card"):
                    yield Static("2. EXITS & SLIPPAGE", classes="card-header")
                    with Vertical(classes="card-body"):
                        yield _setting_line(
                            "Take Profit (%)",
                            "take-profit-pct",
                            placeholder="100.0",
                            value="100.0",
                        )
                        yield _setting_line(
                            "Stop Loss (%)",
                            "stop-loss-pct",
                            placeholder="-30.0",
                            value="-30.0",
                        )
                        yield _setting_line(
                            "Max Slippage (BPS)",
                            "max-slippage",
                            placeholder="500",
                            value="500",
                        )
                        yield _setting_line(
                            "Snipe Delay (sec)",
                            "snipe-delay",
                            placeholder="0",
                            value="0",
                        )
                        yield _setting_line(
                            "No Activity Exit (s)",
                            "rule-no-activity",
                            placeholder="0",
                            value="0",
                        )
            # Row 2: Qualification & Routing
            with Horizontal(classes="settings-row"):
                with Vertical(classes="settings-card"):
                    yield Static("3. QUALIFICATION & RULES", classes="card-header")
                    with Vertical(classes="card-body"):
                        yield _setting_line(
                            "Max Entry MC ($ USD)",
                            "max-entry-mc",
                            placeholder="15000",
                            value="15000",
                        )
                        yield _setting_line(
                            "Min Dev Winrate (%)",
                            "min-winrate-pct",
                            placeholder="40.0",
                            value="40.0",
                        )
                        yield _setting_line(
                            "Max Consec Losses",
                            "rule-max-losses",
                            placeholder="3",
                            value="3",
                        )
                        yield _setting_check(
                            "Require Block 0 Inclusion",
                            "require-block-zero",
                            value=True,
                        )
                        yield _setting_check(
                            "Require Funding Pattern Match",
                            "require-funding-match",
                            value=True,
                        )
                with Vertical(classes="settings-card"):
                    yield Static("4. EXECUTION & ROUTING", classes="card-header")
                    with Vertical(classes="card-body"):
                        yield _setting_line(
                            "Routing Policy",
                            "routing-policy",
                            placeholder="jito",
                            value="jito",
                        )
                        yield _setting_line(
                            "Execution Mode",
                            "execution-mode",
                            placeholder="observe",
                            value="observe",
                        )
                        yield _setting_line(
                            "Target Kind",
                            "target-kind",
                            placeholder="wallet",
                            value="wallet",
                        )
                        yield _setting_check(
                            "Live Trading Mode",
                            "execution-mode-live",
                            live_requested,
                        )
                        yield _setting_check(
                            "Buy Only Once", "rule-buy-once", value=False
                        )

        # Background schema fields kept mounted for full-schema round-trip.
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
            yield Checkbox("Bundle match", id="strategy-bundle", value=False)
            yield Checkbox(
                "Double signature", id="strategy-double-signature", value=False
            )
            yield Checkbox("Prior zero", id="strategy-prior-zero", value=False)
            yield Checkbox(
                "Historical qualification", id="strategy-historical", value=False
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


class RugbotTuiApp(App[None]):
    """Main Textual Terminal User Interface for Rugbot."""

    CSS_PATH: ClassVar[str] = "styles.tcss"

    BINDINGS: ClassVar[list[Binding]] = [
        Binding(
            "1", "show_tracker", "Dashboard", key_display="1", show=True, priority=True
        ),
        Binding(
            "2",
            "show_launches",
            "Dev History",
            key_display="2",
            show=True,
            priority=True,
        ),
        Binding(
            "3", "show_sniper", "Sniper", key_display="3", show=True, priority=True
        ),
        Binding(
            "4", "show_settings", "Settings", key_display="4", show=True, priority=True
        ),
        Binding(
            "f",
            "show_funding_graph",
            "Cluster Graph",
            key_display="5/F",
            show=True,
            priority=True,
        ),
        Binding(
            "a", "add_target", "Add Dev", key_display="A", show=True, priority=True
        ),
        Binding(
            "b", "run_backtest", "Backtest", key_display="B", show=True, priority=True
        ),
        Binding(
            "e",
            "context_action",
            "Edit Policy",
            key_display="E",
            show=True,
            priority=True,
        ),
        Binding(
            "l",
            "toggle_live_trading",
            "Live/Sim",
            key_display="L",
            show=True,
            priority=True,
        ),
        Binding(
            "p", "pause_target", "Pause", key_display="P", show=True, priority=True
        ),
        Binding(
            "c", "clear_targets", "Clear", key_display="C", show=True, priority=True
        ),
        Binding(
            "h",
            "context_secondary_action",
            "Sell 50%",
            key_display="H",
            show=True,
            priority=True,
        ),
        Binding(
            "x",
            "context_dismiss_action",
            "Exit 100%",
            key_display="X",
            show=True,
            priority=True,
        ),
        Binding(
            "slash",
            "toggle_search",
            "Search",
            key_display="/",
            show=True,
            priority=True,
        ),
        Binding(
            "question_mark",
            "show_help",
            "Cheatsheet",
            key_display="?",
            show=True,
            priority=True,
        ),
        Binding("q", "quit", "Quit", key_display="Q", show=True, priority=True),
        # Aliases / Background keys
        Binding("f1", "show_tracker", "Tracker", show=False, priority=True),
        Binding("f2", "show_backtester", "Backtest", show=False, priority=True),
        Binding("f3", "show_sniper", "Sniper", show=False, priority=True),
        Binding("f4", "show_settings", "Settings", show=False, priority=True),
        Binding(
            "ctrl+p", "show_command_palette", "Commands", show=False, priority=True
        ),
        Binding("s", "show_settings", "Settings", show=False, priority=True),
        Binding("n", "analyze_target", "Add Target", show=False, priority=True),
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
        Binding("ctrl+v", "paste_clipboard", "Paste", show=False, priority=True),
        Binding("shift+insert", "paste_clipboard", "Paste", show=False, priority=True),
        Binding("escape", "clear_focus", "Back", show=False, priority=True),
    ]

    def __init__(
        self,
        wallet: str | None = None,
        *,
        endpoint: str = "https://api.mainnet-beta.solana.com",
        fallback_endpoints: tuple[str, ...] = (),
        websocket_endpoint: str | None = None,
        max_transactions: int = 100,
        max_linked_wallets: int = 8,
        refresh_seconds: int = 15,
        as_of_slot: int | None = None,
        state_dir: Path | None = None,
        theme: str = "textual-dark",
        enable_live: bool = False,
        transport: RpcHttpTransport | None = None,
        core: RugbotApp | None = None,
        sniper_daemon: SniperDaemonService | None = None,
        sniper_runtime: SniperRuntime | None = None,
    ) -> None:
        super().__init__()
        self._wallet = wallet
        self._endpoint = endpoint
        self._fallback_endpoints = fallback_endpoints
        self._websocket_endpoint = websocket_endpoint
        self._max_transactions = max_transactions
        self._max_linked_wallets = max_linked_wallets
        self._refresh_seconds = refresh_seconds
        self._as_of_slot = as_of_slot
        self._state_dir = resolve_state_dir(state_dir)
        self._pnl_history = WalletPnlHistory(self._state_dir / "wallet_pnl.jsonl")
        # PnL belongs to the execution wallet, never to the tracked developer.
        self._pnl_wallet_address = ""
        # This is a persisted request only; actual live execution still requires
        # CLI authorization, a matching env key, and every runtime safety gate.
        self._live_requested = bool(enable_live)
        self._simulation_requested = False
        self._enable_live = False
        self._transport = (
            RpcProviderPool((endpoint, *fallback_endpoints))
            if transport is None and fallback_endpoints
            else transport
        )
        self.theme = theme

        # Drive the TUI from the shared RugbotCore facade. When no core is
        # injected, compose one through the canonical factory so the app never
        # builds its own runtime stack.
        if core is not None:
            self._core = core
        elif self._websocket_endpoint is not None:
            self._core = build_ui_runtime(
                state_dir=self._state_dir,
                wallet=wallet,
                sniper_runtime=sniper_runtime,
                sniper_daemon=sniper_daemon,
                endpoint=self._endpoint,
                websocket_endpoint=self._websocket_endpoint,
                transport=self._transport,
            )
        else:
            self._core = build_ui_runtime(
                state_dir=self._state_dir,
                wallet=wallet,
                sniper_runtime=sniper_runtime,
                sniper_daemon=sniper_daemon,
            )

        # Activity cache for instant causal lookup
        self._activity_events: dict[str, ActivityItem] = {}
        self._rendered_launch_mints: set[str] = set()
        # Live funder balances and token holdings cache
        self._funder_balances: dict[str, int] = {}
        self._funder_tokens: dict[str, list[dict[str, Any]]] = {}

    @property
    def _repository(self) -> SQLiteTrackerRepository:
        """Delegate tracker repository access to the shared core."""
        return self._core.repository

    @property
    def _service(self) -> TrackerService:
        """Delegate tracker service access to the shared core."""
        return self._core.service

    @property
    def _sniper_runtime(self) -> SniperRuntime | None:
        """Delegate sniper runtime access to the shared core."""
        return self._core.sniper_runtime

    @property
    def _sniper_daemon(self) -> SniperDaemonService | None:
        """Delegate sniper daemon access to the shared core."""
        return self._core.sniper_daemon

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
                        yield Button(
                            "🎯 Run Backtest (B)",
                            variant="primary",
                            id="run-backtest-btn",
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
                            yield BacktestMatrixWidget(id="backtest-matrix-widget")
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
                yield from _compose_settings_pane(self._wallet, self._live_requested)

            # Tab 5: Cluster Graph
            with TabPane("5: Cluster Graph", id="graph-tab"):
                with Vertical(classes="tab-body"):
                    yield Static(
                        "DEV ON-CHAIN CLUSTER & SATELLITE WALLETS (IN-PLACE EXPLORER)",
                        classes="table-header",
                    )
                    with Horizontal(classes="toolbar-row"):
                        yield Button(
                            "← Back to Dashboard (Esc)",
                            variant="default",
                            id="graph-back-btn",
                            classes="back-to-dashboard-btn",
                        )
                        yield Button(
                            "[+] Track Selected",
                            variant="success",
                            id="graph-track-btn",
                        )
                        yield Button(
                            "🎯 Backtest",
                            variant="default",
                            id="graph-backtest-btn",
                        )
                        yield Button(
                            "🌐 Solscan / GMGN",
                            variant="warning",
                            id="graph-explorer-btn",
                        )
                        yield Input(
                            placeholder="Dev Wallet or Token Mint...",
                            id="new-funder-input",
                        )
                        yield Button(
                            "📋 Paste",
                            variant="default",
                            id="btn-paste-funder-graph",
                            classes="paste-btn",
                        )
                        yield Input(
                            placeholder="Alias / Name (optional)...",
                            id="new-funder-alias",
                        )
                        yield Button(
                            "Add Target", variant="primary", id="add-funder-btn"
                        )
                        yield Input(
                            placeholder="Wallet / Funder...",
                            id="wallet-input",
                            classes="legacy-compat",
                        )
                    yield ClusterGraphWidget(id="cluster-graph-widget")
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
        yield Static(
            r"[bold yellow]\[1][/bold yellow] Dashboard  "
            r"[bold yellow]\[2][/bold yellow] Dev History  "
            r"[bold yellow]\[3][/bold yellow] Sniper  "
            r"[bold yellow]\[4][/bold yellow] Settings  "
            r"[bold yellow]\[5/F][/bold yellow] Cluster Graph  │  "
            r"[bold cyan]\[A][/bold cyan] Add Dev/Token  "
            r"[bold cyan]\[B][/bold cyan] Backtest  "
            r"[bold cyan]\[E][/bold cyan] Edit  "
            r"[bold cyan]\[L][/bold cyan] Live/Sim  "
            r"[bold cyan]\[P][/bold cyan] Pause  "
            r"[bold red]\[C][/bold red] Clear  "
            r"[bold red]\[H][/bold red] Sell 50%  "
            r"[bold red]\[X][/bold red] Exit 100%  │  "
            r"[bold white]\[/][/bold white] Search  "
            r"[bold magenta]\[?][/bold magenta] Cheatsheet  "
            r"[bold white]\[Q][/bold white] Quit",
            id="footer-actions-bar",
        )
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

        # Subscribe to EventBus domain events through the shared core
        self._core.subscribe(self._on_domain_event)

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
        self.run_worker(self._start_tracking(), name="tracked_launch_start")
        # Historical discovery only. New-launch alerts are WSS-driven.
        self.run_worker(self._poll_observation_worker(), name="observation_worker")

    async def _start_tracking(self) -> None:
        """Start WSS tracking and render launch alerts left in the TUI outbox."""

        try:
            await self._core.start()
            self._refresh_header_counts()
            await self._drain_tui_outbox()
        except Exception as error:
            self.notify(
                f"Launch tracking failed to start: {type(error).__name__}",
                severity="error",
            )
            raise

    async def _drain_tui_outbox(self) -> None:
        """Render durable launch alerts created while this TUI was offline."""

        for alert in self._repository.get_undelivered_alerts("tui"):
            launch = self._repository.get_launch(alert.mint)
            if launch is None:
                continue
            self._on_domain_event(
                LaunchDetected(
                    root_funder=launch.root_funder,
                    wallet=launch.creator_wallet,
                    timestamp=launch.created_at,
                    data={
                        "symbol": launch.symbol,
                        "name": launch.name,
                        "mint": launch.mint,
                        "creator": launch.creator_wallet,
                        "root_funder": launch.root_funder,
                        "depth": launch.depth,
                        "slot": launch.created_slot,
                        "signature": launch.created_signature,
                    },
                )
            )

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
        """Release the shared sniper runtime or daemon on application exit."""

        await self._core.stop()
        if self._sniper_runtime is not None:
            await self._sniper_runtime.close()
        elif self._sniper_daemon is not None:
            await self._sniper_daemon.stop()

    def _target_records(self) -> list[TargetRecord]:
        """Project persisted tracker funders and their policies into the TUI."""
        records = []
        for funder in self._repository.get_funders(enabled_only=False):
            if funder.address == _SYSTEM_PROGRAM_ID:
                continue
            launches = self._repository.get_launches_for_funder(funder.address)
            records.append(
                TargetRecord(
                    address=funder.address,
                    label=funder.label or "Tracked funder",
                    policy=self._repository.get_target_execution_policy(funder.address),
                    launches_count=len(launches),
                    winrate_pct=0.0,
                    avg_ath_pct=0.0,
                    perf_metric=f"{len(launches)} launches"
                    if launches
                    else "0 launches",
                )
            )
        return records

    def _ensure_config_target_policy(self) -> None:
        """Seed the active watch target from the verified watcher configuration."""
        try:
            from rugbot.storage.config_store import load_sniper_config_db

            config = load_sniper_config_db(self._state_dir)
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

    def _refresh_footer_actions(self) -> None:
        """Update persistent 2-line shortcuts bar with all core operator keys."""
        line1 = (
            r"[bold yellow]\[1][/bold yellow] Dashboard  "
            r"[bold yellow]\[2][/bold yellow] Dev History  "
            r"[bold yellow]\[3][/bold yellow] Sniper  "
            r"[bold yellow]\[4][/bold yellow] Settings  "
            r"[bold yellow]\[5/F][/bold yellow] Cluster Graph  │  "
            r"[bold cyan]\[A][/bold cyan] Add Dev/Token  "
            r"[bold cyan]\[B][/bold cyan] Backtest  "
            r"[bold cyan]\[E][/bold cyan] Edit  "
            r"[bold cyan]\[L][/bold cyan] Live/Sim  "
            r"[bold cyan]\[P][/bold cyan] Pause"
        )
        line2 = (
            r"[bold red]\[C][/bold red] Clear All  "
            r"[bold red]\[H][/bold red] Sell 50%  "
            r"[bold red]\[X][/bold red] Exit 100%  │  "
            r"[bold white]\[/][/bold white] Search  "
            r"[bold magenta]\[?][/bold magenta] Cheatsheet  "
            r"[bold white]\[Q][/bold white] Quit"
        )
        with contextlib.suppress(Exception):
            self.query_one("#footer-actions-bar", Static).update(f"{line1}\n{line2}")

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
            if token_mint:
                row_id = f"launch_{token_mint}"
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
        if row_id in self._activity_events:
            if isinstance(event, LaunchDetected) and token_mint:
                self._repository.mark_alerts_delivered("tui", (token_mint,))
            return
        self._activity_events[row_id] = item

        # Incremental insert into LiveActivityView
        try:
            self.query_one("#live-activity-view", LiveActivityView).add_event(item)
        except Exception:
            if isinstance(event, LaunchDetected):
                self._activity_events.pop(row_id, None)
            raise

        if isinstance(event, LaunchDetected) and token_mint:
            self._rendered_launch_mints.add(token_mint)
            self._repository.mark_alerts_delivered("tui", (token_mint,))
            self.notify(
                f"New ${token_sym} created by {event.wallet[:8]}…",
                severity="information",
                timeout=10,
            )

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
            launch_observation = self._core.launch_observation
            tracking_status = (
                launch_observation.status.value
                if launch_observation is not None
                else "stopped"
            )
            header.stream_status = tracking_status
            tracking_labels = {
                "pumpportal_live": "ONLINE PUMPPORTAL",
                "wss_live": "ONLINE WSS",
                "http_catchup": "DEGRADED HTTP CATCH-UP",
                "disconnected": "OFFLINE",
                "stopped": "STOPPED",
            }
            self.query_one(
                "#live-activity-view", LiveActivityView
            ).update_tracking_status(tracking_labels.get(tracking_status, "CONNECTING"))
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

    def action_show_help(self) -> None:
        """Display the full interactive operator shortcuts cheatsheet."""
        self.push_screen(HelpCheatsheetScreen())

    def action_clear_targets(self) -> None:
        """Clear all active targets from SQLite database and reset UI tables."""
        try:
            self._repository.clear_all_funders()
            self._refresh_target_records()
            self._refresh_tables()
            with contextlib.suppress(Exception):
                self.query_one("#live-activity-view", LiveActivityView).clear()
                self.query_one("#live-activity-view", LiveActivityView).set_funders([])
            self._refresh_header_counts()
            self.notify("Cleared all targets from database", severity="information")
        except Exception as e:
            self.notify(f"Could not clear targets: {e}", severity="error")

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

    def action_paste_clipboard(self) -> None:
        """Paste plain text from system clipboard into the currently focused input."""
        text = get_system_clipboard().strip()
        if not text:
            self.notify("Clipboard is empty", severity="warning")
            return
        focused = self.focused
        if isinstance(focused, Input):
            pos = focused.cursor_position
            val = focused.value
            focused.value = val[:pos] + text + val[pos:]
            focused.cursor_position = pos + len(text)
            self.notify(f"Pasted: {text[:24]}...", severity="information")
        else:
            with contextlib.suppress(Exception):
                if self.query_one(TabbedContent).active == "graph-tab":
                    inp = self.query_one("#new-funder-input", Input)
                else:
                    inp = self.query_one("#target-wallet", Input)
                inp.value = text
                inp.focus()
                self.notify(f"Pasted Target: {text[:24]}...", severity="information")

    def on_paste(self, event: events.Paste) -> None:
        """Handle terminal bracketed paste sequences."""
        if not event.text:
            return
        text = event.text.strip()
        focused = self.focused
        if isinstance(focused, Input):
            pos = focused.cursor_position
            val = focused.value
            focused.value = val[:pos] + text + val[pos:]
            focused.cursor_position = pos + len(text)
            self.notify(f"Pasted: {text[:24]}...", severity="information")
            event.prevent_default()
            event.stop()

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
        self._set_setting("target-alias", target.label or "")
        if target.policy is None:
            with contextlib.suppress(Exception):
                self.query_one("#settings-status", Static).update(
                    "[yellow]Tracker-only target: enter its policy, then save it.[/yellow]"
                )
            return
        policy = target.policy
        self._set_setting(
            "snipe-size-sol", format_sol_from_lamports(policy.quote_size_lamports)
        )
        self._set_setting(
            "take-profit-pct", format_percent_from_ppm(policy.take_profit_pnl_ppm)
        )
        self._set_setting(
            "stop-loss-pct", format_percent_from_ppm(policy.stop_loss_pnl_ppm)
        )
        self._set_setting("max-slippage", policy.max_slippage_bps)
        self._set_setting("priority-fee", policy.priority_fee_microlamports)
        self._set_setting(
            "jito-tip", format_sol_from_lamports(policy.jito_tip_lamports)
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

        values = self._collect_widget_values()
        mode = text(values, "execution-mode").lower()
        if boolean(values, "execution-mode-live"):
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
        jito_tip = decimal_value(values, "jito-tip")
        return TargetExecutionPolicy(
            funder_address=funder_address,
            monitoring_enabled=True,
            execution_mode=mode_by_name[mode],
            quote_size_lamports=lamports_from_sol(values, "snipe-size-sol"),
            take_profit_pnl_ppm=abs(ppm_from_percent(values, "take-profit-pct")),
            stop_loss_pnl_ppm=-abs(ppm_from_percent(values, "stop-loss-pct")),
            max_slippage_bps=integer(values, "max-slippage", minimum=0),
            priority_fee_microlamports=integer(values, "priority-fee", minimum=0),
            jito_tip_lamports=(
                lamports_from_sol(values, "jito-tip") if jito_tip > 0 else 0
            ),
            updated_at=datetime.now(UTC).isoformat(),
        )

    def _save_target_policy(self) -> None:
        """Persist settings only for the selected target; watcher YAML is unchanged."""
        input_address = text(self._collect_widget_values(), "target-wallet")
        resolved = None
        funder_address = input_address
        if input_address:
            try:
                resolved = resolve_token_or_wallet(
                    input_address,
                    rpc_url=self._endpoint,
                    fallback_endpoints=self._fallback_endpoints,
                )
                funder_address = resolved.target_wallet
            except Exception as exc:
                self.notify(f"Could not resolve target: {exc}", severity="warning")

        try:
            policy = self._policy_from_settings(funder_address)
        except (SniperConfigError, InvalidOperation, ValueError) as error:
            self.notify(f"Target policy rejected: {error}", severity="error")
            with contextlib.suppress(Exception):
                self.query_one("#settings-status", Static).update(
                    f"[bold red]Target policy rejected: {error}[/bold red]"
                )
            return

        label = (resolved.default_label if resolved else None) or "Configured target"
        if self._repository.get_funder(funder_address) is None:
            self._service.add_funder(funder_address, label=label)
        self._service.save_target_execution_policy(policy)

        if resolved and resolved.is_token and resolved.creation_slot:
            self._repository.save_launch(
                LaunchRecord(
                    mint=resolved.input_address,
                    creator_wallet=funder_address,
                    root_funder=funder_address,
                    symbol=resolved.symbol or "PUMP",
                    name=resolved.name or "Pump Token",
                    created_signature=resolved.creation_signature or "",
                    created_slot=resolved.creation_slot,
                    created_at=int(datetime.now(UTC).timestamp()),
                    depth=0,
                    funding_signature=None,
                    funding_amount_lamports=None,
                    funding_timestamp=None,
                )
            )

        if resolved and resolved.bundle_wallets:
            now_iso = datetime.now(UTC).isoformat()
            for sat in resolved.bundle_wallets:
                self._repository.save_wallet(
                    WalletRecord(
                        address=sat,
                        root_funder=funder_address,
                        parent_wallet=funder_address,
                        depth=1,
                        status=WalletStatus.FUNDED,
                        discovered_at=now_iso,
                        expires_at=None,
                        last_active_at=now_iso,
                    )
                )

        self._refresh_target_records()
        self._refresh_tables()
        with contextlib.suppress(Exception):
            self.query_one("#settings-status", Static).update(
                "[bold green]Target policy saved: "
                f"{short_address(funder_address)} · {format_sol_from_lamports(policy.quote_size_lamports)} SOL · "
                f"TP {format_percent_from_ppm(policy.take_profit_pnl_ppm)}% / "
                f"SL {format_percent_from_ppm(policy.stop_loss_pnl_ppm)}%[/bold green]"
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
        samples = [
            HistoricalTokenSample(
                mint=rec.mint,
                symbol=rec.symbol,
                creator_wallet=rec.creator_wallet,
                created_slot=rec.created_slot,
                created_at=rec.created_at,
                ath_multiplier=min(
                    5.0,
                    max(
                        1.10,
                        ((rec.funding_amount_lamports or 1_000_000_000) / 1_000_000_000)
                        * 0.5
                        + (rec.depth * 0.15)
                        + 1.0,
                    ),
                ),
                ath_delay_seconds=60 + (rec.depth * 20),
                rug_delay_seconds=180 + (rec.depth * 60),
                entry_mc_usd=8000.0,
                peak_mc_usd=16000.0,
            )
            for rec in target_launches
        ]

        report = run_cluster_tp_grid_search(
            root_funder=target.address,
            samples=samples,
            buy_size_sol=size_sol,
            realized_dump_loss_pct=0.75,
            jito_tip_sol=jito_sol,
            gas_fee_sol=gas_sol,
        )

        with contextlib.suppress(Exception):
            self.query_one(
                "#backtest-matrix-widget", BacktestMatrixWidget
            ).update_report(report)

        if not samples:
            target.launches_count = 0
            target.winrate_pct = 0.0
            target.avg_ath_pct = 0.0
            target.perf_metric = "0.00R (0 launches)"
            return (
                target,
                f"Target {short_address(target.address)}: BACKTEST RUN · 0 recorded launches in database.",
            )

        wins = 0
        losses = 0
        net_sol_total = 0.0
        for token in samples:
            if token.ath_multiplier >= (1.0 + tp_pct / 100.0):
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
        avg_ath_pct = (
            sum((s.ath_multiplier - 1.0) * 100 for s in samples) / len(samples)
            if samples
            else 0.0
        )

        perf_str = f"{total_r:+.2f}R ({wins}W/{losses}L {winrate_pct:.1f}% WR)"

        target.launches_count = len(target_launches)
        target.winrate_pct = winrate_pct
        target.avg_ath_pct = avg_ath_pct
        target.perf_metric = perf_str

        opt_text = (
            f"👑 Optimal TP: {report.optimal_tp_label} (Net EV: {report.optimal_net_ev_sol:+.4f} SOL)"
            if report.is_net_profitable
            else "⚠️ Cluster Unprofitable"
        )
        log_msg = (
            f"Cluster {short_address(target.address)} ({len(samples)} tokens): "
            f"Current TP +{tp_pct:.0f}% -> {winrate_pct:.1f}% WR ({net_sol_total:+.4f} SOL) │ "
            f"{opt_text} ✓"
        )
        with contextlib.suppress(Exception):
            self.query_one(
                "#backtest-matrix-widget", BacktestMatrixWidget
            ).update_report(report)

        return target, log_msg

    def action_run_backtest(self) -> None:
        """Run cluster Take-Profit grid optimization on the selected target and show metrics."""
        target_addr = self._wallet
        with contextlib.suppress(Exception):
            targets_table = self.query_one("#targets-table", TargetsTable)
            selected = targets_table.get_selected_target()
            if selected:
                target_addr = selected.address

        if not target_addr:
            funders = self._repository.get_funders()
            if funders:
                target_addr = funders[0].address

        if not target_addr:
            self.notify(
                "No target selected to backtest. Add a dev with 'A'.",
                severity="warning",
            )
            return

        with contextlib.suppress(Exception):
            targets_table = self.query_one("#targets-table", TargetsTable)
            target_rec = targets_table.get_target(target_addr)
            if target_rec is not None:
                updated_target, log_msg = self._execute_backtest_simulation(target_rec)
                targets_table.update_target(updated_target)
                self._refresh_tables()
                self._log_activity(log_msg)
                self.action_show_launches()
                self.notify(log_msg, severity="information")
                return

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
        """Switch to in-place Cluster Graph tab without modal overlay."""
        self.query_one(TabbedContent).active = "graph-tab"

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

    def on_input_submitted(self, event: Input.Submitted) -> None:
        """Handle Enter key submissions in input fields."""
        if event.input.id in {"new-funder-input", "new-funder-alias"}:
            self._handle_add_funder_btn()
        elif event.input.id == "target-wallet":
            self._save_target_policy()

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
        with contextlib.suppress(Exception):
            table = self.query_one("#nodes-table", DataTable)
            table.clear(columns=True)
            table.add_column("ALIAS / LABEL", key="label", width=26)
            table.add_column("DEV PUBKEY", key="address", width=44)
            table.add_column("MODE", key="status", width=12)
            table.add_column("CREATED", key="created_at", width=14)

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
            target_addr = ""
            target_label = ""
            with contextlib.suppress(Exception):
                targets_table = self.query_one("#targets-table", TargetsTable)
                selected = targets_table.get_selected_target()
                if selected:
                    target_addr = selected.address
                    target_label = selected.label

            if not target_addr and self._wallet:
                target_addr = self._wallet
                funder_rec = self._repository.get_funder(self._wallet)
                target_label = funder_rec.label if funder_rec else "Target Dev"

            self.query_one("#cluster-graph-widget", ClusterGraphWidget).update_cluster(
                target_addr, target_label, self._repository
            )
            funders = self._repository.get_funders()
            nodes_table = self.query_one("#nodes-table", DataTable)
            nodes_table.clear()
            if not funders:
                nodes_table.add_row(
                    "[dim]No Target[/dim]",
                    "[dim]Use toolbar above to add Dev or Token[/dim]",
                    "[dim]--[/dim]",
                    "[dim]--[/dim]",
                    key="empty",
                )
                return
            for funder in funders:
                policy = self._repository.get_target_execution_policy(funder.address)
                mode_str = (
                    f"[bold red]{policy.execution_mode.value.upper()}[/bold red]"
                    if policy and policy.execution_mode.value == "live"
                    else f"[bold yellow]{policy.execution_mode.value.upper()}[/bold yellow]"
                    if policy and policy.execution_mode.value in {"simulated", "paper"}
                    else "[dim]PAUSED[/dim]"
                )
                nodes_table.add_row(
                    funder.label or "Target Dev",
                    funder.address,
                    mode_str,
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
                token_display = (
                    f"[bold white]{launch.name}[/bold white] [cyan]${launch.symbol}[/cyan] [dim]({short_address(launch.mint)})[/dim]"
                    if launch.name and launch.name != launch.symbol
                    else f"[bold white]${launch.symbol}[/bold white] [dim]({short_address(launch.mint)})[/dim]"
                )
                launches_table.add_row(
                    token_display,
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
                token_display = (
                    f"[bold white]{launch.name}[/bold white] [cyan]${launch.symbol}[/cyan] [dim]({short_address(launch.mint)})[/dim]"
                    if launch.name and launch.name != launch.symbol
                    else f"[bold white]${launch.symbol}[/bold white] [dim]({short_address(launch.mint)})[/dim]"
                )
                launches_table.add_row(
                    token_display,
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
                header.wallet_balance_sol = format_sol_from_lamports(balance_lamports)
                net_lamports = points[-1].net_pnl_lamports
                net_value = format_sol_from_lamports(abs(net_lamports))
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
                    take_profit = format_percent_from_ppm(position.take_profit_pnl_ppm)
                    stop_loss = format_percent_from_ppm(position.stop_loss_pnl_ppm)
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
                            fallback_endpoints=self._fallback_endpoints,
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
        """Feed historical link evidence into the tracker without live alerts."""
        now_ts = int(datetime.now(UTC).timestamp())
        discovered_wallet = False
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
            discovered_wallet = (
                bool(self._service.handle_transfer(transfer)) or discovered_wallet
            )
        for launch in (*report.launches, *report.linked_launches):
            if launch.created_at is None:
                continue
            self._service.record_historical_launch(
                TokenLaunch(
                    signature=launch.signature,
                    slot=launch.slot,
                    timestamp=launch.created_at,
                    creator=launch.creator,
                    mint=launch.mint,
                    symbol=launch.symbol,
                    name=launch.name,
                )
            )
        if discovered_wallet:
            self.run_worker(
                self._core.refresh_launch_observation(),
                name="tracked_launch_refresh",
            )

    # --- Settings loading & saving ---
    def _load_settings_complete(self) -> None:
        """Hydrate every field from the DB-backed watcher config."""
        try:
            from rugbot.storage.config_store import load_sniper_config_db

            config = load_sniper_config_db(self._state_dir)
        except SniperConfigError as error:
            with contextlib.suppress(Exception):
                self.query_one("#settings-status", Static).update(
                    f"[bold red]Config error: {error}[/bold red]"
                )
            return

        for widget_id, value in config_widget_values(config).items():
            if isinstance(value, bool):
                self._set_checkbox(widget_id, value)
            else:
                self._set_setting(widget_id, value)
        for widget_id, value in level_widget_values(config).items():
            self._set_setting(widget_id, value)
        self._pnl_wallet_address = config.execution.signer_pubkey or ""
        with contextlib.suppress(Exception):
            self.query_one("#wallet-pnl-panel", WalletPnlPanel).update_history(
                self._pnl_wallet_address,
                self._pnl_history.read(self._pnl_wallet_address),
            )
        self._live_requested = config.execution.mode.value == "live"
        self._simulation_requested = config.execution.mode.value == "simulation"
        with contextlib.suppress(Exception):
            exec_card = self.query_one("#execution-card", ExecutionCard)
            target = self.query_one(
                "#targets-table", TargetsTable
            ).get_selected_target()
            if target is not None:
                exec_card.update_target(target)

    def _collect_widget_values(self) -> dict[str, str | bool]:
        """Snapshot every named settings control into a plain mapping."""
        return {
            **{w.id: w.value for w in self.query(Input) if w.id},
            **{w.id: w.value for w in self.query(Checkbox) if w.id},
        }

    def _set_setting(self, widget_id: str, value: object) -> None:
        with contextlib.suppress(Exception):
            self.query_one(f"#{widget_id}", Input).value = (
                "" if value is None else str(value)
            )

    def _set_checkbox(self, widget_id: str, value: bool) -> None:
        with contextlib.suppress(Exception):
            self.query_one(f"#{widget_id}", Checkbox).value = value

    def on_checkbox_changed(self, event: Checkbox.Changed) -> None:
        """Keep the optional mode shortcuts synchronized with the canonical mode."""
        if event.checkbox.id == "execution-mode-live":
            if event.value:
                self._set_setting("execution-mode", "live")
            elif text(self._collect_widget_values(), "execution-mode") == "live":
                self._set_setting("execution-mode", "observe")

    def _save_settings(self) -> None:
        """Validate and persist the complete watcher configuration to DB."""
        try:
            from rugbot.storage.config_store import (
                ConfigStore,
                parse_sniper_config_dict,
            )

            document = self._settings_document_from_widgets()
            config = parse_sniper_config_dict(document, source="db")
            ConfigStore(state_dir=self._state_dir).set_config("sniper", document)
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
                f"[bold green]✓ Saved {config.execution.mode.value} · {format_sol_from_lamports(config.execution.quote_size_lamports)} SOL · {config.execution.routing_policy}[/bold green]"
            )
        self.notify("Complete watcher configuration saved", severity="information")

    def _settings_document_from_widgets(self) -> dict[str, Any]:
        """Build the strict YAML mapping from every settings control."""
        return build_settings_document(self._collect_widget_values())

    def _handle_add_funder_btn(self) -> None:
        val = self.query_one("#new-funder-input", Input).value.strip()
        alias_val = ""
        with contextlib.suppress(Exception):
            alias_val = self.query_one("#new-funder-alias", Input).value.strip()
        if not val:
            return
        try:
            resolved = resolve_token_or_wallet(
                val,
                custom_label=alias_val or None,
                rpc_url=self._endpoint,
                fallback_endpoints=self._fallback_endpoints,
            )
            dev_addr = resolved.target_wallet
            dev_label = alias_val or resolved.default_label

            self._service.add_funder(dev_addr, label=dev_label)
            if self._repository.get_target_execution_policy(dev_addr) is None:
                self._service.save_target_execution_policy(
                    TargetExecutionPolicy(
                        funder_address=dev_addr,
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
            if resolved.is_token and resolved.creation_slot:
                self._repository.save_launch(
                    LaunchRecord(
                        mint=resolved.input_address,
                        creator_wallet=dev_addr,
                        root_funder=dev_addr,
                        symbol=resolved.symbol or "PUMP",
                        name=resolved.name or "Pump Token",
                        created_signature=resolved.creation_signature or "",
                        created_slot=resolved.creation_slot,
                        created_at=int(datetime.now(UTC).timestamp()),
                        depth=0,
                        funding_signature=None,
                        funding_amount_lamports=None,
                        funding_timestamp=None,
                    )
                )

            if resolved and resolved.bundle_wallets:
                now_iso = datetime.now(UTC).isoformat()
                for sat in resolved.bundle_wallets:
                    self._repository.save_wallet(
                        WalletRecord(
                            address=sat,
                            root_funder=dev_addr,
                            parent_wallet=dev_addr,
                            depth=1,
                            status=WalletStatus.FUNDED,
                            discovered_at=now_iso,
                            expires_at=None,
                            last_active_at=now_iso,
                        )
                    )

            self.query_one("#new-funder-input", Input).value = ""
            with contextlib.suppress(Exception):
                self.query_one("#new-funder-alias", Input).value = ""
            self._refresh_target_records()
            self._refresh_tables()
            funders = [
                f.address for f in self._repository.get_funders(enabled_only=True)
            ]
            self.query_one("#live-activity-view", LiveActivityView).set_funders(funders)
            self._refresh_header_counts()
            if resolved.is_token:
                self.notify(
                    f"Resolved Token -> Creator Dev {dev_addr[:6]}... ({dev_label})",
                    severity="information",
                )
            else:
                self.notify(
                    f"Added Target Dev {dev_addr[:6]}... ({dev_label})",
                    severity="information",
                )
        except Exception as e:
            self.notify(f"Invalid Address: {e}", severity="error")

    def _handle_graph_track_btn(self) -> None:
        """Enroll the selected row in nodes-table into SQLite as an active target."""
        try:
            table = self.query_one("#nodes-table", DataTable)
            if table.cursor_row < 0 or table.cursor_row >= len(table.rows):
                self.notify("Select a wallet in the table first", severity="warning")
                return
            row_key = list(table.rows.keys())[table.cursor_row]
            funder_addr = str(row_key.value)
            Pubkey.from_string(funder_addr)
            self._service.add_funder(funder_addr, label=f"Tracked {funder_addr[:6]}...")
            if self._repository.get_target_execution_policy(funder_addr) is None:
                self._service.save_target_execution_policy(
                    TargetExecutionPolicy(
                        funder_address=funder_addr,
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
            self._refresh_target_records()
            self._refresh_nodes_table()
            self.notify(
                f"Enrolled {funder_addr[:8]}... as Tracked Target!",
                severity="information",
            )
        except Exception as e:
            self.notify(f"Could not enroll target: {e}", severity="error")

    def _handle_graph_explorer_btn(self) -> None:
        """Open Solscan or GMGN for the selected wallet in nodes-table."""
        try:
            table = self.query_one("#nodes-table", DataTable)
            if table.cursor_row < 0 or table.cursor_row >= len(table.rows):
                return
            row_key = list(table.rows.keys())[table.cursor_row]
            funder_addr = str(row_key.value)
            webbrowser.open(f"https://solscan.io/account/{funder_addr}")
        except Exception as e:
            self.notify(f"Could not open explorer: {e}", severity="error")

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
            "btn-paste-target-settings": self._handle_paste_target_settings,
            "btn-paste-funder-graph": self._handle_paste_funder_graph,
            "add-funder-btn": self._handle_add_funder_btn,
            "save-target-policy-btn": self._save_target_policy,
            "save-settings-btn": self._save_settings,
            "graph-track-btn": self._handle_graph_track_btn,
            "graph-backtest-btn": self.action_show_backtester,
            "graph-explorer-btn": self._handle_graph_explorer_btn,
        }
        handler = dispatch.get(btn_id)
        if handler:
            handler()

    def _handle_paste_target_settings(self) -> None:
        text = get_system_clipboard().strip()
        if text:
            inp = self.query_one("#target-wallet", Input)
            inp.value = text
            inp.focus()
            self.notify(f"Pasted Target: {text[:24]}...", severity="information")
        else:
            self.notify("Clipboard is empty", severity="warning")

    def _handle_paste_funder_graph(self) -> None:
        text = get_system_clipboard().strip()
        if text:
            inp = self.query_one("#new-funder-input", Input)
            inp.value = text
            inp.focus()
            self.notify(f"Pasted Target: {text[:24]}...", severity="information")
        else:
            self.notify("Clipboard is empty", severity="warning")
