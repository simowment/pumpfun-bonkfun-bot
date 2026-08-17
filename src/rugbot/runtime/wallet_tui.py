"""Interactive Textual UI for finalized wallet intelligence reports."""

from __future__ import annotations

import argparse
import os
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar

from dotenv import load_dotenv
from rich.text import Text
from solders.pubkey import Pubkey
from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import (
    Button,
    Checkbox,
    DataTable,
    Footer,
    Header,
    Input,
    Label,
    Static,
    TabbedContent,
    TabPane,
)

from rugbot.decision.playbook_rules import ExitRuleState
from rugbot.domain.decisions import AbstainReason, AbstainResult
from rugbot.execution.ports import ExecutionIntent
from rugbot.execution.position_runtime import PaperPositionState
from rugbot.runtime.cli import _execution_port
from rugbot.runtime.config import (
    ExecutionMode,
    SniperConfigError,
    StrategyFilterSettings,
    load_sniper_config,
    load_sniper_document,
    save_sniper_document,
)
from rugbot.runtime.pump_market import PumpOnlineMarket
from rugbot.runtime.wallet_intelligence import (
    MIN_REPEAT_LAUNCH_EVIDENCE,
    WalletIntelligenceReport,
    WalletLaunch,
    rug_evidence_summary,
    scan_wallet_intelligence,
)
from rugbot.storage.sqlite_state_store import SqliteStateStore

if TYPE_CHECKING:
    from collections.abc import Sequence

    from rugbot.ingest.rpc_observer import RpcHttpTransport


SHORT_IDENTIFIER_LIMIT = 14
GRAPH_PREVIEW_LIMIT = 12
MAX_TUI_HISTORY = 100
MIN_POSITION_ROW_CELLS = 3
LAMPORTS_PER_SOL = 1_000_000_000
SOL_DECIMAL_PLACES = 9


