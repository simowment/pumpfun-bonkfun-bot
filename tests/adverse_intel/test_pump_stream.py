"""Focused checks for the Pump.fun stream trigger and finality boundary."""

import json
import unittest
from pathlib import Path

import base58

from rugbot.domain.observations import RawChainObservation
from rugbot.ingest.pump_stream import (
    ProcessedPumpCreateNotification,
    _parse_pumpportal_notification,
)
from rugbot.ingest.rpc_observer import (
    RpcHttpResponse,
    observe_finalized_transaction,
)


class PumpStreamTests(unittest.IsolatedAsyncioTestCase):
    """Check stream filtering and finalized hydration without a live RPC."""

    def test_parse_notification_requires_pump_create(self) -> None:
        target_wallet = "CvoPbuS2AghzVBYJx7HfQGhALiqif4YwWgHvXmhehuJZ"
        signature = base58.b58encode(bytes(range(64))).decode("ascii")
        mint = "GGYkiUJGopoH9DZUjGFtLiKpXXW5AQovqZ7eVcjEpump"
        message = json.dumps(
            {
                "txType": "create",
                "signature": signature,
                "mint": mint,
                "traderPublicKey": target_wallet,
            }
        )
        self.assertEqual(
            _parse_pumpportal_notification(message, target_wallet),
            {
                "signature": signature,
                "mint": mint,
                "creator": target_wallet,
            },
        )
        self.assertIsNone(
            _parse_pumpportal_notification(
                json.dumps(
                    {
                        "txType": "buy",
                        "signature": signature,
                        "mint": mint,
                        "traderPublicKey": target_wallet,
                    }
                ),
                target_wallet,
            )
        )
        self.assertIsNone(
            _parse_pumpportal_notification(message, "11111111111111111111111111111111")
        )

    async def test_streamed_signature_hydrates_finalized_observation(self) -> None:
        signature = base58.b58encode(bytes(range(64))).decode("ascii")
        responses = {
            "getSlot": {"jsonrpc": "2.0", "id": 1, "result": 200},
            "getTransaction": {
                "jsonrpc": "2.0",
                "id": 1,
                "result": {
                    "slot": 200,
                    "meta": {"err": None},
                    "transaction": {
                        "signatures": [signature],
                        "message": {"accountKeys": [], "instructions": []},
                    },
                },
            },
            "getBlock": {
                "jsonrpc": "2.0",
                "id": 1,
                "result": {
                    "transactions": [
                        {"transaction": {"signatures": [signature]}},
                    ]
                },
            },
        }

        async def transport(endpoint: str, body: bytes) -> RpcHttpResponse:
            del endpoint
            method = json.loads(body)["method"]
            return RpcHttpResponse(
                status=200,
                body=json.dumps(responses[method]).encode("utf-8"),
            )

        result = await observe_finalized_transaction(
            signature,
            expected_slot=200,
            endpoint="https://rpc.example",
            source_id="solana-http-rpc:wallet",
            transport=transport,
        )
        self.assertIsInstance(result, RawChainObservation)
        self.assertEqual(result.commitment, "finalized")
        self.assertEqual(result.transaction_index, 0)
        self.assertEqual(result.signature, bytes(range(64)))


if __name__ == "__main__":
    unittest.main()
