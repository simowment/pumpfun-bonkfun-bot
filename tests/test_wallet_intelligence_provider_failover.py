"""Integration coverage for wallet-intelligence RPC provider failover."""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from pathlib import Path

import base58
import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer
from sol_trade_sdk.solana.provider_pool import RpcHttpResponse, RpcProviderPool

from rugbot.domain.decisions import AbstainResult
from rugbot.ingest.rpc_observer import observe_address
from rugbot.intelligence.token_resolver import resolve_token_or_wallet
from rugbot.intelligence.wallet_intelligence import scan_wallet_intelligence
from rugbot.interfaces.cli.watch import WatchCycleResult, run_watch_cycle
from rugbot.runtime.config import (
    ExecutionMode,
    ListenerKind,
    SniperTarget,
    TargetKind,
)
from rugbot.storage.config_store import load_sniper_config_db
from rugbot.storage.jsonl_observation_store import JsonlObservationStore

WALLET = "2r2HuRi1vLzVxXnWAffWfsAMDkQpfG1c23KPDgR4wp5p"


@pytest.fixture
def anyio_backend() -> str:
    """Use the asyncio backend required by aiohttp."""

    return "asyncio"


@pytest.mark.anyio
async def test_wallet_scan_reaches_fallback_after_primary_rate_limit() -> None:
    """Exercise scan -> observer -> provider pool across real local HTTP."""

    primary_methods: list[str] = []
    fallback_methods: list[str] = []

    async def rate_limited(request: web.Request) -> web.Response:
        payload = await request.json()
        primary_methods.append(payload["method"])
        return web.json_response({"error": "rate limited"}, status=429)

    async def healthy(request: web.Request) -> web.Response:
        payload = await request.json()
        method = payload["method"]
        fallback_methods.append(method)
        result: int | list[object] = 500 if method == "getSlot" else []
        return web.json_response(
            {"jsonrpc": "2.0", "id": payload["id"], "result": result}
        )

    primary_app = web.Application()
    primary_app.router.add_post("/", rate_limited)
    fallback_app = web.Application()
    fallback_app.router.add_post("/", healthy)
    async with (
        TestServer(primary_app) as primary_server,
        TestServer(fallback_app) as fallback_server,
    ):
        primary = str(primary_server.make_url("/"))
        fallback = str(fallback_server.make_url("/"))
        pool = RpcProviderPool((primary, fallback))
        result = await scan_wallet_intelligence(
            WALLET,
            endpoint=primary,
            fallback_endpoints=(fallback,),
            max_transactions=1,
            max_history_pages=1,
            max_linked_wallets=1,
            transport=pool,
        )
        second_result = await scan_wallet_intelligence(
            WALLET,
            endpoint=primary,
            fallback_endpoints=(fallback,),
            max_transactions=1,
            max_history_pages=1,
            max_linked_wallets=1,
            transport=pool,
        )

    assert isinstance(result, AbstainResult)
    assert isinstance(second_result, AbstainResult)
    assert primary_methods == ["getSlot"]
    assert fallback_methods == [
        "getSlot",
        "getSignaturesForAddress",
        "getSlot",
        "getSignaturesForAddress",
    ]


@pytest.mark.anyio
async def test_wallet_scan_reports_exhausted_rpc_rate_limits() -> None:
    """Translate exhausted HTTP 429 failover into an actionable abstention."""

    async def rate_limited(_request: web.Request) -> web.Response:
        return web.json_response({"error": "rate limited"}, status=429)

    primary_app = web.Application()
    primary_app.router.add_post("/", rate_limited)
    fallback_app = web.Application()
    fallback_app.router.add_post("/", rate_limited)
    async with (
        TestServer(primary_app) as primary_server,
        TestServer(fallback_app) as fallback_server,
    ):
        primary = str(primary_server.make_url("/"))
        fallback = str(fallback_server.make_url("/"))
        result = await scan_wallet_intelligence(
            WALLET,
            endpoint=primary,
            fallback_endpoints=(fallback,),
            max_transactions=1,
            max_history_pages=1,
            max_linked_wallets=0,
            transport=RpcProviderPool((primary, fallback)),
        )

    assert isinstance(result, AbstainResult)
    assert result.message == "getSlot was rate-limited by the available RPC providers"


@pytest.mark.anyio
async def test_observer_resumes_older_history_from_before_cursor() -> None:
    """Send the persisted older-history cursor through real local HTTP RPC."""

    before_signature = "1" * 64
    observed_options: dict[str, object] = {}

    async def finalized_rpc(request: web.Request) -> web.Response:
        payload = await request.json()
        method = payload["method"]
        if method == "getSlot":
            result: object = 500
        else:
            observed_options.update(payload["params"][1])
            result = []
        return web.json_response(
            {"jsonrpc": "2.0", "id": payload["id"], "result": result}
        )

    app = web.Application()
    app.router.add_post("/", finalized_rpc)
    async with TestServer(app) as server:
        endpoint = str(server.make_url("/"))
        result = await observe_address(
            WALLET,
            endpoint=endpoint,
            max_signatures=10,
            max_transactions=10,
            max_pages=1,
            before_signature=before_signature,
            standard_history_only=True,
        )

    assert result == ()
    assert observed_options["before"] == before_signature
    assert observed_options["commitment"] == "finalized"


