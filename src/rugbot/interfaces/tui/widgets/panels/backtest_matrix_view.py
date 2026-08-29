"""High-density, visual Take-Profit backtest matrix and Bible qualification widget."""

from __future__ import annotations

from typing import TYPE_CHECKING

from rich import box
from rich.console import Group, RenderableType
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from textual.widgets import Static

if TYPE_CHECKING:
    from rugbot.backtest.runners.cluster_optimizer import ClusterBacktestReport

HIGH_WINRATE_THRESHOLD = 60.0
MIN_WINRATE_THRESHOLD = 33.0


class BacktestMatrixWidget(Static):
    """Rich visual table rendering full TP grid search, adverse dump metrics, and Bible qualification."""

    DEFAULT_CSS = """
    BacktestMatrixWidget {
        width: 100%;
        height: auto;
        layout: vertical;
        background: #0d1117;
        margin-bottom: 1;
    }
    """

    def __init__(
        self, report: ClusterBacktestReport | None = None, **kwargs: object
    ) -> None:
        super().__init__(**kwargs)
        self._report = report

    def update_report(self, report: ClusterBacktestReport) -> None:
        """Update and render the full backtest optimization matrix."""
        self._report = report
        self.update(self._render_matrix())
        self.refresh(layout=True)

    def on_mount(self) -> None:
        if self._report:
            self.update(self._render_matrix())
        else:
            self.update(self._render_empty())

    def _render_empty(self) -> Table:
        table = Table(
            box=box.ROUNDED,
            show_header=True,
            header_style="bold cyan",
            expand=True,
            border_style="#30363d",
            padding=(0, 1),
        )
        table.add_column("CLUSTER TAKE-PROFIT BACKTEST & RISK MATRIX", justify="center")
        table.add_row(
            "[dim]Press [bold cyan]'B'[/bold cyan] or click [bold cyan]'Run Backtest (B)'[/bold cyan] to run multi-token grid search with realistic -75% dump modeling.[/dim]"
        )
        return table

    def _render_matrix(self) -> RenderableType:
        if not self._report or not self._report.evaluations:
            return self._render_empty()

        rep = self._report

        # 1. Parameter Summary Bar
        header_text = Text()
        header_text.append(
            f"CLUSTER: {rep.root_funder[:8]}... ({rep.total_tokens_evaluated} Tokens · {rep.cluster_wallets_count} Wallets)\n",
            style="bold gold1",
        )
        header_text.append(
            f"• Buy Size: {rep.buy_size_sol:.3f} SOL (~${rep.buy_size_sol * 150:.2f})  │  ",
            style="white",
        )
        header_text.append(
            f"Realized Dump Loss: -{rep.realized_dump_loss_pct * 100:.0f}% (Bonding Floor)  │  ",
            style="bold red",
        )
        header_text.append(
            f"Jito Tip: {rep.jito_tip_sol:.4f} SOL  │  DEX Fee: {rep.dex_fee_pct:.1f}%\n",
            style="cyan",
        )
        header_text.append(
            f"• Rug Dynamics: Avg Rug Delay {rep.avg_rug_delay_seconds:.0f}s (±{rep.rug_delay_std_seconds:.0f}s)  │  "
            f"Avg Rug MC ${rep.avg_rug_mc_usd:,.0f}  │  "
            f"Avg ATH: x{rep.avg_ath_multiplier:.2f} (Consistency: {rep.ath_consistency_pct:.1f}%)\n",
            style="bold yellow",
        )

        qual_style = "bold green" if rep.is_bible_qualified else "bold yellow"
        header_text.append(
            f"• Status: [{qual_style}]{rep.qualification_reason}[/{qual_style}]\n"
        )
        header_text.append(
            f"• OPTIMAL TP TARGET: {rep.optimal_tp_label}  (Winrate: {rep.optimal_roi_pct:+.1f}% ROI · Net EV: {rep.optimal_net_ev_sol:+.5f} SOL/trade)",
            style="bold gold1",
        )

        top_panel = Panel(
            header_text,
            title="[bold yellow]CLUSTER TAKE-PROFIT OPTIMIZATION & ADVERSE MODELING[/bold yellow]",
            border_style="gold1",
            box=box.ROUNDED,
        )

        # 2. Detailed TP Grid Search Table
        grid = Table(
            box=box.ROUNDED,
            show_header=True,
            header_style="bold cyan",
            expand=True,
            border_style="#30363d",
            padding=(0, 1),
        )
        grid.add_column("TP TARGET", justify="center", style="bold white", width=11)
        grid.add_column("WINRATE", justify="center", width=10)
        grid.add_column("W / L", justify="center", width=8)
        grid.add_column("GROSS PNL", justify="right", width=14)
        grid.add_column("FEES PAID", justify="right", width=13)
        grid.add_column("NET PNL (SOL)", justify="right", style="bold", width=16)
        grid.add_column("NET ROI", justify="center", style="bold", width=11)
        grid.add_column("STATUS", justify="center", width=16)

        for ev in rep.evaluations:
            status_style = (
                "bold gold1"
                if ev.is_optimal
                else ("green" if ev.net_ev_sol_per_trade > 0 else "dim red")
            )
            status_label = (
                "[OPTIMAL TP]"
                if ev.is_optimal
                else ("PROFITABLE" if ev.net_ev_sol_per_trade > 0 else "NEGATIVE EV")
            )

            wr_style = (
                "bold green"
                if ev.winrate_pct >= HIGH_WINRATE_THRESHOLD
                else ("yellow" if ev.winrate_pct >= MIN_WINRATE_THRESHOLD else "red")
            )
            roi_style = "bold green" if ev.net_roi_pct > 0 else "bold red"
            pnl_style = "bold green" if ev.total_net_pnl_sol > 0 else "bold red"

            gross_total = ev.gross_gains_sol - ev.gross_losses_sol

            grid.add_row(
                ev.tp_pct_label,
                f"[{wr_style}]{ev.winrate_pct:.1f}%[/{wr_style}]",
                f"{ev.wins}W / {ev.losses}L",
                f"{gross_total:>+9.5f} SOL",
                f"{ev.total_fees_paid_sol:>8.5f} SOL",
                f"[{pnl_style}]{ev.total_net_pnl_sol:>+10.5f} SOL[/{pnl_style}]",
                f"[{roi_style}]{ev.net_roi_pct:>+6.1f}%[/{roi_style}]",
                f"[{status_style}]{status_label}[/{status_style}]",
            )

        return Group(top_panel, grid)
