"""Pure formatting and string utility functions for the TUI."""

# ruff: noqa: PLR2004

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

from rugbot.tracker.models import LAMPORTS_PER_SOL

if TYPE_CHECKING:
    from rugbot.runtime.wallet_intelligence import (
        WalletIntelligenceReport,
        WalletLaunch,
    )

SHORT_IDENTIFIER_LIMIT = 14


def format_age(timestamp: int | None, current_timestamp: int | None = None) -> str:
    """Format the relative elapsed age dynamically (e.g. '0s', '3s', '45s', '2m', '1h 12m')."""
    if timestamp is None or timestamp <= 0:
        return "—"
    now_ts = (
        current_timestamp
        if current_timestamp is not None
        else int(datetime.now(UTC).timestamp())
    )
    elapsed = max(0, now_ts - timestamp)

    if elapsed < 60:
        return f"{elapsed}s"
    if elapsed < 3600:
        mins = elapsed // 60
        return f"{mins}m"
    if elapsed < 86400:
        hours = elapsed // 3600
        mins = (elapsed % 3600) // 60
        return f"{hours}h {mins}m" if mins else f"{hours}h"
    days = elapsed // 86400
    return f"{days}d"


def format_amount(lamports: int | None) -> str:
    """Format exact integer lamports as an explicit SOL string (e.g. '3.20 SOL' or '—')."""
    if lamports is None or lamports <= 0:
        return "—"
    return f"{format_sol(lamports)} SOL"


def format_sol(lamports: int) -> str:
    """Format exact integer lamports as a readable decimal SOL string."""
    if type(lamports) is not int or lamports <= 0:
        return "0"
    whole = lamports // LAMPORTS_PER_SOL
    fraction = f"{lamports % LAMPORTS_PER_SOL:09d}".rstrip("0")
    return f"{whole}.{fraction}" if fraction else str(whole)


def short_address(address: str | None) -> str:
    """Shorten an address or signature for compact table columns."""
    if address is None:
        return "--"
    if len(address) <= SHORT_IDENTIFIER_LIMIT:
        return address
    return f"{address[:6]}...{address[-6:]}"


def format_timestamp(ts: int | None) -> str:
    """Format a unix timestamp as HH:MM:SS."""
    if not ts or ts <= 0:
        return "--:--:--"
    return datetime.fromtimestamp(ts, tz=UTC).strftime("%H:%M:%S")


def format_network_endpoint(endpoint: str) -> str:
    """Extract host name from RPC endpoint URL."""
    without_scheme = endpoint.split("://", 1)[-1]
    host_and_port = without_scheme.split("/", 1)[0]
    return host_and_port.split("?", 1)[0]


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


def format_assessment(report: WalletIntelligenceReport) -> str:
    """Format quick assessment summary."""
    if report.wallet_switch_candidate:
        return "QUALIFIED SWITCH CANDIDATE"
    return "NOT QUALIFIED"


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
