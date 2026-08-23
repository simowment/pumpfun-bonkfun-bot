"""Integration coverage for WSS-triggered tracking and TUI launch delivery."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from time import monotonic_ns, time_ns
from uuid import uuid4

import base58
import pytest
from textual.widgets import DataTable, TabbedContent
from websockets.asyncio.server import serve

from rugbot.domain.decisions import AbstainResult
from rugbot.domain.observations import RawChainObservation
from rugbot.ingest.pump.models import TokenLaunch
from rugbot.ingest.pump.pump_create_observation import decode_pump_create_v2_observation
from rugbot.ingest.pump.pump_stream import (
    PumpPortalLaunchNotification,
    parse_pumpportal_notification,
)
from rugbot.ingest.rpc_observer import JSON_TRANSACTION_FORMAT, RpcHttpResponse
from rugbot.integrations.solana_logs_stream import SolanaLogsStream
from rugbot.runtime.app import build_ui_runtime
from rugbot.runtime.workers.tracked_launch_observation import (
    LaunchObservationStatus,
    TrackedLaunchObservationProducer,
)
from rugbot.tui.app import RugbotTuiApp
from rugbot.tui.widgets import LiveActivityView

WALLET = "2r2HuRi1vLzVxXnWAffWfsAMDkQpfG1c23KPDgR4wp5p"
MINT = "E9CqsGL5uXPASB853f87ox8nZVgW7ucoeYMC4bN8pump"
HISTORICAL_MINT = "BVGraUKvZydDXSAHydZvHCTFPATvcUTPoKFkocA8pump"
SIGNATURE = "5x" * 32
HISTORICAL_SIGNATURE = "4x" * 32
CREATE_FIXTURE = Path(
    "fixtures/finalized_transactions/pump_create_v2/"
    "4HbY43S9UigSctrfxY5nszgf3ozN1f4kPQYaqaFLZaCDhwa55rauuRmhP85u67U7dBvGFwB5C6stmkH2b1TNxgQh.json"
)


class _OneShotObservationSource:
    """Return one canonical fixture batch, then remain caught up."""

    def __init__(self, observation: RawChainObservation) -> None:
        self._observation = observation
        self._read = False

    async def read(self) -> tuple[RawChainObservation, ...]:
        """Read the fixture exactly once."""

        if self._read:
            return ()
        self._read = True
        return (self._observation,)


class _EmptyObservationSource:
    """Keep HTTP catch-up healthy without introducing another launch."""

    async def read(self) -> tuple[RawChainObservation, ...]:
        """Return a caught-up finalized address history."""

        return ()


class _OneShotPumpPortalStream:
    """Deliver one recorded provider trigger through the shared stream port."""

    def __init__(self, notification: PumpPortalLaunchNotification) -> None:
        self._notification = notification
        self._delivered = False
        self.connected = False
        self.failed = False

    async def reconcile(self, wallets: tuple[str, ...] | set[str]) -> None:
        """Activate the stream when its creator belongs to the entity."""

        self.connected = self._notification.creator_pubkey in wallets

    async def next_notification(self) -> PumpPortalLaunchNotification:
        """Return the recorded trigger once and then remain connected."""

        if not self._delivered:
            self._delivered = True
            return self._notification
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    async def next_global_notification(self) -> PumpPortalLaunchNotification:
        """Return the same recorded trigger through the global stream port."""

        return await self.next_notification()

    async def close(self) -> None:
        """Close the recorded stream."""

        self.connected = False


def _create_fixture_observation() -> RawChainObservation:
    fixture = json.loads(CREATE_FIXTURE.read_text(encoding="utf-8"))
    parsed_response = fixture["json_parsed_transaction_response"]
    parsed_message = parsed_response["transaction"]["message"]
    account_keys = [item["pubkey"] for item in parsed_message["accountKeys"]]
    create_v2 = fixture["create_v2"]
    compiled_response = {
        "slot": parsed_response["slot"],
        "meta": {
            "err": parsed_response["meta"]["err"],
        },
        "transaction": {
            "signatures": parsed_response["transaction"]["signatures"],
            "message": {
                "accountKeys": account_keys,
                "instructions": [
                    {
                        "accounts": create_v2["account_indices"],
                        "data": create_v2["data_base58"],
                        "programIdIndex": create_v2["program_id_index"],
                    }
                ],
            },
        },
    }
    response_body = json.dumps(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "result": compiled_response,
        },
        separators=(",", ":"),
    ).encode("utf-8")
    observation = RawChainObservation(
        raw_id=uuid4(),
        source_id="recorded-solana-rpc",
        observer_id="integration-test",
        boot_id=uuid4(),
        receive_sequence=1,
        slot=fixture["as_of_slot"],
        parent_slot=None,
        blockhash=None,
        signature=base58.b58decode(fixture["signature"]),
        transaction_index=0,
        outer_instruction_index=None,
        inner_instruction_group_index=None,
        inner_instruction_index=None,
        stack_height=None,
        event_ordinal=None,
        commitment="finalized",
        canonical_status="canonical",
        received_wall_ns=time_ns(),
        received_monotonic_ns=monotonic_ns(),
        program_id=None,
        account_pubkey=None,
        account_owner_program_id=None,
        raw_transaction=response_body,
        raw_transaction_format=JSON_TRANSACTION_FORMAT,
        raw_account_data=None,
        account_write_version=None,
        source_update_kind="transaction",
        raw_source_status=None,
        raw_source_payload=response_body,
        decoder_name=None,
        decoder_version=None,
        idl_hash=None,
    )
    return observation


@pytest.fixture
def anyio_backend() -> str:
    """Run WebSocket and Textual integration paths on the supported loop."""

    return "asyncio"


@pytest.mark.anyio
async def test_shared_wss_stream_routes_wallet_notification() -> None:
    """Verify one native socket subscribes then maps its logs notification."""

    async def handler(websocket: object) -> None:
        request = json.loads(await websocket.recv())
        assert request["method"] == "logsSubscribe"
        assert request["params"][0] == {"mentions": [WALLET]}
        assert request["params"][1] == {"commitment": "finalized"}
        await websocket.send(
            json.dumps({"jsonrpc": "2.0", "id": request["id"], "result": 42})
        )
        await websocket.send(
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "method": "logsNotification",
                    "params": {
                        "result": {
                            "context": {"slot": 123},
                            "value": {"signature": SIGNATURE, "err": None, "logs": []},
                        },
                        "subscription": 42,
                    },
                }
            )
        )

    async with serve(handler, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        stream = SolanaLogsStream(f"ws://127.0.0.1:{port}")
        await stream.reconcile((WALLET,))
        notification = await asyncio.wait_for(stream.next_notification(), timeout=1)
        await stream.close()

    assert notification.wallet == WALLET
    assert notification.signature == SIGNATURE
    assert notification.slot == 123


@pytest.mark.anyio
async def test_pumpportal_trigger_reaches_finalized_tui_history(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify provider trigger, finalized RPC, SQLite, and mounted TUI."""

    monkeypatch.delenv("SOLANA_RPC_HTTP", raising=False)
    monkeypatch.delenv("SOLANA_NODE_RPC_ENDPOINT", raising=False)
    monkeypatch.delenv("SOLANA_RPC_WEBSOCKET", raising=False)
    monkeypatch.delenv("SOLANA_NODE_WSS_ENDPOINT", raising=False)
    observation = _create_fixture_observation()
    decoded = decode_pump_create_v2_observation(observation)
    assert decoded is not None
    assert not isinstance(decoded, AbstainResult)
    signature = base58.b58encode(observation.signature).decode("ascii")
    message = json.dumps(
        {
            "txType": "create",
            "signature": signature,
            "mint": decoded.mint_pubkey,
            "traderPublicKey": decoded.user_pubkey,
        }
    )
    parsed = parse_pumpportal_notification(message, {decoded.user_pubkey})
    assert parsed == {
        "signature": signature,
        "mint": decoded.mint_pubkey,
        "creator": decoded.user_pubkey,
    }
    assert parse_pumpportal_notification(message, {WALLET}) is None

    stream = _OneShotPumpPortalStream(
        PumpPortalLaunchNotification(
            signature=signature,
            mint_pubkey=decoded.mint_pubkey,
            creator_pubkey=decoded.user_pubkey,
        )
    )

    async def recorded_transport(_endpoint: str, body: bytes) -> RpcHttpResponse:
        request = json.loads(body)
        method = request["method"]
        if method == "getSlot":
            response = {"jsonrpc": "2.0", "id": 1, "result": observation.slot + 1}
        elif method == "getTransaction":
            return RpcHttpResponse(status=200, body=observation.raw_transaction)
        elif method == "getBlock":
            response = {
                "jsonrpc": "2.0",
                "id": 1,
                "result": {
                    "transactions": [{"transaction": {"signatures": [signature]}}]
                },
            }
        else:
            raise AssertionError(method)
        return RpcHttpResponse(
            status=200,
            body=json.dumps(response, separators=(",", ":")).encode("utf-8"),
        )

    core = build_ui_runtime(state_dir=tmp_path, endpoint="")
    core.service.add_funder(decoded.user_pubkey, label="Recorded creator")

    async def activation_slot() -> int:
        return observation.slot - 1

    producer = TrackedLaunchObservationProducer(
        service=core.service,
        repository=core.repository,
        endpoint="https://recorded.invalid",
        pumpportal_stream=stream,
        global_launch_handler=core.screener.nominate_live_launch,
        transport=recorded_transport,
        finalized_slot_resolver=activation_slot,
        source_factory=lambda _address: _EmptyObservationSource(),
        poll_interval_seconds=0.01,
    )
    app = RugbotTuiApp(
        core=core,
        endpoint="",
        refresh_seconds=3_600,
        state_dir=tmp_path,
    )
    async with app.run_test(size=(120, 36)) as pilot:
        await producer.start()
        try:
            async with asyncio.timeout(1):
                while core.repository.get_launch(decoded.mint_pubkey) is None:
                    await asyncio.sleep(0.01)
            await pilot.pause()
            assert producer.status is LaunchObservationStatus.PUMPPORTAL_LIVE
            candidate = core.screener.get_candidate(decoded.user_pubkey)
            assert candidate is not None
            assert candidate.token_mint == decoded.mint_pubkey
            assert candidate.is_bible_qualified is False
            activity = app.query_one("#live-activity-view", LiveActivityView)
            assert f"launch_{decoded.mint_pubkey}" in activity._items
            assert len(core.repository.get_undelivered_alerts("discord")) == 1
        finally:
            await producer.stop()

    await core.close()


