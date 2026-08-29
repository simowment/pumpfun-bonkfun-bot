"""Unit & integration tests for copytrade backtesting runner and CLI."""

from __future__ import annotations

from rugbot.backtest.runners.copytrade_backtest_runner import (
    CopytradeBacktestConfig,
    CopytradeSample,
    _eval_copytrade_single_sample,
    run_copytrade_tp_sl_grid_search,
)
from rugbot.interfaces.cli.backtest import main as backtest_cli_main


def test_copytrade_single_sample_tp_hit() -> None:
    sample = CopytradeSample(
        mint="TestMint111111111111111111111111111111111111",
        wallet="TestWallet111111111111111111111111111111111",
        buy_slot=100,
        buy_timestamp=1000,
        buy_sol=0.5,
        buy_tokens=5000000.0,
        buy_price_ppm=1_000_000,
        sell_slot=150,
        sell_timestamp=1050,
        sell_sol=1.0,
        sell_tokens=5000000.0,
        sell_price_ppm=2_000_000,
        trajectory=((0.0, 1.0), (10.0, 1.3), (25.0, 1.8), (50.0, 2.0)),
        peak_multiplier=2.0,
        target_hold_seconds=50.0,
        target_pnl_pct=100.0,
    )

    config = CopytradeBacktestConfig(
        quote_size_sol=0.1,
        copy_lag_slots=1,
        copy_entry_slippage_pct=1.0,
        mirror_target_sells=True,
    )

    # TP at +50% should hit since peak reached 1.8x
    gross, fees, net, is_win = _eval_copytrade_single_sample(
        sample, tp_pct=50.0, sl_pct=20.0, config=config
    )
    assert is_win is True
    assert gross > 0
    assert net > 0


def test_copytrade_lag_drag_penalty() -> None:
    sample = CopytradeSample(
        mint="TestMint222222222222222222222222222222222222",
        wallet="TestWallet222222222222222222222222222222222",
        buy_slot=100,
        buy_timestamp=1000,
        buy_sol=0.5,
        buy_tokens=5000000.0,
        buy_price_ppm=1_000_000,
        sell_slot=110,
        sell_timestamp=1010,
        sell_sol=0.51,  # tight scalp +2%
        sell_tokens=5000000.0,
        sell_price_ppm=1_020_000,
        trajectory=((0.0, 1.0), (5.0, 1.02), (10.0, 0.98)),
        peak_multiplier=1.02,
        target_hold_seconds=10.0,
        target_pnl_pct=2.0,
    )

    # 0 lag vs 5 slots lag
    cfg_fast = CopytradeBacktestConfig(
        quote_size_sol=0.1, copy_lag_slots=0, copy_entry_slippage_pct=0.0
    )
    cfg_slow = CopytradeBacktestConfig(
        quote_size_sol=0.1, copy_lag_slots=5, copy_entry_slippage_pct=2.0
    )

    _, _, net_fast, _ = _eval_copytrade_single_sample(
        sample, tp_pct=15.0, sl_pct=10.0, config=cfg_fast
    )
    _, _, net_slow, _ = _eval_copytrade_single_sample(
        sample, tp_pct=15.0, sl_pct=10.0, config=cfg_slow
    )

    assert net_fast > net_slow


def test_copytrade_grid_search_report() -> None:
    samples = [
        CopytradeSample(
            mint=f"Mint{i}11111111111111111111111111111111111111",
            wallet="TraderWallet11111111111111111111111111111",
            buy_slot=100 + i * 100,
            buy_timestamp=1000 + i * 60,
            buy_sol=0.2,
            buy_tokens=1000000.0,
            buy_price_ppm=1_000_000,
            sell_slot=120 + i * 100,
            sell_timestamp=1020 + i * 60,
            sell_sol=0.3 if i % 2 == 0 else 0.1,
            sell_tokens=1000000.0,
            sell_price_ppm=1_500_000 if i % 2 == 0 else 500_000,
            trajectory=((0.0, 1.0), (10.0, 1.5 if i % 2 == 0 else 0.6)),
            peak_multiplier=1.5 if i % 2 == 0 else 1.0,
            target_hold_seconds=20.0,
            target_pnl_pct=50.0 if i % 2 == 0 else -50.0,
        )
        for i in range(10)
    ]

    config = CopytradeBacktestConfig(
        quote_size_sol=0.1,
        tp_grid=(25.0, 50.0),
        sl_grid=(20.0, 30.0),
    )

    report = run_copytrade_tp_sl_grid_search(
        samples, config, target="TraderWallet11111111111111111111111111111"
    )
    assert report.insufficient_data is False
    assert len(report.evaluations) == 4
    assert report.optimal_tp is not None
    assert report.optimal_sl is not None


def test_cli_copytrade_invocation_json(capsys) -> None:
    code = backtest_cli_main(
        ["FakeWallet11111111111111111111111111111111", "--mode", "copytrade", "--json"]
    )
    assert code == 1
    out = capsys.readouterr().out
    assert '"status": "abstain"' in out
