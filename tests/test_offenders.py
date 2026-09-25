"""Unit tests for the rug_offenders pure helpers (no network)."""

# ruff: noqa: PLR0913, FBT002, TRY003

from rugbot.interfaces.cli.offenders import (
    aggregate_bands,
    band_key,
    build_parser,
    collect_recent_launches,
    default_max_pages,
    summarize_coverage,
)


def _row(
    mint="mint",
    funder=None,
    amount=None,
    ath=None,
    mcap=None,
    funding=False,
    candles=False,
    created_ms=None,
):
    return {
        "mint": mint,
        "creator": "creator",
        "funder": funder,
        "amount_sol": amount,
        "ath_multiple": ath,
        "start_mcap": mcap,
        "created_ms": created_ms,
        "has_funding": funding,
        "has_candles": candles,
    }


def test_band_key_rounding_and_boundaries():
    assert band_key(1.0, 0.5) == 2
    assert band_key(1.1, 0.5) == 2
    assert band_key(1.26, 0.5) == 3
    assert band_key(0.0, 0.5) == 0
    try:
        band_key(1.0, 0.0)
    except ValueError:
        pass
    else:
        raise AssertionError("expected ValueError for band=0")


def test_aggregate_bands_winrate_median_coverage():
    rows = [
        _row("m1", "F1", 1.0, 3.0, 100.0, True, True),
        _row("m2", "F1", 1.1, 1.2, 200.0, True, True),
        _row("m3", "F1", 0.9, None, 300.0, True, False),
        _row("m4", "F2", 5.0, 4.0, None, True, True),
        _row("m5", None, None, None, None, False, False),
    ]
    bands = aggregate_bands(rows, 2.0, 0.5)
    assert len(bands) == 2
    first = bands[0]
    assert first["funder"] == "F1"
    assert first["launches"] == 3
    assert first["scored"] == 2
    assert first["wins"] == 1
    assert first["winrate_pct"] == 50.0
    assert first["median_ath_multiple"] == 2.1
    assert first["max_ath_multiple"] == 3.0
    assert first["median_start_mcap"] == 200.0
    assert first["no_candles"] == 1
    assert first["no_funding_edge"] == 0
    assert bands[0]["launches"] >= bands[1]["launches"]
    assert "qualified" not in first
    assert "recommend" not in first


def test_aggregate_bands_empty_scored_winrate_none():
    rows = [_row("m1", "F1", 1.0, None, None, True, False)]
    bands = aggregate_bands(rows, 2.0, 0.5)
    assert bands[0]["winrate_pct"] is None
    assert bands[0]["scored"] == 0
    assert bands[0]["wins"] == 0


def test_summarize_coverage_counts():
    rows = [
        _row("m1", "F1", 1.0, 3.0, None, True, True),
        _row("m2", None, None, None, None, False, False),
    ]
    coverage = summarize_coverage(rows)
    assert coverage == {
        "launches": 2,
        "with_funding_edge": 1,
        "with_candles": 1,
        "no_funding_edge": 1,
        "no_candles": 1,
    }


def test_no_verdict_keys_in_payload_and_parser():
    parser = build_parser()
    args = parser.parse_args([])
    assert args.limit == 25
    assert args.band == 0.5
    assert args.min_launches == 3
    assert args.win_multiple == 2.0
    rows = [_row("m1", "F1", 1.0, 3.0, 100.0, True, True)]
    bands = aggregate_bands(rows, 2.0, 0.5)
    coverage = summarize_coverage(rows)
    blob = f"{bands!r} {coverage!r}".lower()
    for forbidden in ("qualified", "recommend", "pass", "fail", "worth"):
        assert forbidden not in blob


def _coin(mint):
    return {"mint": mint, "creator": f"creator-{mint}"}


def _paged_lister(pages):
    calls = []

    def fake(limit, offset):
        calls.append((limit, offset))
        return list(pages.get(offset, []))

    fake.calls = calls
    return fake