class WalletIntelApp(App[None]):
    """Display one wallet intelligence report and refresh it on demand."""

    TITLE = "rugbot / wallet-intel"
    SUB_TITLE = ""
    CSS = """
    Screen {
        background: $background;
        color: $foreground;
    }
    Header {
        background: $background;
        color: $foreground;
        height: 3;
    }
    Footer {
        background: $surface;
        color: $foreground-muted;
    }
    #toolbar {
        height: 4;
        padding: 0 2;
        background: $surface;
        border-bottom: solid $border;
    }
    #toolbar-label {
        width: 8;
        padding: 1 1 0 0;
        color: $primary;
        text-style: bold;
    }
    #wallet-input {
        width: 1fr;
        border: none;
        background: $background;
        color: $foreground;
    }
    #wallet-input:focus {
        border: none;
        background: $panel;
    }
    .toolbar-button {
        min-width: 10;
        height: 3;
        margin-left: 1;
        padding: 0 1;
        border: none;
        background: $panel;
        color: $foreground;
        text-style: bold;
    }
    #refresh-button {
        color: $success;
    }
    .toolbar-button:hover,
    .toolbar-button:focus {
        background: $boost;
        color: $foreground;
    }
    #status-row {
        height: 2;
        padding: 0 2;
        background: $background;
        border-bottom: solid $border;
    }
    #status {
        width: 1fr;
        color: $foreground-muted;
    }
    #network,
    #execution-status,
    #tracking-status,
    #last-update {
        width: auto;
        padding: 0 1;
        color: $foreground-muted;
    }
    #status.ok,
    #execution-status.ok {
        color: $success;
    }
    #status.abstain,
    #execution-status.abstain {
        color: $error;
    }
    TabbedContent {
        height: 1fr;
    }
    TabbedContent > ContentTabs {
        background: $background;
        border-bottom: solid $border;
    }
    TabbedContent Tab {
        color: $foreground-muted;
    }
    TabbedContent Tab:hover {
        color: $foreground;
    }
    TabbedContent Tab.-active {
        background: $panel;
        color: $primary;
        text-style: bold;
    }
    TabPane {
        padding: 0 2;
    }
    #overview-scroll,
    #graph-scroll,
    #settings-scroll {
        height: 1fr;
    }
    #trade-scroll {
        height: 1fr;
        padding: 1 0;
    }
    #trade-grid {
        height: auto;
        width: 100%;
    }
    .trade-column {
        width: 1fr;
        padding-right: 2;
    }
    .trade-row {
        height: 3;
        width: 100%;
        margin-bottom: 1;
    }
    .trade-label {
        width: 20;
        padding: 1 1 0 0;
        color: $foreground-muted;
    }
    .trade-input {
        width: 1fr;
    }
    #buy-button {
        width: 13;
        margin-top: 1;
        background: $success;
        color: $background;
        text-style: bold;
    }
    #buy-button:hover,
    #buy-button:focus,
    #sell-button:hover,
    #sell-button:focus {
        background: $primary;
        color: $background;
    }
    #sell-button {
        width: 13;
        margin: 1 0 0 1;
        background: $error;
        color: $background;
        text-style: bold;
    }
    #trade-status {
        min-height: 3;
        margin-top: 1;
        color: $foreground-muted;
    }
    #target-summary {
        height: 1;
        padding: 0;
        color: $foreground-muted;
    }
    #metric-row {
        height: 5;
        padding: 1 0;
        border-bottom: solid $border;
    }
    .metric {
        width: 1fr;
        height: 3;
        margin-right: 1;
        padding: 0 1;
        border: solid $border;
        background: $surface;
        color: $foreground;
    }
    .section-title,
    .settings-heading {
        height: 2;
        padding: 1 0 0 0;
        color: $foreground-muted;
        text-style: bold;
    }
    .section-panel {
        height: auto;
        min-height: 3;
        padding: 0 0 1 0;
        background: $background;
    }
    #overview-grid {
        height: auto;
        min-height: 13;
        padding: 1 0 0 0;
    }
    #assessment-column,
    #flow-column {
        width: 1fr;
        padding-right: 2;
    }
    #assessment-panel {
        color: $warning;
    }
    #execution-panel,
    #flow-panel {
        color: $foreground-muted;
    }
    #graph-map {
        min-height: 9;
        padding: 1 2;
        border: solid $border;
        color: $foreground-muted;
    }
    #signals-panel {
        color: $foreground;
    }
    #warnings-panel {
        color: $warning;
    }
    #launch-summary,
    #graph-summary {
        height: 2;
        padding: 1 0 0 0;
        color: $foreground-muted;
    }
    #launch-tools {
        height: 3;
        margin-bottom: 1;
    }
    #launch-filter {
        width: 1fr;
    }
    #early-only {
        width: 18;
        padding: 1 0 0 1;
    }
    #launch-count {
        width: 20;
        padding: 1 0 0 1;
        color: $foreground-muted;
        text-align: right;
    }
    DataTable {
        height: 1fr;
        border: none;
        background: $background;
        color: $foreground;
    }
    DataTable > .datatable--header {
        background: $panel;
        color: $foreground;
    }
    DataTable > .datatable--even-row {
        background: $surface;
    }
    DataTable > .datatable--cursor {
        background: $boost;
        color: $foreground;
        text-style: bold;
    }
    #nodes-table,
    #edges-table,
    #switches-table,
    #positions-table {
        height: 12;
    }
    #positions-scroll {
        height: 1fr;
    }
    #settings-scroll {
        padding: 1 0;
    }
    .settings-heading {
        border-bottom: solid $border;
        color: $warning;
    }
    .settings-grid {
        height: auto;
        width: 100%;
    }
    .settings-column {
        width: 1fr;
        padding-right: 2;
    }
    .settings-row {
        height: 3;
        width: 100%;
        margin-bottom: 1;
    }
    .settings-label {
        width: 28;
        padding: 1 1 0 0;
        color: $foreground-muted;
    }
    .settings-input {
        width: 1fr;
    }
    .settings-input:focus {
        border: none;
        background: $panel;
    }
    #settings-save {
        width: 13;
        margin-top: 1;
        background: $success;
        color: $background;
        text-style: bold;
    }
    #settings-status {
        height: 2;
        margin-top: 1;
        color: $foreground-muted;
    }
    """
    BINDINGS: ClassVar[list[tuple[str, str, str]]] = [
        ("r", "refresh", "Refresh"),
        ("f", "focus_wallet", "Wallet"),
        ("1", "show_overview", "Overview"),
        ("2", "show_launches", "Launches"),
        ("3", "show_graph", "Graph"),
        ("4", "show_settings", "Settings"),
        ("5", "show_trade", "Buy"),
        ("6", "show_positions", "Positions"),
        ("t", "cycle_theme", "Theme"),
        ("q", "quit", "Quit"),
    ]

    def __init__(  # noqa: PLR0913
        self,
        wallet: str,
        *,
        endpoint: str,
        max_transactions: int = 100,
        max_linked_wallets: int = 8,
        refresh_seconds: int = 30,
        as_of_slot: int | None = None,
        transport: RpcHttpTransport | None = None,
        config_path: Path = Path("watch.yaml"),
        state_dir: Path = Path(".state/watch"),
        theme: str = "textual-dark",
        enable_live: bool = False,
    ) -> None:
        """Initialize the wallet screen without loading signing keys."""

        super().__init__()
        if theme not in self.available_themes:
            raise ValueError(f"unknown Textual theme: {theme}")  # noqa: TRY003
        self._wallet = wallet
        self._endpoint = endpoint
        self._max_transactions = max_transactions
        self._max_linked_wallets = max_linked_wallets
        self._refresh_seconds = refresh_seconds
        self._as_of_slot = as_of_slot
        self._transport = transport
        self._config_path = config_path
        self._state_dir = state_dir
        self._enable_live = enable_live
        self._refreshing = False
        self._trading = False
        self._last_report: WalletIntelligenceReport | None = None
        self._initial_focus_pending = True
        self.theme = theme

    def compose(self) -> ComposeResult:  # noqa: PLR0915
        """Build the TUI layout."""

        yield Header()
        with Horizontal(id="toolbar"):
            yield Label("WATCH", id="toolbar-label")
            yield Input(
                value=self._wallet,
                placeholder="developer wallet address",
                id="wallet-input",
            )
            yield Button("refresh", id="refresh-button", classes="toolbar-button")
            yield Button("Buy", id="trade-button", classes="toolbar-button")
            yield Button("Positions", id="positions-button", classes="toolbar-button")
            yield Button("Settings", id="settings-button", classes="toolbar-button")
        with Horizontal(id="status-row"):
            yield Static("scan pending", id="status")
            yield Static("RPC --", id="network")
            yield Static("SIGNER --", id="execution-status")
            yield Static("TRACK --", id="tracking-status")
            yield Static("--", id="last-update")
        with TabbedContent(initial="overview-tab"):
            with TabPane("Overview", id="overview-tab"):
                with VerticalScroll(id="overview-scroll"):
                    yield Static("--", id="target-summary")
                    with Horizontal(id="metric-row"):
                        yield Static(
                            format_metric(
                                "TX",
                                "--",
                                "successful / scanned",
                                self._theme_color("success"),
                            ),
                            id="activity-metric",
                            classes="metric",
                        )
                        yield Static(
                            format_metric(
                                "CREATES",
                                "--",
                                "target wallet",
                                self._theme_color("warning"),
                            ),
                            id="launch-metric",
                            classes="metric",
                        )
                        yield Static(
                            format_metric(
                                "EARLY",
                                "--",
                                "target + linked",
                                self._theme_color("warning"),
                            ),
                            id="early-metric",
                            classes="metric",
                        )
                        yield Static(
                            format_metric(
                                "LINKED",
                                "--",
                                "direct wallets",
                                self._theme_color("secondary"),
                            ),
                            id="link-metric",
                            classes="metric",
                        )
                        yield Static(
                            format_metric(
                                "SWITCH",
                                "--",
                                "wallet reuse",
                                self._theme_color("error"),
                            ),
                            id="switch-metric",
                            classes="metric",
                        )
                    with Horizontal(id="overview-grid"):
                        with Vertical(id="assessment-column"):
                            yield Label("qualification", classes="section-title")
                            yield Static(
                                "--", id="assessment-panel", classes="section-panel"
                            )
                            yield Label("capital flow", classes="section-title")
                            yield Static("--", id="flow-panel", classes="section-panel")
                        with Vertical(id="flow-column"):
                            yield Label(
                                "creator and market evidence", classes="section-title"
                            )
                            yield Static(
                                "--", id="signals-panel", classes="section-panel"
                            )
                            yield Label("execution", classes="section-title")
                            yield Static(
                                "--", id="execution-panel", classes="section-panel"
                            )
                    yield Label("data quality", classes="section-title")
                    yield Static(
                        "--",
                        id="warnings-panel",
                        classes="section-panel",
                    )
            with TabPane("Launches", id="launches-tab"):
                yield Static(
                    "--",
                    id="launch-summary",
                )
                with Horizontal(id="launch-tools"):
                    yield Input(
                        placeholder="filter symbol, mint, creator, signature",
                        id="launch-filter",
                    )
                    yield Checkbox("early only", id="early-only")
                    yield Static("0 shown", id="launch-count")
                    yield DataTable(
                        id="launches-table", cursor_type="row", zebra_stripes=True
                    )
            with TabPane("Graph", id="graph-tab"):
                with VerticalScroll(id="graph-scroll"):
                    yield Static(
                        "--",
                        id="graph-summary",
                    )
                    yield Static(
                        "No graph yet.",
                        id="graph-map",
                        classes="section-panel",
                    )
                    yield Label("Wallet nodes", classes="section-title")
                    yield DataTable(
                        id="nodes-table", cursor_type="row", zebra_stripes=True
                    )
                    yield Label("Observed transfer edges", classes="section-title")
                    yield DataTable(
                        id="edges-table", cursor_type="row", zebra_stripes=True
                    )
                    yield Label("Wallet-switch candidates", classes="section-title")
                    yield DataTable(
                        id="switches-table", cursor_type="row", zebra_stripes=True
                    )
            with TabPane("Buy", id="trade-tab"):
                with VerticalScroll(id="trade-scroll"):
                    yield Label("submit a Pump.fun buy", classes="section-title")
                    with Horizontal(id="trade-grid"):
                        with Vertical(classes="trade-column"):
                            yield Horizontal(
                                Label("token mint", classes="trade-label"),
                                Input(
                                    placeholder="base58 mint address",
                                    id="trade-mint",
                                    classes="trade-input",
                                ),
                                classes="trade-row",
                            )
                            yield Horizontal(
                                Label("buy amount SOL", classes="trade-label"),
                                Input(
                                    placeholder="0.001",
                                    id="trade-quote",
                                    classes="trade-input",
                                ),
                                classes="trade-row",
                            )
                            yield Horizontal(
                                Label("token units", classes="trade-label"),
                                Input(
                                    placeholder="base units for sell",
                                    id="trade-base",
                                    classes="trade-input",
                                ),
                                classes="trade-row",
                            )
                        with Vertical(classes="trade-column"):
                            yield Horizontal(
                                Label("slippage bps", classes="trade-label"),
                                Input(
                                    placeholder="500",
                                    id="trade-slippage",
                                    classes="trade-input",
                                ),
                                classes="trade-row",
                            )
                            yield Static(
                                "",
                                id="trade-status",
                                classes="section-panel",
                            )
                    yield Button("BUY", id="buy-button", variant="success")
                    yield Button("SELL", id="sell-button", variant="error")
                    yield Static(
                        "A live buy requires execution.mode: live and --enable-live. "
                        "Observe and paper never submit.",
                        id="trade-help",
                        classes="section-panel",
                    )
            with TabPane("Positions", id="positions-tab"):
                with VerticalScroll(id="positions-scroll"):
                    yield Static("OPEN POSITIONS  0", id="positions-summary")
                    yield DataTable(
                        id="positions-table", cursor_type="row", zebra_stripes=True
                    )
                    yield Static(
                        "Select a position to prefill Sell, or use the trade form "
                        "for a custom amount.",
                        id="positions-help",
                        classes="section-panel",
                    )
            with TabPane("Settings", id="settings-tab"):
                with VerticalScroll(id="settings-scroll"):
                    yield Static(
                        f"config  {self._config_path}",
                        id="settings-path",
                        classes="section-title",
                    )
                    yield from self._settings_inputs()
                    yield Button("Save", id="settings-save", variant="primary")
                    yield Static("--", id="settings-status")
        yield Footer()

    def _settings_inputs(self) -> list[object]:
        """Build the small editable strategy settings surface."""

        execution_fields = (
            ("signing wallet public address", "signer-wallet", "optional"),
            ("paper quote size (lamports)", "quote-size", "1000000"),
            ("maximum slippage (bps)", "max-slippage", "500"),
        )
        strategy_fields = (
            ("min volume (micro USD)", "min-volume", "30000000000"),
            ("max creator pairs", "max-pairs", "10"),
            ("history sample count", "history-sample", "10"),
            ("minimum win rate (ppm)", "min-win-rate", "500000"),
            ("maximum buys / hour", "max-buys-hour", "1"),
            ("maximum entry index", "max-entry-index", "1"),
            ("maximum market cap (quote units)", "max-entry-mc", "0"),
            ("entry deviation (ppm)", "entry-deviation", "250000"),
        )
        timing_fields = (
            ("snipe delay (seconds)", "snipe-delay", "0"),
            ("minimum MC (quote units)", "min-mc", "0"),
            ("maximum MC (quote units)", "max-mc", "0"),
            ("maximum token age (minutes)", "max-age", "0"),
            ("follow cooldown (seconds)", "follow-cooldown", "0"),
            ("maximum consecutive losses", "max-losses", "3"),
        )
        return [
            Static("Wallet and sizing", classes="settings-heading"),
            Horizontal(
                self._settings_column(execution_fields[:2]),
                self._settings_column(execution_fields[2:]),
                classes="settings-grid",
            ),
            Static("Strategy gates", classes="settings-heading"),
            Horizontal(
                self._settings_column(strategy_fields[:4]),
                self._settings_column(strategy_fields[4:]),
                classes="settings-grid",
            ),
            Static("Entry and exit", classes="settings-heading"),
            Horizontal(
                self._settings_column(timing_fields[:3]),
                self._settings_column(timing_fields[3:]),
                classes="settings-grid",
            ),
            Static("Evidence requirements", classes="settings-heading"),
            Horizontal(
                Vertical(
                    Checkbox("require bundle match", id="require-bundle"),
                    Checkbox("require double signature", id="require-double-signature"),
                    classes="settings-column",
                ),
                Vertical(
                    Checkbox("require prior zero balance", id="require-zero-balance"),
                    Checkbox(
                        "require historical qualification",
                        id="require-historical-qualification",
                    ),
                    Checkbox("buy only once", id="buy-once"),
                    classes="settings-column",
                ),
                classes="settings-grid",
            ),
        ]

    @staticmethod
    def _settings_column(
        fields: tuple[tuple[str, str, str], ...],
    ) -> Vertical:
        """Build one compact settings column."""

        return Vertical(
            *(
                Horizontal(
                    Label(label, classes="settings-label"),
                    Input(
                        placeholder=placeholder,
                        id=input_id,
                        classes="settings-input",
                    ),
                    classes="settings-row",
                )
                for label, input_id, placeholder in fields
            ),
            classes="settings-column",
        )

    def on_mount(self) -> None:
        """Initialize tables and start the first asynchronous scan."""

        self._configure_tables()
        self._load_settings()
        self._render_execution_summary()
        self._render_trade_state()
        self._render_positions()
        self.set_interval(self._refresh_seconds, self.action_refresh)
        self.action_refresh()
        self.set_timer(0.05, self._focus_refresh_button)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        """Refresh after the toolbar button is pressed."""

        if event.button.id == "refresh-button":
            self.action_refresh()
        elif event.button.id == "settings-button":
            self.action_show_settings()
        elif event.button.id == "trade-button":
            self.action_show_trade()
        elif event.button.id == "positions-button":
            self.action_show_positions()
        elif event.button.id == "settings-save":
            self._save_settings()
        elif event.button.id == "buy-button":
            self.action_buy()
        elif event.button.id == "sell-button":
            self.action_sell()

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        """Prefill a selected launch or position for the trade form."""

        if event.data_table.id == "launches-table":
            mint = str(event.row_key)
            try:
                Pubkey.from_string(mint)
            except ValueError:
                return
            self.query_one("#trade-mint", Input).value = mint
            self.action_show_trade()
            return
        if event.data_table.id != "positions-table":
            return
        row = event.data_table.get_row(event.row_key)
        if len(row) < MIN_POSITION_ROW_CELLS:
            return
        self.query_one("#trade-mint", Input).value = str(row[0])
        self.query_one("#trade-base", Input).value = str(row[2])
        self.action_show_trade()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        """Refresh after entering a new wallet."""

        if event.input.id == "wallet-input":
            self.action_refresh()

    def on_input_changed(self, event: Input.Changed) -> None:
        """Filter the launch table without triggering another RPC scan."""

        if event.input.id == "launch-filter" and self._last_report is not None:
            self._render_launches(self._last_report)

    def on_checkbox_changed(self, event: Checkbox.Changed) -> None:
        """Apply local launch filters without another RPC request."""

        if event.checkbox.id == "early-only" and self._last_report is not None:
            self._render_launches(self._last_report)

    def _focus_refresh_button(self) -> None:
        """Keep the initial wallet value readable until editing is requested."""

        if self._initial_focus_pending and self.is_mounted:
            self._initial_focus_pending = False
            self.set_focus(self.query_one("#refresh-button", Button))

    def action_refresh(self) -> None:
        """Start one exclusive asynchronous wallet scan."""

        if self._refreshing:
            return
        wallet = self.query_one("#wallet-input", Input).value.strip()
        self._wallet = wallet
        self._refreshing = True
        status = self.query_one("#status", Static)
        status.remove_class("ok", "abstain")
        status.update("SCANNING  finalized transactions...")
        self.run_worker(self._scan_and_render(wallet), exclusive=True)

    def action_focus_wallet(self) -> None:
        """Focus the wallet input for quick target changes."""

        self._initial_focus_pending = False
        wallet_input = self.query_one("#wallet-input", Input)
        self.set_focus(wallet_input)
        wallet_input.select_all()

    def action_show_overview(self) -> None:
        """Show the overview tab."""

        self.query_one(TabbedContent).active = "overview-tab"

    def action_show_launches(self) -> None:
        """Show the launch history tab."""

        self.query_one(TabbedContent).active = "launches-tab"

    def action_show_graph(self) -> None:
        """Show the linked-wallet graph tab."""

        self.query_one(TabbedContent).active = "graph-tab"

    def action_show_settings(self) -> None:
        """Show the validated strategy settings editor."""

        self.query_one(TabbedContent).active = "settings-tab"
        self._load_settings()

    def action_show_trade(self) -> None:
        """Show the direct buy controls."""

        self.query_one(TabbedContent).active = "trade-tab"
        self._render_trade_state()

    def action_show_positions(self) -> None:
        """Show durable paper/live positions."""

        self.query_one(TabbedContent).active = "positions-tab"
        self._render_positions()

    def action_buy(self) -> None:
        """Submit one validated buy intent through the configured port."""

        self._start_trade("buy")

    def action_sell(self) -> None:
        """Submit one validated sell intent through the configured port."""

        self._start_trade("sell")

    def _start_trade(self, side: str) -> None:
        """Validate one direct trade form and start its asynchronous submit."""

        if self._trading:
            return
        try:
            mint = self.query_one("#trade-mint", Input).value.strip()
            Pubkey.from_string(mint)
            amount_id = "#trade-quote" if side == "buy" else "#trade-base"
            amount_name = "quote amount" if side == "buy" else "token units"
            amount = (
                _sol_setting_lamports(
                    self.query_one(amount_id, Input).value,
                    amount_name,
                )
                if side == "buy"
                else _setting_positive_int(
                    self.query_one(amount_id, Input).value, amount_name
                )
            )
            slippage_bps = _bounded_setting_int(
                self.query_one("#trade-slippage", Input).value,
                "maximum slippage",
                maximum=10_000,
            )
        except (TypeError, ValueError, SniperConfigError) as error:
            self.query_one("#trade-status", Static).update(f"ABSTAIN  {error}")
            return
        self._trading = True
        self.query_one("#buy-button", Button).disabled = True
        self.query_one("#sell-button", Button).disabled = True
        amount_display = (
            f"{format_sol(amount)} SOL" if side == "buy" else f"{amount} units"
        )
        self.query_one("#trade-status", Static).update(
            f"{side.upper()} REQUESTED  {short_address(mint)}  {amount_display}"
        )
        self.run_worker(
            self._submit_trade(side, mint, amount, slippage_bps), exclusive=True
        )

    async def _submit_trade(
        self, side: str, mint: str, amount: int, slippage_bps: int
    ) -> None:
        """Build one current-slot trade intent and render its receipt."""

        status = self.query_one("#trade-status", Static)
        try:
            config = load_sniper_config(self._config_path)
            if config.execution.mode is ExecutionMode.LIVE and not self._enable_live:
                status.update(
                    f"ABSTAIN  live {side} is locked; restart TUI with --enable-live"
                )
                return
            execution_port = _execution_port(
                config.execution.mode,
                self._endpoint,
                allow_live=self._enable_live,
                expected_signer_pubkey=config.execution.signer_pubkey,
            )
            as_of_slot = await self._buy_as_of_slot()
            receipt = await execution_port.submit(
                ExecutionIntent(
                    intent_id=f"tui-{side}:{mint}:{as_of_slot}",
                    as_of_slot=as_of_slot,
                    market_id=mint,
                    side=side,
                    quote_amount_base_units=amount if side == "buy" else None,
                    base_amount_base_units=amount if side == "sell" else None,
                    max_slippage_bps=slippage_bps,
                    reason_codes=(f"manual_tui_{side}",),
                )
            )
            if receipt.accepted:
                signature = (
                    short_address(receipt.signature) if receipt.signature else "paper"
                )
                status.update(
                    f"{receipt.mode.value.upper()} {side.upper()} ACCEPTED  "
                    f"{signature}  |  "
                    f"{receipt.message}"
                )
                position_error = self._persist_trade_position(
                    side=side,
                    mint=mint,
                    amount=amount,
                    receipt=receipt,
                    as_of_slot=as_of_slot,
                )
                if position_error is not None:
                    status.update(
                        f"{receipt.mode.value.upper()} {side.upper()} ACCEPTED, "
                        f"POSITION SAVE FAILED  {signature}  |  {position_error}"
                    )
                self._render_positions()
            elif receipt.would_submit_transaction:
                signature = (
                    short_address(receipt.signature)
                    if receipt.signature
                    else "signature unavailable"
                )
                status.update(
                    f"{receipt.mode.value.upper()} {side.upper()} SUBMITTED, "
                    f"CONFIRMATION UNKNOWN  {signature}  |  {receipt.message}"
                )
            else:
                status.update(
                    f"{receipt.mode.value.upper()} {side.upper()} NOT SUBMITTED  |  "
                    f"{receipt.message}"
                )
        except (OSError, TypeError, ValueError, SniperConfigError) as error:
            status.update(
                f"ABSTAIN  {side} unavailable: {type(error).__name__}: {error}"
            )
        except Exception as error:  # noqa: BLE001
            status.update(f"ABSTAIN  {side} failed: {type(error).__name__}")
        finally:
            self._trading = False
            if self.is_mounted:
                self.query_one("#buy-button", Button).disabled = False
                self.query_one("#sell-button", Button).disabled = False

    def _persist_trade_position(  # noqa: PLR0911
        self,
        *,
        side: str,
        mint: str,
        amount: int,
        receipt: object,
        as_of_slot: int,
    ) -> str | None:
        """Persist accepted direct trades into the watcher position store."""

        if not hasattr(receipt, "simulated_output_base_units"):
            return "receipt output is unavailable"
        store: SqliteStateStore | None = None
        try:
            store = SqliteStateStore(self._state_dir / "state.sqlite3")
            position = store.get(mint)
            if side == "buy":
                output = receipt.simulated_output_base_units
                if type(output) is not int or output <= 0:
                    return "buy output is unavailable"
                if position is None:
                    position = PaperPositionState(
                        as_of_slot=as_of_slot,
                        market_id=mint,
                        original_position_base_units=output,
                        current_position_base_units=output,
                        exit_rule_state=ExitRuleState(),
                    )
                else:
                    position = PaperPositionState(
                        as_of_slot=as_of_slot,
                        market_id=mint,
                        original_position_base_units=(
                            int(position.original_position_base_units) + output
                        ),
                        current_position_base_units=(
                            int(position.current_position_base_units) + output
                        ),
                        peak_pnl_ppm=position.peak_pnl_ppm,
                        exit_rule_state=position.exit_rule_state,
                        emitted_sell_intent_count=position.emitted_sell_intent_count,
                    )
                store.save(position)
                return None
            if position is None:
                return "no managed position for this mint"
            current = int(position.current_position_base_units)
            if amount > current:
                return f"sell amount exceeds managed position ({current} units)"
            remaining = current - amount
            if remaining <= 0:
                store.remove(mint)
            else:
                store.save(
                    PaperPositionState(
                        as_of_slot=as_of_slot,
                        market_id=mint,
                        original_position_base_units=position.original_position_base_units,
                        current_position_base_units=remaining,
                        peak_pnl_ppm=position.peak_pnl_ppm,
                        exit_rule_state=position.exit_rule_state,
                        emitted_sell_intent_count=position.emitted_sell_intent_count,
                    )
                )
            return None  # noqa: TRY300
        except Exception as error:  # noqa: BLE001
            return f"{type(error).__name__}"
        finally:
            if store is not None:
                store.close()

    async def _buy_as_of_slot(self) -> int:
        """Use an explicit slot in tests or read the latest finalized slot."""

        if self._as_of_slot is not None:
            return self._as_of_slot
        market = PumpOnlineMarket(self._endpoint)
        try:
            slot = await market.finalized_slot()
        finally:
            await market.close()
        if isinstance(slot, AbstainResult):
            raise SniperConfigError(slot.message)
        return slot

    def action_cycle_theme(self) -> None:
        """Switch to the next Textual theme without rebuilding the screen."""

        theme_names = tuple(sorted(self.available_themes))
        current_index = theme_names.index(self.theme)
        self.theme = theme_names[(current_index + 1) % len(theme_names)]

    def watch_theme(self, _theme: str) -> None:
        """Refresh theme-dependent Rich text after a theme change."""

        if not self.is_mounted:
            return
        if self._last_report is not None:
            self._render_metrics(self._last_report)

    def _theme_color(self, name: str) -> str:
        """Resolve a metric accent from the active Textual theme."""

        return getattr(self.current_theme, name)

    def _load_settings(self) -> None:
        """Load settings from the same YAML consumed by the watcher."""

        status = self.query_one("#settings-status", Static)
        try:
            config = load_sniper_config(self._config_path)
        except SniperConfigError as error:
            status.update(f"ABSTAIN  {error}")
            status.add_class("abstain")
            return
        settings = config.strategy
        values = {
            "signer-wallet": config.execution.signer_pubkey,
            "quote-size": config.execution.quote_size_lamports,
            "max-slippage": config.execution.max_slippage_bps,
            "trade-quote": format_sol(config.execution.quote_size_lamports),
            "trade-slippage": config.execution.max_slippage_bps,
            "min-volume": settings.min_volume_usd_micro,
            "max-pairs": settings.max_creator_pairs,
            "history-sample": settings.history_sample_count,
            "min-win-rate": settings.min_win_rate_ppm,
            "max-buys-hour": settings.max_buys_per_hour,
            "max-entry-index": settings.max_entry_transaction_index,
            "max-entry-mc": settings.max_entry_market_cap_quote_base_units,
            "entry-deviation": settings.max_entry_deviation_ppm,
            "snipe-delay": config.rules.snipe_delay_ms // 1000,
            "min-mc": config.rules.min_market_cap_quote_base_units,
            "max-mc": config.rules.max_market_cap_quote_base_units,
            "max-age": (
                0
                if config.rules.max_token_age_ms is None
                else config.rules.max_token_age_ms // 60_000
            ),
            "follow-cooldown": config.rules.copytrade_cooldown_ms // 1000,
            "max-losses": config.rules.max_consecutive_losses,
        }
        for input_id, value in values.items():
            self.query_one(f"#{input_id}", Input).value = (
                "" if value is None else str(value)
            )
        self.query_one(
            "#require-bundle", Checkbox
        ).value = settings.require_bundle_match
        self.query_one(
            "#require-double-signature", Checkbox
        ).value = settings.require_double_signature
        self.query_one(
            "#require-zero-balance", Checkbox
        ).value = settings.require_prior_zero_balance
        self.query_one(
            "#require-historical-qualification", Checkbox
        ).value = settings.require_historical_qualification
        self.query_one("#buy-once", Checkbox).value = config.rules.buy_only_once
        status.remove_class("abstain")
        status.update("loaded  watcher config")
        self._render_trade_state()

    def _render_trade_state(self) -> None:
        """Show whether the configured buy path can submit or only record."""

        status = self.query_one("#trade-status", Static)
        try:
            config = load_sniper_config(self._config_path)
        except SniperConfigError as error:
            status.update(f"ABSTAIN  {error}")
            return
        if config.execution.mode is ExecutionMode.LIVE and self._enable_live:
            if config.execution.signer_pubkey is None:
                status.update("LIVE BLOCKED  execution.signer_pubkey is missing")
            else:
                status.update(
                    f"LIVE  signer {short_address(config.execution.signer_pubkey)}"
                    "  |  submission enabled"
                )
        elif config.execution.mode is ExecutionMode.LIVE:
            status.update("LIVE LOCKED  start TUI with --enable-live")
            return
        else:
            status.update(
                f"{config.execution.mode.value.upper()}  buy/sell buttons record intent only"
            )
        if (
            config.execution.mode is ExecutionMode.LIVE
            and self._enable_live
            and not os.environ.get("SOLANA_PRIVATE_KEY")
        ):
            status.update("LIVE BLOCKED  SOLANA_PRIVATE_KEY is not set")

    def _render_execution_summary(self) -> None:
        """Render non-secret execution configuration and endpoint identity."""

        network = format_network_endpoint(self._endpoint)
        self.query_one("#network", Static).update(f"RPC {network}")
        try:
            config = load_sniper_config(self._config_path)
        except SniperConfigError:
            self.query_one("#execution-status", Static).update("SIGNER CONFIG ERROR")
            self.query_one("#execution-status", Static).remove_class("ok")
            self.query_one("#execution-status", Static).add_class("abstain")
            self.query_one("#execution-panel", Static).update(
                "Execution configuration is invalid. Open Settings to repair it."
            )
            return
        signer = config.execution.signer_pubkey
        signer_display = short_address(signer) if signer else "not configured"
        tracking_display = {
            "new_token_creations": "dev creates",
            "track_buys": "wallet buys (abstain)",
        }[config.tracking_mode.value]
        self.query_one("#execution-status", Static).remove_class("abstain")
        self.query_one("#execution-status", Static).remove_class("ok")
        self.query_one("#execution-status", Static).update(f"SIGNER {signer_display}")
        self.query_one("#tracking-status", Static).update(f"TRACK {tracking_display}")
        self.query_one("#execution-panel", Static).update(
            f"quote {format_sol(config.execution.quote_size_lamports)} SOL  |  "
            f"slippage {config.execution.max_slippage_bps} bps"
        )

    def _save_settings(self) -> None:
        """Validate and atomically persist settings to the canonical YAML."""

        status = self.query_one("#settings-status", Static)
        try:
            current_config = load_sniper_config(self._config_path)
            wallet = _wallet_setting(self.query_one("#wallet-input", Input).value)
            signer_pubkey = _optional_wallet_setting(
                self.query_one("#signer-wallet", Input).value
            )
            signer_pubkey = _require_live_signer(
                current_config.execution.mode.value,
                signer_pubkey,
            )
            settings = StrategyFilterSettings(
                min_volume_usd_micro=_optional_setting_int(
                    self.query_one("#min-volume", Input).value,
                    "min volume",
                ),
                max_creator_pairs=_optional_setting_int(
                    self.query_one("#max-pairs", Input).value,
                    "max creator pairs",
                ),
                history_sample_count=_setting_int(
                    self.query_one("#history-sample", Input).value,
                    "history sample count",
                ),
                min_win_rate_ppm=_setting_int(
                    self.query_one("#min-win-rate", Input).value,
                    "minimum win rate",
                ),
                max_buys_per_hour=_setting_int(
                    self.query_one("#max-buys-hour", Input).value,
                    "maximum buys per hour",
                ),
                max_entry_transaction_index=_setting_int(
                    self.query_one("#max-entry-index", Input).value,
                    "maximum entry index",
                ),
                max_entry_market_cap_quote_base_units=(
                    _optional_setting_int(
                        self.query_one("#max-entry-mc", Input).value,
                        "maximum entry market cap",
                    )
                ),
                max_entry_deviation_ppm=_setting_int(
                    self.query_one("#entry-deviation", Input).value,
                    "entry deviation",
                ),
                require_bundle_match=self.query_one("#require-bundle", Checkbox).value,
                require_double_signature=self.query_one(
                    "#require-double-signature", Checkbox
                ).value,
                require_prior_zero_balance=self.query_one(
                    "#require-zero-balance", Checkbox
                ).value,
                require_historical_qualification=self.query_one(
                    "#require-historical-qualification", Checkbox
                ).value,
            )
            document = load_sniper_document(self._config_path)
            document["target"] = {"kind": "wallet", "id": wallet}
            document["execution"] = {
                "mode": current_config.execution.mode.value,
                "quote_size_lamports": _setting_positive_int(
                    self.query_one("#quote-size", Input).value,
                    "paper quote size",
                ),
                "max_slippage_bps": _bounded_setting_int(
                    self.query_one("#max-slippage", Input).value,
                    "maximum slippage",
                    maximum=10_000,
                ),
                "signer_pubkey": signer_pubkey,
            }
            document["strategy"] = _strategy_to_yaml(settings)
            rules = document.setdefault("rules", {})
            if type(rules) is not dict:
                status.add_class("abstain")
                status.update("ABSTAIN  watcher config.rules must be one mapping")
                return
            rules.update(
                {
                    "snipe_delay_seconds": _setting_int(
                        self.query_one("#snipe-delay", Input).value,
                        "snipe delay",
                    ),
                    "min_market_cap_quote_base_units": _optional_setting_int(
                        self.query_one("#min-mc", Input).value,
                        "minimum market cap",
                    ),
                    "max_market_cap_quote_base_units": _optional_setting_int(
                        self.query_one("#max-mc", Input).value,
                        "maximum market cap",
                    ),
                    "max_token_age_minutes": _setting_int(
                        self.query_one("#max-age", Input).value,
                        "maximum token age",
                    ),
                    "follow_cooldown_seconds": _setting_int(
                        self.query_one("#follow-cooldown", Input).value,
                        "follow cooldown",
                    ),
                    "buy_only_once": self.query_one("#buy-once", Checkbox).value,
                    "max_consecutive_losses": _optional_setting_int(
                        self.query_one("#max-losses", Input).value,
                        "maximum consecutive losses",
                    ),
                }
            )
            save_sniper_document(self._config_path, document)
        except (OSError, TypeError, ValueError, SniperConfigError) as error:
            status.add_class("abstain")
            status.update(f"ABSTAIN  settings not saved: {error}")
            return
        status.remove_class("abstain")
        status.update("saved  restart rug_watch to apply the new config")
        self._render_execution_summary()

    async def _scan_and_render(self, wallet: str) -> None:
        """Scan one wallet and update the UI on the Textual event loop."""

        try:
            result = await scan_wallet_intelligence(
                wallet,
                endpoint=self._endpoint,
                max_transactions=self._max_transactions,
                max_linked_wallets=self._max_linked_wallets,
                as_of_slot=self._as_of_slot,
                transport=self._transport,
            )
            if not self.is_mounted:
                return
            if isinstance(result, WalletIntelligenceReport):
                self._render_report(result)
            else:
                self._render_abstention(result)
        except Exception as error:  # noqa: BLE001
            if not self.is_mounted:
                return
            self._render_abstention(
                AbstainResult(
                    reason=AbstainReason.UNKNOWN_PROTOCOL_STATE,
                    message=f"wallet scan failed: {type(error).__name__}",
                    as_of_slot=-1,
                )
            )
        finally:
            self._refreshing = False
            if self.is_mounted:
                self.query_one("#refresh-button", Button).disabled = False

    def _configure_tables(self) -> None:
        """Set stable columns for each data table."""

        self.query_one("#launches-table", DataTable).add_columns(
            "Scope", "Slot", "Pos", "Symbol", "Mint", "Creator", "Signature"
        )
        self.query_one("#nodes-table", DataTable).add_columns(
            "Scope", "Address", "Role", "Tx", "Creates", "First", "Last"
        )
        self.query_one("#edges-table", DataTable).add_columns(
            "Scope", "Source", "Target", "Transfers", "SOL", "First", "Last"
        )
        self.query_one("#switches-table", DataTable).add_columns(
            "Linked wallet", "Launches", "Early", "Transfer span", "Launch span"
        )
        self.query_one("#positions-table", DataTable).add_columns(
            "Mint", "Peak PnL", "Units", "Slot", "Sell"
        )

    def _render_positions(self) -> None:
        """Render durable positions without loading signing material."""

        table = self.query_one("#positions-table", DataTable)
        table.clear(columns=False)
        state_path = self._state_dir / "state.sqlite3"
        if not state_path.exists():
            self.query_one("#positions-summary", Static).update("OPEN POSITIONS  0")
            return
        try:
            store = SqliteStateStore(state_path)
            try:
                positions = store.read_all()
            finally:
                store.close()
        except Exception as error:  # noqa: BLE001
            self.query_one("#positions-summary", Static).update(
                f"POSITIONS  unavailable: {type(error).__name__}"
            )
            return
        for position in positions:
            table.add_row(
                position.market_id,
                f"{position.peak_pnl_ppm // 10_000}%",
                str(position.current_position_base_units),
                str(position.as_of_slot),
                "select to sell",
                key=position.market_id,
            )
        self.query_one("#positions-summary", Static).update(
            f"OPEN POSITIONS  {len(positions)}"
        )

    def _render_report(self, report: WalletIntelligenceReport) -> None:
        previous = self._last_report
        if previous is None or previous.target_wallet != report.target_wallet:
            previous = None
        new_launches, new_links = report_delta(previous, report)
        self._last_report = report

        status = self.query_one("#status", Static)
        status.remove_class("abstain")
        status.add_class("ok")
        status.update(
            f"OK  slot {report.as_of_slot}  {short_address(report.target_wallet)}  "
            f"+{new_launches} creates  +{new_links} links"
        )
        self.query_one("#target-summary", Static).update(format_target_summary(report))
        self.query_one("#last-update", Static).update(
            f"Updated {datetime.now().strftime('%H:%M:%S')}"
        )
        self._render_metrics(report)
        self.query_one("#flow-panel", Static).update(format_flow(report))
        self.query_one("#assessment-panel", Static).update(format_assessment(report))
        self.query_one("#signals-panel", Static).update(format_signals(report))
        self.query_one("#warnings-panel", Static).update(format_warnings(report))
        self.query_one("#launch-summary", Static).update(
            f"TARGET {report.launch_count}  |  LINKED {report.linked_launch_count}  |  "
            f"EARLY {report.early_launch_count + report.linked_early_launch_count}  |  "
            f"TX {report.scanned_transaction_count}/{report.history_limit}  |  "
            f"SLOT {report.as_of_slot}"
        )
        self.query_one("#graph-summary", Static).update(
            f"DIRECT {report.direct_linked_wallet_count}  |  "
            f"EXPANDED {max(0, len(report.nodes) - report.direct_linked_wallet_count - 1)}  |  "
            f"EDGES {len(report.edges)}  |  "
            f"SLOT {report.as_of_slot}"
        )
        self._render_launches(report)
        self._render_graph(report)
        self._render_positions()
        self._render_execution_summary()

    def _render_metrics(self, report: WalletIntelligenceReport) -> None:
        self.query_one("#activity-metric", Static).update(
            format_metric(
                "TX",
                f"{report.successful_transaction_count}/{report.scanned_transaction_count}",
                "successful / scanned",
                self._theme_color("success"),
            )
        )
        self.query_one("#launch-metric", Static).update(
            format_metric(
                "CREATES",
                str(report.launch_count),
                "target wallet",
                self._theme_color("warning"),
            )
        )
        self.query_one("#early-metric", Static).update(
            format_metric(
                "EARLY",
                str(report.early_launch_count + report.linked_early_launch_count),
                "target + linked",
                self._theme_color("warning"),
            )
        )
        self.query_one("#link-metric", Static).update(
            format_metric(
                "LINKED",
                str(report.direct_linked_wallet_count),
                "direct wallets",
                self._theme_color("secondary"),
            )
        )
        switch = "YES" if report.wallet_switch_candidate else "NO"
        self.query_one("#switch-metric", Static).update(
            format_metric("SWITCH", switch, "wallet reuse", self._theme_color("error"))
        )

    def _render_abstention(self, result: AbstainResult) -> None:
        """Show a typed failure without hiding it behind an exception."""

        self._last_report = None
        status = self.query_one("#status", Static)
        status.remove_class("ok")
        status.add_class("abstain")
        status.update(f"ABSTAIN  {result.reason.value}  |  {result.message}")
        self.query_one("#target-summary", Static).update(
            "TARGET  --  |  NO FINALIZED REPORT"
        )
        self.query_one("#last-update", Static).update("No usable report")
        metric_titles = {
            "activity-metric": "TX",
            "launch-metric": "CREATES",
            "link-metric": "LINKED",
            "switch-metric": "SWITCH",
            "early-metric": "EARLY",
        }
        for metric_id, title in metric_titles.items():
            self.query_one(f"#{metric_id}", Static).update(
                format_metric(title, "--", "no report", self._theme_color("error"))
            )
        self.query_one("#signals-panel", Static).update(f"ABSTAIN\n{result.message}")
        self.query_one("#flow-panel", Static).update("--")
        self.query_one("#assessment-panel", Static).update("--")
        self.query_one("#execution-panel", Static).update("--")
        self.query_one("#warnings-panel", Static).update(
            "Scan failed closed; fix the input or RPC state and refresh."
        )
        self.query_one("#launch-summary", Static).update("NO REPORT")
        self.query_one("#graph-summary", Static).update("NO REPORT")
        self.query_one("#graph-map", Static).update("NO REPORT")
        self._clear_tables()
        self.query_one("#launch-count", Static).update("0 shown")

    def _render_launches(self, report: WalletIntelligenceReport) -> None:
        table = self.query_one("#launches-table", DataTable)
        table.clear(columns=False)
        query = self.query_one("#launch-filter", Input).value.strip().lower()
        early_only = self.query_one("#early-only", Checkbox).value
        launches = tuple((launch, "target") for launch in report.launches) + tuple(
            (launch, "linked") for launch in report.linked_launches
        )
        shown = 0
        for launch, scope in launches:
            if (early_only and not launch.position_is_zero_or_one) or (
                query and not launch_matches(launch, query)
            ):
                continue
            shown += 1
            table.add_row(
                scope,
                str(launch.slot),
                str(launch.transaction_index),
                launch.symbol or launch.name or "-",
                short_address(launch.mint),
                short_address(launch.creator),
                short_address(launch.signature),
                key=launch.mint,
            )
        self.query_one("#launch-count", Static).update(f"{shown}/{len(launches)} shown")

    def _render_graph(self, report: WalletIntelligenceReport) -> None:
        self.query_one("#graph-map", Static).update(format_graph_map(report))
        nodes = self.query_one("#nodes-table", DataTable)
        nodes.clear(columns=False)
        for node in report.nodes:
            scope = (
                "target"
                if node.address == report.target_wallet
                else "direct"
                if "direct_counterparty" in node.roles
                else "expanded"
            )
            nodes.add_row(
                scope,
                short_address(node.address),
                ", ".join(node.roles),
                str(node.scanned_transaction_count),
                str(node.launch_count),
                str(node.first_seen_slot or "-"),
                str(node.last_seen_slot or "-"),
            )
        edges = self.query_one("#edges-table", DataTable)
        edges.clear(columns=False)
        for edge in report.edges:
            scope = (
                "direct"
                if report.target_wallet in {edge.source, edge.target}
                else "expanded"
            )
            edges.add_row(
                scope,
                short_address(edge.source),
                short_address(edge.target),
                str(edge.transfer_count),
                format_sol(edge.amount_lamports),
                str(edge.first_slot),
                str(edge.last_slot),
            )
        switches = self.query_one("#switches-table", DataTable)
        switches.clear(columns=False)
        for switch in report.wallet_switches:
            switches.add_row(
                short_address(switch.linked_wallet),
                str(switch.launch_count),
                str(switch.early_launch_count),
                f"{switch.first_transfer_slot} -> {switch.last_transfer_slot}",
                f"{switch.first_launch_slot} -> {switch.last_launch_slot}",
            )

    def _clear_tables(self) -> None:
        self.query_one("#launches-table", DataTable).clear(columns=False)
        self.query_one("#nodes-table", DataTable).clear(columns=False)
        self.query_one("#edges-table", DataTable).clear(columns=False)
        self.query_one("#switches-table", DataTable).clear(columns=False)


