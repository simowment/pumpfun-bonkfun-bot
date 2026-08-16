"""Durable checkpoint storage for source streams."""

import json
from dataclasses import dataclass
from pathlib import Path
from tempfile import NamedTemporaryFile

CHECKPOINT_STORE_SCHEMA_VERSION = "source-checkpoints-v1"
_STORE_KEYS = {"schema_version", "checkpoints"}
_CHECKPOINT_KEYS = {"last_slot", "receive_sequence"}
_SOURCE_ID_INVALID = "checkpoint source_id is invalid"
_STORE_INVALID_JSON = "checkpoint store is not valid JSON"
_STORE_PAYLOAD_MALFORMED = "checkpoint store payload is malformed"
_STORE_KEYS_INVALID = "checkpoint store keys are invalid"
_STORE_SCHEMA_UNSUPPORTED = "checkpoint store schema version is unsupported"
_STORE_CHECKPOINTS_MALFORMED = "checkpoint store checkpoints are malformed"
_CHECKPOINT_RECORD_MALFORMED = "checkpoint record is malformed"
_DUPLICATE_JSON_KEY = "checkpoint store contains duplicate JSON keys"


@dataclass(frozen=True, slots=True)
class SourceCheckpoint:
    """Durable stream checkpoint.

    Args:
        source_id: Logical source identifier.
        last_slot: Last durably terminal safe slot for replay.
        receive_sequence: Last durably processed receive sequence.
    """

    source_id: str
    last_slot: int
    receive_sequence: int


class CheckpointStoreError(ValueError):
    """Raised when checkpoint state is malformed or unsupported."""


class JsonCheckpointStore:
    """Small JSON checkpoint store for early milestone ingestion.

    This is intentionally simple and auditable. It can be replaced by a
    PostgreSQL implementation once storage adapters exist.
    """

    def __init__(self, path: Path) -> None:
        """Initialize the store.

        Args:
            path: JSON file path used for checkpoints.
        """

        self._path = path

    def load(self, source_id: str) -> SourceCheckpoint | None:
        """Load a checkpoint for one source.

        Args:
            source_id: Logical source identifier.

        Returns:
            Existing checkpoint, or None when no checkpoint exists.
        """

        if not _valid_source_id(source_id):
            raise _store_error(_SOURCE_ID_INVALID)
        checkpoints = self._load_checkpoints()
        checkpoint = checkpoints.get(source_id)
        if checkpoint is None:
            return None
        return checkpoint

    def save(self, checkpoint: SourceCheckpoint) -> None:
        """Save a checkpoint atomically.

        Args:
            checkpoint: Checkpoint to persist.
        """

        checkpoint_error = _checkpoint_error(checkpoint)
        if checkpoint_error is not None:
            raise CheckpointStoreError(checkpoint_error)

        self._path.parent.mkdir(parents=True, exist_ok=True)
        checkpoints = self._load_checkpoints()
        existing = checkpoints.get(checkpoint.source_id)
        if existing is not None:
            if checkpoint.last_slot < existing.last_slot:
                return
            if checkpoint.receive_sequence <= existing.receive_sequence:
                return

        checkpoints[checkpoint.source_id] = checkpoint

        with NamedTemporaryFile(
            "w",
            delete=False,
            dir=self._path.parent,
            encoding="utf-8",
        ) as temp_file:
            json.dump(_store_payload(checkpoints), temp_file, indent=2, sort_keys=True)
            temp_path = Path(temp_file.name)

        temp_path.replace(self._path)

    def _load_checkpoints(self) -> dict[str, SourceCheckpoint]:
        if not self._path.exists():
            return {}

        try:
            with self._path.open("r", encoding="utf-8") as checkpoint_file:
                payload = json.load(
                    checkpoint_file,
                    object_pairs_hook=_strict_json_object,
                )
        except json.JSONDecodeError as error:
            raise _store_error(_STORE_INVALID_JSON) from error

        return _checkpoints_from_payload(payload)


