"""Durable ledger for handled canonical observation identities."""

from __future__ import annotations

import base64
import binascii
import json
import os
from typing import TYPE_CHECKING, Protocol, cast

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path

    from rugbot.storage.jsonl_observation_store import ObservationIdentity

_IDENTITY_LENGTH = 15
_SHA256_HEX_LENGTH = 64
_IDENTITY_FIELDS = frozenset(
    {
        "source_id",
        "slot",
        "commitment",
        "canonical_status",
        "signature",
        "event_ordinal",
        "account_write_version",
        "account_pubkey",
        "account_owner_program_id",
        "raw_transaction_format",
        "source_update_kind",
        "raw_source_status",
        "raw_source_payload_hash",
        "raw_transaction_hash",
        "raw_account_data_hash",
    }
)
_COMMITMENTS = frozenset({"processed", "confirmed", "finalized"})
_CANONICAL_STATUSES = frozenset({"provisional", "canonical", "dead_fork", "replaced"})


class HandledEvidenceLedger(Protocol):
    """Storage boundary for canonical identities already handled downstream."""

    def contains(self, identity: ObservationIdentity) -> bool:
        """Return whether the canonical identity is durably recorded."""

    def append(self, identity: ObservationIdentity) -> bool:
        """Atomically append an unrecorded identity and fsync the record."""


class HandledEvidenceLedgerError(ValueError):
    """Raised when ledger state or an identity is malformed."""

    @classmethod
    def malformed_line(cls, line_number: int) -> HandledEvidenceLedgerError:
        """Build an error for a malformed ledger line."""

        return cls(f"malformed handled-evidence ledger line: {line_number}")

    @classmethod
    def invalid_identity(cls, field_name: str) -> HandledEvidenceLedgerError:
        """Build an error for an invalid identity field."""

        return cls(f"invalid handled-evidence identity field: {field_name}")

    @classmethod
    def incomplete_append(cls) -> HandledEvidenceLedgerError:
        """Build an error for an incomplete atomic append."""

        return cls("ledger append was incomplete")

    @classmethod
    def read_failed(cls) -> HandledEvidenceLedgerError:
        """Build an error for an unreadable ledger."""

        return cls("ledger could not be read")

    @classmethod
    def duplicate_key(cls) -> HandledEvidenceLedgerError:
        """Build an error for a duplicate JSON key."""

        return cls("duplicate handled-evidence ledger key")


class JsonlHandledEvidenceLedger:
    """Strict append-only JSONL ledger for handled observation identities."""

    def __init__(self, path: Path) -> None:
        """Initialize the ledger without loading or mutating raw observations."""

        self._path = path
        self._identity_cache: set[ObservationIdentity] | None = None
        self._file_state: tuple[bool, int, int] | None = None

    def contains(self, identity: ObservationIdentity) -> bool:
        """Check durable state using the canonical identity, never ``raw_id``."""

        validated = _validate_identity(identity)
        return validated in self._read_identity_set()

    def append(self, identity: ObservationIdentity) -> bool:
        """Append one complete identity with one atomic append syscall and fsync."""

        validated = _validate_identity(identity)
        identities = self._read_identity_set()
        if validated in identities:
            return False

        self._path.parent.mkdir(parents=True, exist_ok=True)
        line = _encode_line(validated)
        descriptor = os.open(
            self._path,
            os.O_APPEND | os.O_CREAT | os.O_WRONLY,
            0o600,
        )
        try:
            start_offset = os.lseek(descriptor, 0, os.SEEK_END)
            written = os.write(descriptor, line)
            if written != len(line):
                os.ftruncate(descriptor, start_offset)
                os.fsync(descriptor)
                raise HandledEvidenceLedgerError.incomplete_append()
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        identities.add(validated)
        self._identity_cache = identities
        self._file_state = self._path_state()
        return True

    def _read_identity_set(self) -> set[ObservationIdentity]:
        current_state = self._path_state()
        if self._identity_cache is not None and self._file_state == current_state:
            return self._identity_cache
        if not current_state[0]:
            self._identity_cache = set()
            self._file_state = current_state
            return self._identity_cache

        try:
            raw_lines = self._path.read_bytes().splitlines(keepends=True)
        except (OSError, UnicodeError) as error:
            raise HandledEvidenceLedgerError.read_failed() from error

        identities: set[ObservationIdentity] = set()
        for line_number, raw_line in enumerate(raw_lines, start=1):
            if not raw_line.endswith(b"\n"):
                raise HandledEvidenceLedgerError.malformed_line(line_number)
            try:
                payload = json.loads(
                    raw_line.decode("utf-8"),
                    object_pairs_hook=_reject_duplicate_keys,
                    parse_constant=_reject_json_constant,
                )
                identity = _identity_from_json(payload)
            except (HandledEvidenceLedgerError, UnicodeError, ValueError) as error:
                raise HandledEvidenceLedgerError.malformed_line(line_number) from error
            identities.add(identity)
        self._identity_cache = identities
        self._file_state = self._path_state()
        return identities

    def _path_state(self) -> tuple[bool, int, int]:
        try:
            stat = self._path.stat()
        except FileNotFoundError:
            return False, 0, 0
        except OSError as error:
            raise HandledEvidenceLedgerError.read_failed() from error
        return True, stat.st_mtime_ns, stat.st_size


