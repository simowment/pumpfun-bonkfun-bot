"""Focused regression guards for the unified SQLite watcher state."""

import sqlite3
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from uuid import UUID, uuid4

from rugbot.domain.observations import RawChainObservation
from rugbot.execution.position_runtime import PaperPositionState
from rugbot.ingest.checkpoints import SourceCheckpoint
from rugbot.storage.jsonl_observation_store import observation_identity
from rugbot.storage.paper_position_store import PaperPositionStoreError
from rugbot.storage.sqlite_state_store import SqliteStateStore


class SqliteStateStoreTests(unittest.TestCase):
    """Verify restart durability and strict derived-state boundaries."""

    def test_restart_persists_checkpoint_identity_and_position(self) -> None:
        """One database survives restart for all three derived-state contracts."""

        observation = _observation()
        identity = observation_identity(observation)
        position = PaperPositionState(
            as_of_slot=100,
            market_id="market-a",
            original_position_base_units=100,
            current_position_base_units=100,
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.sqlite3"
            store = SqliteStateStore(path)
            store.save(SourceCheckpoint("source-a", 100, 1))
            self.assertTrue(store.append(identity))
            self.assertFalse(store.append(identity))
            store.save(position)
            store.close()

            restarted = SqliteStateStore(path)
            self.assertEqual(
                restarted.load("source-a"), SourceCheckpoint("source-a", 100, 1)
            )
            changed_runtime_metadata = replace(
                observation,
                raw_id=uuid4(),
                observer_id="new-observer",
                boot_id=uuid4(),
                receive_sequence=99,
            )
            self.assertTrue(
                restarted.contains(observation_identity(changed_runtime_metadata))
            )
            self.assertEqual(restarted.get("market-a"), position)
            connection = sqlite3.connect(path)
            handled_rows = connection.execute(
                "SELECT identity_json FROM handled_evidence"
            ).fetchall()
            connection.close()
            self.assertNotIn(str(observation.raw_id), repr(handled_rows))
            restarted.close()

    def test_malformed_position_state_is_rejected(self) -> None:
        """A corrupt derived row fails closed instead of being ignored."""

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.sqlite3"
            store = SqliteStateStore(path)
            store.close()
            connection = sqlite3.connect(path)
            connection.execute(
                "INSERT INTO positions(market_id, state_json) VALUES (?, ?)",
                ("market-a", '{"unexpected":true}'),
            )
            connection.commit()
            connection.close()

            malformed_store = SqliteStateStore(path)
            try:
                with self.assertRaises(PaperPositionStoreError):
                    malformed_store.read_all()
            finally:
                malformed_store.close()


def _observation() -> RawChainObservation:
    """Build one immutable observation whose identity excludes runtime UUIDs."""

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
