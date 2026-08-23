"""Production composition for the single-wallet sniper daemon."""

# Strict RPC/config failures are translated at the CLI/TUI boundary.
# ruff: noqa: TRY003

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from rugbot.execution.route_simulation import SimulationPumpExecutionPort
from sol_trade_sdk.pump.accounts import (
    TOKEN_2022_PROGRAM,
    TOKEN_ACCOUNT_RENT_EXEMPT_RESERVE,
)
from solders.pubkey import Pubkey
from spl.token.instructions import get_associated_token_address

from rugbot.decision.risk_gatekeeper import (
    ExecutionCostBudget,
    RiskLimits,
    RiskSnapshot,
)
from rugbot.domain.decisions import AbstainResult
from rugbot.execution.live import LivePumpExecutionPort
from rugbot.execution.sender import RoutingPolicy
from rugbot.integrations.solana_rpc import SolanaClient
from rugbot.runtime.config import CoreSniperConfig, ExecutionMode
from rugbot.runtime.execution_factory import build_execution_port
from rugbot.runtime.market.pump_market import PumpOnlineMarket
from rugbot.runtime.workers.sniper_daemon import (
    ProcessedTargetLaunch,
    SniperDaemonService,
)
from rugbot.storage.database import DatabaseManager
from rugbot.storage.sqlite_state_store import SqliteStateStore
from rugbot.storage.tracker import SQLiteTrackerRepository
from rugbot.storage.transaction_state import (
    SqliteTransactionStateStore,
    TransactionIntentRecord,
    TransactionState,
)
from rugbot.tracker.models import (
    FunderRecord,
    TargetExecutionMode,
    TargetExecutionPolicy,
)

if TYPE_CHECKING:
    from pathlib import Path

    from rugbot.execution.ports import ExecutionIntent, ExecutionPort
    from rugbot.execution.position_runtime import (
        PaperPositionState,
        PositionMarketEvidence,
    )
    from rugbot.ingest.pump.pump_stream import ProcessedPumpCreateNotification

BASE_SIGNATURE_FEE_LAMPORTS = 5_000
BUY_ATA_COUNT = 2


class SniperRuntimeError(ValueError):
    """Raised when real runtime evidence is incomplete or malformed."""


class WalletRiskSnapshotResolver:
    """Resolve last-moment wallet, token, exposure, and realized-PnL facts."""

    def __init__(
        self,
        *,
        endpoint: str,
        wallet_pubkey: str,
        positions: SqliteStateStore,
        transactions: SqliteTransactionStateStore | None,
    ) -> None:
        self._client = SolanaClient(endpoint)
        self._wallet = Pubkey.from_string(wallet_pubkey)
        self._positions = positions
        self._transactions = transactions

    async def close(self) -> None:
        """Close the owned RPC client."""

        await self._client.close()

    async def __call__(self, intent: ExecutionIntent) -> RiskSnapshot:
        """Read a coherent conservative snapshot immediately before execution."""

        positions = self._positions.read_all()
        balance = await self._wallet_balance()
        token_balance = 0
        if intent.side == "sell":
            token_balance = await self._token_balance(intent.market_id)
        daily_pnl = (
            _daily_realized_pnl(self._transactions.list_all())
            if intent.side == "buy" and self._transactions is not None
            else 0
        )
        return RiskSnapshot(
            wallet_balance_lamports=balance,
            current_exposure_lamports=sum(
                position.entry_quote_lamports + position.entry_cost_lamports
                for position in positions
            ),
            daily_realized_pnl_lamports=daily_pnl,
            open_positions_count=len(positions),
            position_token_balance_base_units=token_balance,
            kill_switch_active=False,
        )

    async def processed_slot(self) -> int:
        """Read the current processed slot for launch freshness enforcement."""

        response = await self._client.post_rpc(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "getSlot",
                "params": [{"commitment": "processed"}],
            }
        )
        if type(response) is not dict or response.get("error") is not None:
            raise SniperRuntimeError("getSlot RPC response is malformed")
        slot = response.get("result")
        if type(slot) is not int or slot < 0:
            raise SniperRuntimeError("getSlot RPC result is malformed")
        return slot

    async def _wallet_balance(self) -> int:
        response = await self._client.post_rpc(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "getBalance",
                "params": [str(self._wallet), {"commitment": "confirmed"}],
            }
        )
        value = _rpc_value(response, "getBalance")
        if type(value) is not int or value < 0:
            raise SniperRuntimeError("getBalance result is malformed")
        return value

    async def _token_balance(self, mint: str) -> int:
        token_account = get_associated_token_address(
            self._wallet,
            Pubkey.from_string(mint),
            TOKEN_2022_PROGRAM,
        )
        response = await self._client.post_rpc(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "getTokenAccountBalance",
                "params": [str(token_account), {"commitment": "confirmed"}],
            }
        )
        value = _rpc_value(response, "getTokenAccountBalance")
        amount = value.get("amount") if type(value) is dict else None
        if type(amount) is not str or not amount.isdecimal():
            raise SniperRuntimeError("getTokenAccountBalance result is malformed")
        return int(amount)


