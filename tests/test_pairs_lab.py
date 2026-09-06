"""Tests for the pairs lab alpha-extraction harness (paper-only labels)."""

from __future__ import annotations

import json

import pytest

from rugbot.backtest.pairs_lab import (
    LaunchLabel,
    PairsLabConfig,
    PreEntryFeatures,
    bucket_lifts,
    extract_pre_entry_features,
    replay_launch_label,
    run_pairs_lab,
    wilson_interval,
)
from rugbot.discover.store import ensure_discover_schema, upsert_launch, upsert_trade
from rugbot.interfaces.cli.pairs_lab import main as pairs_lab_main
from rugbot.storage.database import DatabaseManager

CREATED_SLOT = 1_000


def _trade(
    slot: int,
    side: str,
    price_ppm: int,
    wallet: str = "w",
    quote: int = 1_000_000_000,
) -> dict[str, object]:
    return {
        "mint": "MINT",
        "signature": f"sig-{slot}-{side}-{wallet}",
        "event_index": 0,
        "slot": slot,
        "tx_index": 0,
        "wallet": wallet,
        "side": side,
        "quote_amount_base_units": quote,
        "base_amount": int(quote * 1_000_000 / price_ppm),
        "price_ppm": price_ppm,
    }


def _launch(mint: str = "MINT", creator: str = "DEV") -> dict[str, object]:
    return {
        "mint": mint,
        "creator": creator,
        "created_signature": "create-sig",
        "created_slot": CREATED_SLOT,
    }


class TestWilsonInterval:
    def test_bounds_contain_proportion_and_shrink_with_n(self) -> None:
        lo10, hi10 = wilson_interval(1, 10)
        lo1000, hi1000 = wilson_interval(100, 1000)
        assert lo10 < 0.1 < hi10
        assert lo1000 < 0.1 < hi1000
        assert (hi10 - lo10) > (hi1000 - lo1000)
        assert 0.0 <= lo1000 and hi1000 <= 1.0

    def test_requires_positive_n(self) -> None:
        with pytest.raises(ValueError, match="n > 0"):
            wilson_interval(0, 0)


class TestFeatureExtraction:
    def test_counts_ratios_and_prices(self) -> None:
        config = PairsLabConfig()
        trades = [
            _trade(CREATED_SLOT, "buy", 100, wallet="A", quote=2_000_000_000),
            _trade(CREATED_SLOT + 1, "buy", 110, wallet="A", quote=1_000_000_000),
            _trade(CREATED_SLOT + 2, "buy", 120, wallet="B", quote=1_000_000_000),
            _trade(CREATED_SLOT + 3, "sell", 115, wallet="B", quote=500_000_000),
            _trade(CREATED_SLOT + 20, "buy", 130, wallet="DEV", quote=500_000_000),
            _trade(CREATED_SLOT + 30, "buy", 140, wallet="E"),
        ]
        features = extract_pre_entry_features(
            config=config, launch=_launch(), trades=trades
        )
        assert features.n_buys == 4
        assert features.n_sells == 1
        assert features.unique_buyers == 3  # A, B, DEV
        assert features.unique_sellers == 1
        assert features.buy_volume_lamports == 4_500_000_000
        assert features.top_buyer_share == pytest.approx(2 / 3)
        assert features.dev_buys == 1
        assert features.same_slot_buys == 1
        assert features.sniper_buyers == 2  # A, B within sniper window
        assert features.sniper_ratio == pytest.approx(2 / 3)
        assert features.first_price_ppm == 100
        assert features.last_price_ppm == 130
        assert features.return_ppm == 300_000
        assert features.range_multiple == pytest.approx(1.3)
        assert features.entry_slot_offset == 30

    def test_zero_prewindow_is_neutral_zeroes(self) -> None:
        config = PairsLabConfig()
        trades = [_trade(CREATED_SLOT + 30, "buy", 140, wallet="E")]
        features = extract_pre_entry_features(
            config=config, launch=_launch(), trades=trades
        )
        assert features.n_buys == 0
        assert features.unique_buyers == 0
        assert features.sniper_ratio == 0.0
        assert features.top_buyer_share == 0.0
        assert features.first_price_ppm == 0
        assert features.return_ppm == 0
        assert features.range_multiple == 1.0
        assert features.entry_slot_offset == 30


