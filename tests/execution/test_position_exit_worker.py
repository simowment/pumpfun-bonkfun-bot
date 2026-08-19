"""SQLite integration tests for the independent position exit worker."""

import asyncio
from pathlib import Path

import pytest
from solders.pubkey import Pubkey

from rugbot.domain.amounts import Lamports, Slot, TokenBaseUnits
from rugbot.execution.ports import ExecutionMode, ExecutionReceipt
from rugbot.execution.position_runtime import (
    PaperPositionState,
    PositionMarketEvidence,
)
from rugbot.runtime.position_exit_worker import (
    MANUAL_FULL_EXIT_PPM,
    MANUAL_HALF_EXIT_PPM,
    PositionExitWorker,
    PositionExitWorkerError,
)
from rugbot.runtime.risk_gatekeeper import RiskDecision, RiskDecisionCode
from rugbot.storage.sqlite_state_store import SqliteStateStore


class _AcceptingExecutionPort:
    def __init__(self) -> None:
        self.intents = []

    async def submit(self, intent):
        self.intents.append(intent)
        return ExecutionReceipt(
            mode=ExecutionMode.PAPER,
            intent_id=intent.intent_id,
            as_of_slot=intent.as_of_slot,
            accepted=True,
            would_submit_transaction=False,
            signature=None,
            simulated_output_base_units=1,
            estimated_fee_lamports=Lamports(0),
            message="paper exit accepted",
        )


def _position(market_id: str) -> PaperPositionState:
    return PaperPositionState(
        as_of_slot=Slot(10),
        market_id=market_id,
        target_id=str(Pubkey.new_unique()),
        execution_mode="paper",
        original_position_base_units=TokenBaseUnits(1_000),
        current_position_base_units=TokenBaseUnits(1_000),
        entry_quote_lamports=25_000_000,
        entry_cost_lamports=1_000_000,
        take_profit_pnl_ppm=100_000,
        stop_loss_pnl_ppm=-200_000,
        max_slippage_bps=500,
    )


def _worker(
    store: SqliteStateStore,
    port: _AcceptingExecutionPort,
    *,
    pnl_ppm: int = 0,
) -> PositionExitWorker:
    async def finalized_slot() -> int:
        return 11

    async def evidence(position, as_of_slot):
        return PositionMarketEvidence(
            as_of_slot=Slot(as_of_slot),
            market_id=position.market_id,
            current_pnl_ppm=pnl_ppm,
            idle_ms=0,
            executable_exit_capacity_base_units=(position.current_position_base_units),
        )

    async def allow_risk(_intent, _position):
        return RiskDecision(True, RiskDecisionCode.ALLOWED, "allowed")

    return PositionExitWorker(
        store=store,
        execution_ports={"paper": port},
        finalized_slot_resolver=finalized_slot,
        evidence_resolver=evidence,
        risk_evaluator=allow_risk,
        poll_interval_seconds=0.01,
    )


def test_tp_exit_runs_without_any_launch_event(tmp_path: Path) -> None:
    store = SqliteStateStore(tmp_path / "state.sqlite3")
    market_id = str(Pubkey.new_unique())
    store.save(_position(market_id))
    port = _AcceptingExecutionPort()

    cycle = asyncio.run(_worker(store, port, pnl_ppm=200_000).run_once())

    assert cycle.as_of_slot == 11
    assert len(cycle.results) == 1
    assert cycle.results[0].action == "sell"
    assert cycle.results[0].error is None
    assert port.intents[0].base_amount_base_units == 1_000
    assert store.get(market_id) is None
    store.close()


def test_manual_half_then_restart_then_full_exit(tmp_path: Path) -> None:
    database_path = tmp_path / "state.sqlite3"
    market_id = str(Pubkey.new_unique())
    first_store = SqliteStateStore(database_path)
    first_store.save(_position(market_id))
    first_port = _AcceptingExecutionPort()

    first = asyncio.run(
        _worker(first_store, first_port).execute_manual_exit(
            market_id,
            sell_fraction_ppm=MANUAL_HALF_EXIT_PPM,
            as_of_slot=11,
        )
    )
    assert first.error is None
    assert first.intent.base_amount_base_units == 500
    assert first_store.get(market_id).current_position_base_units == 500
    first_store.close()

    reopened_store = SqliteStateStore(database_path)
    second_port = _AcceptingExecutionPort()
    second = asyncio.run(
        _worker(reopened_store, second_port).execute_manual_exit(
            market_id,
            sell_fraction_ppm=MANUAL_FULL_EXIT_PPM,
            as_of_slot=12,
        )
    )

    assert second.error is None
    assert second.intent.base_amount_base_units == 500
    assert reopened_store.get(market_id) is None
    reopened_store.close()


def test_manual_exit_rejects_unsupported_fraction(tmp_path: Path) -> None:
    store = SqliteStateStore(tmp_path / "state.sqlite3")
    market_id = str(Pubkey.new_unique())
    store.save(_position(market_id))

    with pytest.raises(PositionExitWorkerError, match="50% or 100%"):
        asyncio.run(
            _worker(store, _AcceptingExecutionPort()).execute_manual_exit(
                market_id,
                sell_fraction_ppm=250_000,
                as_of_slot=11,
            )
        )
    store.close()