def report_delta(
    previous: WalletIntelligenceReport | None,
    current: WalletIntelligenceReport,
) -> tuple[int, int]:
    """Return newly observed launches and links for the same wallet."""

    if previous is None:
        return 0, 0
    previous_launches = {launch.signature for launch in previous.launches}
    current_launches = {launch.signature for launch in current.launches}
    previous_links = {(edge.source, edge.target) for edge in previous.edges}
    current_links = {(edge.source, edge.target) for edge in current.edges}
    return (
        len(current_launches - previous_launches),
        len(current_links - previous_links),
    )


def launch_matches(launch: WalletLaunch, query: str) -> bool:
    """Match a launch against the local table filter."""

    return (
        query
        in " ".join(
            (launch.name, launch.symbol, launch.mint, launch.creator, launch.signature)
        ).lower()
    )


def format_metric(title: str, value: str, detail: str, accent: str) -> Text:
    """Build a metric card with distinct title, value, and supporting detail."""

    metric = Text()
    metric.append(f"{title}\n", style=f"bold {accent}")
    metric.append(f"{value}\n", style=f"bold {accent}")
    metric.append(detail, style=f"dim {accent}")
    return metric


def format_signals(report: WalletIntelligenceReport) -> str:
    """Format the highest-signal creator and market facts for the overview."""

    evidence = rug_evidence_summary(report)
    signals: list[str] = []
    signals.append(
        f"CREATOR HISTORY  {evidence['indexed_created_count'] or 0} created  |  "
        f"{report.creator_history.open_count if report.creator_history else 0} open"
    )
    if report.creator_history is not None:
        history = report.creator_history
        ath = history.ath_symbol or history.ath_token or "-"
        signals.append(f"MARKET  {ath}  |  ATH MC ${history.ath_market_cap or '-'}")
        if history.tokens:
            token = history.tokens[0]
            signals.append(
                f"CURRENT  {token.symbol or '-'}  |  MC ${token.market_cap or '-'}  |  "
                f"LIQ ${token.pool_liquidity or '-'}  |  HOLDERS {token.holders}  |  "
                f"BUNDLER {token.bundler_rate}"
            )
        else:
            signals.append("MARKET  creator-wide enrichment unavailable")
    signals.append(
        f"LINKS  {report.direct_linked_wallet_count} direct  |  "
        f"{report.linked_creator_wallet_count} linked creators  |  "
        f"SWITCH {'YES' if report.wallet_switch_candidate else 'NO'}"
    )
    return "\n".join(signals)


