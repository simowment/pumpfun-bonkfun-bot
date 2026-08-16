"""One small SQLite store for durable derived watcher state."""

from __future__ import annotations

import json
import sqlite3
from threading import RLock
from typing import TYPE_CHECKING

from rugbot.ingest.checkpoints import (
    CheckpointStoreError,
    SourceCheckpoint,
    validate_source_checkpoint,
)
from rugbot.storage.handled_evidence_ledger import (
    HandledEvidenceLedgerError,
    handled_identity_from_json,
    handled_identity_to_json,
    validate_handled_identity,
)
from rugbot.storage.paper_position_store import (
    PaperPositionStoreError,
    paper_position_state_from_json,
    paper_position_state_to_json,
    validate_paper_position_state,
)

if TYPE_CHECKING:
    from pathlib import Path

    from rugbot.execution.position_runtime import PaperPositionState
    from rugbot.storage.jsonl_observation_store import ObservationIdentity


class SqliteStateStoreError(ValueError):
    """Raised when the SQLite state store cannot be initialized or used."""

    @classmethod
    def opened_failed(cls) -> SqliteStateStoreError:
        """Build an error for a database that could not be opened."""

        return cls("SQLite state store could not be opened")

    @classmethod
    def closed(cls) -> SqliteStateStoreError:
        """Build an error for use after close."""

        return cls("SQLite state store is closed")


