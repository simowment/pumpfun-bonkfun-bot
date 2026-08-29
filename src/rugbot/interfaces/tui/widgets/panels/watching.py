"""Watchlist column widget for the Dashboard displaying tracked root funders with intelligence scores."""

# ruff: noqa: S110, BLE001, TC002, PLR2004

from __future__ import annotations

from dataclasses import dataclass

from textual.app import ComposeResult
from textual.containers import VerticalScroll
from textual.message import Message
from textual.reactive import reactive
from textual.widget import Widget
from textual.widgets import Static

from rugbot.interfaces.tui.formatters import format_amount, short_address


@dataclass(frozen=True)
class FunderCardInfo:
    """Display information for a watched funder."""

    address: str
    label: str = "Root Funder"
    enabled: bool = True
    descendants_count: int = 0
    launches_count: int = 0
    balance_lamports: int | None = None
    tokens_count: int = 0
    score: int | None = None
    rugs_count: int = 0
    hitrate_pct: float | None = None
    avg_ath_pct: float | None = None
    funding_source: str | None = None
    funding_parent: str | None = None
    funding_amount_sol: str | None = None
    linked_wallets_count: int = 0
    bundled_count: int = 0


class WatchingView(Widget):
    """Left column widget showing live watched root funders and their stats."""

    DEFAULT_CSS = """
    WatchingView {
        width: 30;
        min-width: 26;
        max-width: 34;
        height: 100%;
        layout: vertical;
        background: $surface;
        border-right: solid $panel;
    }

    .watching-header {
        height: 1;
        width: 100%;
        padding: 0 1;
        background: $boost;
        color: $accent;
        text-style: bold;
    }

    .watching-scroll {
        height: 1fr;
        width: 100%;
        padding: 0 1;
    }
    """

    class FunderSelected(Message):
        """Dispatched when a funder card is selected."""

        def __init__(self, funder_address: str) -> None:
            super().__init__()
            self.funder_address = funder_address

    funders: reactive[list[FunderCardInfo]] = reactive(list)
    selected_funder: reactive[str | None] = reactive(None)

    def compose(self) -> ComposeResult:
        yield Static("WATCHLIST", classes="watching-header")
        with VerticalScroll(id="watching-scroll-container", classes="watching-scroll"):
            yield Static(
                self._render_content(),
                id="watching-content-static",
            )

    def watch_funders(self, _val: list[FunderCardInfo]) -> None:
        try:
            self.query_one("#watching-content-static", Static).update(
                self._render_content()
            )
        except Exception:
            pass

    def watch_selected_funder(self, _val: str | None) -> None:
        try:
            self.query_one("#watching-content-static", Static).update(
                self._render_content()
            )
        except Exception:
            pass

    def _render_content(self) -> str:
        if not self.funders:
            return (
                "[dim]No funders configured.\n"
                "Use 'Funders' tab or Settings [6] to add target dev wallets.[/dim]"
            )
        blocks: list[str] = []
        for f in self.funders:
            blocks.extend(
                self._render_funder_card(
                    f, is_selected=(f.address == self.selected_funder)
                )
            )
            blocks.append("[dim]──────────────────────────────[/dim]")
        return "\n".join(blocks).rstrip()

    def _render_funder_card(
        self, f: FunderCardInfo, *, is_selected: bool = False
    ) -> list[str]:
        prefix = "[bold cyan]> [/bold cyan]" if is_selected else ""
        addr_text = f"{prefix}[bold white]{short_address(f.address)}[/bold white]"
        lines: list[str] = []

        if f.score is not None:
            score_color = (
                "green" if f.score >= 75 else "yellow" if f.score >= 50 else "red"
            )
            lines.append(
                f"{addr_text}  [bold yellow]★[/bold yellow]  [{score_color}]{f.score}% score[/{score_color}]"
            )
        else:
            lines.append(f"{addr_text}  [dim]({f.label})[/dim]")

        if f.balance_lamports is not None:
            lines.append(
                f" [dim]balance[/dim]    [bold white]{format_amount(f.balance_lamports)}[/bold white]"
            )
        if f.tokens_count > 0:
            lines.append(
                f" [dim]holdings[/dim]   [white]{f.tokens_count} tokens held[/white]"
            )

        lines.append(f" [white]{f.launches_count} recorded launches[/white]")
        lines.append(f" [cyan]{f.descendants_count} tracked descendants[/cyan]")

        if f.hitrate_pct is not None:
            lines.append(f" [green]{f.hitrate_pct:.0f}% hitrate[/green]")
        if f.avg_ath_pct is not None:
            lines.append(f" [cyan]+{f.avg_ath_pct:.0f}% avg ATH[/cyan]")

        if f.funding_source or f.funding_parent or f.funding_amount_sol:
            lines.append(" [bold yellow]Funding Provenance[/bold yellow]")
            if f.funding_source:
                lines.append(f" ├─ [white]{f.funding_source}[/white]")
            if f.funding_parent:
                lines.append(f" ├─ [cyan]{f.funding_parent}[/cyan]")
            if f.funding_amount_sol:
                lines.append(f" └─ [bold white]{f.funding_amount_sol}[/bold white]")

        return lines

    def set_funders_info(self, funders_info: list[FunderCardInfo]) -> None:
        """Update the list of displayed funder cards."""
        self.funders = list(funders_info)
        if not self.selected_funder and self.funders:
            self.selected_funder = self.funders[0].address

    def on_click(self) -> None:
        """Cycle or select funder on click."""
        if self.funders:
            if not self.selected_funder:
                self.selected_funder = self.funders[0].address
            self.post_message(self.FunderSelected(self.selected_funder))
