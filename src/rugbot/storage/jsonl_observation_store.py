"""Append-only JSONL storage for raw chain observations."""

import base64
import hashlib
import json
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Literal, Protocol, cast, runtime_checkable
from uuid import UUID

from rugbot.domain.observations import (
    CanonicalStatus,
    Commitment,
    RawChainObservation,
)

OBSERVATION_SCHEMA_VERSION = 4
_BYTE_FIELD_NAMES = {
    "blockhash",
    "signature",
    "program_id",
    "account_pubkey",
    "account_owner_program_id",
    "raw_transaction",
    "raw_account_data",
    "raw_source_payload",
}
_OBSERVATION_FIELDS = frozenset(
    {
        "schema_version",
        "raw_id",
        "source_id",
        "observer_id",
        "boot_id",
        "receive_sequence",
        "slot",
        "parent_slot",
        "blockhash",
        "signature",
        "transaction_index",
        "outer_instruction_index",
        "inner_instruction_group_index",
        "inner_instruction_index",
        "stack_height",
        "event_ordinal",
        "commitment",
        "canonical_status",
        "received_wall_ns",
        "received_monotonic_ns",
        "program_id",
        "account_pubkey",
        "account_owner_program_id",
        "raw_transaction",
        "raw_transaction_format",
        "raw_account_data",
        "account_write_version",
        "source_update_kind",
        "raw_source_status",
        "raw_source_payload",
        "decoder_name",
        "decoder_version",
        "idl_hash",
    }
)
_COMMITMENTS = {"processed", "confirmed", "finalized"}
_CANONICAL_STATUSES = {"provisional", "canonical", "dead_fork", "replaced"}
JsonValue = int | str | None
ObservationJson = dict[str, JsonValue]
ObservationIdentity = tuple[
    str,
    int,
    str,
    str,
    bytes | None,
    int | None,
    int | None,
    bytes | None,
    bytes | None,
    str | None,
    str | None,
    int | None,
    str | None,
    str | None,
    str | None,
]


class ObservationStore(Protocol):
    """Storage adapter contract for immutable raw observations."""

    def append(self, observation: RawChainObservation) -> bool:
        """Append one raw observation durably.

        Returns:
            True when a new row was appended, False when source evidence was
            already present.
        """


@runtime_checkable
class ExistingObservationStore(ObservationStore, Protocol):
    """Observation store that can resolve already-durable source evidence."""

    def find_existing(
        self,
        observation: RawChainObservation,
    ) -> RawChainObservation | None:
        """Return the durable row matching an observation identity, if present."""


class ObservationDecodeError(ValueError):
    """Raised when a stored observation cannot be decoded."""

    @classmethod
    def invalid_json_object(cls) -> "ObservationDecodeError":
        """Build an invalid JSON object error."""

        return cls("observation line is not a JSON object")

    @classmethod
    def unsupported_schema(cls) -> "ObservationDecodeError":
        """Build an unsupported schema error."""

        return cls("unsupported observation schema version")

    @classmethod
    def missing_field(cls, field_name: str) -> "ObservationDecodeError":
        """Build a missing field error."""

        return cls(f"missing or invalid observation field: {field_name}")

    @classmethod
    def invalid_field_set(cls) -> "ObservationDecodeError":
        """Build an exact-field-set contract error."""

        return cls("observation field set does not match the current contract")

    @classmethod
    def invalid_enum(cls, field_name: str) -> "ObservationDecodeError":
        """Build an invalid enum error."""

        return cls(f"invalid observation enum field: {field_name}")


