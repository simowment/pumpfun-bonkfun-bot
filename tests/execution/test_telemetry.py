"""Unit and integration tests for execution telemetry and slot-delta metrics."""

# ruff: noqa: S106

import unittest

from rugbot.execution.telemetry import ExecutionMetrics


class TestExecutionMetrics(unittest.TestCase):
    """Test telemetry calculations, milestones, and slot classification."""

    def test_hot_path_and_observed_calculation(self) -> None:
        metrics = ExecutionMetrics(
            target_wallet="FaBGrHWjcJ8vKnbgUtsdpZjvF7YAAajtQTWmmEHiKtQr",
            token_mint="DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263",
            event_slot=439958750,
            creation_slot=439958750,
            t_received_ns=1_000_000_000,
            t_decoded_ns=1_000_800_000,
            t_matched_ns=1_001_200_000,
            t_built_ns=1_002_000_000,
            t_signed_ns=1_003_000_000,
            first_observed_ns=1_180_000_000,
            landed_slot=439958750,
        )

        self.assertAlmostEqual(metrics.hot_path_ms, 3.0, places=2)
        self.assertAlmostEqual(metrics.observed_latency_ms, 180.0, places=2)
        self.assertEqual(metrics.delta_slots, 0)
        self.assertEqual(metrics.block_class, "B0")

    def test_slot_delta_classification(self) -> None:
        # B0: Landed in exact same block as creation
        b0 = ExecutionMetrics(
            target_wallet="W1",
            token_mint="M1",
            creation_slot=100,
            landed_slot=100,
        )
        self.assertEqual(b0.delta_slots, 0)
        self.assertEqual(b0.block_class, "B0")

        # B1: Landed 1 slot after
        b1 = ExecutionMetrics(
            target_wallet="W1",
            token_mint="M1",
            creation_slot=100,
            landed_slot=101,
        )
        self.assertEqual(b1.delta_slots, 1)
        self.assertEqual(b1.block_class, "B1")

        # B2+: Landed 2 or more slots after
        b2 = ExecutionMetrics(
            target_wallet="W1",
            token_mint="M1",
            creation_slot=100,
            landed_slot=103,
        )
        self.assertEqual(b2.delta_slots, 3)
        self.assertEqual(b2.block_class, "B2+")

        # Invalid: Landed before creation (clock/data anomaly)
        invalid = ExecutionMetrics(
            target_wallet="W1",
            token_mint="M1",
            creation_slot=105,
            landed_slot=100,
        )
        self.assertEqual(invalid.delta_slots, -5)
        self.assertEqual(invalid.block_class, "INVALID")

        # Incomplete / Unlanded
        unlanded = ExecutionMetrics(
            target_wallet="W1",
            token_mint="M1",
            creation_slot=100,
            landed_slot=None,
        )
        self.assertIsNone(unlanded.delta_slots)
        self.assertIsNone(unlanded.block_class)
