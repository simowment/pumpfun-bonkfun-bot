# ruff: noqa: BLE001, S110, TC002, PLR2004

from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import Horizontal
from textual.reactive import reactive
from textual.widget import Widget
from textual.widgets import Static


class CompactHeader(Widget):
    """High-density status header that never invents network telemetry."""

    DEFAULT_CSS = """
    CompactHeader {
        height: 1;
        width: 100%;
        background: #0d1117;
        padding: 0 1;
        overflow: hidden hidden;
    }

    .header-row {
        height: 1;
        width: 100%;
        layout: horizontal;
    }

    #header-telemetry {
        width: 100%;
        height: 1;
    }
    """

    rpc_status: reactive[str] = reactive("unknown")
    rpc_latency_ms: reactive[int | None] = reactive(None)
    send_latency_ms: reactive[int | None] = reactive(None)
    stream_status: reactive[str] = reactive("unknown")
    wallet_balance_sol: reactive[str] = reactive("—")
    daily_pnl_sol: reactive[str] = reactive("--")
    execution_mode: reactive[str] = reactive("DRY RUN")
    active_targets_count: reactive[int] = reactive(0)
    active_positions_count: reactive[int] = reactive(0)

    def compose(self) -> ComposeResult:
        with Horizontal(classes="header-row"):
            yield Static(self._render_header_line(), id="header-telemetry")

    def _render_header_line(self) -> str:
        parts: list[str] = ["[bold cyan]RUGBOT[/bold cyan]"]

        if self.rpc_status == "connected":
            latency = self.rpc_latency_ms
            if latency is not None and latency >= 500:
                parts.append(f"[bold yellow]● RPC DEGRADED {latency}ms[/bold yellow]")
            elif latency is not None:
                parts.append(f"[bold green]● RPC ONLINE {latency}ms[/bold green]")
            else:
                parts.append("[bold green]● RPC ONLINE[/bold green]")
        elif self.rpc_status == "disconnected":
            parts.append("[bold red]● RPC OFFLINE[/bold red]")
        else:
            parts.append("[bold yellow]● RPC CONNECTING[/bold yellow]")

        parts.append(
            f"[dim]TARGETS:[/dim] [white]{self.active_targets_count} active[/white]"
        )
        parts.append(
            f"[dim]POSITIONS:[/dim] [white]{self.active_positions_count} open[/white]"
        )
        parts.append(f"[dim]WALLET:[/dim] [white]{self.wallet_balance_sol} SOL[/white]")

        pnl_color = (
            "green"
            if self.daily_pnl_sol.startswith("+")
            else "red"
            if self.daily_pnl_sol.startswith("-")
            else "white"
        )
        parts.append(
            f"[dim]DAY PNL:[/dim] [bold {pnl_color}]{self.daily_pnl_sol} SOL[/bold {pnl_color}]"
        )
        return "  [dim]|[/dim]  ".join(parts)

    def watch_rpc_status(self, _val: str) -> None:
        self._update_display()

    def watch_rpc_latency_ms(self, _val: int | None) -> None:
        self._update_display()

    def watch_wallet_balance_sol(self, _val: str) -> None:
        self._update_display()

    def watch_daily_pnl_sol(self, _val: str) -> None:
        self._update_display()

    def watch_execution_mode(self, _val: str) -> None:
        self._update_display()

    def watch_active_targets_count(self, _val: int) -> None:
        self._update_display()

    def watch_active_positions_count(self, _val: int) -> None:
        self._update_display()

    def _update_display(self) -> None:
        try:
            self.query_one("#header-telemetry", Static).update(
                self._render_header_line()
            )
        except Exception:
            pass