@pytest.mark.anyio
async def test_http_catchup_detects_launch_while_wss_is_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify failed WSS falls through decoder, SQLite, and mounted TUI."""

    monkeypatch.delenv("SOLANA_RPC_HTTP", raising=False)
    monkeypatch.delenv("SOLANA_NODE_RPC_ENDPOINT", raising=False)
    monkeypatch.delenv("SOLANA_RPC_WEBSOCKET", raising=False)
    monkeypatch.delenv("SOLANA_NODE_WSS_ENDPOINT", raising=False)
    observation = _create_fixture_observation()
    decoded = decode_pump_create_v2_observation(observation)
    assert decoded is not None
    assert not isinstance(decoded, AbstainResult)
    creator = decoded.user_pubkey
    mint = decoded.mint_pubkey
    core = build_ui_runtime(state_dir=tmp_path, endpoint="")
    core.service.add_funder(creator, label="Recorded creator")

    async def activation_slot() -> int:
        return observation.slot - 1

    producer = TrackedLaunchObservationProducer(
        service=core.service,
        repository=core.repository,
        endpoint="http://unused.invalid",
        websocket_endpoint="ws://127.0.0.1:1",
        finalized_slot_resolver=activation_slot,
        source_factory=lambda _address: _OneShotObservationSource(observation),
        poll_interval_seconds=0.01,
    )
    app = RugbotTuiApp(
        core=core,
        endpoint="",
        refresh_seconds=3_600,
        state_dir=tmp_path,
    )
    async with app.run_test(size=(120, 36)) as pilot:
        await producer.start()
        try:
            async with asyncio.timeout(1):
                while core.repository.get_launch(mint) is None:
                    await asyncio.sleep(0.01)
            await pilot.pause()
            activity = app.query_one("#live-activity-view", LiveActivityView)
            assert f"launch_{mint}" in activity._items
            assert decoded.symbol in activity.last_event_str
            assert producer.status is LaunchObservationStatus.HTTP_CATCHUP
            assert core.repository.get_undelivered_alerts("tui") == ()
            assert len(core.repository.get_undelivered_alerts("discord")) == 1
        finally:
            await producer.stop()

    await core.close()


@pytest.mark.anyio
async def test_tui_renders_and_acknowledges_durable_launch_alert(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify event bus, SQLite outbox, and mounted TUI delivery end to end."""

    monkeypatch.delenv("SOLANA_RPC_HTTP", raising=False)
    monkeypatch.delenv("SOLANA_NODE_RPC_ENDPOINT", raising=False)
    monkeypatch.delenv("SOLANA_RPC_WEBSOCKET", raising=False)
    monkeypatch.delenv("SOLANA_NODE_WSS_ENDPOINT", raising=False)
    core = build_ui_runtime(state_dir=tmp_path, endpoint="")
    app = RugbotTuiApp(
        core=core,
        endpoint="",
        refresh_seconds=3_600,
        state_dir=tmp_path,
    )
    async with app.run_test(size=(120, 36)) as pilot:
        core.service.add_funder(WALLET, label="Test entity")
        assert core.service.record_historical_launch(
            TokenLaunch(
                signature=HISTORICAL_SIGNATURE,
                slot=122,
                timestamp=1_699_999_000,
                creator=WALLET,
                mint=HISTORICAL_MINT,
                symbol="OLD",
                name="Historical token",
            )
        )
        assert core.repository.get_undelivered_alerts("tui") == ()
        core.service.handle_launch(
            TokenLaunch(
                signature=SIGNATURE,
                slot=123,
                timestamp=1_700_000_000,
                creator=WALLET,
                mint=MINT,
                symbol="TEST",
                name="Test token",
            )
        )
        await pilot.pause()

        activity = app.query_one("#live-activity-view", LiveActivityView)
        assert f"launch_{MINT}" in activity._items
        assert "TEST" in activity.last_event_str
        assert app.query_one("#launches-table", DataTable).row_count == 2
        assert core.repository.get_launch(HISTORICAL_MINT) is not None
        assert core.repository.get_launch(MINT) is not None
        assert core.repository.get_undelivered_alerts("tui") == ()
        await pilot.press("2")
        await pilot.pause()
        assert app.query_one(TabbedContent).active == "launches-tab"
        await pilot.press("escape")
        await pilot.pause()
        assert app.query_one(TabbedContent).active == "overview-tab"

    await core.close()