class JsonlObservationStore:
    """Append-only JSONL store for milestone-zero raw observation durability."""

    def __init__(self, path: Path) -> None:
        """Initialize the JSONL store.

        Args:
            path: File path used for append-only observation records.
        """

        self._path = path

    def append(self, observation: RawChainObservation) -> bool:
        """Append one observation and fsync the write.

        Args:
            observation: Immutable raw observation to persist.

        Returns:
            True when a new row was appended, False for an idempotent duplicate.
        """

        self._path.parent.mkdir(parents=True, exist_ok=True)
        if observation_identity(observation) in self._read_identity_set():
            return False

        line = json.dumps(
            _observation_to_json(observation),
            separators=(",", ":"),
            sort_keys=True,
        )
        with self._path.open("a", encoding="utf-8") as output_file:
            output_file.write(line)
            output_file.write("\n")
            output_file.flush()
            os.fsync(output_file.fileno())
        return True

    def read_all(self) -> list[RawChainObservation]:
        """Read all stored observations in append order."""

        if not self._path.exists():
            return []

        observations: list[RawChainObservation] = []
        with self._path.open("r", encoding="utf-8") as input_file:
            for line in input_file:
                if line.strip():
                    payload = cast("object", json.loads(line))
                    observations.append(_observation_from_json(payload))
        return observations

    def find_existing(
        self,
        observation: RawChainObservation,
    ) -> RawChainObservation | None:
        """Find the existing durable row matching an observation identity."""

        identity = observation_identity(observation)
        for stored_observation in self.read_all():
            if observation_identity(stored_observation) == identity:
                return stored_observation
        return None

    def _read_identity_set(self) -> set[ObservationIdentity]:
        return {observation_identity(observation) for observation in self.read_all()}


def _observation_to_json(observation: RawChainObservation) -> ObservationJson:
    return {
        "schema_version": OBSERVATION_SCHEMA_VERSION,
        "raw_id": str(observation.raw_id),
        "source_id": observation.source_id,
        "observer_id": observation.observer_id,
        "boot_id": str(observation.boot_id),
        "receive_sequence": observation.receive_sequence,
        "slot": observation.slot,
        "parent_slot": observation.parent_slot,
        "blockhash": _encode_bytes(observation.blockhash),
        "signature": _encode_bytes(observation.signature),
        "transaction_index": observation.transaction_index,
        "outer_instruction_index": observation.outer_instruction_index,
        "inner_instruction_group_index": observation.inner_instruction_group_index,
        "inner_instruction_index": observation.inner_instruction_index,
        "stack_height": observation.stack_height,
        "event_ordinal": observation.event_ordinal,
        "commitment": observation.commitment,
        "canonical_status": observation.canonical_status,
        "received_wall_ns": observation.received_wall_ns,
        "received_monotonic_ns": observation.received_monotonic_ns,
        "program_id": _encode_bytes(observation.program_id),
        "account_pubkey": _encode_bytes(observation.account_pubkey),
        "account_owner_program_id": _encode_bytes(observation.account_owner_program_id),
        "raw_transaction": _encode_bytes(observation.raw_transaction),
        "raw_transaction_format": observation.raw_transaction_format,
        "raw_account_data": _encode_bytes(observation.raw_account_data),
        "account_write_version": observation.account_write_version,
        "source_update_kind": observation.source_update_kind,
        "raw_source_status": observation.raw_source_status,
        "raw_source_payload": _encode_bytes(observation.raw_source_payload),
        "decoder_name": observation.decoder_name,
        "decoder_version": observation.decoder_version,
        "idl_hash": observation.idl_hash,
    }


def observation_identity(observation: RawChainObservation) -> ObservationIdentity:
    """Return the stable evidence identity used by storage and replay."""

    return (
        observation.source_id,
        observation.slot,
        observation.commitment,
        observation.canonical_status,
        observation.signature,
        observation.event_ordinal,
        observation.account_write_version,
        observation.account_pubkey,
        observation.account_owner_program_id,
        observation.raw_transaction_format,
        observation.source_update_kind,
        observation.raw_source_status,
        _payload_hash(observation.raw_source_payload),
        _payload_hash(observation.raw_transaction),
        _payload_hash(observation.raw_account_data),
    )


