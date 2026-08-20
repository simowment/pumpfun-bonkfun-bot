"""SQLite integration tests for the single-wallet sniper daemon."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path

from solders.pubkey import Pubkey

from rugbot.domain.amounts import Lamports
from rugbot.execution.ports import ExecutionMode, ExecutionReceipt
from rugbot.execution.position_runtime import PositionMarketEvidence
from rugbot.ingest.pump_stream import PumpCreateStreamSource
from rugbot.ingest.rpc_observer import RpcHttpResponse
from rugbot.runtime.position_exit_worker import MANUAL_FULL_EXIT_PPM
from rugbot.runtime.risk_gatekeeper import (
    ExecutionCostBudget,
    RiskLimits,
    RiskSnapshot,
)
from rugbot.runtime.sniper_daemon import (
    ProcessedTargetLaunch,
    SniperDaemonService,
    SniperStage,
)
from rugbot.storage.database import DatabaseManager
from rugbot.storage.sqlite_state_store import SqliteStateStore
from rugbot.storage.tracker import SQLiteTrackerRepository
from rugbot.tracker.models import (
    FunderRecord,
    TargetExecutionMode,
    TargetExecutionPolicy,
)


class _RecordingExecutionPort:
    def __init__(self) -> None:
        self.intents = []

    async def submit(self, intent):
        self.intents.append(intent)
        await asyncio.sleep(0)
        return ExecutionReceipt(
            mode=ExecutionMode.SIMULATION,
            intent_id=intent.intent_id,
            as_of_slot=intent.as_of_slot,
            accepted=True,
            would_submit_transaction=False,
            signature=None,
            simulated_output_base_units=1_000 if intent.side == "buy" else 1,
            estimated_fee_lamports=Lamports(5_000),
            message="fixture execution accepted",
        )


def _policy(target_id: str, *, size: int = 25_000_000) -> TargetExecutionPolicy:
    return TargetExecutionPolicy(
        funder_address=target_id,
        monitoring_enabled=True,
        execution_mode=TargetExecutionMode.SIMULATED,
        quote_size_lamports=size,
        take_profit_pnl_ppm=500_000,
        stop_loss_pnl_ppm=-200_000,
        max_slippage_bps=500,
        priority_fee_microlamports=50_000,
        jito_tip_lamports=1_000_000,
        updated_at=datetime.now(UTC).isoformat(),
    )


def _repository(path: Path, *policies: TargetExecutionPolicy):
    database = DatabaseManager(path)
    repository = SQLiteTrackerRepository(database)
    now = datetime.now(UTC).isoformat()
    for policy in policies:
        repository.save_funder(
            FunderRecord(
                id=None,
                address=policy.funder_address,
                label="integration target",
                enabled=True,
                created_at=now,
                last_seen_at=now,
            )
        )
        repository.save_target_execution_policy(policy)
    return database, repository


def _daemon(repository, positions, port) -> SniperDaemonService:
    async def risk_snapshot(_intent):
        return RiskSnapshot(
            wallet_balance_lamports=1_000_000_000,
            current_exposure_lamports=0,
            daily_realized_pnl_lamports=0,
            open_positions_count=len(positions.read_all()),
            position_token_balance_base_units=1_000,
            kill_switch_active=False,
        )

    async def costs(policy):
        return ExecutionCostBudget(
            network_fee_lamports=5_000,
            jito_tip_lamports=policy.jito_tip_lamports,
            ata_rent_lamports=2_039_280,
        )

    async def finalized_slot():
        states = positions.read_all()
        return max((int(state.as_of_slot) for state in states), default=100) + 1

    async def evidence(position, as_of_slot):
        return PositionMarketEvidence(
            as_of_slot=as_of_slot,
            market_id=position.market_id,
            current_pnl_ppm=0,
            idle_ms=0,
            executable_exit_capacity_base_units=(position.current_position_base_units),
        )

    return SniperDaemonService(
        policy_store=repository,
        position_store=positions,
        execution_ports={TargetExecutionMode.SIMULATED.value: port},
        risk_limits=RiskLimits(
            max_buy_lamports=100_000_000,
            max_exposure_lamports=500_000_000,
            daily_loss_limit_lamports=100_000_000,
            max_open_positions=5,
            max_slippage_bps=1_000,
            minimum_wallet_reserve_lamports=15_000_000,
        ),
        risk_snapshot_resolver=risk_snapshot,
        cost_budget_resolver=costs,
        finalized_slot_resolver=finalized_slot,
        evidence_resolver=evidence,
        exit_poll_interval_seconds=10,
    )


def test_processed_launch_opens_and_reloads_target_local_position(
    tmp_path: Path,
) -> None:
    target = str(Pubkey.new_unique())
    market = str(Pubkey.new_unique())
    database, repository = _repository(tmp_path / "tracker.sqlite3", _policy(target))
    positions = SqliteStateStore(tmp_path / "state.sqlite3")
    port = _RecordingExecutionPort()
    daemon = _daemon(repository, positions, port)

    result = asyncio.run(
        daemon.handle_processed_launch(
            ProcessedTargetLaunch(target, market, "launch-signature", 100),
            current_processed_slot=100,
        )
    )

    assert result.stage is SniperStage.POSITION
    assert len(port.intents) == 1
    position = positions.get(market)
    assert position is not None
    assert position.target_id == target
    assert position.take_profit_pnl_ppm == 500_000
    assert position.stop_loss_pnl_ppm == -200_000
    positions.close()

    reopened = SqliteStateStore(tmp_path / "state.sqlite3")
    assert reopened.get(market) == position
    reopened.close()
    database.close()


def test_duplicate_delivery_submits_once_and_kill_switch_keeps_sell(
    tmp_path: Path,
) -> None:
    target = str(Pubkey.new_unique())
    market = str(Pubkey.new_unique())
    database, repository = _repository(tmp_path / "tracker.sqlite3", _policy(target))
    positions = SqliteStateStore(tmp_path / "state.sqlite3")
    port = _RecordingExecutionPort()
    daemon = _daemon(repository, positions, port)
    launch = ProcessedTargetLaunch(target, market, "duplicate-signature", 100)

    async def deliver_twice():
        return await asyncio.gather(
            daemon.handle_processed_launch(launch, current_processed_slot=100),
            daemon.handle_processed_launch(launch, current_processed_slot=100),
        )

    first, second = asyncio.run(deliver_twice())

    assert {first.stage, second.stage} == {SniperStage.POSITION, SniperStage.IDLE}
    assert len([intent for intent in port.intents if intent.side == "buy"]) == 1
    assert daemon.toggle_kill_switch() is True
    blocked = asyncio.run(
        daemon.handle_processed_launch(
            ProcessedTargetLaunch(
                target,
                str(Pubkey.new_unique()),
                "blocked-signature",
                101,
            ),
            current_processed_slot=101,
        )
    )
    assert blocked.stage is SniperStage.FAILED
    assert blocked.risk_decision is not None

    sold = asyncio.run(daemon.manual_sell(market, fraction_ppm=MANUAL_FULL_EXIT_PPM))
    assert sold.error is None
    assert positions.get(market) is None
    assert [intent.side for intent in port.intents] == ["buy", "sell"]
    positions.close()
    database.close()


def test_stale_launch_is_rejected_before_risk_or_execution(tmp_path: Path) -> None:
    target = str(Pubkey.new_unique())
    database, repository = _repository(tmp_path / "tracker.sqlite3", _policy(target))
    positions = SqliteStateStore(tmp_path / "state.sqlite3")
    port = _RecordingExecutionPort()
    daemon = _daemon(repository, positions, port)

    result = asyncio.run(
        daemon.handle_processed_launch(
            ProcessedTargetLaunch(
                target,
                str(Pubkey.new_unique()),
                "old-signature",
                100,
            ),
            current_processed_slot=102,
        )
    )

    assert result.stage is SniperStage.IDLE
    assert result.message == "stale launch rejected"
    assert port.intents == []
    positions.close()
    database.close()


def test_processed_fixture_reaches_daemon_before_finalized_hydration(
    tmp_path: Path,
) -> None:
    fixture_path = next(
        Path("fixtures/finalized_transactions/pump_create_v2").glob("*.json")
    )
    fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
    transaction = fixture["json_parsed_transaction_response"]
    logs = transaction["meta"]["logMessages"]
    slot = transaction["slot"]
    signature = fixture["signature"]
    target = "CvoPbuS2AghzVBYJx7HfQGhALiqif4YwWgHvXmhehuJZ"
    market = "GGYkiUJGopoH9DZUjGFtLiKpXXW5AQovqZ7eVcjEpump"
    database, repository = _repository(tmp_path / "tracker.sqlite3", _policy(target))
    positions = SqliteStateStore(tmp_path / "state.sqlite3")
    daemon = _daemon(repository, positions, _RecordingExecutionPort())

    async def processed_handler(notification):
        result = await daemon.handle_processed_launch(
            ProcessedTargetLaunch(
                notification.creator_pubkey,
                notification.mint_pubkey,
                notification.signature,
                notification.slot,
            ),
            current_processed_slot=notification.slot,
        )
        assert result.stage is SniperStage.POSITION

    responses = {
        "getSlot": {"jsonrpc": "2.0", "id": 1, "result": slot},
        "getTransaction": {
            "jsonrpc": "2.0",
            "id": 1,
            "result": {
                "slot": slot,
                "meta": {"err": None},
                "transaction": {
                    "signatures": [signature],
                    "message": {"accountKeys": [], "instructions": []},
                },
            },
        },
        "getBlock": {
            "jsonrpc": "2.0",
            "id": 1,
            "result": {
                "transactions": [
                    {"transaction": {"signatures": [signature]}},
                ]
            },
        },
    }

    async def transport(_endpoint: str, body: bytes) -> RpcHttpResponse:
        method = json.loads(body)["method"]
        if method == "getTransaction":
            assert positions.get(market) is not None
        return RpcHttpResponse(
            status=200,
            body=json.dumps(responses[method]).encode("utf-8"),
        )

    notification = json.dumps(
        {
            "txType": "create",
            "signature": signature,
            "mint": market,
            "traderPublicKey": target,
        }
    )
    source = PumpCreateStreamSource(
        wallet=target,
        endpoint="https://rpc.example",
        raw_observation_path=tmp_path / "observations.jsonl",
        handled_ledger=positions,
        transport=transport,
        processed_handler=processed_handler,
    )
    source._catchup_complete = True
    source._websocket = _FakeWebsocket(notification)

    result = asyncio.run(source.read())

    assert not isinstance(result, str)
    assert positions.get(market) is not None
    positions.close()
    database.close()


class _FakeWebsocket:
    def __init__(self, message: str) -> None:
        self._message = message

    async def recv(self) -> str:
        return self._message

    async def close(self) -> None:
        return None
