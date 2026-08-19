"""Pure queries and path tree reconstruction algorithms for the tracker."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

from rugbot.tracker.models import (
    LAMPORTS_PER_SOL,
    FundingHop,
    FundingPath,
    LaunchRecord,
)

if TYPE_CHECKING:
    from rugbot.tracker.repository import TrackerRepository

SHORT_IDENTIFIER_LIMIT = 14


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


def build_funding_path(
    creator_address: str, repository: TrackerRepository
) -> FundingPath | None:
    """Reconstruct the cryptographic provenance chain: root -> hop1 -> hop2 -> creator."""
    wallet = repository.get_wallet(creator_address)
    if wallet is None:
        return None

    root_funder = wallet.root_funder
    hops: list[FundingHop] = []
    curr = creator_address
    visited: set[str] = set()

    while curr != root_funder and curr not in visited:
        visited.add(curr)
        parent_transfer = repository.get_parent_transfer(curr)
        if parent_transfer is None:
            break
        hops.append(
            FundingHop(
                from_wallet=parent_transfer.from_wallet,
                to_wallet=parent_transfer.to_wallet,
                amount_lamports=parent_transfer.amount_lamports,
                amount_sol=parent_transfer.amount_sol,
                signature=parent_transfer.signature,
                timestamp=parent_transfer.timestamp,
                depth=parent_transfer.depth,
            )
        )
        curr = parent_transfer.from_wallet

    hops.reverse()
    total_depth = len(hops)
    last_ts = hops[-1].timestamp if hops else None
    launches = repository.get_launches_for_funder(root_funder)
    launch = next((L for L in launches if L.creator_wallet == creator_address), None)
    launch_ts = launch.created_at if launch else None
    time_to_launch = (
        (launch_ts - last_ts)
        if (launch_ts and last_ts and launch_ts >= last_ts)
        else None
    )

    return FundingPath(
        root_funder=root_funder,
        creator_wallet=creator_address,
        hops=tuple(hops),
        total_depth=total_depth,
        last_funding_timestamp=last_ts,
        launch_timestamp=launch_ts,
        time_to_launch_seconds=time_to_launch,
    )


def format_path_tree(
    path: FundingPath | None, launch: LaunchRecord | None = None
) -> str:
    """Build the clean deterministic ASCII tree connecting root funder to creator and token."""
    if path is None or not path.hops:
        if launch is not None:
            return f"ROOT {short_address(launch.root_funder)}\n  └─ CREATE {launch.symbol} ({short_address(launch.mint)})"
        return "No funding path recorded."

    lines: list[str] = [f"FUNDER  {path.root_funder}"]
    indent = "  "
    for hop in path.hops:
        ts_str = format_timestamp(hop.timestamp)
        lines.append(
            f"{indent}└─ {hop.amount_sol} SOL → {short_address(hop.to_wallet)}        {ts_str}"
        )
        indent += "    "

    if launch is not None:
        created_ts = format_timestamp(launch.created_at)
        lines.append(
            f"{indent}└─ CREATE {launch.symbol} ({short_address(launch.mint)})        {created_ts}"
        )

    if path.time_to_launch_seconds is not None:
        lines.append(f"\nLast funding → launch: {path.time_to_launch_seconds} sec")

    return "\n".join(lines)
