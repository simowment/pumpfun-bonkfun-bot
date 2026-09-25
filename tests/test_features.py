"""Tests for rug_features pure helpers (no network)."""

from rugbot.interfaces.cli.features import (
    attention_features,
    build_feature_record,
    entry_close_from_candles,
    entry_mcap_from_token,
    funding_band_for_amount,
    labels_from_trajectory,
    narrative_proxies,
    summarize_coverage,
)

FORBIDDEN_KEYS = {
    "verdict",
    "qualified",
    "worth_tracking",
    "worth-tracking",
    "pass",
    "fail",
    "track",
}


def _candles() -> list[dict]:
    base = 1_700_000_000_000
    return [
        {"timestamp": base, "open": "1", "high": "1", "low": "1", "close": "1"},
        {
            "timestamp": base + 1000,
            "open": "1",
            "high": "2.5",
            "low": "0.5",
            "close": "1",
        },
        {
            "timestamp": base + 2000,
            "open": "1",
            "high": "3.5",
            "low": "0.8",
            "close": "1.2",
        },
        {
            "timestamp": base + 3000,
            "open": "1.2",
            "high": "1.5",
            "low": "0.9",
            "close": "1.1",
        },
    ]


def test_band_rounding() -> None:
    assert funding_band_for_amount(0.24) == 0.0
    assert funding_band_for_amount(0.26) == 0.5
    assert funding_band_for_amount(1.0) == 1.0
    assert funding_band_for_amount(1.26) == 1.5
    assert funding_band_for_amount(None) is None


def test_narrative_proxies() -> None:
    out = narrative_proxies("BONK2X", "Bonk Coin 🚀")
    assert out["symbol_len"] == 6
    assert out["name_len"] == len("Bonk Coin 🚀")
    assert out["symbol_has_digit"] is True
    assert out["symbol_all_caps"] is True
    assert out["symbol_has_emoji"] is False
    assert out["name_has_emoji"] is True
    lower = narrative_proxies("bonk", "b")
    assert lower["symbol_all_caps"] is False
    assert lower["symbol_has_digit"] is False


def test_labels_from_canned_trajectory() -> None:
    points = [(1.0, 0.5), (1.0, 2.5), (2.0, 0.8), (2.0, 3.5)]
    labels = labels_from_trajectory(points, 3.5)
    assert labels["max_multiple_after_entry"] == 3.5
    assert labels["ath_multiple"] == 3.5
    assert labels["reached_2x"] is True
    assert labels["reached_3x"] is True
    assert labels["adverse_multiple"] == 0.5
    small = labels_from_trajectory([(1.0, 1.5), (1.0, 1.8)], 1.8)
    assert small["reached_2x"] is False
    assert small["reached_3x"] is False


def test_null_handling_when_candles_absent() -> None:
    record = build_feature_record(
        mint="MINT",
        creator="CREATOR",
        created_ms=1_700_000_000_000,
        symbol="ABC",
        name="Abc",
        deployer_count=1,
        funding_edge=None,
        candles=[],
        token=None,
        sol_price=None,
        now_ms=1_700_000_100_000,
    )
    assert record["candles_available"] is False
    assert record["entry_price"] is None
    assert record["ath_multiple"] is None
    assert record["reached_2x"] is None
    assert record["reached_3x"] is None
    assert record["entry_mcap_sol"] is None
    assert record["funding_available"] is False


def test_no_verdict_keys_in_payload() -> None:
    record = build_feature_record(
        mint="MINT",
        creator="CREATOR",
        created_ms=1_700_000_000_000,
        symbol="ABC",
        name="Abc",
        deployer_count=2,
        funding_edge=("FUNDER", 1.0),
        candles=_candles(),
        token={"total_supply": 1_000_000_000_000_000, "base_decimals": 6},
        sol_price=200.0,
        now_ms=1_700_000_100_000,
    )
    lowered = {str(k).lower() for k in record}
    assert not (lowered & FORBIDDEN_KEYS)
    coverage = summarize_coverage([record])
    assert not ({str(k).lower() for k in coverage} & FORBIDDEN_KEYS)


def test_attention_features_from_listing_row() -> None:
    """Attention fields derive from the listing row with nulls intact."""
    coin = {
        "twitter": "https://x.com/a/status/123",
        "website": "https://example.com",
        "description": "hello",
        "image_uri": "https://img",
        "profile_image": "",
        "reply_count": 3,
        "verified": True,
        "nsfw": False,
        "boost_mode": "NONE",
        "is_currently_live": False,
        "username": "bob",
        "market_cap": 28.0,
        "usd_market_cap": 2800.0,
        "real_sol_reserves": 5,
        "virtual_sol_reserves": 30_000_000_001,
        "complete": False,
        "ath_market_cap": 5600.0,
        "ath_market_cap_timestamp": 1_700_000_000_1000,
    }
    out = attention_features(coin)
    assert out["has_twitter"] is True
    assert out["twitter_is_status_link"] is True
    assert out["has_website"] is True
    assert out["has_description"] is True
    assert out["description_len"] == 5
    assert out["has_image"] is True
    assert out["has_profile_image"] is False
    assert out["reply_count"] == 3
    assert out["verified"] is True
    assert out["has_username"] is True
    assert out["market_cap"] == 28.0
    assert out["ath_multiple_from_api"] == 2.0


def test_attention_features_nulls_stay_null() -> None:
    """Missing listing fields yield nulls, never fabricated values."""
    out = attention_features(None)
    assert out["has_twitter"] is None
    assert out["ath_multiple_from_api"] is None
    out2 = attention_features({})
    assert out2["has_twitter"] is None
    assert out2["description_len"] is None
    assert out2["ath_multiple_from_api"] is None
    out3 = attention_features(
        {"twitter": "  ", "usd_market_cap": 0.0, "ath_market_cap": 5.0}
    )
    assert out3["has_twitter"] is False
    assert out3["twitter_is_status_link"] is None
    assert out3["ath_multiple_from_api"] is None


def test_build_record_carries_attention_columns() -> None:
    """build_feature_record merges attention fields without verdicts."""
    record = build_feature_record(
        mint="MINT",
        creator="CREATOR",
        created_ms=1_700_000_000_000,
        symbol="ABC",
        name="Abc",
        deployer_count=1,
        funding_edge=None,
        candles=[],
        token=None,
        sol_price=None,
        now_ms=1_700_000_100_000,
        listing={"twitter": "https://x.com/a", "market_cap": 28.0},
    )
    assert record["has_twitter"] is True
    assert record["twitter_is_status_link"] is False
    assert record["market_cap"] == 28.0
    lowered = {str(k).lower() for k in record}
    assert not (lowered & FORBIDDEN_KEYS)


def test_entry_close_and_mcap_helpers() -> None:
    assert entry_close_from_candles([], 0) is None
    mcap, reason = entry_mcap_from_token(None, None, None)
    assert mcap is None and reason is not None
    mcap2, reason2 = entry_mcap_from_token(
        2.0, {"total_supply": 1_000_000_000_000_000, "base_decimals": 6}, 200.0
    )
    assert mcap2 is not None and reason2 is None
