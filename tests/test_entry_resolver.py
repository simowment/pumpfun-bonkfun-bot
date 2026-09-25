"""Pure tests for tiered entry resolution (no network)."""

from __future__ import annotations

from rugbot.backtest.runners import entry_resolver as entry_resolver_module
from rugbot.backtest.runners.entry_resolver import (
    build_entry_sample,
    trajectory_from_1s_candles,
    trajectory_from_early_trades,
)


def test_1s_entry_skips_bundle_candle_and_ath() -> None:
    """Entry is the first achievable second; ATH covers entry-onwards."""
    candles = [
        {
            "timestamp": 1_000_000,
            "open": "1.0",
            "high": "1.1",
            "low": "0.9",
            "close": "1.0",
            "volume": 10,
        },
        {
            "timestamp": 1_001_000,
            "open": "1.0",
            "high": "2.0",
            "low": "0.8",
            "close": "1.5",
            "volume": 5,
        },
    ]
    # created 1s before the first candle: threshold lands on candles[0].
    traj, ath = trajectory_from_1s_candles(candles, created_ms=999_000)
    assert traj[0] == (1.0, 0.8 / 1.0)
    assert traj[1] == (1.0, 2.0 / 1.0)
    assert len(traj) == 2
    assert ath == 2.0


def test_1s_bundle_candle_is_not_the_entry() -> None:
    """Creation/bundle fills are the dev's price; outsiders enter at +1s.

    Mirrors 7hwCFYaDGrTPCREwgsEs6ATc7BZDq5XVWmJp1ouxpump: the creation
    candle close (2.8889e-6) implies a 5.42x ATH, while the achievable
    +1s close (7.0913e-6) implies only ~2.21x.
    """
    t0 = 1_760_000_000_000
    bundle_close = 2.8889e-6
    entry_close = 7.0913e-6
    peak_high = 1.5660e-5
    candles = [
        {
            "timestamp": t0,
            "open": "2.7961e-6",
            "high": "7.1114e-6",
            "low": "2.7961e-6",
            "close": str(bundle_close),
            "volume": 1784.0,
        },
        {
            "timestamp": t0 + 1_000,
            "open": "2.8889e-6",
            "high": "7.1211e-6",
            "low": "2.8889e-6",
            "close": str(entry_close),
            "volume": 17.0,
        },
        {
            "timestamp": t0 + 2_000,
            "open": "7.0913e-6",
            "high": "8.0e-6",
            "low": "6.5e-6",
            "close": "7.5e-6",
            "volume": 9.0,
        },
        {
            "timestamp": t0 + 3_000,
            "open": "7.5e-6",
            "high": str(peak_high),
            "low": "7.0e-6",
            "close": "1.4e-5",
            "volume": 11.0,
        },
    ]
    traj, ath = trajectory_from_1s_candles(candles, created_ms=t0 - 200)
    assert ath is not None
    # Achievable ATH ~2.21x, not the dev-entry artifact ~5.42x.
    assert abs(ath - peak_high / entry_close) < 1e-6
    assert abs(ath - peak_high / bundle_close) > 1.0
    # No point is emitted for the entry candle itself (no sec=0).
    assert traj
    assert all(sec > 0 for sec, _ in traj)
    assert min(sec for sec, _ in traj) == 1.0
    # The bundle/entry-candle lows predate the fill: no false stop.
    bundle_low_mult = 2.7961e-6 / entry_close
    assert all(abs(mult - bundle_low_mult) > 1e-6 for _, mult in traj)
    assert min(mult for _, mult in traj) > 0.7


def test_1s_defaults_to_second_candle_without_created_ms() -> None:
    """created_ms=None falls back to candles[1]; <2 candles abstains."""
    candles = [
        {
            "timestamp": 1_000_000,
            "open": "1.0",
            "high": "1.1",
            "low": "0.9",
            "close": "1.0",
            "volume": 10,
        },
        {
            "timestamp": 1_001_000,
            "open": "1.0",
            "high": "1.6",
            "low": "1.4",
            "close": "1.5",
            "volume": 5,
        },
        {
            "timestamp": 1_002_000,
            "open": "1.5",
            "high": "3.0",
            "low": "1.2",
            "close": "2.0",
            "volume": 6,
        },
    ]
    traj, ath = trajectory_from_1s_candles(candles)
    assert traj == ((1.0, 1.2 / 1.5), (1.0, 3.0 / 1.5))
    assert ath == 3.0 / 1.5
    solo = [candles[0]]
    assert trajectory_from_1s_candles(solo) == ((), None)


