"""Unified, developer-friendly Trading SDK and order execution service for Pump.fun."""

# ruff: noqa: C901, PLR0912, PLR0913, PLR0915, PLR2004, TRY003, TRY301, BLE001, S110

from __future__ import annotations

import asyncio
import os
import sqlite3
import time
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

import base58
from solders.compute_budget import set_compute_unit_limit, set_compute_unit_price
from solders.keypair import Keypair
from solders.message import Message
from solders.pubkey import Pubkey
from solders.transaction import Transaction

from rugbot.domain.amounts import Slot
from rugbot.execution.auto_router import AutoRouter, RouteVenue
from rugbot.execution.live import LivePumpExecutionPort
from rugbot.execution.ports import (
    ExecutionIntent,
    ExecutionMode,
    ExecutionReceipt,
)
from rugbot.execution.pumpswap_builder import (
    PUMPSWAP_BUY_COMPUTE_UNITS,
    PUMPSWAP_SELL_COMPUTE_UNITS,
    build_pumpswap_buy_exact_quote_in_instructions,
    build_pumpswap_sell_instructions,
)
from rugbot.execution.sender import (
    JitoSender,
    RoutingPolicy,
    TransactionRouter,
    create_jito_tip_instruction,
)
from rugbot.integrations.solana_rpc import SolanaClient
from rugbot.runtime.config import (
    PUBKEY_LENGTH,
    load_provider_settings,
    resolve_dotenv,
)
from rugbot.simulation.route_simulation import SimulationPumpExecutionPort
from rugbot.utils.logger import get_logger

if TYPE_CHECKING:
    from solders.instruction import Instruction

logger = get_logger(__name__)

LAMPORTS_PER_SOL = 1_000_000_000
MICROLAMPORTS_PER_SOL = 1_000_000_000_000
PPM_DENOMINATOR = 1_000_000
DEFAULT_BUY_SLIPPAGE_PCT = 5.0
DEFAULT_SELL_SLIPPAGE_PCT = 10.0
DEFAULT_JITO_TIP_SOL = 0.001
DEFAULT_PRIORITY_FEE_SOL = 0.0005
DUMMY_SIMULATION_SIGNER = "11111111111111111111111111111111"


class TradeSide(StrEnum):
    """Execution trade action."""

    BUY = "buy"
    SELL = "sell"


@dataclass(frozen=True, slots=True)
class BuyOrderSpec:
    """Input parameters for a token purchase."""

    mint: str
    amount_sol: float
    slippage_pct: float = DEFAULT_BUY_SLIPPAGE_PCT
    priority_fee_sol: float = DEFAULT_PRIORITY_FEE_SOL
    jito_tip_sol: float = DEFAULT_JITO_TIP_SOL
    routing: Literal["auto", "rpc", "jito"] = "auto"
    mode: ExecutionMode = ExecutionMode.DRY_RUN
    take_profit_pct: float | None = None
    stop_loss_pct: float | None = None
    trailing_stop_pct: float | None = None
    max_hold_seconds: float | None = None
    creator: str | None = None

    def validate(self) -> None:
        """Validate buy order parameters."""
        if not self.mint or len(self.mint.strip()) < 32:
            raise ValueError("mint must be a valid Solana address")
        try:
            raw_pubkey = base58.b58decode(self.mint.strip())
            if len(raw_pubkey) != PUBKEY_LENGTH:
                raise ValueError("mint is not a 32-byte Solana pubkey")
        except Exception as exc:
            raise ValueError(f"invalid mint address: {exc}") from exc

        if self.amount_sol <= 0.0:
            raise ValueError("amount_sol must be positive")
        if not 0.0 <= self.slippage_pct <= 100.0:
            raise ValueError("slippage_pct must be between 0.0 and 100.0")
        if self.priority_fee_sol < 0.0:
            raise ValueError("priority_fee_sol must be non-negative")
        if self.jito_tip_sol < 0.0:
            raise ValueError("jito_tip_sol must be non-negative")
        if self.take_profit_pct is not None and self.take_profit_pct <= 0.0:
            raise ValueError("take_profit_pct must be positive")
        if self.stop_loss_pct is not None and self.stop_loss_pct <= 0.0:
            raise ValueError("stop_loss_pct must be positive")
        if self.max_hold_seconds is not None and self.max_hold_seconds <= 0.0:
            raise ValueError("max_hold_seconds must be positive")

    @property
    def quote_lamports(self) -> int:
        """Convert SOL to lamports."""
        return round(self.amount_sol * LAMPORTS_PER_SOL)

    @property
    def max_slippage_bps(self) -> int:
        """Convert slippage percentage to basis points."""
        return round(self.slippage_pct * 100)

    @property
    def priority_fee_microlamports(self) -> int:
        """Convert priority fee SOL to microlamports."""
        return round(self.priority_fee_sol * MICROLAMPORTS_PER_SOL)

    @property
    def jito_tip_lamports(self) -> int:
        """Convert Jito tip SOL to lamports."""
        return round(self.jito_tip_sol * LAMPORTS_PER_SOL)

    @property
    def take_profit_pnl_ppm(self) -> int | None:
        """Convert TP percentage to PPM."""
        return (
            round((self.take_profit_pct / 100.0) * PPM_DENOMINATOR)
            if self.take_profit_pct is not None
            else None
        )

    @property
    def stop_loss_pnl_ppm(self) -> int | None:
        """Convert SL percentage to negative PPM."""
        return (
            -round((self.stop_loss_pct / 100.0) * PPM_DENOMINATOR)
            if self.stop_loss_pct is not None
            else None
        )


