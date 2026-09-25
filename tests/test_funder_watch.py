"""Tests for the observe-only funder watcher (no network)."""

from __future__ import annotations

from pathlib import Path

from rugbot.interfaces.cli.funder_watch import (
    FunderWatchEvent,
    freshness_bucket,
    in_band,
    load_state,
    new_events,
    save_state,
    scan_funder,
)
from rugbot.tracker.funder_discovery import StagedTransferCandidate


def _event(funder: str, signature: str) -> FunderWatchEvent:
    return FunderWatchEvent(
        funder=funder,
        recipient="Recipient1111111111111111111111111111111111",
        amount_sol=1.0,
        signature=signature,
        slot=10,
        recipient_lifetime_creations=0,
        freshness="fresh",
    )


def test_freshness_buckets() -> None:
    """Bucket 0/1/2+ creation counts correctly."""
    assert freshness_bucket(0) == "fresh"
    assert freshness_bucket(1) == "just_launched"
    assert freshness_bucket(2) == "used"
    assert freshness_bucket(25) == "used"


def test_band_filter_boundaries() -> None:
    """Band edges are inclusive; outside values excluded."""
    assert in_band(0.2, 0.2, 5.0)
    assert in_band(5.0, 0.2, 5.0)
    assert not in_band(0.19, 0.2, 5.0)
    assert not in_band(5.01, 0.2, 5.0)


def test_dedupe_same_signature_not_reemitted() -> None:
    """Seen signatures are filtered across runs."""
    seen = {"FunderA|sig1"}
    kept = new_events(seen, [_event("FunderA", "sig1"), _event("FunderA", "sig2")])
    assert [event.signature for event in kept] == ["sig2"]


def test_state_round_trip(tmp_path: Path) -> None:
    """Seen keys persist and reload via tmp_path."""
    path = tmp_path / "funder_watch.json"
    assert load_state(path) == set()
    save_state(path, {"FunderA|sig1", "FunderB|sig2"})
    assert load_state(path) == {"FunderA|sig1", "FunderB|sig2"}


def test_scan_funder_band_and_freshness() -> None:
    """Out-of-band dropped; fresh-only drops just-launched."""

    def edge_fn(_funder: str) -> tuple[list[StagedTransferCandidate], None]:
        return (
            [
                StagedTransferCandidate(
                    "Fresh11111111111111111111111111111111", 1.0, 5, "sig-fresh"
                ),
                StagedTransferCandidate(
                    "Big1111111111111111111111111111111111", 99.0, 6, "sig-big"
                ),
                StagedTransferCandidate(
                    "Used11111111111111111111111111111111", 1.0, 7, "sig-used"
                ),
            ],
            None,
        )

    counts = {"Fresh": 0, "Used": 5}

    def creations_fn(wallet: str) -> int:
        return 0 if wallet.startswith("Fresh") else 5

    assert counts  # documents the seam mapping used below
    events, warning = scan_funder(
        "FunderA",
        "http://localhost",
        min_sol=0.2,
        max_sol=5.0,
        fresh_only=True,
        edge_fn=edge_fn,
        creations_fn=creations_fn,
    )
    assert warning is None
    assert [event.signature for event in events] == ["sig-fresh"]
    assert events[0].freshness == "fresh"

    events_all, _ = scan_funder(
        "FunderA",
        "http://localhost",
        min_sol=0.2,
        max_sol=5.0,
        fresh_only=False,
        edge_fn=edge_fn,
        creations_fn=lambda wallet: 1 if wallet.startswith("Fresh") else 5,
    )
    assert [event.freshness for event in events_all] == [
        "just_launched",
        "used",
    ]


def test_scan_funder_fail_soft() -> None:
    """Edge errors return a warning instead of raising."""

    def boom(_funder: str) -> tuple[list[StagedTransferCandidate], None]:
        raise RuntimeError("rpc down")  # noqa: TRY003 - test seam signal

    events, warning = scan_funder(
        "FunderA",
        "http://localhost",
        min_sol=0.2,
        max_sol=5.0,
        fresh_only=True,
        edge_fn=boom,
    )
    assert events == []
    assert warning is not None and "RuntimeError" in warning
