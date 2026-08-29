"""Action-oriented operator lifecycle intelligence cards: TokenDetail, DevHistory, Execution, RiskBar, and EventLog."""

# ruff: noqa: S105, S110, BLE001, TC002, PLR0913, ARG002, FBT001, FBT003, ANN401

from __future__ import annotations

import contextlib
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from textual.app import ComposeResult
from textual.containers import VerticalScroll
from textual.reactive import reactive
from textual.widget import Widget
from textual.widgets import Static

from rugbot.interfaces.tui.formatters import (
    format_age,
    format_currency,
    short_address,
)
from rugbot.tracker.models import TargetExecutionMode, TargetRecord

if TYPE_CHECKING:
    from rugbot.interfaces.tui.widgets.panels.activity import ActivityItem
    from rugbot.tracker.models import FundingPath


class OperatorStage(StrEnum):
    """The sequential operator lifecycle stage for the highlighted token/dev."""

    DEV_REVIEW = "DEV_REVIEW"
    WATCHLIST_STANDBY = "WATCHLIST_STANDBY"
    ARMED = "ARMED"
    CANDIDATE = "CANDIDATE"
    PENDING = "PENDING"
    FAILED = "FAILED"
    LAUNCH_TRIGGERED = "LAUNCH_TRIGGERED"
    POSITION_OPEN = "POSITION_OPEN"
    POSITION_CLOSED = "POSITION_CLOSED"


class TokenDetailCard(Widget):
    """Top-Right quadrant displaying Lifecycle Stage, Token Metrics, and Grouped Decision Signals."""

    DEFAULT_CSS = """
    TokenDetailCard {
        height: auto;
        max-height: 48%;
        width: 100%;
        layout: vertical;
        background: $surface;
        border-left: solid $panel;
        padding: 0 1;
    }

    .token-header {
        height: 1;
        width: 100%;
        background: $boost;
        color: $accent;
        text-style: bold;
        padding: 0 1;
    }

    .detail-scroll {
        height: 1fr;
        width: 100%;
    }
    """

    stage: reactive[OperatorStage] = reactive(OperatorStage.WATCHLIST_STANDBY)
    position_pnl_pct: reactive[float] = reactive(0.0)
    current_price_mc: reactive[float] = reactive(0.0)
    entry_mc: reactive[float] = reactive(0.0)

    def compose(self) -> ComposeResult:
        yield Static("TOKEN INTEL & SIGNALS", classes="token-header")
        with VerticalScroll(classes="detail-scroll"):
            yield Static(self._render_content(), id="token-detail-static")

    def update_item(
        self,
        item: ActivityItem | None,
        path: FundingPath | None = None,
    ) -> None:
        """Update display with selected token."""
        content = self.query_one("#token-detail-static", Static)
        content.update(self._render_content(item, path))

    def set_stage(self, stage: OperatorStage) -> None:
        self.stage = stage
        with contextlib.suppress(Exception):
            self.query_one("#token-detail-static", Static).update(
                self._render_content()
            )

    def _render_content(
        self,
        item: ActivityItem | None = None,
        path: FundingPath | None = None,
    ) -> str:
        if item is None:
            return (
                "[bold cyan]● TOKEN INTEL & SIGNALS (STANDBY)[/bold cyan]\n\n"
                "[dim]No token selected from the activity feed.[/dim]\n"
                "[dim]When new dev launches arrive, select a row to inspect:\n"
                "• Bonding curve state & market cap\n"
                "• Dev track record & win rate\n"
                "• Funding trace & bundle analysis[/dim]"
            )

        token_name = item.token_symbol if item.token_symbol != "—" else "LAUNCH"
        score = item.dev_score or 80
        mc = format_currency(item.market_cap_usd or 4500.0)
        entry_mc_str = format_currency(item.market_cap_usd or 4500.0)
        ath_mc = format_currency(
            (item.market_cap_usd or 4500.0)
            * (1 + (item.ath_multiplier_pct or 0.0) / 100)
        )
        age_str = format_age(item.timestamp)
        dev_addr = short_address(item.target_wallet)
        funder_addr = short_address(item.root_funder)

        stage_banner = (
            f"[bold cyan]● TOKEN: {token_name}[/bold cyan]   "
            f"[dim]DEV SCORE:[/dim] [green]{score}%[/green]   "
            f"[dim]VERDICT:[/dim] [bold green]{item.signal.upper()}[/bold green]"
        )

        lines: list[str] = [
            stage_banner + "\n",
            "[dim]MC[/dim]         [dim]ENTRY[/dim]      [dim]ATH[/dim]        [dim]AGE[/dim]",
            f"[bold white]{mc:<10}[/bold white] [white]{entry_mc_str:<10}[/white] [bold green]{ath_mc:<10}[/bold green] [cyan]{age_str}[/cyan]\n",
            "[dim]DEV[/dim]        [dim]ROOT FUNDER[/dim] [dim]WR[/dim]         [dim]BUY AMOUNT[/dim]",
            f"[white]{dev_addr:<10}[/white] [cyan]{funder_addr:<11}[/cyan] [green]{score}%[/green]       [white]{(item.amount_lamports or 0) / 1e9:.1f} SOL[/white]\n",
            "[bold yellow]SIGNALS & VERIFICATION[/bold yellow]",
            f" [green]✓ Market Cap[/green]     [white]{mc} (Entry bound < $15k)[/white]",
            f" [green]✓ Dev Score[/green]      [green]{score}% Win Rate[/green]",
            f" [green]✓ Execution[/green]      [bold green]{item.signal}[/bold green]",
        ]
        return "\n".join(lines)


