"""Append-only observation store tests."""

import json
import unittest
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import UUID

from rugbot.domain.observations import RawChainObservation
from rugbot.storage.jsonl_observation_store import (
    JsonlObservationStore,
    ObservationDecodeError,
)

BOOT_ID = UUID("00000000-0000-0000-0000-000000000001")
RAW_ID = UUID("00000000-0000-0000-0000-000000000010")


class JsonlObservationStoreTests(unittest.TestCase):
    """Tests for append-only raw observation persistence."""

    def test_append_and_read_preserves_raw_bytes(self) -> None:
        """Stored observations round-trip with raw byte fields intact."""

        with TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "observations.jsonl"
            store = JsonlObservationStore(path)
            observation = _raw_observation(slot=100, receive_sequence=1)

            store.append(observation)
            stored = store.read_all()

        self.assertEqual(stored, [observation])
        self.assertEqual(stored[0].raw_transaction, b"transaction-bytes")
        self.assertEqual(stored[0].raw_transaction_format, "test-transaction-bytes")
        self.assertEqual(stored[0].account_pubkey, b"account-pubkey")
        self.assertEqual(stored[0].account_owner_program_id, b"account-owner")
        self.assertEqual(stored[0].raw_source_payload, b"source-payload")

    def test_append_only_adds_new_lines_without_overwriting(self) -> None:
        """Multiple appends preserve existing records in order."""

        with TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "observations.jsonl"
            store = JsonlObservationStore(path)

            first = _raw_observation(slot=100, receive_sequence=1)
            second = _raw_observation(slot=101, receive_sequence=2)
            store.append(first)
            store.append(second)

            lines = path.read_text(encoding="utf-8").splitlines()
            stored = store.read_all()

        self.assertEqual(len(lines), 2)
        self.assertEqual(stored, [first, second])
        decoded_line = json.loads(lines[0])
        self.assertEqual(decoded_line["schema_version"], 4)
        self.assertIsInstance(decoded_line["account_pubkey"], str)
        self.assertIsInstance(decoded_line["account_owner_program_id"], str)
        self.assertIsInstance(decoded_line["raw_transaction"], str)
        self.assertEqual(
            decoded_line["raw_transaction_format"], "test-transaction-bytes"
        )

    def test_duplicate_source_evidence_is_idempotent_after_reopen(self) -> None:
        """Repeated delivery of the same source payload appends only once."""

        with TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "observations.jsonl"
            first_store = JsonlObservationStore(path)
            observation = _raw_observation(slot=100, receive_sequence=1)
            duplicate = _raw_observation(slot=100, receive_sequence=2)

            self.assertTrue(first_store.append(observation))
            reopened_store = JsonlObservationStore(path)
            self.assertFalse(reopened_store.append(duplicate))

            stored = reopened_store.read_all()

        self.assertEqual(stored, [observation])

    def test_same_slot_status_with_different_payload_is_distinct(self) -> None:
        """Different raw payloads for one slot/status remain separate evidence."""

        with TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "observations.jsonl"
            store = JsonlObservationStore(path)
            first = _raw_observation(
                slot=100,
                receive_sequence=1,
                overrides=_ObservationOverrides(raw_source_payload=b"payload-one"),
            )
            second = _raw_observation(
                slot=100,
                receive_sequence=2,
                overrides=_ObservationOverrides(raw_source_payload=b"payload-two"),
            )

            self.assertTrue(store.append(first))
            self.assertTrue(store.append(second))
            stored = store.read_all()

        self.assertEqual(stored, [first, second])

    def test_account_pubkey_distinguishes_account_update_identity(self) -> None:
        """Different accounts with equal source/update bytes are distinct evidence."""

        with TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "observations.jsonl"
            store = JsonlObservationStore(path)
            first = _raw_observation(
                slot=100,
                receive_sequence=1,
                overrides=_ObservationOverrides(
                    account_pubkey=b"account-one",
                    raw_source_payload=b"same-payload",
                ),
            )
            second = _raw_observation(
                slot=100,
                receive_sequence=2,
                overrides=_ObservationOverrides(
                    account_pubkey=b"account-two",
                    raw_source_payload=b"same-payload",
                ),
            )

            self.assertTrue(store.append(first))
            self.assertTrue(store.append(second))
            stored = store.read_all()

        self.assertEqual(stored, [first, second])

    def test_account_owner_distinguishes_account_update_identity(self) -> None:
        """Different account owners remain distinct raw source evidence."""

        with TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "observations.jsonl"
            store = JsonlObservationStore(path)
            first = _raw_observation(
                slot=100,
                receive_sequence=1,
                overrides=_ObservationOverrides(
                    account_owner_program_id=b"owner-one",
                    raw_source_payload=b"same-payload",
                ),
            )
            second = _raw_observation(
                slot=100,
                receive_sequence=2,
                overrides=_ObservationOverrides(
                    account_owner_program_id=b"owner-two",
                    raw_source_payload=b"same-payload",
                ),
            )

            self.assertTrue(store.append(first))
            self.assertTrue(store.append(second))
            stored = store.read_all()

        self.assertEqual(stored, [first, second])

    def test_account_commitment_distinguishes_account_update_identity(self) -> None:
        """Processed and finalized account evidence both persist."""

        with TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "observations.jsonl"
            store = JsonlObservationStore(path)
            processed = _raw_observation(
                slot=100,
                receive_sequence=1,
                overrides=_ObservationOverrides(
                    commitment="processed",
                    canonical_status="provisional",
                    source_update_kind="account",
                    raw_source_payload=b"same-account-payload",
                ),
            )
            finalized = _raw_observation(
                slot=100,
                receive_sequence=2,
                overrides=_ObservationOverrides(
                    commitment="finalized",
                    canonical_status="canonical",
                    source_update_kind="account",
                    raw_source_payload=b"same-account-payload",
                ),
            )

            self.assertTrue(store.append(processed))
            self.assertTrue(store.append(finalized))
            self.assertFalse(store.append(finalized))
            stored = store.read_all()

        self.assertEqual(stored, [processed, finalized])

    def test_legacy_schema_without_account_pubkey_abstains(self) -> None:
        """Rows from an older raw contract are rejected without migration."""

        with TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "observations.jsonl"
            store = JsonlObservationStore(path)
            observation = _raw_observation(slot=100, receive_sequence=1)
            store.append(observation)

            payload = json.loads(path.read_text(encoding="utf-8").strip())
            payload["schema_version"] = 1
            del payload["account_pubkey"]
            del payload["account_owner_program_id"]
            path.write_text(json.dumps(payload) + "\n", encoding="utf-8")

            with self.assertRaises(ObservationDecodeError):
                store.read_all()

    def test_schema_v2_without_account_owner_abstains(self) -> None:
        """Rows without the current raw contract are rejected."""

        with TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "observations.jsonl"
            store = JsonlObservationStore(path)
            observation = _raw_observation(slot=100, receive_sequence=1)
            store.append(observation)

            payload = json.loads(path.read_text(encoding="utf-8").strip())
            payload["schema_version"] = 2
            del payload["account_owner_program_id"]
            path.write_text(json.dumps(payload) + "\n", encoding="utf-8")

            with self.assertRaises(ObservationDecodeError):
                store.read_all()

    def test_schema_v2_account_owner_migration_is_removed(self) -> None:
        """Legacy owner inference is not part of the current raw contract."""

        with TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "observations.jsonl"
            store = JsonlObservationStore(path)
            observation = _raw_observation(
                slot=100,
                receive_sequence=1,
                overrides=_ObservationOverrides(source_update_kind="account"),
            )
            store.append(observation)

            payload = json.loads(path.read_text(encoding="utf-8").strip())
            payload["schema_version"] = 2
            del payload["account_owner_program_id"]
            path.write_text(json.dumps(payload) + "\n", encoding="utf-8")

            with self.assertRaises(ObservationDecodeError):
                store.read_all()

    def test_schema_v3_without_transaction_format_abstains(self) -> None:
        """Rows without the current transaction format field are rejected."""

        with TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "observations.jsonl"
            store = JsonlObservationStore(path)
            observation = _raw_observation(slot=100, receive_sequence=1)
            store.append(observation)

            payload = json.loads(path.read_text(encoding="utf-8").strip())
            payload["schema_version"] = 3
            del payload["raw_transaction_format"]
            path.write_text(json.dumps(payload) + "\n", encoding="utf-8")

            with self.assertRaises(ObservationDecodeError):
                store.read_all()

    def test_boolean_integer_fields_are_rejected(self) -> None:
        """JSON booleans cannot satisfy integer observation fields."""

        with TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "observations.jsonl"
            store = JsonlObservationStore(path)
            store.append(_raw_observation(slot=100, receive_sequence=1))
            payload = json.loads(path.read_text(encoding="utf-8").strip())
            payload["slot"] = True
            path.write_text(json.dumps(payload) + "\n", encoding="utf-8")

            with self.assertRaises(ObservationDecodeError):
                store.read_all()

    def test_current_rows_require_the_exact_field_set(self) -> None:
        """Current evidence cannot gain or lose fields silently."""

        with TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "observations.jsonl"
            store = JsonlObservationStore(path)
            store.append(_raw_observation(slot=100, receive_sequence=1))
            payload = json.loads(path.read_text(encoding="utf-8").strip())
            payload["unexpected"] = "rejected"
            path.write_text(json.dumps(payload) + "\n", encoding="utf-8")

            with self.assertRaises(ObservationDecodeError):
                store.read_all()


