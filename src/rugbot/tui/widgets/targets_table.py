"""Table of tracked funders and their persisted execution policies."""

# ruff: noqa: TC002, ANN401

from __future__ import annotations

import contextlib
from typing import Any

from textual.app import ComposeResult
from textual.containers import Vertical
from textual.message import Message
from textual.reactive import reactive
from textual.widget import Widget
from textual.widgets import DataTable, Static

from rugbot.tracker.models import TargetExecutionMode, TargetRecord
from rugbot.tui.formatters import format_sol, short_address


class TargetsTable(Widget):
    """Dense, scan-first table of tracker funders and target-local policies."""

    DEFAULT_CSS = """
    TargetsTable {
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

    .targets-table-container {
        height: 1fr;
        width: 100%;
        overflow: hidden hidden;
    }

    DataTable {
        height: 100%;
        width: 100%;
        background: #0d1117;
        border: none;
        scrollbar-gutter: stable;
        overflow-x: hidden;
    }

    .panel-action-bar {
        height: 1;
        padding: 0 1;
        background: #161b22;
        color: #8b949e;
        border-top: solid #21262d;
    }
    """

    class TargetSelected(Message):
        """Emit the newly highlighted target policy."""

        def __init__(self, target: TargetRecord) -> None:
            super().__init__()
            self.target = target

    selected_target_address: reactive[str | None] = reactive(None)

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._targets: dict[str, TargetRecord] = {}
        self._selected_target_address: str | None = None

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static("TARGETS", classes="panel-header")
            with Vertical(classes="targets-table-container"):
                yield DataTable(id="targets-datatable", cursor_type="row")
            yield Static(
                "[bold cyan][A][/bold cyan] ADD DEV   "
                "[bold cyan][F][/bold cyan] CLUSTER GRAPH   "
                "[bold cyan][E][/bold cyan] EDIT   "
                "[bold cyan][L][/bold cyan] LIVE/SIM   "
                "[bold cyan][P][/bold cyan] PAUSE",
                classes="panel-action-bar",
            )

    def on_mount(self) -> None:
        table = self.query_one("#targets-datatable", DataTable)
        table.add_column("TARGET", key="wallet", width=14)
        table.add_column("MODE", key="mode", width=12)
        table.add_column("BUY", key="buy", width=7)
        self.refresh_table()

    def set_targets(self, targets: list[TargetRecord]) -> None:
        """Replace projections with the repository-backed funder set."""
        self._targets = {target.address: target for target in targets}
        if self._selected_target_address not in self._targets:
            self._selected_target_address = targets[0].address if targets else None
        with contextlib.suppress(Exception):
            self.query_one("#targets-datatable", DataTable).clear()
        self.refresh_table()

    def refresh_table(self) -> None:
        """Render each persisted policy without visual fallback values."""
        table = self.query_one("#targets-datatable", DataTable)
        if not self._targets:
            if "empty" not in table.rows:
                table.add_row(
                    "[dim]No target[/dim]",
                    "[dim]--[/dim]",
                    "[dim]--[/dim]",
                    key="empty",
                )
            return

        if "empty" in table.rows:
            table.clear()

        for target in self._targets.values():
            marker, mode_text, buy_text = self._policy_cells(target)
            label_display = (
                target.label
                if target.label and target.label != "Target Dev"
                else short_address(target.address)
            )
            wallet_text = f"{marker} [white]{label_display}[/white]"
            if target.address in table.rows:
                table.update_cell(target.address, "wallet", wallet_text)
                table.update_cell(target.address, "mode", mode_text)
                table.update_cell(target.address, "buy", buy_text)
            else:
                table.add_row(wallet_text, mode_text, buy_text, key=target.address)

    @staticmethod
    def _policy_cells(target: TargetRecord) -> tuple[str, str, str]:
        policy = target.policy
        if policy is None:
            return "[dim]o[/dim]", "[yellow]UNCONFIGURED[/yellow]", "--"
        buy_text = format_sol(policy.quote_size_lamports)
        if not policy.monitoring_enabled:
            return "[dim]o[/dim]", "[yellow]PAUSED[/yellow]", buy_text
        if policy.execution_mode is TargetExecutionMode.LIVE:
            return (
                "[bold green]*[/bold green]",
                "[bold green]LIVE[/bold green]",
                buy_text,
            )
        if policy.execution_mode is TargetExecutionMode.SIMULATED:
            return (
                "[bold cyan]*[/bold cyan]",
                "[bold cyan]DRY RUN[/bold cyan]",
                buy_text,
            )
        return "[dim]o[/dim]", "[dim]OBSERVE[/dim]", buy_text

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        row_key = event.row_key.value if event.row_key else None
        if row_key is not None and row_key in self._targets:
            self._selected_target_address = row_key
            self.post_message(self.TargetSelected(self._targets[row_key]))

    def get_active_targets_count(self) -> int:
        """Return targets whose monitoring policy is currently enabled."""
        return sum(
            target.policy is not None and target.policy.monitoring_enabled
            for target in self._targets.values()
        )

    def get_selected_target(self) -> TargetRecord | None:
        """Return the highlighted target, or the first available target."""
        if self._selected_target_address in self._targets:
            return self._targets[self._selected_target_address]
        return next(iter(self._targets.values()), None)

    def update_target(self, target: TargetRecord) -> None:
        """Update one policy projection after a persisted mutation."""
        self._targets[target.address] = target
        self.refresh_table()
