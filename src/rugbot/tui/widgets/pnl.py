"""Wallet equity and observed net-PnL history for the Rugbot TUI."""

# ruff: noqa: TRY003, TRY301, TC003

from __future__ import annotations

import contextlib
import json
import os
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING

from textual.widget import Widget
from textual.widgets import Static

from rugbot.tracker.models import LAMPORTS_PER_SOL

if TYPE_CHECKING:
    from textual.app import ComposeResult

from rugbot.tui.formatters import short_address

_HISTORY_FIELDS = frozenset(
    {
        "wallet_address",
        "observed_at_epoch",
        "wallet_balance_lamports",
        "equity_lamports",
        "net_pnl_lamports",
    }
)
_SPARK_CHARS = "▁▂▃▄▅▆▇█"


@dataclass(frozen=True, slots=True)
class WalletPnlPoint:
    """One finalized balance observation used by the dashboard curve."""

    wallet_address: str
    observed_at_epoch: int
    wallet_balance_lamports: int
    equity_lamports: int
    net_pnl_lamports: int


class WalletPnlHistory:
    """Append-only, strict JSONL history for one or more public wallets."""

    def __init__(self, path: Path) -> None:
        self._path = path

    def read(self, wallet_address: str) -> tuple[WalletPnlPoint, ...]:
        """Read valid history for one wallet in file order."""

        if not self._path.exists():
            return ()
        points: list[WalletPnlPoint] = []
        try:
            lines = self._path.read_text(encoding="utf-8").splitlines()
        except OSError as error:
            raise ValueError("wallet PnL history could not be read") from error
        for line_number, line in enumerate(lines, start=1):
            if not line.strip():
                continue
            try:
                payload = json.loads(line)
                point = _point_from_json(payload)
            except (TypeError, ValueError, json.JSONDecodeError) as error:
                raise ValueError(
                    f"wallet PnL history is malformed at line {line_number}"
                ) from error
            if point.wallet_address == wallet_address:
                points.append(point)
        return tuple(points)

    def append(self, point: WalletPnlPoint) -> None:
        """Durably append one validated point without rewriting prior evidence."""

        validated = _validate_point(point)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        encoded = (json.dumps(_point_to_json(validated), sort_keys=True) + "\n").encode(
            "utf-8"
        )
        descriptor: int | None = None
        try:
            descriptor = os.open(
                self._path,
                os.O_CREAT | os.O_APPEND | os.O_WRONLY,
                0o600,
            )
            offset = 0
            while offset < len(encoded):
                written = os.write(descriptor, encoded[offset:])
                if written <= 0:
                    raise OSError("wallet PnL history write made no progress")
                offset += written
            os.fsync(descriptor)
        except OSError as error:
            raise ValueError("wallet PnL history could not be written") from error
        finally:
            if descriptor is not None:
                os.close(descriptor)

    def record_balance(
        self,
        wallet_address: str,
        balance_lamports: int,
        *,
        observed_at_epoch: int,
    ) -> WalletPnlPoint:
        """Record one finalized balance and derive its observed wallet delta."""

        if type(wallet_address) is not str or not wallet_address:
            raise ValueError("wallet PnL wallet address is required")
        if type(balance_lamports) is not int or balance_lamports < 0:
            raise ValueError(
                "wallet balance must be a non-negative integer lamport value"
            )
        if type(observed_at_epoch) is not int or observed_at_epoch < 0:
            raise ValueError("wallet PnL observation time is malformed")
        previous = self.read(wallet_address)
        baseline = previous[0].wallet_balance_lamports if previous else balance_lamports
        point = WalletPnlPoint(
            wallet_address=wallet_address,
            observed_at_epoch=observed_at_epoch,
            wallet_balance_lamports=balance_lamports,
            equity_lamports=balance_lamports,
            net_pnl_lamports=balance_lamports - baseline,
        )
        self.append(point)
        return point


def _validate_point(point: WalletPnlPoint) -> WalletPnlPoint:
    if type(point) is not WalletPnlPoint:
        raise ValueError("wallet PnL point is malformed")
    if type(point.wallet_address) is not str or not point.wallet_address:
        raise ValueError("wallet PnL point wallet address is malformed")
    if type(point.observed_at_epoch) is not int or point.observed_at_epoch < 0:
        raise ValueError("wallet PnL point time is malformed")
    if any(
        type(value) is not int
        for value in (
            point.wallet_balance_lamports,
            point.equity_lamports,
            point.net_pnl_lamports,
        )
    ):
        raise ValueError("wallet PnL point amounts must be integers")
    if point.wallet_balance_lamports < 0 or point.equity_lamports < 0:
        raise ValueError("wallet PnL point balances must be non-negative")
    return point


def _point_from_json(payload: object) -> WalletPnlPoint:
    if type(payload) is not dict or frozenset(payload) != _HISTORY_FIELDS:
        raise ValueError("wallet PnL point fields are malformed")
    return _validate_point(
        WalletPnlPoint(
            wallet_address=payload["wallet_address"],
            observed_at_epoch=payload["observed_at_epoch"],
            wallet_balance_lamports=payload["wallet_balance_lamports"],
            equity_lamports=payload["equity_lamports"],
            net_pnl_lamports=payload["net_pnl_lamports"],
        )
    )


