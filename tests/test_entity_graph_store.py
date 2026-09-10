"""Unit tests for entity-graph dossier persistence (no network)."""

from pathlib import Path

from rugbot.storage.database import DatabaseManager
from rugbot.storage.tracker import SQLiteTrackerRepository
from rugbot.tracker.models import EntityGraphSnapshotRecord


def _repo(tmp_path: Path) -> SQLiteTrackerRepository:
    """Build a tracker repository on an isolated temp database."""
    return SQLiteTrackerRepository(DatabaseManager(tmp_path / "graph.db"))


def _snapshot(
    wallet: str = "WalletA", graph_json: str = '{"a": 1}'
) -> EntityGraphSnapshotRecord:
    """Build one canned entity-graph snapshot record."""
    return EntityGraphSnapshotRecord(
        wallet=wallet,
        query=wallet,
        graph_json=graph_json,
        created_at="2026-09-05T00:00:00",
        updated_at="2026-09-05T00:00:00",
    )


def test_save_and_load_round_trip(tmp_path: Path) -> None:
    """A saved dossier loads back intact; missing wallets return None."""
    repo = _repo(tmp_path)
    assert repo.get_entity_graph("WalletA") is None
    repo.save_entity_graph(_snapshot())
    loaded = repo.get_entity_graph("WalletA")
    assert loaded is not None
    assert loaded.wallet == "WalletA"
    assert loaded.query == "WalletA"
    assert loaded.graph_json == '{"a": 1}'


def test_save_replaces_latest_per_wallet(tmp_path: Path) -> None:
    """Re-saving a wallet replaces its dossier instead of duplicating."""
    repo = _repo(tmp_path)
    repo.save_entity_graph(_snapshot())
    repo.save_entity_graph(_snapshot(graph_json='{"b": 2}'))
    loaded = repo.get_entity_graph("WalletA")
    assert loaded is not None
    assert loaded.graph_json == '{"b": 2}'


def test_graphs_are_keyed_per_wallet(tmp_path: Path) -> None:
    """Two wallets keep independent dossiers."""
    repo = _repo(tmp_path)
    repo.save_entity_graph(_snapshot(wallet="WalletA"))
    repo.save_entity_graph(_snapshot(wallet="WalletB", graph_json='{"b": 2}'))
    assert repo.get_entity_graph("WalletA") is not None
    assert repo.get_entity_graph("WalletB") is not None
    assert repo.get_entity_graph("Nobody") is None