@dataclass(slots=True)
class SniperRuntime:
    """Owned daemon and adapters used by one CLI or TUI process."""

    daemon: SniperDaemonService
    positions: SqliteStateStore
    market: PumpOnlineMarket
    risk_resolver: WalletRiskSnapshotResolver
    execution_port: SimulationPumpExecutionPort | LivePumpExecutionPort
    policy_database: DatabaseManager
    transaction_reader: SqliteTransactionStateStore | None

    async def close(self) -> None:
        """Stop workers and close all owned network and persistence resources."""

        await self.daemon.stop()
        await self.execution_port.close()
        await self.risk_resolver.close()
        await self.market.close()
        if self.transaction_reader is not None:
            self.transaction_reader.close()
        self.positions.close()
        self.policy_database.close()

    async def handle_processed_create(
        self,
        notification: ProcessedPumpCreateNotification,
    ) -> None:
        """Translate one decoded processed create event into a daemon command."""

        await self.daemon.handle_processed_launch(
            ProcessedTargetLaunch(
                target_id=notification.creator_pubkey,
                market_id=notification.mint_pubkey,
                signature=notification.signature,
                slot=notification.slot,
            ),
            current_processed_slot=await self.risk_resolver.processed_slot(),
        )


def build_sniper_runtime(
    *,
    config: CoreSniperConfig,
    endpoint: str,
    state_dir: Path,
    execution_port_override: ExecutionPort | None = None,
) -> SniperRuntime | None:
    """Build the real simulation/live runtime, or no runtime for observe mode."""

    if config.execution.mode in (ExecutionMode.OBSERVE, ExecutionMode.PAPER):
        return None
    signer_pubkey = config.execution.signer_pubkey
    if signer_pubkey is None:
        raise SniperRuntimeError("execution.signer_pubkey is required for sniper mode")

    transaction_path = state_dir / "transactions.sqlite3"
    execution_port = execution_port_override or build_execution_port(
        config.execution.mode,
        endpoint,
        expected_signer_pubkey=signer_pubkey,
        execution=config.execution,
        transaction_state_path=transaction_path,
    )
    if not isinstance(
        execution_port,
        (SimulationPumpExecutionPort, LivePumpExecutionPort),
    ):
        raise SniperRuntimeError("sniper execution port is not operational")

    positions = SqliteStateStore(state_dir / "state.sqlite3")
    policy_database = DatabaseManager(state_dir / "rugbot.db")
    policy_store = SQLiteTrackerRepository(policy_database)
    _ensure_target_policy(policy_store, config)
    market = PumpOnlineMarket(endpoint)
    transaction_reader = (
        SqliteTransactionStateStore(transaction_path)
        if config.execution.mode is ExecutionMode.LIVE
        else None
    )
    risk_resolver = WalletRiskSnapshotResolver(
        endpoint=endpoint,
        wallet_pubkey=signer_pubkey,
        positions=positions,
        transactions=transaction_reader,
    )

    async def costs(policy: TargetExecutionPolicy) -> ExecutionCostBudget:
        priority_fee = (
            config.execution.compute_unit_limit
            * policy.priority_fee_microlamports
            // 1_000_000
        )
        return ExecutionCostBudget(
            network_fee_lamports=BASE_SIGNATURE_FEE_LAMPORTS + priority_fee,
            jito_tip_lamports=(
                policy.jito_tip_lamports
                if config.execution.routing_policy == RoutingPolicy.JITO_ONLY.value
                else 0
            ),
            ata_rent_lamports=BUY_ATA_COUNT * TOKEN_ACCOUNT_RENT_EXEMPT_RESERVE,
        )

    async def finalized_slot() -> int:
        value = await market.finalized_slot()
        if isinstance(value, AbstainResult):
            raise SniperRuntimeError(value.message)
        return value

    async def evidence(
        position: PaperPositionState,
        as_of_slot: int,
    ) -> PositionMarketEvidence | AbstainResult | None:
        result = await market.position_evidence_at_slot(
            position,
            as_of_slot=as_of_slot,
            entry_quote_lamports=position.entry_quote_lamports,
        )
        return result

    mode = "live" if config.execution.mode is ExecutionMode.LIVE else "simulated"
    limits = config.risk
    daemon = SniperDaemonService(
        policy_store=policy_store,
        position_store=positions,
        execution_ports={mode: execution_port},
        risk_limits=RiskLimits(
            max_buy_lamports=limits.max_buy_lamports,
            max_exposure_lamports=limits.max_exposure_lamports,
            daily_loss_limit_lamports=limits.daily_loss_limit_lamports,
            max_open_positions=limits.max_open_positions,
            max_slippage_bps=config.execution.max_slippage_bps,
            minimum_wallet_reserve_lamports=(limits.minimum_wallet_reserve_lamports),
        ),
        risk_snapshot_resolver=risk_resolver,
        cost_budget_resolver=costs,
        finalized_slot_resolver=finalized_slot,
        evidence_resolver=evidence,
    )
    return SniperRuntime(
        daemon=daemon,
        positions=positions,
        market=market,
        risk_resolver=risk_resolver,
        execution_port=execution_port,
        policy_database=policy_database,
        transaction_reader=transaction_reader,
    )


