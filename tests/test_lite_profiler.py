"""Pure unit coverage for the lite launch profiler (no network)."""

from __future__ import annotations

from decimal import Decimal

from rugbot.decision.lite_profiler import profile_launches
from rugbot.interfaces.cli.wallet import _format_usd_compact


def test_empty_candles_are_skipped() -> None:
    """Empty or invalid candle lists abstain without counting a launch."""
    report = profile_launches(
        {"mint-a": [], "mint-b": [{"open": "0", "high": "5"}]},
        [1_000_000],
    )
    assert report.launch_count == 0
    assert report.skipped_count == 2
    assert report.per_tp[0].hits == 0
    assert report.per_tp[0].winrate_ppm == 0
    assert report.optimal_tp is None


def test_entry_uses_first_close_not_open() -> None:
    """Entry is the first close; the open curve-start constant is ignored."""
    report = profile_launches(
        {"mint-a": [{"open": "1.0", "high": "3.0", "close": "2.0"}]},
        [1_000_000],
        fee_bps=0,
    )
    assert report.launch_count == 1
    assert report.launches[0].entry_price_sol == Decimal("2.0")
    assert report.launches[0].hits == (False,)


def test_single_mint_ath_math() -> None:
    """A 3x ATH hits the 2x TP with full winrate and exact net EV."""
    report = profile_launches(
        {"mint-a": [{"open": "1.0", "high": "3.0", "close": "1.0"}]},
        [1_000_000],
        fee_bps=0,
    )
    assert report.launch_count == 1
    assert report.skipped_count == 0
    assert report.launches[0].hits == (True,)
    estimate = report.per_tp[0]
    assert (estimate.hits, estimate.winrate_ppm) == (1, 1_000_000)
    assert estimate.ev_net_ppm == 1_000_000
    optimal = report.optimal_tp
    assert optimal is not None
    assert (optimal.tp_multiple, optimal.ev_multiple) == (3.0, 3.0)
    assert optimal.winrate_pct == 100.0
    assert optimal.qualifies is False


def test_optimal_tp_picks_floor_constrained_not_moonshot() -> None:
    """Optimum respects the 70% floor: 1.2x wins over the 9x moonshot."""
    candles = {
        "mint-1.2x": [{"open": "1.0", "high": "1.2", "close": "1.0"}],
        "mint-2.4x": [{"open": "1.0", "high": "2.4", "close": "1.0"}],
        "mint-9.0x": [{"open": "1.0", "high": "9.0", "close": "1.0"}],
    }
    report = profile_launches(candles, fee_bps=0)
    assert report.launch_count == 3
    optimal = report.optimal_tp
    assert optimal is not None
    assert optimal.tp_multiple == 1.2
    assert optimal.winrate_pct == 100.0
    assert optimal.ev_multiple == 1.2
    assert optimal.launch_count == 3
    assert optimal.qualifies is False


def test_optimal_tp_empty_is_none() -> None:
    """Empty input scores nothing and yields a documented none optimal."""
    report = profile_launches({}, [])
    assert report.launch_count == 0
    assert report.skipped_count == 0
    assert report.optimal_tp is None


def test_ath_stats_three_mints_and_empty() -> None:
    """ATH avg/max/min/median cover scored launches; empty input is zeros."""
    candles = {
        "mint-1.2x": [{"open": "1.0", "high": "1.2", "close": "1.0"}],
        "mint-2.4x": [{"open": "1.0", "high": "2.4", "close": "1.0"}],
        "mint-9.0x": [{"open": "1.0", "high": "9.0", "close": "1.0"}],
    }
    report = profile_launches(candles, [1_000_000], fee_bps=0)
    assert report.launch_count == 3
    assert report.ath_avg == (1.2 + 2.4 + 9.0) / 3
    assert report.ath_max == 9.0
    assert report.ath_min == 1.2
    assert report.ath_median == 2.4
    assert report.optimal_tp is not None
    assert report.optimal_tp.tp_multiple == 1.2
    skipped = profile_launches({"mint-a": []}, [1_000_000])
    assert skipped.launch_count == 0
    assert (skipped.ath_avg, skipped.ath_max) == (0.0, 0.0)
    assert (skipped.ath_min, skipped.ath_median) == (0.0, 0.0)


def test_mcap_sol_stats_three_mints_and_empty() -> None:
    """SOL mcap stats cover scored launches with mcap input; empty is zeros."""
    candles = {
        "mint-a": [{"open": "1.0", "high": "1.2", "close": "1.0"}],
        "mint-b": [{"open": "1.0", "high": "2.4", "close": "2.0"}],
        "mint-c": [{"open": "1.0", "high": "9.0", "close": "3.0"}],
        "mint-no-mcap": [{"open": "1.0", "high": "5.0", "close": "1.0"}],
    }
    mcap = {
        "mint-a": (10.0, 12.0),
        "mint-b": (20.0, 48.0),
        "mint-c": (30.0, 270.0),
    }
    report = profile_launches(candles, [1_000_000], fee_bps=0, mcap_sol_by_mint=mcap)
    assert report.launch_count == 4
    assert report.mcap_scored_count == 3
    assert report.entry_mcap_sol_avg == 20.0
    assert report.entry_mcap_sol_min < report.entry_mcap_sol_avg
    assert report.entry_mcap_sol_avg < report.entry_mcap_sol_max
    assert (report.entry_mcap_sol_min, report.entry_mcap_sol_max) == (10.0, 30.0)
    assert report.ath_mcap_sol_avg == (12.0 + 48.0 + 270.0) / 3
    assert report.ath_mcap_sol_median == 48.0
    assert report.ath_mcap_sol_max == 270.0
    assert report.ath_mcap_sol_min == 12.0
    assert report.launches[3].ath_mcap_sol == 0.0
    empty = profile_launches({"mint-a": []}, [1_000_000])
    assert empty.mcap_scored_count == 0
    assert empty.optimal_tp is None
    assert (empty.entry_mcap_sol_avg, empty.ath_mcap_sol_avg) == (0.0, 0.0)
    assert (empty.entry_mcap_sol_min, empty.entry_mcap_sol_max) == (0.0, 0.0)
    assert (empty.ath_mcap_sol_median, empty.ath_mcap_sol_max) == (0.0, 0.0)
    assert empty.ath_mcap_sol_min == 0.0


def test_format_usd_compact() -> None:
    """USD helper compacts thousands to $K with sensible small values."""
    assert _format_usd_compact(3500.0) == "$3.5K"
    assert _format_usd_compact(10500.0) == "$10.5K"
    assert _format_usd_compact(999.0) == "$999.00"
    assert _format_usd_compact(0.5) == "$0.5000"
