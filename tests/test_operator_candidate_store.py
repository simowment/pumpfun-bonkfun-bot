"""Unit tests for operator-candidate persistence (no network)."""

from pathlib import Path

from rugbot.storage.database import DatabaseManager
from rugbot.storage.tracker import SQLiteTrackerRepository
from rugbot.tracker.models import OperatorCandidateRecord


def _repo(tmp_path: Path) -> SQLiteTrackerRepository:
    """Build a tracker repository on an isolated temp database."""
    return SQLiteTrackerRepository(DatabaseManager(tmp_path / "candidates.db"))


def _candidate(
    wallet: str = "WalletA",
    source_entity: str = "EntityX",
    created_count: int = 3,
    first_seen_at: str = "2026-09-05T00:00:00",
    last_seen_at: str = "2026-09-05T00:00:00",
) -> OperatorCandidateRecord:
    """Build one canned operator-candidate record."""
    return OperatorCandidateRecord(
        wallet=wallet,
        source_entity=source_entity,
        created_count=created_count,
        first_seen_at=first_seen_at,
        last_seen_at=last_seen_at,
    )


def test_save_and_load_round_trip(tmp_path: Path) -> None:
    """A saved candidate loads back intact; unknown entities yield nothing."""
    repo = _repo(tmp_path)
    assert repo.get_operator_candidates() == ()
    assert repo.save_operator_candidates([_candidate()]) == 1
    loaded = repo.get_operator_candidates()
    assert len(loaded) == 1
    assert loaded[0] == _candidate()


def test_merge_keeps_max_count_and_first_seen(tmp_path: Path) -> None:
    """A narrower later slice cannot shrink the count or reset first_seen."""
    repo = _repo(tmp_path)
    repo.save_operator_candidates([_candidate(created_count=51)])
    repo.save_operator_candidates(
        [
            _candidate(
                created_count=2,
                first_seen_at="2026-09-06T00:00:00",
                last_seen_at="2026-09-06T00:00:00",
            )
        ]
    )
    loaded = repo.get_operator_candidates()
    assert len(loaded) == 1
    assert loaded[0].created_count == 51
    assert loaded[0].first_seen_at == "2026-09-05T00:00:00"
    assert loaded[0].last_seen_at == "2026-09-06T00:00:00"


def test_candidates_are_scoped_by_source_entity(tmp_path: Path) -> None:
    """One wallet under two entities is stored once and filterable per entity."""
    repo = _repo(tmp_path)
    repo.save_operator_candidates(
        [_candidate(wallet="WalletA", source_entity="EntityX")]
    )
    repo.save_operator_candidates(
        [_candidate(wallet="WalletB", source_entity="EntityY")]
    )
    assert len(repo.get_operator_candidates()) == 2
    entity_x = repo.get_operator_candidates(source_entity="EntityX")
    assert [c.wallet for c in entity_x] == ["WalletA"]


def test_empty_save_is_a_noop(tmp_path: Path) -> None:
    """Saving no candidates reports zero and writes nothing."""
    repo = _repo(tmp_path)
    assert repo.save_operator_candidates([]) == 0
    assert repo.get_operator_candidates() == ()
