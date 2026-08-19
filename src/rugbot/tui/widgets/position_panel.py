# ruff: noqa: F401, TC002, BLE001, S105, S110, FBT003

from __future__ import annotations

from typing import Any

from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.reactive import reactive
from textual.widget import Widget
from textual.widgets import Static


class PositionExecutionPanel(Widget):
    """Displays active trading position on the left and last execution latency on the right."""

    DEFAULT_CSS = """
    PositionExecutionPanel {
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

    .split-body {
        height: 1fr;
        width: 100%;
        layout: horizontal;
        overflow: hidden hidden;
    }

    #position-left-sub {
        width: 60%;
        height: 100%;
        padding: 1;
        border-right: solid #21262d;
    }

    #execution-right-sub {
        width: 40%;
        height: 100%;
        padding: 1;
    }
    """

    has_open_position: reactive[bool] = reactive(False)
    position_token: reactive[str] = reactive("")
    position_pnl_pct: reactive[float] = reactive(0.0)

    has_last_execution: reactive[bool] = reactive(False)
    last_token: reactive[str] = reactive("—")
    last_result: reactive[str] = reactive("—")
    last_detect_ms: reactive[int] = reactive(0)
    last_confirm_ms: reactive[int] = reactive(0)

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static("CURRENT POSITION / EXECUTION", classes="panel-header")
            with Horizontal(classes="split-body"):
                yield Static(self._render_position_left(), id="position-left-sub")
                yield Static(self._render_execution_right(), id="execution-right-sub")

    def _render_position_left(self) -> str:
        if not self.has_open_position or not self.position_token:
            return "[dim]No open position[/dim]"
        pnl_col = "green" if self.position_pnl_pct >= 0 else "red"
        pnl_sign = "+" if self.position_pnl_pct >= 0 else ""
        return f"[bold white]{self.position_token}[/bold white]\n[bold {pnl_col}]{pnl_sign}{self.position_pnl_pct:.1f}%[/bold {pnl_col}]"

    def _render_execution_right(self) -> str:
        if not self.has_last_execution or self.last_token == "—":
            return "[dim]Last execution\n\nNo execution recorded yet[/dim]"

        total_lat = self.last_detect_ms + self.last_confirm_ms
        res_col = "bold green" if self.last_result == "CONFIRMED" else "bold red"
        lines = [
            "[dim]Last execution[/dim]",
            f"[dim]Token[/dim]            [bold cyan]{self.last_token}[/bold cyan]",
            f"[dim]Result[/dim]           [{res_col}]{self.last_result}[/{res_col}]",
            f"[dim]Detect → Send[/dim]    [cyan]{self.last_detect_ms}ms[/cyan]",
            f"[dim]Send → Confirm[/dim]   [cyan]{self.last_confirm_ms}ms[/cyan]",
            f"[dim]Total latency[/dim]    [cyan]{total_lat}ms[/cyan]",
        ]
        return "\n".join(lines)

    def update_execution(
        self,
        *,
        token: str,
        result: str,
        detect_ms: int,
        confirm_ms: int,
    ) -> None:
        self.has_last_execution = True
        self.last_token = token
        self.last_result = result
        self.last_detect_ms = detect_ms
        self.last_confirm_ms = confirm_ms
        try:
            self.query_one("#execution-right-sub", Static).update(
                self._render_execution_right()
            )
        except Exception:
            pass
