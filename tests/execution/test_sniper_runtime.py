"""Integration coverage for the production sniper runtime composition."""

from __future__ import annotations

import asyncio
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
from typing import ClassVar

from solders.pubkey import Pubkey

from rugbot.domain.amounts import Slot, TokenBaseUnits
from rugbot.execution.ports import ExecutionIntent
from rugbot.execution.position_runtime import PaperPositionState
from rugbot.runtime.config import parse_sniper_config
from rugbot.runtime.sniper_runtime import (
    WalletRiskSnapshotResolver,
    build_sniper_runtime,
)
from rugbot.storage.database import DatabaseManager
from rugbot.storage.sqlite_state_store import SqliteStateStore
from rugbot.storage.tracker import SQLiteTrackerRepository


class _RpcHandler(BaseHTTPRequestHandler):
    requests: ClassVar[list[dict[str, object]]] = []

    def do_POST(self) -> None:
        length = int(self.headers["Content-Length"])
        request = json.loads(self.rfile.read(length))
        type(self).requests.append(request)
        method = request["method"]
        if method == "getBalance":
            result = {"context": {"slot": 901}, "value": 500_000_000}
        elif method == "getTokenAccountBalance":
            result = {
                "context": {"slot": 901},
                "value": {"amount": "750", "decimals": 6},
            }
        elif method == "getSlot":
            result = 901
        else:
            raise AssertionError(method)
        body = json.dumps({"jsonrpc": "2.0", "id": 1, "result": result}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, _format: str, *_args: object) -> None:
        return


def test_wallet_risk_snapshot_uses_real_http_and_sqlite_boundaries(tmp_path) -> None:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _RpcHandler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    endpoint = f"http://127.0.0.1:{server.server_port}"
    wallet = str(Pubkey.new_unique())
    market = str(Pubkey.new_unique())
    positions = SqliteStateStore(tmp_path / "state.sqlite3")
    positions.save(
        PaperPositionState(
            as_of_slot=Slot(900),
            market_id=market,
            target_id=str(Pubkey.new_unique()),
            execution_mode="live",
            original_position_base_units=TokenBaseUnits(1_000),
            current_position_base_units=TokenBaseUnits(750),
            entry_quote_lamports=25_000_000,
            entry_cost_lamports=2_000_000,
            take_profit_pnl_ppm=120_000,
            stop_loss_pnl_ppm=-20_000,
            max_slippage_bps=500,
        )
    )
    resolver = WalletRiskSnapshotResolver(
        endpoint=endpoint,
        wallet_pubkey=wallet,
        positions=positions,
        transactions=None,
    )

    async def exercise() -> None:
        intent = ExecutionIntent(
            intent_id="manual-sell",
            as_of_slot=Slot(901),
            market_id=market,
            side="sell",
            quote_amount_base_units=None,
            base_amount_base_units=500,
            max_slippage_bps=500,
            reason_codes=("manual_exit_50",),
        )
        snapshot = await resolver(intent)
        assert snapshot.wallet_balance_lamports == 500_000_000
        assert snapshot.current_exposure_lamports == 27_000_000
        assert snapshot.open_positions_count == 1
        assert snapshot.position_token_balance_base_units == 750
        assert await resolver.processed_slot() == 901
        await resolver.close()

    try:
        asyncio.run(exercise())
        methods = [request["method"] for request in _RpcHandler.requests]
        assert methods[-3:] == ["getBalance", "getTokenAccountBalance", "getSlot"]
    finally:
        asyncio.run(resolver.close())
        positions.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_runtime_build_seeds_target_policy_in_project_sqlite(tmp_path) -> None:
    target = str(Pubkey.new_unique())
    signer = str(Pubkey.new_unique())
    config = parse_sniper_config(
        f"""
target:
  kind: wallet
  id: {target}
execution:
  mode: simulation
  quote_size_lamports: 25000000
  signer_pubkey: {signer}
risk:
  max_buy_lamports: 25000000
  max_exposure_lamports: 25000000
  daily_loss_limit_lamports: 25000000
  max_open_positions: 1
  minimum_wallet_reserve_lamports: 15000000
rules:
  sell:
    take_profit_levels:
      - trigger_pnl_ppm: 120000
        sell_fraction_ppm: 1000000
    stop_loss_levels:
      - trigger_pnl_ppm: -20000
        sell_fraction_ppm: 1000000
"""
    )
    runtime = build_sniper_runtime(
        config=config,
        endpoint="http://127.0.0.1:1",
        state_dir=tmp_path,
    )
    assert runtime is not None
    database = DatabaseManager(tmp_path / "rugbot.db")
    try:
        policy = SQLiteTrackerRepository(database).get_target_execution_policy(target)
        assert policy is not None
        assert policy.quote_size_lamports == 25_000_000
        assert policy.take_profit_pnl_ppm == 120_000
        assert policy.stop_loss_pnl_ppm == -20_000
    finally:
        database.close()
        asyncio.run(runtime.close())
