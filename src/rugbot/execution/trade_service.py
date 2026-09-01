"""Unified, developer-friendly Trading SDK and order execution service for Pump.fun."""

# ruff: noqa: PLR0913, PLR0912, TRY003, BLE001

from __future__ import annotations

import asyncio
import os
import time
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Literal

import base58
from solders.pubkey import Pubkey

from rugbot.domain.amounts import Slot
from rugbot.execution.live import LivePumpExecutionPort
from rugbot.execution.ports import (
    ExecutionIntent,
    ExecutionMode,
    ExecutionReceipt,
)
from rugbot.execution.sender import RoutingPolicy
from rugbot.runtime.config import (
    PUBKEY_LENGTH,
    load_provider_settings,
    resolve_dotenv,
)
from rugbot.simulation.route_simulation import SimulationPumpExecutionPort
from rugbot.utils.logger import get_logger

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
        return int(round(self.amount_sol * LAMPORTS_PER_SOL))

    @property
    def max_slippage_bps(self) -> int:
        """Convert slippage percentage to basis points."""
        return int(round(self.slippage_pct * 100))

    @property
    def priority_fee_microlamports(self) -> int:
        """Convert priority fee SOL to microlamports."""
        return int(round(self.priority_fee_sol * MICROLAMPORTS_PER_SOL))

    @property
    def jito_tip_lamports(self) -> int:
        """Convert Jito tip SOL to lamports."""
        return int(round(self.jito_tip_sol * LAMPORTS_PER_SOL))

    @property
    def take_profit_pnl_ppm(self) -> int | None:
        """Convert TP percentage to PPM."""
        return (
            int(round((self.take_profit_pct / 100.0) * PPM_DENOMINATOR))
            if self.take_profit_pct is not None
            else None
        )

    @property
    def stop_loss_pnl_ppm(self) -> int | None:
        """Convert SL percentage to negative PPM."""
        return (
            -int(round((self.stop_loss_pct / 100.0) * PPM_DENOMINATOR))
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
        return int(round(self.slippage_pct * 100))

    @property
    def priority_fee_microlamports(self) -> int:
        """Convert priority fee SOL to microlamports."""
        return int(round(self.priority_fee_sol * MICROLAMPORTS_PER_SOL))

    @property
    def jito_tip_lamports(self) -> int:
        """Convert Jito tip SOL to lamports."""
        return int(round(self.jito_tip_sol * LAMPORTS_PER_SOL))


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


import sqlite3
from pathlib import Path


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
        self._positions: dict[str, ActivePosition] = {}
        self._closed_trades: list[dict[str, Any]] = []
        self._lock = asyncio.Lock()
        self._init_db()
        self._load_from_db()

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

    async def tick(self) -> list[TradeResult]:
        """Evaluate current prices for all active positions and auto-trigger TP/SL exits."""
        triggered_trades: list[TradeResult] = []
        if not self._positions:
            return triggered_trades

        # Evaluate positions without holding the long lock during RPC
        positions_to_check = list(self._positions.values())

        for pos in positions_to_check:
            try:
                # Estimate current sell value via simulation port
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
                    if receipt.accepted and receipt.simulated_output_base_units:
                        current_sol = (
                            receipt.simulated_output_base_units / LAMPORTS_PER_SOL
                        )
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
                        if (
                            pos.take_profit_pct
                            and pos.current_pnl_pct >= pos.take_profit_pct
                        ):
                            logger.info(
                                "TAKE-PROFIT triggered for %s at +%.2f%% (Target: +%.2f%%)",
                                pos.mint,
                                pos.current_pnl_pct,
                                pos.take_profit_pct,
                            )
                            sell_res = await self.sell(
                                pos.mint, percent=100.0, mode=pos.mode
                            )
                            triggered_trades.append(sell_res)
                            continue

                        # Check Stop-Loss Trigger
                        if pos.stop_loss_pct and pos.current_pnl_pct <= -abs(
                            pos.stop_loss_pct
                        ):
                            logger.info(
                                "STOP-LOSS triggered for %s at %.2f%% (Target: -%.2f%%)",
                                pos.mint,
                                pos.current_pnl_pct,
                                pos.stop_loss_pct,
                            )
                            sell_res = await self.sell(
                                pos.mint, percent=100.0, mode=pos.mode
                            )
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
                            sell_res = await self.sell(
                                pos.mint, percent=100.0, mode=pos.mode
                            )
                            triggered_trades.append(sell_res)
                            continue
                finally:
                    await port.close()
            except Exception as exc:
                logger.debug("Failed to tick position %s: %s", pos.mint, exc)

        return triggered_trades


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
