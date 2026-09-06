"""Free operator-graph traversal over public Pump.fun trade histories.

No RPC, no API key: links wallets that create tokens AND trade each
other's launches, using only the public swap/frontend APIs. This finds
rotating operators whose funding edges are invisible when indexed APIs
are unavailable — a trader that dumps the seed's mints within seconds
and has its own created coins is economically linked regardless of
whether the SOL funding transfer itself was observed.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from rugbot.integrations.pumpfun_api import get_client
from rugbot.utils.logger import get_logger

logger = get_logger(__name__)

MAX_GRAPH_MINTS = 10
# Paging runs oldest-bound: newest-first pages are followed to exhaustion
# so the block-0 cluster surface (always at the tail of history) is
# reached. The per-mint page backstop only fires on pathological
# histories; the total call budget is the real bound (free REST calls,
# so the budget prices time, not quota).
MAX_TRADE_PAGES_PER_MINT = 100
MAX_GRAPH_CALLS = 200
# Proven live: limit=50 returns full pages while limit=200 returns empty,
# despite the documented 200 max. Stay on the working size and paginate.
MAX_TRADES_PER_PAGE = 50
MAX_CREATOR_CHECKS = 60
MAX_LINKED_MINTS_PER_WALLET = 5


class PumpGraphClient(Protocol):
    """Minimal public-API surface used by the operator traversal."""

    def fetch_user_created_coins(
        self, wallet: str, limit: int, offset: int
    ) -> dict[str, object]:
        """Return the creator listing mapping for one wallet."""
        ...  # pragma: no cover - protocol stub

    def fetch_trades(
        self, mint: str, limit: int, cursor: str | None
    ) -> dict[str, object]:
        """Return one trade-history page mapping for one mint."""
        ...  # pragma: no cover - protocol stub


@dataclass(frozen=True, slots=True)
class OperatorLink:
    """One wallet economically linked to the seed operator."""

    wallet: str
    via_mint: str
    created_mints: tuple[str, ...] = ()
    created_count: int = 0
    linked: bool = False
    source: str = "trade-graph"


@dataclass(slots=True)
class _GraphScan:
    """Counting public-API accessor shared by the traversal stages."""

    api: PumpGraphClient
    calls: int = 0

    def read(self, fn_name: str, *args: object, **kwargs: object) -> dict | None:
        """Invoke one public call, counting it and degrading on failure."""
        self.calls += 1
        try:
            result = getattr(self.api, fn_name)(*args, **kwargs)
        except Exception as exc:  # noqa: BLE001 — graph is additive fail-soft
            logger.warning("operator graph %s failed: %s", fn_name, exc)
            return None
        return result if isinstance(result, dict) else None


def find_operator_links(
    seed_wallet: str,
    *,
    client: PumpGraphClient | None = None,
    max_mints: int = MAX_GRAPH_MINTS,
    max_creator_checks: int = MAX_CREATOR_CHECKS,
) -> tuple[list[OperatorLink], str | None, int]:
    """Link rotating operator wallets through public trade histories.

    For each of the seed's mints (capped), pages the public trade history
    newest-to-oldest and ranks unique co-traders earliest-first; the
    earliest co-traders that have created coins of their own are reported
    as linked. Every public call is counted; any failure degrades to a
    partial result with an honest warning instead of raising.
    """

    scan = _GraphScan(api=client if client is not None else get_client())
    mints, mints_warning = _seed_mints(scan, seed_wallet, max_mints)
    if not mints:
        return [], mints_warning, scan.calls
    rank, via = _collect_co_traders(scan, mints)
    rank.pop(seed_wallet, None)
    ordered = sorted(rank, key=lambda wallet: rank[wallet], reverse=True)
    links = _check_creators(scan, ordered[:max_creator_checks], via)
    warnings = [note for note in (mints_warning, _cap_note(ordered, links)) if note]
    links.sort(key=lambda link: (not link.linked, -link.created_count))
    return links, "; ".join(warnings) if warnings else None, scan.calls


def _seed_mints(
    scan: _GraphScan, seed_wallet: str, max_mints: int
) -> tuple[list[str], str | None]:
    """List the seed's indexed mints, capped (pure traversal input)."""
    payload = scan.read("fetch_user_created_coins", seed_wallet, limit=max_mints)
    if payload is None:
        return [], "operator graph unavailable: creator listing failed"
    raw_coins = payload.get("coins")
    if not isinstance(raw_coins, list):
        return [], "operator graph unavailable: creator listing malformed"
    mints: list[str] = []
    for coin in raw_coins[:max_mints]:
        if not isinstance(coin, dict):
            continue
        mint = coin.get("mint", coin.get("address"))
        if isinstance(mint, str) and mint and mint not in mints:
            mints.append(mint)
    if not mints:
        return [], "operator graph empty: seed has no indexed mints"
    return mints, None