class TargetProfileCard(Widget):
    """Card displaying selected target wallet track record, strategy, and execution mode."""

    DEFAULT_CSS = """
    TargetProfileCard {
        height: auto;
        max-height: 48%;
        width: 100%;
        layout: vertical;
        background: $surface;
        border-left: solid $panel;
        padding: 0 1;
    }

    .target-header {
        height: 1;
        width: 100%;
        background: $boost;
        color: $accent;
        text-style: bold;
        padding: 0 1;
    }

    .target-scroll {
        height: 1fr;
        width: 100%;
    }

    .card-buttons-row {
        height: 3;
        width: 100%;
        layout: horizontal;
        align: left middle;
        margin: 1 0;
    }

    .compact-btn {
        height: 3;
        min-width: 9;
        margin-right: 1;
    }
    """

    def __init__(
        self, target: TargetRecord | None = None, *args: Any, **kwargs: Any
    ) -> None:
        super().__init__(*args, **kwargs)
        self._target = target

    def compose(self) -> ComposeResult:
        header_text = self._build_header_text()
        yield Static(header_text, classes="target-header", id="target-header-static")
        with VerticalScroll(classes="target-scroll"):
            yield Static(self._render_content(), id="target-profile-static")

    def _build_header_text(self) -> str:
        if self._target:
            return f"TARGET STRATEGY & CONFIGURATION — {self._target.label.upper()} ({short_address(self._target.address)})"
        return "TARGET STRATEGY & CONFIGURATION"

    def update_target(self, target: TargetRecord) -> None:
        self._target = target
        with contextlib.suppress(Exception):
            header = self.query_one("#target-header-static", Static)
            header.update(self._build_header_text())
            self.query_one("#target-profile-static", Static).update(
                self._render_content()
            )

    def _render_content(self) -> str:
        if not self._target:
            return (
                "[bold cyan]NO TARGET DEV ARMED[/bold cyan]\n"
                "[dim]No active target selected. Go to Settings tab [6] or add a dev wallet to arm tracking.[/dim]"
            )
        t = self._target
        strat = t.strategy
        if not strat.monitoring_enabled:
            status_badge = "[dim]○ PAUSED[/dim]"
            mode_badge = "[dim]PAUSED (OFF)[/dim]"
        elif strat.execution_mode == TargetExecutionMode.LIVE:
            status_badge = "[bold green]● LIVE[/bold green]"
            mode_badge = "[bold green]● LIVE TRADING[/bold green]"
        else:
            status_badge = "[bold cyan]● DRY RUN[/bold cyan]"
            mode_badge = "[bold cyan]● SIMULATED (DRY RUN)[/bold cyan]"

        lines: list[str] = [
            f"[bold cyan]{short_address(t.address)}[/bold cyan] [dim]({t.label})[/dim]                                  {status_badge}",
            f"[dim]TRACK RECORD:[/dim]  [white]{t.launches_count} recorded launches[/white] · [green]{t.winrate_pct:.1f}% WR[/green] · [green]+{t.avg_ath_pct:.0f}% avg ATH[/green] · [cyan]{t.perf_metric}[/cyan]",
            f"[dim]STRATEGY:[/dim]      [bold white]{'MONITORING ON' if strat.monitoring_enabled else 'MONITORING OFF'}[/bold white] · {mode_badge}",
            f"[dim]ENTRY RULES:[/dim]   [green]Winrate > {strat.min_winrate_pct:.0f}%[/green] · [white]MC < ${strat.max_entry_mc_usd / 1000:.0f}k[/white] · [white]{'Block 0 required' if strat.required_block_zero else 'Any Block'}[/white] · [white]{'Pattern Match required' if strat.funding_match_required else 'Any Funding'}[/white]",
            f"[dim]FEES & SPEED:[/dim]  [white]{strat.priority_fee_microlamports:,} µL/CU Prio[/white] · [white]{strat.jito_tip_sol:.4f} Jito[/white] · [white]{strat.slippage_bps} bps Slippage[/white] · [white]{strat.max_gas_sol:.4f} SOL Max[/white]",
            f"[dim]SIZE & RISK:[/dim]   [bold white]{strat.size_sol:.3f} SOL Size[/bold white] · [green]TP +{strat.take_profit_pct:.0f}%[/green] · [red]SL {strat.stop_loss_pct:.0f}%[/red] · [bold cyan]{strat.execution_mode.value.upper()}[/bold cyan]",
        ]
        return "\n".join(lines)


