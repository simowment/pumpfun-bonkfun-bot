"""Focused durability and validation tests for handled evidence."""

import os
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch
from uuid import UUID, uuid4

from rugbot.domain.observations import RawChainObservation
from rugbot.storage.handled_evidence_ledger import (
    HandledEvidenceLedgerError,
    JsonlHandledEvidenceLedger,
)
from rugbot.storage.jsonl_observation_store import observation_identity


class HandledEvidenceLedgerTests(unittest.TestCase):
    """Verify strict durable identity storage independent of raw observations."""

    def test_identity_round_trip_survives_restart_without_raw_uuid(self) -> None:
        """A canonical identity remains handled across a new ledger instance."""

        observation = _observation()
        identity = observation_identity(observation)
        changed_runtime_metadata = replace(
            observation,
            raw_id=uuid4(),
            observer_id="new-observer",
            boot_id=uuid4(),
            receive_sequence=99,
            received_wall_ns=99,
            received_monotonic_ns=99,
        )

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "handled.jsonl"
            ledger = JsonlHandledEvidenceLedger(path)
            self.assertTrue(ledger.append(identity))
            self.assertFalse(
                ledger.append(observation_identity(changed_runtime_metadata))
            )
            self.assertTrue(
                JsonlHandledEvidenceLedger(path).contains(
                    observation_identity(changed_runtime_metadata)
                )
            )
            self.assertNotIn(str(observation.raw_id), path.read_text(encoding="utf-8"))

    def test_malformed_state_is_rejected(self) -> None:
        """Truncated, duplicate-key, and unexpected-field state fails closed."""

        malformed_lines = (
            b'{"source_id":"x"}',
            b'{"source_id":"x","source_id":"y"}\n',
            b'{"unexpected":true}\n',
        )
        for malformed_line in malformed_lines:
            with self.subTest(malformed_line=malformed_line):
                with tempfile.TemporaryDirectory() as directory:
                    path = Path(directory) / "handled.jsonl"
                    path.write_bytes(malformed_line)
                    with self.assertRaises(HandledEvidenceLedgerError):
                        JsonlHandledEvidenceLedger(path).contains(
                            observation_identity(_observation())
                        )

    def test_append_does_not_mutate_raw_observation(self) -> None:
        """Appending only extracts identity data from the frozen raw object."""

        observation = _observation()
        with tempfile.TemporaryDirectory() as directory:
            ledger = JsonlHandledEvidenceLedger(Path(directory) / "handled.jsonl")
            ledger.append(observation_identity(observation))

        self.assertEqual(
            observation.raw_id, UUID("00000000-0000-0000-0000-000000000020")
        )
        self.assertEqual(observation, _observation())

    def test_partial_append_rolls_back_before_failing(self) -> None:
        """A short write leaves no malformed ledger record behind."""

        observation = _observation()
        identity = observation_identity(observation)
        path: Path
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "handled.jsonl"
            real_write = os.write

            def partial_write(descriptor: int, data: bytes) -> int:
                return real_write(descriptor, data[:-1])

            with patch(
                "rugbot.storage.handled_evidence_ledger.os.write",
                side_effect=partial_write,
            ):
                with self.assertRaises(HandledEvidenceLedgerError):
                    JsonlHandledEvidenceLedger(path).append(identity)

            self.assertEqual(path.read_bytes(), b"")


def _observation() -> RawChainObservation:
    return RawChainObservation(
        raw_id=UUID("00000000-0000-0000-0000-000000000020"),
        source_id="test-source",
        observer_id="test-observer",
        boot_id=UUID("00000000-0000-0000-0000-000000000001"),
        receive_sequence=1,
        slot=100,
        parent_slot=99,
        blockhash=None,
        signature=b"signature",
        transaction_index=None,
        outer_instruction_index=None,
        inner_instruction_group_index=None,
        inner_instruction_index=None,
        stack_height=None,
        event_ordinal=1,
        commitment="finalized",
        canonical_status="canonical",
        received_wall_ns=1,
        received_monotonic_ns=1,
        program_id=None,
        account_pubkey=None,
        account_owner_program_id=None,
        raw_transaction=None,
        raw_transaction_format=None,
        raw_account_data=None,
        account_write_version=None,
        source_update_kind="slot",
        raw_source_status=None,
        raw_source_payload=b"slot",
        decoder_name=None,
        decoder_version=None,
        idl_hash=None,
    )


if __name__ == "__main__":
    unittest.main()
