"""Fast sell and position exit modal dialog with per-wallet controls."""

# ruff: noqa: TC002, TC003, S107

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import ClassVar

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Checkbox, Label, Static

from rugbot.tui.formatters import short_address

PERCENT_100 = 100


@dataclass(frozen=True, slots=True)
class FastSellOrder:
    """Submitted fast sell order parameters."""

    market_id: str
    percentage: int  # 25, 50, 100
    selected_wallet_addresses: tuple[str, ...]
    max_slippage_bps: int
    close_ata: bool


class FastSellModal(ModalScreen[FastSellOrder | None]):
    """Modal screen for rapid position reduction or emergency exit."""

    BINDINGS: ClassVar[list[Binding]] = [
        Binding("enter", "submit", "Submit", show=False, priority=True),
        Binding("escape", "cancel", "Cancel", show=False, priority=True),
    ]

    DEFAULT_CSS = """
    FastSellModal {
        align: center middle;
        background: rgba(0, 0, 0, 0.75);
    }

    .sell-card {
        width: 76;
        height: auto;
        max-height: 90%;
        background: $surface;
        border: solid $error;
        padding: 1 2;
    }

    .sell-title {
        text-style: bold;
        color: $error;
        margin-bottom: 1;
    }

    .sell-label {
        color: $text-muted;
        margin-top: 1;
        margin-bottom: 0;
    }

    .sell-pct-container {
        height: auto;
        margin-bottom: 1;
    }

    .pct-btn {
        margin-right: 1;
        min-width: 13;
    }

    .wallets-container {
        height: auto;
        max-height: 5;
        border: solid $panel;
        padding: 0 1;
        margin-bottom: 1;
    }

    .action-row {
        height: auto;
        margin-top: 1;
    }

    .action-btn {
        width: 1fr;
        margin: 0 1;
    }
    """

    def __init__(
        self,
        mint: str,
        token_symbol: str = "TOKEN",
        current_balance: str = "0",
        available_wallets: Sequence[tuple[str, str]] | None = None,
    ) -> None:
        super().__init__()
        self._mint = mint
        self._token_symbol = token_symbol
        self._current_balance = current_balance
        self._available_wallets = list(available_wallets or [])
        self._selected_pct: int = PERCENT_100
        self._selected_wallets: set[str] = {w[0] for w in self._available_wallets}

    def compose(self) -> ComposeResult:
        with Vertical(classes="sell-card"):
            yield Static(
                f"FAST SELL: {self._token_symbol} ({short_address(self._mint)})",
                classes="sell-title",
            )
            yield Static(
                f"Current Balance: [bold white]{self._current_balance}[/bold white] {self._token_symbol}",
            )

            yield Label("EXIT STRATEGY / PERCENTAGE:", classes="sell-label")
            with Horizontal(classes="sell-pct-container"):
                yield Button("25%", id="btn-sell-25", classes="pct-btn")
                yield Button("50%", id="btn-sell-50", classes="pct-btn")
                yield Button("Take Initials", id="btn-sell-initials", classes="pct-btn")
                yield Button(
                    "100% (Dump)", id="btn-sell-100", classes="pct-btn", variant="error"
                )

            yield Label("EXIT PER-WALLET:", classes="sell-label")
            with Vertical(classes="wallets-container"):
                for addr, label in self._available_wallets:
                    yield Checkbox(
                        f"{label} ({short_address(addr)})",
                        value=addr in self._selected_wallets,
                        id=f"chk-sell-{addr}",
                    )

            with Horizontal(classes="action-row"):
                yield Button(
                    "CONFIRM EXIT (Enter)",
                    variant="error",
                    id="submit-sell-btn",
                    classes="action-btn",
                )
                yield Button(
                    "Cancel (Esc)",
                    variant="default",
                    id="cancel-sell-btn",
                    classes="action-btn",
                )

    def on_button_pressed(self, event: Button.Pressed) -> None:
        """Handle sell preset clicks."""
        if event.button.id == "btn-sell-25":
            self._selected_pct = 25
            self._highlight_pct("btn-sell-25")
        elif event.button.id == "btn-sell-50":
            self._selected_pct = 50
            self._highlight_pct("btn-sell-50")
        elif event.button.id == "btn-sell-initials":
            self._selected_pct = 50  # Take initials = 50% exit
            self._highlight_pct("btn-sell-initials")
        elif event.button.id == "btn-sell-100":
            self._selected_pct = PERCENT_100
            self._highlight_pct("btn-sell-100")
        elif event.button.id == "submit-sell-btn":
            self._dispatch_order()
        elif event.button.id == "cancel-sell-btn":
            self.dismiss(None)

    def action_submit(self) -> None:
        """Submit the selected exit from the keyboard."""
        self._dispatch_order()

    def action_cancel(self) -> None:
        """Close the modal without creating an exit order."""
        self.dismiss(None)

    def _highlight_pct(self, active_id: str) -> None:
        for btn_id in (
            "btn-sell-25",
            "btn-sell-50",
            "btn-sell-initials",
            "btn-sell-100",
        ):
            btn = self.query_one(f"#{btn_id}", Button)
            btn.variant = "error" if btn_id == active_id else "default"

    def on_checkbox_changed(self, event: Checkbox.Changed) -> None:
        if event.checkbox.id and event.checkbox.id.startswith("chk-sell-"):
            addr = event.checkbox.id[9:]
            if event.value:
                self._selected_wallets.add(addr)
            else:
                self._selected_wallets.discard(addr)

    def _dispatch_order(self) -> None:
        target_addrs = tuple(self._selected_wallets)
        if not target_addrs:
            return

        order = FastSellOrder(
            market_id=self._mint,
            percentage=self._selected_pct,
            selected_wallet_addresses=target_addrs,
            max_slippage_bps=1500,  # 15% slippage on emergency exit
            close_ata=self._selected_pct == PERCENT_100,
        )
        self.dismiss(order)