class TestLabelReplay:
    def test_tp_ladder_full_path(self) -> None:
        config = PairsLabConfig()
        trades = [
            _trade(CREATED_SLOT, "buy", 900, wallet="A"),
            _trade(CREATED_SLOT + 30, "buy", 1000, wallet="E"),  # entry
            _trade(CREATED_SLOT + 40, "buy", 2500, wallet="F"),  # tp1
            _trade(CREATED_SLOT + 50, "buy", 2500, wallet="F"),  # hold (pnl 150 < 400)
            _trade(CREATED_SLOT + 60, "buy", 5500, wallet="G"),  # tp2
            _trade(CREATED_SLOT + 70, "buy", 11000, wallet="H"),  # tp3 -> closed
        ]
        label = replay_launch_label(config=config, launch=_launch(), trades=trades)
        assert label is not None
        assert label.exit_reason == "tp3_900pct"
        assert label.tranche_count == 3
        assert label.is_win
        assert label.net_pnl_lamports > 0
        assert label.pnl_pct > 100.0
        assert label.peak_multiple == pytest.approx(11.0)
        assert label.touched_2x and label.touched_5x and label.touched_10x
        assert label.entry_slot_offset == 30

    def test_stop_loss_one_shot(self) -> None:
        config = PairsLabConfig()
        trades = [
            _trade(CREATED_SLOT + 30, "buy", 1000, wallet="E"),  # entry
            _trade(CREATED_SLOT + 40, "sell", 550, wallet="F"),  # pnl -45%
        ]
        label = replay_launch_label(config=config, launch=_launch(), trades=trades)
        assert label is not None
        assert label.exit_reason == "stop_loss"
        assert label.tranche_count == 1
        assert not label.is_win
        assert -50.0 < label.pnl_pct < -45.0
        assert label.peak_multiple == pytest.approx(1.0)

    def test_horizon_close_is_fee_only_loss_on_flat_path(self) -> None:
        config = PairsLabConfig()
        trades = [
            _trade(CREATED_SLOT + 30, "buy", 1000, wallet="E"),  # entry
            _trade(CREATED_SLOT + 100, "buy", 1000, wallet="F"),  # flat hold
        ]
        label = replay_launch_label(config=config, launch=_launch(), trades=trades)
        assert label is not None
        assert label.exit_reason == "horizon_close"
        assert label.tranche_count == 1
        assert not label.is_win
        # ~125 bps buy + ~125 bps sell on a flat path
        assert -4.0 < label.pnl_pct < -1.0
        assert label.touched_2x is False

    def test_partial_tp_then_horizon_close(self) -> None:
        config = PairsLabConfig()
        trades = [
            _trade(CREATED_SLOT + 30, "buy", 1000, wallet="E"),  # entry
            _trade(CREATED_SLOT + 40, "buy", 2500, wallet="F"),  # tp1 only
        ]
        label = replay_launch_label(config=config, launch=_launch(), trades=trades)
        assert label is not None
        assert label.exit_reason == "tp1_100pct+horizon_close"
        assert label.tranche_count == 2  # tp1 + forced remainder
        assert label.is_win

    def test_no_entry_window_trade_returns_none(self) -> None:
        config = PairsLabConfig()
        trades = [
            _trade(CREATED_SLOT, "buy", 100, wallet="A"),
            _trade(CREATED_SLOT + 24, "buy", 120, wallet="A"),
        ]
        label = replay_launch_label(config=config, launch=_launch(), trades=trades)
        assert label is None

    def test_invalid_entry_price_returns_none(self) -> None:
        config = PairsLabConfig()
        trade: dict[str, object] = {
            "mint": "MINT",
            "signature": "sig-bad-price",
            "event_index": 0,
            "slot": CREATED_SLOT + 30,
            "tx_index": 0,
            "wallet": "E",
            "side": "buy",
            "quote_amount_base_units": 1_000_000_000,
            "base_amount": None,  # price fallback unavailable
            "price_ppm": None,
        }
        label = replay_launch_label(config=config, launch=_launch(), trades=[trade])
        assert label is None


