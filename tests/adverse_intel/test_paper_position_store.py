"""Focused regression guards for durable paper position storage."""

import json
import tempfile
import unittest
from dataclasses import FrozenInstanceError, replace
from pathlib import Path
from unittest.mock import patch

from rugbot.decision.playbook_rules import ExitRuleState
from rugbot.domain.amounts import Slot, TokenBaseUnits
from rugbot.execution.position_runtime import PaperPositionState
from rugbot.storage.paper_position_store import (
    PaperPositionStore,
    PaperPositionStoreError,
)


class PaperPositionStoreTests(unittest.TestCase):
    """Guard restart durability, strict decoding, and atomic replacement."""

    def test_restart_round_trip_uses_market_identity_and_frozen_records(self) -> None:
        """A replacement survives restart without runtime or UUID identity."""

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "positions.json"
            store = PaperPositionStore(path)
            initial = _position("market-a")
            advanced = replace(
                initial,
                as_of_slot=Slot(101),
                current_position_base_units=TokenBaseUnits(60),
                peak_pnl_ppm=250_000,
                exit_rule_state=ExitRuleState(
                    filled_take_profit_level_indices=(0,),
                    exited_fraction_ppm=400_000,
                ),
                emitted_sell_intent_count=1,
            )

            store.save(initial)
            store.save(advanced)
            restored = PaperPositionStore(path).get("market-a")

            self.assertEqual(restored, advanced)
            self.assertEqual(PaperPositionStore(path).read_all(), (advanced,))
            payload = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(payload[0]["market_id"], "market-a")
            self.assertNotIn("raw_id", payload[0])
            self.assertNotIn("uuid", path.read_text(encoding="utf-8").lower())
            with self.assertRaises(FrozenInstanceError):
                restored.current_position_base_units = TokenBaseUnits(1)  # type: ignore[union-attr,misc]

    def test_malformed_state_is_rejected_strictly(self) -> None:
        """Truncation, duplicate keys, extra fields, and bad values fail closed."""

        valid_record = _record()
        malformed_payloads = (
            b'[{"market_id":"market-a"}',
            b'[{"market_id":"a","market_id":"b"}]\n',
            json.dumps([{**valid_record, "unexpected": True}]).encode(),
            json.dumps([valid_record, valid_record]).encode(),
            json.dumps([{**valid_record, "as_of_slot": True}]).encode(),
            json.dumps(
                [
                    {
                        **valid_record,
                        "exit_rule_state": {
                            **valid_record["exit_rule_state"],
                            "exited_fraction_ppm": 1_000_001,
                        },
                    }
                ]
            ).encode(),
        )

        for payload in malformed_payloads:
            with self.subTest(payload=payload):
                with tempfile.TemporaryDirectory() as directory:
                    path = Path(directory) / "positions.json"
                    path.write_bytes(payload)
                    with self.assertRaises(PaperPositionStoreError):
                        PaperPositionStore(path).read_all()

    def test_failed_atomic_replace_preserves_previous_snapshot(self) -> None:
        """A failed replacement leaves the last durable snapshot readable."""

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "positions.json"
            store = PaperPositionStore(path)
            initial = _position("market-a")
            store.save(initial)

            with (
                patch(
                    "pathlib.Path.replace",
                    side_effect=OSError("replace failed"),
                ),
                self.assertRaises(PaperPositionStoreError),
            ):
                store.save(replace(initial, as_of_slot=Slot(101)))

            self.assertEqual(PaperPositionStore(path).get("market-a"), initial)
            self.assertFalse(path.with_name(".positions.json.tmp").exists())

    def test_remove_survives_restart_without_affecting_other_markets(self) -> None:
        """Removing one canonical market is durable and narrowly scoped."""

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "positions.json"
            store = PaperPositionStore(path)
            first = _position("market-a")
            second = _position("market-b")
            store.save(second)
            store.save(first)

            self.assertTrue(store.remove("market-a"))
            self.assertFalse(store.remove("market-a"))
            self.assertEqual(PaperPositionStore(path).read_all(), (second,))


def _position(market_id: str) -> PaperPositionState:
    return PaperPositionState(
        as_of_slot=Slot(100),
        market_id=market_id,
        original_position_base_units=TokenBaseUnits(100),
        current_position_base_units=TokenBaseUnits(100),
    )


def _record() -> dict[str, object]:
    return {
        "as_of_slot": 100,
        "market_id": "market-a",
        "original_position_base_units": 100,
        "current_position_base_units": 100,
        "peak_pnl_ppm": 0,
        "exit_rule_state": {
            "filled_take_profit_level_indices": [],
            "filled_stop_loss_level_indices": [],
            "filled_big_buy_level_indices": [],
            "exited_fraction_ppm": 0,
        },
        "emitted_sell_intent_count": 0,
    }


if __name__ == "__main__":
    unittest.main()
