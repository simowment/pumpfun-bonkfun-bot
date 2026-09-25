"""WalletTradeSource behavior without network (fake stream/poll/hydrator)."""

import asyncio
import importlib
import json
from pathlib import Path
from types import ModuleType
from uuid import uuid4

import base58
import pytest

from rugbot.domain.decisions import AbstainReason, AbstainResult
from rugbot.domain.observations import RawChainObservation
from rugbot.integrations import solana_logs_stream
from rugbot.integrations.solana_logs_stream import (
    SolanaLogsStream,
    SolanaLogsStreamError,
    WalletLogNotification,
)
from rugbot.runtime.workers import wallet_trade_source as source_module
from rugbot.runtime.workers.wallet_trade_source import (
    WalletTradeSource,
    detection_lag_ms,
    observation_block_time_ms,
)

WALLET = "4vw54BmAogeRV3vPKWyFet5yf8DTLcREzdSzx4rw9Ud9"
SIGNATURE = base58.b58encode(bytes(range(64))).decode("ascii")
BLOCK_TIME = 1_700_000_000


def _payload_bytes(block_time: int | None = BLOCK_TIME) -> bytes:
    return json.dumps({"result": {"blockTime": block_time, "slot": 42}}).encode()


def _observation(signature: str = SIGNATURE) -> RawChainObservation:
    return RawChainObservation(
        raw_id=uuid4(),
        source_id="wallet-trade-source",
        observer_id="test",
        boot_id=uuid4(),
        receive_sequence=0,
        slot=42,
        parent_slot=None,
        blockhash=None,
        signature=base58.b58decode(signature),
        transaction_index=0,
        outer_instruction_index=None,
        inner_instruction_group_index=None,
        inner_instruction_index=None,
        stack_height=None,
        event_ordinal=None,
        commitment="finalized",
        canonical_status="canonical",
        received_wall_ns=0,
        received_monotonic_ns=0,
        program_id=None,
        account_pubkey=None,
        account_owner_program_id=None,
        raw_transaction=None,
        raw_transaction_format=None,
        raw_account_data=None,
        account_write_version=None,
        source_update_kind="transaction",
        raw_source_status=None,
        raw_source_payload=_payload_bytes(),
        decoder_name=None,
        decoder_version=None,
        idl_hash=None,
    )


class FakeStream:
    """Scripted trigger stream with controllable health flags."""

    def __init__(self, notifications=None, failures_before=0) -> None:
        self.notifications = list(notifications or [])
        self.failures_before = failures_before
        self.attempts = 0
        self._connected = False
        self._failed = False
        self.reconciled: list[object] = []

    @property
    def connected(self) -> bool:
        return self._connected

    @property
    def failed(self) -> bool:
        return self._failed

    async def reconcile(self, wallets) -> None:
        self.reconciled.append(tuple(wallets))

    async def next_notification(self) -> WalletLogNotification:
        self.attempts += 1
        if self.attempts <= self.failures_before:
            self._failed = True
            raise OSError("boom")
        self._connected = True
        self._failed = False
        return self.notifications.pop(0)


class FakePoll:
    """Scripted finalized fallback source."""

    def __init__(self, batches, address=WALLET) -> None:
        self.batches = list(batches)
        self.address = address
        self.handled_ledger = None
        self.reads = 0

    async def read(self):
        self.reads += 1
        return self.batches.pop(0)


def _abstain() -> AbstainResult:
    return AbstainResult(
        reason=AbstainReason.STALE_STATE, message="empty", as_of_slot=-1
    )


def _source(stream, poll_batches, **kwargs):
    async def hydrate(wallet, signature, slot):
        return (_observation(signature),)

    return WalletTradeSource(
        [WALLET],
        endpoint="http://localhost:8899",
        websocket_endpoint="ws://localhost:8900",
        poll_source=FakePoll(poll_batches),
        stream=stream,
        hydrate_fn=hydrate,
        read_timeout_seconds=0.2,
        now_ms_fn=lambda: BLOCK_TIME * 1000 + 1500,
        **kwargs,
    )


async def _run(coro):
    return await coro


def test_ws_success_reports_transport_and_lag() -> None:
    stream = FakeStream(
        [WalletLogNotification(wallet=WALLET, signature=SIGNATURE, slot=42)]
    )
    source = _source(stream, [_abstain()])
    result = asyncio.run(_run(source.read()))
    assert not isinstance(result, AbstainResult)
    assert source.last_transport == "ws"
    assert source.last_detection_lag_ms == 1500
    assert source.last_signature == SIGNATURE
    assert source.last_wallet == WALLET


def test_dedupe_by_signature() -> None:
    stream = FakeStream(
        [
            WalletLogNotification(wallet=WALLET, signature=SIGNATURE, slot=42),
            WalletLogNotification(wallet=WALLET, signature=SIGNATURE, slot=42),
        ]
    )
    source = _source(stream, [_abstain()])
    first = asyncio.run(_run(source.read()))
    second = asyncio.run(_run(source.read()))
    assert not isinstance(first, AbstainResult)
    assert isinstance(second, AbstainResult)


