"""EXECUTION rail panel matching operator mockup."""

# ruff: noqa: TC002, BLE001, S105, S110, ANN401, TC001

from __future__ import annotations

import contextlib
from typing import TYPE_CHECKING, Any

from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.reactive import reactive
from textual.widget import Widget
from textual.widgets import Button, Static

from rugbot.tracker.models import TargetRecord
from rugbot.tui.formatters import format_sol, short_address
from rugbot.tui.widgets.inspector import OperatorStage

if TYPE_CHECKING:
    from rugbot.tui.widgets.activity import ActivityItem


class ExecutionCard(Widget):
    """EXECUTION panel displaying active Target, Mode, Sizing, Slippage, Priority, and Status."""

    DEFAULT_CSS = """
    ExecutionCard {
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

    #execution-content {
        height: 1fr;
        padding: 0 1;
        overflow: hidden hidden;
    }

    .panel-action-bar {
        height: 1;
        padding: 0 1;
        background: #161b22;
        color: #8b949e;
        border-top: solid #21262d;
    }

    #exec-action-buttons {
        display: none;
    }
    """

    stage: reactive[OperatorStage] = reactive(OperatorStage.ARMED)
    target_address: reactive[str] = reactive("—")
    mode_str: reactive[str] = reactive("UNCONFIGURED")
    buy_size_text: reactive[str] = reactive("—")
    slippage_text: reactive[str] = reactive("—")
    fee_text: reactive[str] = reactive("—")
    exit_text: reactive[str] = reactive("—")
    monitoring_status: reactive[str] = reactive("TRACKER ONLY")
    status_detail: reactive[str] = reactive("Configure a target policy")

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._target: TargetRecord | None = None
        self._selected_item: ActivityItem | None = None

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static("EXECUTION", classes="panel-header")
            yield Static(self._render_content(), id="execution-content")
            with Horizontal(id="exec-action-buttons"):
                yield Button("Go Live", id="btn-exec-go-live")
                yield Button("Edit", id="btn-exec-edit")
                yield Button("Exit", id="btn-exec-exit")
                yield Button("Sell 50%", id="btn-exec-sell50")
                yield Button("Simulate", id="btn-exec-simulate")
            yield Static(
                "[bold cyan][E][/bold cyan] EDIT STRATEGY   [bold cyan][P][/bold cyan] PAUSE/RESUME",
                classes="panel-action-bar",
            )

    def _render_content(self) -> str:
        if self._target is None or self._target.policy is None:
            return (
                "[bold yellow]TRACKER ONLY[/bold yellow]\n"
                "[dim]No persisted execution policy for this funder.\n"
                "Configure size, TP/SL, slippage, and fees in Settings.[/dim]"
            )
        if self.stage == OperatorStage.POSITION_OPEN:
            item = self._selected_item
            symbol = item.token_symbol if item and item.token_symbol != "—" else "TOKEN"
            return "\n".join(
                (
                    "[bold green]LIVE POSITION[/bold green]",
                    f"[bold white]{symbol}[/bold white]",
                    f"[dim]Target[/dim]       [cyan]{self.target_address}[/cyan]",
                    f"[dim]Buy size[/dim]     [white]{self.buy_size_text} SOL[/white]",
                    f"[dim]Mode[/dim]         [cyan]{self.mode_str}[/cyan]",
                    "",
                    "[dim]STATUS[/dim]",
                    "[bold green]● IN POSITION[/bold green]",
                    f"[dim]TP / SL: {self.exit_text}[/dim]",
                )
            )

        if self.stage == OperatorStage.CANDIDATE:
            item = self._selected_item
            symbol = item.token_symbol if item and item.token_symbol != "—" else "TOKEN"
            mc_val = (
                f"${item.market_cap_usd:,.0f}" if item and item.market_cap_usd else "—"
            )
            lines = [
                "[bold yellow]EXECUTION CANDIDATE[/bold yellow]",
                f"[bold white]{symbol}[/bold white]",
                f"[dim]Target[/dim]       [cyan]{self.target_address}[/cyan]",
                f"[dim]Market Cap[/dim]   [white]{mc_val}[/white]",
                f"[dim]Buy size[/dim]     [white]{self.buy_size_text} SOL[/white]",
                f"[dim]Mode[/dim]         [cyan]{self.mode_str}[/cyan]",
                "",
                "[dim]STATUS[/dim]",
                "[bold green]● CANDIDATE READY[/bold green]",
                f"[dim]{self.status_detail}[/dim]",
            ]
            return "\n".join(lines)

        if self.stage in {OperatorStage.PENDING, OperatorStage.FAILED}:
            failed = self.stage == OperatorStage.FAILED
            color = "red" if failed else "yellow"
            label = "EXECUTION FAILED" if failed else "EXECUTION PENDING"
            return "\n".join(
                (
                    f"[bold {color}]{label}[/bold {color}]",
                    f"[dim]Target[/dim]       [cyan]{self.target_address}[/cyan]",
                    f"[dim]Mode[/dim]         [cyan]{self.mode_str}[/cyan]",
                    f"[dim]Buy size[/dim]     [white]{self.buy_size_text} SOL[/white]",
                    "",
                    "[dim]STATUS[/dim]",
                    f"[{color}]{self.status_detail}[/{color}]",
                )
            )

        status_bullet = (
            "[bold green]●[/bold green]"
            if self.monitoring_status == "MONITORING"
            else "[dim]○[/dim]"
        )
        status_col = "bold green" if self.monitoring_status == "MONITORING" else "dim"

        lines = [
            f"[dim]Target[/dim]       [cyan]{self.target_address}[/cyan]",
            f"[dim]Mode[/dim]         [cyan]{self.mode_str}[/cyan]",
            f"[dim]Buy size[/dim]     [white]{self.buy_size_text} SOL[/white]",
            f"[dim]TP / SL[/dim]      [white]{self.exit_text}[/white]",
            f"[dim]Slippage[/dim]     [white]{self.slippage_text}[/white]",
            f"[dim]Fees[/dim]         [white]{self.fee_text}[/white]",
            "",
            "[dim]STATUS[/dim]",
            f"{status_bullet} [{status_col}]{self.monitoring_status}[/{status_col}]",
            f"[dim]{self.status_detail}[/dim]",
        ]
        return "\n".join(lines)

    def set_stage(self, stage: OperatorStage) -> None:
        self.stage = stage
        with contextlib.suppress(Exception):
            if stage == OperatorStage.POSITION_OPEN:
                self.query_one("#btn-exec-exit").display = True
                self.query_one("#btn-exec-sell50").display = True
                self.query_one("#btn-exec-simulate").display = False
                self.query_one("#btn-exec-edit").display = False
            elif stage == OperatorStage.CANDIDATE:
                self.query_one("#btn-exec-simulate").display = True
                self.query_one("#btn-exec-exit").display = False
                self.query_one("#btn-exec-sell50").display = False
                self.query_one("#btn-exec-edit").display = True
            else:
                self.query_one("#btn-exec-exit").display = False
                self.query_one("#btn-exec-sell50").display = False
                self.query_one("#btn-exec-simulate").display = False
                self.query_one("#btn-exec-edit").display = True
        self._refresh()

    def update_target(self, target: TargetRecord) -> None:
        self.stage = OperatorStage.ARMED
        self._target = target
        self.target_address = short_address(target.address)
        policy = target.policy
        if policy is None:
            self.mode_str = "UNCONFIGURED"
            self.monitoring_status = "TRACKER ONLY"
            self.status_detail = "Configure this target before execution"
        else:
            self.buy_size_text = format_sol(policy.quote_size_lamports)
            self.slippage_text = f"{policy.max_slippage_bps} bps"
            self.fee_text = (
                f"{_format_priority_fee(policy.priority_fee_microlamports)}µ/CU + "
                f"{format_sol(policy.jito_tip_lamports)}◎"
            )
            self.exit_text = (
                f"{_format_pnl_ppm(policy.take_profit_pnl_ppm)} / "
                f"{_format_pnl_ppm(policy.stop_loss_pnl_ppm)}"
            )
            self.mode_str = (
                "DRY RUN"
                if policy.execution_mode.value == "simulated"
                else policy.execution_mode.value.upper()
            )
            if not policy.monitoring_enabled:
                self.monitoring_status = "PAUSED"
                self.status_detail = "Persisted target policy is paused"
            elif policy.execution_mode.value == "off":
                self.monitoring_status = "TRACKER ONLY"
                self.status_detail = "Observed only; execution is disabled"
            else:
                self.monitoring_status = "MONITORING"
                self.status_detail = "Persisted target policy"
        with contextlib.suppress(Exception):
            self.query_one("#btn-exec-edit").display = True
            self.query_one("#btn-exec-exit").display = False
        self._refresh()

    def update_item(self, item: ActivityItem | None) -> None:
        self._selected_item = item
        if item:
            self.stage = OperatorStage.CANDIDATE
            if item.target_wallet:
                self.target_address = short_address(item.target_wallet)
            if item.reason or item.latency_summary:
                self.status_detail = item.reason or item.latency_summary
        else:
            self.stage = OperatorStage.ARMED
        self._refresh()

    def set_execution_mode(self, mode: str) -> None:
        self.mode_str = "LIVE" if mode.lower() == "live" else "DRY RUN"
        self._refresh()

    def update_runtime_stage(self, stage: str, message: str) -> None:
        """Project the daemon's canonical lifecycle into the execution rail."""

        stage_map = {
            "IDLE": OperatorStage.ARMED,
            "CANDIDATE": OperatorStage.CANDIDATE,
            "PENDING": OperatorStage.PENDING,
            "POSITION": OperatorStage.POSITION_OPEN,
            "FAILED": OperatorStage.FAILED,
        }
        self.stage = stage_map.get(stage, OperatorStage.FAILED)
        self.status_detail = message
        self._refresh()

    def _refresh(self) -> None:
        try:
            self.query_one("#execution-content", Static).update(self._render_content())
        except Exception:
            pass


def _format_pnl_ppm(value: int) -> str:
    """Format an exact PnL threshold stored in parts per million."""
    sign = "+" if value > 0 else "-" if value < 0 else ""
    whole, fraction = divmod(abs(value), 10_000)
    return f"{sign}{whole}.{fraction:04d}%"


def _format_priority_fee(microlamports: int) -> str:
    if microlamports % 1_000 == 0:
        return f"{microlamports // 1_000}k"
    return str(microlamports)
