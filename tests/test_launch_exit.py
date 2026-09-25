"""Unit tests for automated launch exit controller."""

from solders.keypair import Keypair

from rugbot.core.decision.take_profit import TakeProfitLadder
from rugbot.execution.launch.exit_controller import (
    ExitAction,
    LaunchPositionState,
    build_launch_sell_instructions,
)


def test_take_profit_ladder_trigger():
    """Verify tiered take profit trigger on price appreciation."""
    state = LaunchPositionState(
        mint="4k3Dyjzvzp8eMZWUXbBCjEvwSkkk59S5iCNLY3QrkX6R",
        entry_price_sol=0.00001,
        total_tokens=1_000_000,
        remaining_tokens=1_000_000,
        high_water_mark_price=0.00001,
        created_at_timestamp=1000.0,
        tp_ladder=TakeProfitLadder.from_tuples(((100.0, 0.50), (400.0, 0.25))),
        external_buyers_seen=1,
    )

    # 1. Price doubles (+100% -> 2x)
    signal = state.evaluate(current_price_sol=0.00002, now=1010.0)
    assert signal.action == ExitAction.TAKE_PROFIT
    assert signal.sell_tokens == 500_000  # 50% of 1_000_000
    state.remaining_tokens -= signal.sell_tokens

    # 2. Subsequent check at same price should HOLD (tier already executed)
    signal_hold = state.evaluate(current_price_sol=0.00002, now=1015.0)
    assert signal_hold.action == ExitAction.HOLD


def test_trailing_stop_trigger():
    """Verify trailing stop triggers after high-water mark drawdown."""
    state = LaunchPositionState(
        mint="4k3Dyjzvzp8eMZWUXbBCjEvwSkkk59S5iCNLY3QrkX6R",
        entry_price_sol=0.00001,
        total_tokens=1_000_000,
        remaining_tokens=1_000_000,
        high_water_mark_price=0.00001,
        created_at_timestamp=1000.0,
        tp_ladder=TakeProfitLadder.from_tuples(((100.0, 0.50),)),
        trailing_stop_pct=15.0,
        external_buyers_seen=5,
    )

    # Price pumps +50% to 0.000015 (establishing high water mark)
    state.evaluate(current_price_sol=0.000015, now=1010.0)
    assert state.high_water_mark_price == 0.000015

    # Price drops by 20% to 0.000012 (> 15% trailing stop threshold)
    signal = state.evaluate(current_price_sol=0.000012, now=1020.0)
    assert signal.action == ExitAction.TRAILING_STOP
    assert signal.sell_tokens == 1_000_000


def test_dead_launch_timeout():
    """Verify dead-launch auto-refund timeout triggers if 0 external buyers arrive."""
    state = LaunchPositionState(
        mint="4k3Dyjzvzp8eMZWUXbBCjEvwSkkk59S5iCNLY3QrkX6R",
        entry_price_sol=0.00001,
        total_tokens=500_000,
        remaining_tokens=500_000,
        high_water_mark_price=0.00001,
        created_at_timestamp=1000.0,
        tp_ladder=TakeProfitLadder.from_tuples(((100.0, 0.50),)),
        dead_launch_timeout_seconds=45.0,
        external_buyers_seen=0,
    )

    # Within timeout (e.g. 20s elapsed): should HOLD
    assert (
        state.evaluate(current_price_sol=0.00001, now=1020.0).action == ExitAction.HOLD
    )

    # Exceeded timeout (e.g. 50s elapsed) with 0 buyers: should dump remaining tokens to recover SOL
    signal = state.evaluate(current_price_sol=0.00001, now=1050.0)
    assert signal.action == ExitAction.DEAD_LAUNCH_TIMEOUT
    assert signal.sell_tokens == 500_000


def test_build_launch_sell_instructions():
    """Verify sell_v2 instruction construction for position exits."""
    payer = Keypair()
    mint = Keypair().pubkey()
    ixs = build_launch_sell_instructions(
        payer=payer,
        mint=mint,
        tokens_to_sell=500_000,
        min_sol_out_lamports=1,
    )
    assert len(ixs) == 1
    # sell_v2 instruction has 26 accounts
    assert len(ixs[0].accounts) == 26
