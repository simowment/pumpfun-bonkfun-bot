"""High-density, factual on-chain cluster graph and bundle intelligence widget."""

from __future__ import annotations

from typing import TYPE_CHECKING

from rich import box
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from rich.tree import Tree
from textual.widgets import Static

from rugbot.interfaces.tui.formatters import format_age, short_address

if TYPE_CHECKING:
    from rugbot.storage.tracker import SQLiteTrackerRepository
    from rugbot.tracker.models import LaunchRecord, TargetExecutionPolicy, WalletRecord


class ClusterGraphWidget(Static):
    """Rich visual tree and matrix displaying real developer funding, token creations, and satellite wallets."""

    DEFAULT_CSS = """
    ClusterGraphWidget {
        width: 100%;
        height: auto;
        layout: vertical;
        background: #0d1117;
        margin-bottom: 1;
    }
    """

    def __init__(
        self,
        dev_address: str = "",
        dev_label: str = "",
        **kwargs: object,
    ) -> None:
        super().__init__(**kwargs)
        self._dev_address = dev_address
        self._dev_label = dev_label

    def update_cluster(
        self,
        dev_address: str,
        dev_label: str,
        repository: SQLiteTrackerRepository | None = None,
    ) -> None:
        """Update cluster visual rendering from durable repository records."""
        self._dev_address = dev_address
        self._dev_label = dev_label
        self.update(self._render_real_cluster_tree(repository))

    def on_mount(self) -> None:
        repo = getattr(self.app, "_repository", None)
        self.update(self._render_real_cluster_tree(repo))

    def _render_real_cluster_tree(
        self, repository: SQLiteTrackerRepository | None
    ) -> Table:
        """Render cluster intelligence based strictly on durable SQLite records."""
        grid = Table(
            box=box.ROUNDED,
            show_header=True,
            header_style="bold cyan",
            expand=True,
            border_style="#58a6ff",
            padding=(0, 1),
        )
        grid.add_column("👑 TARGET DEV & RECORD", ratio=3)
        grid.add_column("🪙 RECORDED TOKEN LAUNCHES", ratio=3)
        grid.add_column("⚡ LINKED SATELLITE WALLETS", ratio=4)

        if not self._dev_address:
            grid.add_row(
                Panel(
                    "[dim]No dev address selected[/dim]",
                    border_style="dim",
                    box=box.ROUNDED,
                ),
                Panel(
                    "[dim]No token launch recorded[/dim]",
                    border_style="dim",
                    box=box.ROUNDED,
                ),
                Panel(
                    "[dim]No satellite wallets found[/dim]",
                    border_style="dim",
                    box=box.ROUNDED,
                ),
            )
            return grid

        launches: tuple[LaunchRecord, ...] = ()
        wallets: tuple[WalletRecord, ...] = ()
        policy: TargetExecutionPolicy | None = None

        if repository is not None:
            launches = repository.get_launches_for_funder(self._dev_address)
            wallets = tuple(
                w
                for w in repository.get_wallets()
                if w.root_funder == self._dev_address
            )
            policy = repository.get_target_execution_policy(self._dev_address)

        # 1. Column 1: Root Dev Identity & Policy
        dev_text = Text()
        label_display = self._dev_label if self._dev_label else "Target Dev"
        dev_text.append(f"Label: {label_display}\n", style="bold gold1")
        dev_text.append(f"Pubkey: {short_address(self._dev_address)}\n", style="cyan")
        dev_text.append(f"Full: {self._dev_address}\n", style="dim")
        dev_text.append(f"Launches In DB: {len(launches)}\n", style="bold white")
        dev_text.append(f"Linked Wallets: {len(wallets)}\n", style="bold white")

        if policy is not None:
            mode_color = "red" if policy.execution_mode.value == "live" else "yellow"
            dev_text.append(
                f"Mode: [{mode_color}]{policy.execution_mode.value.upper()}[/{mode_color}]\n"
            )
            dev_text.append(
                f"Buy Size: {policy.quote_size_lamports / 1_000_000_000:.3f} SOL\n",
                style="white",
            )
            dev_text.append(
                f"TP: +{policy.take_profit_pnl_ppm / 1_000:.1f}%\n", style="green"
            )
        else:
            dev_text.append("Policy: [dim]Unconfigured (Edit with 'E')[/dim]\n")

        col1_panel = Panel(
            dev_text,
            title="[bold yellow]🎯 TARGET IDENTITY & POLICY[/bold yellow]",
            border_style="gold1",
            box=box.ROUNDED,
        )

        # 2. Column 2: Recorded Launches Tree
        token_tree = Tree("🪙 [bold green]Durable Token Records[/bold green]")
        if launches:
            for launch in launches[:6]:
                token_label = (
                    f"[bold white]{launch.name}[/bold white] [cyan]${launch.symbol}[/cyan]"
                    if launch.name and launch.name != launch.symbol
                    else f"[bold white]${launch.symbol}[/bold white]"
                )
                node = token_tree.add(
                    f"🪙 {token_label} ({short_address(launch.mint)})"
                )
                node.add(f"• Slot: {launch.created_slot:,}")
                node.add(f"• Age: {format_age(launch.created_at)}")
        else:
            token_tree.add("[dim]No launches captured yet for this dev.[/dim]")
            token_tree.add("[dim]Bot will detect new launches 24/7 in real-time.[/dim]")

        col2_panel = Panel(
            token_tree,
            title=f"[bold green]📊 TOKEN CREATIONS ({len(launches)})[/bold green]",
            border_style="green",
            box=box.ROUNDED,
        )

        # 3. Column 3: Linked Satellites Tree
        bundle_tree = Tree(
            f"⚡ [bold cyan]Discovered Satellites ({len(wallets)})[/bold cyan]"
        )
        if wallets:
            for w in wallets[:6]:
                w_node = bundle_tree.add(
                    f"⚡ [bold cyan]{short_address(w.address)}[/bold cyan]"
                )
                w_node.add(f"• Depth: {w.depth} · Status: {w.status.value.upper()}")
        else:
            bundle_tree.add("[dim]No satellite wallets linked yet.[/dim]")
            bundle_tree.add(
                "[dim]On-chain transfer tracker will discover tree on funding.[/dim]"
            )

        col3_panel = Panel(
            bundle_tree,
            title=f"[bold red]👥 CLUSTER WALLETS ({len(wallets)})[/bold red]",
            border_style="red",
            box=box.ROUNDED,
        )

        grid.add_row(col1_panel, col2_panel, col3_panel)
        return grid
