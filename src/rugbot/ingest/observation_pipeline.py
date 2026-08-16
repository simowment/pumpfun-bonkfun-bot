"""Durable raw-observation ingestion pipeline helpers."""

from typing import Protocol

from rugbot.domain.observations import RawChainObservation
from rugbot.ingest.checkpoints import SourceCheckpoint
from rugbot.storage.jsonl_observation_store import (
    ExistingObservationStore,
    ObservationStore,
)

_ObservationEvidenceIdentity = tuple[
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
    bytes | None,
    bytes | None,
    bytes | None,
]


class ObservationPipelineError(ValueError):
    """Raised when durable observation pipeline invariants are violated."""

    @classmethod
    def existing_observation_mismatch(cls) -> "ObservationPipelineError":
        """Build an existing-observation identity mismatch error."""

        return cls("existing observation identity mismatch")


class CheckpointWriter(Protocol):
    """Checkpoint writer contract used by ingestion pipelines."""

    def save(self, checkpoint: SourceCheckpoint) -> None:
        """Persist one source checkpoint."""


class DurableObservationIngestor:
    """Persist raw observations before advancing terminal source checkpoints."""

    def __init__(
        self,
        *,
        observation_store: ObservationStore,
        checkpoint_writer: CheckpointWriter,
    ) -> None:
        """Initialize the durable ingestion helper."""

        self._observation_store = observation_store
        self._checkpoint_writer = checkpoint_writer

    def persist_observation(self, observation: RawChainObservation) -> bool:
        """Persist an observation and checkpoint only terminal slot evidence.

        Args:
            observation: Raw observation to append before checkpointing.

        Returns:
            True when the observation store appended a new row, False when the
            same source evidence was already durable.
        """

        appended = self._observation_store.append(observation)
        checkpoint_observation = _checkpoint_observation(
            observation=observation,
            appended=appended,
            observation_store=self._observation_store,
        )
        if checkpoint_observation is not None:
            self._checkpoint_writer.save(
                SourceCheckpoint(
                    source_id=checkpoint_observation.source_id,
                    last_slot=checkpoint_observation.slot,
                    receive_sequence=checkpoint_observation.receive_sequence,
                )
            )
        return appended


def _checkpoint_observation(
    *,
    observation: RawChainObservation,
    appended: bool,
    observation_store: ObservationStore,
) -> RawChainObservation | None:
    if not _is_checkpointable_slot_observation(observation):
        return None
    if appended:
        return observation
    if isinstance(observation_store, ExistingObservationStore):
        return _validated_existing_observation(
            requested=observation,
            existing=observation_store.find_existing(observation),
        )
    return None


def _validated_existing_observation(
    *,
    requested: RawChainObservation,
    existing: RawChainObservation | None,
) -> RawChainObservation | None:
    if existing is None:
        return None
    if type(existing) is not RawChainObservation:
        raise ObservationPipelineError.existing_observation_mismatch()
    if _observation_evidence_identity(existing) != _observation_evidence_identity(
        requested
    ):
        raise ObservationPipelineError.existing_observation_mismatch()
    if type(existing.receive_sequence) is not int or existing.receive_sequence < 0:
        raise ObservationPipelineError.existing_observation_mismatch()
    return existing


def _observation_evidence_identity(
    observation: RawChainObservation,
) -> _ObservationEvidenceIdentity:
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
        observation.raw_source_payload,
        observation.raw_transaction,
        observation.raw_account_data,
    )


def _is_checkpointable_slot_observation(observation: RawChainObservation) -> bool:
    return observation.source_update_kind == "slot" and _is_terminal_slot_observation(
        observation
    )


def _is_terminal_slot_observation(observation: RawChainObservation) -> bool:
    return (
        observation.commitment == "finalized"
        and observation.canonical_status == "canonical"
    ) or observation.canonical_status == "dead_fork"