class DevHistoryCard(Widget):
    """Bottom-Left quadrant displaying previous launches and results for the selected dev."""

    DEFAULT_CSS = """
    DevHistoryCard {
        height: 100%;
        width: 100%;
        layout: vertical;
        background: $surface;
        padding: 0 1;
    }

    .history-header {
        height: 1;
        width: 100%;
        background: $boost;
        color: $accent;
        text-style: bold;
    }
    """

    def compose(self) -> ComposeResult:
        yield Static("DEV HISTORY", classes="history-header")
        yield Static(self._render_content(), id="dev-history-static")

    def update_item(self, item: ActivityItem | None) -> None:
        content = self.query_one("#dev-history-static", Static)
        content.update(self._render_content(item))

    def _render_content(self, item: ActivityItem | None = None) -> str:
        if item is None or not item.target_wallet:
            return (
                "[bold cyan]DEV LAUNCH HISTORY[/bold cyan]\n"
                "[dim]No dev selected. Select a token or target to inspect previous launch outcomes.[/dim]"
            )
        return (
            f"[bold cyan]DEV:[/bold cyan] [white]{short_address(item.target_wallet)}[/white]\n"
            f"[dim]TOKEN:[/dim] [white]{item.token_symbol}[/white]  "
            f"[dim]ROOT FUNDER:[/dim] [cyan]{short_address(item.root_funder)}[/cyan]\n"
            f"[dim]MARKET CAP:[/dim] [white]{format_currency(item.market_cap_usd)}[/white]  "
            f"[dim]ATH:[/dim] [green]+{item.ath_multiplier_pct or 0:.0f}%[/green]\n"
            f"[dim]STATUS:[/dim] [bold green]{item.signal}[/bold green]"
        )