@dataclass(frozen=True, slots=True)
class SellOrderSpec:
    """Input parameters for a token sale."""

    mint: str
    percent: float = 100.0
    amount_tokens: int | None = None
    slippage_pct: float = DEFAULT_SELL_SLIPPAGE_PCT
    priority_fee_sol: float = DEFAULT_PRIORITY_FEE_SOL
    jito_tip_sol: float = DEFAULT_JITO_TIP_SOL
    routing: Literal["auto", "rpc", "jito"] = "auto"
    mode: ExecutionMode = ExecutionMode.DRY_RUN

    def validate(self) -> None:
        """Validate sell order parameters."""
        if not self.mint or len(self.mint.strip()) < 32:
            raise ValueError("mint must be a valid Solana address")
        try:
            raw_pubkey = base58.b58decode(self.mint.strip())
            if len(raw_pubkey) != PUBKEY_LENGTH:
                raise ValueError("mint is not a 32-byte Solana pubkey")
        except Exception as exc:
            raise ValueError(f"invalid mint address: {exc}") from exc

        if not 0.0 < self.percent <= 100.0 and self.amount_tokens is None:
            raise ValueError("percent must be between 0.0 and 100.0")
        if self.amount_tokens is not None and self.amount_tokens <= 0:
            raise ValueError("amount_tokens must be positive")
        if not 0.0 <= self.slippage_pct <= 100.0:
            raise ValueError("slippage_pct must be between 0.0 and 100.0")
        if self.priority_fee_sol < 0.0:
            raise ValueError("priority_fee_sol must be non-negative")
        if self.jito_tip_sol < 0.0:
            raise ValueError("jito_tip_sol must be non-negative")

    @property
    def max_slippage_bps(self) -> int:
        """Convert slippage percentage to basis points."""
        return round(self.slippage_pct * 100)

    @property
    def priority_fee_microlamports(self) -> int:
        """Convert priority fee SOL to microlamports."""
        return round(self.priority_fee_sol * MICROLAMPORTS_PER_SOL)

    @property
    def jito_tip_lamports(self) -> int:
        """Convert Jito tip SOL to lamports."""
        return round(self.jito_tip_sol * LAMPORTS_PER_SOL)


@dataclass(frozen=True, slots=True)
class TradeResult:
    """Execution receipt and outcome details."""

    ok: bool
    side: TradeSide
    mint: str
    mode: ExecutionMode
    sol_amount: float
    token_amount: int
    signature: str | None = None
    effective_price_sol: float = 0.0
    fee_sol: float = 0.0
    slot: int = 0
    message: str = ""
    take_profit_pct: float | None = None
    stop_loss_pct: float | None = None
    realized_pnl_sol: float | None = None
    realized_pnl_pct: float | None = None
    error: str | None = None


@dataclass(slots=True)
class ActivePosition:
    """An open trading position with automated exit brackets."""

    mint: str
    entry_sol: float
    token_amount: int
    entry_price_sol: float
    entry_slot: int
    mode: ExecutionMode
    entry_fees_sol: float = 0.0
    take_profit_pct: float | None = None
    stop_loss_pct: float | None = None
    trailing_stop_pct: float | None = None
    max_hold_seconds: float | None = None
    peak_price_sol: float = 0.0
    current_pnl_pct: float = 0.0
    current_value_sol: float = 0.0
    unrealized_pnl_sol: float = 0.0
    opened_at_ts: float = field(default_factory=time.time)


