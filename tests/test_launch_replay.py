"""Pure-logic tests for the event-driven launch replay."""

import pytest

from rugbot.backtest.launch_replay import (
    EXIT_DEV_SELL,
    EXIT_STOP_LOSS,
    EXIT_TAKE_PROFIT,
    INITIAL_VIRTUAL_BASE,
    INITIAL_VIRTUAL_QUOTE,
    ExitRule,
    LaunchReplay,
    LaunchReplayError,
    ReplayCosts,
    _virtual_reserves,
    market_cap_sol,
    summarize_rules,
    trades_from_swap_api,
)

DEV = "dev"
BUNDLER = "bundler"
CREATE_SLOT = 1000
LAUNCH_PRICE = 30 / 1_073_000_000  # 30 SOL virtual / 1.073B tokens


def _raw(slot: int, second: int, wallet: str, side: str, mc_sol: float) -> dict:
    return {
        "slotIndexId": f"{slot:012d}0000000000",
        "timestamp": f"2026-09-25T06:00:{second:02d}.000Z",
        "userAddress": wallet,
        "type": side,
        "priceSol": str(mc_sol / 1_000_000_000),
        "amountSol": "1",
        "program": "pump",
    }


def _launch() -> list:
    return trades_from_swap_api(
        [
            _raw(CREATE_SLOT, 0, DEV, "buy", 35),
            _raw(CREATE_SLOT, 0, BUNDLER, "buy", 45),
            _raw(CREATE_SLOT + 2, 1, "a", "buy", 48),
            _raw(CREATE_SLOT + 3, 1, DEV, "sell", 44),
            _raw(CREATE_SLOT + 6, 3, "b", "buy", 90),
            _raw(CREATE_SLOT + 7, 4, "c", "buy", 110),
            _raw(CREATE_SLOT + 20, 9, "d", "sell", 20),
        ]
    )


def _replay(**costs: float) -> LaunchReplay:
    return LaunchReplay(
        "mint",
        create_slot=CREATE_SLOT,
        creator=DEV,
        trades=_launch(),
        costs=ReplayCosts(**costs),
    )


def test_reserves_round_trip_launch_state() -> None:
    quote, base = _virtual_reserves(LAUNCH_PRICE)
    assert abs(quote - INITIAL_VIRTUAL_QUOTE) / INITIAL_VIRTUAL_QUOTE < 1e-6
    assert abs(base - INITIAL_VIRTUAL_BASE) / INITIAL_VIRTUAL_BASE < 1e-6


def test_entry_lands_at_end_of_delay_slot() -> None:
    replay = _replay(entry_delay_slots=2)
    assert replay.profile.entry_mc_sol == pytest.approx(48)
    assert replay.profile.first_insider_sell_s == 1


def test_take_profit_fills_after_reaction_not_at_trigger() -> None:
    result = _replay(reaction_slots=1).run(ExitRule(50, None, False, None))
    assert result.exit_reason == EXIT_TAKE_PROFIT
    assert result.exit_mc_sol == pytest.approx(110)  # trigger at 90, lands at +1 slot


def test_dev_sell_exit_uses_creator_and_block0_buyers() -> None:
    result = _replay().run(ExitRule(None, None, True, None))
    assert result.exit_reason == EXIT_DEV_SELL


def test_stop_fills_at_gap_price() -> None:
    result = _replay().run(ExitRule(1000, 30, False, None))
    assert result.exit_reason == EXIT_STOP_LOSS
    assert result.exit_mc_sol == pytest.approx(20)
    assert result.net_pnl_sol < -0.05


def test_summaries_rank_by_net_ev() -> None:
    rules = [ExitRule(50, None, False, None), ExitRule(None, None, True, None)]
    summaries = summarize_rules([_replay()], rules)
    assert summaries[0].rule.take_profit_pct == 50
    assert summaries[0].net_ev_sol > summaries[1].net_ev_sol


def test_no_trading_after_entry_is_rejected() -> None:
    with pytest.raises(LaunchReplayError):
        LaunchReplay(
            "mint",
            create_slot=CREATE_SLOT,
            creator=DEV,
            trades=_launch()[:1],
            costs=ReplayCosts(),
        )


def test_market_cap_uses_one_billion_supply() -> None:
    assert market_cap_sol(LAUNCH_PRICE) == pytest.approx(27.96, abs=0.01)
