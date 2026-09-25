"""Reusable reporting sheet and statistical performance table for tokens, executions, and cabal clusters."""

# ruff: noqa: TC003, PLR2004, BLE001, TRY003, S603, S607

from __future__ import annotations

import csv
import io
import json
import statistics
import subprocess
import sys
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

import pyarrow as pa
import pyarrow.parquet as pq
from rich import box
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from rugbot.integrations.pumpfun_api import PumpFunApiClient, get_client
from rugbot.utils.logger import get_logger

logger = get_logger(__name__)

DEFAULT_CSV_DELIMITER: Final[str] = ","
DEFAULT_PUMP_INITIAL_MCAP_USD: Final[float] = 5000.0
MS_PER_SECOND: Final[float] = 1000.0
TIME_DISPLAY_THRESHOLD_SECONDS: Final[float] = 300.0
MIN_WIN_ATH_MULTIPLIER: Final[float] = 2.0


def copy_to_clipboard(text: str) -> bool:
    """Copy text to the system clipboard across Windows, macOS, and Linux."""
    clean_text = text.strip()
    if not clean_text:
        return False
    text_bytes = clean_text.encode("utf-8")
    try:
        if sys.platform == "win32":
            subprocess.run(["clip.exe"], input=text_bytes, check=True)
            return True
        if sys.platform == "darwin":
            subprocess.run(["pbcopy"], input=text_bytes, check=True)
            return True
        if sys.platform.startswith("linux"):
            for tool in (
                ["wl-copy"],
                ["xclip", "-selection", "clipboard"],
                ["xsel", "--clipboard", "--input"],
            ):
                try:
                    subprocess.run(tool, input=text_bytes, check=True)
                except FileNotFoundError:
                    continue
                else:
                    return True
    except Exception as exc:
        logger.debug("Failed copying text to clipboard: %s", exc)
    return False


@dataclass(slots=True)
class TokenTradeStatRow:
    """Standardized single token trade or execution record."""

    mint: str
    symbol: str = "UNKNOWN"
    token_name: str = ""
    launch_time: str = ""
    launch_timestamp: float = 0.0
    entry_time: str = ""
    entry_timestamp: float = 0.0
    entry_age_seconds: float = 0.0
    entry_price_sol: float = 0.0
    entry_sol_amount: float = 0.0
    entry_mcap_usd: float = 0.0
    ath_mcap_usd: float = 0.0
    ath_multiplier: float = 1.0
    time_to_peak_seconds: float = 0.0
    confluence_count: int = 1
    cabal_cluster_id: str = ""
    buyer_wallet: str = ""
    exit_time: str = ""
    exit_reason: str = "holding"
    exit_mcap_usd: float = 0.0
    realized_pnl_sol: float = 0.0
    net_roi_pct: float = 0.0
    is_win: bool = False
    status: str = "CLOSED"

    def to_dict(self) -> dict[str, Any]:
        """Convert row to serializable dictionary."""
        return asdict(self)


@dataclass(slots=True)
class StatsSummary:
    """Aggregated portfolio or sample key performance indicators."""

    sample_count: int = 0
    win_count: int = 0
    loss_count: int = 0
    winrate_pct: float = 0.0
    median_ath_mult: float = 0.0
    mean_ath_mult: float = 0.0
    avg_time_to_peak_seconds: float = 0.0
    avg_entry_age_seconds: float = 0.0
    total_entry_sol: float = 0.0
    total_realized_pnl_sol: float = 0.0
    net_roi_pct: float = 0.0
    net_ev_sol_per_trade: float = 0.0
    profit_factor: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        """Convert summary to dictionary."""
        return asdict(self)


