"""Fake-transport tests for the Solana RPC response cache (no network)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from rugbot.integrations.rpc_cache import (
    VOLATILE_TTL_SECONDS,
    RpcResponseCache,
    is_immutable_request,
)
from rugbot.integrations.solana_rpc import SolanaClient


@pytest.fixture
def anyio_backend() -> str:
    """Run async cache tests on asyncio."""
    return "asyncio"


class FakeClock:
    """Controllable clock for TTL tests."""

    def __init__(self, now: float = 1_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


class FakeNetwork:
    """Counting fake network transport."""

    def __init__(self, handler: Any) -> None:
        self.handler = handler
        self.calls = 0

    async def __call__(self, method: str, params: Any) -> dict[str, Any]:
        self.calls += 1
        return await self.handler(method, params)


async def cached_fetch(
    cache: RpcResponseCache,
    network: FakeNetwork,
    method: str,
    params: Any,
) -> dict[str, Any]:
    """Emulate the transport policy: cache-first, store only on success."""
    cached = cache.lookup(method, params)
    if cached is not None:
        return cached
    response = await network(method, params)
    cache.store(method, params, response)
    return response


def _cache(tmp_path: Path, **kwargs: Any) -> RpcResponseCache:
    return RpcResponseCache(db_path=tmp_path / "rpc_cache.sqlite3", **kwargs)


@pytest.mark.anyio
async def test_finalized_call_makes_zero_network_hits_on_repeat(
    tmp_path: Path,
) -> None:
    """Second identical finalized call is served from cache (1 network hit)."""
    cache = _cache(tmp_path)
    params = ["sig123", {"encoding": "jsonParsed", "commitment": "finalized"}]

    async def handler(method: str, params: Any) -> dict[str, Any]:
        return {"jsonrpc": "2.0", "id": 1, "result": {"slot": 42}}

    network = FakeNetwork(handler)
    first = await cached_fetch(cache, network, "getTransaction", params)
    second = await cached_fetch(cache, network, "getTransaction", params)
    assert first == second
    assert network.calls == 1
    assert is_immutable_request("getTransaction", params) is True
    cache.close()


@pytest.mark.anyio
async def test_failures_are_not_cached(tmp_path: Path) -> None:
    """Network errors never poison the cache; the next call retries."""
    cache = _cache(tmp_path)
    params = ["sig9", {"encoding": "jsonParsed", "commitment": "finalized"}]
    attempts = {"count": 0}

    async def handler(method: str, params: Any) -> dict[str, Any]:
        attempts["count"] += 1
        if attempts["count"] == 1:
            raise RuntimeError("boom")
        return {"jsonrpc": "2.0", "id": 1, "result": {"slot": 7}}

    network = FakeNetwork(handler)
    with pytest.raises(RuntimeError):
        await cached_fetch(cache, network, "getTransaction", params)
    result = await cached_fetch(cache, network, "getTransaction", params)
    assert result["result"] == {"slot": 7}
    assert network.calls == 2
    assert cache.lookup("getTransaction", params) == result
    cache.close()


@pytest.mark.anyio
async def test_ttl_expiry_refetches_volatile_calls(tmp_path: Path) -> None:
    """Slot-dependent calls hit cache within TTL and refetch after expiry."""
    clock = FakeClock()
    cache = _cache(tmp_path, now_fn=clock)

    async def handler(method: str, params: Any) -> dict[str, Any]:
        return {"jsonrpc": "2.0", "id": 1, "result": 999}

    network = FakeNetwork(handler)
    await cached_fetch(cache, network, "getSlot", None)
    await cached_fetch(cache, network, "getSlot", None)
    assert network.calls == 1
    clock.now += VOLATILE_TTL_SECONDS + 1.0
    await cached_fetch(cache, network, "getSlot", None)
    assert network.calls == 2
    cache.close()


class _FakeResponse:
    def __init__(self, payload: dict[str, Any]) -> None:
        self.status = 200
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    async def json(self) -> dict[str, Any]:
        return self._payload


class _FakePostContext:
    def __init__(self, payload: dict[str, Any], session: _FakeSession) -> None:
        self._payload = payload
        self._session = session

    async def __aenter__(self) -> _FakeResponse:
        self._session.posts += 1
        return _FakeResponse(self._payload)

    async def __aexit__(self, *args: Any) -> bool:
        return False


class _FakeSession:
    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload
        self.posts = 0

    def post(self, *args: Any, **kwargs: Any) -> _FakePostContext:
        return _FakePostContext(self._payload, self)


class _NoopLimiter:
    async def acquire(self) -> None:
        return None


@pytest.mark.anyio
async def test_solana_client_post_rpc_serves_repeat_from_cache(
    tmp_path: Path,
) -> None:
    """SolanaClient.post_rpc makes one network POST for two identical calls."""
    payload = {"jsonrpc": "2.0", "id": 1, "result": {"slot": 123}}
    cache = _cache(tmp_path)
    client = SolanaClient("http://localhost:8899")
    client._rpc_cache = cache
    client._rate_limiter = _NoopLimiter()  # type: ignore[assignment]
    session = _FakeSession(payload)

    async def fake_get_session() -> Any:
        return session

    client._get_session = fake_get_session  # type: ignore[method-assign]
    body = {"jsonrpc": "2.0", "id": 1, "method": "getSlot"}
    first = await client.post_rpc(body)
    second = await client.post_rpc(body)
    assert first == payload
    assert second == payload
    assert session.posts == 1
    await client.close()
