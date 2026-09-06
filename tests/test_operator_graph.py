"""Unit tests for the free trade-graph operator traversal (no network)."""

from typing import Any

from rugbot.tracker import operator_graph as graph_module
from rugbot.tracker.operator_graph import (
    MAX_CREATOR_CHECKS,
    find_operator_links,
    operator_links_to_json,
)


class _FakePumpClient:
    """Fake public API client serving canned coins and trades."""

    def __init__(
        self,
        coins_by_wallet: dict[str, dict[str, Any]],
        trades_by_mint: dict[str, list[dict[str, Any]]],
        failures: set[str] | None = None,
    ) -> None:
        """Record fixture listings, trade pages, and failing methods."""
        self._coins = coins_by_wallet
        self._trades = trades_by_mint
        self._failures = failures or set()
        self.calls = 0

    def fetch_user_created_coins(
        self, wallet: str, limit: int = 50, offset: int = 0
    ) -> dict[str, Any]:
        """Serve the canned coin listing while counting public calls."""
        self.calls += 1
        if "coins" in self._failures:
            raise RuntimeError("coins unavailable")  # noqa: TRY003
        return self._coins.get(
            wallet, {"limit": limit, "offset": offset, "count": 0, "coins": []}
        )

    def fetch_trades(
        self, mint: str, limit: int = 200, cursor: str | None = None
    ) -> dict[str, Any]:
        """Serve one canned trade page without cursor paging."""
        self.calls += 1
        if "trades" in self._failures:
            raise RuntimeError("trades unavailable")  # noqa: TRY003
        return {"trades": self._trades.get(mint, []), "pagination": {}}


def _coins(*mints: str, count: int | None = None) -> dict[str, Any]:
    """Build one canned creator listing for the given mints."""
    coins = [{"mint": mint} for mint in mints]
    return {
        "limit": 50,
        "offset": 0,
        "count": count if count is not None else len(coins),
        "coins": coins,
    }


def _trade(user: str, side: str = "sell") -> dict[str, Any]:
    """Build one canned trade row for a co-trader."""
    return {"userAddress": user, "type": side, "priceSol": "0.00000001"}


def test_co_trader_with_own_mints_links() -> None:
    """A dumper with created coins links through the shared mint."""
    client = _FakePumpClient(
        {
            "SeedDev": _coins("MintA"),
            "DumperX": _coins("MintB", count=3),
            "Random": _coins(),
        },
        {"MintA": [_trade("Fresh1"), _trade("DumperX"), _trade("Random")]},
    )
    links, warning, calls = find_operator_links("SeedDev", client=client)
    assert warning is None
    assert calls == 1 + 1 + 3
    linked = {link.wallet: link for link in links}
    assert set(linked) == {"Fresh1", "DumperX", "Random"}
    assert linked["DumperX"].linked is True
    assert linked["DumperX"].via_mint == "MintA"
    assert linked["DumperX"].created_count == 3
    assert linked["Random"].linked is False
    assert links[0].wallet == "DumperX"
    payload = operator_links_to_json(links)
    assert payload[0]["source"] == "trade-graph"


def test_seed_wallet_excluded_from_candidates() -> None:
    """The seed's own trades never link back to itself."""
    client = _FakePumpClient(
        {"SeedDev": _coins("MintA")},
        {"MintA": [_trade("SeedDev", "buy"), _trade("SeedDev", "sell")]},
    )
    links, _, _ = find_operator_links("SeedDev", client=client)
    assert links == []


def test_empty_trades_yield_empty_graph_with_note() -> None:
    """Mints without indexed trades report an empty graph, not failure."""
    client = _FakePumpClient({"SeedDev": _coins("MintA")}, {})
    links, warning, _ = find_operator_links("SeedDev", client=client)
    assert links == []
    assert warning is None


def test_creator_checks_capped_on_busy_mint() -> None:
    """Co-trader lookups stop at the cap with an honest warning."""
    traders = {f"Trader{i:02d}": _trade(f"Trader{i:02d}") for i in range(75)}
    client = _FakePumpClient(
        {"SeedDev": _coins("MintA")},
        {"MintA": list(traders.values())},
    )
    links, warning, _ = find_operator_links("SeedDev", client=client)
    assert len(links) == MAX_CREATOR_CHECKS
    assert warning is not None and "capped" in warning


def test_failed_listing_degrades_to_partial_with_warning() -> None:
    """A dead creator endpoint returns partial results plus a warning."""
    client = _FakePumpClient({}, {}, failures={"coins"})
    links, warning, calls = find_operator_links("SeedDev", client=client)
    assert links == []
    assert warning is not None
    assert calls == 1


class _PagedPumpClient(_FakePumpClient):
    """Fake client serving multi-page newest-first trade histories."""

    def __init__(
        self,
        coins_by_wallet: dict[str, dict[str, Any]],
        pages_by_mint: dict[str, list[list[dict[str, Any]]]],
    ) -> None:
        """Record per-mint page lists indexed by cursor position."""
        super().__init__(coins_by_wallet, {})
        self._pages = pages_by_mint

    def fetch_trades(
        self, mint: str, limit: int = 200, cursor: str | None = None
    ) -> dict[str, Any]:
        """Serve the cursor-indexed page with a next-page cursor."""
        self.calls += 1
        pages = self._pages.get(mint, [])
        index = int(cursor) if cursor is not None else 0
        if index >= len(pages):
            return {"trades": [], "pagination": {}}
        if index + 1 < len(pages):
            pagination: dict[str, Any] = {
                "hasMore": True,
                "nextCursor": str(index + 1),
            }
        else:
            pagination = {"hasMore": False}
        return {"trades": pages[index], "pagination": pagination}


def test_oldest_page_traders_rank_first() -> None:
    """Block-0 traders on the last page outrank newest-page traders."""
    client = _PagedPumpClient(
        {"SeedDev": _coins("MintA"), "Ancient": _coins("MintOld", count=2)},
        {
            "MintA": [
                [_trade("Newcomer")],
                [_trade("Middle")],
                [_trade("Ancient", "buy"), _trade("SeedDev", "buy")],
            ]
        },
    )
    links, warning, _ = find_operator_links("SeedDev", client=client)
    assert warning is None
    linked = {link.wallet: link for link in links}
    assert set(linked) == {"Newcomer", "Middle", "Ancient"}
    assert linked["Ancient"].linked is True
    assert linked["Ancient"].created_count == 2
    assert links[0].wallet == "Ancient"


def test_graph_call_budget_bounds_paging(
    monkeypatch: Any,
) -> None:
    """Exhaustion paging stops at the call budget with honest accounting."""
    pages = [[_trade(f"Trader{i}")] for i in range(50)]
    client = _PagedPumpClient({"SeedDev": _coins("MintA")}, {"MintA": pages})
    monkeypatch.setattr(graph_module, "MAX_GRAPH_CALLS", 5)
    links, _, calls = find_operator_links("SeedDev", client=client)
    assert calls <= 5 + 15
    assert len(links) <= 15