@dataclass(slots=True)
class TokenStatsSheet:
    """Comprehensive, reusable multi-format performance sheet."""

    title: str = "TOKEN PERFORMANCE & EXECUTION SHEET"
    rows: list[TokenTradeStatRow] = field(default_factory=list)

    def add_row(self, row: TokenTradeStatRow) -> None:
        """Append a stat row to the sheet."""
        self.rows.append(row)

    def compute_summary(self) -> StatsSummary:
        """Compute aggregated KPIs across all recorded rows."""
        count = len(self.rows)
        if count == 0:
            return StatsSummary()

        wins = sum(1 for r in self.rows if r.is_win)
        losses = count - wins
        winrate = (wins / count) * 100.0

        ath_mults = [r.ath_multiplier for r in self.rows if r.ath_multiplier > 0]
        median_ath = statistics.median(ath_mults) if ath_mults else 1.0
        mean_ath = statistics.mean(ath_mults) if ath_mults else 1.0

        peak_times = [
            r.time_to_peak_seconds for r in self.rows if r.time_to_peak_seconds > 0
        ]
        avg_peak = statistics.mean(peak_times) if peak_times else 0.0

        ages = [r.entry_age_seconds for r in self.rows if r.entry_age_seconds >= 0]
        avg_age = statistics.mean(ages) if ages else 0.0

        total_entry = sum(r.entry_sol_amount for r in self.rows)
        total_pnl = sum(r.realized_pnl_sol for r in self.rows)
        net_roi = (
            (total_pnl / max(0.001, total_entry)) * 100.0 if total_entry > 0 else 0.0
        )
        net_ev = total_pnl / count

        gross_wins = sum(
            r.realized_pnl_sol for r in self.rows if r.realized_pnl_sol > 0
        )
        gross_losses = abs(
            sum(r.realized_pnl_sol for r in self.rows if r.realized_pnl_sol < 0)
        )
        profit_factor = (
            (gross_wins / gross_losses)
            if gross_losses > 0
            else (99.9 if gross_wins > 0 else 1.0)
        )

        return StatsSummary(
            sample_count=count,
            win_count=wins,
            loss_count=losses,
            winrate_pct=round(winrate, 1),
            median_ath_mult=round(median_ath, 2),
            mean_ath_mult=round(mean_ath, 2),
            avg_time_to_peak_seconds=round(avg_peak, 1),
            avg_entry_age_seconds=round(avg_age, 1),
            total_entry_sol=round(total_entry, 4),
            total_realized_pnl_sol=round(total_pnl, 4),
            net_roi_pct=round(net_roi, 1),
            net_ev_sol_per_trade=round(net_ev, 4),
            profit_factor=round(profit_factor, 2),
        )

    def to_table(self) -> str:
        """Render high-density aligned monospace ASCII terminal table."""
        summary = self.compute_summary()
        lines: list[str] = []

        header = (
            f"{'#':<3} | {'Mint':<10} | {'Symbol':<8} | {'Launch Time (UTC)':<19} | "
            f"{'Entry Age':<9} | {'Entry SOL':<9} | {'ATH Mcap':<10} | {'ATH':<6} | "
            f"{'Peak Time':<9} | {'Exit Reason':<16} | {'PnL (SOL)':<10} | {'ROI %':<8}"
        )
        sep = "=" * len(header)
        sub_sep = "-" * len(header)

        lines.append(sep)
        lines.append(f"  {self.title.upper()} (N = {len(self.rows)})")
        lines.append(sep)
        lines.append(header)
        lines.append(sub_sep)

        for idx, r in enumerate(self.rows, start=1):
            m_short = f"{r.mint[:8]}.."
            sym_short = r.symbol[:8]
            l_time = r.launch_time[:19] if r.launch_time else "unknown"
            age_str = (
                f"{r.entry_age_seconds:.1f}s"
                if r.entry_age_seconds < 300
                else f"{r.entry_age_seconds / 60:.1f}m"
            )
            peak_str = (
                f"{r.time_to_peak_seconds:.0f}s"
                if r.time_to_peak_seconds < 300
                else f"{r.time_to_peak_seconds / 60:.1f}m"
            )
            ath_mc_str = f"${r.ath_mcap_usd:,.0f}" if r.ath_mcap_usd > 0 else "N/A"
            ath_m_str = f"{r.ath_multiplier:.1f}x"
            pnl_str = f"{r.realized_pnl_sol:+.4f}"
            roi_str = f"{r.net_roi_pct:+.1f}%"
            exit_r = r.exit_reason[:16]

            lines.append(
                f"{idx:<3} | {m_short:<10} | {sym_short:<8} | {l_time:<19} | "
                f"{age_str:>9} | {r.entry_sol_amount:>7.3f} SOL | {ath_mc_str:>10} | {ath_m_str:>6} | "
                f"{peak_str:>9} | {exit_r:<16} | {pnl_str:>10} | {roi_str:>8}"
            )

        lines.append(sep)
        lines.append(
            f"SUMMARY: N = {summary.sample_count} | Winrate: {summary.winrate_pct:.1f}% ({summary.win_count}/{summary.sample_count}) | "
            f"Median ATH: {summary.median_ath_mult:.1f}x | Avg Peak: {summary.avg_time_to_peak_seconds:.0f}s | "
            f"Net PnL: {summary.total_realized_pnl_sol:+.4f} SOL | Net ROI: {summary.net_roi_pct:+.1f}% | EV: {summary.net_ev_sol_per_trade:+.4f} SOL"
        )
        lines.append(sep)
        return "\n".join(lines)

    def to_csv(self, file_path: str | Path | None = None) -> str:
        """Export sheet rows to standard RFC 4180 CSV string and optionally persist to disk."""
        output = io.StringIO()
        fieldnames = [
            "mint",
            "symbol",
            "token_name",
            "launch_time",
            "launch_timestamp",
            "entry_time",
            "entry_timestamp",
            "entry_age_seconds",
            "entry_sol_amount",
            "entry_price_sol",
            "entry_mcap_usd",
            "ath_mcap_usd",
            "ath_multiplier",
            "time_to_peak_seconds",
            "confluence_count",
            "cabal_cluster_id",
            "buyer_wallet",
            "exit_time",
            "exit_reason",
            "exit_mcap_usd",
            "realized_pnl_sol",
            "net_roi_pct",
            "is_win",
            "status",
        ]
        writer = csv.DictWriter(
            output, fieldnames=fieldnames, delimiter=DEFAULT_CSV_DELIMITER
        )
        writer.writeheader()
        for r in self.rows:
            writer.writerow(r.to_dict())

        csv_content = output.getvalue()
        if file_path:
            p = Path(file_path)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(csv_content, encoding="utf-8")
            logger.info("Saved CSV performance sheet to %s", p)

        return csv_content

    def to_markdown(self) -> str:
        """Export sheet rows to standard GitHub Flavored Markdown table."""
        summary = self.compute_summary()
        lines: list[str] = [
            f"### {self.title}",
            "",
            "| # | Mint | Symbol | Launch Time (UTC) | Entry Age | Entry SOL | ATH Mcap | ATH Mult | Time to Peak | Exit Reason | Realized PnL | Net ROI |",
            "| :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- |",
        ]
        for idx, r in enumerate(self.rows, start=1):
            m_link = f"[{r.mint[:8]}...](https://dexscreener.com/solana/{r.mint})"
            l_time = r.launch_time[:19] if r.launch_time else "unknown"
            lines.append(
                f"| {idx} | {m_link} | {r.symbol} | {l_time} | {r.entry_age_seconds:.1f}s | "
                f"{r.entry_sol_amount:.3f} SOL | ${r.ath_mcap_usd:,.0f} | {r.ath_multiplier:.1f}x | "
                f"{r.time_to_peak_seconds:.0f}s | `{r.exit_reason}` | **{r.realized_pnl_sol:+.4f} SOL** | **{r.net_roi_pct:+.1f}%** |"
            )

        lines.extend(
            [
                "",
                f"> **Summary KPIs**: Sample Size $N = {summary.sample_count}$ | **Winrate: {summary.winrate_pct:.1f}%** | "
                f"Median ATH: **{summary.median_ath_mult:.1f}x** | Avg Peak: **{summary.avg_time_to_peak_seconds:.0f}s** | "
                f"Net PnL: **{summary.total_realized_pnl_sol:+.4f} SOL** | Net ROI: **{summary.net_roi_pct:+.1f}%**",
                "",
            ]
        )
        return "\n".join(lines)

    def to_json(self, indent: int = 2) -> str:
        """Export sheet rows and aggregated summary to JSON string."""
        data = {
            "title": self.title,
            "summary": self.compute_summary().to_dict(),
            "rows": [r.to_dict() for r in self.rows],
        }
        return json.dumps(data, indent=indent)

    def to_parquet(self, file_path: str | Path) -> str:
        """Export sheet rows to Apache Parquet format (.parquet) using PyArrow.

        Args:
            file_path: Destination path on disk.

        Returns:
            Resolved absolute path to the saved parquet file.
        """
        p = Path(file_path)
        p.parent.mkdir(parents=True, exist_ok=True)

        data = {
            "mint": [r.mint for r in self.rows],
            "symbol": [r.symbol for r in self.rows],
            "token_name": [r.token_name for r in self.rows],
            "launch_time": [r.launch_time for r in self.rows],
            "launch_timestamp": [r.launch_timestamp for r in self.rows],
            "entry_time": [r.entry_time for r in self.rows],
            "entry_timestamp": [r.entry_timestamp for r in self.rows],
            "entry_age_seconds": [r.entry_age_seconds for r in self.rows],
            "entry_price_sol": [r.entry_price_sol for r in self.rows],
            "entry_sol_amount": [r.entry_sol_amount for r in self.rows],
            "entry_mcap_usd": [r.entry_mcap_usd for r in self.rows],
            "ath_mcap_usd": [r.ath_mcap_usd for r in self.rows],
            "ath_multiplier": [r.ath_multiplier for r in self.rows],
            "time_to_peak_seconds": [r.time_to_peak_seconds for r in self.rows],
            "confluence_count": [r.confluence_count for r in self.rows],
            "cabal_cluster_id": [r.cabal_cluster_id for r in self.rows],
            "buyer_wallet": [r.buyer_wallet for r in self.rows],
            "exit_time": [r.exit_time for r in self.rows],
            "exit_reason": [r.exit_reason for r in self.rows],
            "exit_mcap_usd": [r.exit_mcap_usd for r in self.rows],
            "realized_pnl_sol": [r.realized_pnl_sol for r in self.rows],
            "net_roi_pct": [r.net_roi_pct for r in self.rows],
            "is_win": [r.is_win for r in self.rows],
            "status": [r.status for r in self.rows],
        }
        table = pa.Table.from_pydict(data)
        pq.write_table(table, p, compression="zstd")
        logger.info("Saved Parquet performance sheet to %s", p)
        return str(p)

    @classmethod
    def from_parquet(
        cls, file_path: str | Path, title: str | None = None
    ) -> TokenStatsSheet:
        """Construct a TokenStatsSheet by reading an Apache Parquet (.parquet) file.

        Args:
            file_path: Path to the parquet file.
            title: Optional custom sheet title.

        Returns:
            Populated TokenStatsSheet.
        """
        p = Path(file_path)
        if not p.exists():
            raise FileNotFoundError(f"Parquet file not found: {p}")

        table = pq.read_table(p)
        d = table.to_pydict()
        sheet_title = title or f"PARQUET SHEET: {p.stem.upper()}"
        sheet = cls(title=sheet_title)

        num_rows = len(table)
        for i in range(num_rows):
            row = TokenTradeStatRow(
                mint=str(d["mint"][i]),
                symbol=str(d.get("symbol", [""])[i] or "UNKNOWN"),
                token_name=str(d.get("token_name", [""])[i] or ""),
                launch_time=str(d.get("launch_time", [""])[i] or ""),
                launch_timestamp=float(d.get("launch_timestamp", [0.0])[i] or 0.0),
                entry_time=str(d.get("entry_time", [""])[i] or ""),
                entry_timestamp=float(d.get("entry_timestamp", [0.0])[i] or 0.0),
                entry_age_seconds=float(d.get("entry_age_seconds", [0.0])[i] or 0.0),
                entry_price_sol=float(d.get("entry_price_sol", [0.0])[i] or 0.0),
                entry_sol_amount=float(d.get("entry_sol_amount", [0.0])[i] or 0.0),
                entry_mcap_usd=float(d.get("entry_mcap_usd", [0.0])[i] or 0.0),
                ath_mcap_usd=float(d.get("ath_mcap_usd", [0.0])[i] or 0.0),
                ath_multiplier=float(d.get("ath_multiplier", [1.0])[i] or 1.0),
                time_to_peak_seconds=float(
                    d.get("time_to_peak_seconds", [0.0])[i] or 0.0
                ),
                confluence_count=int(d.get("confluence_count", [1])[i] or 1),
                cabal_cluster_id=str(d.get("cabal_cluster_id", [""])[i] or ""),
                buyer_wallet=str(d.get("buyer_wallet", [""])[i] or ""),
                exit_time=str(d.get("exit_time", [""])[i] or ""),
                exit_reason=str(d.get("exit_reason", [""])[i] or ""),
                exit_mcap_usd=float(d.get("exit_mcap_usd", [0.0])[i] or 0.0),
                realized_pnl_sol=float(d.get("realized_pnl_sol", [0.0])[i] or 0.0),
                net_roi_pct=float(d.get("net_roi_pct", [0.0])[i] or 0.0),
                is_win=bool(d.get("is_win", [False])[i]),
                status=str(d.get("status", ["CLOSED"])[i] or "CLOSED"),
            )
            sheet.add_row(row)

        return sheet

    def to_rich_table(self, *, full_mint: bool = False) -> Table:
        """Create a styled rich Table representation of the performance sheet."""
        table = Table(
            title=f"[bold green]{self.title}[/bold green]",
            box=box.ROUNDED,
            header_style="bold cyan",
            show_header=True,
        )

        table.add_column("#", justify="right", style="dim", width=3)
        if full_mint:
            table.add_column("Mint (Copyable)", style="bold bright_cyan", no_wrap=True)
        else:
            table.add_column("Mint", style="bold bright_cyan", width=12)
        table.add_column("Symbol", style="bold yellow")
        table.add_column("Launch Time (UTC)", style="white")
        table.add_column("Entry Age", justify="right", style="magenta")
        table.add_column("Entry SOL", justify="right")
        table.add_column("ATH Mcap", justify="right", style="bold green")
        table.add_column("ATH", justify="right", style="bold green")
        table.add_column("Peak Time", justify="right")
        table.add_column("Exit Reason", style="dim")
        table.add_column("PnL (SOL)", justify="right")
        table.add_column("ROI %", justify="right")

        for idx, r in enumerate(self.rows, start=1):
            mint_display = r.mint if full_mint else f"{r.mint[:8]}..."
            l_time = r.launch_time[:19] if r.launch_time else "unknown"
            age_str = (
                f"{r.entry_age_seconds:.1f}s"
                if r.entry_age_seconds < 300
                else f"{r.entry_age_seconds / 60:.1f}m"
            )
            peak_str = (
                f"{r.time_to_peak_seconds:.0f}s"
                if r.time_to_peak_seconds < 300
                else f"{r.time_to_peak_seconds / 60:.1f}m"
            )
            ath_mc_str = f"${r.ath_mcap_usd:,.0f}" if r.ath_mcap_usd > 0 else "N/A"
            ath_m_str = (
                f"[green]{r.ath_multiplier:.1f}x[/green]"
                if r.ath_multiplier >= 2.0
                else f"{r.ath_multiplier:.1f}x"
            )
            pnl_style = "green" if r.realized_pnl_sol >= 0 else "red"
            pnl_str = f"[{pnl_style}]{r.realized_pnl_sol:+.4f}[/{pnl_style}]"
            roi_style = "bold green" if r.net_roi_pct >= 0 else "bold red"
            roi_str = f"[{roi_style}]{r.net_roi_pct:+.1f}%[/{roi_style}]"

            table.add_row(
                str(idx),
                mint_display,
                r.symbol,
                l_time,
                age_str,
                f"{r.entry_sol_amount:.3f} SOL",
                ath_mc_str,
                ath_m_str,
                peak_str,
                r.exit_reason[:16],
                pnl_str,
                roi_str,
            )

        return table

    def print_rich(
        self,
        *,
        full_mint: bool = False,
        show_mints_panel: bool = True,
        console: Console | None = None,
    ) -> None:
        """Render and print the performance sheet and copyable mints panel with Rich."""
        if sys.platform == "win32":
            try:
                sys.stdout.reconfigure(encoding="utf-8")
            except Exception:
                logger.debug("Failed setting stdout encoding to utf-8")

        rich_console = console or Console()
        table = self.to_rich_table(full_mint=full_mint)
        rich_console.print(table)

        # 1. Summary KPI Panel
        summary = self.compute_summary()
        pnl_col = "green" if summary.total_realized_pnl_sol >= 0 else "red"
        roi_col = "bold green" if summary.net_roi_pct >= 0 else "bold red"
        ev_col = "bold green" if summary.net_ev_sol_per_trade >= 0 else "bold red"

        summary_text = (
            f"[bold white]Sample Size:[/bold white] {summary.sample_count}   "
            f"[bold white]Winrate:[/bold white] [bold green]{summary.winrate_pct:.1f}%[/bold green] ({summary.win_count}/{summary.sample_count})   "
            f"[bold white]Median ATH:[/bold white] [yellow]{summary.median_ath_mult:.1f}x[/yellow]   "
            f"[bold white]Avg Time-to-Peak:[/bold white] {summary.avg_time_to_peak_seconds:.0f}s   "
            f"[bold white]Net PnL:[/bold white] [{pnl_col}]{summary.total_realized_pnl_sol:+.4f} SOL[/]   "
            f"[bold white]Net ROI:[/bold white] [{roi_col}]{summary.net_roi_pct:+.1f}%[/]   "
            f"[bold white]EV/Trade:[/bold white] [{ev_col}]{summary.net_ev_sol_per_trade:+.4f} SOL[/]"
        )
        rich_console.print(
            Panel(
                summary_text,
                title="[bold green]📊 Aggregate Summary KPIs[/bold green]",
                border_style="green",
            )
        )

        # 2. Copyable Mints Panel
        if show_mints_panel and self.rows:
            mint_lines = []
            for idx, r in enumerate(self.rows, start=1):
                sym = f"{r.symbol:<8}"
                pnl_c = "green" if r.realized_pnl_sol >= 0 else "red"
                mint_lines.append(
                    f"[dim]{idx:>2}.[/dim] [bold yellow]{sym}[/bold yellow] : [bold bright_cyan]{r.mint}[/bold bright_cyan] "
                    f"(ATH: [green]{r.ath_multiplier:.1f}x[/green], PnL: [{pnl_c}]{r.realized_pnl_sol:+.4f} SOL[/{pnl_c}])"
                )
            panel_content = "\n".join(mint_lines)
            rich_console.print(
                Panel(
                    panel_content,
                    title="📋 [bold white]Copyable Mints (Double-click address to select & copy)[/bold white]",
                    subtitle="[dim]Tip: pass '--copy <#>' to copy a mint directly to system clipboard[/dim]",
                    border_style="cyan",
                )
            )

    def get_mints(self) -> list[str]:
        """Return list of token mint addresses."""
        return [r.mint for r in self.rows]

    @classmethod
    def from_executions(
        cls,
        executions: Sequence[dict[str, Any]],
        client: PumpFunApiClient | None = None,
    ) -> TokenStatsSheet:
        """Construct a TokenStatsSheet enriched with token launch metadata from execution records."""
        api = client or get_client()
        sheet = cls(title="BOT EXECUTION PERFORMANCE SHEET")

        for ex in executions:
            mint = str(ex.get("mint") or "")
            if not mint:
                continue

            # Fetch token launch and ATH metadata
            symbol = "UNKNOWN"
            name = ""
            created_ts = 0.0
            ath_mcap = 0.0
            ath_ts = 0.0
            try:
                meta = api.fetch_token(mint)
                if meta:
                    symbol = str(meta.get("symbol") or "UNKNOWN")
                    name = str(meta.get("name") or "")
                    c_ms = float(meta.get("created_timestamp") or 0.0)
                    created_ts = c_ms / MS_PER_SECOND if c_ms > 0 else 0.0
                    ath_mcap = float(
                        meta.get("ath_market_cap") or meta.get("usd_market_cap") or 0.0
                    )
                    a_ms = float(meta.get("ath_market_cap_timestamp") or 0.0)
                    ath_ts = a_ms / MS_PER_SECOND if a_ms > 0 else 0.0
            except Exception as exc:
                logger.debug("Failed fetching token meta for %s: %s", mint, exc)

            entry_pnl = float(ex.get("realized_pnl_sol") or 0.0)
            entry_sol = float(ex.get("entry_sol_amount") or 0.0)
            roi_pct = float(ex.get("roi_pct") or 0.0)
            high_price = float(ex.get("high_price_seen") or 0.0)
            entry_price = float(ex.get("entry_price_sol") or 0.00003)
            ath_mult = (
                (high_price / max(0.000001, entry_price))
                if high_price > 0
                else (ath_mcap / DEFAULT_PUMP_INITIAL_MCAP_USD if ath_mcap > 0 else 1.0)
            )

            opened_str = str(ex.get("opened_at") or "")
            entry_ts = 0.0
            if opened_str:
                try:
                    entry_ts = datetime.fromisoformat(opened_str).timestamp()
                except Exception:
                    entry_ts = 0.0

            entry_age = (
                max(0.0, entry_ts - created_ts)
                if created_ts > 0 and entry_ts > 0
                else 0.0
            )
            time_to_peak = (
                max(0.0, ath_ts - created_ts) if ath_ts > 0 and created_ts > 0 else 0.0
            )

            launch_time_str = (
                datetime.fromtimestamp(created_ts, UTC).strftime(
                    "%Y-%m-%d %H:%M:%S UTC"
                )
                if created_ts > 0
                else "unknown"
            )

            is_closed = bool(ex.get("is_closed"))
            status = "CLOSED" if is_closed else "OPEN"
            is_win = entry_pnl > 0

            row = TokenTradeStatRow(
                mint=mint,
                symbol=symbol,
                token_name=name,
                launch_time=launch_time_str,
                launch_timestamp=created_ts,
                entry_time=opened_str,
                entry_timestamp=entry_ts,
                entry_age_seconds=round(entry_age, 1),
                entry_price_sol=entry_price,
                entry_sol_amount=entry_sol,
                entry_mcap_usd=DEFAULT_PUMP_INITIAL_MCAP_USD,
                ath_mcap_usd=ath_mcap,
                ath_multiplier=round(ath_mult, 2),
                time_to_peak_seconds=round(time_to_peak, 1),
                cabal_cluster_id=str(ex.get("cabal_cluster_id") or ""),
                buyer_wallet=str(ex.get("wallet_address") or ""),
                exit_time=str(ex.get("closed_at") or ""),
                exit_reason=str(ex.get("exit_reason") or "holding"),
                realized_pnl_sol=entry_pnl,
                net_roi_pct=roi_pct,
                is_win=is_win,
                status=status,
            )
            sheet.add_row(row)

        return sheet

    @classmethod
    def from_cabal_clusters(
        cls,
        clusters: Sequence[Any],
        client: PumpFunApiClient | None = None,
    ) -> TokenStatsSheet:
        """Construct a TokenStatsSheet enriched with historical token launch & ATH metrics from cabal clusters."""
        api = client or get_client()
        sheet = cls(title="CABAL HISTORICAL LAUNCHES SHEET")
        seen_mints: set[str] = set()

        for c in clusters:
            cid = getattr(c, "cluster_id", "cabal")
            typical_buy = float(getattr(c, "typical_buy_sol", 0.25))
            funder = getattr(c, "funder", "")
            tokens = getattr(c, "winner_tokens", set()) or {
                t.get("mint") for t in getattr(c, "tokens", []) if t.get("mint")
            }

            for mint in tokens:
                if not mint or mint in seen_mints:
                    continue
                seen_mints.add(mint)

                symbol = "UNKNOWN"
                name = ""
                created_ts = 0.0
                ath_mcap = 0.0
                ath_ts = 0.0
                try:
                    meta = api.fetch_token(mint)
                    if meta:
                        symbol = str(meta.get("symbol") or "UNKNOWN")
                        name = str(meta.get("name") or "")
                        c_ms = float(meta.get("created_timestamp") or 0.0)
                        created_ts = c_ms / MS_PER_SECOND if c_ms > 0 else 0.0
                        ath_mcap = float(
                            meta.get("ath_market_cap")
                            or meta.get("usd_market_cap")
                            or 0.0
                        )
                        a_ms = float(meta.get("ath_market_cap_timestamp") or 0.0)
                        ath_ts = a_ms / MS_PER_SECOND if a_ms > 0 else 0.0
                except Exception as exc:
                    logger.debug("Failed fetching token meta for %s: %s", mint, exc)

                launch_time_str = (
                    datetime.fromtimestamp(created_ts, UTC).strftime(
                        "%Y-%m-%d %H:%M:%S UTC"
                    )
                    if created_ts > 0
                    else "unknown"
                )

                time_to_peak = (
                    max(0.0, ath_ts - created_ts)
                    if ath_ts > 0 and created_ts > 0
                    else getattr(c, "avg_time_to_peak_sec", 60.0)
                )
                ath_mult = (
                    (ath_mcap / DEFAULT_PUMP_INITIAL_MCAP_USD)
                    if ath_mcap > 0
                    else getattr(c, "median_ath", 5.0)
                )

                # Simulated net execution proceeds under profitable preset
                entry_sol = typical_buy * 0.50
                is_win = ath_mult >= 2.0
                if is_win:
                    # 50% @ 2.0x, 25% @ 5.0x, 25% @ trailing stop (4.25x)
                    exit_reason = "take_profit_ladder"
                    gross_ret = (
                        (entry_sol * 0.50 * 2.0)
                        + (entry_sol * 0.25 * min(ath_mult, 5.0))
                        + (entry_sol * 0.25 * min(ath_mult * 0.85, 4.25))
                    )
                    realized_pnl = gross_ret - entry_sol - (entry_sol * 0.025 + 0.005)
                else:
                    exit_reason = "adverse_exit"
                    realized_pnl = -entry_sol * 0.85

                roi_pct = (realized_pnl / max(0.001, entry_sol)) * 100.0

                row = TokenTradeStatRow(
                    mint=mint,
                    symbol=symbol,
                    token_name=name,
                    launch_time=launch_time_str,
                    launch_timestamp=created_ts,
                    entry_time=launch_time_str,
                    entry_timestamp=created_ts + 2.5,
                    entry_age_seconds=2.5,  # Typical early buyer block-0/block-1 entry
                    entry_price_sol=0.00003,
                    entry_sol_amount=round(entry_sol, 4),
                    entry_mcap_usd=DEFAULT_PUMP_INITIAL_MCAP_USD,
                    ath_mcap_usd=round(ath_mcap, 0),
                    ath_multiplier=round(ath_mult, 2),
                    time_to_peak_seconds=round(time_to_peak, 1),
                    confluence_count=len(getattr(c, "wallets", [])),
                    cabal_cluster_id=cid,
                    buyer_wallet=funder,
                    exit_time="closed",
                    exit_reason=exit_reason,
                    realized_pnl_sol=round(realized_pnl, 4),
                    net_roi_pct=round(roi_pct, 1),
                    is_win=is_win,
                    status="CLOSED",
                )
                sheet.add_row(row)

        return sheet


__all__ = [
    "DEFAULT_CSV_DELIMITER",
    "DEFAULT_PUMP_INITIAL_MCAP_USD",
    "StatsSummary",
    "TokenStatsSheet",
    "TokenTradeStatRow",
    "copy_to_clipboard",
]
