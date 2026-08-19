"""Focused safety checks for the disabled-by-default live adapter path."""

import asyncio
import unittest

from rugbot.execution.live import LivePumpExecutionPort
from rugbot.execution.ports import ExecutionMode


class LiveExecutionTests(unittest.TestCase):
    """Check fee typing and fail-closed validation without using a signer."""

    def test_live_adapter_rejects_malformed_signing_key(self) -> None:
        with self.assertRaisesRegex(ValueError, "valid Solana keypair"):
            LivePumpExecutionPort("https://rpc.example", "test-key")

    def test_malformed_intent_returns_non_submitting_receipt(self) -> None:
        adapter = object.__new__(LivePumpExecutionPort)

        receipt = asyncio.run(adapter.submit(object()))

        self.assertEqual(receipt.mode, ExecutionMode.LIVE)
        self.assertFalse(receipt.accepted)
        self.assertFalse(receipt.would_submit_transaction)
        self.assertIsNone(receipt.signature)
