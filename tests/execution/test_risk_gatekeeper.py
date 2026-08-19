"""Behavior tests for asymmetric last-moment execution risk gates."""

import pytest
from solders.pubkey import Pubkey

from rugbot.execution.ports import ExecutionIntent
from rugbot.runtime.risk_gatekeeper import (
    ExecutionCostBudget,
    RiskDecisionCode,
    RiskGatekeeper,
    RiskLimits,
    RiskSnapshot,
)

LIMITS = RiskLimits(
    max_buy_lamports=50_000_000,
    max_exposure_lamports=200_000_000,
    daily_loss_limit_lamports=100_000_000,
    max_open_positions=3,
    max_slippage_bps=1_000,
    minimum_wallet_reserve_lamports=10_000_000,
)
COSTS = ExecutionCostBudget(
    network_fee_lamports=5_000,
    jito_tip_lamports=1_000_000,
    ata_rent_lamports=2_039_280,
)


def _buy() -> ExecutionIntent:
    return ExecutionIntent(
        intent_id="risk-buy",
        as_of_slot=100,
        market_id=str(Pubkey.new_unique()),
        side="buy",
        quote_amount_base_units=25_000_000,
        base_amount_base_units=None,
        max_slippage_bps=500,
        reason_codes=("known_operator_wallet",),
    )


def _sell(*, amount: int = 500) -> ExecutionIntent:
    return ExecutionIntent(
        intent_id="risk-sell",
        as_of_slot=101,
        market_id=str(Pubkey.new_unique()),
        side="sell",
        quote_amount_base_units=None,
        base_amount_base_units=amount,
        max_slippage_bps=500,
        reason_codes=("stop_loss",),
    )


def _snapshot(**changes: object) -> RiskSnapshot:
    values = {
        "wallet_balance_lamports": 1_000_000_000,
        "current_exposure_lamports": 100_000_000,
        "daily_realized_pnl_lamports": 0,
        "open_positions_count": 0,
        "position_token_balance_base_units": 1_000,
        "kill_switch_active": False,
    }
    values.update(changes)
    return RiskSnapshot(**values)


def test_buy_passes_with_integer_budget_and_reserve() -> None:
    decision = RiskGatekeeper(LIMITS).evaluate(
        _buy(),
        snapshot=_snapshot(),
        cost_budget=COSTS,
    )

    assert decision.allowed
    assert decision.code is RiskDecisionCode.ALLOWED


@pytest.mark.parametrize(
    ("snapshot_changes", "expected_code"),
    [
        ({"kill_switch_active": True}, RiskDecisionCode.KILL_SWITCH),
        ({"current_exposure_lamports": 190_000_000}, RiskDecisionCode.EXPOSURE_LIMIT),
        (
            {"daily_realized_pnl_lamports": -100_000_000},
            RiskDecisionCode.DAILY_LOSS_LIMIT,
        ),
        ({"open_positions_count": 3}, RiskDecisionCode.OPEN_POSITION_LIMIT),
        (
            {"wallet_balance_lamports": 20_000_000},
            RiskDecisionCode.INSUFFICIENT_BALANCE,
        ),
    ],
)
def test_buy_risk_blocks_are_explicit(
    snapshot_changes: dict[str, object],
    expected_code: RiskDecisionCode,
) -> None:
    decision = RiskGatekeeper(LIMITS).evaluate(
        _buy(),
        snapshot=_snapshot(**snapshot_changes),
        cost_budget=COSTS,
    )

    assert not decision.allowed
    assert decision.code is expected_code


def test_kill_switch_and_daily_loss_do_not_block_risk_reducing_sell() -> None:
    decision = RiskGatekeeper(LIMITS).evaluate(
        _sell(),
        snapshot=_snapshot(
            kill_switch_active=True,
            daily_realized_pnl_lamports=-500_000_000,
            current_exposure_lamports=500_000_000,
        ),
        cost_budget=COSTS,
    )

    assert decision.allowed


def test_sell_cannot_exceed_current_position() -> None:
    decision = RiskGatekeeper(LIMITS).evaluate(
        _sell(amount=1_001),
        snapshot=_snapshot(),
        cost_budget=COSTS,
    )

    assert not decision.allowed
    assert decision.code is RiskDecisionCode.POSITION_BALANCE


def test_slippage_hard_limit_applies_to_buy_and_sell() -> None:
    intent = _sell()
    excessive = ExecutionIntent(
        intent_id=intent.intent_id,
        as_of_slot=intent.as_of_slot,
        market_id=intent.market_id,
        side=intent.side,
        quote_amount_base_units=intent.quote_amount_base_units,
        base_amount_base_units=intent.base_amount_base_units,
        max_slippage_bps=1_001,
        reason_codes=intent.reason_codes,
    )

    decision = RiskGatekeeper(LIMITS).evaluate(
        excessive,
        snapshot=_snapshot(),
        cost_budget=COSTS,
    )

    assert decision.code is RiskDecisionCode.SLIPPAGE_LIMIT
