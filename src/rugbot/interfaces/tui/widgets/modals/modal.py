"""Deep inspection modal dialog opened on Enter."""

# ruff: noqa: ANN001, TC002

from __future__ import annotations

import webbrowser
from typing import TYPE_CHECKING

from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Static

from rugbot.interfaces.tui.formatters import (
    format_age,
    format_amount,
    format_timestamp,
)

if TYPE_CHECKING:
    from rugbot.interfaces.tui.widgets.panels.activity import ActivityItem
    from rugbot.tracker.models import FundingPath


class DetailInspectModal(ModalScreen[None]):
    """Modal screen presenting comprehensive on-chain provenance details."""

    DEFAULT_CSS = """
    DetailInspectModal {
        align: center middle;
        background: rgba(0, 0, 0, 0.7);
    }

    .modal-card {
        width: 80;
        height: auto;
        max-height: 90%;
        background: $surface;
        border: solid $accent;
        padding: 1 2;
    }

    .modal-title {
        text-style: bold;
        color: $accent;
        margin-bottom: 1;
    }

    .modal-section-title {
        text-style: bold;
        color: $warning;
        margin-top: 1;
    }

    .modal-content {
        margin-bottom: 1;
    }

    .modal-links-row {
        height: 3;
        width: 100%;
        layout: horizontal;
        margin-top: 1;
    }

    .link-btn {
        height: 3;
        min-width: 13;
        margin-right: 1;
    }

    .modal-links {
        margin-top: 1;
        color: $text-muted;
    }

    .modal-btn {
        margin-top: 1;
        width: 100%;
    }
    """

    def __init__(
        self,
        item: ActivityItem,
        path: FundingPath | None = None,
    ) -> None:
        super().__init__()
        self._item = item
        self._path = path

    def compose(self) -> ComposeResult:
        with Vertical(classes="modal-card"):
            yield Static(
                f"INSPECT: {self._item.event_type.upper()} {self._item.token_symbol}",
                classes="modal-title",
            )
            yield Static(self._render_details(), classes="modal-content")
            yield Static("EXPLORER & TRADING LINKS", classes="modal-section-title")
            with Horizontal(classes="modal-links-row"):
                if self._item.token_mint:
                    yield Button(
                        "Axiom",
                        variant="primary",
                        id="btn-link-axiom",
                        classes="link-btn",
                    )
                    yield Button(
                        "GMGN",
                        variant="primary",
                        id="btn-link-gmgn",
                        classes="link-btn",
                    )
                    yield Button(
                        "Pump.fun",
                        variant="warning",
                        id="btn-link-pumpfun",
                        classes="link-btn",
                    )
                if self._item.target_wallet:
                    yield Button(
                        "Dev Solscan",
                        variant="default",
                        id="btn-link-dev-solscan",
                        classes="link-btn",
                    )
                if self._item.signature:
                    yield Button(
                        "Tx Solscan",
                        variant="default",
                        id="btn-link-tx-solscan",
                        classes="link-btn",
                    )
            yield Static(self._render_links(), classes="modal-links")
            yield Button(
                "Close (Esc)",
                variant="default",
                id="close-modal-btn",
                classes="modal-btn",
            )

    def _render_details(self) -> str:
        lines: list[str] = [
            f"Event Type:   [bold cyan]{self._item.event_type.upper()}[/bold cyan]",
            f"Observed:     [bold white]{format_timestamp(self._item.timestamp)}[/bold white] ({format_age(self._item.timestamp)} ago)",
            f"Root Funder:  [bold yellow]{self._item.root_funder}[/bold yellow]",
            f"Target/Actor: [bold white]{self._item.target_wallet}[/bold white]",
        ]

        if self._item.amount_lamports:
            lines.append(
                f"Amount:       [bold green]{format_amount(self._item.amount_lamports)}[/bold green]"
            )

        lines.append(f"Depth/Hops:   [bold white]{self._item.hops}[/bold white]")

        if self._item.token_mint:
            lines.append(
                f"Mint Address: [bold magenta]{self._item.token_mint}[/bold magenta]"
            )

        if self._item.signature:
            lines.append(f"Signature:    [dim]{self._item.signature}[/dim]")

        if self._path and self._path.time_to_launch_seconds is not None:
            lines.append(
                f"Timing:       [bold white]{self._path.time_to_launch_seconds}s from root funding to event[/bold white]"
            )

        return "\n".join(lines)

    def _render_links(self) -> str:
        mint = self._item.token_mint
        wallet = self._item.target_wallet
        sig = self._item.signature

        links: list[str] = []
        if mint:
            links.append(f"• Axiom:   [dim]https://axiom.trade/token/{mint}[/dim]")
            links.append(f"• GMGN:    [dim]https://gmgn.ai/sol/token/{mint}[/dim]")
            links.append(f"• PumpFun: [dim]https://pump.fun/{mint}[/dim]")
        if wallet:
            links.append(f"• Solscan: [dim]https://solscan.io/account/{wallet}[/dim]")
        if sig:
            links.append(f"• Tx:      [dim]https://solscan.io/tx/{sig}[/dim]")

        return "\n".join(links)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        button_id = event.button.id
        mint = self._item.token_mint
        wallet = self._item.target_wallet
        sig = self._item.signature

        if button_id == "close-modal-btn":
            self.dismiss()
        elif button_id == "btn-link-axiom" and mint:
            webbrowser.open(f"https://axiom.trade/token/{mint}")
        elif button_id == "btn-link-gmgn" and mint:
            webbrowser.open(f"https://gmgn.ai/sol/token/{mint}")
        elif button_id == "btn-link-pumpfun" and mint:
            webbrowser.open(f"https://pump.fun/{mint}")
        elif button_id == "btn-link-dev-solscan" and wallet:
            webbrowser.open(f"https://solscan.io/account/{wallet}")
        elif button_id == "btn-link-tx-solscan" and sig:
            webbrowser.open(f"https://solscan.io/tx/{sig}")

    def on_key(self, event) -> None:
        if event.key in ("escape", "enter"):
            self.dismiss()