def _observation_from_json(payload: object) -> RawChainObservation:
    data = _require_mapping(payload)
    if frozenset(data) != _OBSERVATION_FIELDS:
        raise ObservationDecodeError.invalid_field_set()
    schema_version = _required_int(data, "schema_version")
    if schema_version != OBSERVATION_SCHEMA_VERSION:
        raise ObservationDecodeError.unsupported_schema()

    program_id = _optional_bytes(data, "program_id")
    source_update_kind = _optional_str(data, "source_update_kind")
    account_owner_program_id = _required_optional_bytes(
        data, "account_owner_program_id"
    )

    return RawChainObservation(
        raw_id=UUID(_required_str(data, "raw_id")),
        source_id=_required_str(data, "source_id"),
        observer_id=_required_str(data, "observer_id"),
        boot_id=UUID(_required_str(data, "boot_id")),
        receive_sequence=_required_int(data, "receive_sequence"),
        slot=_required_int(data, "slot"),
        parent_slot=_optional_int(data, "parent_slot"),
        blockhash=_optional_bytes(data, "blockhash"),
        signature=_optional_bytes(data, "signature"),
        transaction_index=_optional_int(data, "transaction_index"),
        outer_instruction_index=_optional_int(data, "outer_instruction_index"),
        inner_instruction_group_index=_optional_int(
            data,
            "inner_instruction_group_index",
        ),
        inner_instruction_index=_optional_int(data, "inner_instruction_index"),
        stack_height=_optional_int(data, "stack_height"),
        event_ordinal=_optional_int(data, "event_ordinal"),
        commitment=_required_commitment(data),
        canonical_status=_required_canonical_status(data),
        received_wall_ns=_required_int(data, "received_wall_ns"),
        received_monotonic_ns=_required_int(data, "received_monotonic_ns"),
        program_id=program_id,
        account_pubkey=_optional_bytes(data, "account_pubkey"),
        account_owner_program_id=account_owner_program_id,
        raw_transaction=_optional_bytes(data, "raw_transaction"),
        raw_transaction_format=_optional_str(data, "raw_transaction_format"),
        raw_account_data=_optional_bytes(data, "raw_account_data"),
        account_write_version=_optional_int(data, "account_write_version"),
        source_update_kind=source_update_kind,
        raw_source_status=_optional_int(data, "raw_source_status"),
        raw_source_payload=_optional_bytes(data, "raw_source_payload"),
        decoder_name=_optional_str(data, "decoder_name"),
        decoder_version=_optional_str(data, "decoder_version"),
        idl_hash=_optional_str(data, "idl_hash"),
    )


def _require_mapping(payload: object) -> Mapping[str, object]:
    if not isinstance(payload, dict):
        raise ObservationDecodeError.invalid_json_object()
    return cast("Mapping[str, object]", payload)


def _required_str(data: Mapping[str, object], field_name: str) -> str:
    value = data.get(field_name)
    if not isinstance(value, str):
        raise ObservationDecodeError.missing_field(field_name)
    return value


def _optional_str(data: Mapping[str, object], field_name: str) -> str | None:
    value = data.get(field_name)
    if value is None:
        return None
    if not isinstance(value, str):
        raise ObservationDecodeError.missing_field(field_name)
    return value


def _required_int(data: Mapping[str, object], field_name: str) -> int:
    value = data.get(field_name)
    if type(value) is not int:
        raise ObservationDecodeError.missing_field(field_name)
    return value


def _optional_int(data: Mapping[str, object], field_name: str) -> int | None:
    value = data.get(field_name)
    if value is None:
        return None
    if type(value) is not int:
        raise ObservationDecodeError.missing_field(field_name)
    return value


def _required_commitment(data: Mapping[str, object]) -> Commitment:
    value = _required_enum(data, "commitment", _COMMITMENTS)
    return cast("Commitment", value)


def _required_canonical_status(data: Mapping[str, object]) -> CanonicalStatus:
    value = _required_enum(data, "canonical_status", _CANONICAL_STATUSES)
    return cast("CanonicalStatus", value)


def _required_enum(
    data: Mapping[str, object],
    field_name: str,
    allowed_values: set[str],
) -> str:
    value = _required_str(data, field_name)
    if value not in allowed_values:
        raise ObservationDecodeError.invalid_enum(field_name)
    return value


def _optional_bytes(data: Mapping[str, object], field_name: str) -> bytes | None:
    value = data.get(field_name)
    if value is None:
        return None
    if not isinstance(value, str):
        raise ObservationDecodeError.missing_field(field_name)
    return base64.b64decode(value.encode("ascii"), validate=True)


def _required_optional_bytes(
    data: Mapping[str, object], field_name: str
) -> bytes | None:
    if field_name not in data:
        raise ObservationDecodeError.missing_field(field_name)
    return _optional_bytes(data, field_name)


def _encode_bytes(value: bytes | None) -> Literal[None] | str:
    if value is None:
        return None
    return base64.b64encode(value).decode("ascii")


def _payload_hash(payload: bytes | None) -> str | None:
    if payload is None:
        return None
    return hashlib.sha256(payload).hexdigest()
