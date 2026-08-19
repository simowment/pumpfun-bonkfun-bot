"""Last-moment integer risk gates for sniper execution intents."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from rugbot.execution.ports import ExecutionIntent, validate_execution_intent

BASIS_POINTS_DENOMINATOR = 10_000


class RiskDecisionCode(StrEnum):
    """Machine-readable execution risk outcome."""

    ALLOWED = "allowed"
    INVALID_INTENT = "invalid_intent"
    KILL_SWITCH = "kill_switch"
    SLIPPAGE_LIMIT = "slippage_limit"
    BUY_SIZE_LIMIT = "buy_size_limit"
    EXPOSURE_LIMIT = "exposure_limit"
    DAILY_LOSS_LIMIT = "daily_loss_limit"
    OPEN_POSITION_LIMIT = "open_position_limit"
    INSUFFICIENT_BALANCE = "insufficient_balance"
    POSITION_BALANCE = "position_balance"


@dataclass(frozen=True, slots=True)
class RiskLimits:
    """Configured hard bounds for one target execution strategy."""

    max_buy_lamports: int
    max_exposure_lamports: int
    daily_loss_limit_lamports: int
    max_open_positions: int
    max_slippage_bps: int
    minimum_wallet_reserve_lamports: int


@dataclass(frozen=True, slots=True)
class ExecutionCostBudget:
    """Worst-case native costs reserved before submission."""

    network_fee_lamports: int
    jito_tip_lamports: int
    ata_rent_lamports: int

    @property
    def total_lamports(self) -> int:
        """Return the maximum native cost reserved for this attempt."""

        return (
            self.network_fee_lamports + self.jito_tip_lamports + self.ata_rent_lamports
        )


@dataclass(frozen=True, slots=True)
class RiskSnapshot:
    """Point-in-time wallet and portfolio facts used by the gate."""

    wallet_balance_lamports: int
    current_exposure_lamports: int
    daily_realized_pnl_lamports: int
    open_positions_count: int
    position_token_balance_base_units: int
    kill_switch_active: bool


@dataclass(frozen=True, slots=True)
class RiskDecision:
    """One explicit allow or deny result."""

    allowed: bool
    code: RiskDecisionCode
    message: str


class RiskGatekeeperError(ValueError):
    """Raised when risk configuration or snapshots are malformed."""

    @classmethod
    def invalid_field(cls, field_name: str) -> RiskGatekeeperError:
        """Build an error for a malformed risk field."""

        return cls(f"{field_name} must be a non-negative integer")


class RiskGatekeeper:
    """Evaluate buy and sell intents against one strict risk contract."""

    def __init__(self, limits: RiskLimits) -> None:
        """Validate and retain immutable target risk limits."""

        _validate_limits(limits)
        self._limits = limits

    def evaluate(
        self,
        intent: ExecutionIntent,
        *,
        snapshot: RiskSnapshot,
        cost_budget: ExecutionCostBudget,
    ) -> RiskDecision:
        """Allow only an intent that satisfies all applicable last-moment gates."""

        intent_error = validate_execution_intent(intent)
        if intent_error is not None:
            return _denied(RiskDecisionCode.INVALID_INTENT, intent_error)
        _validate_snapshot(snapshot)
        _validate_cost_budget(cost_budget)
        if intent.max_slippage_bps > self._limits.max_slippage_bps:
            return _denied(
                RiskDecisionCode.SLIPPAGE_LIMIT,
                "intent slippage exceeds the configured hard limit",
            )
        if intent.side == "sell":
            return self._evaluate_sell(intent, snapshot, cost_budget)
        return self._evaluate_buy(intent, snapshot, cost_budget)

    def _evaluate_buy(  # noqa: PLR0911 - every return is a required hard risk gate.
        self,
        intent: ExecutionIntent,
        snapshot: RiskSnapshot,
        cost_budget: ExecutionCostBudget,
    ) -> RiskDecision:
        quote_amount = int(intent.quote_amount_base_units)
        if snapshot.kill_switch_active:
            return _denied(
                RiskDecisionCode.KILL_SWITCH,
                "kill switch blocks new buy exposure",
            )
        if quote_amount > self._limits.max_buy_lamports:
            return _denied(
                RiskDecisionCode.BUY_SIZE_LIMIT,
                "buy amount exceeds the configured hard limit",
            )
        if (
            snapshot.current_exposure_lamports + quote_amount
            > self._limits.max_exposure_lamports
        ):
            return _denied(
                RiskDecisionCode.EXPOSURE_LIMIT,
                "buy would exceed maximum portfolio exposure",
            )
        realized_loss = max(0, -snapshot.daily_realized_pnl_lamports)
        if realized_loss >= self._limits.daily_loss_limit_lamports:
            return _denied(
                RiskDecisionCode.DAILY_LOSS_LIMIT,
                "daily realized loss limit blocks new buy exposure",
            )
        if snapshot.open_positions_count >= self._limits.max_open_positions:
            return _denied(
                RiskDecisionCode.OPEN_POSITION_LIMIT,
                "maximum open positions blocks new buy exposure",
            )
        required_balance = (
            quote_amount
            + cost_budget.total_lamports
            + self._limits.minimum_wallet_reserve_lamports
        )
        if snapshot.wallet_balance_lamports < required_balance:
            return _denied(
                RiskDecisionCode.INSUFFICIENT_BALANCE,
                "wallet balance cannot fund buy, costs, and reserve",
            )
        return _allowed()

    @staticmethod
    def _evaluate_sell(
        intent: ExecutionIntent,
        snapshot: RiskSnapshot,
        cost_budget: ExecutionCostBudget,
    ) -> RiskDecision:
        base_amount = int(intent.base_amount_base_units)
        if base_amount > snapshot.position_token_balance_base_units:
            return _denied(
                RiskDecisionCode.POSITION_BALANCE,
                "sell amount exceeds the current position balance",
            )
        upfront_cost = cost_budget.network_fee_lamports + cost_budget.jito_tip_lamports
        if snapshot.wallet_balance_lamports < upfront_cost:
            return _denied(
                RiskDecisionCode.INSUFFICIENT_BALANCE,
                "wallet balance cannot fund sell submission costs",
            )
        return _allowed()


def _allowed() -> RiskDecision:
    return RiskDecision(
        allowed=True,
        code=RiskDecisionCode.ALLOWED,
        message="execution risk checks passed",
    )


def _denied(code: RiskDecisionCode, message: str) -> RiskDecision:
    return RiskDecision(allowed=False, code=code, message=message)


def _validate_limits(limits: object) -> None:
    if not isinstance(limits, RiskLimits):
        raise RiskGatekeeperError.invalid_field("limits")
    for field_name, value in (
        ("max_buy_lamports", limits.max_buy_lamports),
        ("max_exposure_lamports", limits.max_exposure_lamports),
        ("daily_loss_limit_lamports", limits.daily_loss_limit_lamports),
        ("max_open_positions", limits.max_open_positions),
        ("minimum_wallet_reserve_lamports", limits.minimum_wallet_reserve_lamports),
    ):
        _validate_non_negative_int(value, field_name)
    if type(limits.max_slippage_bps) is not int or not (
        0 <= limits.max_slippage_bps <= BASIS_POINTS_DENOMINATOR
    ):
        raise RiskGatekeeperError.invalid_field("max_slippage_bps")


def _validate_snapshot(snapshot: object) -> None:
    if not isinstance(snapshot, RiskSnapshot):
        raise RiskGatekeeperError.invalid_field("snapshot")
    for field_name, value in (
        ("wallet_balance_lamports", snapshot.wallet_balance_lamports),
        ("current_exposure_lamports", snapshot.current_exposure_lamports),
        (
            "position_token_balance_base_units",
            snapshot.position_token_balance_base_units,
        ),
        ("open_positions_count", snapshot.open_positions_count),
    ):
        _validate_non_negative_int(value, field_name)
    if type(snapshot.daily_realized_pnl_lamports) is not int:
        raise RiskGatekeeperError.invalid_field("daily_realized_pnl_lamports")
    if type(snapshot.kill_switch_active) is not bool:
        raise RiskGatekeeperError.invalid_field("kill_switch_active")


def _validate_cost_budget(cost_budget: object) -> None:
    if not isinstance(cost_budget, ExecutionCostBudget):
        raise RiskGatekeeperError.invalid_field("cost_budget")
    for field_name, value in (
        ("network_fee_lamports", cost_budget.network_fee_lamports),
        ("jito_tip_lamports", cost_budget.jito_tip_lamports),
        ("ata_rent_lamports", cost_budget.ata_rent_lamports),
    ):
        _validate_non_negative_int(value, field_name)


def _validate_non_negative_int(value: object, field_name: str) -> None:
    if type(value) is not int or value < 0:
        raise RiskGatekeeperError.invalid_field(field_name)


__all__ = [
    "ExecutionCostBudget",
    "RiskDecision",
    "RiskDecisionCode",
    "RiskGatekeeper",
    "RiskGatekeeperError",
    "RiskLimits",
    "RiskSnapshot",
]
