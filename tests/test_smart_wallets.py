"""Pure-logic tests for cross-launch wallet scoring."""

from rugbot.backtest.launch_replay import LaunchTrade
from rugbot.discover.smart_wallets import score_wallets, wallet_launches

CREATE_SLOT = 100


def _trade(slot: int, wallet: str, *, buy: bool, sol: float) -> LaunchTrade:
    return LaunchTrade(
        slot=slot,
        timestamp_s=float(slot),
        wallet=wallet,
        is_buy=buy,
        price_sol=3e-8,
        amount_sol=sol,
        on_curve=True,
    )


def _launch(mint: str, *, steady_wins: bool, lucky_wins: bool) -> dict:
    trades = [
        _trade(CREATE_SLOT, "dev", buy=True, sol=1.0),
        _trade(CREATE_SLOT + 1, "steady", buy=True, sol=0.5),
        _trade(CREATE_SLOT + 9, "lucky", buy=True, sol=0.5),
        _trade(CREATE_SLOT + 20, "steady", buy=False, sol=0.8 if steady_wins else 0.2),
        _trade(CREATE_SLOT + 21, "lucky", buy=False, sol=5.0 if lucky_wins else 0.1),
    ]
    return wallet_launches(mint, trades)


def test_wallet_launch_measures_delay_and_realized_pnl() -> None:
    steady = _launch("m", steady_wins=True, lucky_wins=False)["steady"]
    assert steady.entry_delay_slots == 1
    assert steady.realized_pnl_sol == 0.8 - 0.5


def test_consistent_wallet_outranks_outlier_wallet() -> None:
    launches = [
        _launch(f"m{i}", steady_wins=i != 0, lucky_wins=i == 0) for i in range(10)
    ]
    scores = score_wallets(launches, min_launches=5)
    assert [score.wallet for score in scores][:2] == ["steady", "lucky"]
    lucky = next(score for score in scores if score.wallet == "lucky")
    assert lucky.total_pnl_sol > 0  # one 10x covers nine losses
    assert scores[0].winrate_floor > lucky.winrate_floor
    assert scores[0].early_share == 1.0
