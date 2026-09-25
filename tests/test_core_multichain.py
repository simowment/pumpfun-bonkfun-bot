"""Tests for chain-agnostic core domain models, ports, and decision logic."""

from __future__ import annotations

import pytest

from rugbot.core.decision.risk_gatekeeper import RiskConfig, RiskGatekeeper
from rugbot.core.decision.take_profit import TakeProfitLadder
from rugbot.core.decision.trailing_stop import TrailingStopState
from rugbot.core.models.address import Address
from rugbot.core.models.order import (
    ExecutionMode,
    OrderIntent,
    OrderSide,
    TradeReceipt,
)
from rugbot.core.models.quote import ExecutionQuote
from rugbot.core.models.token import TokenAmount, TokenMetadata


def test_address_models() -> None:
    sol_addr = Address(
        "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P", chain_id="solana:mainnet"
    )
    assert str(sol_addr) == "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
    assert sol_addr.is_solana()
    assert not sol_addr.is_evm()

    evm_addr = Address(
        "0x1234567890abcdef1234567890abcdef12345678", chain_id="evm:robinhood_orbit"
    )
    assert str(evm_addr) == "0x1234567890abcdef1234567890abcdef12345678"
    assert evm_addr.is_evm()
    assert not evm_addr.is_solana()

    with pytest.raises(ValueError, match="non-empty string"):
        Address("")


def test_token_amount_and_metadata() -> None:
    amount = TokenAmount.from_ui(1.5, decimals=9)
    assert amount.raw_units == 1_500_000_000
    assert amount.ui_value == 1.5

    meta = TokenMetadata(
        address=Address("6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"),
        symbol="TEST",
        name="Test Token",
        decimals=6,
    )
    assert meta.symbol == "TEST"
    assert meta.decimals == 6


def test_order_intent_and_receipt() -> None:
    token = Address("6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P")
    intent = OrderIntent(
        intent_id="intent_001",
        target_token=token,
        side=OrderSide.BUY,
        amount_in=TokenAmount.from_ui(0.1, decimals=9),
        mode=ExecutionMode.DRY_RUN,
    )
    assert intent.side == OrderSide.BUY
    assert intent.amount_in.ui_value == 0.1

    receipt = TradeReceipt(
        ok=True,
        intent_id="intent_001",
        target_token=token,
        side=OrderSide.BUY,
        tx_hash="fake_hash",
        filled_amount_in=TokenAmount.from_ui(0.1, decimals=9),
        filled_amount_out=TokenAmount.from_ui(1000, decimals=6),
        effective_price=0.0001,
        fee_paid_native=0.00005,
    )
    assert receipt.ok
    assert receipt.effective_price == 0.0001


def test_execution_quote() -> None:
    token = Address("6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P")
    quote = ExecutionQuote(
        target_token=token,
        side=OrderSide.BUY,
        amount_in=TokenAmount.from_ui(1.0, decimals=9),
        expected_amount_out=TokenAmount.from_ui(1000.0, decimals=6),
        minimum_amount_out=TokenAmount.from_ui(950.0, decimals=6),
        price_impact_pct=0.5,
        route_venue="bonding_curve",
    )
    assert quote.expected_price == 0.001
    assert quote.route_venue == "bonding_curve"


def test_trailing_stop_logic() -> None:
    ts = TrailingStopState(high_water_mark=100.0, trailing_pct=10.0)

    # Price moves up: new high water mark
    ts, triggered = ts.update(120.0)
    assert not triggered
    assert ts.high_water_mark == 120.0

    # Price drops to 110 (8.33% drop): not triggered
    ts, triggered = ts.update(110.0)
    assert not triggered
    assert ts.high_water_mark == 120.0

    # Price drops to 105 (12.5% drop from 120): triggered
    ts, triggered = ts.update(105.0)
    assert triggered


def test_take_profit_ladder() -> None:
    ladder = TakeProfitLadder.from_tuples(((50.0, 0.5), (100.0, 1.0)))

    # No gain
    ladder, sell_frac = ladder.evaluate(entry_price=10.0, current_price=12.0)
    assert sell_frac == 0.0

    # 50% gain -> sell 50%
    ladder, sell_frac = ladder.evaluate(entry_price=10.0, current_price=15.0)
    assert sell_frac == 0.5
    assert 0 in ladder.executed_tier_indices

    # Same price again -> do not re-sell
    ladder, sell_frac = ladder.evaluate(entry_price=10.0, current_price=15.0)
    assert sell_frac == 0.0

    # 100% gain -> sell remaining
    ladder, sell_frac = ladder.evaluate(entry_price=10.0, current_price=21.0)
    assert sell_frac == 1.0
    assert 1 in ladder.executed_tier_indices


def test_risk_gatekeeper() -> None:
    cfg = RiskConfig(
        max_concurrent_positions=2,
        max_total_exposure_native=5.0,
        max_single_position_native=2.0,
        max_daily_loss_native=1.0,
    )
    gk = RiskGatekeeper(cfg)

    # Allowed
    allowed, err = gk.can_open_position(0, 0.0, 1.5, 0.0)
    assert allowed
    assert err is None

    # Max concurrent
    allowed, err = gk.can_open_position(2, 2.0, 1.0, 0.0)
    assert not allowed
    assert "Max concurrent" in err

    # Single position exceeded
    allowed, err = gk.can_open_position(0, 0.0, 2.5, 0.0)
    assert not allowed
    assert "exceeds max position" in err

    # Total exposure exceeded
    allowed, err = gk.can_open_position(1, 4.0, 1.5, 0.0)
    assert not allowed
    assert "exceed max capital" in err

    # Daily loss exceeded
    allowed, err = gk.can_open_position(0, 0.0, 1.0, 1.2)
    assert not allowed
    assert "Daily loss limit" in err