def test_ws_failures_fall_back_to_poll_then_recover() -> None:
    stream = FakeStream([], failures_before=10)
    poll_signature = base58.b58encode(bytes([7]) * 64).decode("ascii")
    poll_obs = _observation(poll_signature)
    poll = FakePoll([(poll_obs,)])
    source = WalletTradeSource(
        [WALLET],
        endpoint="http://localhost:8899",
        websocket_endpoint="ws://localhost:8900",
        poll_source=poll,
        stream=stream,
        hydrate_fn=None,
        read_timeout_seconds=0.05,
        ws_failure_threshold=2,
        now_ms_fn=lambda: BLOCK_TIME * 1000 + 9000,
    )
    first = asyncio.run(_run(source.read()))
    assert isinstance(first, AbstainResult)
    assert source.transport_in_use == "ws"
    second = asyncio.run(_run(source.read()))
    assert not isinstance(second, AbstainResult)
    assert source.last_transport == "poll"
    assert source.last_detection_lag_ms == 9000
    assert source.transport_in_use == "poll"
    stream._connected = True
    stream._failed = False
    stream.failures_before = 1
    stream.notifications.append(
        WalletLogNotification(wallet=WALLET, signature=SIGNATURE, slot=42)
    )

    async def hydrate(wallet, signature, slot):
        return (_observation(signature),)

    source._hydrate_fn = hydrate
    third = asyncio.run(_run(source.read()))
    assert not isinstance(third, AbstainResult)
    assert source.last_transport == "ws"
    assert source.transport_in_use == "ws"


def test_default_hydrate_retries_until_finalized(monkeypatch) -> None:
    stale = AbstainResult(
        reason=AbstainReason.STALE_STATE, message="not final", as_of_slot=-1
    )
    calls = {"count": 0}

    async def fake_hydrate(*args, **kwargs):
        calls["count"] += 1
        if calls["count"] < 3:
            return stale
        return _observation(SIGNATURE)

    monkeypatch.setattr(source_module, "observe_finalized_transaction", fake_hydrate)
    monkeypatch.setattr(source_module, "HYDRATE_RETRY_SECONDS", 0.0)
    stream = FakeStream(
        [WalletLogNotification(wallet=WALLET, signature=SIGNATURE, slot=42)]
    )
    source = WalletTradeSource(
        [WALLET],
        endpoint="http://localhost:8899",
        websocket_endpoint="ws://localhost:8900",
        poll_source=FakePoll([_abstain()]),
        stream=stream,
        hydrate_fn=None,
        read_timeout_seconds=0.2,
        now_ms_fn=lambda: BLOCK_TIME * 1000 + 1500,
    )
    # The constructor already bound the default hydrator (hydrate_fn=None).
    result = asyncio.run(source.read())
    assert not isinstance(result, AbstainResult)
    assert calls["count"] == 3
    assert source.last_transport == "ws"


def test_read_never_raises_on_transport_errors() -> None:
    class ExplodingStream(FakeStream):
        async def next_notification(self):
            raise RuntimeError

    stream = ExplodingStream()
    source = _source(stream, [_abstain()], ws_failure_threshold=100)
    for _ in range(3):
        result = asyncio.run(_run(source.read()))
        assert isinstance(result, AbstainResult)


def test_lag_helpers() -> None:
    obs = _observation()
    assert observation_block_time_ms(obs) == BLOCK_TIME * 1000
    assert detection_lag_ms(obs, now_ms=BLOCK_TIME * 1000 + 250) == 250
    bare = _observation()
    object.__setattr__(bare, "raw_source_payload", None)
    assert observation_block_time_ms(bare) is None
    assert detection_lag_ms(bare, now_ms=0) is None


def test_commitment_reaches_subscribe_params(monkeypatch) -> None:
    sent: list[str] = []

    class FakeSocket:
        async def send(self, message: str) -> None:
            sent.append(message)

        async def close(self) -> None:
            return None

    async def fake_connect(endpoint: str):
        assert endpoint == "ws://localhost:8900"
        return FakeSocket()

    monkeypatch.setattr(solana_logs_stream.websockets, "connect", fake_connect)
    stream = SolanaLogsStream("ws://localhost:8900", commitment="processed")
    assert stream.commitment == "processed"
    asyncio.run(stream.reconcile([WALLET]))
    asyncio.run(stream._connect())
    assert len(sent) == 1
    params = json.loads(sent[0])["params"]
    assert params[1] == {"commitment": "processed"}
    with pytest.raises(SolanaLogsStreamError):
        SolanaLogsStream("ws://localhost:8900", commitment="bogus")


def test_source_rejects_bad_commitment_and_empty_addresses() -> None:
    poll = FakePoll([_abstain()])
    with pytest.raises(ValueError):
        WalletTradeSource(
            [],
            endpoint="http://localhost:8899",
            websocket_endpoint=None,
            poll_source=poll,
        )
    with pytest.raises(ValueError):
        WalletTradeSource(
            [WALLET],
            endpoint="http://localhost:8899",
            websocket_endpoint=None,
            commitment="bogus",
            poll_source=poll,
        )


def test_no_execution_port_imports() -> None:
    modules = [
        "rugbot.runtime.workers.wallet_trade_source",
        "rugbot.analysis.wallet_registry",
        "rugbot.interfaces.cli.copytrade",
        "rugbot.interfaces.cli.registry",
    ]
    for module in modules:
        imported = importlib.import_module(module)
        # No direct execution-port reference in the module's own namespace.
        for name, value in vars(imported).items():
            if name == "rugbot":
                continue
            assert "rugbot.execution" not in name
            if isinstance(value, ModuleType) and (
                value.__name__ == "rugbot.execution"
                or value.__name__.startswith("rugbot.execution.")
            ):
                raise AssertionError(  # noqa: TRY003
                    f"{module} binds execution port {value.__name__}"
                )
        path = imported.__file__
        assert path is not None
        text = Path(path).read_text(encoding="utf-8")
        assert "rugbot.execution" not in text
        assert "place_order" not in text
        assert "submit_order" not in text
