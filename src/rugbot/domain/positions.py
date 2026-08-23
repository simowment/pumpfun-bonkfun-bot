"""Domain models for open trading positions, calibration evidence, and exit states."""

# ruff: noqa: TC001

from __future__ import annotations

from dataclasses import dataclass, field

from rugbot.domain.amounts import QuoteBaseUnits, Slot, TokenBaseUnits


@dataclass(frozen=True, slots=True)
class ExitRuleState:
    """Immutable position-exit state used to make levels one-shot."""

    filled_take_profit_level_indices: tuple[int, ...] = ()
    filled_stop_loss_level_indices: tuple[int, ...] = ()
    filled_big_buy_level_indices: tuple[int, ...] = ()
    exited_fraction_ppm: int = 0


@dataclass(frozen=True, slots=True)
class CalibratedExitEvidence:
    """Point-in-time CopyTrade exit calibration for one open market."""

    as_of_slot: Slot
    market_id: str
    take_profit_pnl_ppm: int
    adverse_event_slot: Slot | None = None


@dataclass(frozen=True, slots=True)
class PositionMarketEvidence:
    """Immutable point-in-time market and PnL evidence for one position."""

    as_of_slot: Slot
    market_id: str
    current_pnl_ppm: int
    idle_ms: int
    executable_exit_capacity_base_units: TokenBaseUnits | None
    current_market_cap_quote_base_units: QuoteBaseUnits | None = None
    calibrated_exit_evidence: CalibratedExitEvidence | None = None


@dataclass(frozen=True, slots=True)
class PaperPositionState:
    """State carried between paper position evaluations."""

    as_of_slot: Slot
    market_id: str
    target_id: str
    execution_mode: str
    original_position_base_units: TokenBaseUnits
    current_position_base_units: TokenBaseUnits
    entry_quote_lamports: int
    entry_cost_lamports: int
    take_profit_pnl_ppm: int | None
    stop_loss_pnl_ppm: int | None
    max_slippage_bps: int
    peak_pnl_ppm: int = 0
    exit_rule_state: ExitRuleState = field(default_factory=ExitRuleState)


__all__ = [
    "CalibratedExitEvidence",
    "ExitRuleState",
    "PaperPositionState",
    "PositionMarketEvidence",
]
