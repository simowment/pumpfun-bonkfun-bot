"""Checkpoint store tests."""

import ast
import json
import unittest
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, cast

from rugbot.ingest.checkpoints import (
    CHECKPOINT_STORE_SCHEMA_VERSION,
    CheckpointStoreError,
    JsonCheckpointStore,
    SourceCheckpoint,
)

CHECKPOINT_MODULE = Path("src/rugbot/ingest/checkpoints.py")
FORBIDDEN_IMPORT_PREFIXES = (
    "requests",
    "aiohttp",
    "httpx",
    "grpc",
    "sqlite",
    "psycopg",
    "rugbot.execution",
    "rugbot.protocol",
    "rugbot.storage",
    "src.core",
    "src.trading",
    "src.platforms",
    "solana",
    "solders",
    "dotenv",
)


class JsonCheckpointStoreTests(unittest.TestCase):
    """Tests for JSON checkpoint persistence."""

    def test_save_and_load_checkpoint(self) -> None:
        """Saved checkpoints can be loaded by source ID."""

        with TemporaryDirectory() as temp_dir:
            checkpoint_path = Path(temp_dir) / "checkpoints.json"
            store = JsonCheckpointStore(checkpoint_path)

            checkpoint = SourceCheckpoint(
                source_id="geyser-main",
                last_slot=42,
                receive_sequence=7,
            )
            store.save(checkpoint)

            loaded = store.load("geyser-main")

        self.assertEqual(checkpoint, loaded)

    def test_save_writes_versioned_schema(self) -> None:
        """New checkpoint writes use an explicit schema version."""

        with TemporaryDirectory() as temp_dir:
            checkpoint_path = Path(temp_dir) / "checkpoints.json"
            store = JsonCheckpointStore(checkpoint_path)

            store.save(
                SourceCheckpoint(
                    source_id="geyser-main",
                    last_slot=42,
                    receive_sequence=7,
                )
            )
            payload = json.loads(checkpoint_path.read_text(encoding="utf-8"))

        self.assertEqual(payload["schema_version"], CHECKPOINT_STORE_SCHEMA_VERSION)
        self.assertEqual(
            payload["checkpoints"]["geyser-main"],
            {"last_slot": 42, "receive_sequence": 7},
        )

    def test_missing_checkpoint_returns_none(self) -> None:
        """Missing source IDs return None."""

        with TemporaryDirectory() as temp_dir:
            store = JsonCheckpointStore(Path(temp_dir) / "checkpoints.json")
            self.assertIsNone(store.load("missing"))

    def test_save_does_not_move_checkpoint_backwards(self) -> None:
        """Older checkpoints cannot overwrite newer durable positions."""

        with TemporaryDirectory() as temp_dir:
            store = JsonCheckpointStore(Path(temp_dir) / "checkpoints.json")

            newer = SourceCheckpoint(
                source_id="geyser-main",
                last_slot=50,
                receive_sequence=10,
            )
            older = SourceCheckpoint(
                source_id="geyser-main",
                last_slot=49,
                receive_sequence=99,
            )
            store.save(newer)
            store.save(older)

            loaded = store.load("geyser-main")

        self.assertEqual(newer, loaded)

    def test_save_rejects_receive_sequence_regression(self) -> None:
        """Receive sequence regression cannot overwrite a checkpoint."""

        with TemporaryDirectory() as temp_dir:
            store = JsonCheckpointStore(Path(temp_dir) / "checkpoints.json")

            newer = SourceCheckpoint(
                source_id="geyser-main",
                last_slot=50,
                receive_sequence=10,
            )
            regressed_sequence = SourceCheckpoint(
                source_id="geyser-main",
                last_slot=51,
                receive_sequence=9,
            )
            store.save(newer)
            store.save(regressed_sequence)

            loaded = store.load("geyser-main")

        self.assertEqual(newer, loaded)

    def test_loads_legacy_checkpoint_shape_strictly(self) -> None:
        """Existing bare checkpoint files remain readable when exact."""

        with TemporaryDirectory() as temp_dir:
            checkpoint_path = Path(temp_dir) / "checkpoints.json"
            checkpoint_path.write_text(
                json.dumps(
                    {
                        "geyser-main": {
                            "last_slot": 42,
                            "receive_sequence": 7,
                        }
                    }
                ),
                encoding="utf-8",
            )

            loaded = JsonCheckpointStore(checkpoint_path).load("geyser-main")

        self.assertEqual(
            loaded,
            SourceCheckpoint(
                source_id="geyser-main",
                last_slot=42,
                receive_sequence=7,
            ),
        )

    def test_save_upgrades_legacy_checkpoint_shape(self) -> None:
        """Saving over legacy state preserves entries in the versioned envelope."""

        with TemporaryDirectory() as temp_dir:
            checkpoint_path = Path(temp_dir) / "checkpoints.json"
            checkpoint_path.write_text(
                json.dumps(
                    {
                        "geyser-main": {
                            "last_slot": 42,
                            "receive_sequence": 7,
                        }
                    }
                ),
                encoding="utf-8",
            )
            store = JsonCheckpointStore(checkpoint_path)

            store.save(
                SourceCheckpoint(
                    source_id="geyser-backup",
                    last_slot=50,
                    receive_sequence=8,
                )
            )
            payload = json.loads(checkpoint_path.read_text(encoding="utf-8"))

        self.assertEqual(payload["schema_version"], CHECKPOINT_STORE_SCHEMA_VERSION)
        self.assertEqual(payload["checkpoints"]["geyser-main"]["last_slot"], 42)
        self.assertEqual(payload["checkpoints"]["geyser-backup"]["last_slot"], 50)

    def test_malformed_store_state_raises(self) -> None:
        """Malformed durable checkpoint state cannot be coerced."""

        malformed_payloads = (
            [],
            {
                "schema_version": "source-checkpoints-v0",
                "checkpoints": {},
            },
            {
                "schema_version": CHECKPOINT_STORE_SCHEMA_VERSION,
                "checkpoints": [],
            },
            {
                "schema_version": CHECKPOINT_STORE_SCHEMA_VERSION,
                "checkpoints": {
                    "geyser-main": {
                        "last_slot": "42",
                        "receive_sequence": 7,
                    }
                },
            },
            {
                "schema_version": CHECKPOINT_STORE_SCHEMA_VERSION,
                "checkpoints": {
                    "geyser-main": {
                        "last_slot": 42,
                        "receive_sequence": True,
                    }
                },
            },
            {
                "schema_version": CHECKPOINT_STORE_SCHEMA_VERSION,
                "checkpoints": {
                    "geyser-main": {
                        "last_slot": 42,
                        "receive_sequence": 7,
                        "extra": 1,
                    }
                },
            },
            {
                " ": {
                    "last_slot": 42,
                    "receive_sequence": 7,
                }
            },
        )
        for payload in malformed_payloads:
            with self.subTest(payload=payload):
                with TemporaryDirectory() as temp_dir:
                    checkpoint_path = Path(temp_dir) / "checkpoints.json"
                    checkpoint_path.write_text(json.dumps(payload), encoding="utf-8")

                    with self.assertRaises(CheckpointStoreError):
                        JsonCheckpointStore(checkpoint_path).load("geyser-main")

    def test_invalid_json_store_state_raises(self) -> None:
        """Invalid JSON does not become an empty checkpoint set."""

        with TemporaryDirectory() as temp_dir:
            checkpoint_path = Path(temp_dir) / "checkpoints.json"
            checkpoint_path.write_text("{", encoding="utf-8")

            with self.assertRaises(CheckpointStoreError):
                JsonCheckpointStore(checkpoint_path).load("geyser-main")

    def test_duplicate_json_keys_raise(self) -> None:
        """Duplicate JSON keys are malformed durable state."""

        payloads = (
            """
            {
              "schema_version": "source-checkpoints-v0",
              "schema_version": "source-checkpoints-v1",
              "checkpoints": {}
            }
            """,
            """
            {
              "schema_version": "source-checkpoints-v1",
              "checkpoints": [],
              "checkpoints": {
                "geyser-main": {
                  "last_slot": 42,
                  "receive_sequence": 7
                }
              }
            }
            """,
            """
            {
              "schema_version": "source-checkpoints-v1",
              "checkpoints": {
                "geyser-main": {
                  "last_slot": "bad",
                  "last_slot": 42,
                  "receive_sequence": 7
                }
              }
            }
            """,
        )
        for payload in payloads:
            with self.subTest(payload=payload):
                with TemporaryDirectory() as temp_dir:
                    checkpoint_path = Path(temp_dir) / "checkpoints.json"
                    checkpoint_path.write_text(payload, encoding="utf-8")

                    with self.assertRaises(CheckpointStoreError):
                        JsonCheckpointStore(checkpoint_path).load("geyser-main")

    def test_save_rejects_malformed_checkpoint(self) -> None:
        """Checkpoint objects must carry exact strict integer fields."""

        bool_slot = bool(1)
        malformed_checkpoints = (
            cast("Any", object()),
            cast("Any", object.__new__(SourceCheckpoint)),
            replace(_checkpoint(), source_id=" "),
            replace(_checkpoint(), last_slot=cast("Any", -1)),
            replace(_checkpoint(), last_slot=cast("Any", bool_slot)),
            replace(_checkpoint(), last_slot=cast("Any", 1.0)),
            replace(_checkpoint(), receive_sequence=cast("Any", "7")),
        )
        for checkpoint in malformed_checkpoints:
            with self.subTest(checkpoint=checkpoint):
                with TemporaryDirectory() as temp_dir:
                    store = JsonCheckpointStore(Path(temp_dir) / "checkpoints.json")

                    with self.assertRaises(CheckpointStoreError):
                        store.save(checkpoint)

    def test_load_rejects_malformed_requested_source_id(self) -> None:
        """Requested source IDs are exact nonblank strings."""

        with TemporaryDirectory() as temp_dir:
            store = JsonCheckpointStore(Path(temp_dir) / "checkpoints.json")

            with self.assertRaises(CheckpointStoreError):
                store.load(" ")

    def test_checkpoint_store_stays_non_trading(self) -> None:
        """Checkpoint persistence must not grow adapters or signer paths."""

        source = CHECKPOINT_MODULE.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(CHECKPOINT_MODULE))
        violations = [
            imported_name
            for imported_name in _imported_module_names(tree)
            if imported_name.startswith(FORBIDDEN_IMPORT_PREFIXES)
        ]

        self.assertEqual(violations, [])
        for token in _forbidden_source_tokens():
            self.assertNotIn(token, source)


def _checkpoint() -> SourceCheckpoint:
    return SourceCheckpoint(
        source_id="geyser-main",
        last_slot=42,
        receive_sequence=7,
    )


def _imported_module_names(tree: ast.AST) -> list[str]:
    names: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            names.append(node.module)
    return names


def _forbidden_source_tokens() -> tuple[str, ...]:
    return (
        "Key" + "pair",
        "Wal" + "let",
        "PRIVATE" + "_KEY",
        "send" + "_transaction",
        "send" + "_raw_transaction",
        "simulate" + "_transaction",
        "request" + "_airdrop",
        "float(",
    )


if __name__ == "__main__":
    unittest.main()
