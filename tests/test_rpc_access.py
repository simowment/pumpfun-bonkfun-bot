"""Integration tests for the canonical RPC chokepoint.

Covers the two properties the abstraction exists to guarantee: explicit
endpoint precedence (per-call flag > .env file > inherited process env) and
provider health that accumulates across calls because one pool is shared per
endpoint set (per running event loop for async pools). Network cases run
against real local aiohttp servers rather than mocks, per the repository
verification policy.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from rugbot.backtest.runners import copytrade_backtest_runner
from rugbot.domain import market_data
from rugbot.integrations import rpc_access
from rugbot.integrations.rpc_access import (
    ENDPOINT_SOURCE_DOTENV,
    ENDPOINT_SOURCE_ENVIRON,
    ENDPOINT_SOURCE_NONE,
    ENDPOINT_SOURCE_OVERRIDE,
    RpcAccessError,
    clear_shared_pools,
    resolve_rpc_endpoints,
    rpc_health,
    shared_async_pool,
    shared_sync_pool,
    sync_rpc_result,
)
from rugbot.intelligence import token_resolver
from rugbot.runtime import config as config_module

if TYPE_CHECKING:
    from collections.abc import Iterator

    from pytest import MonkeyPatch

INHERITED_ENDPOINT = "https://inherited.example/rpc"
FILE_ENDPOINT = "https://from-file.example/rpc"
FLAG_ENDPOINT = "https://from-flag.example/rpc"
RPC_ENV_KEYS = ("SOLANA_RPC_HTTP", "SOLANA_RPC_HTTP_FALLBACKS")
# Providers that used to be appended by hardcoded fallback lists. They must
# never reappear: one of them shipped with an embedded API key in source.
FORBIDDEN_RPC_HOSTS = (
    "alchemy.com",
    "publicnode.com",
    "ankr.com",
    "mainnet-beta.solana.com",
)


@pytest.fixture(autouse=True)
def isolated_rpc_environment(monkeypatch: MonkeyPatch) -> Iterator[None]:
    """Start each test with no RPC env and no inherited pool health."""
    for key in RPC_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    clear_shared_pools()
    yield
    clear_shared_pools()


@pytest.fixture
def anyio_backend() -> str:
    """Run the local-server cases on asyncio."""
    return "asyncio"


def stub_dotenv(monkeypatch: MonkeyPatch, file_values: dict[str, str]) -> None:
    """Model ``resolve_dotenv`` as "the saved file overwrites the process env".

    Args:
        monkeypatch: Pytest patch handle.
        file_values: Values the simulated ``.env`` file contributes.
    """

    def apply_file(**_kwargs: Any) -> None:
        for key, value in file_values.items():
            os.environ[key] = value

    monkeypatch.setattr(config_module, "resolve_dotenv", apply_file)


def test_flag_override_beats_file_and_inherited_env(
    monkeypatch: MonkeyPatch,
) -> None:
    """A per-call --rpc value replaces the configured primary outright."""
    monkeypatch.setenv("SOLANA_RPC_HTTP", INHERITED_ENDPOINT)
    stub_dotenv(monkeypatch, {"SOLANA_RPC_HTTP": FILE_ENDPOINT})

    endpoints = resolve_rpc_endpoints(primary=FLAG_ENDPOINT)

    assert endpoints.ordered == (FLAG_ENDPOINT,)
    assert endpoints.primary == FLAG_ENDPOINT
    assert endpoints.source == ENDPOINT_SOURCE_OVERRIDE


def test_saved_file_beats_inherited_process_default(
    monkeypatch: MonkeyPatch,
) -> None:
    """The .env file wins over a harness-injected environment default."""
    monkeypatch.setenv("SOLANA_RPC_HTTP", INHERITED_ENDPOINT)
    stub_dotenv(monkeypatch, {"SOLANA_RPC_HTTP": FILE_ENDPOINT})

    endpoints = resolve_rpc_endpoints()

    assert endpoints.ordered == (FILE_ENDPOINT,)
    assert endpoints.source == ENDPOINT_SOURCE_DOTENV


def test_inherited_default_is_used_when_no_file_value(
    monkeypatch: MonkeyPatch,
) -> None:
    """An inherited endpoint still resolves when the file supplies nothing."""
    monkeypatch.setenv("SOLANA_RPC_HTTP", INHERITED_ENDPOINT)
    stub_dotenv(monkeypatch, {})

    endpoints = resolve_rpc_endpoints()

    assert endpoints.ordered == (INHERITED_ENDPOINT,)
    assert endpoints.source == ENDPOINT_SOURCE_ENVIRON


def test_explicit_fallbacks_replace_file_fallbacks(
    monkeypatch: MonkeyPatch,
) -> None:
    """Caller-supplied failover endpoints win, with no file leakage."""
    stub_dotenv(
        monkeypatch,
        {
            "SOLANA_RPC_HTTP": FILE_ENDPOINT,
            "SOLANA_RPC_HTTP_FALLBACKS": "https://file-fallback.example/rpc",
        },
    )

    endpoints = resolve_rpc_endpoints(
        primary=FLAG_ENDPOINT, fallbacks=("https://call-fallback.example/rpc",)
    )

    assert endpoints.ordered == (
        FLAG_ENDPOINT,
        "https://call-fallback.example/rpc",
    )


def test_unconfigured_process_fails_closed(monkeypatch: MonkeyPatch) -> None:
    """No endpoint means no request, never an implicit public RPC."""
    stub_dotenv(monkeypatch, {})

    endpoints = resolve_rpc_endpoints()

    assert endpoints.ordered == ()
    assert endpoints.primary is None
    assert endpoints.source == ENDPOINT_SOURCE_NONE
    with pytest.raises(RpcAccessError):
        sync_rpc_result("getSlot", [])
    with pytest.raises(RpcAccessError):
        shared_sync_pool(())


def test_resolved_endpoints_never_include_hardcoded_providers(
    monkeypatch: MonkeyPatch,
) -> None:
    """Resolution adds nothing the operator did not configure."""
    stub_dotenv(
        monkeypatch,
        {"SOLANA_RPC_HTTP_FALLBACKS": "https://operator-fallback.example/rpc"},
    )
    monkeypatch.setenv("SOLANA_RPC_HTTP", INHERITED_ENDPOINT)

    joined = " ".join(resolve_rpc_endpoints().ordered).lower()

    for host in FORBIDDEN_RPC_HOSTS:
        assert host not in joined


def test_rewired_modules_carry_no_hardcoded_rpc_hosts() -> None:
    """The three bypassing transports no longer name a provider in source."""
    modules = (market_data, token_resolver, copytrade_backtest_runner)
    for module in modules:
        source = Path(str(module.__file__)).read_text(encoding="utf-8").lower()
        for host in FORBIDDEN_RPC_HOSTS:
            assert host not in source, f"{module.__name__} still names {host}"
    assert not hasattr(market_data, "_ALCHEMY_FALLBACK_RPC")


@pytest.mark.anyio
async def test_pools_are_shared_per_endpoint_set_and_pace() -> None:
    """One pool per endpoint set keeps health between calls.

    Async pools are keyed by the running event loop because their pacing
    lock binds to it; outside any loop each call builds a fresh pool so a
    dead loop's lock can never be re-acquired by the next loop.
    """
    ordered = ("https://primary.example/rpc", "https://fallback.example/rpc")

    assert shared_sync_pool(ordered) is shared_sync_pool(ordered)
    assert shared_async_pool(ordered) is shared_async_pool(ordered)
    assert shared_async_pool(ordered, minimum_interval_seconds=0.125) is (
        shared_async_pool(ordered, minimum_interval_seconds=0.125)
    )
    # A different pace is a different pool identity, so pacing stays explicit.
    assert shared_async_pool(ordered) is not shared_async_pool(
        ordered, minimum_interval_seconds=0.125
    )
    assert shared_sync_pool(ordered) is not shared_sync_pool(
        ("https://other.example/rpc",)
    )
    # A worker thread has no running loop, so pooling there must be refused.
    assert await asyncio.to_thread(shared_async_pool, ordered) is not (
        await asyncio.to_thread(shared_async_pool, ordered)
    )


@pytest.mark.anyio
async def test_cooldown_from_one_call_spares_the_next(
    monkeypatch: MonkeyPatch,
) -> None:
    """A throttled provider is skipped by later calls, not re-probed.

    This is the behaviour per-call pools could never provide: health recorded
    by the first request must change the failover order of the second.
    """
    stub_dotenv(monkeypatch, {})
    hits = {"primary": 0, "fallback": 0}

    async def throttled(_request: web.Request) -> web.Response:
        hits["primary"] += 1
        return web.json_response({"error": "rate limited"}, status=429)

    async def healthy(request: web.Request) -> web.Response:
        hits["fallback"] += 1
        payload = await request.json()
        return web.json_response(
            {"jsonrpc": "2.0", "id": payload["id"], "result": {"slot": 705}}
        )

    primary_app = web.Application()
    primary_app.router.add_post("/", throttled)
    fallback_app = web.Application()
    fallback_app.router.add_post("/", healthy)
    async with (
        TestServer(primary_app) as primary_server,
        TestServer(fallback_app) as fallback_server,
    ):
        endpoints = resolve_rpc_endpoints(
            primary=str(primary_server.make_url("/")),
            fallbacks=(str(fallback_server.make_url("/")),),
        )
        first = await asyncio.to_thread(
            sync_rpc_result, "getSlot", [], endpoints=endpoints
        )
        health_after_first = rpc_health(endpoints)
        second = await asyncio.to_thread(
            sync_rpc_result, "getSlot", [], endpoints=endpoints
        )

    assert first == {"slot": 705}
    assert second == {"slot": 705}
    assert hits == {"primary": 1, "fallback": 2}
    primary_health, fallback_health = health_after_first
    assert primary_health.last_status == 429
    assert primary_health.consecutive_failures == 1
    assert primary_health.cooldown_remaining_seconds > 0
    assert fallback_health.last_status == 200
    assert fallback_health.consecutive_failures == 0


@pytest.mark.anyio
async def test_failures_use_the_transient_vocabulary(
    monkeypatch: MonkeyPatch,
) -> None:
    """Rate-limit and exhaustion messages stay classified as retryable.

    ``discover.collector._is_rate_limit_abstain`` treats a message as transient
    when it contains "rate-limited" or "transport failed", so both wordings are
    a contract, not prose.
    """
    stub_dotenv(monkeypatch, {})
    # No busy-retry: the exhausted pool must surface its error immediately.
    monkeypatch.setattr(rpc_access, "RPC_BUSY_ATTEMPTS", 1)

    async def throttled(_request: web.Request) -> web.Response:
        return web.json_response({"error": "rate limited"}, status=429)

    app = web.Application()
    app.router.add_post("/", throttled)
    async with TestServer(app) as server:
        endpoints = resolve_rpc_endpoints(primary=str(server.make_url("/")))
        with pytest.raises(RpcAccessError) as rate_limited:
            await asyncio.to_thread(sync_rpc_result, "getSlot", [], endpoints=endpoints)
        # The provider is now cooling down, so the next call never reaches it.
        with pytest.raises(RpcAccessError) as exhausted:
            await asyncio.to_thread(sync_rpc_result, "getSlot", [], endpoints=endpoints)

    assert rate_limited.value.status == 429
    assert "rate-limited" in str(rate_limited.value).lower()
    assert "transport failed" in str(exhausted.value).lower()
