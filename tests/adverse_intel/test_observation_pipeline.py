"""Durable observation ingestion pipeline tests."""

import unittest
from uuid import UUID

from rugbot.domain.observations import RawChainObservation
from rugbot.ingest.checkpoints import SourceCheckpoint
from rugbot.ingest.observation_pipeline import (
    DurableObservationIngestor,
    ObservationPipelineError,
)

BOOT_ID = UUID("00000000-0000-0000-0000-000000000001")
RAW_ID = UUID("00000000-0000-0000-0000-000000000020")


class DurableObservationIngestorTests(unittest.TestCase):
    """Tests for observation-first checkpoint ordering."""

    def test_terminal_observation_is_appended_before_checkpoint(self) -> None:
        """Terminal slot checkpoints advance only after raw observation append."""

        calls: list[str] = []
        store = _RecordingObservationStore(calls)
        checkpoints = _RecordingCheckpointWriter(calls)
        ingestor = DurableObservationIngestor(
            observation_store=store,
            checkpoint_writer=checkpoints,
        )
        observation = _raw_observation(
            commitment="finalized",
            canonical_status="canonical",
        )

        appended = ingestor.persist_observation(observation)

        self.assertTrue(appended)
        self.assertEqual(calls, ["append", "checkpoint"])
        self.assertEqual(store.observations, [observation])
        self.assertEqual(
            checkpoints.checkpoints,
            [
                SourceCheckpoint(
                    source_id="geyser-main",
                    last_slot=100,
                    receive_sequence=3,
                )
            ],
        )

    def test_provisional_observation_does_not_checkpoint(self) -> None:
        """Processed provisional observations are durable but not safe checkpoints."""

        calls: list[str] = []
        store = _RecordingObservationStore(calls)
        checkpoints = _RecordingCheckpointWriter(calls)
        ingestor = DurableObservationIngestor(
            observation_store=store,
            checkpoint_writer=checkpoints,
        )
        observation = _raw_observation(canonical_status="provisional")

        appended = ingestor.persist_observation(observation)

        self.assertTrue(appended)
        self.assertEqual(calls, ["append"])
        self.assertEqual(checkpoints.checkpoints, [])

    def test_processed_canonical_slot_does_not_checkpoint(self) -> None:
        """Malformed processed/canonical slot rows do not advance checkpoints."""

        calls: list[str] = []
        store = _RecordingObservationStore(calls)
        checkpoints = _RecordingCheckpointWriter(calls)
        ingestor = DurableObservationIngestor(
            observation_store=store,
            checkpoint_writer=checkpoints,
        )
        observation = _raw_observation(
            commitment="processed",
            canonical_status="canonical",
        )

        appended = ingestor.persist_observation(observation)

        self.assertTrue(appended)
        self.assertEqual(calls, ["append"])
        self.assertEqual(checkpoints.checkpoints, [])

    def test_duplicate_terminal_evidence_does_not_advance_checkpoint(self) -> None:
        """Duplicate terminal evidence cannot checkpoint an undurable sequence."""

        calls: list[str] = []
        store = _RecordingObservationStore(calls, append_result=False)
        checkpoints = _RecordingCheckpointWriter(calls)
        ingestor = DurableObservationIngestor(
            observation_store=store,
            checkpoint_writer=checkpoints,
        )
        observation = _raw_observation(canonical_status="dead_fork")

        appended = ingestor.persist_observation(observation)

        self.assertFalse(appended)
        self.assertEqual(calls, ["append"])
        self.assertEqual(checkpoints.checkpoints, [])

    def test_mismatched_existing_duplicate_repair_fails_closed(self) -> None:
        """Custom duplicate repair stores cannot checkpoint arbitrary rows."""

        calls: list[str] = []
        store = _MismatchedExistingObservationStore(
            calls,
            existing=_raw_observation(canonical_status="dead_fork", slot=101),
        )
        checkpoints = _RecordingCheckpointWriter(calls)
        ingestor = DurableObservationIngestor(
            observation_store=store,
            checkpoint_writer=checkpoints,
        )
        observation = _raw_observation(canonical_status="dead_fork", slot=100)

        with self.assertRaises(ObservationPipelineError):
            ingestor.persist_observation(observation)

        self.assertEqual(calls, ["append", "find_existing"])
        self.assertEqual(checkpoints.checkpoints, [])

    def test_terminal_account_observation_does_not_checkpoint(self) -> None:
        """Account stream finality does not advance slot-stream checkpoints."""

        calls: list[str] = []
        store = _RecordingObservationStore(calls)
        checkpoints = _RecordingCheckpointWriter(calls)
        ingestor = DurableObservationIngestor(
            observation_store=store,
            checkpoint_writer=checkpoints,
        )
        observation = _raw_observation(
            canonical_status="canonical",
            source_update_kind="account",
        )

        appended = ingestor.persist_observation(observation)

        self.assertTrue(appended)
        self.assertEqual(calls, ["append"])
        self.assertEqual(checkpoints.checkpoints, [])


class _RecordingObservationStore:
    def __init__(self, calls: list[str], *, append_result: bool = True) -> None:
        self._calls = calls
        self._append_result = append_result
        self.observations: list[RawChainObservation] = []

    def append(self, observation: RawChainObservation) -> bool:
        self._calls.append("append")
        self.observations.append(observation)
        return self._append_result


class _MismatchedExistingObservationStore:
    def __init__(
        self,
        calls: list[str],
        *,
        existing: RawChainObservation,
    ) -> None:
        self._calls = calls
        self._existing = existing

    def append(self, _observation: RawChainObservation) -> bool:
        self._calls.append("append")
        return False

    def find_existing(
        self,
        _observation: RawChainObservation,
    ) -> RawChainObservation | None:
        self._calls.append("find_existing")
        return self._existing


class _RecordingCheckpointWriter:
    def __init__(self, calls: list[str]) -> None:
        self._calls = calls
        self.checkpoints: list[SourceCheckpoint] = []

    def save(self, checkpoint: SourceCheckpoint) -> None:
        self._calls.append("checkpoint")
        self.checkpoints.append(checkpoint)


def _raw_observation(
    *,
    commitment: str = "processed",
    canonical_status: str,
    slot: int = 100,
    receive_sequence: int = 3,
    source_update_kind: str | None = "slot",
) -> RawChainObservation:
    return RawChainObservation(
        raw_id=RAW_ID,
        source_id="geyser-main",
        observer_id="observer-1",
        boot_id=BOOT_ID,
        receive_sequence=receive_sequence,
        slot=slot,
        parent_slot=slot - 1,
        blockhash=None,
        signature=None,
        transaction_index=None,
        outer_instruction_index=None,
        inner_instruction_group_index=None,
        inner_instruction_index=None,
        stack_height=None,
        event_ordinal=None,
        commitment=commitment,
        canonical_status=canonical_status,
        received_wall_ns=10,
        received_monotonic_ns=20,
        program_id=None,
        account_pubkey=None,
        account_owner_program_id=None,
        raw_transaction=None,
        raw_transaction_format=None,
        raw_account_data=None,
        account_write_version=None,
        source_update_kind=source_update_kind,
        raw_source_status=1,
        raw_source_payload=b"slot-update",
        decoder_name=None,
        decoder_version=None,
        idl_hash=None,
    )


if __name__ == "__main__":
    unittest.main()
