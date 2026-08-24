"""Pure formatting and string utility functions for the TUI."""

# ruff: noqa: PLR2004

from __future__ import annotations

from typing import TYPE_CHECKING

from rugbot.utils.formatters import (
    format_age,
    format_amount,
    format_network_endpoint,
    format_sol,
    format_timestamp,
    short_address,
)

if TYPE_CHECKING:
    from rugbot.intelligence.wallet_intelligence import (
        WalletIntelligenceReport,
        WalletLaunch,
    )

__all__ = [
    "format_age",
    "format_amount",
    "format_currency",
    "format_flow",
    "format_graph_map",
    "format_network_endpoint",
    "format_sol",
    "format_timestamp",
    "generate_sparkline",
    "launch_matches",
    "report_delta",
    "short_address",
]


def launch_matches(launch: WalletLaunch, query: str) -> bool:
    """Match a launch against the local table filter."""
    return (
        query
        in " ".join(
            (launch.name, launch.symbol, launch.mint, launch.creator, launch.signature)
        ).lower()
    )


def report_delta(
    previous: WalletIntelligenceReport | None,
    current: WalletIntelligenceReport,
) -> tuple[int, int]:
    """Return newly observed launches and links for the same wallet."""
    if previous is None:
        return 0, 0
    previous_launches = {launch.signature for launch in previous.launches}
    current_launches = {launch.signature for launch in current.launches}
    previous_links = {(edge.source, edge.target) for edge in previous.edges}
    current_links = {(edge.source, edge.target) for edge in current.edges}
    return (
        len(current_launches - previous_launches),
        len(current_links - previous_links),
    )


def format_flow(report: WalletIntelligenceReport) -> str:
    """Format observed native funding and distribution."""
    return " | ".join(
        (
            f"IN  {format_sol(report.native_in_lamports)} SOL",
            f"OUT  {format_sol(report.native_out_lamports)} SOL",
            f"NET  {format_sol(report.native_in_lamports - report.native_out_lamports)} SOL",
        )
    )


def format_graph_map(report: WalletIntelligenceReport) -> str:
    """Render a bounded ASCII relationship graph from observed edges."""
    lines = [
        f"TARGET  {short_address(report.target_wallet)} (as of slot {report.as_of_slot})"
    ]
    for edge in report.edges[:8]:
        direction = "<--" if edge.target == report.target_wallet else "-->"
        other = edge.source if edge.target == report.target_wallet else edge.target
        lines.append(
            f"  {direction} DIRECT  {short_address(other)} ({format_sol(edge.amount_lamports)} SOL, {edge.transfer_count} txs)"
        )
    return "\n".join(lines)


SPARK_LEVELS = (" ", "▂", "▃", "▄", "▅", "▆", "▇", "█")


def generate_sparkline(values: list[float] | tuple[float, ...] | None) -> str:
    """Generate an 8-level unicode sparkline chart from numeric series."""
    if not values:
        return "▅▅▅▅▅"
    min_v = min(values)
    max_v = max(values)
    if max_v == min_v:
        return "▅" * len(values)
    span = max_v - min_v
    result: list[str] = []
    for v in values:
        idx = int(((v - min_v) / span) * 7)
        idx = max(0, min(7, idx))
        result.append(SPARK_LEVELS[idx])
    return "".join(result)


def format_currency(val: float | int | None) -> str:
    """Format USD market cap or liquidity cleanly (e.g. $4.2k, $1.8M, $640)."""
    if val is None or val <= 0:
        return "$0"
    if val >= 1_000_000:
        return f"${val / 1_000_000:.1f}M"
    if val >= 1_000:
        return f"${val / 1_000:.1f}k"
    return f"${val:.0f}"
