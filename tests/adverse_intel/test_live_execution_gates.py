"""Hermetic RPC contract tests for live execution gates."""

import asyncio
import base64
import unittest

from solders.compute_budget import set_compute_unit_limit
from solders.hash import Hash
from solders.pubkey import Pubkey
from solders.transaction import Transaction

from rugbot.execution.landing import (
    observe_finalized_signatures,
    wait_for_finalized_signatures,
)
from rugbot.execution.simulation import simulate_unsigned_transaction


class LiveExecutionGateTests(unittest.TestCase):
    """Exercise RPC payloads without a network or transaction submission."""

    def test_simulation_uses_original_blockhash_and_strict_options(self) -> None:
        client = _FakeRpcClient(
            {
                "err": None,
                "unitsConsumed": 123,
                "loadedAccountsDataSize": 456,
                "logs": ["ok"],
            }
        )

        blockhash = Hash.new_unique()
        result = asyncio.run(
            simulate_unsigned_transaction(
                client,
                payer=Pubkey.new_unique(),
                instructions=(set_compute_unit_limit(400_000),),
                recent_blockhash=blockhash,
                max_compute_units=400_000,
                max_loaded_accounts_data_size=1_000,
            )
        )

        self.assertTrue(result.accepted)
        request = client.requests[0]
        self.assertEqual(request["method"], "simulateTransaction")
        options = request["params"][1]
        self.assertFalse(options["sigVerify"])
        self.assertFalse(options["replaceRecentBlockhash"])
        self.assertEqual(options["commitment"], "finalized")
        self.assertEqual(
            base64.b64decode(request["params"][0]),
            client.simulated_transaction_bytes,
        )
        simulated = Transaction.from_bytes(client.simulated_transaction_bytes)
        self.assertEqual(simulated.message.recent_blockhash, blockhash)

    def test_finalized_status_preserves_execution_error(self) -> None:
        client = _FakeRpcClient(
            {
                "value": [
                    {
                        "slot": 123,
                        "confirmationStatus": "finalized",
                        "err": {"InstructionError": [1, "Custom"]},
                    },
                    None,
                ]
            }
        )

        result = asyncio.run(observe_finalized_signatures(client, ("sig-a", "sig-b")))

        self.assertEqual(result[0].slot, 123)
        self.assertTrue(result[0].finalized)
        self.assertIsNotNone(result[0].err)
        self.assertFalse(result[1].transaction_found)

    def test_landing_waits_for_successful_variant_after_failed_variant(self) -> None:
        client = _SequenceRpcClient(
            [
                {
                    "value": [
                        {
                            "slot": 123,
                            "confirmationStatus": "finalized",
                            "err": {"InstructionError": [1, "Custom"]},
                        },
                        None,
                    ]
                },
                {
                    "value": [
                        {
                            "slot": 123,
                            "confirmationStatus": "finalized",
                            "err": {"InstructionError": [1, "Custom"]},
                        },
                        {"slot": 124, "confirmationStatus": "finalized", "err": None},
                    ]
                },
            ]
        )

        result = asyncio.run(
            wait_for_finalized_signatures(
                client,
                ("sig-a", "sig-b"),
                poll_interval_seconds=0.001,
                max_polls=2,
            )
        )

        self.assertEqual(result[1].signature, "sig-b")
        self.assertTrue(result[1].finalized)
        self.assertIsNone(result[1].err)


class _FakeRpcClient:
    def __init__(self, result: dict[str, object]) -> None:
        self.result = result
        self.requests: list[dict[str, object]] = []
        self.simulated_transaction_bytes = b""

    async def post_rpc(self, request: dict[str, object]) -> dict[str, object]:
        self.requests.append(request)
        if request["method"] == "simulateTransaction":
            self.simulated_transaction_bytes = base64.b64decode(request["params"][0])
        return {"jsonrpc": "2.0", "id": 1, "result": self.result}


class _SequenceRpcClient:
    def __init__(self, results: list[dict[str, object]]) -> None:
        self.results = iter(results)

    async def post_rpc(self, _request: dict[str, object]) -> dict[str, object]:
        return {"jsonrpc": "2.0", "id": 1, "result": next(self.results)}


if __name__ == "__main__":
    unittest.main()
