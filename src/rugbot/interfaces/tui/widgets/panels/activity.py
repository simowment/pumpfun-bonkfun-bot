"""LIVE ACTIVITY panel matching operator mockup."""

# ruff: noqa: PLR0913, TC002, BLE001, S105, S110, ANN401, S107

from __future__ import annotations

import time
from typing import Any

from textual.app import ComposeResult
from textual.containers import Vertical
from textual.message import Message
from textual.reactive import reactive
from textual.widget import Widget
from textual.widgets import DataTable, Static

from rugbot.interfaces.tui.formatters import short_address


class ActivityItem:
    """Canonical model for a single row in the LiveActivityView."""

    def __init__(
        self,
        row_id: str,
        timestamp: int,
        event_type: str,
        root_funder: str,
        target_wallet: str,
        token_symbol: str = "—",
        token_name: str = "",
        token_mint: str = "",
        amount_lamports: int | None = None,
        block_number: int = -1,
        market_cap_usd: float | None = None,
        dev_score: float | None = None,
        signal: str = "UNASSESSED",
        signature: str = "",
        reason: str = "",
        latency_summary: str = "",
        hops: int = 1,
        **_kwargs: Any,
    ) -> None:
        self.row_id = row_id
        self.timestamp = timestamp
        self.event_type = event_type
        self.root_funder = root_funder
        self.target_wallet = target_wallet
        self.token_symbol = token_symbol
        self.token_name = token_name
        self.token_mint = token_mint
        self.amount_lamports = amount_lamports
        self.block_number = block_number
        self.market_cap_usd = market_cap_usd
        self.dev_score = dev_score
        self.signal = signal
        self.signature = signature
        self.reason = reason
        self.latency_summary = latency_summary
        self.hops = hops


class EmptyStateView(Widget):
    """Empty state widget for backwards compatibility."""

    def compose(self) -> ComposeResult:
        yield Static("Waiting for qualified launch", id="empty-info-static")

    def update_state(self, **_kwargs: Any) -> None:
        pass


