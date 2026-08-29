"""Unified, developer-friendly Trading SDK and order execution service for Pump.fun."""

# ruff: noqa: S105, PLR0913, PLR0912, PLR0911, TRY003, BLE001

from __future__ import annotations

import asyncio
import os
import time
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Literal

import base58

from rugbot.domain.amounts import Lamports, Slot
from rugbot.execution.live import LivePumpExecutionPort
from rugbot.execution.ports import (
    MAX_SLIPPAGE_BPS,
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
from rugbot.utils.logger import get_logger

logger = get_logger(__name__)

LAMPORTS_PER_SOL = 1_000_000_000
MICROLAMPORTS_PER_SOL = 1_000_000_000_000
PPM_DENOMINATOR = 1_000_000
DEFAULT_BUY_SLIPPAGE_PCT = 5.0
DEFAULT_SELL_SLIPPAGE_PCT = 10.0
DEFAULT_JITO_TIP_SOL = 0.001
DEFAULT_PRIORITY_FEE_SOL = 0.0005


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
    mode: ExecutionMode = ExecutionMode.PAPER
    take_profit_pct: float | None = None
    stop_loss_pct: float | None = None
    trailing_stop_pct: float | None = None
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
    mode: ExecutionMode = ExecutionMode.PAPER

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
    take_profit_pct: float | None = None
    stop_loss_pct: float | None = None
    trailing_stop_pct: float | None = None
    peak_price_sol: float = 0.0
    current_pnl_pct: float = 0.0
    opened_at_ts: float = field(default_factory=time.time)


class TradingService:
    """Unified trading client for executing and managing Pump.fun trades."""

    def __init__(
        self,
        *,
        endpoint: str | None = None,
        private_key: str | None = None,
        default_mode: ExecutionMode = ExecutionMode.PAPER,
        default_routing: RoutingPolicy = RoutingPolicy.RPC_ONLY,
    ) -> None:
        providers = load_provider_settings()
        self._endpoint = endpoint or providers.rpc_http or ""
        self._private_key = private_key or os.environ.get("SOLANA_PRIVATE_KEY")
        self._default_mode = default_mode
        self._default_routing = default_routing
        self._positions: dict[str, ActivePosition] = {}
        self._lock = asyncio.Lock()

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
                "token_amount": pos.token_amount,
                "entry_price_sol": pos.entry_price_sol,
                "entry_slot": pos.entry_slot,
                "mode": pos.mode.value,
                "take_profit_pct": pos.take_profit_pct,
                "stop_loss_pct": pos.stop_loss_pct,
                "trailing_stop_pct": pos.trailing_stop_pct,
                "current_pnl_pct": pos.current_pnl_pct,
                "opened_at_ts": pos.opened_at_ts,
            }
            for pos in self._positions.values()
        ]

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
        """Validate and dispatch a Buy order specification."""
        spec.validate()
        async with self._lock:
            # Paper execution path
            if spec.mode in (
                ExecutionMode.PAPER,
                ExecutionMode.SIMULATION,
                ExecutionMode.OBSERVE,
            ):
                estimated_tokens = int(spec.amount_sol * 30_000_000)
                price_sol = spec.amount_sol / (estimated_tokens or 1)

                pos = ActivePosition(
                    mint=spec.mint,
                    entry_sol=spec.amount_sol,
                    token_amount=estimated_tokens,
                    entry_price_sol=price_sol,
                    entry_slot=0,
                    mode=spec.mode,
                    take_profit_pct=spec.take_profit_pct,
                    stop_loss_pct=spec.stop_loss_pct,
                    trailing_stop_pct=spec.trailing_stop_pct,
                    peak_price_sol=price_sol,
                )
                self._positions[spec.mint] = pos

                return TradeResult(
                    ok=True,
                    side=TradeSide.BUY,
                    mint=spec.mint,
                    mode=spec.mode,
                    sol_amount=spec.amount_sol,
                    token_amount=estimated_tokens,
                    signature=f"paper_buy_{int(time.time() * 1000)}",
                    effective_price_sol=price_sol,
                    fee_sol=spec.priority_fee_sol,
                    message=f"Paper buy {spec.amount_sol:.4f} SOL filled ({estimated_tokens:,} tokens)",
                    take_profit_pct=spec.take_profit_pct,
                    stop_loss_pct=spec.stop_loss_pct,
                )

            # Live execution path
            if not self._private_key:
                return TradeResult(
                    ok=False,
                    side=TradeSide.BUY,
                    mint=spec.mint,
                    mode=spec.mode,
                    sol_amount=spec.amount_sol,
                    token_amount=0,
                    error="Live execution requires SOLANA_PRIVATE_KEY",
                )

            routing_policy = (
                RoutingPolicy.JITO_ONLY
                if spec.routing == "jito"
                else RoutingPolicy.RPC_ONLY
            )
            adapter = LivePumpExecutionPort(
                endpoint=self._endpoint,
                private_key=self._private_key,
                routing_policy=routing_policy,
                jito_tip_lamports=spec.jito_tip_lamports,
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

            try:
                receipt: ExecutionReceipt = await adapter.submit(intent)
                if not receipt.accepted:
                    return TradeResult(
                        ok=False,
                        side=TradeSide.BUY,
                        mint=spec.mint,
                        mode=spec.mode,
                        sol_amount=spec.amount_sol,
                        token_amount=0,
                        error=receipt.message or "Order rejected",
                    )

                tokens = receipt.simulated_output_base_units or int(
                    spec.amount_sol * 30_000_000
                )
                sol = spec.amount_sol
                price = (sol / tokens) if tokens > 0 else 0.0

                pos = ActivePosition(
                    mint=spec.mint,
                    entry_sol=sol,
                    token_amount=tokens,
                    entry_price_sol=price,
                    entry_slot=int(receipt.as_of_slot),
                    mode=spec.mode,
                    take_profit_pct=spec.take_profit_pct,
                    stop_loss_pct=spec.stop_loss_pct,
                    trailing_stop_pct=spec.trailing_stop_pct,
                    peak_price_sol=price,
                )
                self._positions[spec.mint] = pos

                return TradeResult(
                    ok=True,
                    side=TradeSide.BUY,
                    mint=spec.mint,
                    mode=spec.mode,
                    sol_amount=sol,
                    token_amount=tokens,
                    signature=receipt.signature,
                    effective_price_sol=price,
                    fee_sol=float(receipt.estimated_fee_lamports or 0)
                    / LAMPORTS_PER_SOL,
                    slot=int(receipt.as_of_slot),
                    message="Live buy executed successfully",
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
        """Validate and dispatch a Sell order specification."""
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

            if spec.mode in (
                ExecutionMode.PAPER,
                ExecutionMode.SIMULATION,
                ExecutionMode.OBSERVE,
            ):
                price_sol = (
                    (pos.entry_price_sol * 1.2) if pos is not None else 0.00000003
                )
                proceeds_sol = sell_tokens * price_sol

                if pos is not None:
                    remaining_tokens = max(0, pos.token_amount - sell_tokens)
                    if remaining_tokens == 0:
                        del self._positions[spec.mint]
                    else:
                        pos.token_amount = remaining_tokens

                return TradeResult(
                    ok=True,
                    side=TradeSide.SELL,
                    mint=spec.mint,
                    mode=spec.mode,
                    sol_amount=proceeds_sol,
                    token_amount=sell_tokens,
                    signature=f"paper_sell_{int(time.time() * 1000)}",
                    effective_price_sol=price_sol,
                    fee_sol=spec.priority_fee_sol,
                    message=f"Paper sell {sell_tokens:,} tokens filled (~{proceeds_sol:.4f} SOL)",
                )

            # Live execution path
            if not self._private_key:
                return TradeResult(
                    ok=False,
                    side=TradeSide.SELL,
                    mint=spec.mint,
                    mode=spec.mode,
                    sol_amount=0.0,
                    token_amount=sell_tokens,
                    error="Live execution requires SOLANA_PRIVATE_KEY",
                )

            routing_policy = (
                RoutingPolicy.JITO_ONLY
                if spec.routing == "jito"
                else RoutingPolicy.RPC_ONLY
            )
            adapter = LivePumpExecutionPort(
                endpoint=self._endpoint,
                private_key=self._private_key,
                routing_policy=routing_policy,
                jito_tip_lamports=spec.jito_tip_lamports,
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

            try:
                receipt: ExecutionReceipt = await adapter.submit(intent)
                if not receipt.accepted:
                    return TradeResult(
                        ok=False,
                        side=TradeSide.SELL,
                        mint=spec.mint,
                        mode=spec.mode,
                        sol_amount=0.0,
                        token_amount=sell_tokens,
                        error=receipt.message or "Sell rejected",
                    )

                sol = float(receipt.simulated_output_base_units or 0) / LAMPORTS_PER_SOL
                tokens = sell_tokens
                price = (sol / tokens) if tokens > 0 else 0.0

                if pos is not None:
                    remaining = max(0, pos.token_amount - tokens)
                    if remaining == 0:
                        del self._positions[spec.mint]
                    else:
                        pos.token_amount = remaining

                return TradeResult(
                    ok=True,
                    side=TradeSide.SELL,
                    mint=spec.mint,
                    mode=spec.mode,
                    sol_amount=sol,
                    token_amount=tokens,
                    signature=receipt.signature,
                    effective_price_sol=price,
                    fee_sol=float(receipt.estimated_fee_lamports or 0)
                    / LAMPORTS_PER_SOL,
                    slot=int(receipt.as_of_slot),
                    message="Live sell executed successfully",
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


__all__ = [
    "ActivePosition",
    "BuyOrderSpec",
    "SellOrderSpec",
    "TradeResult",
    "TradeSide",
    "TradingService",
]