def _check_creators(
    scan: _GraphScan, ordered: list[str], via: dict[str, str]
) -> list[OperatorLink]:
    """Check the earliest co-traders for their own created coins."""
    links: list[OperatorLink] = []
    for trader in ordered:
        listing = scan.read("fetch_user_created_coins", trader, limit=5)
        created, created_count = _created_summary(listing)
        links.append(
            OperatorLink(
                wallet=trader,
                via_mint=via.get(trader, ""),
                created_mints=created,
                created_count=created_count,
                linked=bool(created or created_count > 0),
            )
        )
    return links


def _created_summary(listing: dict | None) -> tuple[tuple[str, ...], int]:
    """Summarize one creator listing into mints plus a count."""
    if not isinstance(listing, dict):
        return (), 0
    found: list[str] = []
    raw = listing.get("coins")
    if isinstance(raw, list):
        for coin in raw:
            if not isinstance(coin, dict):
                continue
            mint_value = coin.get("mint", coin.get("address"))
            if isinstance(mint_value, str) and mint_value:
                found.append(mint_value)
    count = listing.get("count")
    if not isinstance(count, int) or count < 0:
        count = len(found)
    return tuple(found[:MAX_LINKED_MINTS_PER_WALLET]), count


def _cap_note(ordered: list[str], links: list[OperatorLink]) -> str | None:
    """Note when co-trader lookups were capped."""
    if len(ordered) > len(links):
        return f"operator graph capped at {len(links)} of {len(ordered)} co-traders"
    return None


def operator_links_to_json(links: list[OperatorLink]) -> list[dict[str, object]]:
    """Serialize operator links for the existing JSON pipeline."""
    return [
        {
            "wallet": link.wallet,
            "via_mint": link.via_mint,
            "created_mints": list(link.created_mints),
            "created_count": link.created_count,
            "linked": link.linked,
            "source": link.source,
        }
        for link in links
    ]


# The collector is defined below its first caller to keep each function
# under the project's complexity budget.
def _collect_co_traders(
    scan: _GraphScan, mints: list[str]
) -> tuple[dict[str, tuple[int, int]], dict[str, str]]:
    """Rank unique co-traders earliest-first across the seed's mints."""
    rank: dict[str, tuple[int, int]] = {}
    via: dict[str, str] = {}
    for mint in mints:
        if scan.calls >= MAX_GRAPH_CALLS:
            break
        cursor: str | None = None
        page_no = 0
        for _ in range(MAX_TRADE_PAGES_PER_MINT):
            if scan.calls >= MAX_GRAPH_CALLS:
                break
            page = scan.read(
                "fetch_trades", mint, limit=MAX_TRADES_PER_PAGE, cursor=cursor
            )
            if page is None:
                break
            if not _record_page_traders(page, mint, page_no, rank, via):
                break
            page_no += 1
            cursor = _next_cursor(page)
            if cursor is None:
                break
    return rank, via


def _record_page_traders(
    page: dict,
    mint: str,
    page_no: int,
    rank: dict[str, tuple[int, int]],
    via: dict[str, str],
) -> bool:
    """Record one trade page's traders; False when the page is empty."""
    trades = page.get("trades")
    if not isinstance(trades, list) or not trades:
        return False
    for index, trade in enumerate(trades):
        if not isinstance(trade, dict):
            continue
        trader = trade.get("userAddress")
        if not isinstance(trader, str) or not trader:
            continue
        position = (page_no, index)
        if trader not in rank or position > rank[trader]:
            rank[trader] = position
            via[trader] = mint
    return True


def _next_cursor(page: dict) -> str | None:
    """Extract the next page cursor, or None when paging is complete."""
    pagination = page.get("pagination")
    if not isinstance(pagination, dict):
        return None
    if not pagination.get("hasMore"):
        return None
    cursor = pagination.get("nextCursor")
    return cursor if isinstance(cursor, str) and cursor else None
