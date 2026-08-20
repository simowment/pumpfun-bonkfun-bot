"""Interactive Shortcuts & Operator Cheatsheet Modal."""

from __future__ import annotations

from typing import TYPE_CHECKING

from rich import box
from rich.table import Table
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Static

if TYPE_CHECKING:
    from textual.app import ComposeResult
    from textual.events import Key


class HelpCheatsheetScreen(ModalScreen[None]):
    """Cheatsheet modal displaying every single keyboard shortcut and operator workflow."""

    DEFAULT_CSS = """
    HelpCheatsheetScreen {
        align: center middle;
        background: rgba(0, 0, 0, 0.85);
    }

    #cheatsheet-card {
        width: 100;
        height: auto;
        max-height: 90%;
        background: #0d1117;
        border: solid #58a6ff;
        padding: 1 2;
    }

    #cheatsheet-title {
        text-style: bold;
        color: #58a6ff;
        margin-bottom: 1;
    }

    #cheatsheet-table-box {
        height: auto;
        margin-bottom: 1;
    }

    #close-cheatsheet-btn {
        width: 100%;
        height: 3;
        margin-top: 1;
    }
    """

    def compose(self) -> ComposeResult:
        with Vertical(id="cheatsheet-card"):
            yield Static(
                "[bold cyan]RUGBOT OPERATOR SHORTCUTS & CHEATSHEET[/bold cyan]",
                id="cheatsheet-title",
            )
            yield Static(self._render_cheatsheet_table(), id="cheatsheet-table-box")
            yield Button(
                "Close (Esc / Enter)", variant="primary", id="close-cheatsheet-btn"
            )

    def _render_cheatsheet_table(self) -> Table:
        table = Table(
            box=box.ROUNDED,
            show_header=True,
            header_style="bold cyan",
            expand=True,
            border_style="#30363d",
        )
        table.add_column("KEY", justify="center", style="bold yellow", width=12)
        table.add_column("ACTION / WORKFLOW", style="bold white", width=26)
        table.add_column("DESCRIPTION & EFFECT", style="dim", width=54)

        table.add_row(
            "[1]",
            "Dashboard View",
            "Targets table, Live activity feed, and Execution cockpit.",
        )
        table.add_row(
            "[2]",
            "Dev Token History",
            "Chronological token creations, ATH multipliers, and rug timers.",
        )
        table.add_row(
            "[3]",
            "Sniper Positions",
            "Active and historical positions, live PnL, gas costs, and emergency exits.",
        )
        table.add_row(
            "[4]",
            "Target Settings",
            "Configure buy size (SOL), TP/SL (%), slippage, and Jito MEV tips.",
        )
        table.add_row(
            "[5] or [F]",
            "Cluster Graph",
            "In-place on-chain flowchart: Root dev -> Mega-Bundle -> Satellites.",
        )
        table.add_row(
            "[A]",
            "Add Dev / Token",
            "Paste token mint or dev address + optional alias (stored in SQLite).",
        )
        table.add_row(
            "[E]",
            "Edit Target Policy",
            "Open Settings for highlighted target to customize take-profit & size.",
        )
        table.add_row(
            "[L]",
            "Toggle Live / Sim",
            "Arm real Solana transactions (🔴 LIVE) or zero-risk paper trade (🧪 SIM).",
        )
        table.add_row(
            "[P]",
            "Pause / Resume",
            "Enable or disable 24/7 background detection for the selected target.",
        )
        table.add_row(
            "[H]",
            "Quick Sell 50%",
            "Instantly dump half of the open position to secure profits.",
        )
        table.add_row(
            "[X]",
            "Panic Exit 100%",
            "Immediately market-sell the entire position with high priority fee.",
        )
        table.add_row(
            r"\[/]",
            "Global Search",
            "Filter wallets, tokens, mints, and signatures instantly.",
        )
        table.add_row(
            r"\[?]",
            "Open Cheatsheet",
            "Display this operator shortcuts reference table.",
        )
        table.add_row(
            r"\[Q]",
            "Quit Bot",
            "Safely exit the terminal application.",
        )
        return table

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "close-cheatsheet-btn":
            self.dismiss()

    def on_key(self, event: Key) -> None:
        if event.key in ("escape", "enter", "?", "q"):
            self.dismiss()