def test_1s_pessimistic_sl_before_tp_in_one_candle() -> None:
    """Low point precedes high point at the same second (SL first)."""
    candles = [
        {
            "timestamp": 5_000,
            "open": "1.0",
            "high": "1.0",
            "low": "1.0",
            "close": "1.0",
            "volume": 7,
        },
        {
            "timestamp": 6_000,
            "open": "1.0",
            "high": "3.0",
            "low": "0.5",
            "close": "2.0",
            "volume": 4,
        },
    ]
    traj, ath = trajectory_from_1s_candles(candles, created_ms=4_000)
    assert traj == ((1.0, 0.5), (1.0, 3.0))
    assert ath == 3.0


def test_1s_skips_bad_prices() -> None:
    """Unparsable candles are skipped; empty input yields ((), None)."""
    assert trajectory_from_1s_candles([]) == ((), None)
    bad = [{"timestamp": 1, "close": "0", "high": "x", "low": "-1"}]
    assert trajectory_from_1s_candles(bad) == ((), None)


def test_early_trades_entry_and_timing() -> None:
    """Entry is first trade price; seconds scale by slot delta x0.4."""
    trades = [
        {"slot": 100, "side": "buy", "price_ppm": 1_000, "signature": "a"},
        {"slot": 105, "side": "sell", "price_ppm": 2_000, "signature": "b"},
    ]
    traj, ath = trajectory_from_early_trades(trades)
    assert traj == ((0.0, 1.0), (2.0, 2.0))
    assert ath == 2.0
    assert trajectory_from_early_trades([]) == ((), None)


def test_builder_tier1_uses_1s_interval_only() -> None:
    """Tier-1 fetch uses interval='1s'; coarser grains never define entry."""
    seen: list[str] = []

    class StubClient:
        def fetch_candlesticks(self, mint: str, **kwargs: object) -> list[dict]:
            seen.append(str(kwargs.get("interval")))
            return [
                {
                    "timestamp": 1_000_000,
                    "open": "1.0",
                    "high": "1.1",
                    "low": "0.9",
                    "close": "1.0",
                    "volume": 1784.0,
                },
                {
                    "timestamp": 1_001_000,
                    "open": "1.0",
                    "high": "2.2",
                    "low": "1.8",
                    "close": "2.0",
                    "volume": 9,
                },
                {
                    "timestamp": 1_002_000,
                    "open": "2.0",
                    "high": "2.4",
                    "low": "1.6",
                    "close": "2.1",
                    "volume": 5,
                },
            ]

    sample = build_entry_sample(
        "MINT",
        creator="CREATOR",
        created_at=1000,
        created_slot=1,
        client=StubClient(),
    )
    assert sample is not None
    assert seen == ["1s"]
    assert sample.entry_basis == "1s"
    # Entry is the +1s close (2.0), not the bundle close (1.0).
    assert sample.trajectory[0] == (1.0, 1.6 / 2.0)


def test_builder_tier2_fallback_and_none_excluded() -> None:
    """Empty 1s window falls back to on-chain; total miss returns None."""
    er = entry_resolver_module

    class EmptyClient:
        def fetch_candlesticks(self, mint: str, **kwargs: object) -> list[dict]:
            assert kwargs.get("interval") == "1s"
            return []

    orig = (
        er.fetch_early_launch_trades
        if hasattr(er, "fetch_early_launch_trades")
        else None
    )
    monkey_trades = [
        {"slot": 10, "side": "buy", "price_ppm": 500, "signature": "s1"},
        {"slot": 12, "side": "buy", "price_ppm": 1_000, "signature": "s2"},
    ]
    er.fetch_early_launch_trades = lambda mint, rpc_url=None: monkey_trades  # type: ignore[assignment]
    try:
        sample = build_entry_sample(
            "MINT2",
            creator="C",
            created_at=0,
            created_slot=0,
            client=EmptyClient(),
        )
        assert sample is not None
        assert sample.entry_basis == "onchain_early"
        assert sample.trajectory[0] == (0.0, 1.0)
    finally:
        if orig is not None:
            er.fetch_early_launch_trades = orig  # type: ignore[assignment]

    er.fetch_early_launch_trades = lambda mint, rpc_url=None: None  # type: ignore[assignment]
    try:
        assert (
            build_entry_sample(
                "MINT3",
                creator="C",
                created_at=0,
                created_slot=0,
                client=EmptyClient(),
            )
            is None
        )
    finally:
        if orig is not None:
            er.fetch_early_launch_trades = orig  # type: ignore[assignment]
