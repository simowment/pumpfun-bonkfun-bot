"""Tests for the entity ATH pure helpers (no network)."""

from __future__ import annotations

from decimal import Decimal

from rugbot.decision.lite_profiler import (
    LiteLaunchResult,
    LiteOptimalTp,
    LiteProfileReport,
)
from rugbot.interfaces.cli.entity_ath import parse_entity_specs, summarize_entity


def _report() -> LiteProfileReport:
    launches = (
        LiteLaunchResult(
            mint="m1",
            entry_price_sol=Decimal("1"),
            ath_multiple=Decimal("2"),
            hits=(),
            entry_mcap_sol=10.0,
            ath_mcap_sol=20.0,
        ),
        LiteLaunchResult(
            mint="m2",
            entry_price_sol=Decimal("1"),
            ath_multiple=Decimal("4"),
            hits=(),
            entry_mcap_sol=30.0,
            ath_mcap_sol=120.0,
        ),
        LiteLaunchResult(
            mint="m3",
            entry_price_sol=Decimal("1"),
            ath_multiple=Decimal("6"),
            hits=(),
            entry_mcap_sol=0.0,
            ath_mcap_sol=0.0,
        ),
    )
    return LiteProfileReport(
        launch_count=3,
        skipped_count=0,
        launches=launches,
        ath_avg=4.0,
        ath_max=6.0,
        ath_min=2.0,
        ath_median=4.0,
        entry_mcap_sol_avg=20.0,
        entry_mcap_sol_min=10.0,
        entry_mcap_sol_max=30.0,
        ath_mcap_sol_avg=70.0,
        ath_mcap_sol_median=70.0,
        ath_mcap_sol_max=120.0,
        ath_mcap_sol_min=20.0,
        mcap_scored_count=2,
        optimal_tp=LiteOptimalTp(
            tp_multiple=2.0,
            winrate_pct=100.0,
            ev_multiple=1.9,
            launch_count=3,
            qualifies=False,
        ),
    )


def test_parse_bare_targets() -> None:
    assert parse_entity_specs(["w1", "w2"], []) == {"target": ["w1", "w2"]}


def test_parse_entity_groups() -> None:
    assert parse_entity_specs([], ["a=w1,w2", "b=w3"]) == {
        "a": ["w1", "w2"],
        "b": ["w3"],
    }


def test_parse_merge_and_dedupe() -> None:
    result = parse_entity_specs(["w1", "w1", "w2"], ["a=w2,w3", "a=w3,w4"])
    assert result == {"target": ["w1", "w2"], "a": ["w2", "w3", "w4"]}


def test_parse_combining_both() -> None:
    result = parse_entity_specs(["w0"], ["a=w1,w2"])
    assert result == {"target": ["w0"], "a": ["w1", "w2"]}


def test_summarize_entity_mcap_stats_and_qualifies() -> None:
    summary = summarize_entity("a", _report(), 5)
    assert summary.name == "a"
    assert summary.scanned == 5
    assert summary.launches == 3
    assert summary.mcap_scored == 2
    assert summary.entry_mcap_avg_sol == 20.0
    assert summary.entry_mcap_median_sol == 20.0
    assert summary.ath_mcap_avg_sol == 70.0
    assert summary.ath_mcap_median_sol == 70.0
    assert summary.ath_mcap_max_sol == 120.0
    assert summary.ath_mcap_min_sol == 20.0
    assert summary.ath_multiple_avg == 4.0
    assert summary.ath_multiple_median == 4.0
    assert summary.ath_multiple_max == 6.0
    assert summary.qualifies is False