def _label(mint: str, *, is_win: bool) -> LaunchLabel:
    return LaunchLabel(
        mint=mint,
        entry_slot=CREATED_SLOT + 30,
        entry_slot_offset=30,
        exit_slot=CREATED_SLOT + 60,
        entry_price_ppm=1_000,
        net_pnl_lamports=1_000_000 if is_win else -1_000_000,
        pnl_pct=1.0 if is_win else -1.0,
        is_win=is_win,
        exit_reason="horizon_close",
        tranche_count=1,
        peak_multiple=1.0,
        touched_2x=False,
        touched_5x=False,
        touched_10x=False,
    )


def _features(mint: str, unique_buyers: int) -> PreEntryFeatures:
    return PreEntryFeatures(
        mint=mint,
        n_buys=unique_buyers,
        n_sells=0,
        unique_buyers=unique_buyers,
        unique_sellers=0,
        buy_volume_lamports=unique_buyers * 1_000_000_000,
        top_buyer_share=0.5,
        dev_buys=0,
        same_slot_buys=0,
        sniper_buyers=0,
        sniper_ratio=0.0,
        first_price_ppm=100,
        last_price_ppm=100,
        return_ppm=0,
        range_multiple=1.0,
        entry_slot_offset=30,
    )


class TestBucketLifts:
    def test_tercile_lift_and_suppression(self) -> None:
        # unique_buyers 1..9 -> buckets (<=4, (4,7], >7); wins only in top bucket
        pairs = [
            (_features(f"m{i}", i), _label(f"m{i}", is_win=i >= 8))
            for i in range(1, 10)
        ]
        config = PairsLabConfig(min_bucket_count=2)
        lifts = bucket_lifts(pairs, config=config)
        buckets = lifts["unique_buyers"]
        assert [b.bucket_index for b in buckets] == [1, 2, 3]
        assert buckets[0].count == 4 and buckets[1].count == 3 and buckets[2].count == 2
        assert buckets[2].winrate == 1.0
        assert buckets[0].winrate == 0.0
        assert buckets[2].lift_pp == pytest.approx(700 / 9)  # 100% - 2/9 overall
        assert buckets[2].wilson_lo <= buckets[2].winrate <= buckets[2].wilson_hi

        suppressed = bucket_lifts(pairs, config=PairsLabConfig(min_bucket_count=3))
        assert [b.bucket_index for b in suppressed["unique_buyers"]] == [1, 2]

    def test_tied_feature_collapses_to_single_bucket(self) -> None:
        pairs = [
            (_features(f"m{i}", 5), _label(f"m{i}", is_win=i % 2 == 0))
            for i in range(9)
        ]
        lifts = bucket_lifts(pairs, config=PairsLabConfig(min_bucket_count=1))
        buckets = lifts["unique_buyers"]
        assert len(buckets) == 1
        assert buckets[0].count == 9
        assert buckets[0].winrate == pytest.approx(5 / 9)  # i = 0,2,4,6,8