def _ensure_target_policy(
    policy_store: SQLiteTrackerRepository,
    config: CoreSniperConfig,
) -> None:
    """Seed the configured target without overwriting operator-local policy."""

    target_id = config.target.id
    now = datetime.now(UTC).isoformat()
    if policy_store.get_funder(target_id) is None:
        policy_store.save_funder(
            FunderRecord(
                id=None,
                address=target_id,
                label="Configured target",
                enabled=True,
                created_at=now,
                last_seen_at=now,
            )
        )
    if policy_store.get_target_execution_policy(target_id) is not None:
        return
    take_profit = (
        config.rules.sell.take_profit_levels[0].trigger_pnl_ppm
        if config.rules.sell.take_profit_levels
        else 0
    )
    stop_loss = (
        config.rules.sell.stop_loss_levels[0].trigger_pnl_ppm
        if config.rules.sell.stop_loss_levels
        else 0
    )
    policy_store.save_target_execution_policy(
        TargetExecutionPolicy(
            funder_address=target_id,
            monitoring_enabled=True,
            execution_mode=(
                TargetExecutionMode.LIVE
                if config.execution.mode is ExecutionMode.LIVE
                else TargetExecutionMode.SIMULATED
            ),
            quote_size_lamports=config.execution.quote_size_lamports,
            take_profit_pnl_ppm=take_profit,
            stop_loss_pnl_ppm=stop_loss,
            max_slippage_bps=config.execution.max_slippage_bps,
            priority_fee_microlamports=(config.execution.priority_fee_microlamports),
            jito_tip_lamports=config.execution.jito_tip_lamports,
            updated_at=now,
        )
    )


def _rpc_value(response: object, method: str) -> object:
    if type(response) is not dict or response.get("error") is not None:
        raise SniperRuntimeError(f"{method} RPC response is malformed")
    result = response.get("result")
    if type(result) is not dict or "value" not in result:
        raise SniperRuntimeError(f"{method} RPC result is malformed")
    return result["value"]


def _daily_realized_pnl(records: tuple[TransactionIntentRecord, ...]) -> int:
    """Calculate today's realized PnL from exact reconciled wallet deltas."""

    day_start = datetime.now(UTC).replace(
        hour=0,
        minute=0,
        second=0,
        microsecond=0,
    )
    day_start_ms = int(day_start.timestamp() * 1_000)
    by_market: dict[str, list[TransactionIntentRecord]] = {}
    for record in records:
        if record.state is TransactionState.RECONCILED:
            by_market.setdefault(record.market_id, []).append(record)

    realized = 0
    for market_records in by_market.values():
        buys = [record for record in market_records if record.side == "buy"]
        sells_today = [
            record
            for record in market_records
            if record.side == "sell"
            and record.reconciled_at_ts is not None
            and record.reconciled_at_ts >= day_start_ms
        ]
        if not sells_today:
            continue
        if len(buys) != 1:
            raise SniperRuntimeError(
                "daily realized PnL requires exactly one reconciled entry per market"
            )
        buy = buys[0]
        if (
            buy.token_delta_base_units is None
            or buy.token_delta_base_units <= 0
            or buy.sol_delta_lamports is None
            or buy.sol_delta_lamports >= 0
        ):
            raise SniperRuntimeError("reconciled entry economics are malformed")
        for sell in sells_today:
            if (
                sell.token_delta_base_units is None
                or sell.token_delta_base_units >= 0
                or sell.sol_delta_lamports is None
            ):
                raise SniperRuntimeError("reconciled exit economics are malformed")
            sold_units = -sell.token_delta_base_units
            entry_basis = (
                -buy.sol_delta_lamports * sold_units // buy.token_delta_base_units
            )
            realized += sell.sol_delta_lamports - entry_basis
    return realized


__all__ = [
    "SniperRuntime",
    "SniperRuntimeError",
    "WalletRiskSnapshotResolver",
    "build_sniper_runtime",
]
