"""Position tracking and trade performance models."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from rugbot.core.models.address import Address
    from rugbot.core.models.token import TokenAmount


@dataclass(frozen=True, slots=True)
class ActivePosition:
    """An open trading position being managed."""

    target_token: Address
    entry_price: float
    amount: TokenAmount
    cost_native: float
    peak_price: float
    entry_timestamp: float


@dataclass(frozen=True, slots=True)
class ClosedTrade:
    """A finalized round-trip trade."""

    target_token: Address
    entry_price: float
    exit_price: float
    amount: TokenAmount
    net_pnl_native: float
    net_roi_pct: float
    hold_duration_seconds: float
    exit_reason: str


@dataclass(frozen=True, slots=True)
class PortfolioMetrics:
    """Aggregate portfolio health and PnL metrics."""

    open_positions_count: int
    total_cost_native: float
    current_value_native: float
    unrealized_pnl_native: float
    realized_pnl_native: float
    winrate_pct: float
