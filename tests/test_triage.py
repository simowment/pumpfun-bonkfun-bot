"""Unit tests for the rug_triage measurement sheet (no network)."""

from rugbot.interfaces.cli.triage import (
    CEX_FLEET_UNRESOLVED,
    assemble_sheet,
    cadence_seconds,
    classify_funding_shape,
    creator_only_activity,
)
from rugbot.tracker.funding_chain import is_cex_shaped_source


def test_classify_direct() -> None:
    assert (
        classify_funding_shape(
            creator_creations=3,
            upstream_depth=0,
            source_recipient_count=2,
            source_creation_count=5,
        )
        == "1 direct"
    )


def test_classify_cex_band() -> None:
    assert (
        classify_funding_shape(
            creator_creations=1,
            upstream_depth=1,
            source_recipient_count=500,
            source_creation_count=0,
        )
        == "2 CEX-band"
    )


def test_classify_relay() -> None:
    assert (
        classify_funding_shape(
            creator_creations=1,
            upstream_depth=2,
            source_recipient_count=4,
            source_creation_count=0,
        )
        == "3 relay"
    )


def test_cadence_median_gap() -> None:
    assert cadence_seconds([100.0, 160.0, 220.0]) == 60.0


def test_cadence_edge_cases() -> None:
    assert cadence_seconds([]) is None
    assert cadence_seconds([42.0]) is None


def test_assemble_sheet_has_no_verdict() -> None:
    sheet = assemble_sheet(
        mint="MINT",
        creator="CREATOR",
        archetype="Type 1",
        funding_shape="1 direct",
        funding_source="SOURCE",
        upstream_depth=0,
        entity_activity={"n_launches": 3},
        bundler_profile={"status": "unavailable", "reason": "no trade data"},
        backtest={"net_ev_sol": 0.01},
        next_action="re-arm listener on dev wallet CREATOR (observe-only; no auto-arm)",
    )
    blob = str(sheet).lower()
    assert "qualified" not in blob
    assert "recommend" not in blob
    assert set(sheet) == {
        "mint",
        "creator",
        "entity_activity",
        "bundler_profile",
        "backtest",
        "heads_up",
        "reference",
    }


def test_is_cex_shaped_boundary() -> None:
    assert (
        is_cex_shaped_source(source_creation_count=0, source_recipient_count=50) is True
    )
    assert (
        is_cex_shaped_source(source_creation_count=0, source_recipient_count=49)
        is False
    )
    assert (
        is_cex_shaped_source(source_creation_count=1, source_recipient_count=500)
        is False
    )


def test_creator_only_activity_for_cex_source() -> None:
    activity = creator_only_activity([1_789_071_923_000], active_days=7.0)
    assert activity["n_launches"] == 1
    assert activity["fleet"] == CEX_FLEET_UNRESOLVED
    blob = str(activity).lower()
    assert "qualified" not in blob
    assert "recommend" not in blob