class SqliteStateStore:
    """Durably store checkpoints, handled identities, and paper positions.

    Raw observations intentionally remain in the append-only JSONL store. This
    database contains only derived state needed to resume the watcher.
    """

    def __init__(self, path: Path) -> None:
        """Create the database and its fixed core tables."""

        self._path = path
        self._lock = RLock()
        self._connection: sqlite3.Connection | None = None
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            connection = sqlite3.connect(
                path,
                isolation_level=None,
                check_same_thread=False,
            )
            self._connection = connection
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute("PRAGMA synchronous = FULL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS checkpoints (
                    source_id TEXT PRIMARY KEY NOT NULL,
                    last_slot INTEGER NOT NULL,
                    receive_sequence INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS handled_evidence (
                    identity_json TEXT PRIMARY KEY NOT NULL
                );
                CREATE TABLE IF NOT EXISTS positions (
                    market_id TEXT PRIMARY KEY NOT NULL,
                    state_json TEXT NOT NULL
                );
                """
            )
        except sqlite3.Error as error:
            if self._connection is not None:
                self._connection.close()
            raise SqliteStateStoreError.opened_failed() from error

    def close(self) -> None:
        """Close the database connection."""

        with self._lock:
            if self._connection is not None:
                self._connection.close()
                self._connection = None

    def __enter__(self) -> SqliteStateStore:
        """Return this store for scoped runtime ownership."""

        return self

    def __exit__(self, *_: object) -> None:
        """Close the store at the end of a scoped runtime operation."""

        self.close()

    def load(self, source_id: str) -> SourceCheckpoint | None:
        """Load one source checkpoint."""

        _validate_source_id(source_id)
        try:
            with self._locked_connection() as connection:
                row = connection.execute(
                    """
                    SELECT last_slot, receive_sequence
                    FROM checkpoints
                    WHERE source_id = ?
                    """,
                    (source_id,),
                ).fetchone()
        except sqlite3.Error as error:
            raise CheckpointStoreError.read_failed() from error
        if row is None:
            return None
        try:
            return validate_source_checkpoint(
                SourceCheckpoint(
                    source_id=source_id,
                    last_slot=row[0],
                    receive_sequence=row[1],
                )
            )
        except (CheckpointStoreError, TypeError, ValueError) as error:
            raise CheckpointStoreError.malformed_record() from error

    def _save_checkpoint(self, checkpoint: SourceCheckpoint) -> None:
        """Persist a monotonic source checkpoint transactionally."""

        validated = validate_source_checkpoint(checkpoint)
        try:
            with self._write_transaction() as connection:
                existing_row = connection.execute(
                    """
                    SELECT last_slot, receive_sequence
                    FROM checkpoints
                    WHERE source_id = ?
                    """,
                    (validated.source_id,),
                ).fetchone()
                if existing_row is not None:
                    existing = validate_source_checkpoint(
                        SourceCheckpoint(
                            source_id=validated.source_id,
                            last_slot=existing_row[0],
                            receive_sequence=existing_row[1],
                        )
                    )
                    if validated.last_slot < existing.last_slot:
                        return
                    if validated.receive_sequence <= existing.receive_sequence:
                        return
                connection.execute(
                    """
                    INSERT INTO checkpoints(source_id, last_slot, receive_sequence)
                    VALUES (?, ?, ?)
                    ON CONFLICT(source_id) DO UPDATE SET
                        last_slot = excluded.last_slot,
                        receive_sequence = excluded.receive_sequence
                    """,
                    (
                        validated.source_id,
                        validated.last_slot,
                        validated.receive_sequence,
                    ),
                )
        except (CheckpointStoreError, sqlite3.Error) as error:
            if isinstance(error, CheckpointStoreError):
                raise
            raise CheckpointStoreError.write_failed() from error

    def contains(self, identity: ObservationIdentity) -> bool:
        """Return whether a canonical handled identity is present."""

        validated = validate_handled_identity(identity)
        try:
            with self._locked_connection() as connection:
                rows = connection.execute(
                    "SELECT identity_json FROM handled_evidence"
                ).fetchall()
                identities = {_identity_from_record(row[0]) for row in rows}
        except (HandledEvidenceLedgerError, sqlite3.Error) as error:
            if isinstance(error, HandledEvidenceLedgerError):
                raise
            raise HandledEvidenceLedgerError.read_failed() from error
        return validated in identities

    def append(self, identity: ObservationIdentity) -> bool:
        """Atomically insert one handled identity and report whether it was new."""

        validated = validate_handled_identity(identity)
        key = _identity_key(validated)
        try:
            with self._write_transaction() as connection:
                rows = connection.execute(
                    "SELECT identity_json FROM handled_evidence"
                ).fetchall()
                identities = {_identity_from_record(row[0]) for row in rows}
                if validated in identities:
                    return False
                connection.execute(
                    "INSERT INTO handled_evidence(identity_json) VALUES (?)",
                    (key,),
                )
        except (HandledEvidenceLedgerError, sqlite3.Error) as error:
            if isinstance(error, HandledEvidenceLedgerError):
                raise
            raise HandledEvidenceLedgerError.incomplete_append() from error
        return True

    def read_all(self) -> tuple[PaperPositionState, ...]:
        """Read all paper positions in canonical market order."""

        try:
            with self._locked_connection() as connection:
                rows = connection.execute(
                    "SELECT market_id, state_json FROM positions"
                ).fetchall()
            states = []
            for market_id, state_json in rows:
                state = paper_position_state_from_json(_strict_json_loads(state_json))
                if state.market_id != market_id:
                    raise PaperPositionStoreError.malformed_state()
                states.append(state)
        except (
            PaperPositionStoreError,
            sqlite3.Error,
            UnicodeError,
            ValueError,
        ) as error:
            if isinstance(error, PaperPositionStoreError):
                raise
            raise PaperPositionStoreError.malformed_state() from error
        return tuple(sorted(states, key=lambda state: state.market_id))

    def get(self, market_id: str) -> PaperPositionState | None:
        """Read one paper position by canonical market identity."""

        _validate_market_id(market_id)
        return next(
            (state for state in self.read_all() if state.market_id == market_id),
            None,
        )

    def _save_position(self, state: PaperPositionState) -> None:
        """Persist one paper position snapshot transactionally."""

        validated = validate_paper_position_state(state)
        encoded = json.dumps(
            paper_position_state_to_json(validated),
            separators=(",", ":"),
            sort_keys=True,
        )
        try:
            with self._write_transaction() as connection:
                connection.execute(
                    """
                    INSERT INTO positions(market_id, state_json)
                    VALUES (?, ?)
                    ON CONFLICT(market_id) DO UPDATE SET
                        state_json = excluded.state_json
                    """,
                    (validated.market_id, encoded),
                )
        except sqlite3.Error as error:
            raise PaperPositionStoreError.write_failed() from error

    def save(self, value: SourceCheckpoint | PaperPositionState) -> None:
        """Persist either a source checkpoint or a paper position snapshot."""

        if type(value) is SourceCheckpoint:
            self._save_checkpoint(value)
            return
        self._save_position(value)

    def remove(self, market_id: str) -> bool:
        """Remove one paper position transactionally."""

        _validate_market_id(market_id)
        try:
            with self._write_transaction() as connection:
                cursor = connection.execute(
                    "DELETE FROM positions WHERE market_id = ?", (market_id,)
                )
        except sqlite3.Error as error:
            raise PaperPositionStoreError.write_failed() from error
        return cursor.rowcount == 1

    def _locked_connection(self) -> _ConnectionLock:
        """Hold the process lock while a connection is used."""

        return _ConnectionLock(self._lock, self._connection_or_raise())

    def _write_transaction(self) -> _WriteTransaction:
        """Begin an immediate transaction and commit or roll it back."""

        return _WriteTransaction(self._lock, self._connection_or_raise())

    def _connection_or_raise(self) -> sqlite3.Connection:
        if self._connection is None:
            raise SqliteStateStoreError.closed()
        return self._connection


class _ConnectionLock:
    def __init__(self, lock: RLock, connection: sqlite3.Connection) -> None:
        self._lock = lock
        self._connection = connection

    def __enter__(self) -> sqlite3.Connection:
        self._lock.acquire()
        return self._connection

    def __exit__(self, *_: object) -> None:
        self._lock.release()


class _WriteTransaction(_ConnectionLock):
    def __enter__(self) -> sqlite3.Connection:
        connection = super().__enter__()
        try:
            connection.execute("BEGIN IMMEDIATE")
        except BaseException:
            self._lock.release()
            raise
        return connection

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        try:
            if exc_type is None:
                self._connection.commit()
            else:
                self._connection.rollback()
        finally:
            self._lock.release()


def _identity_key(identity: ObservationIdentity) -> str:
    return json.dumps(
        handled_identity_to_json(identity),
        separators=(",", ":"),
        sort_keys=True,
    )


def _identity_from_record(record: object) -> ObservationIdentity:
    if type(record) is not str:
        raise HandledEvidenceLedgerError.malformed_line(0)
    try:
        return handled_identity_from_json(_strict_json_loads(record))
    except (UnicodeError, ValueError) as error:
        raise HandledEvidenceLedgerError.malformed_line(0) from error


def _strict_json_loads(value: object) -> object:
    if type(value) is not str:
        raise ValueError
    return json.loads(
        value,
        object_pairs_hook=_reject_duplicate_keys,
        parse_constant=_reject_json_constant,
    )


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError
        result[key] = value
    return result


def _reject_json_constant(_value: str) -> None:
    raise ValueError


def _validate_source_id(value: object) -> None:
    if type(value) is not str or not value.strip():
        raise CheckpointStoreError.invalid_source_id()


def _validate_market_id(value: object) -> None:
    if type(value) is not str or not value:
        raise PaperPositionStoreError.invalid_field("market_id")


__all__ = ["SqliteStateStore", "SqliteStateStoreError"]
