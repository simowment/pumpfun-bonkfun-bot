"""Quick Buy modal screen for single-click multi-wallet execution with presets."""

# ruff: noqa: TC002, TC003

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import ClassVar

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import (
    Button,
    Checkbox,
    Input,
    Label,
    Static,
)

from rugbot.domain.amounts import Lamports
from rugbot.interfaces.tui.formatters import short_address


@dataclass(frozen=True, slots=True)
class QuickBuyOrder:
    """Submitted quick buy order parameters."""

    market_id: str
    amount_lamports: Lamports
    selected_wallet_addresses: tuple[str, ...]
    max_slippage_bps: int
    priority_tip_lamports: Lamports


class QuickBuyModal(ModalScreen[QuickBuyOrder | None]):
    """Modal screen for rapid multi-wallet token purchases."""

    BINDINGS: ClassVar[list[Binding]] = [
        Binding("enter", "submit", "Submit", show=False, priority=True),
        Binding("escape", "cancel", "Cancel", show=False, priority=True),
    ]

    DEFAULT_CSS = """
    QuickBuyModal {
        align: center middle;
        background: rgba(0, 0, 0, 0.75);
    }

    .quickbuy-card {
        width: 78;
        height: 90%;
        background: $surface;
        border: solid $accent;
        padding: 1 2;
        overflow-y: auto;
    }

    .quickbuy-title {
        text-style: bold;
        color: $accent;
        margin-bottom: 1;
    }

    .quickbuy-label {
        color: $text-muted;
        margin-top: 1;
        margin-bottom: 0;
    }

    .quickbuy-input {
        margin-bottom: 1;
    }

    .presets-container {
        height: auto;
        margin-bottom: 1;
    }

    .preset-btn {
        margin-right: 1;
        min-width: 14;
    }

    .wallets-container {
        height: auto;
        max-height: 6;
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

    .info-badge {
        color: $success;
        margin-bottom: 1;
    }
    """

    def __init__(
        self,
        default_mint: str = "",
        available_wallets: Sequence[tuple[str, str]] | None = None,
        default_preset_sol: float = 0.1,
    ) -> None:
        """Initialize QuickBuyModal.

        Args:
            default_mint: Optional prefilled contract address.
            available_wallets: List of (address, label) tuples.
            default_preset_sol: Initial default buy amount.
        """
        super().__init__()
        self._default_mint = default_mint
        self._available_wallets = list(available_wallets or [])
        self._selected_amount_sol: float = default_preset_sol
        self._selected_wallets: set[str] = {w[0] for w in self._available_wallets}

    def compose(self) -> ComposeResult:
        with Vertical(classes="quickbuy-card"):
            yield Static("QUICK BUY (MULTI-WALLET SNIPER)", classes="quickbuy-title")

            yield Label("CONTRACT ADDRESS (CA / MINT):", classes="quickbuy-label")
            yield Input(
                value=self._default_mint,
                placeholder="Paste Solana token mint address...",
                id="ca-input",
                classes="quickbuy-input",
            )
            yield Static(
                "Auto-detected: Pump.fun / Raydium ready",
                id="ca-badge",
                classes="info-badge",
            )

            yield Label("PRESET BUY AMOUNT (PER WALLET):", classes="quickbuy-label")
            with Horizontal(classes="presets-container"):
                yield Button(
                    "P1: 0.1 SOL", id="btn-p1", classes="preset-btn", variant="primary"
                )
                yield Button("P2: 0.5 SOL", id="btn-p2", classes="preset-btn")
                yield Button("P3: 1.0 SOL", id="btn-p3", classes="preset-btn")
                yield Button("Custom", id="btn-custom", classes="preset-btn")

            with Horizontal(id="custom-amount-row", classes="presets-container"):
                yield Label("Custom SOL: ", classes="quickbuy-label")
                yield Input(
                    value=str(self._selected_amount_sol),
                    id="custom-amount-input",
                    classes="quickbuy-input",
                )

            yield Label(
                "TARGET WALLETS (SIMULTANEOUS DISPATCH):", classes="quickbuy-label"
            )
            with Vertical(classes="wallets-container"):
                for addr, label in self._available_wallets:
                    yield Checkbox(
                        f"{label} ({short_address(addr)})",
                        value=addr in self._selected_wallets,
                        id=f"chk-{addr}",
                    )

            with Horizontal(classes="action-row"):
                yield Button(
                    "FIRE SIMULTANEOUS BUY (Enter)",
                    variant="success",
                    id="submit-buy-btn",
                    classes="action-btn",
                )
                yield Button(
                    "Cancel (Esc)",
                    variant="error",
                    id="cancel-buy-btn",
                    classes="action-btn",
                )

    def on_button_pressed(self, event: Button.Pressed) -> None:
        """Handle preset selection or modal action buttons."""
        if event.button.id == "btn-p1":
            self._selected_amount_sol = 0.1
            self.query_one("#custom-amount-input", Input).value = "0.1"
            self._highlight_preset("btn-p1")
        elif event.button.id == "btn-p2":
            self._selected_amount_sol = 0.5
            self.query_one("#custom-amount-input", Input).value = "0.5"
            self._highlight_preset("btn-p2")
        elif event.button.id == "btn-p3":
            self._selected_amount_sol = 1.0
            self.query_one("#custom-amount-input", Input).value = "1.0"
            self._highlight_preset("btn-p3")
        elif event.button.id == "btn-custom":
            self._highlight_preset("btn-custom")
        elif event.button.id == "submit-buy-btn":
            self._dispatch_order()
        elif event.button.id == "cancel-buy-btn":
            self.dismiss(None)

    def action_submit(self) -> None:
        """Submit the selected paper order from the keyboard."""
        self._dispatch_order()

    def action_cancel(self) -> None:
        """Close the modal without creating an order."""
        self.dismiss(None)

    def _highlight_preset(self, active_id: str) -> None:
        for btn_id in ("btn-p1", "btn-p2", "btn-p3", "btn-custom"):
            btn = self.query_one(f"#{btn_id}", Button)
            btn.variant = "primary" if btn_id == active_id else "default"

    def on_checkbox_changed(self, event: Checkbox.Changed) -> None:
        """Track selected execution wallets."""
        if event.checkbox.id and event.checkbox.id.startswith("chk-"):
            addr = event.checkbox.id[4:]
            if event.value:
                self._selected_wallets.add(addr)
            else:
                self._selected_wallets.discard(addr)

    def _dispatch_order(self) -> None:
        """Validate inputs and dismiss modal with execution order."""
        ca = self.query_one("#ca-input", Input).value.strip()
        if not ca:
            self.query_one("#ca-badge", Static).update(
                "[red]Error: Contract Address is required[/red]"
            )
            return

        amt_str = self.query_one("#custom-amount-input", Input).value.strip()
        try:
            amt = float(amt_str)
            if amt <= 0:
                self.query_one("#ca-badge", Static).update(
                    "[red]Error: SOL amount must be positive[/red]"
                )
                return
        except ValueError:
            self.query_one("#ca-badge", Static).update(
                "[red]Error: Invalid SOL amount[/red]"
            )
            return

        lamports = Lamports(int(amt * 1_000_000_000))
        target_addrs = tuple(self._selected_wallets)
        if not target_addrs:
            self.query_one("#ca-badge", Static).update(
                "[red]Error: Select at least 1 wallet[/red]"
            )
            return

        order = QuickBuyOrder(
            market_id=ca,
            amount_lamports=lamports,
            selected_wallet_addresses=target_addrs,
            max_slippage_bps=1000,  # 10%
            priority_tip_lamports=Lamports(2_000_000),  # 0.002 SOL Jito tip
        )
        self.dismiss(order)
