"""Focused tests for finalized RPC backtest dataset construction."""

import asyncio
import json
import unittest
from collections.abc import Mapping
from dataclasses import replace
from typing import cast

import base58

from rugbot.backtest.dataset import FinalizedBacktestDataset
from rugbot.backtest.rpc_dataset import build_finalized_rpc_dataset
from rugbot.domain.amounts import Slot
from rugbot.domain.decisions import AbstainReason, AbstainResult
from rugbot.ingest.rpc_observer import RpcHttpResponse
from tests.adverse_intel.test_pump_create_observation import (
    _artifact,
    _observation,
)

ADDRESS = base58.b58encode(b"rpc-dataset-address".ljust(32, b"a")).decode()


class BacktestRpcDatasetTests(unittest.TestCase):
    """Verify bounded RPC evidence reaches the canonical dataset builder."""

    def test_fetches_finalized_observation_and_decodes_pinned_launch(self) -> None:
        observation = _observation(_artifact())
        payload = json.loads(observation.raw_source_payload)
        instructions = payload["result"]["transaction"]["message"]["instructions"]
        payload["result"]["transaction"]["message"]["instructions"] = (
            instructions[:4] + instructions[5:]
        )
        observation = replace(
            observation,
            raw_source_payload=json.dumps(payload).encode("utf-8"),
        )
        signature = base58.b58encode(observation.signature).decode("ascii")
        transport = _FakeTransport(
            get_slot=observation.slot,
            signatures=[_signature_row(signature, observation.slot)],
            transactions={signature: observation.raw_source_payload},
            blocks={observation.slot: _block_response(signature)},
        )

        result = asyncio.run(
            build_finalized_rpc_dataset(
                address=ADDRESS,
                endpoint="https://rpc.example",
                start_slot=Slot(observation.slot),
                end_slot=Slot(observation.slot),
                max_transactions=1,
                transport=transport,
            )
        )

        self.assertIsInstance(result, FinalizedBacktestDataset)
        dataset = cast("FinalizedBacktestDataset", result)
        self.assertEqual(dataset.as_of_slot, observation.slot)
        self.assertEqual(len(dataset.observations), 1)
        self.assertEqual(len(dataset.launches), 1)
        self.assertEqual(dataset.launches[0].signature, observation.signature)
        self.assertEqual(
            [call["method"] for call in transport.calls],
            [
                "getSlot",
                "getSignaturesForAddress",
                "getSignaturesForAddress",
                "getTransaction",
                "getBlock",
            ],
        )
        history_options = cast("list[object]", transport.calls[1]["params"])[1]
        self.assertEqual(
            history_options,
            {"commitment": "finalized", "limit": 1},
        )

    def test_history_newer_than_dataset_cutoff_is_excluded(self) -> None:
        """Newer history is skipped after the requested window is proven."""

        observation = _observation(_artifact())
        signature = base58.b58encode(observation.signature).decode("ascii")
        transport = _FakeTransport(
            get_slot=observation.slot,
            signatures=[_signature_row(signature, observation.slot)],
            transactions={signature: observation.raw_source_payload},
            blocks={observation.slot: _block_response(signature)},
        )

        result = asyncio.run(
            build_finalized_rpc_dataset(
                address=ADDRESS,
                endpoint="https://rpc.example",
                start_slot=Slot(observation.slot - 10),
                end_slot=Slot(observation.slot - 1),
                max_transactions=1,
                transport=transport,
            )
        )

        self.assertIsInstance(result, FinalizedBacktestDataset)
        self.assertEqual(cast("FinalizedBacktestDataset", result).observations, ())

    def test_incomplete_trade_instruction_abstains_on_automatic_fill_path(self) -> None:
        observation = _observation(_artifact())
        signature = base58.b58encode(observation.signature).decode("ascii")
        transport = _FakeTransport(
            get_slot=observation.slot,
            signatures=[_signature_row(signature, observation.slot)],
            transactions={signature: observation.raw_source_payload},
            blocks={observation.slot: _block_response(signature)},
        )

        result = asyncio.run(
            build_finalized_rpc_dataset(
                address=ADDRESS,
                endpoint="https://rpc.example",
                start_slot=Slot(observation.slot),
                end_slot=Slot(observation.slot),
                max_transactions=1,
                transport=transport,
            )
        )

        self.assertIsInstance(result, AbstainResult)
        if isinstance(result, AbstainResult):
            self.assertIs(result.reason, AbstainReason.MISSING_FEATURE)

    def test_history_older_than_dataset_window_is_excluded(self) -> None:
        """Older history is excluded after crossing the window floor."""

        observation = _observation(_artifact())
        signature = base58.b58encode(observation.signature).decode("ascii")
        transport = _FakeTransport(
            get_slot=observation.slot,
            signatures=[_signature_row(signature, observation.slot)],
            transactions={signature: observation.raw_source_payload},
            blocks={observation.slot: _block_response(signature)},
        )

        result = asyncio.run(
            build_finalized_rpc_dataset(
                address=ADDRESS,
                endpoint="https://rpc.example",
                start_slot=Slot(observation.slot + 1),
                end_slot=Slot(observation.slot + 10),
                max_transactions=1,
                transport=transport,
            )
        )

        self.assertIsInstance(result, FinalizedBacktestDataset)
        self.assertEqual(cast("FinalizedBacktestDataset", result).observations, ())

    def test_malformed_or_non_finalized_rpc_evidence_abstains(self) -> None:
        observation = _observation(_artifact())
        signature = base58.b58encode(observation.signature).decode("ascii")
        malformed = _FakeTransport(
            get_slot=observation.slot,
            signatures=[
                {
                    "signature": signature,
                    "slot": observation.slot,
                    "confirmationStatus": "confirmed",
                }
            ],
            transactions={},
            blocks={},
        )

        result = asyncio.run(
            build_finalized_rpc_dataset(
                address=ADDRESS,
                endpoint="https://rpc.example",
                start_slot=Slot(observation.slot),
                end_slot=Slot(observation.slot),
                max_transactions=1,
                transport=malformed,
            )
        )

        self.assertIsInstance(result, AbstainResult)
        self.assertEqual(
            cast("AbstainResult", result).reason,
            AbstainReason.UNKNOWN_PROTOCOL_STATE,
        )
        self.assertEqual(
            [call["method"] for call in malformed.calls],
            ["getSlot", "getSignaturesForAddress"],
        )

    def test_malformed_pinned_launch_payload_abstains(self) -> None:
        observation = _observation(_artifact())
        signature = base58.b58encode(observation.signature).decode("ascii")
        envelope = cast("dict[str, object]", json.loads(observation.raw_source_payload))
        result_body = cast("dict[str, object]", envelope["result"])
        transaction = cast("dict[str, object]", result_body["transaction"])
        message = cast("dict[str, object]", transaction["message"])
        message.pop("instructions")
        malformed_body = json.dumps(envelope, separators=(",", ":")).encode()
        transport = _FakeTransport(
            get_slot=observation.slot,
            signatures=[_signature_row(signature, observation.slot)],
            transactions={signature: malformed_body},
            blocks={observation.slot: _block_response(signature)},
        )

        result = asyncio.run(
            build_finalized_rpc_dataset(
                address=ADDRESS,
                endpoint="https://rpc.example",
                start_slot=Slot(observation.slot),
                end_slot=Slot(observation.slot),
                max_transactions=1,
                transport=transport,
            )
        )

        self.assertIsInstance(result, AbstainResult)
        self.assertEqual(
            cast("AbstainResult", result).reason,
            AbstainReason.UNKNOWN_PROTOCOL_STATE,
        )

    def test_invalid_bounds_abstain_before_rpc(self) -> None:
        transport = _FakeTransport(
            get_slot=0,
            signatures=[],
            transactions={},
            blocks={},
        )

        invalid_window = asyncio.run(
            build_finalized_rpc_dataset(
                address=ADDRESS,
                endpoint="https://rpc.example",
                start_slot=Slot(20),
                end_slot=Slot(10),
                max_transactions=1,
                transport=transport,
            )
        )
        invalid_limit = asyncio.run(
            build_finalized_rpc_dataset(
                address=ADDRESS,
                endpoint="https://rpc.example",
                start_slot=Slot(0),
                end_slot=Slot(10),
                max_transactions=1001,
                transport=transport,
            )
        )

        self.assertIsInstance(invalid_window, AbstainResult)
        self.assertIsInstance(invalid_limit, AbstainResult)
        self.assertEqual(transport.calls, [])