def format_assessment(report: WalletIntelligenceReport) -> str:
    """Format a readable qualification decision without overstating evidence."""

    total_launches = report.launch_count + report.linked_launch_count
    if total_launches < MIN_REPEAT_LAUNCH_EVIDENCE:
        lines = [
            f"NOT QUALIFIED  {total_launches} launch found in bounded history",
            "Need repeat launch evidence before treating this wallet as a known operator.",
        ]
    else:
        lines = ["REVIEW  repeat launch activity observed"]
    if report.wallet_switch_candidate:
        lines.append(
            f"SWITCH CANDIDATE  {len(report.wallet_switches)} wallet(s) with transfer/launch overlap"
        )
    return "\n".join(lines)


def format_target_summary(report: WalletIntelligenceReport) -> str:
    """Show the current launch identity and its entry position."""

    if not report.launches:
        return (
            f"NO PUMP CREATES  |  FINALIZED SLOT {report.as_of_slot}  |  "
            f"WINDOW {report.history_limit} TRANSACTIONS"
        )
    launch = max(report.launches, key=lambda item: (item.slot, item.transaction_index))
    label = launch.symbol or launch.name or "UNKNOWN"
    position = "EARLY" if launch.position_is_zero_or_one else "NOT EARLY"
    return (
        f"{label}  {short_address(launch.mint)}  |  creator {short_address(launch.creator)}  |  "
        f"slot {launch.slot}  tx {launch.transaction_index}  {position}"
    )