def _encode_line(identity: ObservationIdentity) -> bytes:
    payload = handled_identity_to_json(identity)
    return (json.dumps(payload, separators=(",", ":"), sort_keys=True) + "\n").encode(
        "utf-8"
    )


def handled_identity_to_json(identity: ObservationIdentity) -> dict[str, object]:
    """Return the strict, raw-UUID-free JSON representation of an identity."""

    validated = _validate_identity(identity)
    return {
        "source_id": validated[0],
        "slot": validated[1],
        "commitment": validated[2],
        "canonical_status": validated[3],
        "signature": _encode_bytes(validated[4]),
        "event_ordinal": validated[5],
        "account_write_version": validated[6],
        "account_pubkey": _encode_bytes(validated[7]),
        "account_owner_program_id": _encode_bytes(validated[8]),
        "raw_transaction_format": validated[9],
        "source_update_kind": validated[10],
        "raw_source_status": validated[11],
        "raw_source_payload_hash": validated[12],
        "raw_transaction_hash": validated[13],
        "raw_account_data_hash": validated[14],
    }


def handled_identity_from_json(payload: object) -> ObservationIdentity:
    """Decode and validate one canonical handled-evidence identity."""

    return _identity_from_json(payload)


def validate_handled_identity(identity: object) -> ObservationIdentity:
    """Validate one canonical handled-evidence identity."""

    return _validate_identity(identity)


def _identity_from_json(payload: object) -> ObservationIdentity:
    if type(payload) is not dict:
        raise HandledEvidenceLedgerError.invalid_identity("record")
    data = cast("Mapping[str, object]", payload)
    if frozenset(data) != _IDENTITY_FIELDS:
        raise HandledEvidenceLedgerError.invalid_identity("record")

    values: list[object] = [
        _required_str(data, "source_id"),
        _required_nonnegative_int(data, "slot"),
        _required_enum(data, "commitment", _COMMITMENTS),
        _required_enum(data, "canonical_status", _CANONICAL_STATUSES),
        _optional_bytes(data, "signature"),
        _optional_nonnegative_int(data, "event_ordinal"),
        _optional_nonnegative_int(data, "account_write_version"),
        _optional_bytes(data, "account_pubkey"),
        _optional_bytes(data, "account_owner_program_id"),
        _optional_str(data, "raw_transaction_format"),
        _optional_str(data, "source_update_kind"),
        _optional_nonnegative_int(data, "raw_source_status"),
        _optional_hash(data, "raw_source_payload_hash"),
        _optional_hash(data, "raw_transaction_hash"),
        _optional_hash(data, "raw_account_data_hash"),
    ]
    return cast("ObservationIdentity", tuple(values))


