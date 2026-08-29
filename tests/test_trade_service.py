"""Unit and integration tests for the unified TradingService SDK and order specs."""

from __future__ import annotations

import pytest

from rugbot.execution.ports import ExecutionMode
from rugbot.execution.trade_service import (
    ActivePosition,
    BuyOrderSpec,
    SellOrderSpec,
    TradeResult,
    TradeSide,
    TradingService,
)

VALID_MINT = "279mMFSUjS2kg4S3yQwwv3zZBqCtZ1Quvmg8FUHYpump"


def test_buy_order_spec_validation() -> None:
    """Test BuyOrderSpec input validation and calculations."""
    spec = BuyOrderSpec(
        mint=VALID_MINT,
        amount_sol=0.25,
        slippage_pct=5.0,
        priority_fee_sol=0.001,
        jito_tip_sol=0.002,
        take_profit_pct=50.0,
        stop_loss_pct=20.0,
    )
    spec.validate()

    assert spec.quote_lamports == 250_000_000
    assert spec.max_slippage_bps == 500
    assert spec.priority_fee_microlamports == 1_000_000_000
    assert spec.jito_tip_lamports == 2_000_000
    assert spec.take_profit_pnl_ppm == 500_000
    assert spec.stop_loss_pnl_ppm == -200_000


def test_buy_order_spec_invalid_inputs() -> None:
    """Test BuyOrderSpec rejects invalid inputs."""
    with pytest.raises(ValueError, match="amount_sol must be positive"):
        BuyOrderSpec(mint=VALID_MINT, amount_sol=0.0).validate()

    with pytest.raises(ValueError, match="slippage_pct must be between"):
        BuyOrderSpec(mint=VALID_MINT, amount_sol=0.1, slippage_pct=150.0).validate()

    with pytest.raises(ValueError, match="valid Solana address|invalid mint address"):
        BuyOrderSpec(mint="not-a-valid-solana-key", amount_sol=0.1).validate()


def test_sell_order_spec_validation() -> None:
    """Test SellOrderSpec input validation."""
    spec = SellOrderSpec(
        mint=VALID_MINT,
        percent=50.0,
        slippage_pct=10.0,
    )
    spec.validate()
    assert spec.max_slippage_bps == 1000

    with pytest.raises(ValueError, match="percent must be between"):
        SellOrderSpec(mint=VALID_MINT, percent=150.0).validate()


@pytest.mark.anyio
async def test_trading_service_paper_lifecycle() -> None:
    """Test full buy -> position -> sell lifecycle in paper execution mode."""
    service = TradingService(default_mode=ExecutionMode.PAPER)

    # 1. Execute Buy
    buy_result = await service.buy(
        mint=VALID_MINT,
        amount_sol=0.5,
        take_profit_pct=100.0,
        stop_loss_pct=30.0,
    )
    assert buy_result.ok is True
    assert buy_result.side == TradeSide.BUY
    assert buy_result.mint == VALID_MINT
    assert buy_result.sol_amount == 0.5
    assert buy_result.token_amount > 0
    assert buy_result.take_profit_pct == 100.0
    assert buy_result.stop_loss_pct == 30.0

    # 2. Check Positions
    positions = service.get_positions()
    assert len(positions) == 1
    assert positions[0]["mint"] == VALID_MINT
    assert positions[0]["take_profit_pct"] == 100.0

    # 3. Execute Partial Sell (50%)
    initial_tokens = positions[0]["token_amount"]
    sell_result_50 = await service.sell(
        mint=VALID_MINT,
        percent=50.0,
    )
    assert sell_result_50.ok is True
    assert sell_result_50.side == TradeSide.SELL
    assert sell_result_50.token_amount == initial_tokens // 2

    # Verify updated position
    pos = service.get_position(VALID_MINT)
    assert pos is not None
    assert pos.token_amount == initial_tokens - (initial_tokens // 2)

    # 4. Execute Full Sell (100%)
    sell_result_100 = await service.sell(
        mint=VALID_MINT,
        percent=100.0,
    )
    assert sell_result_100.ok is True
    assert service.get_position(VALID_MINT) is None
    assert len(service.get_positions()) == 0