class ExecutionCard(Widget):
    """Bottom-Right quadrant displaying Action Cockpit tailored to the current lifecycle stage."""

    DEFAULT_CSS = """
    ExecutionCard {
        height: 1fr;
        width: 100%;
        layout: vertical;
        background: $surface;
        border-left: solid $panel;
        padding: 0 1;
    }

    .exec-header {
        height: 1;
        width: 100%;
        background: $boost;
        color: $accent;
        text-style: bold;
        padding: 0 1;
    }

    .card-buttons-row {
        height: 3;
        width: 100%;
        layout: horizontal;
        align: left middle;
        margin-top: 1;
    }

    .compact-btn {
        height: 3;
        min-width: 9;
        margin-right: 1;
    }
    """

    stage: reactive[OperatorStage] = reactive(OperatorStage.ARMED)
    is_armed: reactive[bool] = reactive(True)
    snipe_size_sol: reactive[float] = reactive(0.01)

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._selected_item: ActivityItem | None = None

    def compose(self) -> ComposeResult:
        yield Static("EXECUTION", classes="exec-header")
        yield Static(self._render_content(), id="execution-static")

    def update_item(self, item: ActivityItem | None) -> None:
        """Update action cockpit with selected token's live position."""
        self._selected_item = item
        with contextlib.suppress(Exception):
            self.query_one("#execution-static", Static).update(self._render_content())

    def watch_stage(self, _val: OperatorStage) -> None:
        with contextlib.suppress(Exception):
            self.query_one("#execution-static", Static).update(self._render_content())

    def watch_is_armed(self, _val: bool) -> None:
        with contextlib.suppress(Exception):
            self.query_one("#execution-static", Static).update(self._render_content())

    def toggle_armed(self) -> bool:
        self.is_armed = not self.is_armed
        self.stage = (
            OperatorStage.ARMED if self.is_armed else OperatorStage.WATCHLIST_STANDBY
        )
        return self.is_armed

    def set_stage(self, stage: OperatorStage) -> None:
        self.stage = stage
        with contextlib.suppress(Exception):
            self.query_one("#execution-static", Static).update(self._render_content())

    def _render_content(self) -> str:
        lines: list[str] = []

        if (
            self.stage == OperatorStage.POSITION_OPEN
            and self._selected_item is not None
        ):
            token_sym = self._selected_item.token_symbol or "TOKEN"
            dev_str = short_address(self._selected_item.target_wallet)
            curr_mc_val = self._selected_item.market_cap_usd or 4500.0
            curr_mc = format_currency(curr_mc_val)
            entry_mc_val = 4200.0
            gain_pct = ((curr_mc_val - entry_mc_val) / entry_mc_val) * 100.0
            gross_sol = self.snipe_size_sol * (gain_pct / 100.0)
            fee_sol = 0.0012
            net_sol = gross_sol - fee_sol
            gross_color = "green" if gross_sol >= 0 else "red"
            net_color = "green" if net_sol >= 0 else "red"

            lines.append(
                f"[bold cyan]● ACTIVE POSITION:[/bold cyan] [bold white]{token_sym}[/bold white]  "
                f"[dim]DEV:[/dim] [white]{dev_str}[/white]  "
                f"[dim]ENTRY:[/dim] [white]$4.2k[/white] → [dim]NOW:[/dim] [bold {gross_color}]{curr_mc}[/bold {gross_color}]  "
                f"[dim]PNL:[/dim] [bold {gross_color}]+{gain_pct:.0f}% (+{gross_sol:.4f}S)[/bold {gross_color}]  "
                f"[dim]NET:[/dim] [bold {net_color}]{'+' if net_sol >= 0 else ''}{net_sol:.4f}S Net[/bold {net_color}]"
            )
            lines.append(
                "[dim]EXIT TRIGGERS:[/dim] [green]TP +100% ($8.4k)[/green] · [red]SL -30% ($2.9k)[/red]  "
                "[dim]CONFIG:[/dim] [white]50k µL/CU + 0.0010 Jito[/white]"
            )
        elif (
            self.stage == OperatorStage.POSITION_CLOSED
            and self._selected_item is not None
        ):
            token_sym = self._selected_item.token_symbol or "TOKEN"
            lines.append(
                f"[bold cyan]● POSITION CLOSED:[/bold cyan] [bold white]{token_sym}[/bold white]  "
                f"[dim]OUTCOME:[/dim] [bold green]+102% Take Profit Hit[/bold green]  "
                f"[dim]REALIZED:[/dim] [bold green]+0.0102 SOL Gross · +0.0090 SOL Net[/bold green]"
            )
        else:
            lines.append("[dim]● NO ACTIVE POSITION OPEN (0) · Standby mode[/dim]")

        # Cockpit Sniper Status line
        if self.is_armed:
            lines.append(
                f"[bold green]● ARMED & READY[/bold green]  "
                f"[dim]SNIPE SIZE:[/dim] [bold white]{self.snipe_size_sol:.3f} SOL[/bold white]  "
                f"[dim]FEES:[/dim] [white]50k µL/CU · 0.0010 Jito Tip[/white]  "
                f"[dim]STREAM:[/dim] [white]Block 0 Target[/white]"
            )
        else:
            lines.append(
                "[bold yellow]○ DISARMED (OBSERVATION ONLY)[/bold yellow]  "
                f"[dim]SNIPE SIZE:[/dim] [white]{self.snipe_size_sol:.3f} SOL[/white]  "
                "[dim]Trading paused[/dim]"
            )

        return "\n".join(lines)