def _validate_identity(identity: object) -> ObservationIdentity:
    if type(identity) is not tuple or len(identity) != _IDENTITY_LENGTH:
        raise HandledEvidenceLedgerError.invalid_identity("record")
    values = list(identity)
    if type(values[0]) is not str:
        raise HandledEvidenceLedgerError.invalid_identity("source_id")
    _validate_nonnegative_int(values[1], "slot")
    _validate_enum(values[2], "commitment", _COMMITMENTS)
    _validate_enum(values[3], "canonical_status", _CANONICAL_STATUSES)
    _validate_optional_bytes_value(values[4], "signature")
    _validate_optional_nonnegative_int(values[5], "event_ordinal")
    _validate_optional_nonnegative_int(values[6], "account_write_version")
    _validate_optional_bytes_value(values[7], "account_pubkey")
    _validate_optional_bytes_value(values[8], "account_owner_program_id")
    _validate_optional_str_value(values[9], "raw_transaction_format")
    _validate_optional_str_value(values[10], "source_update_kind")
    _validate_optional_nonnegative_int(values[11], "raw_source_status")
    for index, field_name in zip(
        (12, 13, 14),
        ("raw_source_payload_hash", "raw_transaction_hash", "raw_account_data_hash"),
        strict=True,
    ):
        _validate_optional_hash_value(values[index], field_name)
    return cast("ObservationIdentity", identity)


def _required_str(data: Mapping[str, object], field_name: str) -> str:
    value = data[field_name]
    if type(value) is not str:
        raise HandledEvidenceLedgerError.invalid_identity(field_name)
    return value


def _optional_str(data: Mapping[str, object], field_name: str) -> str | None:
    value = data[field_name]
    if value is not None and type(value) is not str:
        raise HandledEvidenceLedgerError.invalid_identity(field_name)
    return cast("str | None", value)


def _required_nonnegative_int(data: Mapping[str, object], field_name: str) -> int:
    value = data[field_name]
    _validate_nonnegative_int(value, field_name)
    return cast("int", value)


def _optional_nonnegative_int(
    data: Mapping[str, object], field_name: str
) -> int | None:
    value = data[field_name]
    _validate_optional_nonnegative_int(value, field_name)
    return cast("int | None", value)


def _required_enum(
    data: Mapping[str, object], field_name: str, allowed_values: frozenset[str]
) -> str:
    value = _required_str(data, field_name)
    _validate_enum(value, field_name, allowed_values)
    return value


def _optional_bytes(data: Mapping[str, object], field_name: str) -> bytes | None:
    value = data[field_name]
    if value is None:
        return None
    if type(value) is not str:
        raise HandledEvidenceLedgerError.invalid_identity(field_name)
    try:
        return base64.b64decode(value.encode("ascii"), validate=True)
    except (binascii.Error, UnicodeError) as error:
        raise HandledEvidenceLedgerError.invalid_identity(field_name) from error


def _optional_hash(data: Mapping[str, object], field_name: str) -> str | None:
    value = data[field_name]
    _validate_optional_hash_value(value, field_name)
    return cast("str | None", value)


def _validate_nonnegative_int(value: object, field_name: str) -> None:
    if type(value) is not int or value < 0:
        raise HandledEvidenceLedgerError.invalid_identity(field_name)


def _validate_optional_nonnegative_int(value: object, field_name: str) -> None:
    if value is not None:
        _validate_nonnegative_int(value, field_name)


def _validate_enum(
    value: object, field_name: str, allowed_values: frozenset[str]
) -> None:
    if type(value) is not str or value not in allowed_values:
        raise HandledEvidenceLedgerError.invalid_identity(field_name)


def _validate_optional_str_value(value: object, field_name: str) -> None:
    if value is not None and type(value) is not str:
        raise HandledEvidenceLedgerError.invalid_identity(field_name)


def _validate_optional_bytes_value(value: object, field_name: str) -> None:
    if value is not None and type(value) is not bytes:
        raise HandledEvidenceLedgerError.invalid_identity(field_name)


def _validate_optional_hash_value(value: object, field_name: str) -> None:
    if value is None:
        return
    if (
        type(value) is not str
        or len(value) != _SHA256_HEX_LENGTH
        or value.lower() != value
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise HandledEvidenceLedgerError.invalid_identity(field_name)


def _encode_bytes(value: bytes | None) -> str | None:
    if value is None:
        return None
    return base64.b64encode(value).decode("ascii")


def _reject_duplicate_keys(
    pairs: list[tuple[str, object]],
) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise HandledEvidenceLedgerError.duplicate_key()
        result[key] = value
    return result


def _reject_json_constant(value: str) -> None:
    raise HandledEvidenceLedgerError.invalid_identity(value)


__all__ = [
    "HandledEvidenceLedger",
    "HandledEvidenceLedgerError",
    "JsonlHandledEvidenceLedger",
    "handled_identity_from_json",
    "handled_identity_to_json",
    "validate_handled_identity",
]
