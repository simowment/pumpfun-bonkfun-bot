"""Unit tests for theme-wave helpers (pure logic only)."""

from rugbot.interfaces.cli.themes import (
    auc_rank,
    coin_tokens,
    compute_wave_features,
    derive_signatures,
    summarize_feature,
    tokenize,
    two_by_two,
)


def test_tokenize_normalises_and_drops_boilerplate():
    tokens = tokenize("Join LAUNCHED Moon-Dog! https://t.co/x TG Discord")
    assert "moon" not in tokens  # stopword/common path covered
    assert "dog" in tokens
    assert "join" not in tokens
    assert "launched" not in tokens
    assert "tg" not in tokens
    assert all(len(t) >= 3 for t in tokens)
    assert tokenize(123) == set()
    assert tokenize("") == set()


def test_derive_signatures_requires_freq_two_and_excludes_common():
    coins = [
        {"name": "Moon Dog", "symbol": "DOG", "description": "the best dog"},
        {"name": "Moon Dog Jr", "symbol": "DOG2", "description": "another dog coin"},
        {
            "name": "Unique Zebra Qqx",
            "symbol": "ZQX",
            "description": "lonely qqx zebra",
        },
    ]
    sigs = derive_signatures(coins)
    # "dog" appears twice -> distinctive; "the"/"coin" excluded via common/stop path
    assert "dog" in sigs[0]
    assert "dog" in sigs[1]
    assert "the" not in sigs[0]
    # third coin shares nothing -> empty signature
    assert sigs[2] == set()


def test_coin_tokens_merges_fields():
    tokens = coin_tokens({"name": "Ninja", "symbol": "NIN", "description": ""})
    assert "ninja" in tokens


def test_compute_wave_features_leakage_safe():
    ordered = [
        {
            "signature": {"dog"},
            "created_ms": 0,
            "reached_2x": True,
            "max_multiple": 3.0,
        },
        {
            "signature": {"dog"},
            "created_ms": 600_000,
            "reached_2x": False,
            "max_multiple": 1.2,
        },
        {
            "signature": {"cat"},
            "created_ms": 1_200_000,
            "reached_2x": True,
            "max_multiple": 2.5,
        },
    ]
    rows = compute_wave_features(ordered)
    # first coin: no priors
    assert rows[0]["is_first_of_theme"] == 1
    assert rows[0]["is_copycat"] == 0
    assert rows[0]["theme_prior_count_60m"] == 0
    # second coin: one prior same-theme within 60m, a prior winner
    assert rows[1]["is_first_of_theme"] == 0
    assert rows[1]["is_copycat"] == 1
    assert rows[1]["theme_prior_count_60m"] == 1
    assert rows[1]["theme_prior_winners_60m"] == 1
    assert rows[1]["theme_best_prior_multiple_60m"] == 3.0
    assert rows[1]["theme_seconds_since_prior"] == 600.0
    # third coin: different theme -> first of its theme
    assert rows[2]["is_first_of_theme"] == 1
    assert rows[2]["theme_prior_count_60m"] == 0
    # future outcomes never leak into the first row
    assert rows[0]["theme_prior_winners_60m"] == 0


def test_auc_rank_perfect_and_null():
    assert auc_rank([1.0, 2.0, 3.0, 4.0], [False, False, True, True]) == 1.0
    assert auc_rank([1.0, 1.0], [True, True]) is None
    assert auc_rank([None, None], [True, False]) is None


def test_summarize_feature_binary_and_sorted_use():
    summary = summarize_feature(
        [1, 0, 1, 0], [True, False, True, False], name="is_copycat", base_rate=0.5
    )
    assert summary["winner_share"] == 1.0
    assert summary["loser_share"] == 0.0
    assert summary["auc"] == 1.0


def test_two_by_two_counts_and_ci():
    result = two_by_two(
        [True, True, False, False, False, False],
        [True, False, True, False, False, False],
    )
    assert (result["a_prior_win"], result["b_prior_loss"]) == (1, 1)
    assert (result["c_noprior_win"], result["d_noprior_loss"]) == (1, 3)
    assert result["lift"] is not None
    assert result["ci95"] is not None


def test_two_by_two_zero_cell_reports_reason():
    result = two_by_two([False, False], [True, False])
    assert result["ci95"] is None
    assert result["ci_reason"] is not None