def _checkpoints_from_payload(payload: object) -> dict[str, SourceCheckpoint]:
    if type(payload) is not dict:
        raise _store_error(_STORE_PAYLOAD_MALFORMED)
    if "schema_version" in payload:
        return _versioned_checkpoints_from_payload(payload)
    return _legacy_checkpoints_from_payload(payload)


def _versioned_checkpoints_from_payload(
    payload: dict[object, object],
) -> dict[str, SourceCheckpoint]:
    if set(payload) != _STORE_KEYS:
        raise _store_error(_STORE_KEYS_INVALID)
    if payload["schema_version"] != CHECKPOINT_STORE_SCHEMA_VERSION:
        raise _store_error(_STORE_SCHEMA_UNSUPPORTED)
    raw_checkpoints = payload["checkpoints"]
    if type(raw_checkpoints) is not dict:
        raise _store_error(_STORE_CHECKPOINTS_MALFORMED)
    return _checkpoint_map_from_records(raw_checkpoints)


def _legacy_checkpoints_from_payload(
    payload: dict[object, object],
) -> dict[str, SourceCheckpoint]:
    return _checkpoint_map_from_records(payload)


def _checkpoint_map_from_records(
    records: dict[object, object],
) -> dict[str, SourceCheckpoint]:
    checkpoints: dict[str, SourceCheckpoint] = {}
    for source_id, record in records.items():
        if not _valid_source_id(source_id):
            raise _store_error(_SOURCE_ID_INVALID)
        checkpoints[source_id] = _checkpoint_from_record(source_id, record)
    return checkpoints


def _checkpoint_from_record(source_id: str, record: object) -> SourceCheckpoint:
    if type(record) is not dict or set(record) != _CHECKPOINT_KEYS:
        raise _store_error(_CHECKPOINT_RECORD_MALFORMED)
    checkpoint = SourceCheckpoint(
        source_id=source_id,
        last_slot=record["last_slot"],
        receive_sequence=record["receive_sequence"],
    )
    checkpoint_error = _checkpoint_error(checkpoint)
    if checkpoint_error is not None:
        raise CheckpointStoreError(checkpoint_error)
    return checkpoint


def _store_payload(checkpoints: dict[str, SourceCheckpoint]) -> dict[str, object]:
    return {
        "schema_version": CHECKPOINT_STORE_SCHEMA_VERSION,
        "checkpoints": {
            source_id: _checkpoint_record(checkpoint)
            for source_id, checkpoint in checkpoints.items()
        },
    }


def _checkpoint_record(checkpoint: SourceCheckpoint) -> dict[str, int]:
    return {
        "last_slot": checkpoint.last_slot,
        "receive_sequence": checkpoint.receive_sequence,
    }


def _checkpoint_error(checkpoint: object) -> str | None:
    if type(checkpoint) is not SourceCheckpoint:
        return "checkpoint is malformed"
    try:
        source_id = checkpoint.source_id
        last_slot = checkpoint.last_slot
        receive_sequence = checkpoint.receive_sequence
    except AttributeError:
        return "checkpoint is malformed"
    checks = (
        (
            not _valid_source_id(source_id),
            "checkpoint source_id is invalid",
        ),
        (
            not _non_negative_int(last_slot),
            "checkpoint last_slot is invalid",
        ),
        (
            not _non_negative_int(receive_sequence),
            "checkpoint receive_sequence is invalid",
        ),
    )
    for has_error, message in checks:
        if has_error:
            return message
    return None


def _valid_source_id(value: object) -> bool:
    return type(value) is str and bool(value.strip())


def _non_negative_int(value: object) -> bool:
    return type(value) is int and value >= 0


def _strict_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise _store_error(_DUPLICATE_JSON_KEY)
        result[key] = value
    return result


def _store_error(message: str) -> CheckpointStoreError:
    return CheckpointStoreError(message)
