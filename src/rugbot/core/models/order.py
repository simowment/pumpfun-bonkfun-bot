"""Universal order and trade outcome data contracts."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from rugbot.core.models.address import Address
from rugbot.core.models.token import TokenAmount

DEFAULT_SLIPPAGE_BPS = 250
MAX_SLIPPAGE_BPS = 10_000


class OrderSide(StrEnum):
    """Trading direction."""

    BUY = "buy"
    SELL = "sell"


class ExecutionMode(StrEnum):
    """Execution environment mode."""

    DRY_RUN = "dry_run"
    PAPER = "paper"
    LIVE = "live"


class FillStatus(StrEnum):
    """Lifecycle status of an execution."""

    PENDING = "pending"
    FILLED = "filled"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass(frozen=True, slots=True)
class OrderIntent:
    """Chain-agnostic trade intent dispatched to an execution port.

    Attributes:
        intent_id: Unique identifier for tracking and deduplication.
        target_token: Address of the token to trade.
        side: Buy or sell.
        amount_in: Exact input amount (native quote currency on buy, token on sell).
        max_slippage_bps: Maximum allowable slippage in basis points (1 bps = 0.01%).
        mode: Dry-run, paper, or live.
        priority_fee_native: Additional priority fee in native gas units (SOL or ETH).
        tip_native: MEV tip in native currency (Jito tip on Solana, builder tip on EVM).
    """

    intent_id: str
    target_token: Address
    side: OrderSide
    amount_in: TokenAmount
    max_slippage_bps: int = DEFAULT_SLIPPAGE_BPS
    mode: ExecutionMode = ExecutionMode.DRY_RUN
    priority_fee_native: float = 0.0
    tip_native: float = 0.0

    def __post_init__(self) -> None:
        if not self.intent_id:
            msg = "intent_id is required"
            raise ValueError(msg)
        if not isinstance(self.target_token, Address):
            msg = "target_token must be an Address"
            raise TypeError(msg)
        if not isinstance(self.amount_in, TokenAmount) or self.amount_in.raw_units <= 0:
            msg = "amount_in must be a positive TokenAmount"
            raise ValueError(msg)
        if not (0 <= self.max_slippage_bps <= MAX_SLIPPAGE_BPS):
            msg = f"max_slippage_bps must be between 0 and {MAX_SLIPPAGE_BPS}"
            raise ValueError(msg)


@dataclass(frozen=True, slots=True)
class TradeReceipt:
    """Receipt returned by an execution port following order processing.

    Attributes:
        ok: True if transaction executed successfully (or simulated successfully).
        intent_id: Correlating intent identifier.
        target_token: Address of traded token.
        side: OrderSide (buy or sell).
        tx_hash: Transaction hash or signature, None for dry-run/failed.
        filled_amount_in: Exact input spent.
        filled_amount_out: Exact output received.
        effective_price: Realized price (quote per token).
        fee_paid_native: Gas, priority, and MEV fees paid in native units.
        error_message: Explanatory error details on failure.
    """

    ok: bool
    intent_id: str
    target_token: Address
    side: OrderSide
    tx_hash: str | None
    filled_amount_in: TokenAmount
    filled_amount_out: TokenAmount
    effective_price: float
    fee_paid_native: float
    error_message: str | None = None
