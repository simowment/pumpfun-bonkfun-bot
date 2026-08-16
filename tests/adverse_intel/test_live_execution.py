"""Focused safety checks for the disabled-by-default live adapter path."""

import asyncio
import unittest
from unittest.mock import patch

from rugbot.execution.live import LivePumpExecutionPort
from rugbot.execution.ports import ExecutionMode


class LiveExecutionTests(unittest.TestCase):
    """Check fee typing and fail-closed validation without using a signer."""

    def test_priority_fee_manager_receives_integer_zero_extra_fee(self) -> None:
        with (
            patch("rugbot.execution.live.SolanaClient"),
            patch("rugbot.execution.live.Wallet"),
            patch("rugbot.execution.live.PriorityFeeManager") as priority_fee_manager,
        ):
            LivePumpExecutionPort("https://rpc.example", "test-key")

        self.assertEqual(priority_fee_manager.call_args.kwargs["extra_fee"], 0)
        self.assertIs(type(priority_fee_manager.call_args.kwargs["extra_fee"]), int)

    def test_malformed_intent_returns_non_submitting_receipt(self) -> None:
        adapter = object.__new__(LivePumpExecutionPort)

        receipt = asyncio.run(adapter.submit(object()))

        self.assertEqual(receipt.mode, ExecutionMode.LIVE)
        self.assertFalse(receipt.accepted)
        self.assertFalse(receipt.would_submit_transaction)
        self.assertIsNone(receipt.signature)
