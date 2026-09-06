"""Unit tests for Pump.fun trade-page caching (no network)."""

import urllib.error
from typing import Any
from unittest.mock import patch

from rugbot.integrations.pumpfun_api import PumpFunApiClient
from rugbot.integrations.rpc_cache import RpcResponseCache


def _page(trades: list[dict[str, Any]], cursor: str | None = None) -> dict[str, Any]:
    """Build one canned trade-history page."""
    pagination: dict[str, Any] = {"hasMore": cursor is not None}
    if cursor is not None:
        pagination["nextCursor"] = cursor
    return {"trades": trades, "pagination": pagination}


def _trade(user: str) -> dict[str, Any]:
    """Build one canned trade row."""
    return {"userAddress": user, "type": "sell", "priceSol": "0.00000001"}


def test_repeat_page_call_hits_network_once(tmp_path: Any) -> None:
    """An identical repeat call is served from cache (1 HTTP hit)."""
    hits: list[str] = []
    payload = _page([_trade("TraderA")])

    def _fake_http(url: str) -> dict[str, Any]:
        """Count HTTP hits while serving the canned page."""
        hits.append(url)
        return payload

    cache = RpcResponseCache(db_path=tmp_path / "pages.sqlite3")
    try:
        client = PumpFunApiClient(page_cache=cache)
        with patch(
            "rugbot.integrations.pumpfun_api._http_json", side_effect=_fake_http
        ):
            first = client.fetch_trades("MintX", limit=50)
            second = client.fetch_trades("MintX", limit=50)
    finally:
        cache.close()
    assert first == second
    assert len(hits) == 1


def test_cursor_page_served_from_cache_without_expiry(tmp_path: Any) -> None:
    """A stored cursor page survives (immutable fills never expire)."""
    hits: list[str] = []
    payload = _page([_trade("Ancient")], cursor="9")

    def _fake_http(url: str) -> dict[str, Any]:
        """Count HTTP hits while serving the canned cursor page."""
        hits.append(url)
        return payload

    cache = RpcResponseCache(db_path=tmp_path / "pages.sqlite3")
    try:
        client = PumpFunApiClient(page_cache=cache)
        with patch(
            "rugbot.integrations.pumpfun_api._http_json", side_effect=_fake_http
        ):
            client.fetch_trades("MintX", limit=50, cursor="3")
        with patch(
            "rugbot.integrations.pumpfun_api._http_json", side_effect=_fake_http
        ):
            replay = client.fetch_trades("MintX", limit=50, cursor="3")
    finally:
        cache.close()
    assert replay["trades"] == [_trade("Ancient")]
    assert len(hits) == 1


def test_failed_fetch_is_never_cached(tmp_path: Any) -> None:
    """A failed fetch returns empty twice with two HTTP hits, nothing stored."""
    hits: list[str] = []

    def _failing_http(url: str) -> dict[str, Any]:
        """Count HTTP hits while always failing."""
        hits.append(url)
        raise urllib.error.HTTPError(url, 429, "throttled", None, None)

    cache = RpcResponseCache(db_path=tmp_path / "pages.sqlite3")
    try:
        client = PumpFunApiClient(page_cache=cache)
        with patch(
            "rugbot.integrations.pumpfun_api._http_json", side_effect=_failing_http
        ):
            assert client.fetch_trades("MintX", limit=50) == {
                "trades": [],
                "pagination": {},
            }
        with patch(
            "rugbot.integrations.pumpfun_api._http_json", side_effect=_failing_http
        ):
            assert client.fetch_trades("MintX", limit=50) == {
                "trades": [],
                "pagination": {},
            }
    finally:
        cache.close()
    assert len(hits) == 2
