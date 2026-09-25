"""Reconstruct an entity's token-creation history from its funding wallet.

The creator index only reports a token against the wallet that *created* it.
A funding wallet is never a creator, so its own launch count is always zero
and says nothing. The entity's real history is the set of tokens created by
the wallets it funded -- the burners it dispersed staging capital to.

This module turns a funding wallet's disbursements into a single token
timeline by resolving each funded wallet's creations and merging them, with
the received SOL and funding slot carried on each event so a burst is
visible.

Orchestration is side-effect free: callers inject the transfer list and the
per-wallet launch resolver.
"""

from __future__ import annotations

import dataclasses
import itertools
import statistics
from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING

from rugbot.utils.logger import get_logger

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from rugbot.tracker.funding_chain import FundedTransfer

logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class LaunchEvent:
    """One token creation attributed to a wallet the funder disbursed to."""

    mint: str
    symbol: str
    name: str
    creator: str
    funder: str
    created_at_ms: int | None
    received_sol: float
    funding_slot: int | None
    funded_at_s: int | None = None


@dataclass(frozen=True, slots=True)
class EntityLaunchHistory:
    """Merged token-creation timeline for one funding wallet's disbursal set."""

    funders: tuple[str, ...]
    recipients: int
    launches: tuple[LaunchEvent, ...]
    warning: str | None


ACTIVE_WINDOW_S = 7 * 24 * 3600


@dataclass(frozen=True, slots=True)
class LaunchActivity:
    """Whether an entity is still launching, and how often."""

    last_launch_s: int | None
    launches_last_7d: int
    median_interval_s: float | None
    active: bool


def launch_activity(history: EntityLaunchHistory, *, now_s: int) -> LaunchActivity:
    """Summarize launch recency and cadence; active means a launch in 7 days."""
    times = sorted(
        event.created_at_ms // 1000
        for event in history.launches
        if event.created_at_ms is not None
    )
    if not times:
        return LaunchActivity(None, 0, None, active=False)
    intervals = [later - earlier for earlier, later in itertools.pairwise(times)]
    recent = sum(1 for stamp in times if now_s - stamp <= ACTIVE_WINDOW_S)
    return LaunchActivity(
        last_launch_s=times[-1],
        launches_last_7d=recent,
        median_interval_s=statistics.median(intervals) if intervals else None,
        active=recent > 0,
    )


def _coin_event(
    coin: Mapping[str, object],
    *,
    creator: str,
    funder: str,
    received_sol: float,
    funding_slot: int | None,
) -> LaunchEvent | None:
    """Narrow one creator-index coin entry into a launch event."""
    mint = coin.get("mint")
    if not isinstance(mint, str) or not mint:
        return None
    symbol = coin.get("symbol")
    name = coin.get("name")
    created = coin.get("created_timestamp")
    return LaunchEvent(
        mint=mint,
        symbol=symbol if isinstance(symbol, str) else "",
        name=name if isinstance(name, str) else "",
        creator=creator,
        funder=funder,
        created_at_ms=created if isinstance(created, int) else None,
        received_sol=received_sol,
        funding_slot=funding_slot,
    )


def build_launch_history(
    funder: str,
    *,
    transfers: Sequence[FundedTransfer],
    launch_fetch: Callable[[str], Sequence[Mapping[str, object]] | None],
) -> EntityLaunchHistory:
    """Merge every funded wallet's creations into one token timeline.

    Args:
        funder: Funding wallet the disbursements came from.
        transfers: Outbound staging-band transfers observed leaving the funder.
        launch_fetch: ``wallet -> coins`` resolver (creator index page coins).

    Returns:
        EntityLaunchHistory with events sorted oldest-first and de-duplicated
        by mint. Wallets whose lookup fails contribute nothing and set a
        warning rather than aborting the merge.
    """
    totals: dict[str, float] = {}
    slots: dict[str, int | None] = {}
    funded_at: dict[str, int] = {}
    for transfer in transfers:
        totals[transfer.recipient] = (
            totals.get(transfer.recipient, 0.0) + transfer.amount_sol
        )
        slots.setdefault(transfer.recipient, transfer.slot)
        if transfer.block_time is not None:
            funded_at[transfer.recipient] = min(
                funded_at.get(transfer.recipient, transfer.block_time),
                transfer.block_time,
            )
    events: list[LaunchEvent] = []
    seen_mints: set[str] = set()
    failures = 0
    for wallet, received in totals.items():
        try:
            coins = launch_fetch(wallet)
        except Exception:  # noqa: BLE001 - one bad lookup must not abort the merge
            failures += 1
            logger.warning("launch lookup failed for %s", wallet[:8])
            continue
        for coin in coins or ():
            if not isinstance(coin, Mapping):
                continue
            event = _coin_event(
                coin,
                creator=wallet,
                funder=funder,
                received_sol=received,
                funding_slot=slots.get(wallet),
            )
            if event is None or event.mint in seen_mints:
                continue
            # A creator's tokens from before this funder paid it belong to
            # someone else's history (or an earlier life of the wallet).
            wallet_funded_at = funded_at.get(wallet)
            if (
                wallet_funded_at is not None
                and event.created_at_ms is not None
                and event.created_at_ms < wallet_funded_at * 1000
            ):
                continue
            event = dataclasses.replace(event, funded_at_s=wallet_funded_at)
            seen_mints.add(event.mint)
            events.append(event)
    events.sort(key=lambda event: event.created_at_ms or 0)
    warning = f"{failures} wallet launch lookups failed" if failures else None
    return EntityLaunchHistory(
        funders=(funder,),
        recipients=len(totals),
        launches=tuple(events),
        warning=warning,
    )


def merge_launch_histories(
    histories: Sequence[EntityLaunchHistory],
) -> EntityLaunchHistory:
    """Merge per-funder timelines into one entity token-creation timeline.

    Args:
        histories: Per-funder histories in attribution priority order.

    Returns:
        One entity history with unioned funders, summed recipients, mint
        deduplication keeping the first occurrence, and oldest-first order.
    """
    funders: list[str] = []
    for history in histories:
        for funder in history.funders:
            if funder not in funders:
                funders.append(funder)
    events: list[LaunchEvent] = []
    seen_mints: set[str] = set()
    recipients = 0
    warnings: list[str] = []
    for history in histories:
        recipients += history.recipients
        if history.warning is not None:
            warnings.append(history.warning)
        for event in history.launches:
            if event.mint in seen_mints:
                continue
            seen_mints.add(event.mint)
            events.append(event)
    events.sort(key=lambda event: event.created_at_ms or 0)
    return EntityLaunchHistory(
        funders=tuple(funders),
        recipients=recipients,
        launches=tuple(events),
        warning="; ".join(warnings) if warnings else None,
    )


__all__ = [
    "ACTIVE_WINDOW_S",
    "EntityLaunchHistory",
    "LaunchActivity",
    "LaunchEvent",
    "build_launch_history",
    "launch_activity",
    "merge_launch_histories",
]