def _point_to_json(point: WalletPnlPoint) -> dict[str, object]:
    return {
        "wallet_address": point.wallet_address,
        "observed_at_epoch": point.observed_at_epoch,
        "wallet_balance_lamports": point.wallet_balance_lamports,
        "equity_lamports": point.equity_lamports,
        "net_pnl_lamports": point.net_pnl_lamports,
    }


class WalletPnlPanel(Widget):
    """Compact dashboard panel showing equity and observed net-PnL curves."""

    DEFAULT_CSS = """
    WalletPnlPanel {
        height: 8;
        min-height: 6;
        width: 100%;
        layout: vertical;
        background: $surface;
        border-left: solid $panel;
        border-top: solid $panel;
        padding: 0 1;
    }

    .pnl-header {
        height: 1;
        width: 100%;
        background: $boost;
        color: $accent;
        text-style: bold;
    }

    .pnl-content {
        height: 1fr;
        width: 100%;
    }
    """

    def __init__(self, *args: object, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)
        self._wallet_address = ""
        self._points: tuple[WalletPnlPoint, ...] = ()

    def compose(self) -> ComposeResult:
        yield Static("WALLET PNL", classes="pnl-header")
        yield Static(self._render_content(), classes="pnl-content", id="pnl-content")

    @property
    def points(self) -> tuple[WalletPnlPoint, ...]:
        """Return the points currently rendered by the panel."""

        return self._points

    def update_history(
        self,
        wallet_address: str,
        points: tuple[WalletPnlPoint, ...],
    ) -> None:
        """Replace the displayed history with validated point-in-time data."""

        self._wallet_address = wallet_address
        self._points = tuple(_validate_point(point) for point in points)
        self._refresh()

    def update_error(self, message: str) -> None:
        """Display a fail-closed state when the derived history is malformed."""

        self._points = ()
        with contextlib.suppress(Exception):
            self.query_one("#pnl-content", Static).update(
                f"[bold yellow]PNL UNAVAILABLE[/bold yellow]\n[dim]{message}[/dim]"
            )

    def _refresh(self) -> None:
        with contextlib.suppress(Exception):
            self.query_one("#pnl-content", Static).update(self._render_content())

    def _render_content(self) -> str:
        if not self._points:
            wallet = short_address(self._wallet_address) if self._wallet_address else ""
            target_str = f" {wallet}" if wallet else ""
            return f"[dim]WALLET{target_str}   awaiting RPC…[/dim]"
        latest = self._points[-1]
        equity = _format_sol(latest.equity_lamports)
        net = _format_signed_sol(latest.net_pnl_lamports)
        equity_curve = _sparkline(
            tuple(point.equity_lamports for point in self._points)
        )
        net_curve = _sparkline(tuple(point.net_pnl_lamports for point in self._points))
        return (
            f"[dim]Wallet[/dim] [white]{short_address(latest.wallet_address)}[/white]  "
            f"[dim]Equity[/dim] [bold white]{equity} SOL[/bold white]  "
            f"[dim]Net Δ[/dim] [bold {_pnl_color(latest.net_pnl_lamports)}]{net} SOL[/bold {_pnl_color(latest.net_pnl_lamports)}]\n"
            f"[cyan]EQUITY[/cyan] [white]{equity_curve}[/white]\n"
            f"[yellow]NET PNL[/yellow] [white]{net_curve}[/white]\n"
            "[dim]Observed wallet balance delta · deposits/withdrawals included · finalized RPC[/dim]"
        )


def _sparkline(values: tuple[int, ...], *, width: int = 44) -> str:
    if not values:
        return ""
    if len(values) == 1:
        return _SPARK_CHARS[0] * width
    sampled = _resample(values, width)
    minimum = min(sampled)
    maximum = max(sampled)
    if minimum == maximum:
        return _SPARK_CHARS[3] * width
    span = maximum - minimum
    return "".join(
        _SPARK_CHARS[(value - minimum) * (len(_SPARK_CHARS) - 1) // span]
        for value in sampled
    )


def _resample(values: tuple[int, ...], width: int) -> tuple[int, ...]:
    if len(values) <= width:
        return values + (values[-1],) * (width - len(values))
    return tuple(
        values[index * (len(values) - 1) // (width - 1)] for index in range(width)
    )


def _format_sol(lamports: int) -> str:
    value = Decimal(lamports) / Decimal(LAMPORTS_PER_SOL)
    return format(value, ".9f").rstrip("0").rstrip(".") or "0"


def _format_signed_sol(lamports: int) -> str:
    sign = "+" if lamports >= 0 else ""
    return (
        f"{sign}{_format_sol(lamports)}"
        if lamports >= 0
        else f"-{_format_sol(abs(lamports))}"
    )


def _pnl_color(lamports: int) -> str:
    if lamports > 0:
        return "green"
    if lamports < 0:
        return "red"
    return "white"


__all__ = ["WalletPnlHistory", "WalletPnlPanel", "WalletPnlPoint"]
