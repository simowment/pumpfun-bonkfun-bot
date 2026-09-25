"""Automated bonding-curve exit and capital recovery controller for launched tokens."""

# ruff: noqa: PLR0913, TC001, TC002

from __future__ import annotations

import time
from dataclasses import dataclass
from enum import StrEnum
from typing import Final

from solders.instruction import Instruction
from solders.keypair import Keypair
from solders.pubkey import Pubkey

from rugbot.core.decision.take_profit import TakeProfitLadder
from rugbot.execution.launch.bundle_assembler import (
    PUMP_CANONICAL_BUYBACK_RECIPIENT,
    PUMP_CANONICAL_FEE_RECIPIENT,
)
from rugbot.execution.v2_builder import (
    PumpV2BuildContext,
    build_sell_v2_instructions,
)
from rugbot.ingest.pump.create_decoder import SPL_2022_PROGRAM_ID
from rugbot.utils.logger import get_logger

logger = get_logger(__name__)

DEFAULT_TP_LEVELS: Final[tuple[tuple[float, float], ...]] = (
    (100.0, 0.50),  # +100% (2x): sell 50%
    (400.0, 0.25),  # +400% (5x): sell 25%
)
DEFAULT_TRAILING_STOP_PCT: Final[float] = 15.0
DEFAULT_DEAD_LAUNCH_TIMEOUT_SECONDS: Final[float] = 45.0


class ExitAction(StrEnum):
    """Exit decision taken on the launched token."""

    HOLD = "hold"
    TAKE_PROFIT = "take_profit"
    TRAILING_STOP = "trailing_stop"
    DEAD_LAUNCH_TIMEOUT = "dead_launch_timeout"


@dataclass(frozen=True, slots=True)
class LaunchExitSignal:
    """Actionable exit signal generated for a launched position."""

    action: ExitAction
    sell_tokens: int
    reason: str
    price_sol: float


@dataclass
class LaunchPositionState:
    """Live state tracking for an automated launch position."""

    mint: str
    entry_price_sol: float
    total_tokens: int
    remaining_tokens: int
    high_water_mark_price: float
    created_at_timestamp: float
    tp_ladder: TakeProfitLadder
    dead_launch_timeout_seconds: float = DEFAULT_DEAD_LAUNCH_TIMEOUT_SECONDS
    trailing_stop_pct: float = DEFAULT_TRAILING_STOP_PCT
    external_buyers_seen: int = 0

    def on_external_trade(self, price_sol: float) -> None:
        """Record an external market trade and update price watermarks."""
        self.external_buyers_seen += 1
        self.high_water_mark_price = max(self.high_water_mark_price, price_sol)

    def evaluate(
        self,
        current_price_sol: float,
        now: float | None = None,
    ) -> LaunchExitSignal:
        """Evaluate position against TP ladder, trailing stop, and dead-launch timeout."""
        if self.remaining_tokens <= 0:
            return LaunchExitSignal(
                action=ExitAction.HOLD,
                sell_tokens=0,
                reason="Position fully exited",
                price_sol=current_price_sol,
            )

        current_time = now if now is not None else time.time()
        elapsed_seconds = current_time - self.created_at_timestamp

        # 1. Check Dead-Launch Timeout: No external interest arrived within window
        if (
            self.external_buyers_seen == 0
            and elapsed_seconds >= self.dead_launch_timeout_seconds
        ):
            return LaunchExitSignal(
                action=ExitAction.DEAD_LAUNCH_TIMEOUT,
                sell_tokens=self.remaining_tokens,
                reason=(
                    f"Dead launch: 0 external buyers after {elapsed_seconds:.1f}s. "
                    "Auto-dumping to recover dev buy."
                ),
                price_sol=current_price_sol,
            )

        # Update high-water mark
        self.high_water_mark_price = max(self.high_water_mark_price, current_price_sol)

        # 2. Check Take Profit Ladder
        updated_ladder, fraction = self.tp_ladder.evaluate(
            entry_price=self.entry_price_sol,
            current_price=current_price_sol,
        )
        if fraction > 0.0:
            tokens_to_sell = int(self.remaining_tokens * fraction)
            if tokens_to_sell > 0:
                self.tp_ladder = updated_ladder
                return LaunchExitSignal(
                    action=ExitAction.TAKE_PROFIT,
                    sell_tokens=tokens_to_sell,
                    reason=f"TP triggered: selling {fraction * 100:.0f}% of position at {current_price_sol:.9f} SOL",
                    price_sol=current_price_sol,
                )

        # 3. Check Trailing Stop (Active only after price gained >= 20% over entry)
        if self.high_water_mark_price >= self.entry_price_sol * 1.20:
            drawdown_pct = (
                (self.high_water_mark_price - current_price_sol)
                / self.high_water_mark_price
            ) * 100.0
            if drawdown_pct >= self.trailing_stop_pct:
                return LaunchExitSignal(
                    action=ExitAction.TRAILING_STOP,
                    sell_tokens=self.remaining_tokens,
                    reason=(
                        f"Trailing stop triggered: {drawdown_pct:.1f}% drop from peak "
                        f"{self.high_water_mark_price:.9f} SOL"
                    ),
                    price_sol=current_price_sol,
                )

        return LaunchExitSignal(
            action=ExitAction.HOLD,
            sell_tokens=0,
            reason="Holding position",
            price_sol=current_price_sol,
        )


def build_launch_sell_instructions(
    *,
    payer: Keypair,
    mint: Pubkey,
    tokens_to_sell: int,
    min_sol_out_lamports: int = 1,
    fee_recipient: Pubkey | None = None,
    buyback_recipient: Pubkey | None = None,
) -> tuple[Instruction, ...]:
    """Build pure sell_v2 instructions to exit a launched token position."""
    actual_fee_recipient = (
        fee_recipient
        if fee_recipient is not None
        else Pubkey.from_string(PUMP_CANONICAL_FEE_RECIPIENT)
    )
    actual_buyback_recipient = (
        buyback_recipient
        if buyback_recipient is not None
        else Pubkey.from_string(PUMP_CANONICAL_BUYBACK_RECIPIENT)
    )

    context = PumpV2BuildContext(
        mint=mint,
        creator=payer.pubkey(),
        user=payer.pubkey(),
        base_token_program=Pubkey.from_string(SPL_2022_PROGRAM_ID),
        fee_recipient=actual_fee_recipient,
        buyback_fee_recipient=actual_buyback_recipient,
        amount=tokens_to_sell,
        quote_limit=min_sol_out_lamports,
    )
    sell_set = build_sell_v2_instructions(context)
    return sell_set.instructions


__all__ = [
    "DEFAULT_DEAD_LAUNCH_TIMEOUT_SECONDS",
    "DEFAULT_TP_LEVELS",
    "DEFAULT_TRAILING_STOP_PCT",
    "ExitAction",
    "LaunchExitSignal",
    "LaunchPositionState",
    "build_launch_sell_instructions",
]
