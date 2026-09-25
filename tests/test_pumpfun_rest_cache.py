"""Cached pump.fun REST reads: token, user coins, candles, creator index."""

import json
import urllib.request
from typing import Any
from unittest.mock import patch

import base58

import rugbot.integrations.pumpfun_creator_index as creator_index
from rugbot.integrations.pumpfun_api import PumpFunApiClient
from rugbot.integrations.rpc_cache import RpcResponseCache


def _valid_address(seed: int) -> str:
    """Build a deterministic canonical base58 Solana address."""
    raw = bytes([(seed + i) % 256 for i in range(32)])
    return base58.b58encode(raw).decode("ascii")


def test_fetch_token_served_from_cache(tmp_path: Any) -> None:
    """Second identical fetch_token call is served from cache."""
    hits: list[str] = []
    payload = {"mint": "MintX", "symbol": "TST"}

    def _fake_http(url: str) -> dict[str, Any]:
        hits.append(url)
        return dict(payload)

    cache = RpcResponseCache(db_path=tmp_path / "c.sqlite3")
    try:
        client = PumpFunApiClient(page_cache=cache)
        with patch(
            "rugbot.integrations.pumpfun_api._http_json", side_effect=_fake_http
        ):
            first = client.fetch_token("MintX")
            second = client.fetch_token("MintX")
    finally:
        cache.close()
    assert first == payload
    assert second == payload
    assert len(hits) == 1


def test_fetch_user_created_coins_served_from_cache(tmp_path: Any) -> None:
    """Second identical fetch_user_created_coins call hits HTTP once."""
    hits: list[str] = []
    payload = {"limit": 50, "offset": 0, "count": 1, "coins": [{"mint": "M"}]}

    def _fake_http(url: str) -> dict[str, Any]:
        hits.append(url)
        return dict(payload)

    cache = RpcResponseCache(db_path=tmp_path / "c.sqlite3")
    try:
        client = PumpFunApiClient(page_cache=cache)
        with patch(
            "rugbot.integrations.pumpfun_api._http_json", side_effect=_fake_http
        ):
            first = client.fetch_user_created_coins("WalletA")
            second = client.fetch_user_created_coins("WalletA")
    finally:
        cache.close()
    assert first == second
    assert len(hits) == 1


def test_fetch_candlesticks_list_round_trips(tmp_path: Any) -> None:
    """A candlestick list round-trips through the {"result": ...} wrapper."""
    hits: list[str] = []
    candles = [
        {
            "timestamp": 1_700_000_000_000,
            "open": 1.0,
            "high": 2.0,
            "low": 0.5,
            "close": 1.5,
            "volume": 10.0,
        }
    ]

    def _fake_http(url: str) -> list[dict[str, Any]]:
        hits.append(url)
        return [dict(c) for c in candles]

    cache = RpcResponseCache(db_path=tmp_path / "c.sqlite3")
    try:
        client = PumpFunApiClient(page_cache=cache)
        with patch(
            "rugbot.integrations.pumpfun_api._http_json", side_effect=_fake_http
        ):
            first = client.fetch_candlesticks("MintX")
            second = client.fetch_candlesticks("MintX")
        stored = cache.lookup(
            "pumpfun/candles",
            {"mint": "MintX", "interval": "1s", "limit": 300, "created_ts": 0},
        )
    finally:
        cache.close()
    assert first == candles
    assert second == candles
    assert len(hits) == 1
    assert stored == {"result": candles}


def test_creator_index_first_page_cached(tmp_path: Any) -> None:
    """A creator-index first-page call is cached within TTL."""
    creator = _valid_address(7)
    mint = _valid_address(9)
    rows = [
        {
            "mint": mint,
            "creator": creator,
            "name": "Test",
            "symbol": "TST",
            "created_timestamp": 123,
        }
    ]
    calls: list[str] = []

    class _FakeResponse:
        def __init__(self, payload: object) -> None:
            self._raw = json.dumps(payload).encode("utf-8")

        def read(self) -> bytes:
            return self._raw

        def __enter__(self) -> Any:
            return self

        def __exit__(self, *args: Any) -> bool:
            return False

    def _fake_urlopen(request: Any, timeout: Any = None) -> Any:
        calls.append(str(request.full_url))
        return _FakeResponse(rows)

    cache = RpcResponseCache(db_path=tmp_path / "c.sqlite3")
    try:
        with (
            patch.object(creator_index, "_shared_cache", return_value=cache),
            patch.object(urllib.request, "urlopen", side_effect=_fake_urlopen),
        ):
            first = creator_index.fetch_pumpfun_created_tokens(creator)
            second = creator_index.fetch_pumpfun_created_tokens(creator)
    finally:
        cache.close()
    assert [t.mint for t in first] == [mint]
    assert first == second
    assert len(calls) == 1
