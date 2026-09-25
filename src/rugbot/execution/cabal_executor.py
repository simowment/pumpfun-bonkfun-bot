"""Execution and exit manager for insider cabal copytrading.

Coordinates:
1. Stealth wallet acquisition from `WalletPool`.
2. Jito validator tip floor estimation via `JitoClient`.
3. High-fidelity paper execution via `PaperExecutionAdapter`.
4. Pre-defined tiered Take-Profit ladders (e.g., 50% at 2x, 25% at 5x).
5. Trailing stop protection (15% drop from peak).
6. Exit modeling: adverse prints on rug dump, no fixed stops on rugs.
"""

# ruff: noqa: TRY003, PLR2004, PLR0913, ANN401

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Final

from rugbot.adapters.simulation.paper_execution import PaperExecutionAdapter
from rugbot.core.decision.take_profit import TakeProfitLadder
from rugbot.core.decision.trailing_stop import TrailingStopState
from rugbot.core.models.address import Address
from rugbot.core.models.order import (
    ExecutionMode,
    OrderIntent,
    OrderSide,
    TradeReceipt,
)
from rugbot.core.models.token import TokenAmount
from rugbot.execution.wallet_pool import WalletPool
from rugbot.integrations.jito import JitoClient
from rugbot.utils.logger import get_logger

if TYPE_CHECKING:
    from rugbot.core.ports.execution_port import ExecutionPort

logger = get_logger(__name__)

SOLANA_CHAIN_ID: Final[str] = "solana:101"
DEFAULT_SLIPPAGE_PCT: Final[float] = 1.5
DEFAULT_TP_LEVELS: Final[tuple[tuple[float, float], ...]] = (
    (100.0, 0.50),  # +100% (2x): sell 50%
    (400.0, 0.25),  # +400% (5x): sell 25%
)
DEFAULT_TRAILING_STOP_PCT: Final[float] = 15.0


@dataclass
class CabalActivePosition:
    """Active copytrade position with tiered exits and trailing stop."""

    position_id: str
    mint: str
    wallet_address: str
    cabal_cluster_id: str
    entry_price_sol: float
    entry_sol_amount: float
    token_amount: float
    remaining_tokens: float
    tp_ladder: TakeProfitLadder
    trailing_stop: TrailingStopState
    high_price_seen: float
    cost_basis_remaining_sol: float = 0.0
    realized_pnl_sol: float = 0.0
    is_closed: bool = False
    opened_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    closed_at: str | None = None
    exit_reason: str | None = None

    @property
    def current_roi_pct(self) -> float:
        """Calculate ROI percentage from current high water mark."""
        if self.entry_price_sol <= 0:
            return 0.0
        return (
            (self.high_price_seen - self.entry_price_sol) / self.entry_price_sol
        ) * 100.0


