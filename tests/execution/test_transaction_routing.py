"""Integration and behavior tests for TransactionRouter, JitoSender, and RpcSender."""

# ruff: noqa: S106

import unittest
from unittest.mock import AsyncMock

from solders.keypair import Keypair

from rugbot.execution.sender.base import RoutingPolicy, SubmissionResult
from rugbot.execution.sender.jito import (
    JITO_FALLBACK_TIP_ACCOUNTS,
    JitoSender,
    create_jito_tip_instruction,
)
from rugbot.execution.sender.router import TransactionRouter
from rugbot.execution.sender.rpc import RpcSender
from rugbot.execution.telemetry import ExecutionMetrics


class TestTransactionRouting(unittest.IsolatedAsyncioTestCase):
    """Test exclusive routing policies and tip instruction creation."""

    def test_jito_tip_accounts_and_instruction(self) -> None:
        sender = JitoSender()
        self.assertEqual(len(sender.tip_accounts), 8)
        self.assertIn(
            "ADuUkR4vqLUMWXxW9gh6D6L8pMSawimctcNZ5pGwDcEt",
            sender.tip_accounts,
        )

        payer = Keypair().pubkey()
        tip_ix = create_jito_tip_instruction(
            payer=payer,
            tip_lamports=1_000_000,
        )
        self.assertEqual(len(tip_ix.accounts), 2)
        self.assertEqual(tip_ix.accounts[0].pubkey, payer)
        self.assertTrue(tip_ix.accounts[0].is_signer)
        self.assertIn(
            str(tip_ix.accounts[1].pubkey),
            JITO_FALLBACK_TIP_ACCOUNTS,
        )

    async def test_router_rpc_only_policy(self) -> None:
        mock_rpc = AsyncMock(spec=RpcSender)
        mock_rpc.name = "rpc"
        mock_rpc.send_transaction.return_value = SubmissionResult(
            sender_name="rpc",
            signature="test_sig_rpc_1",
            ack_ms=18.5,
            acknowledged=True,
        )

        router = TransactionRouter(rpc_sender=mock_rpc, jito_sender=None)
        telemetry = ExecutionMetrics(target_wallet="W", token_mint="M")

        res = await router.route(
            raw_tx_bytes=b"dummy_tx_bytes",
            policy=RoutingPolicy.RPC_ONLY,
            telemetry=telemetry,
        )

        self.assertTrue(res.acknowledged)
        self.assertEqual(res.signature, "test_sig_rpc_1")
        self.assertEqual(telemetry.rpc_ack_ms, 18.5)
        self.assertEqual(telemetry.first_ack_sender, "rpc")

    async def test_router_jito_only_never_calls_rpc(self) -> None:
        mock_rpc = AsyncMock(spec=RpcSender)
        mock_rpc.name = "rpc"
        mock_jito = AsyncMock(spec=JitoSender)
        mock_jito.name = "jito"

        async def fake_jito_send(_bytes: bytes) -> SubmissionResult:
            return SubmissionResult(
                sender_name="jito",
                signature="test_sig_jito",
                ack_ms=10.0,
                acknowledged=True,
            )

        mock_jito.send_transaction.side_effect = fake_jito_send

        router = TransactionRouter(rpc_sender=mock_rpc, jito_sender=mock_jito)
        telemetry = ExecutionMetrics(target_wallet="W", token_mint="M")

        res = await router.route(
            raw_tx_bytes=b"dummy_tx_bytes",
            policy=RoutingPolicy.JITO_ONLY,
            telemetry=telemetry,
        )

        self.assertTrue(res.acknowledged)
        self.assertEqual(res.sender_name, "jito")
        self.assertEqual(res.signature, "test_sig_jito")
        self.assertEqual(telemetry.first_ack_sender, "jito")

        self.assertAlmostEqual(telemetry.jito_ack_ms, 10.0, delta=5.0)
        mock_rpc.send_transaction.assert_not_awaited()

    async def test_router_jito_failure_does_not_fan_out_to_rpc(self) -> None:
        mock_rpc = AsyncMock(spec=RpcSender)
        mock_rpc.name = "rpc"
        mock_jito = AsyncMock(spec=JitoSender)
        mock_jito.name = "jito"

        async def fake_jito_fail(_bytes: bytes) -> SubmissionResult:
            return SubmissionResult(
                sender_name="jito",
                signature="",
                ack_ms=5.0,
                acknowledged=False,
                error_message="Jito 429 rate limited",
            )

        mock_jito.send_transaction.side_effect = fake_jito_fail

        router = TransactionRouter(rpc_sender=mock_rpc, jito_sender=mock_jito)
        telemetry = ExecutionMetrics(target_wallet="W", token_mint="M")

        res = await router.route(
            raw_tx_bytes=b"dummy_tx_bytes",
            policy=RoutingPolicy.JITO_ONLY,
            telemetry=telemetry,
        )

        self.assertFalse(res.acknowledged)
        self.assertEqual(res.sender_name, "jito")
        self.assertIsNone(telemetry.first_ack_sender)
        mock_rpc.send_transaction.assert_not_awaited()
