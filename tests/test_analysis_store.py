"""Tests for the analysis store and --from-store path (no network)."""

from __future__ import annotations

import json
from typing import Any

from rugbot.analysis.store import AnalysisStore
from rugbot.interfaces.cli import features as features_mod


def _record(mint: str, created: int, **over: Any) -> dict[str, Any]:
    """Build a minimal feature record for store tests."""
    record: dict[str, Any] = {
        "mint": mint,
        "creator": "CREATOR",
        "created_at_ms": created,
        "reached_2x": True,
        "label_unavailable_reason": None,
        "error": None,
    }
    record.update(over)
    return record


def test_round_trip(tmp_path) -> None:
    """Rows persist and read back with bool types intact."""
    store = AnalysisStore(tmp_path / "store.sqlite3")
    try:
        assert store.upsert_launches([_record("M1", 1000)]) == 1
        rows = store.get_launches()
        assert len(rows) == 1
        assert rows[0]["mint"] == "M1"
        assert rows[0]["reached_2x"] is True
        assert store.count() == 1
    finally:
        store.close()


def test_upsert_conflict_updates(tmp_path) -> None:
    """Upsert on the same mint updates the row and keeps it unique."""
    store = AnalysisStore(tmp_path / "store.sqlite3")
    try:
        store.upsert_launches([_record("M1", 1000, funder=None)])
        store.upsert_launches([_record("M1", 1000, funder="FUNDER")])
        assert store.count() == 1
        rows = store.get_launches()
        assert rows[0]["funder"] == "FUNDER"
    finally:
        store.close()


def test_ordering_and_limit(tmp_path) -> None:
    """get_launches honors ordering and limit."""
    store = AnalysisStore(tmp_path / "store.sqlite3")
    try:
        store.upsert_launches(
            [_record("M1", 1000), _record("M2", 3000), _record("M3", 2000)]
        )
        rows = store.get_launches(order="created_at_ms DESC")
        assert [r["mint"] for r in rows] == ["M2", "M3", "M1"]
        rows = store.get_launches(limit=2, order="created_at_ms DESC")
        assert [r["mint"] for r in rows] == ["M2", "M3"]
        rows = store.get_launches(order="created_at_ms ASC")
        assert [r["mint"] for r in rows] == ["M1", "M3", "M2"]
    finally:
        store.close()


def test_latest_created_at_ms(tmp_path) -> None:
    """latest_created_at_ms tracks the max timestamp."""
    store = AnalysisStore(tmp_path / "store.sqlite3")
    try:
        assert store.latest_created_at_ms() is None
        store.upsert_launches([_record("M1", 1000), _record("M2", 5000)])
        assert store.latest_created_at_ms() == 5000
    finally:
        store.close()


def test_unknown_keys_ignored(tmp_path) -> None:
    """Unknown feature keys never crash the store."""
    store = AnalysisStore(tmp_path / "store.sqlite3")
    try:
        row = _record("M1", 1000)
        row["brand_new_future_key"] = "x"
        assert store.upsert_launches([row]) == 1
        assert store.get_launches()[0]["mint"] == "M1"
    finally:
        store.close()


def test_pending_label_count(tmp_path) -> None:
    """pending_label_count counts rows with a label reason set."""
    store = AnalysisStore(tmp_path / "store.sqlite3")
    try:
        store.upsert_launches(
            [
                _record("M1", 1000, label_unavailable_reason=None),
                _record("M2", 2000, label_unavailable_reason="no candles"),
            ]
        )
        assert store.pending_label_count(9999, 300) == 1
    finally:
        store.close()


def test_from_store_performs_no_network(tmp_path, monkeypatch, capsys) -> None:
    """--from-store reads the store without touching the network client."""
    store = AnalysisStore(tmp_path / "store.sqlite3")
    try:
        store.upsert_launches([_record("M1", 1000), _record("M2", 2000)])
    finally:
        store.close()

    def _raise(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("network touched")  # noqa: TRY003

    monkeypatch.setattr(features_mod, "get_client", _raise)
    monkeypatch.setattr(features_mod, "collect_recent_launches", _raise)
    code = features_mod.main(
        ["--from-store", "--store", str(tmp_path / "store.sqlite3"), "--json"]
    )
    assert code == 0
    out = capsys.readouterr().out
    payload = json.loads(out.splitlines()[0])
    assert payload["coverage"]["rows"] == 2
    assert len(payload["rows"]) == 2