def test_collect_dedupes_mints_across_overlapping_pages():
    lister = _paged_lister(
        {
            0: [_coin("m1"), _coin("m2"), _coin("m3")],
            70: [_coin("m3"), _coin("m4"), _coin("m5")],
            140: [_coin("m5"), _coin("m6")],
        }
    )
    coins, pages = collect_recent_launches(10, lister=lister)
    assert [c["mint"] for c in coins] == ["m1", "m2", "m3", "m4", "m5", "m6"]
    assert pages == 3


def test_collect_stops_at_limit():
    lister = _paged_lister(
        {
            0: [_coin(f"m{i}") for i in range(5)],
            70: [_coin(f"m{i}") for i in range(5, 10)],
        }
    )
    coins, pages = collect_recent_launches(4, lister=lister)
    assert [c["mint"] for c in coins] == ["m0", "m1", "m2", "m3"]
    assert pages == 1
    assert lister.calls == [(70, 0)]


def test_collect_stops_on_no_progress_page():
    lister = _paged_lister(
        {
            0: [_coin("m1"), _coin("m2")],
            70: [_coin("m1"), _coin("m2")],
            140: [_coin("m3")],
        }
    )
    coins, pages = collect_recent_launches(10, lister=lister)
    assert [c["mint"] for c in coins] == ["m1", "m2"]
    assert pages == 2
    assert len(lister.calls) == 2


def test_collect_stops_on_empty_page():
    lister = _paged_lister({0: [_coin("m1")]})
    coins, pages = collect_recent_launches(10, lister=lister)
    assert [c["mint"] for c in coins] == ["m1"]
    assert pages == 2


def test_collect_max_pages_bound_honoured():
    lister = _paged_lister(
        {
            0: [_coin("m1")],
            70: [_coin("m2")],
            140: [_coin("m3")],
        }
    )
    coins, pages = collect_recent_launches(10, max_pages=1, lister=lister)
    assert [c["mint"] for c in coins] == ["m1"]
    assert pages == 1


def test_default_max_pages_derivation():
    assert default_max_pages(25) == 3
    assert default_max_pages(70) == 3
    assert default_max_pages(400) == 8


def test_aggregate_bands_cadence_and_daily_rate():
    now_s = 100000.0
    # F1 has 3 launches: at 100000 - 7200 (2h ago), 100000 - 3600 (1h ago), 100000 (now)
    # Median gap: 3600s (1h). Daily rate: 86400 / 3600 = 24.0 launches/day
    rows = [
        _row("m1", "F1", 1.0, 3.0, 100.0, True, True, created_ms=(now_s - 7200) * 1000),
        _row("m2", "F1", 1.0, 1.2, 200.0, True, True, created_ms=(now_s - 3600) * 1000),
        _row("m3", "F1", 1.0, 2.5, 300.0, True, True, created_ms=now_s * 1000),
    ]
    bands = aggregate_bands(rows, 2.0, 0.5, now_s=now_s)
    assert len(bands) == 1
    band = bands[0]
    assert band["cadence_seconds"] == 3600.0
    assert band["cadence_hours"] == 1.0
    assert band["daily_launch_rate"] == 24.0
    assert band["last_launch_hours_ago"] == 0.0
    assert band["active"] is True


def test_aggregate_bands_inactive_dormant_funder():
    now_s = 500000.0
    # Launch 48h ago (172800s ago)
    rows = [
        _row(
            "m1", "F1", 1.0, 1.5, 100.0, True, True, created_ms=(now_s - 172800) * 1000
        ),
    ]
    bands = aggregate_bands(rows, 2.0, 0.5, now_s=now_s)
    assert len(bands) == 1
    band = bands[0]
    assert band["cadence_seconds"] is None
    assert band["cadence_hours"] is None
    assert band["daily_launch_rate"] is None
    assert band["last_launch_hours_ago"] == 48.0
    assert band["active"] is False


def test_build_parser_activity_options():
    parser = build_parser()
    args = parser.parse_args(
        [
            "--min-daily-rate",
            "1.0",
            "--max-cadence-hours",
            "24.0",
            "--active-only",
            "--active-hours",
            "12.0",
            "--full-address",
        ]
    )
    assert args.min_daily_rate == 1.0
    assert args.max_cadence_hours == 24.0
    assert args.active_only is True
    assert args.active_hours == 12.0
    assert args.full_address is True
