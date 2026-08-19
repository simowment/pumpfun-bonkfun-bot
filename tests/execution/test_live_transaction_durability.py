"""Live-adapter integration tests for durable pre-dispatch state."""

from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import base58
from solders.hash import Hash
from solders.instruction import AccountMeta, Instruction
from solders.keypair import Keypair
from solders.pubkey import Pubkey
from solders.transaction import Transaction

from rugbot.execution.landing import FinalizedLanding
from rugbot.execution.landing_reconciliation import LandingReconciliation
from rugbot.execution.live import LivePumpExecutionPort
from rugbot.execution.ports import ExecutionIntent
from rugbot.execution.sender import RoutingPolicy, SubmissionResult
from rugbot.protocol.pump.create_decoder import PUMP_PROGRAM_ID
from rugbot.protocol.pump.trade_decoder import BUY_V2_ACCOUNT_NAMES
from rugbot.storage.transaction_state import (
    SqliteTransactionStateStore,
    TransactionState,
)


class LiveTransactionDurabilityTests(unittest.IsolatedAsyncioTestCase):
    """Exercise signing, SQLite persistence, dispatch, and restart boundaries."""

    async def test_signed_bytes_are_durable_before_dispatch_crash(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            database_path = Path(temporary_directory) / "transactions.sqlite3"
            router = _RecordingRouter(crash=True)
            port = _live_port(database_path, router)
            intent = _intent()

            with self.assertRaisesRegex(RuntimeError, "simulated process crash"):
                await _submit_with_local_trade_pipeline(port, intent)
            captured = router.raw_transactions[0]
            port._transaction_store.close()

            with SqliteTransactionStateStore(database_path) as recovered:
                pending = recovered.list_recovery_pending()
                self.assertEqual(len(pending), 1)
                self.assertEqual(pending[0].state, TransactionState.SUBMITTED)
                self.assertEqual(pending[0].raw_tx_bytes, captured)
                signed = Transaction.from_bytes(captured)
                self.assertEqual(pending[0].signature, str(signed.signatures[0]))
                self.assertEqual(
                    pending[0].blockhash, str(signed.message.recent_blockhash)
                )
                self.assertEqual(pending[0].last_valid_block_height, 200)

    async def test_restart_does_not_resubmit_an_existing_intent(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            database_path = Path(temporary_directory) / "transactions.sqlite3"
            crashing_router = _RecordingRouter(crash=True)
            keypair = Keypair()
            first_port = _live_port(database_path, crashing_router, keypair=keypair)
            intent = _intent()
            with self.assertRaises(RuntimeError):
                await _submit_with_local_trade_pipeline(first_port, intent)
            first_port._transaction_store.close()

            replacement_router = _RecordingRouter()
            restarted_port = _live_port(
                database_path,
                replacement_router,
                keypair=keypair,
            )
            receipt = await restarted_port.submit(intent)

            self.assertFalse(receipt.accepted)
            self.assertFalse(receipt.would_submit_transaction)
            self.assertIn("SUBMITTED", receipt.message)
            self.assertEqual(replacement_router.raw_transactions, [])
            restarted_port._transaction_store.close()

    async def test_finalized_success_reaches_confirmed_durable_state(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            database_path = Path(temporary_directory) / "transactions.sqlite3"
            router = _RecordingRouter()
            port = _live_port(database_path, router)
            intent = _intent()

            receipt = await _submit_with_local_trade_pipeline(
                port,
                intent,
                finalize=True,
            )

            self.assertTrue(receipt.accepted)
            self.assertEqual(receipt.simulated_output_base_units, 777)
            record = port._transaction_store.get(intent.intent_id)
            self.assertIsNotNone(record)
            self.assertEqual(record.state, TransactionState.RECONCILED)
            self.assertEqual(record.landed_slot, 123)
            port._transaction_store.close()

    async def test_restart_resends_only_the_same_valid_signed_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            database_path = Path(temporary_directory) / "transactions.sqlite3"
            crashing_router = _RecordingRouter(crash=True)
            keypair = Keypair()
            first_port = _live_port(database_path, crashing_router, keypair=keypair)
            with self.assertRaises(RuntimeError):
                await _submit_with_local_trade_pipeline(first_port, _intent())
            signed_bytes = crashing_router.raw_transactions[0]
            first_port._transaction_store.close()

            recovery_router = _RecordingRouter()
            recovered_port = _live_port(
                database_path,
                recovery_router,
                keypair=keypair,
            )
            recovered_port._client.block_height = 150
            with (
                patch(
                    "rugbot.execution.live.observe_finalized_signature",
                    new=AsyncMock(
                        return_value=FinalizedLanding(
                            signature=str(
                                Transaction.from_bytes(signed_bytes).signatures[0]
                            ),
                            finalized=False,
                            slot=None,
                            err=None,
                            transaction_found=False,
                        )
                    ),
                ),
                patch(
                    "rugbot.execution.live.wait_for_finalized_signatures",
                    new=AsyncMock(
                        return_value=(
                            FinalizedLanding(
                                signature=str(
                                    Transaction.from_bytes(signed_bytes).signatures[0]
                                ),
                                finalized=True,
                                slot=123,
                                err=None,
                                transaction_found=True,
                            ),
                        )
                    ),
                ),
                patch(
                    "rugbot.execution.live.reconcile_finalized_landing",
                    new=AsyncMock(return_value=_reconciliation()),
                ),
            ):
                recovered = await recovered_port.recover_pending()

            self.assertEqual(recovery_router.raw_transactions, [signed_bytes])
            self.assertEqual(recovered[0].state, TransactionState.RECONCILED)
            recovered_port._transaction_store.close()

    async def test_restart_expires_absent_signature_without_resubmission(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            database_path = Path(temporary_directory) / "transactions.sqlite3"
            crashing_router = _RecordingRouter(crash=True)
            keypair = Keypair()
            first_port = _live_port(database_path, crashing_router, keypair=keypair)
            with self.assertRaises(RuntimeError):
                await _submit_with_local_trade_pipeline(first_port, _intent())
            first_port._transaction_store.close()

            recovery_router = _RecordingRouter()
            recovered_port = _live_port(
                database_path,
                recovery_router,
                keypair=keypair,
            )
            recovered_port._client.block_height = 201
            with patch(
                "rugbot.execution.live.observe_finalized_signature",
                new=AsyncMock(
                    return_value=FinalizedLanding(
                        signature="absent",
                        finalized=False,
                        slot=None,
                        err=None,
                        transaction_found=False,
                    )
                ),
            ):
                recovered = await recovered_port.recover_pending()

            self.assertEqual(recovery_router.raw_transactions, [])
            self.assertEqual(recovered[0].state, TransactionState.EXPIRED)
            recovered_port._transaction_store.close()

    async def test_restart_cancels_intent_and_never_dispatched_signed_state(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            database_path = Path(temporary_directory) / "transactions.sqlite3"
            keypair = Keypair()
            with SqliteTransactionStateStore(database_path) as store:
                unsigned = replace(_intent(), intent_id="unsigned-recovery")
                signed = replace(_intent(), intent_id="signed-recovery")
                store.create_intent(unsigned, wallet_pubkey=str(keypair.pubkey()))
                store.create_intent(signed, wallet_pubkey=str(keypair.pubkey()))
                store.store_signed(
                    signed.intent_id,
                    raw_tx_bytes=b"durable-signed-bytes",
                    signature="durable-signature",
                    blockhash="durable-blockhash",
                    last_valid_block_height=200,
                )

            router = _RecordingRouter()
            port = _live_port(database_path, router, keypair=keypair)
            recovered = await port.recover_pending()

            self.assertEqual(
                [record.state for record in recovered],
                [TransactionState.CANCELLED, TransactionState.CANCELLED],
            )
            self.assertEqual(router.raw_transactions, [])
            port._transaction_store.close()


def _intent() -> ExecutionIntent:
    return ExecutionIntent(
        intent_id="durable-live-buy",
        as_of_slot=100,
        market_id=str(Pubkey.new_unique()),
        side="buy",
        quote_amount_base_units=25_000_000,
        base_amount_base_units=None,
        max_slippage_bps=500,
        reason_codes=("known_operator_wallet",),
    )


def _live_port(
    database_path: Path,
    router: _RecordingRouter,
    *,
    keypair: Keypair | None = None,
) -> LivePumpExecutionPort:
    keypair = keypair or Keypair()
    port = object.__new__(LivePumpExecutionPort)
    port.endpoint = "https://rpc.example"
    port.private_key = base58.b58encode(bytes(keypair)).decode("ascii")
    port.max_retries = 2
    port.fixed_priority_fee_microlamports = 200_000
    port.jito_tip_lamports = 0
    port.compute_unit_limit = 400_000
    port.loaded_accounts_data_size_limit = 128_000
    port.routing_policy = RoutingPolicy.RPC_ONLY
    port.jito_block_engine_url = "https://jito.example"
    port.transaction_state_path = database_path
    port._keypair = keypair
    port._client = _FakeLiveClient()
    port._router = router
    port._jito_sender = SimpleNamespace(
        tip_accounts=(),
        close=AsyncMock(),
    )
    port._rpc_sender = SimpleNamespace()
    port._transaction_store = SqliteTransactionStateStore(database_path)
    port._initialized = True
    return port


async def _submit_with_local_trade_pipeline(
    port: LivePumpExecutionPort,
    intent: ExecutionIntent,
    *,
    finalize: bool = False,
):
    landing = FinalizedLanding(
        signature="placeholder",
        finalized=True,
        slot=123,
        err=None,
        transaction_found=True,
    )

    async def finalized_signature(_client, signatures):
        return (
            FinalizedLanding(
                signature=signatures[0],
                finalized=landing.finalized,
                slot=landing.slot,
                err=landing.err,
                transaction_found=landing.transaction_found,
            ),
        )

    with (
        patch(
            "rugbot.execution.live._fetch_trade_accounts",
            new=AsyncMock(return_value=(100, {})),
        ),
        patch(
            "rugbot.execution.live._build_trade_context",
            return_value=(
                SimpleNamespace(amount=777, quote_limit=25_000_000),
                object(),
            ),
        ),
        patch(
            "rugbot.execution.live._build_transaction_instructions",
            return_value=(
                Instruction(
                    Pubkey.from_string(PUMP_PROGRAM_ID),
                    b"",
                    [
                        AccountMeta(Pubkey.new_unique(), False, False)
                        for _name in BUY_V2_ACCOUNT_NAMES
                    ],
                ),
            ),
        ),
        patch(
            "rugbot.execution.live.validate_pump_v2_instructions",
            side_effect=lambda instructions, policy: instructions,
        ),
        patch(
            "rugbot.execution.live.simulate_unsigned_transaction",
            new=AsyncMock(return_value=SimpleNamespace(accepted=True)),
        ),
        patch(
            "rugbot.execution.live.wait_for_finalized_signatures",
            new=finalized_signature if finalize else AsyncMock(),
        ),
        patch(
            "rugbot.execution.live.reconcile_finalized_landing",
            new=AsyncMock(
                return_value=LandingReconciliation(
                    signature="placeholder",
                    landed_slot=123,
                    token_delta_base_units=777,
                    sol_delta_lamports=-25_000_000,
                    network_fee_lamports=5_000,
                    jito_tip_lamports=0,
                    ata_rent_lamports=2_039_280,
                    protocol_fee_lamports=250_000,
                )
            ),
        ),
    ):
        return await port.submit(intent)


class _FakeLiveClient:
    def __init__(self) -> None:
        self.block_height = 150

    async def get_cached_blockhash_context(self) -> tuple[Hash, int]:
        return Hash.new_unique(), 200

    async def post_rpc(self, request: dict[str, object]) -> dict[str, object]:
        assert request.get("method") == "getBlockHeight"
        return {"jsonrpc": "2.0", "id": 1, "result": self.block_height}


class _RecordingRouter:
    def __init__(self, *, crash: bool = False) -> None:
        self.crash = crash
        self.raw_transactions: list[bytes] = []

    async def route(self, raw_transaction: bytes, **_kwargs):
        self.raw_transactions.append(raw_transaction)
        if self.crash:
            raise RuntimeError("simulated process crash")  # noqa: TRY003
        signature = str(Transaction.from_bytes(raw_transaction).signatures[0])
        return SubmissionResult(
            sender_name="rpc",
            signature=signature,
            ack_ms=1.0,
            acknowledged=True,
        )


def _reconciliation() -> LandingReconciliation:
    return LandingReconciliation(
        signature="placeholder",
        landed_slot=123,
        token_delta_base_units=777,
        sol_delta_lamports=-25_000_000,
        network_fee_lamports=5_000,
        jito_tip_lamports=0,
        ata_rent_lamports=2_039_280,
        protocol_fee_lamports=250_000,
    )


if __name__ == "__main__":
    unittest.main()