class CabalExecutor:
    """Manages entry and exit lifecycle for cabal copytrades."""

    def __init__(
        self,
        wallet_pool: WalletPool | None = None,
        execution_adapter: ExecutionPort | None = None,
        jito_client: JitoClient | None = None,
        *,
        store: Any | None = None,
        trailing_stop_pct: float = DEFAULT_TRAILING_STOP_PCT,
        tp_levels: tuple[tuple[float, float], ...] = DEFAULT_TP_LEVELS,
        paper_balance_sol: float = 2.0,
        live_mode: bool = False,
        trade_service: Any | None = None,
    ) -> None:
        self.wallet_pool = wallet_pool or WalletPool.from_env()
        self.execution_adapter = execution_adapter or PaperExecutionAdapter()
        self.jito_client = jito_client or JitoClient()
        self.store = store
        self.trailing_stop_pct = trailing_stop_pct
        self.tp_levels = tp_levels
        self.initial_cash_balance_sol = paper_balance_sol
        self.cash_balance_sol = paper_balance_sol
        self.live_mode = live_mode
        self.trade_service = trade_service
        self._positions: dict[str, CabalActivePosition] = {}

        if self.live_mode:
            # Strictly validate fail-closed safety check per AGENTS.md §7
            has_signer = (
                self.trade_service is not None
                and getattr(self.trade_service, "_private_key", None)
            ) or getattr(self.execution_adapter, "can_execute_live", False)
            if not has_signer:
                raise RuntimeError(
                    "Live execution requested, but no valid TradeService or signer keypair configured. "
                    "In adherence to AGENTS.md §7, live execution fails closed."
                )

    async def _dispatch_order(self, intent: OrderIntent) -> TradeReceipt:
        """Dispatch order via TradeService if live_mode and configured, else via execution_adapter."""
        if self.live_mode and self.trade_service is not None:
            from rugbot.execution.ports import (  # noqa: PLC0415
                ExecutionMode as SvcExecutionMode,
            )

            if intent.side == OrderSide.BUY:
                svc_res = await self.trade_service.buy(
                    mint=intent.target_token.raw,
                    amount_sol=intent.amount_in.ui_value,
                    slippage_pct=intent.max_slippage_bps / 100.0,
                    jito_tip_sol=intent.tip_native,
                    mode=SvcExecutionMode.LIVE,
                )
                return TradeReceipt(
                    ok=svc_res.ok,
                    intent_id=intent.intent_id,
                    target_token=intent.target_token,
                    side=OrderSide.BUY,
                    tx_hash=svc_res.signature,
                    filled_amount_in=intent.amount_in,
                    filled_amount_out=TokenAmount(
                        raw_units=svc_res.token_amount, decimals=6
                    ),
                    effective_price=svc_res.effective_price_sol,
                    fee_paid_native=svc_res.fee_sol,
                    error_message=svc_res.error,
                )

            # SELL
            svc_res = await self.trade_service.sell(
                mint=intent.target_token.raw,
                amount_tokens=intent.amount_in.raw_units,
                slippage_pct=intent.max_slippage_bps / 100.0,
                mode=SvcExecutionMode.LIVE,
            )
            return TradeReceipt(
                ok=svc_res.ok,
                intent_id=intent.intent_id,
                target_token=intent.target_token,
                side=OrderSide.SELL,
                tx_hash=svc_res.signature,
                filled_amount_in=intent.amount_in,
                filled_amount_out=TokenAmount(
                    raw_units=round(svc_res.sol_amount * 1e9), decimals=9
                ),
                effective_price=svc_res.effective_price_sol,
                fee_paid_native=svc_res.fee_sol,
                error_message=svc_res.error,
            )

        return await self.execution_adapter.execute(intent)

    @property
    def active_positions(self) -> list[CabalActivePosition]:
        """Return list of unclosed positions."""
        return [p for p in self._positions.values() if not p.is_closed]

    def get_position(self, position_id: str) -> CabalActivePosition | None:
        """Find a position by ID."""
        return self._positions.get(position_id)

    async def enter_position(
        self,
        mint: str,
        cabal_cluster_id: str,
        amount_sol: float,
        initial_price_sol: float,
    ) -> tuple[CabalActivePosition, TradeReceipt]:
        """Execute paper buy order using a stealth wallet from pool."""
        # 1. Acquire execution wallet
        wallet = self.wallet_pool.acquire_wallet()

        # 2. Get Jito tip floor for bundling priority
        tip_floor = self.jito_client.fetch_tip_floor()
        tip_sol = tip_floor.p75 if tip_floor else 0.002
        logger.info(
            "Acquired execution wallet %s for mint %s (Jito p75 tip: %.4f SOL)",
            wallet.address,
            mint,
            tip_sol,
        )

        # 3. Simulate or execute buy order
        exec_mode = ExecutionMode.LIVE if self.live_mode else ExecutionMode.PAPER
        target_token = Address(raw=mint, chain_id=SOLANA_CHAIN_ID)
        native_amount = TokenAmount(
            raw_units=int(amount_sol * 1_000_000_000),
            decimals=9,
        )
        intent = OrderIntent(
            intent_id=f"intent-{uuid.uuid4().hex[:8]}",
            target_token=target_token,
            side=OrderSide.BUY,
            amount_in=native_amount,
            max_slippage_bps=int(DEFAULT_SLIPPAGE_PCT * 100),
            mode=exec_mode,
            tip_native=tip_sol,
        )
        receipt = await self._dispatch_order(intent)

        if initial_price_sol > 0:
            tokens_received = (amount_sol / initial_price_sol) * (
                1.0 - DEFAULT_SLIPPAGE_PCT / 100.0
            )
        elif receipt.filled_amount_out and receipt.filled_amount_out.ui_value > 0:
            tokens_received = receipt.filled_amount_out.ui_value
        else:
            quote = await self.execution_adapter.get_quote(
                target_token=target_token,
                side=OrderSide.BUY,
                amount_in=native_amount,
            )
            tokens_received = quote.expected_amount_out.ui_value
        position_id = f"pos-{uuid.uuid4().hex[:8]}"

        position = CabalActivePosition(
            position_id=position_id,
            mint=mint,
            wallet_address=wallet.address,
            cabal_cluster_id=cabal_cluster_id,
            entry_price_sol=initial_price_sol,
            entry_sol_amount=amount_sol,
            cost_basis_remaining_sol=amount_sol,
            token_amount=tokens_received,
            remaining_tokens=tokens_received,
            tp_ladder=TakeProfitLadder.from_tuples(self.tp_levels),
            trailing_stop=TrailingStopState(
                high_water_mark=initial_price_sol,
                trailing_pct=self.trailing_stop_pct,
            ),
            high_price_seen=initial_price_sol,
        )
        self._positions[position_id] = position
        self.cash_balance_sol -= amount_sol
        if self.store is not None:
            self.store.record_execution(position)
        return position, receipt

    async def update_price_tick(
        self,
        position_id: str,
        current_price_sol: float,
    ) -> tuple[CabalActivePosition, TradeReceipt | None]:
        """Update position on new price tick, evaluating TP ladder and trailing stop."""
        pos = self._positions.get(position_id)
        if not pos or pos.is_closed:
            raise ValueError(f"Position {position_id} is invalid or already closed")

        receipt: TradeReceipt | None = None
        pos.high_price_seen = max(pos.high_price_seen, current_price_sol)

        # 1. Evaluate Take-Profit ladder
        updated_ladder, sell_fraction = pos.tp_ladder.evaluate(
            pos.entry_price_sol, current_price_sol
        )
        pos.tp_ladder = updated_ladder

        exec_mode = ExecutionMode.LIVE if self.live_mode else ExecutionMode.PAPER
        if sell_fraction > 0.0 and pos.remaining_tokens > 0:
            tokens_to_sell = pos.remaining_tokens * sell_fraction
            logger.info(
                "Take-Profit triggered for %s: selling %.1f%% (%.2f tokens) at %.6f SOL",
                pos.position_id,
                sell_fraction * 100.0,
                tokens_to_sell,
                current_price_sol,
            )
            target_token = Address(raw=pos.mint, chain_id=SOLANA_CHAIN_ID)
            sell_amount = TokenAmount(
                raw_units=int(tokens_to_sell * 1_000_000),
                decimals=6,
            )
            intent = OrderIntent(
                intent_id=f"intent-{uuid.uuid4().hex[:8]}",
                target_token=target_token,
                side=OrderSide.SELL,
                amount_in=sell_amount,
                max_slippage_bps=int(DEFAULT_SLIPPAGE_PCT * 100),
                mode=exec_mode,
            )
            receipt = await self._dispatch_order(intent)

            proceeds_sol = tokens_to_sell * current_price_sol
            cost_basis = pos.entry_sol_amount * (tokens_to_sell / pos.token_amount)
            pos.realized_pnl_sol += proceeds_sol - cost_basis
            pos.cost_basis_remaining_sol = max(
                0.0, pos.cost_basis_remaining_sol - cost_basis
            )
            pos.remaining_tokens -= tokens_to_sell
            self.cash_balance_sol += proceeds_sol

            if pos.remaining_tokens <= 1e-6:
                pos.is_closed = True
                pos.closed_at = datetime.now(UTC).isoformat()
                pos.exit_reason = "tp_completed"
                self.wallet_pool.release_wallet(pos.wallet_address)
                return pos, receipt

        # 2. Evaluate Trailing Stop
        updated_stop, should_exit = pos.trailing_stop.update(current_price_sol)
        pos.trailing_stop = updated_stop

        if should_exit and pos.remaining_tokens > 0:
            logger.info(
                "Trailing stop triggered for %s: dropping %.1f%% from peak %.6f SOL",
                pos.position_id,
                self.trailing_stop_pct,
                pos.high_price_seen,
            )
            target_token = Address(raw=pos.mint, chain_id=SOLANA_CHAIN_ID)
            stop_sell_amount = TokenAmount(
                raw_units=int(pos.remaining_tokens * 1_000_000),
                decimals=6,
            )
            intent = OrderIntent(
                intent_id=f"intent-{uuid.uuid4().hex[:8]}",
                target_token=target_token,
                side=OrderSide.SELL,
                amount_in=stop_sell_amount,
                max_slippage_bps=int(DEFAULT_SLIPPAGE_PCT * 100),
                mode=exec_mode,
            )
            receipt = await self._dispatch_order(intent)

            proceeds_sol = pos.remaining_tokens * current_price_sol
            cost_basis = pos.cost_basis_remaining_sol
            pos.realized_pnl_sol += proceeds_sol - cost_basis
            pos.cost_basis_remaining_sol = 0.0
            pos.remaining_tokens = 0.0
            pos.is_closed = True
            pos.closed_at = datetime.now(UTC).isoformat()
            pos.exit_reason = "trailing_stop"
            self.cash_balance_sol += proceeds_sol
            self.wallet_pool.release_wallet(pos.wallet_address)

        if self.store is not None:
            self.store.record_execution(pos)

        return pos, receipt

    async def exit_position(
        self,
        position_id: str,
        current_price_sol: float,
        reason: str = "manual_exit",
    ) -> tuple[CabalActivePosition, TradeReceipt]:
        """Emergency or adverse event exit: liquidate remaining position."""
        pos = self._positions.get(position_id)
        if not pos or pos.is_closed:
            raise ValueError(f"Position {position_id} is invalid or already closed")

        exec_mode = ExecutionMode.LIVE if self.live_mode else ExecutionMode.PAPER
        target_token = Address(raw=pos.mint, chain_id=SOLANA_CHAIN_ID)
        exit_amount = TokenAmount(
            raw_units=int(pos.remaining_tokens * 1_000_000),
            decimals=6,
        )
        intent = OrderIntent(
            intent_id=f"intent-{uuid.uuid4().hex[:8]}",
            target_token=target_token,
            side=OrderSide.SELL,
            amount_in=exit_amount,
            max_slippage_bps=int(DEFAULT_SLIPPAGE_PCT * 100),
            mode=exec_mode,
        )
        receipt = await self._dispatch_order(intent)

        proceeds_sol = pos.remaining_tokens * current_price_sol
        cost_basis = pos.cost_basis_remaining_sol
        pos.realized_pnl_sol += proceeds_sol - cost_basis
        pos.cost_basis_remaining_sol = 0.0
        pos.remaining_tokens = 0.0
        pos.is_closed = True
        pos.closed_at = datetime.now(UTC).isoformat()
        pos.exit_reason = reason
        self.cash_balance_sol += proceeds_sol
        self.wallet_pool.release_wallet(pos.wallet_address)

        if self.store is not None:
            self.store.record_execution(pos)

        return pos, receipt

    @property
    def all_positions(self) -> list[CabalActivePosition]:
        """Return all tracked positions (open and closed)."""
        return list(self._positions.values())

    @property
    def closed_positions(self) -> list[CabalActivePosition]:
        """Return list of closed positions."""
        return [p for p in self._positions.values() if p.is_closed]

    def get_portfolio_equity(
        self, current_prices: dict[str, float] | None = None
    ) -> float:
        """Calculate total portfolio equity (cash + open positions value)."""
        open_val = 0.0
        for pos in self.active_positions:
            price = (
                current_prices.get(pos.mint, pos.high_price_seen)
                if current_prices
                else pos.high_price_seen
            )
            open_val += pos.remaining_tokens * price
        return self.cash_balance_sol + open_val

    def get_realized_pnl(self) -> float:
        """Return total realized PnL across all closed and partial trades."""
        return sum(pos.realized_pnl_sol for pos in self._positions.values())

    def get_unrealized_pnl(
        self, current_prices: dict[str, float] | None = None
    ) -> float:
        """Return floating unrealized PnL on active positions."""
        unrealized = 0.0
        for pos in self.active_positions:
            price = (
                current_prices.get(pos.mint, pos.high_price_seen)
                if current_prices
                else pos.high_price_seen
            )
            cur_val = pos.remaining_tokens * price
            unrealized += cur_val - pos.cost_basis_remaining_sol
        return unrealized

    def get_session_stats(
        self, current_prices: dict[str, float] | None = None
    ) -> dict[str, Any]:
        """Return comprehensive financial metrics for paper trading session."""
        realized = self.get_realized_pnl()
        unrealized = self.get_unrealized_pnl(current_prices)
        net_pnl = realized + unrealized
        initial = max(0.0001, self.initial_cash_balance_sol)
        net_roi_pct = (net_pnl / initial) * 100.0

        closed = self.closed_positions
        wins = sum(1 for p in closed if p.realized_pnl_sol > 0)
        losses = sum(1 for p in closed if p.realized_pnl_sol < 0)
        winrate = (wins / len(closed) * 100.0) if closed else 0.0

        return {
            "initial_balance_sol": round(self.initial_cash_balance_sol, 4),
            "cash_balance_sol": round(self.cash_balance_sol, 4),
            "equity_sol": round(self.get_portfolio_equity(current_prices), 4),
            "realized_pnl_sol": round(realized, 4),
            "unrealized_pnl_sol": round(unrealized, 4),
            "net_pnl_sol": round(net_pnl, 4),
            "net_roi_pct": round(net_roi_pct, 2),
            "total_trades": len(self._positions),
            "closed_trades": len(closed),
            "wins": wins,
            "losses": losses,
            "winrate_pct": round(winrate, 1),
            "open_positions": len(self.active_positions),
        }


__all__ = [
    "DEFAULT_SLIPPAGE_PCT",
    "DEFAULT_TP_LEVELS",
    "DEFAULT_TRAILING_STOP_PCT",
    "CabalActivePosition",
    "CabalExecutor",
]