class TestRunPairsLab:
    def test_report_structure_and_coverage(self) -> None:
        config = PairsLabConfig(min_labels=1, min_bucket_count=1)

        def mint_trades(prefix: str) -> list[dict[str, object]]:
            return [
                {
                    **_trade(CREATED_SLOT, "buy", 900, wallet="A"),
                    "mint": prefix,
                },
                {
                    **_trade(CREATED_SLOT + 30, "buy", 1000, wallet="E"),
                    "mint": prefix,
                },
                {
                    **_trade(CREATED_SLOT + 70, "buy", 11000, wallet="H"),
                    "mint": prefix,
                },
            ]

        launches = [
            {**_launch(), "mint": "m1"},
            {**_launch(), "mint": "m2"},
            {**_launch(), "mint": "m3"},
            {**_launch(), "mint": "m_no_trades"},
        ]
        trades = mint_trades("m1") + mint_trades("m2") + mint_trades("m3")
        report = run_pairs_lab(launches=launches, trades=trades, config=config)
        assert report.coverage == {
            "launches": 4,
            "with_trades": 3,
            "entry_able": 3,
            "labeled": 3,
        }
        assert not report.insufficient_data
        assert report.message == "ok"
        assert report.base["winrate"] == 1.0
        assert report.base["p_peak_ge_10x"] == 1.0
        # one 11x print fires only TP1; the remainder horizon-closes at 11x
        assert "tp1_100pct+horizon_close" in report.exit_reasons
        assert report.feature_lifts  # min_bucket_count=1 keeps buckets
        assert len(report.labeled) == 3

    def test_fail_closed_below_min_labels(self) -> None:
        config = PairsLabConfig(min_labels=30)
        trades = [
            {
                **_trade(CREATED_SLOT + 30, "buy", 1000, wallet="E"),
                "mint": "m1",
            },
        ]
        report = run_pairs_lab(
            launches=[{**_launch(), "mint": "m1"}], trades=trades, config=config
        )
        assert report.insufficient_data
        assert report.base["labeled"] == 1  # labeled but below min_labels


class TestCli:
    def _write_db(self, tmp_path) -> None:
        db = DatabaseManager(tmp_path / "rugbot.db")
        ensure_discover_schema(db)
        try:
            for index in range(3):
                mint = f"mint{index}"
                upsert_launch(
                    db,
                    mint=mint,
                    creator=f"creator{index}",
                    created_signature=f"create{index}",
                    created_slot=CREATED_SLOT,
                )
                upsert_trade(
                    db,
                    mint=mint,
                    signature=f"{mint}-a",
                    event_index=0,
                    slot=CREATED_SLOT,
                    side="buy",
                    quote_amount_base_units=1_000_000_000,
                    base_amount=1_000_000,
                    price_ppm=1_000_000,
                    wallet="w1",
                )
                upsert_trade(
                    db,
                    mint=mint,
                    signature=f"{mint}-b",
                    event_index=0,
                    slot=CREATED_SLOT + 30,
                    side="buy",
                    quote_amount_base_units=1_000_000_000,
                    base_amount=500_000,
                    price_ppm=2_000_000,
                    wallet="w2",
                )
                upsert_trade(
                    db,
                    mint=mint,
                    signature=f"{mint}-c",
                    event_index=0,
                    slot=CREATED_SLOT + 60,
                    side="sell",
                    quote_amount_base_units=1_200_000_000,
                    base_amount=500_000,
                    price_ppm=2_400_000,
                    wallet="w2",
                )
        finally:
            db.close()

    def test_json_report_on_tmp_db(self, tmp_path, capsys) -> None:
        self._write_db(tmp_path)
        code = pairs_lab_main(
            [
                "--state-dir",
                str(tmp_path),
                "--json",
                "--min-labels",
                "1",
                "--min-bucket-count",
                "1",
            ]
        )
        assert code == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["coverage"]["labeled"] == 3
        assert payload["insufficient_data"] is False
        assert payload["base"]["winrate"] == 1.0
        assert len(payload["launches"]) == 3
        assert payload["launches"][0]["is_win"] is True

    def test_missing_db_fail_closed(self, tmp_path, capsys) -> None:
        code = pairs_lab_main(["--state-dir", str(tmp_path), "--json"])
        assert code == 1
        payload = json.loads(capsys.readouterr().out)
        assert payload["status"] == "abstain"
        assert payload["insufficient_data"] is True

    def test_invalid_tp_override_fails_closed(self, tmp_path, capsys) -> None:
        code = pairs_lab_main(["--state-dir", str(tmp_path), "--tp", "100,400"])
        assert code == 1
        payload = json.loads(capsys.readouterr().out)
        assert payload["status"] == "abstain"
        assert "config error" in payload["message"]

    def test_human_report_renders(self, tmp_path, capsys) -> None:
        self._write_db(tmp_path)
        code = pairs_lab_main(["--state-dir", str(tmp_path), "--min-labels", "1"])
        assert code == 0
        text = capsys.readouterr().out
        assert "PAIRS LAB" in text
        assert "labeled=3" in text
        assert "paper-only replay" in text