def format_flow(report: WalletIntelligenceReport) -> str:
    """Format observed native funding and distribution."""

    return " | ".join(
        (
            f"IN  {format_sol(report.native_in_lamports)} SOL",
            f"OUT  {format_sol(report.native_out_lamports)} SOL",
            f"NET  {format_sol(report.native_in_lamports - report.native_out_lamports)} SOL",
        )
    )


def format_graph_map(report: WalletIntelligenceReport) -> str:
    """Render a bounded ASCII relationship graph from observed edges."""

    lines = [
        "RELATIONSHIP GRAPH",
        f"TARGET  {short_address(report.target_wallet)}",
    ]
    direct_edges = tuple(
        edge
        for edge in report.edges
        if report.target_wallet in {edge.source, edge.target}
    )
    if not direct_edges:
        lines.append("  +-- (no direct native-transfer edges observed)")
    for index, edge in enumerate(direct_edges[:GRAPH_PREVIEW_LIMIT]):
        branch = (
            "  `--"
            if index == min(len(direct_edges), GRAPH_PREVIEW_LIMIT) - 1
            else "  |--"
        )
        direction = "-->" if edge.source == report.target_wallet else "<--"
        peer = edge.target if direction == "-->" else edge.source
        lines.append(f"{branch} {direction} DIRECT  {short_address(peer)}")
        lines.append(
            f"      {edge.transfer_count} tx | {format_sol(edge.amount_lamports)} SOL | "
            f"slots {edge.first_slot}->{edge.last_slot}"
        )
    if len(direct_edges) > GRAPH_PREVIEW_LIMIT:
        lines.append(
            f"  ... {len(direct_edges) - GRAPH_PREVIEW_LIMIT} more direct edges in table"
        )
    expanded_edges = tuple(
        edge
        for edge in report.edges
        if report.target_wallet not in {edge.source, edge.target}
    )
    if expanded_edges:
        lines.append("EXPANDED")
        for edge in expanded_edges[:GRAPH_PREVIEW_LIMIT]:
            lines.append(
                f"  {short_address(edge.source)} -- {edge.transfer_count} tx --> "
                f"{short_address(edge.target)}"
            )
        if len(expanded_edges) > GRAPH_PREVIEW_LIMIT:
            lines.append(
                f"  ... {len(expanded_edges) - GRAPH_PREVIEW_LIMIT} more expanded edges"
            )
    return "\n".join(lines)


