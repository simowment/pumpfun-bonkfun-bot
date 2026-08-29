"""Pure scalper strategy decisions for Pump.fun high-frequency scalps.

Paper-only evaluator: no live orders. Decision is deterministic given
entry price and current price, with tranches and circuit-breaker tracking
handled by the caller.

TP/SL are expressed in percent of entry price (>=0).
sell_fractions sum to 1.0.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class ScalperConfig:
    """Configurable scalper parameters."""

    position_size_sol: float = 1.35
    entry_mc_max_sol: float = 15_000.0
    # threshold expressed as quote_lamports max for entry (proxy for MC)
    entry_max_quote_lamports: int = 15_000_000_000  # 15k SOL in lamports proxy
    tp_levels_pct: tuple[float, ...] = (25.0, 35.0, 45.0)
    sl_pct: float = 12.0
    sell_fractions: tuple[float, ...] = (0.2, 0.3, 0.5)
    daily_loss_stop: int = 5
    max_concurrent: int = 3
    max_hold_slots: int = 25
    min_trades_for_entry: int = 1
    max_entry_slot_offset: int = 12

    def __post_init__(self) -> None:
        if len(self.tp_levels_pct) != len(self.sell_fractions):
            raise ValueError("tp_levels_pct and sell_fractions must have same length")
        if abs(sum(self.sell_fractions) - 1.0) > 1e-9:
            raise ValueError("sell_fractions must sum to 1.0")
        if self.sl_pct <= 0:
            raise ValueError("sl_pct must be positive")
        if any(v <= 0 for v in self.tp_levels_pct):
            raise ValueError("tp_levels_pct must be positive")
        if self.daily_loss_stop <= 0:
            raise ValueError("daily_loss_stop must be positive")
        if self.position_size_sol <= 0:
            raise ValueError("position_size_sol must be positive")


@dataclass(frozen=True, slots=True)
class ScalperSignal:
    """Decision at a price tick."""

    action: str  # "hold" | "take_profit" | "stop_loss" | "timeout"
    tranche_index: int | None = None
    fraction: float | None = None
    pnl_pct: float = 0.0
    reason: str = ""


@dataclass(frozen=True, slots=True)
class ScalperPositionState:
    """Mutable-ish per-position tracker stored outside pure function."""

    entry_price_ppm: int
    entry_slot: int
    filled_fractions: tuple[bool, ...] = field(default_factory=tuple)
    closed: bool = False


def _pnl_pct(entry_ppm: int, current_ppm: int) -> float:
    if entry_ppm <= 0:
        return 0.0
    return (current_ppm - entry_ppm) / entry_ppm * 100.0


def decide_scalper_exit(
    *,
    config: ScalperConfig,
    entry_price_ppm: int,
    current_price_ppm: int,
    current_slot: int,
    entry_slot: int,
    filled: tuple[bool, ...],
    consecutive_losses: int = 0,
) -> ScalperSignal:
    """Pure decision: given prices and filled tranches, what to do.

    Args:
        config: Strategy config.
        entry_price_ppm: Entry price in ppm (quote/base normalized).
        current_price_ppm: Current observed price ppm.
        current_slot: Current slot.
        entry_slot: Entry slot.
        filled: Which TP tranches already taken.
        consecutive_losses: Daily consecutive losses (for circuit breaker).

    Returns:
        ScalperSignal with action.
    """
    if consecutive_losses >= config.daily_loss_stop:
        return ScalperSignal(
            action="hold",
            pnl_pct=_pnl_pct(entry_price_ppm, current_price_ppm),
            reason="circuit_breaker",
        )
    pnl = _pnl_pct(entry_price_ppm, current_price_ppm)

    # Stop-loss has priority (one-shot)
    if pnl <= -abs(config.sl_pct):
        return ScalperSignal(
            action="stop_loss", fraction=1.0, pnl_pct=pnl, reason="stop_loss"
        )

    # Timeout
    if current_slot - entry_slot > config.max_hold_slots:
        if pnl > 0:
            return ScalperSignal(
                action="take_profit", fraction=1.0, pnl_pct=pnl, reason="timeout_profit"
            )
        return ScalperSignal(
            action="stop_loss", fraction=1.0, pnl_pct=pnl, reason="timeout_loss"
        )

    # TP tranches in order
    for idx, tp in enumerate(config.tp_levels_pct):
        if idx < len(filled) and filled[idx]:
            continue
        if pnl >= tp:
            frac = (
                config.sell_fractions[idx] if idx < len(config.sell_fractions) else 0.0
            )
            return ScalperSignal(
                action="take_profit",
                tranche_index=idx,
                fraction=frac,
                pnl_pct=pnl,
                reason=f"tp{idx + 1}_{tp:.0f}pct",
            )

    return ScalperSignal(action="hold", pnl_pct=pnl, reason="hold")


def next_filled(filled: tuple[bool, ...], tranche_index: int) -> tuple[bool, ...]:
    """Return new filled tuple with tranche marked."""
    lst = list(filled)
    # extend if needed
    while len(lst) <= tranche_index:
        lst.append(False)
    lst[tranche_index] = True
    return tuple(lst)


def should_enter(*, config: ScalperConfig, price_ppm: int, slot_offset: int) -> bool:
    """Pure entry predicate (proxy MC check via price)."""
    # price_ppm >0 always; we use slot_offset as proxy for earliness
    if slot_offset > config.max_entry_slot_offset:
        return False
    # If entry_max_quote_lamports is set, caller should have filtered by lamports.
    # Here we just check price is positive.
    return price_ppm > 0
