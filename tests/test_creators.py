"""Unit tests for the rug_creators pure helpers (no network)."""

from rugbot.interfaces.cli.creators import (
    assemble_payload,
    build_parser,
    collect_until_age,
    group_by_creator,
    partition_settled,
    score_launches,
)

NOW_S = 1_789_152_000.0


def _coin(mint, creator, age_s, mcap=1000.0):
    return {
        "mint": mint,
        "creator": creator,
        "created_timestamp": int((NOW_S - age_s) * 1000),
        "usd_market_cap": mcap,
    }


def _fake_resolver(aths):
    def fake(mint, created_ms):
        return aths.get(mint)

    return fake


def test_partition_settled_filters_by_age():
    coins = [
        _coin("old1", "C1", 7200),
        _coin("old2", "C1", 3700),
        _coin("young", "C2", 600),
        {"mint": "notime", "creator": "C3"},
    ]
    settled, excluded = partition_settled(coins, NOW_S, 3600.0)
    assert [c["mint"] for c in settled] == ["old1", "old2"]
    assert excluded == 2


def test_grouping_winrate_median_cadence():
    coins = [
        _coin("m1", "C1", 7200, 100.0),
        _coin("m2", "C1", 7100, 200.0),
        _coin("m3", "C1", 7000, 300.0),
        _coin("m4", "C2", 6900, 400.0),
    ]
    rows = score_launches(
        coins, _fake_resolver({"m1": 3.0, "m2": 1.2, "m3": None, "m4": 5.0})
    )
    creators = group_by_creator(rows, 2.0)
    assert [c["creator"] for c in creators] == ["C1", "C2"]
    first = creators[0]
    assert first["launches"] == 3
    assert first["scored"] == 2
    assert first["wins"] == 1
    assert first["winrate_pct"] == 50.0
    assert first["median_ath_multiple"] == 2.1
    assert first["max_ath_multiple"] == 3.0
    assert first["median_start_mcap"] == 200.0
    assert first["no_candles"] == 1
    assert first["cadence_seconds"] == 100.0
    assert first["first_launch_s"] < first["last_launch_s"]
    assert creators[1]["winrate_pct"] == 100.0


def test_no_candles_excluded_from_denominator():
    coins = [_coin("m1", "C1", 7200), _coin("m2", "C1", 7100)]
    rows = score_launches(coins, _fake_resolver({"m1": None, "m2": None}))
    creators = group_by_creator(rows, 2.0)
    assert creators[0]["scored"] == 0
    assert creators[0]["wins"] == 0
    assert creators[0]["winrate_pct"] is None
    assert creators[0]["no_candles"] == 2


def test_no_verdict_keys_in_payload():
    coins = [_coin("m1", "C1", 7200), _coin("m2", "C1", 7100)]
    rows = score_launches(coins, _fake_resolver({"m1": 3.0, "m2": 1.2}))
    creators = group_by_creator(rows, 2.0)
    payload = assemble_payload(
        creators=creators,
        coverage={
            "launches_listed": 2,
            "pages_fetched": 1,
            "settled": 2,
            "unsettled_excluded": 0,
            "no_candles": 0,
        },
        limit=2,
        min_age_min=60.0,
        win_multiple=2.0,
        min_launches=2,
    )
    keys: set[str] = set()

    def _walk(obj: object) -> None:
        if isinstance(obj, dict):
            for key, value in obj.items():
                keys.add(str(key).lower())
                _walk(value)
        elif isinstance(obj, list):
            for value in obj:
                _walk(value)

    _walk(payload)
    assert not keys & {"qualified", "recommend", "verdict", "passed", "failed"}
    body = repr({k: v for k, v in payload.items() if k != "reference"}).lower()
    for forbidden in ("qualified", "recommend", "worth tracking"):
        assert forbidden not in body
    assert set(payload["coverage"]) == {
        "launches_listed",
        "pages_fetched",
        "settled",
        "unsettled_excluded",
        "no_candles",
    }


def test_collect_stops_on_no_progress_when_target_unmet():
    pages = {
        0: [_coin("m1", "C1", 300), _coin("m2", "C2", 200)],
        70: [_coin("m3", "C3", 5000), _coin("m4", "C4", 100)],
    }
    calls = []

    def fake(limit, offset):
        calls.append((limit, offset))
        return list(pages.get(offset, []))

    coins, _pages = collect_until_age(500, 3600.0, NOW_S, lister=fake)
    assert [c["mint"] for c in coins] == ["m1", "m2", "m3", "m4"]
    assert calls[-1] == (70, 140)


def _shifting_lister(total_unique, age_step_s=60.0, overlap=60):
    """Pages of 70 with ``70 - overlap`` new mints each; age grows by index."""
    coins = [
        {
            "mint": f"m{i}",
            "creator": f"C{i % 7}",
            "created_timestamp": int((NOW_S - i * age_step_s) * 1000),
            "usd_market_cap": 1000.0,
        }
        for i in range(total_unique)
    ]

    def fake(limit, offset):
        start = (offset // 70) * (70 - overlap)
        return [dict(c) for c in coins[start : start + 70]]

    return fake


def test_collect_continues_past_first_settled_until_target():
    coins, _pages = collect_until_age(
        500, 3600.0, NOW_S, settled_target=25, lister=_shifting_lister(400)
    )
    mints = [c["mint"] for c in coins]
    assert len(mints) == len(set(mints))
    assert len(coins) > 70
    settled, _excluded = partition_settled(coins, NOW_S, 3600.0)
    assert len(settled) >= 25


def test_collect_stops_once_settled_target_met():
    coins, _pages = collect_until_age(
        500, 3600.0, NOW_S, settled_target=25, lister=_shifting_lister(400)
    )
    assert len(coins) <= 140
    settled, _excluded = partition_settled(coins, NOW_S, 3600.0)
    assert len(settled) >= 25


def test_collect_stops_at_max_pages():
    coins, _pages = collect_until_age(
        500,
        3600.0,
        NOW_S,
        settled_target=25,
        max_pages=1,
        lister=_shifting_lister(400),
    )
    assert len(coins) == 70


def test_collect_stops_on_frozen_listing():
    calls = []

    def fake(limit, offset):
        calls.append(offset)
        return [_coin("m1", "C1", 100), _coin("m2", "C2", 100)]

    coins, _pages = collect_until_age(
        500, 3600.0, NOW_S, settled_target=25, lister=fake
    )
    assert [c["mint"] for c in coins] == ["m1", "m2"]
    assert len(calls) <= 6


def test_parser_defaults():
    args = build_parser().parse_args([])
    assert args.limit == 2500
    assert args.settled_target == 300
    assert args.min_age_min == 60.0
    assert args.min_launches == 2
    assert args.win_multiple == 2.0
