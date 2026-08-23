"""WALLET / RISK panel with balance, exposure, daily PnL, and visual budget progress bar."""

# ruff: noqa: PLR0913, F401, TC002, BLE001, S110

from __future__ import annotations

from typing import Any

from textual.app import ComposeResult
from textual.containers import Vertical
from textual.reactive import reactive
from textual.widget import Widget
from textual.widgets import Static


class WalletRiskPanel(Widget):
    """Clean wallet balance and risk budget telemetry widget."""

    DEFAULT_CSS = """
    WalletRiskPanel {
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

    .wallet-risk-content {
        height: 1fr;
        padding: 0 1;
        overflow: hidden hidden;
    }
    """

    balance_sol: reactive[float] = reactive(0.0)
    exposure_sol: reactive[float] = reactive(0.0)
    open_positions: reactive[int] = reactive(0)
    daily_pnl_sol: reactive[float] = reactive(0.0)
    risk_budget_sol: reactive[float] = reactive(0.0)
    budget_pct: reactive[int] = reactive(0)
    telemetry_available: reactive[bool] = reactive(default=False)

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static("WALLET / RISK", classes="panel-header")
            yield Static(
                self._render_content(),
                id="wallet-risk-static",
                classes="wallet-risk-content",
            )

    def _render_content(self) -> str:
        if not self.telemetry_available:
            return "\n".join(
                (
                    "[dim]Balance[/dim]          [yellow]NO DATA[/yellow]",
                    "[dim]Exposure[/dim]         [yellow]NO DATA[/yellow]",
                    "[dim]Open positions[/dim]   [yellow]NO DATA[/yellow]",
                    "[dim]Daily PnL[/dim]        [yellow]NO DATA[/yellow]",
                    "[dim]Risk budget left[/dim] [yellow]NO DATA[/yellow]",
                    "",
                    "[dim]Awaiting execution-wallet telemetry.[/dim]",
                )
            )
        pnl_color = "green" if self.daily_pnl_sol >= 0 else "red"
        pnl_sign = "+" if self.daily_pnl_sol >= 0 else ""

        # Visual progress bar (18 chars)
        filled_len = int(18 * (self.budget_pct / 100.0))
        empty_len = 18 - filled_len
        bar_str = f"[bold green]{'█' * filled_len}[/bold green][dim]{'░' * empty_len}[/dim] [green]{self.budget_pct}%[/green]"

        lines = [
            f"[dim]Balance[/dim]          [white]{self.balance_sol:.3f} SOL[/white]",
            f"[dim]Exposure[/dim]         [white]{self.exposure_sol:.3f} SOL[/white]",
            f"[dim]Open positions[/dim]   [white]{self.open_positions}[/white]",
            f"[dim]Daily PnL[/dim]        [bold {pnl_color}]{pnl_sign}{self.daily_pnl_sol:.3f} SOL[/bold {pnl_color}]",
            f"[dim]Risk budget left[/dim] [white]{self.risk_budget_sol:.3f} SOL[/white]",
            "",
            bar_str,
        ]
        return "\n".join(lines)

    def update_telemetry(
        self,
        *,
        balance: float = 0.0,
        exposure: float = 0.0,
        positions: int = 0,
        daily_pnl: float = 0.0,
        budget_left: float = 0.0,
        pct: int = 0,
    ) -> None:
        self.balance_sol = balance
        self.exposure_sol = exposure
        self.open_positions = positions
        self.daily_pnl_sol = daily_pnl
        self.risk_budget_sol = budget_left
        self.budget_pct = pct
        self.telemetry_available = True
        try:
            self.query_one("#wallet-risk-static", Static).update(self._render_content())
        except Exception:
            pass
