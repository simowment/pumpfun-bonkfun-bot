"""Operator integration test for direct TUI-to-daemon position commands."""

from __future__ import annotations

import asyncio
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from solders.pubkey import Pubkey
from textual.widgets import DataTable, TabbedContent

from rugbot.domain.amounts import Lamports, Slot, TokenBaseUnits
from rugbot.execution.ports import ExecutionMode, ExecutionReceipt
from rugbot.execution.position_runtime import (
    PaperPositionState,
    PositionMarketEvidence,
)
from rugbot.runtime.risk_gatekeeper import ExecutionCostBudget, RiskLimits, RiskSnapshot
from rugbot.runtime.sniper_daemon import SniperDaemonService
from rugbot.storage.database import DatabaseManager
from rugbot.storage.sqlite_state_store import SqliteStateStore
from rugbot.storage.tracker import SQLiteTrackerRepository
from rugbot.tracker.models import (
    FunderRecord,
    TargetExecutionMode,
    TargetExecutionPolicy,
)
from rugbot.tui.app import RugbotTuiApp

_POSITION_MUTATION_ERROR = "TUI daemon command did not mutate the durable position"


class _TuiDaemonTestApp(RugbotTuiApp):
    async def _poll_observation_worker(self) -> None:
        await asyncio.Event().wait()


class _ExitPort:
    def __init__(self) -> None:
        self.intents = []

    async def submit(self, intent):
        self.intents.append(intent)
        return ExecutionReceipt(
            mode=ExecutionMode.SIMULATION,
            intent_id=intent.intent_id,
            as_of_slot=intent.as_of_slot,
            accepted=True,
            would_submit_transaction=False,
            signature=None,
            simulated_output_base_units=1,
            estimated_fee_lamports=Lamports(5_000),
            message="fixture sell accepted",
        )


async def _wait_for_position_amount(
    pilot,
    positions: SqliteStateStore,
    market_id: str,
    expected_amount: int | None,
) -> None:
    for _ in range(20):
        await pilot.pause()
        position = positions.get(market_id)
        amount = None if position is None else int(position.current_position_base_units)
        if amount == expected_amount:
            return
    raise AssertionError(_POSITION_MUTATION_ERROR)


def test_tui_shortcuts_sell_daemon_position_in_sqlite() -> None:
    with tempfile.TemporaryDirectory() as temporary_directory:
        asyncio.run(_exercise_tui_daemon_bridge(Path(temporary_directory)))


async def _exercise_tui_daemon_bridge(tmp_path: Path) -> None:
    target_id = str(Pubkey.new_unique())
    market_id = str(Pubkey.new_unique())
    state_dir = tmp_path / "state"
    config_path = tmp_path / "watch.yaml"
    config_path.write_text(
        "target:\n"
        "  kind: wallet\n"
        f"  id: {target_id}\n"
        "execution:\n"
        "  mode: simulation\n"
        "  quote_size_lamports: 25000000\n"
        f"  signer_pubkey: {Pubkey.new_unique()!s}\n",
        encoding="utf-8",
    )
    database = DatabaseManager(state_dir / "rugbot.db")
    repository = SQLiteTrackerRepository(database)
    now = datetime.now(UTC).isoformat()
    repository.save_funder(
        FunderRecord(None, target_id, "operator target", True, now, now)
    )
    repository.save_target_execution_policy(
        TargetExecutionPolicy(
            funder_address=target_id,
            monitoring_enabled=True,
            execution_mode=TargetExecutionMode.SIMULATED,
            quote_size_lamports=25_000_000,
            take_profit_pnl_ppm=500_000,
            stop_loss_pnl_ppm=-200_000,
            max_slippage_bps=500,
            priority_fee_microlamports=50_000,
            jito_tip_lamports=1_000_000,
            updated_at=now,
        )
    )
    positions = SqliteStateStore(state_dir / "state.sqlite3")
    positions.save(
        PaperPositionState(
            as_of_slot=Slot(100),
            market_id=market_id,
            target_id=target_id,
            execution_mode=TargetExecutionMode.SIMULATED.value,
            original_position_base_units=TokenBaseUnits(1_000),
            current_position_base_units=TokenBaseUnits(1_000),
            entry_quote_lamports=25_000_000,
            entry_cost_lamports=1_000_000,
            take_profit_pnl_ppm=500_000,
            stop_loss_pnl_ppm=-200_000,
            max_slippage_bps=500,
        )
    )
    port = _ExitPort()

    async def risk_snapshot(_intent):
        return RiskSnapshot(1_000_000_000, 0, 0, 1, 1_000, False)

    async def costs(_policy):
        return ExecutionCostBudget(5_000, 1_000_000, 2_039_280)

    async def finalized_slot():
        position = positions.get(market_id)
        return (int(position.as_of_slot) if position is not None else 101) + 1

    async def evidence(position, as_of_slot):
        return PositionMarketEvidence(
            as_of_slot=Slot(as_of_slot),
            market_id=position.market_id,
            current_pnl_ppm=0,
            idle_ms=0,
            executable_exit_capacity_base_units=(position.current_position_base_units),
        )

    daemon = SniperDaemonService(
        policy_store=repository,
        position_store=positions,
        execution_ports={TargetExecutionMode.SIMULATED.value: port},
        risk_limits=RiskLimits(100_000_000, 500_000_000, 100_000_000, 5, 1_000, 0),
        risk_snapshot_resolver=risk_snapshot,
        cost_budget_resolver=costs,
        finalized_slot_resolver=finalized_slot,
        evidence_resolver=evidence,
        exit_poll_interval_seconds=60,
    )
    app = _TuiDaemonTestApp(
        config_path=config_path,
        state_dir=state_dir,
        refresh_seconds=99_999,
        sniper_daemon=daemon,
    )

    try:
        async with app.run_test(size=(120, 36)) as pilot:
            await pilot.pause()
            await pilot.press("f3")
            await pilot.pause()
            assert app.query_one(TabbedContent).active == "positions-tab"
            assert app.query_one("#positions-table", DataTable).row_count == 1

            await pilot.press("h")
            await _wait_for_position_amount(pilot, positions, market_id, 500)

            await pilot.press("e")
            await _wait_for_position_amount(pilot, positions, market_id, None)

        assert [intent.side for intent in port.intents] == ["sell", "sell"]
    finally:
        positions.close()
        database.close()