@dataclass(frozen=True, slots=True)
class _ObservationOverrides:
    account_pubkey: bytes | None = b"account-pubkey"
    account_owner_program_id: bytes | None = b"account-owner"
    commitment: str = "processed"
    canonical_status: str = "provisional"
    source_update_kind: str | None = "slot"
    raw_source_payload: bytes = b"source-payload"
    raw_transaction_format: str | None = "test-transaction-bytes"


def _raw_observation(
    *,
    slot: int,
    receive_sequence: int,
    overrides: _ObservationOverrides | None = None,
) -> RawChainObservation:
    values = overrides or _ObservationOverrides()
    return RawChainObservation(
        raw_id=RAW_ID,
        source_id="geyser-main",
        observer_id="observer-1",
        boot_id=BOOT_ID,
        receive_sequence=receive_sequence,
        slot=slot,
        parent_slot=slot - 1,
        blockhash=b"blockhash",
        signature=b"signature",
        transaction_index=1,
        outer_instruction_index=2,
        inner_instruction_group_index=3,
        inner_instruction_index=4,
        stack_height=5,
        event_ordinal=6,
        commitment=values.commitment,
        canonical_status=values.canonical_status,
        received_wall_ns=10,
        received_monotonic_ns=20,
        program_id=b"program",
        account_pubkey=values.account_pubkey,
        account_owner_program_id=values.account_owner_program_id,
        raw_transaction=b"transaction-bytes",
        raw_transaction_format=values.raw_transaction_format,
        raw_account_data=b"account-bytes",
        account_write_version=7,
        source_update_kind=values.source_update_kind,
        raw_source_status=8,
        raw_source_payload=values.raw_source_payload,
        decoder_name="decoder",
        decoder_version="decoder-v1",
        idl_hash="idl-hash",
    )


if __name__ == "__main__":
    unittest.main()
