"""Tests for the bounded wallet history and linked-wallet graph."""

import base64
import json
import unittest
from collections.abc import Mapping
from pathlib import Path
from typing import cast

import base58
from solders.transaction import VersionedTransaction

from rugbot.domain.decisions import AbstainResult
from rugbot.ingest.rpc_observer import RpcHttpResponse
from rugbot.runtime.wallet_intelligence import (
    WalletIntelligenceReport,
    report_to_json,
    scan_wallet_intelligence,
)

FIXTURE = Path(
    "fixtures/finalized_transactions/pump_create_v2/"
    "4HbY43S9UigSctrfxY5nszgf3ozN1f4kPQYaqaFLZaCDhwa55rauuRmhP85u67U7dBvGFwB5C6stmkH2b1TNxgQh.json"
)
TARGET = base58.b58encode(b"target-wallet".ljust(32, b"t")).decode()
PEER = "CvoPbuS2AghzVBYJx7HfQGhALiqif4YwWgHvXmhehuJZ"
TRANSFER_SIGNATURE = base58.b58encode(b"transfer-signature".ljust(64, b"x")).decode()
TRANSFER_SLOT = 200


class WalletIntelligenceTests(unittest.IsolatedAsyncioTestCase):
    """Verify direct links and creator-wallet switching evidence."""

    async def test_funded_counterparty_with_create_is_reported_as_switch_candidate(
        self,
    ) -> None:
        artifact = json.loads(FIXTURE.read_text(encoding="utf-8"))
        transport = _WalletTransport(artifact)

        result = await scan_wallet_intelligence(
            TARGET,
            endpoint="https://rpc.example",
            max_transactions=5,
            max_linked_wallets=1,
            as_of_slot=cast("int", artifact["as_of_slot"]),
            transport=transport,
        )

        self.assertIsInstance(result, WalletIntelligenceReport)
        result = cast("WalletIntelligenceReport", result)
        self.assertEqual(result.direct_linked_wallet_count, 1)
        self.assertEqual(result.linked_creator_wallet_count, 1)
        self.assertTrue(result.wallet_switch_candidate)
        self.assertEqual(result.wallet_switches[0].linked_wallet, PEER)
        self.assertEqual(result.wallet_switches[0].first_transfer_slot, TRANSFER_SLOT)
        self.assertEqual(result.wallet_switches[0].first_launch_slot, 430584458)
        self.assertEqual(result.edges[0].source, TARGET)
        self.assertEqual(result.edges[0].target, PEER)
        self.assertEqual(result.edges[0].amount_lamports, 123)
        peer_node = next(node for node in result.nodes if node.address == PEER)
        self.assertIn("creator", peer_node.roles)
        payload = report_to_json(result)
        self.assertEqual(payload["graph"]["edges"][0]["kind"], "direct_native_transfer")
        self.assertEqual(payload["launches"], [])
        self.assertEqual(payload["linked_launches"][0]["transaction_index"], 0)
        self.assertTrue(payload["linked_launches"][0]["position_is_zero_or_one"])

    async def test_future_linked_launch_is_excluded_from_target_cutoff(self) -> None:
        artifact = json.loads(FIXTURE.read_text(encoding="utf-8"))
        result = await scan_wallet_intelligence(
            TARGET,
            endpoint="https://rpc.example",
            max_transactions=5,
            max_linked_wallets=1,
            as_of_slot=TRANSFER_SLOT,
            transport=_WalletTransport(artifact),
        )

        self.assertIsInstance(result, WalletIntelligenceReport)
        result = cast("WalletIntelligenceReport", result)
        self.assertEqual(result.as_of_slot, TRANSFER_SLOT)
        self.assertEqual(result.linked_launches, ())
        self.assertFalse(result.wallet_switch_candidate)
        self.assertEqual(result.linked_creator_wallet_count, 0)
        self.assertTrue(
            any("no evidence at cutoff" in warning for warning in result.warnings)
        )

    async def test_invalid_cutoff_abstains_before_rpc(self) -> None:
        result = await scan_wallet_intelligence(
            TARGET,
            endpoint="https://rpc.example",
            as_of_slot=-1,
            transport=_WalletTransport({}),
        )

        self.assertIsInstance(result, AbstainResult)

    async def test_invalid_wallet_abstains_before_rpc(self) -> None:
        result = await scan_wallet_intelligence(
            "not-a-wallet",
            endpoint="https://rpc.example",
            transport=_WalletTransport({}),
        )

        self.assertIsInstance(result, AbstainResult)