class LiveActivityView(Widget):
    """LIVE ACTIVITY panel matching the operator mockup with structured top card, recent feed, and filter bar."""

    DEFAULT_CSS = """
    LiveActivityView {
        height: 100%;
        width: 100%;
        background: #0d1117;
        border: solid #21262d;
        padding: 0;
        overflow: hidden hidden;
    }

    .panel-header {
        height: 1;
        padding: 0 1;
        color: #e3b341;
        text-style: bold;
        background: #161b22;
    }

    #activity-top-card {
        height: auto;
        padding: 0 1;
        border-bottom: solid #21262d;
    }

    .activity-table-container {
        height: 1fr;
        width: 100%;
        overflow: hidden hidden;
    }

    DataTable {
        height: 100%;
        width: 100%;
        background: #0d1117;
        border: none;
        overflow-x: hidden;
    }

    .feed-filter-bar {
        height: 1;
        padding: 0 1;
        background: #161b22;
        color: #8b949e;
        border-top: solid #21262d;
    }
    """

    class EventSelected(Message):
        def __init__(self, item: ActivityItem | None) -> None:
            super().__init__()
            self.item = item

    class FullInspectRequested(Message):
        def __init__(self, item: ActivityItem) -> None:
            super().__init__()
            self.item = item

    watching_target: reactive[str] = reactive("—")
    mode_str: reactive[str] = reactive("DRY RUN")
    stream_status: reactive[str] = reactive("NO DATA")
    rpc_status_str: reactive[str] = reactive("NO DATA")
    selected_row_id: reactive[str | None] = reactive(None)
    last_event_str: reactive[str] = reactive("—")

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._items: dict[str, ActivityItem] = {}
        self._row_order: list[str] = []
        self._active_filter: str = "ALL"

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static("LIVE ACTIVITY", classes="panel-header")
            yield Static(self._render_top_card(), id="activity-top-card")
            with Vertical(classes="activity-table-container"):
                yield DataTable(id="activity-table", cursor_type="row")
            yield Static(
                "FILTER: [bold cyan]ALL[/bold cyan]   [cyan]LAUNCH[/cyan]   [yellow]SKIP[/yellow]   [cyan]SIM[/cyan]   [green]EXEC[/green]   [red]FAIL[/red]   [magenta]SELL[/magenta]",
                classes="feed-filter-bar",
            )

    def on_mount(self) -> None:
        table = self.query_one("#activity-table", DataTable)
        table.add_column("TIME", key="time", width=9)
        table.add_column("STATUS", key="status", width=8)
        table.add_column("TOKEN", key="token", width=9)
        table.add_column("DETAIL", key="detail", width=26)

    def _render_top_card(self) -> str:
        rpc_style = (
            "bold green" if self.rpc_status_str.startswith("ONLINE") else "yellow"
        )
        lines = [
            "[yellow]Waiting for next launch[/yellow]",
            f"[dim]Watching[/dim]      [cyan]{self.watching_target}[/cyan]",
            f"[dim]Target Mode[/dim]   [cyan]{self.mode_str}[/cyan]",
            f"[dim]Tracking[/dim]      [{rpc_style}]● {self.rpc_status_str}[/{rpc_style}]",
            f"[dim]Last event[/dim]    [white]{self.last_event_str}[/white]",
        ]
        return "\n".join(lines)

    def add_event(self, item: ActivityItem) -> None:
        # Ignore non-trading internal lifecycle events in the trading activity table
        if item.event_type.upper() in (
            "FUNDER_ADDED",
            "FUNDER_REMOVED",
            "STREAM_CONNECTED",
            "STREAM_RECONNECTED",
            "TTL_EXPIRED",
        ):
            return

        table = self.query_one("#activity-table", DataTable)
        self._items[item.row_id] = item
        self._row_order.insert(0, item.row_id)

        ev_upper = item.event_type.upper()
        if ev_upper in {"PASS", "QUALIFIED"}:
            type_badge = "[bold green]PASS[/bold green]"
        elif ev_upper in {"SKIP", "SKIPPED", "IGNORED"}:
            type_badge = "[bold yellow]SKIP[/bold yellow]"
        elif ev_upper in {"EXEC", "BUY"}:
            type_badge = "[bold green]EXEC[/bold green]"
        elif ev_upper in {"SIM", "SIMULATED"}:
            type_badge = "[bold cyan]SIM[/bold cyan]"
        elif ev_upper in {"LAUNCH", "DETECTED", "LAUNCH_DETECTED"}:
            type_badge = "[bold cyan]LAUNCH[/bold cyan]"
        elif ev_upper in {"FAIL", "FAILED", "REJECTED"}:
            type_badge = "[bold red]FAIL[/bold red]"
        elif ev_upper in {"SELL", "EXIT"}:
            type_badge = "[bold magenta]SELL[/bold magenta]"
        else:
            type_badge = f"[white]{ev_upper}[/white]"

        token_display = (
            f"[bold white]{item.token_symbol}[/bold white]"
            if item.token_symbol != "—"
            else "[dim]—[/dim]"
        )
        time_display = time.strftime("%H:%M:%S", time.localtime(item.timestamp))

        detail_text = item.reason or item.latency_summary or item.signal
        if not detail_text or detail_text == "UNASSESSED":
            detail_text = "observed on-chain"

        table.add_row(
            time_display, type_badge, token_display, detail_text, key=item.row_id
        )
        table.move_cursor(row=table.row_count - 1)
        self.last_event_str = f"{type_badge} {item.token_symbol}"
        self._update_top()

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        """Expose the actual selected feed row to the enclosing operator view."""
        row_id = event.row_key.value if event.row_key is not None else None
        if row_id is None or row_id not in self._items:
            return
        self.selected_row_id = row_id
        self.post_message(self.EventSelected(self._items[row_id]))

    def set_funders(self, funders: list[str]) -> None:
        if funders:
            self.watching_target = short_address(funders[0])
            self._update_top()

    def update_summary(
        self, target: str, mode: str, rpc_status: str = "NO DATA"
    ) -> None:
        self.watching_target = short_address(target)
        self.mode_str = mode
        self.rpc_status_str = rpc_status
        self._update_top()

    def update_tracking_status(self, status: str) -> None:
        """Update the launch-feed health label from canonical runtime state."""

        self.rpc_status_str = status
        self._update_top()

    def clear(self) -> None:
        """Wipe all activity feed items and reset state."""
        self._items.clear()
        self._row_order.clear()
        self.selected_row_id = None
        self.last_event_str = "—"
        try:
            self.query_one("#activity-table", DataTable).clear()
        except Exception:
            pass
        self._update_top()

    def _update_top(self) -> None:
        try:
            self.query_one("#activity-top-card", Static).update(self._render_top_card())
        except Exception:
            pass

    def resume_follow(self) -> None:
        pass