def format_warnings(report: WalletIntelligenceReport) -> str:
    """Format data-quality limits so they remain visible in the dashboard."""

    if not report.warnings:
        return "No warnings reported by the finalized scan."
    return "\n".join(f"WARN  {warning}" for warning in report.warnings)


def format_overview(report: WalletIntelligenceReport) -> str:
    """Format a compact text overview for callers outside the TUI."""

    return "\n".join(
        (
            f"Wallet: {report.target_wallet}",
            f"Finalized slot: {report.as_of_slot}",
            f"Transactions scanned: {report.scanned_transaction_count}",
            f"Successful transactions: {report.successful_transaction_count}",
            f"Observed span: {report.first_seen_slot or '-'} -> {report.last_seen_slot or '-'}",
            f"Pump launches: {report.launch_count}",
            f"GMGN indexed creations: "
            f"{report.creator_history.total_created_count if report.creator_history else '-'}",
            f"Direct linked wallets: {report.direct_linked_wallet_count}",
            f"Linked creator wallets: {report.linked_creator_wallet_count}",
            f"Wallet switch candidate: {'YES' if report.wallet_switch_candidate else 'NO'}",
            f"Native in: {format_sol(report.native_in_lamports)} SOL",
            f"Native out: {format_sol(report.native_out_lamports)} SOL",
            "Rug assessment (finalized RPC): "
            f"{rug_evidence_summary(report)['assessment']}",
            "",
            format_signals(report),
            "",
            format_warnings(report),
        )
    )