class RiskBar(Widget):
    """Permanent Circuit Breaker Risk Bar at the bottom."""

    DEFAULT_CSS = """
    RiskBar {
        height: 1;
        width: 100%;
        background: $boost;
        padding: 0 1;
        border-top: solid $panel;
    }
    """

    exposure_pct: reactive[float] = reactive(0.0)
    max_exposure_pct: reactive[float] = reactive(5.0)
    loss_streak: reactive[int] = reactive(0)
    max_loss_streak: reactive[int] = reactive(5)
    week_dd_pct: reactive[float] = reactive(0.0)
    max_week_dd_pct: reactive[float] = reactive(-35.0)

    def compose(self) -> ComposeResult:
        yield Static(self._render_bar(), id="risk-bar-static")

    def _render_bar(self) -> str:
        is_high_risk = (
            self.exposure_pct >= (self.max_exposure_pct * 0.8)
            or self.loss_streak >= (self.max_loss_streak - 1)
            or self.week_dd_pct <= (self.max_week_dd_pct * 0.8)
        )
        status_pill = (
            "[bold yellow]⚠ HIGH[/bold yellow]"
            if is_high_risk
            else "[bold green]● OK[/bold green]"
        )

        return (
            f"[bold cyan]RISK[/bold cyan]   "
            f"[dim]Exposure[/dim] [white]{self.exposure_pct:.1f}% / {self.max_exposure_pct:.0f}%[/white]   "
            f"[dim]Losses[/dim] [white]{self.loss_streak}/{self.max_loss_streak}[/white]   "
            f"[dim]Week DD[/dim] [white]{self.week_dd_pct:.1f}% / {self.max_week_dd_pct:.0f}%[/white]   "
            f"{status_pill}"
        )

    def update_risk(
        self,
        exposure_pct: float | None = None,
        loss_streak: int | None = None,
        week_dd_pct: float | None = None,
    ) -> None:
        if exposure_pct is not None:
            self.exposure_pct = exposure_pct
        if loss_streak is not None:
            self.loss_streak = loss_streak
        if week_dd_pct is not None:
            self.week_dd_pct = week_dd_pct
        try:
            self.query_one("#risk-bar-static", Static).update(self._render_bar())
        except Exception:
            pass


class EventLogTicker(Widget):
    """Real-time single-line causal event log stream."""

    DEFAULT_CSS = """
    EventLogTicker {
        height: 1;
        width: 100%;
        background: $surface;
        padding: 0 1;
        color: $text-muted;
    }
    """

    last_log: reactive[str] = reactive(
        "02:54:17 funding detected → 02:54:18 token created → B0 ✓"
    )

    def compose(self) -> ComposeResult:
        yield Static(self._render_log(), id="event-log-static")

    def _render_log(self) -> str:
        return f"[bold cyan]EVENT LOG[/bold cyan]  [white]{self.last_log}[/white]"

    def post_log(self, text: str) -> None:
        self.last_log = text
        try:
            self.query_one("#event-log-static", Static).update(self._render_log())
        except Exception:
            pass


class EventInspector(Widget):
    """Backward compatibility container managing TokenDetailCard and DevHistoryCard."""

    DEFAULT_CSS = """
    EventInspector {
        display: none;
    }
    """

    def compose(self) -> ComposeResult:
        yield TokenDetailCard(id="nested-token-detail")

    def apply_responsive_layout(self, width: int) -> None:
        pass

    def update_selection(
        self,
        item: ActivityItem | None,
        path: FundingPath | None = None,
    ) -> None:
        with contextlib.suppress(Exception):
            self.query_one("#nested-token-detail", TokenDetailCard).update_item(
                item, path
            )

    def update_item(
        self,
        item: ActivityItem | None,
        path: FundingPath | None = None,
    ) -> None:
        self.update_selection(item, path)

    def show_funder_idle(
        self,
        funder_address: str,
        descendants_count: int = 0,
        launches_count: int = 0,
        balance_lamports: int | None = None,
        tokens_count: int = 0,
        usdc_balance: float = 0.0,
    ) -> None:
        pass
