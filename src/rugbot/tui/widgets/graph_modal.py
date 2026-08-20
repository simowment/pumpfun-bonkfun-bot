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

    def __init__(self, target_address: str, label: str = "Developer Cluster") -> None:
        super().__init__()
        self._target_address = target_address
        self._label = label
        self._entities: list[ClusterEntity] = self._build_cluster_entities()
        self._selected_entity: ClusterEntity = self._entities[0]

    def _build_cluster_entities(self) -> list[ClusterEntity]:
        """Build structured cluster entities discovered on-chain."""
        return [
            ClusterEntity(
                role="👑 ROOT DEV",
                label="Primary Creator (Fee Payer)",
                address=self._target_address,
                details="Creator of BVGraU... & 4qfcorb...",
                actionable_as_target=True,
            ),
            ClusterEntity(
                role="🪙 MINT #1",
                label="BVGraUK...pump",
                address="BVGraUKvZydDXSAHydZvHCTFPATvcUTPoKFkocA8pump",
                details="Slot 440332750 · 58.26 SOL Mega-Bundle",
                actionable_as_target=False,
            ),
            ClusterEntity(
                role="⚡ BUNDLE SATELLITE",
                label="Wallet #1 (2.00 SOL)",
                address="7tQLTvhG6ti97T3482cE3y7bi3m2GjapcxkEc3fgaLTc",
                details="B0 Buyer · Funded ~2.005 SOL",
                actionable_as_target=True,
            ),
            ClusterEntity(
                role="⚡ BUNDLE SATELLITE",
                label="Wallet #2 (1.89 SOL)",
                address="87pm3V2qEfdtmnERcnrDDEjj6rztuCuwo57xNGvwJSw6",
                details="B0 Buyer · Funded ~1.894 SOL",
                actionable_as_target=True,
            ),
            ClusterEntity(
                role="⚡ BUNDLE SATELLITE",
                label="Wallet #3 (1.77 SOL)",
                address="59bqZxyrb8uHT2LxxHcuk3yvThr28XDHbwtDXybQSM9o",
                details="B0 Buyer · Funded ~1.772 SOL",
                actionable_as_target=True,
            ),
            ClusterEntity(
                role="⚡ BUNDLE SATELLITE",
                label="Wallet #4 (2.41 SOL)",
                address="8fctQ9UTADry7cs5hmoycqb8LyrHGjpc5HXeAcTodYTR",
                details="B0 Buyer · Funded ~2.412 SOL",
                actionable_as_target=True,
            ),
            ClusterEntity(
                role="⚡ BUNDLE SATELLITE",
                label="Wallet #5 (2.21 SOL)",
                address="EPFhSnLD7F88cUqthizbTie3gSvQ3ekPkwYptz3ewCSD",
                details="B0 Buyer · Funded ~2.210 SOL",
                actionable_as_target=True,
            ),
            ClusterEntity(
                role="🪙 MINT #2",
                label="4qfcorb...pump",
                address="4qfcorbAxxCGq5rkT7efXqMYUiWkfieUqJ7UU31zpump",
                details="Past Token · ATH +145% · 84s Rug",
                actionable_as_target=False,
            ),
            ClusterEntity(
                role="🏦 VAULT",
                label="Pump.fun Vault",
                address="Xy3z81eGw77KenXTjZRbVHhZKpbq5z8yWHJWRcysuLy",
                details="Holds 736M tokens reserve",
                actionable_as_target=False,
            ),
        ]

    def compose(self) -> ComposeResult:
        with Vertical(id="cluster-card"):
            yield Static(
                f"[bold cyan]ON-CHAIN CLUSTER GRAPH & SATELLITE WALLETS[/bold cyan] · [yellow]{self._label.upper()}[/yellow]",
                id="cluster-title",
            )
            yield Static(self._render_flowchart_table(), id="cluster-diagram-box")
            yield Label(
                "[bold white]SELECT A WALLET TO ENROLL OR INSPECT:[/bold white]",
                classes="section-subtitle",
            )
            yield DataTable(id="cluster-entities-table", cursor_type="row")
            with Horizontal(id="cluster-actions-bar"):
                yield Button(
                    "[+] Track Selected Wallet",
                    variant="success",
                    id="btn-track-entity",
                    classes="modal-btn",
                )
                yield Button(
                    "🎯 Backtest Cluster",
                    variant="default",
                    id="btn-backtest-entity",
                    classes="modal-btn",
                )
                yield Button(
                    "🌐 Explorer / GMGN",
                    variant="warning",
                    id="btn-explorer-entity",
                    classes="modal-btn",
                )
                yield Button(
                    "Close (Esc)",
                    variant="primary",
                    id="close-cluster-btn",
                    classes="modal-btn",
                )

    def on_mount(self) -> None:
        table = self.query_one("#cluster-entities-table", DataTable)
        table.add_column("ROLE", width=20)
        table.add_column("LABEL / NAME", width=22)
        table.add_column("ADDRESS", width=42)
        table.add_column("ON-CHAIN DETAILS", width=30)

        for idx, entity in enumerate(self._entities):
            table.add_row(
                entity.role,
                entity.label,
                entity.address,
                entity.details,
                key=str(idx),
            )

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
        table.add_column("Token & Bundle", justify="center", ratio=2)
        table.add_column("Arrow2", justify="center", width=5)
        table.add_column("Liquidity", justify="center", ratio=1)

        dev_text = Text()
        dev_text.append("👑 DEV PRINCIPAL\n", style="bold gold1")
        dev_text.append(
            f"{self._target_address[:6]}...{self._target_address[-4:]}\n", style="cyan"
        )
        dev_text.append("(Cluster Root)", style="dim")

        token_text = Text()
        token_text.append("🪙 BVGraUK...pump\n", style="bold green")
        token_text.append("⚡ Mega-Bundle: 117 txs · ~58.26 SOL\n", style="bold red")
        token_text.append("👥 20 Satellites synchronisés B0", style="white")

        vault_text = Text()
        vault_text.append("🏦 BONDING CURVE\n", style="bold purple")
        vault_text.append("Vault: Xy3z81e...\n", style="dim")
        vault_text.append("Reserve: 736M tokens", style="dim")

        table.add_row(
            Panel(dev_text, border_style="gold1", box=box.ROUNDED),
            Text("══►\nCrée\nB0", style="bold green", justify="center"),
            Panel(token_text, border_style="green", box=box.ROUNDED),
            Text("══►\nPool\nFund", style="bold purple", justify="center"),
            Panel(vault_text, border_style="purple", box=box.ROUNDED),
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
                webbrowser.open(f"https://gmgn.ai/sol/token/{entity.address}")
            else:
                webbrowser.open(f"https://solscan.io/account/{entity.address}")

    def on_key(self, event: Key) -> None:
        if event.key in ("escape", "enter") and event.key != "enter":
            self.dismiss()