class _WalletTransport:
    def __init__(self, artifact: Mapping[str, object]) -> None:
        self.artifact = artifact
        self.calls: list[dict[str, object]] = []

    async def __call__(self, _endpoint: str, body: bytes) -> RpcHttpResponse:
        request = cast("dict[str, object]", json.loads(body))
        self.calls.append(request)
        method = cast("str", request["method"])
        if method == "getSlot":
            return _response(200)
        address = cast("list[object]", request["params"])[0]
        if method == "getSignaturesForAddress":
            if address == TARGET:
                return _response(
                    [
                        {
                            "signature": TRANSFER_SIGNATURE,
                            "slot": TRANSFER_SLOT,
                            "confirmationStatus": "finalized",
                        }
                    ]
                )
            return _response(
                [
                    {
                        "signature": self.artifact["signature"],
                        "slot": self.artifact["as_of_slot"],
                        "confirmationStatus": "finalized",
                    }
                ]
            )
        if method == "getTransaction":
            signature = cast("list[object]", request["params"])[0]
            if signature == TRANSFER_SIGNATURE:
                return _response(_transfer_transaction())
            return RpcHttpResponse(
                status=200,
                body=_create_transaction(self.artifact),
            )
        if method == "getBlock":
            signature = (
                TRANSFER_SIGNATURE
                if address == TRANSFER_SLOT
                else self.artifact["signature"]
            )
            return _response(
                {"transactions": [{"transaction": {"signatures": [signature]}}]}
            )
        raise AssertionError(f"unexpected RPC method: {method}")  # noqa: TRY003


def _response(result: object) -> RpcHttpResponse:
    return RpcHttpResponse(
        status=200,
        body=json.dumps(
            {"jsonrpc": "2.0", "id": 1, "result": result},
            separators=(",", ":"),
        ).encode("utf-8"),
    )


def _transfer_transaction() -> dict[str, object]:
    transfer_data = base58.b58encode(
        b"\x02\x00\x00\x00" + (123).to_bytes(8, "little")
    ).decode("ascii")
    return {
        "slot": TRANSFER_SLOT,
        "meta": {"err": None, "loadedAddresses": {"writable": [], "readonly": []}},
        "transaction": {
            "signatures": [TRANSFER_SIGNATURE],
            "message": {
                "accountKeys": [TARGET, PEER, "11111111111111111111111111111111"],
                "instructions": [
                    {"programIdIndex": 2, "accounts": [0, 1], "data": transfer_data}
                ],
            },
        },
    }


def _create_transaction(artifact: Mapping[str, object]) -> bytes:
    base64_response = artifact["base64_transaction_response"]
    transaction = VersionedTransaction.from_bytes(
        base64.b64decode(base64_response["transaction"][0])
    )
    return _response(
        {
            "slot": artifact["as_of_slot"],
            "meta": {
                "err": None,
                "loadedAddresses": base64_response["meta"]["loadedAddresses"],
            },
            "transaction": {
                "signatures": [artifact["signature"]],
                "message": {
                    "accountKeys": [
                        str(pubkey) for pubkey in transaction.message.account_keys
                    ],
                    "instructions": [
                        {
                            "programIdIndex": instruction.program_id_index,
                            "accounts": list(instruction.accounts),
                            "data": base58.b58encode(instruction.data).decode("ascii"),
                        }
                        for instruction in transaction.message.instructions
                    ],
                },
            },
        }
    ).body


if __name__ == "__main__":
    unittest.main()
