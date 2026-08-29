"""Interactive and actionable On-Chain Cluster & Bundle Graph modal."""

# ruff: noqa: SLF001

from __future__ import annotations

import webbrowser
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from rich import box
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, DataTable, Label, Static

from rugbot.tracker.models import (
    TargetExecutionMode,
    TargetExecutionPolicy,
)

if TYPE_CHECKING:
    from textual.app import ComposeResult
    from textual.events import Key


@dataclass(frozen=True, slots=True)
class ClusterEntity:
    """A discovered entity node within an on-chain developer cluster."""

    role: str
    label: str
    address: str
    details: str
    actionable_as_target: bool


class ClusterGraphModal(ModalScreen[None]):
    """Actionable on-chain cluster graph modal with Rich panels and instant target enrollment."""

    DEFAULT_CSS = """
    ClusterGraphModal {
        align: center middle;
        background: rgba(0, 0, 0, 0.85);
    }

    #cluster-card {
        width: 105;
        height: auto;
        max-height: 94%;
        background: #0d1117;
        border: solid #58a6ff;
        padding: 1 2;
    }

    #cluster-title {
        text-style: bold;
        color: #58a6ff;
        margin-bottom: 1;
    }

    #cluster-diagram-box {
        height: auto;
        max-height: 12;
        margin-bottom: 1;
        background: #161b22;
        border: solid #30363d;
        padding: 0 1;
    }

    #cluster-entities-table {
        height: 8;
        background: #0d1117;
        border: solid #30363d;
        margin-bottom: 1;
    }

    #cluster-actions-bar {
        width: 100%;
        height: 3;
        layout: horizontal;
        margin-top: 1;
    }

    .modal-btn {
        margin-right: 1;
        height: 3;
        min-width: 16;
    }

    #close-cluster-btn {
        min-width: 12;
    }
    """

    def __init__(
        self,
        target_address: str,
        label: str = "Developer Cluster",
        entities: list[ClusterEntity] | None = None,
    ) -> None:
        super().__init__()
        self._target_address = target_address
        self._label = label
        self._entities: list[ClusterEntity] = (
            entities
            if entities
            else [
                ClusterEntity(
                    role="👑 ROOT DEV",
                    label="Primary Creator (Fee Payer)",
                    address=self._target_address,
                    details="Scanned Target Wallet",
                    actionable_as_target=True,
                )
            ]
        )
        self._selected_entity: ClusterEntity = self._entities[0]

    def compose(self) -> ComposeResult:
        with Vertical(id="cluster-card"):
            yield Static(
                f"[bold cyan]ON-CHAIN CLUSTER GRAPH & SATELLITE WALLETS[/bold cyan] · [yellow]{self._label.upper()}[/yellow]",
                id="cluster-title",
            )
            yield Static(self._render_flowchart_table(), id="cluster-diagram-box")

            yield Label(
                "[bold white]IDENTIFIED CLUSTER ENTITIES & SATELLITES (Select to Track):[/bold white]"
            )
            table = DataTable(id="cluster-entities-table", cursor_type="row")
            table.add_columns(
                "Role",
                "Alias / Label",
                "Solana Address",
                "On-Chain Details",
                "Actionable",
            )
            for idx, entity in enumerate(self._entities):
                table.add_row(
                    entity.role,
                    entity.label,
                    f"{entity.address[:8]}...{entity.address[-6:]}",
                    entity.details,
                    "TRACKABLE" if entity.actionable_as_target else "INFO ONLY",
                    key=str(idx),
                )
            yield table

            with Horizontal(id="cluster-actions-bar"):
                yield Button(
                    "🎯 Track Selected",
                    variant="primary",
                    id="btn-track-entity",
                    classes="modal-btn",
                )
                yield Button(
                    "📊 Backtest Entity",
                    variant="warning",
                    id="btn-backtest-entity",
                    classes="modal-btn",
                )
                yield Button(
                    "🌐 View On-Chain",
                    variant="default",
                    id="btn-explorer-entity",
                    classes="modal-btn",
                )
                yield Button("✕ Close", id="close-cluster-btn", classes="modal-btn")

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        idx = int(event.row_key.value)
        if 0 <= idx < len(self._entities):
            self._selected_entity = self._entities[idx]

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        idx = int(event.row_key.value)
        if 0 <= idx < len(self._entities):
            self._selected_entity = self._entities[idx]

    def _render_flowchart_table(self) -> Table:
        """Render mathematically aligned Rich Flowchart without broken Unicode edges."""
        table = Table(
            box=box.ROUNDED,
            show_header=False,
            expand=True,
            border_style="cyan",
            padding=(0, 1),
        )
        table.add_column("Dev Root", justify="center", ratio=1)
        table.add_column("Arrow", justify="center", width=5)
        table.add_column("Cluster Nodes", justify="center", ratio=2)
        table.add_column("Arrow2", justify="center", width=5)
        table.add_column("Status", justify="center", ratio=1)

        dev_text = Text()
        dev_text.append("👑 DEV PRINCIPAL\n", style="bold gold1")
        dev_text.append(
            f"{self._target_address[:6]}...{self._target_address[-4:]}\n", style="cyan"
        )
        dev_text.append("(Cluster Root)", style="dim")

        node_count = len(self._entities)
        nodes_text = Text()
        nodes_text.append(
            f"👥 Cluster Network ({node_count} nodes)\n", style="bold green"
        )
        nodes_text.append(
            f"⚡ Discovered on-chain from {self._target_address[:6]}...\n",
            style="bold red",
        )
        nodes_text.append("Verified via transaction history", style="white")

        status_text = Text()
        status_text.append("🏦 ON-CHAIN STATE\n", style="bold purple")
        status_text.append("Finalized RPC data\n", style="dim")
        status_text.append("Live Graph Tracking", style="dim")

        table.add_row(
            Panel(dev_text, border_style="gold1", box=box.ROUNDED),
            Text("══►\nDiscovers\nNodes", style="bold green", justify="center"),
            Panel(nodes_text, border_style="green", box=box.ROUNDED),
            Text("══►\nTracks\nState", style="bold purple", justify="center"),
            Panel(status_text, border_style="purple", box=box.ROUNDED),
        )
        return table

    def _enroll_selected_target(self, entity: ClusterEntity) -> None:
        """Enroll the selected cluster entity into SQLite targets repository."""
        app = self.app
        if not hasattr(app, "_service") or not hasattr(app, "_repository"):
            return
        now = datetime.now(UTC).isoformat()
        app._service.add_funder(entity.address, label=f"Cluster {entity.label}")
        if app._repository.get_target_execution_policy(entity.address) is None:
            app._service.save_target_execution_policy(
                TargetExecutionPolicy(
                    funder_address=entity.address,
                    monitoring_enabled=True,
                    execution_mode=TargetExecutionMode.SIMULATED,
                    quote_size_lamports=25_000_000,
                    take_profit_pnl_ppm=100_000,
                    stop_loss_pnl_ppm=-30_000,
                    max_slippage_bps=500,
                    priority_fee_microlamports=50_000,
                    jito_tip_lamports=1_500_000,
                    updated_at=now,
                )
            )
        if hasattr(app, "_refresh_target_records"):
            app._refresh_target_records()
        self.notify(
            f"Enrolled {entity.address[:8]}... as Tracked Target in SQLite!",
            severity="information",
        )

    def on_button_pressed(self, event: Button.Pressed) -> None:
        btn_id = event.button.id
        entity = self._selected_entity

        if btn_id == "close-cluster-btn":
            self.dismiss()
        elif btn_id == "btn-track-entity":
            if not entity.actionable_as_target:
                self.notify(
                    f"{entity.role} is a token/contract, not a wallet to track",
                    severity="warning",
                )
                return
            self._enroll_selected_target(entity)
        elif btn_id == "btn-backtest-entity":
            self.dismiss()
            if hasattr(self.app, "action_show_backtester"):
                self.app.action_show_backtester()
        elif btn_id == "btn-explorer-entity":
            if entity.role.startswith("🪙"):
                webbrowser.open(f"https://dexscreener.com/solana/{entity.address}")
            else:
                webbrowser.open(f"https://solscan.io/account/{entity.address}")

    def on_key(self, event: Key) -> None:
        if event.key in ("escape", "enter") and event.key != "enter":
            self.dismiss()