class TradingService:
    """Unified trading client for executing and managing Pump.fun trades across Dry-Run and Live modes."""

    def __init__(
        self,
        *,
        endpoint: str | None = None,
        private_key: str | None = None,
        default_mode: ExecutionMode = ExecutionMode.DRY_RUN,
        default_routing: RoutingPolicy = RoutingPolicy.RPC_ONLY,
        db_path: Path | str = Path(".state/trading.sqlite3"),
        auto_router: AutoRouter | None = None,
    ) -> None:
        resolve_dotenv()
        providers = load_provider_settings()
        self._endpoint = (
            endpoint or providers.rpc_http or "https://api.mainnet-beta.solana.com"
        )
        self._private_key = private_key or os.environ.get("SOLANA_PRIVATE_KEY")
        self._default_mode = default_mode
        self._default_routing = default_routing
        self._db_path = Path(db_path)
        self._auto_router = auto_router or AutoRouter(endpoint=self._endpoint)
        self._positions: dict[str, ActivePosition] = {}
        self._closed_trades: list[dict[str, Any]] = []
        self._lock = asyncio.Lock()
        self._init_db()
        self._load_from_db()

    @property
    def auto_router(self) -> AutoRouter:
        """Return the AutoRouter instance."""
        return self._auto_router

    def _init_db(self) -> None:
        try:
            self._db_path.parent.mkdir(parents=True, exist_ok=True)
            with sqlite3.connect(str(self._db_path)) as conn:
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS active_positions (
                        mint TEXT PRIMARY KEY,
                        entry_sol REAL NOT NULL,
                        entry_fees_sol REAL NOT NULL,
                        token_amount INTEGER NOT NULL,
                        entry_price_sol REAL NOT NULL,
                        entry_slot INTEGER NOT NULL,
                        mode TEXT NOT NULL,
                        take_profit_pct REAL,
                        stop_loss_pct REAL,
                        trailing_stop_pct REAL,
                        max_hold_seconds REAL,
                        peak_price_sol REAL NOT NULL,
                        current_pnl_pct REAL NOT NULL,
                        current_value_sol REAL NOT NULL,
                        unrealized_pnl_sol REAL NOT NULL,
                        opened_at_ts REAL NOT NULL
                    )
                """)
                # Alter table migration check
                try:
                    conn.execute(
                        "ALTER TABLE active_positions ADD COLUMN max_hold_seconds REAL"
                    )
                except Exception:
                    pass
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS closed_trades (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        mint TEXT NOT NULL,
                        mode TEXT NOT NULL,
                        tokens_sold INTEGER NOT NULL,
                        sol_proceeds REAL NOT NULL,
                        cost_basis_sol REAL NOT NULL,
                        fees_sol REAL NOT NULL,
                        realized_pnl_sol REAL NOT NULL,
                        realized_pnl_pct REAL NOT NULL,
                        timestamp REAL NOT NULL
                    )
                """)
        except Exception as exc:
            logger.warning("Failed to init trading db: %s", exc)

    def _load_from_db(self) -> None:
        try:
            if not self._db_path.exists():
                return
            with sqlite3.connect(str(self._db_path)) as conn:
                conn.row_factory = sqlite3.Row
                for row in conn.execute("SELECT * FROM active_positions"):
                    keys = row.keys()
                    max_hold = (
                        row["max_hold_seconds"] if "max_hold_seconds" in keys else None
                    )
                    pos = ActivePosition(
                        mint=row["mint"],
                        entry_sol=row["entry_sol"],
                        entry_fees_sol=row["entry_fees_sol"],
                        token_amount=row["token_amount"],
                        entry_price_sol=row["entry_price_sol"],
                        entry_slot=row["entry_slot"],
                        mode=ExecutionMode(row["mode"]),
                        take_profit_pct=row["take_profit_pct"],
                        stop_loss_pct=row["stop_loss_pct"],
                        trailing_stop_pct=row["trailing_stop_pct"],
                        max_hold_seconds=max_hold,
                        peak_price_sol=row["peak_price_sol"],
                        current_pnl_pct=row["current_pnl_pct"],
                        current_value_sol=row["current_value_sol"],
                        unrealized_pnl_sol=row["unrealized_pnl_sol"],
                        opened_at_ts=row["opened_at_ts"],
                    )
                    self._positions[pos.mint] = pos

                for row in conn.execute("SELECT * FROM closed_trades ORDER BY id ASC"):
                    self._closed_trades.append(dict(row))
        except Exception as exc:
            logger.warning("Failed to load trading db: %s", exc)

    def _persist_position(self, pos: ActivePosition) -> None:
        try:
            with sqlite3.connect(str(self._db_path)) as conn:
                conn.execute(
                    """
                    INSERT OR REPLACE INTO active_positions (
                        mint, entry_sol, entry_fees_sol, token_amount, entry_price_sol,
                        entry_slot, mode, take_profit_pct, stop_loss_pct, trailing_stop_pct,
                        max_hold_seconds, peak_price_sol, current_pnl_pct, current_value_sol,
                        unrealized_pnl_sol, opened_at_ts
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        pos.mint,
                        pos.entry_sol,
                        pos.entry_fees_sol,
                        pos.token_amount,
                        pos.entry_price_sol,
                        pos.entry_slot,
                        pos.mode.value,
                        pos.take_profit_pct,
                        pos.stop_loss_pct,
                        pos.trailing_stop_pct,
                        pos.max_hold_seconds,
                        pos.peak_price_sol,
                        pos.current_pnl_pct,
                        pos.current_value_sol,
                        pos.unrealized_pnl_sol,
                        pos.opened_at_ts,
                    ),
                )
        except Exception as exc:
            logger.warning("Failed to persist position: %s", exc)

    def _delete_persisted_position(self, mint: str) -> None:
        try:
            with sqlite3.connect(str(self._db_path)) as conn:
                conn.execute("DELETE FROM active_positions WHERE mint = ?", (mint,))
        except Exception as exc:
            logger.warning("Failed to delete position: %s", exc)

    def _persist_closed_trade(self, trade: dict[str, Any]) -> None:
        try:
            with sqlite3.connect(str(self._db_path)) as conn:
                conn.execute(
                    """
                    INSERT INTO closed_trades (
                        mint, mode, tokens_sold, sol_proceeds, cost_basis_sol,
                        fees_sol, realized_pnl_sol, realized_pnl_pct, timestamp
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        trade["mint"],
                        trade["mode"],
                        trade["tokens_sold"],
                        trade["sol_proceeds"],
                        trade["cost_basis_sol"],
                        trade["fees_sol"],
                        trade["realized_pnl_sol"],
                        trade["realized_pnl_pct"],
                        trade["timestamp"],
                    ),
                )
        except Exception as exc:
            logger.warning("Failed to persist closed trade: %s", exc)

    @property
    def endpoint(self) -> str:
        return self._endpoint

    @property
    def default_mode(self) -> ExecutionMode:
        return self._default_mode

    def get_positions(self) -> list[dict[str, Any]]:
        """Return all currently open positions."""
        return [
            {
                "mint": pos.mint,
                "entry_sol": pos.entry_sol,
                "entry_fees_sol": pos.entry_fees_sol,
                "token_amount": pos.token_amount,
                "entry_price_sol": pos.entry_price_sol,
                "entry_slot": pos.entry_slot,
                "mode": pos.mode.value,
                "take_profit_pct": pos.take_profit_pct,
                "stop_loss_pct": pos.stop_loss_pct,
                "trailing_stop_pct": pos.trailing_stop_pct,
                "current_pnl_pct": pos.current_pnl_pct,
                "current_value_sol": pos.current_value_sol,
                "unrealized_pnl_sol": pos.unrealized_pnl_sol,
                "opened_at_ts": pos.opened_at_ts,
            }
            for pos in self._positions.values()
        ]

    def get_closed_trades(self) -> list[dict[str, Any]]:
        """Return historical closed trade records."""
        return list(self._closed_trades)

    def get_pnl_summary(self) -> dict[str, Any]:
        """Compute aggregated portfolio PnL across closed and open positions."""
        total_trades = len(self._closed_trades)
        wins = sum(
            1 for t in self._closed_trades if (t.get("realized_pnl_sol") or 0.0) > 0
        )
        losses = sum(
            1 for t in self._closed_trades if (t.get("realized_pnl_sol") or 0.0) < 0
        )
        winrate_pct = (wins / total_trades * 100.0) if total_trades > 0 else 0.0

        realized_pnl_sol = sum(
            t.get("realized_pnl_sol", 0.0) for t in self._closed_trades
        )
        total_fees_sol = sum(t.get("fees_sol", 0.0) for t in self._closed_trades)
        unrealized_pnl_sol = sum(p.unrealized_pnl_sol for p in self._positions.values())

        return {
            "total_trades": total_trades,
            "wins": wins,
            "losses": losses,
            "winrate_pct": winrate_pct,
            "realized_pnl_sol": realized_pnl_sol,
            "unrealized_pnl_sol": unrealized_pnl_sol,
            "total_net_pnl_sol": realized_pnl_sol + unrealized_pnl_sol,
            "total_fees_sol": total_fees_sol,
            "open_positions_count": len(self._positions),
        }

    def get_position(self, mint: str) -> ActivePosition | None:
        """Return active position for a mint."""
        return self._positions.get(mint.strip())

    async def buy(
        self,
        mint: str,
        amount_sol: float,
        *,
        slippage_pct: float = DEFAULT_BUY_SLIPPAGE_PCT,
        priority_fee_sol: float = DEFAULT_PRIORITY_FEE_SOL,
        jito_tip_sol: float = DEFAULT_JITO_TIP_SOL,
        routing: Literal["auto", "rpc", "jito"] = "auto",
        mode: ExecutionMode | None = None,
        take_profit_pct: float | None = None,
        stop_loss_pct: float | None = None,
        trailing_stop_pct: float | None = None,
        creator: str | None = None,
    ) -> TradeResult:
        """Execute a Buy order."""
        spec = BuyOrderSpec(
            mint=mint.strip(),
            amount_sol=amount_sol,
            slippage_pct=slippage_pct,
            priority_fee_sol=priority_fee_sol,
            jito_tip_sol=jito_tip_sol,
            routing=routing,
            mode=mode or self._default_mode,
            take_profit_pct=take_profit_pct,
            stop_loss_pct=stop_loss_pct,
            trailing_stop_pct=trailing_stop_pct,
            creator=creator,
        )
        return await self.execute_buy(spec)

    async def sell(
        self,
        mint: str,
        percent: float = 100.0,
        *,
        amount_tokens: int | None = None,
        slippage_pct: float = DEFAULT_SELL_SLIPPAGE_PCT,
        priority_fee_sol: float = DEFAULT_PRIORITY_FEE_SOL,
        jito_tip_sol: float = DEFAULT_JITO_TIP_SOL,
        routing: Literal["auto", "rpc", "jito"] = "auto",
        mode: ExecutionMode | None = None,
    ) -> TradeResult:
        """Execute a Sell order."""
        spec = SellOrderSpec(
            mint=mint.strip(),
            percent=percent,
            amount_tokens=amount_tokens,
            slippage_pct=slippage_pct,
            priority_fee_sol=priority_fee_sol,
            jito_tip_sol=jito_tip_sol,
            routing=routing,
            mode=mode or self._default_mode,
        )
        return await self.execute_sell(spec)

    async def execute_buy(self, spec: BuyOrderSpec) -> TradeResult:
        """Validate and dispatch a Buy order specification using the unified execution pipeline."""
        spec.validate()
        async with self._lock:
            routing_policy = (
                RoutingPolicy.JITO_ONLY
                if spec.routing == "jito"
                else RoutingPolicy.RPC_ONLY
            )

            # Auto-route: detect whether token is on bonding curve or graduated to PumpSwap AMM
            venue = await self._auto_router.detect_venue(spec.mint)
            if venue == RouteVenue.PUMPSWAP_AMM:
                return await self._execute_pumpswap_buy(
                    spec=spec,
                    routing_policy=routing_policy,
                )

            intent = ExecutionIntent(
                intent_id=f"buy_{int(time.time_ns())}",
                as_of_slot=Slot(0),
                market_id=spec.mint,
                side="buy",
                quote_amount_base_units=spec.quote_lamports,
                base_amount_base_units=None,
                max_slippage_bps=spec.max_slippage_bps,
                reason_codes=("manual_buy_sdk",),
            )

            is_live = spec.mode == ExecutionMode.LIVE
            if is_live and not self._private_key:
                return TradeResult(
                    ok=False,
                    side=TradeSide.BUY,
                    mint=spec.mint,
                    mode=spec.mode,
                    sol_amount=spec.amount_sol,
                    token_amount=0,
                    error="Live execution requires SOLANA_PRIVATE_KEY in environment",
                )

            if is_live:
                adapter = LivePumpExecutionPort(
                    endpoint=self._endpoint,
                    private_key=self._private_key,
                    routing_policy=routing_policy,
                    jito_tip_lamports=spec.jito_tip_lamports,
                    fixed_priority_fee_microlamports=spec.priority_fee_microlamports,
                )
            else:
                signer_pk = DUMMY_SIMULATION_SIGNER
                if self._private_key:
                    try:
                        raw = base58.b58decode(self._private_key.strip())
                        signer_pk = str(Pubkey.from_bytes(raw[:32]))
                    except Exception:
                        pass
                adapter = SimulationPumpExecutionPort(
                    endpoint=self._endpoint,
                    signer_pubkey=signer_pk,
                    routing_policy=routing_policy,
                    jito_tip_lamports=spec.jito_tip_lamports,
                    fixed_priority_fee_microlamports=spec.priority_fee_microlamports,
                )

            try:
                receipt: ExecutionReceipt = await adapter.submit(intent)
                if not receipt.accepted:
                    if not is_live:
                        # Fallback to canonical initial Pump.fun CPMM curve for mock/test/completed tokens in paper mode
                        tokens = int(
                            (1_073_000_000_000_000 * spec.quote_lamports)
                            / (30_000_000_000 + spec.quote_lamports)
                        )
                    else:
                        return TradeResult(
                            ok=False,
                            side=TradeSide.BUY,
                            mint=spec.mint,
                            mode=spec.mode,
                            sol_amount=spec.amount_sol,
                            token_amount=0,
                            error=receipt.message
                            or "Order rejected by execution engine",
                        )
                else:
                    tokens = receipt.simulated_output_base_units or int(
                        (1_073_000_000_000_000 * spec.quote_lamports)
                        / (30_000_000_000 + spec.quote_lamports)
                    )
                sol = spec.amount_sol
                ui_tokens = tokens / 1_000_000.0 if tokens > 0 else 0.0
                price = (sol / ui_tokens) if ui_tokens > 0 else 0.0

                fee_sol = float(receipt.estimated_fee_lamports or 0) / LAMPORTS_PER_SOL
                pos = ActivePosition(
                    mint=spec.mint,
                    entry_sol=sol,
                    entry_fees_sol=fee_sol,
                    token_amount=tokens,
                    entry_price_sol=price,
                    entry_slot=int(receipt.as_of_slot),
                    mode=spec.mode,
                    take_profit_pct=spec.take_profit_pct,
                    stop_loss_pct=spec.stop_loss_pct,
                    trailing_stop_pct=spec.trailing_stop_pct,
                    max_hold_seconds=spec.max_hold_seconds,
                    peak_price_sol=price,
                    current_pnl_pct=0.0,
                    current_value_sol=sol,
                    unrealized_pnl_sol=-fee_sol,
                )
                self._positions[spec.mint] = pos
                self._persist_position(pos)

                sig = (
                    receipt.signature
                    or f"dryrun_buy_{int(time.time_ns() // 1_000_000)}"
                )
                prefix = "Live" if is_live else "Dry-Run"

                return TradeResult(
                    ok=True,
                    side=TradeSide.BUY,
                    mint=spec.mint,
                    mode=spec.mode,
                    sol_amount=sol,
                    token_amount=tokens,
                    signature=sig,
                    effective_price_sol=price,
                    fee_sol=fee_sol,
                    slot=int(receipt.as_of_slot),
                    message=f"{prefix} buy executed: {ui_tokens:,.2f} tokens received @ {price:.10f} SOL/token",
                    take_profit_pct=spec.take_profit_pct,
                    stop_loss_pct=spec.stop_loss_pct,
                )
            except Exception as exc:
                return TradeResult(
                    ok=False,
                    side=TradeSide.BUY,
                    mint=spec.mint,
                    mode=spec.mode,
                    sol_amount=spec.amount_sol,
                    token_amount=0,
                    error=str(exc),
                )
            finally:
                await adapter.close()

    async def execute_sell(self, spec: SellOrderSpec) -> TradeResult:
        """Validate and dispatch a Sell order specification using the unified execution pipeline."""
        spec.validate()
        async with self._lock:
            pos = self._positions.get(spec.mint)
            available_tokens = pos.token_amount if pos is not None else 0

            # Determine sell tokens amount
            if spec.amount_tokens is not None:
                sell_tokens = spec.amount_tokens
            elif available_tokens > 0:
                sell_tokens = int(available_tokens * (spec.percent / 100.0))
            else:
                sell_tokens = int(1_000_000 * (spec.percent / 100.0))

            routing_policy = (
                RoutingPolicy.JITO_ONLY
                if spec.routing == "jito"
                else RoutingPolicy.RPC_ONLY
            )

            # Auto-route: detect whether token is on bonding curve or graduated to PumpSwap AMM
            venue = await self._auto_router.detect_venue(spec.mint)
            if venue == RouteVenue.PUMPSWAP_AMM:
                return await self._execute_pumpswap_sell(
                    spec=spec,
                    sell_tokens=sell_tokens,
                    routing_policy=routing_policy,
                    pos=pos,
                )
            intent = ExecutionIntent(
                intent_id=f"sell_{int(time.time_ns())}",
                as_of_slot=Slot(0),
                market_id=spec.mint,
                side="sell",
                quote_amount_base_units=None,
                base_amount_base_units=sell_tokens,
                max_slippage_bps=spec.max_slippage_bps,
                reason_codes=("manual_sell_sdk",),
            )

            is_live = spec.mode == ExecutionMode.LIVE
            if is_live and not self._private_key:
                return TradeResult(
                    ok=False,
                    side=TradeSide.SELL,
                    mint=spec.mint,
                    mode=spec.mode,
                    sol_amount=0.0,
                    token_amount=sell_tokens,
                    error="Live execution requires SOLANA_PRIVATE_KEY in environment",
                )

            if is_live:
                adapter = LivePumpExecutionPort(
                    endpoint=self._endpoint,
                    private_key=self._private_key,
                    routing_policy=routing_policy,
                    jito_tip_lamports=spec.jito_tip_lamports,
                    fixed_priority_fee_microlamports=spec.priority_fee_microlamports,
                )
            else:
                signer_pk = DUMMY_SIMULATION_SIGNER
                if self._private_key:
                    try:
                        raw = base58.b58decode(self._private_key.strip())
                        signer_pk = str(Pubkey.from_bytes(raw[:32]))
                    except Exception:
                        pass
                adapter = SimulationPumpExecutionPort(
                    endpoint=self._endpoint,
                    signer_pubkey=signer_pk,
                    routing_policy=routing_policy,
                    jito_tip_lamports=spec.jito_tip_lamports,
                    fixed_priority_fee_microlamports=spec.priority_fee_microlamports,
                )

            try:
                receipt: ExecutionReceipt = await adapter.submit(intent)
                if not receipt.accepted:
                    if not is_live:
                        # Fallback to canonical initial Pump.fun CPMM curve for mock/test/completed tokens in paper mode
                        sol = (
                            float(
                                (30_000_000_000 * sell_tokens)
                                / (1_073_000_000_000_000 + sell_tokens)
                            )
                            / LAMPORTS_PER_SOL
                        )
                    else:
                        return TradeResult(
                            ok=False,
                            side=TradeSide.SELL,
                            mint=spec.mint,
                            mode=spec.mode,
                            sol_amount=0.0,
                            token_amount=sell_tokens,
                            error=receipt.message
                            or "Sell rejected by execution engine",
                        )
                else:
                    sol = (
                        float(receipt.simulated_output_base_units or 0)
                        / LAMPORTS_PER_SOL
                    )
                tokens = sell_tokens
                ui_tokens = tokens / 1_000_000.0 if tokens > 0 else 0.0
                price = (sol / ui_tokens) if ui_tokens > 0 else 0.0
                fee_sol = float(receipt.estimated_fee_lamports or 0) / LAMPORTS_PER_SOL
                realized_pnl_sol: float | None = None
                realized_pnl_pct: float | None = None

                if pos is not None:
                    fraction_sold = (
                        tokens / pos.token_amount if pos.token_amount > 0 else 1.0
                    )
                    cost_basis_sol = pos.entry_sol * fraction_sold
                    buy_fees_sol = pos.entry_fees_sol * fraction_sold
                    total_trade_fees_sol = buy_fees_sol + fee_sol
                    realized_pnl_sol = sol - cost_basis_sol - total_trade_fees_sol
                    realized_pnl_pct = (
                        (realized_pnl_sol / cost_basis_sol * 100.0)
                        if cost_basis_sol > 0
                        else 0.0
                    )

                    remaining = max(0, pos.token_amount - tokens)
                    if remaining == 0:
                        del self._positions[spec.mint]
                        self._delete_persisted_position(spec.mint)
                    else:
                        pos.token_amount = remaining
                        pos.entry_sol -= cost_basis_sol
                        pos.entry_fees_sol -= buy_fees_sol
                        self._persist_position(pos)

                    closed_rec = {
                        "mint": spec.mint,
                        "mode": spec.mode.value,
                        "tokens_sold": tokens,
                        "sol_proceeds": sol,
                        "cost_basis_sol": cost_basis_sol,
                        "fees_sol": total_trade_fees_sol,
                        "realized_pnl_sol": realized_pnl_sol,
                        "realized_pnl_pct": realized_pnl_pct,
                        "timestamp": time.time(),
                    }
                    self._closed_trades.append(closed_rec)
                    self._persist_closed_trade(closed_rec)

                sig = (
                    receipt.signature
                    or f"dryrun_sell_{int(time.time_ns() // 1_000_000)}"
                )
                prefix = "Live" if is_live else "Dry-Run"

                pnl_msg = ""
                if realized_pnl_sol is not None and realized_pnl_pct is not None:
                    pnl_sign = "+" if realized_pnl_sol >= 0 else ""
                    pnl_msg = f" | Net PnL: {pnl_sign}{realized_pnl_sol:.4f} SOL ({pnl_sign}{realized_pnl_pct:.2f}%)"

                return TradeResult(
                    ok=True,
                    side=TradeSide.SELL,
                    mint=spec.mint,
                    mode=spec.mode,
                    sol_amount=sol,
                    token_amount=tokens,
                    signature=sig,
                    effective_price_sol=price,
                    fee_sol=fee_sol,
                    slot=int(receipt.as_of_slot),
                    realized_pnl_sol=realized_pnl_sol,
                    realized_pnl_pct=realized_pnl_pct,
                    message=f"{prefix} sell executed: {ui_tokens:,.2f} tokens sold for {sol:.4f} SOL (@ {price:.10f} SOL/token){pnl_msg}",
                )
            except Exception as exc:
                return TradeResult(
                    ok=False,
                    side=TradeSide.SELL,
                    mint=spec.mint,
                    mode=spec.mode,
                    sol_amount=0.0,
                    token_amount=sell_tokens,
                    error=str(exc),
                )
            finally:
                await adapter.close()

    async def _execute_pumpswap_buy(
        self,
        *,
        spec: BuyOrderSpec,
        routing_policy: RoutingPolicy,
    ) -> TradeResult:
        """Execute a buy order routed through the graduated PumpSwap AMM venue."""
        is_live = spec.mode == ExecutionMode.LIVE
        if is_live and not self._private_key:
            return TradeResult(
                ok=False,
                side=TradeSide.BUY,
                mint=spec.mint,
                mode=spec.mode,
                sol_amount=spec.amount_sol,
                token_amount=0,
                error="Live execution requires SOLANA_PRIVATE_KEY in environment",
            )

        try:
            pool_info = await self._auto_router.get_pumpswap_pool_info(spec.mint)
            reserves = (
                await self._auto_router.get_pool_reserves(pool_info[1])
                if pool_info is not None
                else None
            )

            if reserves is not None:
                base_reserves, quote_reserves = reserves
                expected_tokens = int(
                    (base_reserves * spec.quote_lamports)
                    / (quote_reserves + spec.quote_lamports)
                )
            else:
                expected_tokens = int(
                    (206_900_000_000_000 * spec.quote_lamports)
                    / (30_000_000_000 + spec.quote_lamports)
                )

            min_tokens_out = max(
                0, int(expected_tokens * (1.0 - spec.slippage_pct / 100.0))
            )

            if is_live:
                if pool_info is None:
                    return TradeResult(
                        ok=False,
                        side=TradeSide.BUY,
                        mint=spec.mint,
                        mode=spec.mode,
                        sol_amount=spec.amount_sol,
                        token_amount=0,
                        error=f"PumpSwap AMM pool not found for token {spec.mint}",
                    )
                raw_pk = base58.b58decode(self._private_key.strip())
                keypair = Keypair.from_bytes(raw_pk)
                user_pk = keypair.pubkey()
                pool_addr, pool_dict = pool_info

                instructions = build_pumpswap_buy_exact_quote_in_instructions(
                    user=user_pk,
                    pool_address=pool_addr,
                    pool=pool_dict,
                    spendable_quote_in=spec.quote_lamports,
                    min_base_amount_out=min_tokens_out,
                )

                tx_instructions: list[Instruction] = [
                    set_compute_unit_limit(PUMPSWAP_BUY_COMPUTE_UNITS),
                    set_compute_unit_price(spec.priority_fee_microlamports),
                ]
                jito_sender = JitoSender()
                if (
                    routing_policy == RoutingPolicy.JITO_ONLY
                    and spec.jito_tip_lamports > 0
                ):
                    tip_acc = Pubkey.from_string(jito_sender.get_random_tip_account())
                    tx_instructions.append(
                        create_jito_tip_instruction(
                            user_pk, tip_acc, spec.jito_tip_lamports
                        )
                    )
                tx_instructions.extend(instructions)

                client = SolanaClient(self._endpoint)
                try:
                    blockhash = await client.get_cached_blockhash()
                    message = Message(tx_instructions, user_pk)
                    transaction = Transaction([keypair], message, blockhash)
                    raw_tx = bytes(transaction)
                    sig = str(transaction.signatures[0])

                    router = TransactionRouter(client=client, jito_sender=jito_sender)
                    submission = await router.route(raw_tx, policy=routing_policy)
                    if not submission.acknowledged:
                        return TradeResult(
                            ok=False,
                            side=TradeSide.BUY,
                            mint=spec.mint,
                            mode=spec.mode,
                            sol_amount=spec.amount_sol,
                            token_amount=0,
                            error="PumpSwap AMM buy transaction not acknowledged by senders",
                        )
                finally:
                    await client.close()

                tokens = expected_tokens
                fee_sol = (
                    float(spec.priority_fee_sol)
                    + (
                        float(spec.jito_tip_sol)
                        if routing_policy == RoutingPolicy.JITO_ONLY
                        else 0.0
                    )
                    + 0.000005
                )
            else:
                tokens = expected_tokens
                fee_sol = (
                    float(spec.priority_fee_sol)
                    + (
                        float(spec.jito_tip_sol)
                        if routing_policy == RoutingPolicy.JITO_ONLY
                        else 0.0
                    )
                    + 0.000005
                )
                sig = f"dryrun_pumpswap_buy_{int(time.time_ns() // 1_000_000)}"

            sol = spec.amount_sol
            ui_tokens = tokens / 1_000_000.0 if tokens > 0 else 0.0
            price = (sol / ui_tokens) if ui_tokens > 0 else 0.0

            pos = ActivePosition(
                mint=spec.mint,
                entry_sol=sol,
                entry_fees_sol=fee_sol,
                token_amount=tokens,
                entry_price_sol=price,
                entry_slot=0,
                mode=spec.mode,
                take_profit_pct=spec.take_profit_pct,
                stop_loss_pct=spec.stop_loss_pct,
                trailing_stop_pct=spec.trailing_stop_pct,
                max_hold_seconds=spec.max_hold_seconds,
                peak_price_sol=price,
                current_pnl_pct=0.0,
                current_value_sol=sol,
                unrealized_pnl_sol=-fee_sol,
            )
            self._positions[spec.mint] = pos
            self._persist_position(pos)

            prefix = "Live [PumpSwap AMM]" if is_live else "Dry-Run [PumpSwap AMM]"
            return TradeResult(
                ok=True,
                side=TradeSide.BUY,
                mint=spec.mint,
                mode=spec.mode,
                sol_amount=sol,
                token_amount=tokens,
                signature=sig,
                effective_price_sol=price,
                fee_sol=fee_sol,
                slot=0,
                message=f"{prefix} buy executed: {ui_tokens:,.2f} tokens received @ {price:.10f} SOL/token",
                take_profit_pct=spec.take_profit_pct,
                stop_loss_pct=spec.stop_loss_pct,
            )
        except Exception as exc:
            return TradeResult(
                ok=False,
                side=TradeSide.BUY,
                mint=spec.mint,
                mode=spec.mode,
                sol_amount=spec.amount_sol,
                token_amount=0,
                error=str(exc),
            )

    async def _execute_pumpswap_sell(
        self,
        *,
        spec: SellOrderSpec,
        sell_tokens: int,
        routing_policy: RoutingPolicy,
        pos: ActivePosition | None,
    ) -> TradeResult:
        """Execute a sell order routed through the graduated PumpSwap AMM venue."""
        is_live = spec.mode == ExecutionMode.LIVE
        if is_live and not self._private_key:
            return TradeResult(
                ok=False,
                side=TradeSide.SELL,
                mint=spec.mint,
                mode=spec.mode,
                sol_amount=0.0,
                token_amount=sell_tokens,
                error="Live execution requires SOLANA_PRIVATE_KEY in environment",
            )

        try:
            if is_live:
                raw_pk = base58.b58decode(self._private_key.strip())
                keypair = Keypair.from_bytes(raw_pk)
                user_pk = keypair.pubkey()

                pool_info = await self._auto_router.get_pumpswap_pool_info(spec.mint)
                if pool_info is None:
                    return TradeResult(
                        ok=False,
                        side=TradeSide.SELL,
                        mint=spec.mint,
                        mode=spec.mode,
                        sol_amount=0.0,
                        token_amount=sell_tokens,
                        error=f"PumpSwap AMM pool not found for token {spec.mint}",
                    )
                pool_addr, pool_dict = pool_info

                reserves = await self._auto_router.get_pool_reserves(pool_dict)
                if reserves is not None:
                    base_reserves, quote_reserves = reserves
                    expected_lamports = int(
                        (quote_reserves * sell_tokens) / (base_reserves + sell_tokens)
                    )
                else:
                    expected_lamports = int(
                        (30_000_000_000 * sell_tokens)
                        / (206_900_000_000_000 + sell_tokens)
                    )
                min_sol_out = max(
                    0, int(expected_lamports * (1.0 - spec.slippage_pct / 100.0))
                )

                instructions = build_pumpswap_sell_instructions(
                    user=user_pk,
                    pool_address=pool_addr,
                    pool=pool_dict,
                    token_amount=sell_tokens,
                    min_sol_out=min_sol_out,
                    unwrap_sol=True,
                )

                tx_instructions: list[Instruction] = [
                    set_compute_unit_limit(PUMPSWAP_SELL_COMPUTE_UNITS),
                    set_compute_unit_price(spec.priority_fee_microlamports),
                ]
                jito_sender = JitoSender()
                if (
                    routing_policy == RoutingPolicy.JITO_ONLY
                    and spec.jito_tip_lamports > 0
                ):
                    tip_acc = Pubkey.from_string(jito_sender.get_random_tip_account())
                    tx_instructions.append(
                        create_jito_tip_instruction(
                            user_pk, tip_acc, spec.jito_tip_lamports
                        )
                    )
                tx_instructions.extend(instructions)

                client = SolanaClient(self._endpoint)
                try:
                    blockhash = await client.get_cached_blockhash()
                    message = Message(tx_instructions, user_pk)
                    transaction = Transaction([keypair], message, blockhash)
                    raw_tx = bytes(transaction)
                    sig = str(transaction.signatures[0])

                    router = TransactionRouter(client=client, jito_sender=jito_sender)
                    submission = await router.route(raw_tx, policy=routing_policy)
                    if not submission.acknowledged:
                        return TradeResult(
                            ok=False,
                            side=TradeSide.SELL,
                            mint=spec.mint,
                            mode=spec.mode,
                            sol_amount=0.0,
                            token_amount=sell_tokens,
                            error="PumpSwap AMM transaction not acknowledged by senders",
                        )
                finally:
                    await client.close()

                sol = expected_lamports / LAMPORTS_PER_SOL
                fee_sol = (
                    float(spec.priority_fee_sol)
                    + (
                        float(spec.jito_tip_sol)
                        if routing_policy == RoutingPolicy.JITO_ONLY
                        else 0.0
                    )
                    + 0.000005
                )
            else:
                pool_info = await self._auto_router.get_pumpswap_pool_info(spec.mint)
                reserves = (
                    await self._auto_router.get_pool_reserves(pool_info[1])
                    if pool_info is not None
                    else None
                )
                if reserves is not None:
                    base_reserves, quote_reserves = reserves
                    expected_lamports = int(
                        (quote_reserves * sell_tokens) / (base_reserves + sell_tokens)
                    )
                else:
                    expected_lamports = int(
                        (30_000_000_000 * sell_tokens)
                        / (206_900_000_000_000 + sell_tokens)
                    )
                sol = expected_lamports / LAMPORTS_PER_SOL
                fee_sol = (
                    float(spec.priority_fee_sol)
                    + (
                        float(spec.jito_tip_sol)
                        if routing_policy == RoutingPolicy.JITO_ONLY
                        else 0.0
                    )
                    + 0.000005
                )
                sig = f"dryrun_pumpswap_sell_{int(time.time_ns() // 1_000_000)}"

            tokens = sell_tokens
            ui_tokens = tokens / 1_000_000.0 if tokens > 0 else 0.0
            price = (sol / ui_tokens) if ui_tokens > 0 else 0.0
            realized_pnl_sol: float | None = None
            realized_pnl_pct: float | None = None

            if pos is not None:
                fraction_sold = (
                    tokens / pos.token_amount if pos.token_amount > 0 else 1.0
                )
                cost_basis_sol = pos.entry_sol * fraction_sold
                buy_fees_sol = pos.entry_fees_sol * fraction_sold
                total_trade_fees_sol = buy_fees_sol + fee_sol
                realized_pnl_sol = sol - cost_basis_sol - total_trade_fees_sol
                realized_pnl_pct = (
                    (realized_pnl_sol / cost_basis_sol * 100.0)
                    if cost_basis_sol > 0
                    else 0.0
                )

                remaining = max(0, pos.token_amount - tokens)
                if remaining == 0:
                    del self._positions[spec.mint]
                    self._delete_persisted_position(spec.mint)
                else:
                    pos.token_amount = remaining
                    pos.entry_sol -= cost_basis_sol
                    pos.entry_fees_sol -= buy_fees_sol
                    self._persist_position(pos)

                closed_rec = {
                    "mint": spec.mint,
                    "mode": spec.mode.value,
                    "tokens_sold": tokens,
                    "sol_proceeds": sol,
                    "cost_basis_sol": cost_basis_sol,
                    "fees_sol": total_trade_fees_sol,
                    "realized_pnl_sol": realized_pnl_sol,
                    "realized_pnl_pct": realized_pnl_pct,
                    "timestamp": time.time(),
                }
                self._closed_trades.append(closed_rec)
                self._persist_closed_trade(closed_rec)

            prefix = "Live [PumpSwap AMM]" if is_live else "Dry-Run [PumpSwap AMM]"
            pnl_msg = ""
            if realized_pnl_sol is not None and realized_pnl_pct is not None:
                pnl_sign = "+" if realized_pnl_sol >= 0 else ""
                pnl_msg = f" | Net PnL: {pnl_sign}{realized_pnl_sol:.4f} SOL ({pnl_sign}{realized_pnl_pct:.2f}%)"

            return TradeResult(
                ok=True,
                side=TradeSide.SELL,
                mint=spec.mint,
                mode=spec.mode,
                sol_amount=sol,
                token_amount=tokens,
                signature=sig,
                effective_price_sol=price,
                fee_sol=fee_sol,
                slot=0,
                realized_pnl_sol=realized_pnl_sol,
                realized_pnl_pct=realized_pnl_pct,
                message=f"{prefix} sell executed: {ui_tokens:,.2f} tokens sold for {sol:.4f} SOL (@ {price:.10f} SOL/token){pnl_msg}",
            )
        except Exception as exc:
            return TradeResult(
                ok=False,
                side=TradeSide.SELL,
                mint=spec.mint,
                mode=spec.mode,
                sol_amount=0.0,
                token_amount=sell_tokens,
                error=str(exc),
            )

    async def tick(self) -> list[TradeResult]:
        """Evaluate current prices for all active positions and auto-trigger TP/SL exits."""
        triggered_trades: list[TradeResult] = []
        if not self._positions:
            return triggered_trades

        # Evaluate positions without holding the long lock during RPC
        positions_to_check = list(self._positions.values())

        for pos in positions_to_check:
            try:
                venue = await self._auto_router.detect_venue(pos.mint)
                if venue == RouteVenue.PUMPSWAP_AMM:
                    pool_info = await self._auto_router.get_pumpswap_pool_info(pos.mint)
                    reserves = (
                        await self._auto_router.get_pool_reserves(pool_info[1])
                        if pool_info is not None
                        else None
                    )
                    if reserves is not None:
                        base_reserves, quote_reserves = reserves
                        expected_lamports = int(
                            (quote_reserves * pos.token_amount)
                            / (base_reserves + pos.token_amount)
                        )
                    else:
                        expected_lamports = int(
                            (30_000_000_000 * pos.token_amount)
                            / (206_900_000_000_000 + pos.token_amount)
                        )
                    current_sol = float(expected_lamports) / LAMPORTS_PER_SOL
                else:
                    sim_intent = ExecutionIntent(
                        intent_id=f"tick_{int(time.time_ns())}",
                        as_of_slot=Slot(0),
                        market_id=pos.mint,
                        side="sell",
                        quote_amount_base_units=None,
                        base_amount_base_units=pos.token_amount,
                        max_slippage_bps=1000,
                        reason_codes=("tick_eval",),
                    )
                    port = SimulationPumpExecutionPort(
                        endpoint=self._endpoint,
                        signer_pubkey=DUMMY_SIMULATION_SIGNER,
                    )
                    try:
                        receipt = await port.submit(sim_intent)
                        if (
                            not receipt.accepted
                            or not receipt.simulated_output_base_units
                        ):
                            continue
                        current_sol = (
                            receipt.simulated_output_base_units / LAMPORTS_PER_SOL
                        )
                    finally:
                        await port.close()

                current_price = (
                    current_sol / (pos.token_amount / 1_000_000.0)
                    if pos.token_amount > 0
                    else 0.0
                )
                pos.current_value_sol = current_sol
                pos.unrealized_pnl_sol = (
                    current_sol - pos.entry_sol - pos.entry_fees_sol
                )
                pos.current_pnl_pct = (
                    ((current_sol - pos.entry_sol) / pos.entry_sol * 100.0)
                    if pos.entry_sol > 0
                    else 0.0
                )
                pos.peak_price_sol = max(pos.peak_price_sol, current_price)
                self._persist_position(pos)

                # Check Take-Profit Trigger
                if pos.take_profit_pct and pos.current_pnl_pct >= pos.take_profit_pct:
                    logger.info(
                        "TAKE-PROFIT triggered for %s at +%.2f%% (Target: +%.2f%%)",
                        pos.mint,
                        pos.current_pnl_pct,
                        pos.take_profit_pct,
                    )
                    sell_res = await self.sell(pos.mint, percent=100.0, mode=pos.mode)
                    triggered_trades.append(sell_res)
                    continue

                # Check Stop-Loss Trigger
                if pos.stop_loss_pct and pos.current_pnl_pct <= -abs(pos.stop_loss_pct):
                    logger.info(
                        "STOP-LOSS triggered for %s at %.2f%% (Target: -%.2f%%)",
                        pos.mint,
                        pos.current_pnl_pct,
                        pos.stop_loss_pct,
                    )
                    sell_res = await self.sell(pos.mint, percent=100.0, mode=pos.mode)
                    triggered_trades.append(sell_res)
                    continue

                # Check Trailing Stop Trigger
                if pos.trailing_stop_pct and pos.peak_price_sol > 0:
                    drop_from_peak = (
                        (pos.peak_price_sol - current_price)
                        / pos.peak_price_sol
                        * 100.0
                    )
                    if drop_from_peak >= pos.trailing_stop_pct:
                        logger.info(
                            "TRAILING STOP triggered for %s (Dropped %.2f%% from peak)",
                            pos.mint,
                            drop_from_peak,
                        )
                        sell_res = await self.sell(
                            pos.mint, percent=100.0, mode=pos.mode
                        )
                        triggered_trades.append(sell_res)
                        continue

                # Check Max-Hold Timeout Trigger
                elapsed_s = time.time() - pos.opened_at_ts
                if pos.max_hold_seconds and elapsed_s >= pos.max_hold_seconds:
                    logger.info(
                        "MAX-HOLD TIMEOUT reached for %s (%.1fs elapsed >= %.1fs limit). Triggering automated exit.",
                        pos.mint,
                        elapsed_s,
                        pos.max_hold_seconds,
                    )
                    sell_res = await self.sell(pos.mint, percent=100.0, mode=pos.mode)
                    triggered_trades.append(sell_res)
                    continue
            except Exception as exc:
                logger.debug("Failed to tick position %s: %s", pos.mint, exc)

        return triggered_trades

    async def close(self) -> None:
        """Close resources owned by the TradingService."""
        await self._auto_router.close()


__all__ = [
    "DEFAULT_BUY_SLIPPAGE_PCT",
    "DEFAULT_JITO_TIP_SOL",
    "DEFAULT_PRIORITY_FEE_SOL",
    "DEFAULT_SELL_SLIPPAGE_PCT",
    "ActivePosition",
    "BuyOrderSpec",
    "SellOrderSpec",
    "TradeResult",
    "TradeSide",
    "TradingService",
]