@pytest.mark.anyio
async def test_observer_persists_completed_rows_before_later_rate_limit(
    tmp_path: Path,
) -> None:
    """Fsync a successful row before a later transaction in the batch hits 429."""

    first_signature = base58.b58encode(bytes([1]) * 64).decode("ascii")
    second_signature = base58.b58encode(bytes([2]) * 64).decode("ascii")
    store = JsonlObservationStore(tmp_path / "observations.jsonl")

    async def recorded_transport(_endpoint: str, body: bytes) -> RpcHttpResponse:
        payload = json.loads(body)
        method = payload["method"]
        if method == "getSlot":
            result: object = 500
        elif method == "getSignaturesForAddress":
            result = [
                {
                    "signature": first_signature,
                    "slot": 499,
                    "confirmationStatus": "finalized",
                },
                {
                    "signature": second_signature,
                    "slot": 498,
                    "confirmationStatus": "finalized",
                },
            ]
        elif method == "getTransaction":
            signature = payload["params"][0]
            if signature == second_signature:
                return RpcHttpResponse(status=429, body=b'{"error":"rate limited"}')
            result = {
                "slot": 499,
                "meta": {"err": None},
                "transaction": {"signatures": [first_signature]},
            }
        elif method == "getBlock":
            result = {"signatures": [first_signature]}
        else:
            raise AssertionError(method)
        return RpcHttpResponse(
            status=200,
            body=json.dumps({"jsonrpc": "2.0", "id": 1, "result": result}).encode(),
        )

    result = await observe_address(
        WALLET,
        endpoint="https://recorded.invalid",
        max_signatures=2,
        max_transactions=2,
        max_pages=1,
        transport=recorded_transport,
        observation_store=store,
        standard_history_only=True,
    )

    assert isinstance(result, AbstainResult)
    assert "rate-limited" in result.message
    restored = store.read_all()
    assert len(restored) == 1
    assert restored[0].signature == bytes([1]) * 64


@pytest.mark.anyio
async def test_token_resolution_reaches_fallback_after_primary_rate_limit() -> None:
    """Exercise synchronous resolver failover across real local HTTP."""

    primary_methods: list[str] = []
    fallback_methods: list[str] = []

    async def rate_limited(request: web.Request) -> web.Response:
        payload = await request.json()
        primary_methods.append(payload["method"])
        return web.json_response({"error": "rate limited"}, status=429)

    async def healthy(request: web.Request) -> web.Response:
        payload = await request.json()
        fallback_methods.append(payload["method"])
        return web.json_response(
            {
                "jsonrpc": "2.0",
                "id": payload["id"],
                "result": {"value": None},
            }
        )

    primary_app = web.Application()
    primary_app.router.add_post("/", rate_limited)
    fallback_app = web.Application()
    fallback_app.router.add_post("/", healthy)
    async with (
        TestServer(primary_app) as primary_server,
        TestServer(fallback_app) as fallback_server,
    ):
        resolved = await asyncio.to_thread(
            resolve_token_or_wallet,
            WALLET,
            rpc_url=str(primary_server.make_url("/")),
            fallback_endpoints=(str(fallback_server.make_url("/")),),
        )

    assert resolved.target_wallet == WALLET
    assert resolved.is_token is False
    assert primary_methods == ["getAccountInfo"]
    assert fallback_methods == ["getAccountInfo"]


@pytest.mark.anyio
async def test_watch_cycle_reaches_standard_rpc_fallback(
    tmp_path: Path,
) -> None:
    """Exercise canonical watch polling through one persistent provider pool."""

    primary_methods: list[str] = []
    fallback_methods: list[str] = []

    async def rate_limited(request: web.Request) -> web.Response:
        payload = await request.json()
        primary_methods.append(payload["method"])
        return web.json_response({"error": "rate limited"}, status=429)

    async def healthy(request: web.Request) -> web.Response:
        payload = await request.json()
        method = payload["method"]
        fallback_methods.append(method)
        result: int | list[object] = 500 if method == "getSlot" else []
        return web.json_response(
            {"jsonrpc": "2.0", "id": payload["id"], "result": result}
        )

    primary_app = web.Application()
    primary_app.router.add_post("/", rate_limited)
    fallback_app = web.Application()
    fallback_app.router.add_post("/", healthy)
    config = load_sniper_config_db(tmp_path)
    config = replace(
        config,
        target=SniperTarget(kind=TargetKind.WALLET, id=WALLET),
        execution=replace(config.execution, mode=ExecutionMode.OBSERVE),
        listener=ListenerKind.RPC,
    )
    async with (
        TestServer(primary_app) as primary_server,
        TestServer(fallback_app) as fallback_server,
    ):
        primary = str(primary_server.make_url("/"))
        fallback = str(fallback_server.make_url("/"))
        result = await run_watch_cycle(
            config,
            endpoint=primary,
            state_dir=tmp_path,
            max_transactions=1,
            transport=RpcProviderPool((primary, fallback)),
        )

    assert isinstance(result, WatchCycleResult)
    assert result.report.abstention is None
    assert primary_methods == ["getSlot"]
    assert fallback_methods == ["getSlot", "getSignaturesForAddress"]


@pytest.mark.anyio
async def test_standard_history_flag_disables_helius_only_method() -> None:
    """Keep provider pools on methods portable across ordered endpoints."""

    methods: list[str] = []

    async def recorded_transport(_endpoint: str, body: bytes) -> RpcHttpResponse:
        payload = json.loads(body)
        method = payload["method"]
        methods.append(method)
        result: int | list[object] = 500 if method == "getSlot" else []
        return RpcHttpResponse(
            status=200,
            body=json.dumps(
                {"jsonrpc": "2.0", "id": payload["id"], "result": result}
            ).encode(),
        )

    result = await observe_address(
        WALLET,
        endpoint="https://mainnet.helius-rpc.com/?api-key=test",
        max_signatures=1,
        max_transactions=1,
        max_pages=1,
        transport=recorded_transport,
        standard_history_only=True,
    )

    assert result == ()
    assert methods == ["getSlot", "getSignaturesForAddress"]