def short_address(value: str) -> str:
    """Keep long Solana identifiers readable in fixed-width tables."""

    if len(value) <= SHORT_IDENTIFIER_LIMIT:
        return value
    return f"{value[:6]}...{value[-6:]}"


def format_network_endpoint(endpoint: str) -> str:
    """Show the RPC host without exposing query-string credentials."""

    host = endpoint.split("?", 1)[0].rstrip("/")
    return host.removeprefix("https://").removeprefix("http://")


def _setting_int(value: str, field_name: str) -> int:
    if not value or not value.isdecimal():
        raise SniperConfigError(  # noqa: TRY003
            f"{field_name} must be a non-negative integer"
        )
    return int(value)


def _wallet_setting(value: str) -> str:
    wallet = value.strip()
    if not wallet:
        raise SniperConfigError("wallet address is required")  # noqa: TRY003
    return wallet


def _optional_wallet_setting(value: str) -> str | None:
    wallet = value.strip()
    return wallet or None


def _require_live_signer(mode: str, signer_pubkey: str | None) -> str | None:
    if mode == ExecutionMode.LIVE.value and signer_pubkey is None:
        raise SniperConfigError(  # noqa: TRY003
            "signing wallet public address is required for live mode"
        )
    return signer_pubkey


def _setting_positive_int(value: str, field_name: str) -> int:
    parsed = _setting_int(value, field_name)
    if parsed <= 0:
        raise SniperConfigError(f"{field_name} must be positive")  # noqa: TRY003
    return parsed


