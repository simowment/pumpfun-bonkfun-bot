"""Canonical tracker DB path resolution must be host-independent and fail closed."""

from __future__ import annotations

import pytest

from rugbot.runtime.config import (
    TRACKER_DB_FILENAME,
    TrackerDbPathError,
    resolve_state_dir,
    resolve_tracker_db_path,
)


def test_default_is_canonical_state_dir_without_host_literal(
    monkeypatch, tmp_path
) -> None:
    """With no override the tracker DB is ``<state_dir>/rugbot.db``."""

    monkeypatch.delenv("RUGBOT_DB_PATH", raising=False)
    state_dir = tmp_path / "state"
    assert resolve_tracker_db_path(state_dir) == state_dir / TRACKER_DB_FILENAME
    assert resolve_tracker_db_path(str(state_dir)) == state_dir / TRACKER_DB_FILENAME


def test_default_matches_the_runtime_state_dir(monkeypatch) -> None:
    """The CLI default must be the same store the watch runtime already uses."""

    monkeypatch.delenv("RUGBOT_DB_PATH", raising=False)
    assert resolve_tracker_db_path() == resolve_state_dir(None) / TRACKER_DB_FILENAME


def test_absolute_override_is_honored(monkeypatch, tmp_path) -> None:
    """An explicit absolute ``RUGBOT_DB_PATH`` wins over the default."""

    override = tmp_path / "nested" / "custom.db"
    monkeypatch.setenv("RUGBOT_DB_PATH", str(override))
    assert resolve_tracker_db_path() == override
    assert override.parent.is_dir()


def test_relative_override_is_anchored_to_cwd(monkeypatch, tmp_path) -> None:
    """A relative override never resolves against an unknown directory."""

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("RUGBOT_DB_PATH", "relative/tracker.db")
    resolved = resolve_tracker_db_path()
    assert resolved.is_absolute()
    assert resolved == tmp_path / "relative" / "tracker.db"


def test_directory_override_fails_closed(monkeypatch, tmp_path) -> None:
    """Naming a directory must abort instead of writing an unintended file."""

    monkeypatch.setenv("RUGBOT_DB_PATH", str(tmp_path))
    with pytest.raises(TrackerDbPathError):
        resolve_tracker_db_path()


def test_blank_override_falls_back_to_canonical(monkeypatch, tmp_path) -> None:
    """An empty override is not a path and must not be trusted."""

    monkeypatch.setenv("RUGBOT_DB_PATH", "   ")
    state_dir = tmp_path / "state"
    assert resolve_tracker_db_path(state_dir) == state_dir / TRACKER_DB_FILENAME