class _FakeTransport:
    def __init__(
        self,
        *,
        get_slot: int,
        signatures: list[dict[str, object]],
        transactions: Mapping[str, bytes],
        blocks: Mapping[int, bytes],
    ) -> None:
        self._get_slot = get_slot
        self._signatures = signatures
        self._signature_calls = 0
        self._transactions = transactions
        self._blocks = blocks
        self.calls: list[dict[str, object]] = []

    async def __call__(self, _endpoint: str, body: bytes) -> RpcHttpResponse:
        request = cast("dict[str, object]", json.loads(body))
        self.calls.append(request)
        method = request["method"]
        params = cast("list[object]", request["params"])
        if method == "getSlot":
            response = _rpc_response(self._get_slot)
        elif method == "getSignaturesForAddress":
            response = _rpc_response(
                self._signatures if self._signature_calls == 0 else []
            )
            self._signature_calls += 1
        elif method == "getTransaction":
            response = self._transactions[cast("str", params[0])]
        elif method == "getBlock":
            response = self._blocks[cast("int", params[0])]
        else:
            raise AssertionError
        return RpcHttpResponse(status=200, body=response)


def _signature_row(signature: str, slot: int) -> dict[str, object]:
    return {
        "signature": signature,
        "slot": slot,
        "confirmationStatus": "finalized",
    }


def _rpc_response(result: object) -> bytes:
    return json.dumps(
        {"jsonrpc": "2.0", "id": 1, "result": result},
        separators=(",", ":"),
    ).encode()


def _block_response(*signatures: str) -> bytes:
    return _rpc_response(
        {"transactions": [{"transaction": {"signatures": list(signatures)}}]}
    )


if __name__ == "__main__":
    unittest.main()