def _sol_setting_lamports(value: str, field_name: str) -> int:
    """Parse a positive SOL amount into lamports without floating point."""

    text = value.strip()
    whole, separator, fraction = text.partition(".")
    if separator and len(fraction) > SOL_DECIMAL_PLACES:
        raise SniperConfigError(  # noqa: TRY003
            f"{field_name} supports at most {SOL_DECIMAL_PLACES} decimal places"
        )
    if not whole:
        whole = "0"
    if not whole.isdecimal() or (separator and not fraction.isdecimal()):
        raise SniperConfigError(  # noqa: TRY003
            f"{field_name} must be a positive SOL amount, for example 0.001"
        )
    lamports = int(whole) * LAMPORTS_PER_SOL
    if fraction:
        lamports += int(fraction.ljust(SOL_DECIMAL_PLACES, "0"))
    if lamports <= 0:
        raise SniperConfigError(f"{field_name} must be positive")  # noqa: TRY003
    return lamports


def _bounded_setting_int(value: str, field_name: str, *, maximum: int) -> int:
    parsed = _setting_int(value, field_name)
    if parsed > maximum:
        raise SniperConfigError(  # noqa: TRY003
            f"{field_name} must be between 0 and {maximum}"
        )
    return parsed


def _optional_setting_int(value: str, field_name: str) -> int | None:
    if not value.strip():
        return None
    return _setting_int(value, field_name)


def _strategy_to_yaml(settings: StrategyFilterSettings) -> dict[str, object]:
    return {
        "min_volume_usd_micro": settings.min_volume_usd_micro,
        "max_creator_pairs": settings.max_creator_pairs,
        "history_sample_count": settings.history_sample_count,
        "min_win_rate_ppm": settings.min_win_rate_ppm,
        "max_buys_per_hour": settings.max_buys_per_hour,
        "max_entry_transaction_index": settings.max_entry_transaction_index,
        "max_entry_market_cap_quote_base_units": (
            settings.max_entry_market_cap_quote_base_units
        ),
        "max_entry_deviation_ppm": settings.max_entry_deviation_ppm,
        "require_bundle_match": settings.require_bundle_match,
        "require_double_signature": settings.require_double_signature,
        "require_prior_zero_balance": settings.require_prior_zero_balance,
        "require_historical_qualification": settings.require_historical_qualification,
    }


def format_sol(lamports: int) -> str:
    """Format lamports with integer arithmetic only."""

    whole, fraction = divmod(lamports, LAMPORTS_PER_SOL)
    return f"{whole}.{fraction:0{SOL_DECIMAL_PLACES}d}".rstrip("0").rstrip(".")


def main(argv: Sequence[str] | None = None) -> int:  # noqa: C901
    """Start the wallet intelligence TUI."""

    parser = argparse.ArgumentParser(description="Interactive wallet intelligence TUI.")
    parser.add_argument("--wallet")
    parser.add_argument("--config", type=Path, default=Path("watch.yaml"))
    parser.add_argument("--state-dir", type=Path, default=Path(".state/watch"))
    parser.add_argument("--max-transactions", type=int, default=100)
    parser.add_argument("--max-linked-wallets", type=int, default=8)
    parser.add_argument("--refresh-seconds", type=int, default=30)
    parser.add_argument("--as-of-slot", type=int)
    parser.add_argument(
        "--theme",
        default="textual-dark",
        help="Textual theme name; press t to cycle themes while running",
    )
    parser.add_argument(
        "--enable-live",
        action="store_true",
        help="allow live submission when execution.mode is live",
    )
    args = parser.parse_args(argv)
    try:
        config = load_sniper_config(args.config)
    except SniperConfigError as error:
        parser.error(str(error))
    if config.target.kind.value != "wallet":
        parser.error("watcher config target must be a wallet for the TUI")
    if args.enable_live:
        if config.execution.mode is not ExecutionMode.LIVE:
            parser.error("--enable-live requires execution.mode: live in watch.yaml")
        load_dotenv()
    endpoint = os.environ.get("SOLANA_RPC_HTTP") or os.environ.get(
        "SOLANA_NODE_RPC_ENDPOINT"
    )
    if not endpoint:
        parser.error("SOLANA_RPC_HTTP or SOLANA_NODE_RPC_ENDPOINT is required")
    wallet = args.wallet
    if wallet is None:
        wallet = config.target.id
    if args.refresh_seconds <= 0:
        parser.error("--refresh-seconds must be positive")
    if not 1 <= args.max_transactions <= MAX_TUI_HISTORY:
        parser.error("--max-transactions must be between 1 and 100")
    if args.as_of_slot is not None and args.as_of_slot < 0:
        parser.error("--as-of-slot must be non-negative")
    try:
        app = WalletIntelApp(
            wallet,
            endpoint=endpoint,
            max_transactions=args.max_transactions,
            max_linked_wallets=args.max_linked_wallets,
            refresh_seconds=args.refresh_seconds,
            as_of_slot=args.as_of_slot,
            config_path=args.config,
            state_dir=args.state_dir,
            theme=args.theme,
            enable_live=args.enable_live,
        )
    except ValueError as error:
        parser.error(str(error))
    app.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
