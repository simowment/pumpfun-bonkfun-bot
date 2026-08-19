"""Execution-port domain contracts for isolated adverse-intel decisions."""

from dataclasses import dataclass
from enum import Enum
from typing import Literal, Protocol

from rugbot.domain.amounts import Lamports, Slot

ExecutionSide = Literal["buy", "sell"]
MAX_SLIPPAGE_BPS = 10_000


class ExecutionMode(Enum):
    """Supported execution isolation modes."""

    OBSERVE = "observe"
    PAPER = "paper"
    SIMULATION = "simulation"
    PROBE = "probe"
    LIVE = "live"


@dataclass(frozen=True, slots=True)
class ExecutionIntent:
    """An execution request from the decision system.

    Args:
        intent_id: Deterministic decision-local identifier.
        as_of_slot: Slot boundary for all state used by the decision.
        market_id: Market or mint identifier.
        side: Buy or sell direction.
        quote_amount_base_units: Quote amount in base units, when applicable.
        base_amount_base_units: Base token amount in base units, when applicable.
        max_slippage_bps: Maximum slippage tolerance in basis points.
        reason_codes: Machine-readable decision reasons.
    """

    intent_id: str
    as_of_slot: Slot
    market_id: str
    side: ExecutionSide
    quote_amount_base_units: int | None
    base_amount_base_units: int | None
    max_slippage_bps: int
    reason_codes: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ExecutionReceipt:
    """Execution-port response for observe, paper, probe, or live modes."""

    mode: ExecutionMode
    intent_id: str
    as_of_slot: Slot
    accepted: bool
    would_submit_transaction: bool
    signature: str | None
    simulated_output_base_units: int | None
    estimated_fee_lamports: Lamports | None
    message: str


def validate_execution_intent(intent: object) -> str | None:
    """Return a validation error for malformed execution intents."""

    if not isinstance(intent, ExecutionIntent):
        return "execution intent is malformed"
    field_error = _validate_intent_fields(intent)
    if field_error is not None:
        return field_error
    return _validate_intent_side_amounts(intent)


def non_submitting_receipt(
    *,
    mode: ExecutionMode,
    intent: ExecutionIntent | None,
    message: str,
    estimated_fee_lamports: Lamports | None = None,
) -> ExecutionReceipt:
    """Build a receipt that cannot imply transaction submission."""

    return ExecutionReceipt(
        mode=mode,
        intent_id=_receipt_intent_id(intent),
        as_of_slot=_receipt_as_of_slot(intent),
        accepted=False,
        would_submit_transaction=False,
        signature=None,
        simulated_output_base_units=None,
        estimated_fee_lamports=estimated_fee_lamports,
        message=message,
    )


class ExecutionPort(Protocol):
    """Execution adapter boundary used by decision and replay code."""

    async def submit(self, intent: ExecutionIntent) -> ExecutionReceipt:
        """Submit or simulate an execution intent."""


class PaperTradeSimulator(Protocol):
    """Local deterministic paper-trade simulator contract."""

    async def simulate(self, intent: ExecutionIntent) -> ExecutionReceipt:
        """Simulate one execution intent without network submission."""


def _validate_intent_fields(intent: ExecutionIntent) -> str | None:
    identity_error = _validate_intent_identity(intent)
    if identity_error is not None:
        return identity_error
    if not _valid_slippage_bps(intent.max_slippage_bps):
        return "max_slippage_bps must be an integer between 0 and 10000"
    if not _valid_reason_codes(intent.reason_codes):
        return "reason_codes are required"
    return None


def _validate_intent_identity(intent: ExecutionIntent) -> str | None:
    if type(intent.intent_id) is not str or not intent.intent_id:
        return "intent_id is required"
    if not _non_negative_int(intent.as_of_slot):
        return "as_of_slot must be a non-negative integer"
    if type(intent.market_id) is not str or not intent.market_id:
        return "market_id is required"
    if type(intent.side) is not str or intent.side not in ("buy", "sell"):
        return "side must be buy or sell"
    return None


def _receipt_intent_id(intent: ExecutionIntent | None) -> str:
    if intent is not None and type(intent.intent_id) is str:
        return intent.intent_id
    return ""


def _receipt_as_of_slot(intent: ExecutionIntent | None) -> Slot:
    if intent is not None and _non_negative_int(intent.as_of_slot):
        return intent.as_of_slot
    return Slot(-1)


def _validate_intent_side_amounts(intent: ExecutionIntent) -> str | None:
    if intent.side == "buy":
        if not _positive_int(intent.quote_amount_base_units):
            return "buy intent requires a positive quote amount"
        if intent.base_amount_base_units is not None:
            return "buy intent must not include a base amount"
    if intent.side == "sell":
        if not _positive_int(intent.base_amount_base_units):
            return "sell intent requires a positive base amount"
        if intent.quote_amount_base_units is not None:
            return "sell intent must not include a quote amount"
    return None


def _valid_slippage_bps(value: object) -> bool:
    return type(value) is int and 0 <= value <= MAX_SLIPPAGE_BPS


def _valid_reason_codes(reason_codes: object) -> bool:
    return (
        type(reason_codes) is tuple
        and bool(reason_codes)
        and all(
            type(reason_code) is str and reason_code for reason_code in reason_codes
        )
    )


def _non_negative_int(value: object) -> bool:
    return type(value) is int and value >= 0


def _positive_int(value: object) -> bool:
    return type(value) is int and value > 0
