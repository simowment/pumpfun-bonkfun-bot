"""Unified SQLite database connection manager for Rugbot."""

# ruff: noqa: TRY003

from __future__ import annotations

import sqlite3
from pathlib import Path
from threading import RLock


class DatabaseManager:
    """Thread-safe SQLite connection manager with WAL mode and schema initialization."""

    def __init__(self, path: Path | str = Path(".state/watch/rugbot.db")) -> None:
        self._path = Path(path)
        self._lock = RLock()
        self._connection: sqlite3.Connection | None = None
        self._initialize()

    @property
    def path(self) -> Path:
        return self._path

    @property
    def connection(self) -> sqlite3.Connection:
        with self._lock:
            if self._connection is None:
                self._initialize()
            if self._connection is None:
                raise RuntimeError("Failed to initialize SQLite database connection")
            return self._connection

    def _initialize(self) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
        except (PermissionError, OSError):
            self._path = Path.home() / ".rugbot" / "state" / self._path.name
            self._path.parent.mkdir(parents=True, exist_ok=True)

        conn = sqlite3.connect(
            str(self._path),
            check_same_thread=False,
            isolation_level=None,
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")
        self._connection = conn

    def close(self) -> None:
        with self._lock:
            if self._connection is not None:
                self._connection.close()
                self._connection = None
