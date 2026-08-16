"""Interactive Textual UI for finalized wallet intelligence reports."""

from __future__ import annotations

import argparse
import os
import tempfile
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar

import yaml
from rich.text import Text
from textual.app import App, ComposeResult
from textual.containers import Horizontal, VerticalScroll
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

from rugbot.domain.decisions import AbstainReason, AbstainResult
from rugbot.runtime.config import (
    SniperConfigError,
    StrategyFilterSettings,
    load_sniper_config,
    parse_sniper_config,
)
from rugbot.runtime.wallet_intelligence import (
    WalletIntelligenceReport,
    WalletLaunch,
    rug_evidence_summary,
    scan_wallet_intelligence,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

    from rugbot.ingest.rpc_observer import RpcHttpTransport


SHORT_IDENTIFIER_LIMIT = 14
GRAPH_PREVIEW_LIMIT = 12
MAX_TUI_HISTORY = 100
COLOR_TEXT = "#d8dadd"
COLOR_MUTED = "#777b84"
COLOR_TEAL = "#a8c98b"
COLOR_BLUE = "#9aa0aa"
COLOR_AMBER = "#d3a96b"
COLOR_CORAL = "#e87979"


class WalletIntelApp(App[None]):
    """Display one wallet intelligence report and refresh it on demand."""

    TITLE = "rugbot / wallet-intel"
    SUB_TITLE = "finalized / read-only"
    CSS = """
    Screen {
        background: #0e0f11;
        color: #d8dadd;
    }
    Header {
        background: #0e0f11;
        color: #d8dadd;
        height: 3;
    }
    Footer {
        background: #151619;
        color: #777b84;
    }
    #toolbar {
        height: 3;
        padding: 0 2;
        background: #151619;
        border-bottom: solid #292b30;
    }
    #toolbar-label {
        width: 10;
        padding: 1 1 0 0;
        color: #d3a96b;
        text-style: bold;
    }
    #wallet-input {
        width: 1fr;
        border: none;
        background: #0e0f11;
        color: #d8dadd;
    }
    #wallet-input:focus {
        border: tall #d3a96b;
        background: #191a1e;
    }
    #refresh-button {
        width: 9;
        margin-left: 1;
        background: #d3a96b;
        color: #17120a;
        text-style: bold;
    }
    #refresh-button:hover {
        background: #e2be85;
    }
    #refresh-button:focus {
        border: tall #d8dadd;
    }
    #status-row {
        height: 2;
        padding: 0 2;
        background: #0e0f11;
        border-bottom: solid #292b30;
    }
    #status {
        width: 1fr;
        color: #777b84;
    }
    #last-update {
        width: auto;
        padding: 0 1;
        color: #777b84;
    }
    #status.ok {
        color: #a8c98b;
    }
    #status.abstain {
        color: #e87979;
    }
    TabbedContent {
        height: 1fr;
    }
    TabbedContent > ContentTabs {
        background: #0e0f11;
        border-bottom: solid #292b30;
    }
    TabbedContent Tab {
        color: #777b84;
    }
    TabbedContent Tab:hover {
        color: #d8dadd;
    }
    TabbedContent Tab.-active {
        background: #191a1e;
        color: #d3a96b;
        text-style: bold;
    }
    TabPane {
        padding: 0 2;
    }
    #overview-scroll,
    #graph-scroll {
        height: 1fr;
    }
    #metric-row {
        height: 5;
        padding: 1 0;
        border-bottom: solid #292b30;
    }
    .metric {
        width: 1fr;
        height: 3;
        margin-right: 2;
        padding: 0;
        background: #0e0f11;
    }
    .section-title {
        height: 1;
        padding: 0;
        color: #777b84;
        text-style: bold;
    }
    .section-panel {
        height: auto;
        padding: 0 0 1 0;
        border: none;
        background: #0e0f11;
    }
    #signals-panel {
        color: #c0a77b;
    }
    #flow-panel,
    #graph-map {
        color: #9aa0aa;
    }
    #warnings-panel {
        color: #c0a77b;
    }
    #launch-summary,
    #graph-summary {
        height: 2;
        color: #777b84;
    }
    #launch-tools {
        height: 3;
        margin-bottom: 1;
    }
    #launch-filter {
        width: 1fr;
    }
    #launch-count {
        width: 22;
        padding: 1 0 0 1;
        color: #777b84;
        text-align: right;
    }
    DataTable {
        height: 1fr;
        border: none;
        background: #0e0f11;
        color: #c4c7cc;
    }
    DataTable > .datatable--header {
        background: #191a1e;
        color: #d8dadd;
    }
    DataTable > .datatable--even-row {
        background: #111215;
    }
    DataTable > .datatable--cursor {
        background: #2b2922;
        color: #f1e7d4;
        text-style: bold;
    }
    #nodes-table,
    #edges-table {
        height: 12;
    }
    #settings-scroll {
        height: 1fr;
        padding: 1 0;
    }
    .settings-row {
        height: 3;
        margin-bottom: 1;
    }
    .settings-label {
        width: 34;
        padding: 1 1 0 0;
        color: #777b84;
    }
    .settings-input {
        width: 28;
    }
    #settings-save {
        width: 12;
        margin-top: 1;
        background: #d3a96b;
        color: #17120a;
        text-style: bold;
    }
    #settings-status {
        height: 2;
        margin-top: 1;
        color: #777b84;
    }
    """
    BINDINGS: ClassVar[list[tuple[str, str, str]]] = [
        ("r", "refresh", "Refresh"),
        ("f", "focus_wallet", "Wallet"),
        ("1", "show_overview", "Overview"),
        ("2", "show_launches", "Launches"),
        ("3", "show_graph", "Graph"),
        ("4", "show_settings", "Settings"),
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
    ) -> None:
        """Initialize the wallet screen without loading signing keys."""

        super().__init__()
        self._wallet = wallet
        self._endpoint = endpoint
        self._max_transactions = max_transactions
        self._max_linked_wallets = max_linked_wallets
        self._refresh_seconds = refresh_seconds
        self._as_of_slot = as_of_slot
        self._transport = transport
        self._config_path = config_path
        self._refreshing = False
        self._last_report: WalletIntelligenceReport | None = None
        self._initial_focus_pending = True

    def compose(self) -> ComposeResult:
        """Build the TUI layout."""

        yield Header()
        with Horizontal(id="toolbar"):
            yield Label("wallet", id="toolbar-label")
            yield Input(
                value=self._wallet,
                placeholder="wallet address",
                id="wallet-input",
            )
            yield Button("Refresh", id="refresh-button", variant="primary")
        with Horizontal(id="status-row"):
            yield Static("scan pending", id="status")
            yield Static("--", id="last-update")
        with TabbedContent(initial="overview-tab"):
            with TabPane("Overview", id="overview-tab"):
                with VerticalScroll(id="overview-scroll"):
                    with Horizontal(id="metric-row"):
                        yield Static(
                            format_metric("ACTIVITY", "--", "transactions", COLOR_TEAL),
                            id="activity-metric",
                            classes="metric",
                        )
                        yield Static(
                            format_metric(
                                "LAUNCHES", "--", "in newest tx window", COLOR_AMBER
                            ),
                            id="launch-metric",
                            classes="metric",
                        )
                        yield Static(
                            format_metric(
                                "LINKS", "--", "direct to wallet", COLOR_BLUE
                            ),
                            id="link-metric",
                            classes="metric",
                        )
                        yield Static(
                            format_metric("SWITCH", "--", "evidence", COLOR_CORAL),
                            id="switch-metric",
                            classes="metric",
                        )
                    yield Label("flow", classes="section-title")
                    yield Static(
                        "--",
                        id="flow-panel",
                        classes="section-panel",
                    )
                    yield Label("signals", classes="section-title")
                    yield Static(
                        "--",
                        id="signals-panel",
                        classes="section-panel",
                    )
                    yield Label("quality", classes="section-title")
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
                        placeholder="filter launches",
                        id="launch-filter",
                    )
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

        fields = (
            ("min volume (micro USD)", "min-volume", "30000000000"),
            ("max creator pairs", "max-pairs", "10"),
            ("history sample count", "history-sample", "10"),
            ("minimum win rate (ppm)", "min-win-rate", "500000"),
            ("maximum buys / hour", "max-buys-hour", "1"),
            ("maximum entry index", "max-entry-index", "1"),
            ("maximum entry market cap", "max-entry-mc", "0"),
            ("entry deviation (ppm)", "entry-deviation", "250000"),
        )
        rows: list[object] = []
        for label, input_id, placeholder in fields:
            rows.append(
                Horizontal(
                    Label(label, classes="settings-label"),
                    Input(
                        placeholder=placeholder,
                        id=input_id,
                        classes="settings-input",
                    ),
                    classes="settings-row",
                )
            )
        rows.extend(
            (
                Checkbox("require bundle match", id="require-bundle"),
                Checkbox("require double signature", id="require-double-signature"),
                Checkbox("require prior zero balance", id="require-zero-balance"),
            )
        )
        return rows

    def on_mount(self) -> None:
        """Initialize tables and start the first asynchronous scan."""

        self._configure_tables()
        self._load_settings()
        self.set_interval(self._refresh_seconds, self.action_refresh)
        self.action_refresh()
        self.set_timer(0.05, self._focus_refresh_button)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        """Refresh after the toolbar button is pressed."""

        if event.button.id == "refresh-button":
            self.action_refresh()
        elif event.button.id == "settings-save":
            self._save_settings()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        """Refresh after entering a new wallet."""

        if event.input.id == "wallet-input":
            self.action_refresh()

    def on_input_changed(self, event: Input.Changed) -> None:
        """Filter the launch table without triggering another RPC scan."""

        if event.input.id == "launch-filter" and self._last_report is not None:
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
            "min-volume": settings.min_volume_usd_micro,
            "max-pairs": settings.max_creator_pairs,
            "history-sample": settings.history_sample_count,
            "min-win-rate": settings.min_win_rate_ppm,
            "max-buys-hour": settings.max_buys_per_hour,
            "max-entry-index": settings.max_entry_transaction_index,
            "max-entry-mc": settings.max_entry_market_cap_quote_base_units,
            "entry-deviation": settings.max_entry_deviation_ppm,
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
        status.remove_class("abstain")
        status.update("loaded  watcher config")

    def _save_settings(self) -> None:
        """Validate and atomically persist settings to the canonical YAML."""

        status = self.query_one("#settings-status", Static)
        try:
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
                max_entry_market_cap_quote_base_units=_optional_setting_int(
                    self.query_one("#max-entry-mc", Input).value,
                    "maximum entry market cap",
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
            )
            document = yaml.safe_load(self._config_path.read_text(encoding="utf-8"))
            if type(document) is not dict:
                status.add_class("abstain")
                status.update("ABSTAIN  watcher config must be one mapping")
                return
            document["strategy"] = _strategy_to_yaml(settings)
            candidate = yaml.safe_dump(document, sort_keys=False)
            parse_sniper_config(candidate)
            _atomic_write(self._config_path, candidate)
        except (OSError, TypeError, ValueError, SniperConfigError) as error:
            status.add_class("abstain")
            status.update(f"ABSTAIN  settings not saved: {error}")
            return
        status.remove_class("abstain")
        status.update("saved  watcher will use these settings on its next cycle")

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
            "Slot", "Pos", "Symbol", "Mint", "Creator", "Signature"
        )
        self.query_one("#nodes-table", DataTable).add_columns(
            "Scope", "Address", "Role", "Tx", "Creates", "First", "Last"
        )
        self.query_one("#edges-table", DataTable).add_columns(
            "Scope", "Source", "Target", "Transfers", "SOL", "First", "Last"
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
        self.query_one("#last-update", Static).update(
            f"Updated {datetime.now().strftime('%H:%M:%S')}"
        )
        self._render_metrics(report)
        self.query_one("#flow-panel", Static).update(format_flow(report))
        self.query_one("#signals-panel", Static).update(format_signals(report))
        self.query_one("#warnings-panel", Static).update(format_warnings(report))
        self.query_one("#launch-summary", Static).update(
            f"CREATES {report.launch_count} IN NEWEST {report.history_limit} TX  |  "
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

    def _render_metrics(self, report: WalletIntelligenceReport) -> None:
        self.query_one("#activity-metric", Static).update(
            format_metric(
                "ACTIVITY",
                str(report.scanned_transaction_count),
                f"{report.successful_transaction_count} successful",
                COLOR_TEAL,
            )
        )
        self.query_one("#launch-metric", Static).update(
            format_metric(
                "LAUNCHES",
                str(report.launch_count),
                f"in newest {report.history_limit} tx",
                COLOR_AMBER,
            )
        )
        self.query_one("#link-metric", Static).update(
            format_metric(
                "LINKS",
                str(report.direct_linked_wallet_count),
                f"{report.linked_creator_wallet_count} creator-linked",
                COLOR_BLUE,
            )
        )
        switch = "YES" if report.wallet_switch_candidate else "NO"
        self.query_one("#switch-metric", Static).update(
            format_metric("SWITCH", switch, "evidence", COLOR_CORAL)
        )

    def _render_abstention(self, result: AbstainResult) -> None:
        """Show a typed failure without hiding it behind an exception."""

        self._last_report = None
        status = self.query_one("#status", Static)
        status.remove_class("ok")
        status.add_class("abstain")
        status.update(f"ABSTAIN  {result.reason.value}  |  {result.message}")
        self.query_one("#last-update", Static).update("No usable report")
        for metric_id in (
            "activity-metric",
            "launch-metric",
            "link-metric",
            "switch-metric",
        ):
            title = metric_id.removesuffix("-metric").upper()
            self.query_one(f"#{metric_id}", Static).update(
                format_metric(title, "--", "abstained", COLOR_CORAL)
            )
        self.query_one("#signals-panel", Static).update(f"ABSTAIN\n{result.message}")
        self.query_one("#flow-panel", Static).update("No flow rendered.")
        self.query_one("#warnings-panel", Static).update(
            "No report was rendered. The scan failed closed; retry after fixing the input or RPC state."
        )
        self.query_one("#launch-summary", Static).update("No launch report available.")
        self.query_one("#graph-summary", Static).update("No graph report available.")
        self.query_one("#graph-map", Static).update("No graph rendered.")
        self._clear_tables()
        self.query_one("#launch-count", Static).update("0 shown")

    def _render_launches(self, report: WalletIntelligenceReport) -> None:
        table = self.query_one("#launches-table", DataTable)
        table.clear(columns=False)
        query = self.query_one("#launch-filter", Input).value.strip().lower()
        shown = 0
        for launch in report.launches:
            if query and not launch_matches(launch, query):
                continue
            shown += 1
            table.add_row(
                str(launch.slot),
                str(launch.transaction_index),
                launch.symbol or launch.name or "-",
                short_address(launch.mint),
                short_address(launch.creator),
                short_address(launch.signature),
            )
        self.query_one("#launch-count", Static).update(
            f"{shown}/{report.launch_count} shown"
        )

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

    def _clear_tables(self) -> None:
        self.query_one("#launches-table", DataTable).clear(columns=False)
        self.query_one("#nodes-table", DataTable).clear(columns=False)
        self.query_one("#edges-table", DataTable).clear(columns=False)


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
    metric.append(f"{value}\n", style=f"bold {COLOR_TEXT}")
    metric.append(detail, style=COLOR_MUTED)
    return metric


def format_signals(report: WalletIntelligenceReport) -> str:
    """Format bounded behavioral signals without presenting them as proof."""

    evidence = rug_evidence_summary(report)
    signals: list[str] = []
    signals.append(f"FINALIZED ASSESSMENT  {evidence['assessment']}")
    signals.append(
        f"INDEXED HISTORY  {evidence['indexed_creator_history']}  |  "
        f"created {evidence['indexed_created_count'] or '-'}"
    )
    signals.append(
        f"OPERATOR HISTORY  {evidence['operator_history']}  |  "
        f"direct {evidence['launch_count']}  linked {evidence['linked_launch_count']}"
    )
    signals.append(
        f"EARLY POSITIONS  {evidence['early_position_launch_count']}  |  "
        f"LINKED CREATORS  {evidence['linked_creator_wallet_count']}"
    )
    signals.append(
        f"WALLET LINKS  {evidence['direct_linked_wallet_count']}  |  "
        f"SWITCHES {evidence['wallet_switch_count']}  |  "
        f"FRESH {evidence['fresh_wallet_proven_count']}  |  "
        f"MULTI-HOP {evidence['multi_hop_transfer_count']}"
    )
    if report.creator_history is not None:
        history = report.creator_history
        ath = history.ath_symbol or history.ath_token or "-"
        signals.append(
            f"GMGN HISTORY  {history.total_created_count} created  |  "
            f"{history.open_count} open  |  ATH {ath} {history.ath_market_cap or '-'}"
        )
    else:
        signals.append("GMGN HISTORY  unavailable; indexed creator attribution missing")
    if evidence["flags"]:
        signals.append("FLAGS  " + ", ".join(evidence["flags"]))
    if not signals:
        signals.append("NO POSITIVE CREATOR OR LINK SIGNALS IN THIS BOUNDED WINDOW")
    return "\n".join(signals)


def format_flow(report: WalletIntelligenceReport) -> str:
    """Format observed native flows and the bounded slot span."""

    return " | ".join(
        (
            f"IN  {format_sol(report.native_in_lamports)} SOL",
            f"OUT  {format_sol(report.native_out_lamports)} SOL",
            f"SPAN  {report.first_seen_slot or '-'} -> {report.last_seen_slot or '-'}",
        )
    )


def format_graph_map(report: WalletIntelligenceReport) -> str:
    """Render direct target links without implying expanded links are direct."""

    lines = [f"TARGET  {short_address(report.target_wallet)}"]
    direct_edges = tuple(
        edge
        for edge in report.edges
        if report.target_wallet in {edge.source, edge.target}
    )
    if not direct_edges:
        lines.append("  (no direct native-transfer edges observed)")
        return "\n".join(lines)
    for edge in direct_edges[:GRAPH_PREVIEW_LIMIT]:
        direction = "<-" if edge.target == report.target_wallet else "->"
        peer = edge.source if direction == "<-" else edge.target
        lines.append(
            f"  {direction} {short_address(peer)}  "
            f"{edge.transfer_count} transfer(s), {format_sol(edge.amount_lamports)} SOL"
        )
    if len(direct_edges) > GRAPH_PREVIEW_LIMIT:
        lines.append(
            f"  ... {len(direct_edges) - GRAPH_PREVIEW_LIMIT} more direct links in table"
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


def _setting_int(value: str, field_name: str) -> int:
    if not value or not value.isdecimal():
        raise SniperConfigError(  # noqa: TRY003
            f"{field_name} must be a non-negative integer"
        )
    return int(value)


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
    }


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w",
        delete=False,
        dir=path.parent,
        encoding="utf-8",
    ) as temporary:
        temporary.write(text)
        temporary.flush()
        os.fsync(temporary.fileno())
        temporary_path = Path(temporary.name)
    temporary_path.replace(path)


def format_sol(lamports: int) -> str:
    """Format lamports with integer arithmetic only."""

    whole, fraction = divmod(lamports, 1_000_000_000)
    return f"{whole}.{fraction:09d}".rstrip("0").rstrip(".")


def main(argv: Sequence[str] | None = None) -> int:
    """Start the wallet intelligence TUI."""

    parser = argparse.ArgumentParser(description="Interactive wallet intelligence TUI.")
    parser.add_argument("--wallet", required=True)
    parser.add_argument("--config", type=Path, default=Path("watch.yaml"))
    parser.add_argument("--max-transactions", type=int, default=100)
    parser.add_argument("--max-linked-wallets", type=int, default=8)
    parser.add_argument("--refresh-seconds", type=int, default=30)
    parser.add_argument("--as-of-slot", type=int)
    args = parser.parse_args(argv)
    endpoint = os.environ.get("SOLANA_RPC_HTTP") or os.environ.get(
        "SOLANA_NODE_RPC_ENDPOINT"
    )
    if not endpoint:
        parser.error("SOLANA_RPC_HTTP or SOLANA_NODE_RPC_ENDPOINT is required")
    if args.refresh_seconds <= 0:
        parser.error("--refresh-seconds must be positive")
    if not 1 <= args.max_transactions <= MAX_TUI_HISTORY:
        parser.error("--max-transactions must be between 1 and 100")
    if args.as_of_slot is not None and args.as_of_slot < 0:
        parser.error("--as-of-slot must be non-negative")
    app = WalletIntelApp(
        args.wallet,
        endpoint=endpoint,
        max_transactions=args.max_transactions,
        max_linked_wallets=args.max_linked_wallets,
        refresh_seconds=args.refresh_seconds,
        as_of_slot=args.as_of_slot,
        config_path=args.config,
    )
    app.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
