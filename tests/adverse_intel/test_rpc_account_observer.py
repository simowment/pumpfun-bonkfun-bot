"""Focused tests for finalized RPC account-info observation ingestion."""

import asyncio
import base64
import json
import unittest
from typing import cast
from uuid import UUID

import base58

from rugbot.domain.decisions import AbstainReason, AbstainResult
from rugbot.domain.observations import RawChainObservation
from rugbot.ingest.rpc_account_observer import (
    observe_account_info,
    observe_multiple_account_info,
)
from rugbot.ingest.rpc_observer import RpcHttpResponse

ADDRESS = base58.b58encode(b"account".ljust(32, b"a")).decode()
OWNER = base58.b58encode(b"owner".ljust(32, b"o")).decode()
BOOT_ID = UUID("00000000-0000-0000-0000-000000000001")
ACCOUNT_BYTES = b"opaque-account-bytes"


class RpcAccountObserverTests(unittest.TestCase):
    """Tests for one bounded finalized getAccountInfo call."""

    def test_emits_finalized_raw_account_observation_with_provenance(self) -> None:
        """The observer preserves raw bytes and the exact RPC response body."""

        response_body = _rpc_response(
            {
                "context": {"slot": 700},
                "value": {
                    "data": [base64.b64encode(ACCOUNT_BYTES).decode(), "base64"],
                    "owner": OWNER,
                },
            },
            whitespace=True,
        )
        transport = _FakeTransport(response_body)

        result = asyncio.run(
            observe_account_info(
                ADDRESS,
                endpoint="https://rpc.example",
                source_id="test-account-rpc",
                observer_id="test-observer",
                boot_id=BOOT_ID,
                receive_sequence_start=8,
                transport=transport,
                as_of_slot=700,
            )
        )

        self.assertIsInstance(result, RawChainObservation)
        observation = cast("RawChainObservation", result)
        self.assertEqual(observation.slot, 700)
        self.assertEqual(observation.account_pubkey, base58.b58decode(ADDRESS))
        self.assertEqual(observation.account_owner_program_id, base58.b58decode(OWNER))
        self.assertEqual(observation.raw_account_data, ACCOUNT_BYTES)
        self.assertEqual(observation.raw_source_payload, response_body)
        self.assertEqual(observation.commitment, "finalized")
        self.assertEqual(observation.canonical_status, "canonical")
        self.assertEqual(observation.source_update_kind, "account")
        self.assertEqual(observation.source_id, "test-account-rpc")
        self.assertEqual(observation.observer_id, "test-observer")
        self.assertEqual(observation.boot_id, BOOT_ID)
        self.assertEqual(observation.receive_sequence, 9)
        self.assertIsNone(observation.account_write_version)
        self.assertEqual(len(transport.calls), 1)
        self.assertEqual(transport.calls[0]["method"], "getAccountInfo")
        self.assertEqual(
            transport.calls[0]["params"],
            [
                ADDRESS,
                {
                    "commitment": "finalized",
                    "encoding": "base64",
                    "minContextSlot": 700,
                },
            ],
        )

    def test_newer_context_is_not_historical_evidence(self) -> None:
        result = asyncio.run(
            observe_account_info(
                ADDRESS,
                endpoint="https://rpc.example",
                transport=_FakeTransport(
                    _rpc_response(
                        {
                            "context": {"slot": 701},
                            "value": _account_value(),
                        }
                    )
                ),
                as_of_slot=700,
            )
        )

        self.assertIsInstance(result, AbstainResult)
        if isinstance(result, AbstainResult):
            self.assertEqual(result.reason, AbstainReason.STALE_STATE)
            self.assertEqual(result.as_of_slot, 701)

    def test_malformed_json_abstains(self) -> None:
        """Malformed transport bytes never become account evidence."""

        result = asyncio.run(
            observe_account_info(
                ADDRESS,
                endpoint="https://rpc.example",
                transport=_FakeTransport(b"not-json"),
            )
        )

        self.assertIsInstance(result, AbstainResult)
        self.assertEqual(
            cast("AbstainResult", result).reason,
            AbstainReason.UNKNOWN_PROTOCOL_STATE,
        )

    def test_non_finalized_context_abstains(self) -> None:
        """An explicitly non-finalized response context is rejected."""

        body = _rpc_response(
            {
                "context": {"commitment": "confirmed", "slot": 700},
                "value": _account_value(),
            }
        )

        result = asyncio.run(
            observe_account_info(
                ADDRESS,
                endpoint="https://rpc.example",
                transport=_FakeTransport(body),
            )
        )

        self.assertIsInstance(result, AbstainResult)
        abstention = cast("AbstainResult", result)
        self.assertEqual(abstention.reason, AbstainReason.STALE_STATE)
        self.assertEqual(abstention.as_of_slot, 700)

    def test_missing_account_data_abstains(self) -> None:
        """Null values and omitted raw data cannot enter the backtest."""

        for value in (None, {"owner": OWNER}):
            with self.subTest(value=value):
                body = _rpc_response({"context": {"slot": 700}, "value": value})
                result = asyncio.run(
                    observe_account_info(
                        ADDRESS,
                        endpoint="https://rpc.example",
                        transport=_FakeTransport(body),
                    )
                )

                self.assertIsInstance(result, AbstainResult)
                self.assertEqual(
                    cast("AbstainResult", result).reason,
                    AbstainReason.MISSING_FEATURE,
                )

    def test_unsupported_encoding_abstains(self) -> None:
        """Parsed or alternate account encodings are not silently accepted."""

        body = _rpc_response(
            {
                "context": {"slot": 700},
                "value": {
                    "data": [base64.b64encode(ACCOUNT_BYTES).decode(), "base58"],
                    "owner": OWNER,
                },
            }
        )

        result = asyncio.run(
            observe_account_info(
                ADDRESS,
                endpoint="https://rpc.example",
                transport=_FakeTransport(body),
            )
        )

        self.assertIsInstance(result, AbstainResult)
        self.assertEqual(
            cast("AbstainResult", result).reason,
            AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
        )

    def test_malformed_base64_account_bytes_abstain(self) -> None:
        """Malformed raw bytes are rejected even when the encoding is allowlisted."""

        body = _rpc_response(
            {
                "context": {"slot": 700},
                "value": {"data": ["not-base64!", "base64"], "owner": OWNER},
            }
        )

        result = asyncio.run(
            observe_account_info(
                ADDRESS,
                endpoint="https://rpc.example",
                transport=_FakeTransport(body),
            )
        )

        self.assertIsInstance(result, AbstainResult)
        self.assertEqual(
            cast("AbstainResult", result).reason,
            AbstainReason.UNKNOWN_PROTOCOL_STATE,
        )

    def test_invalid_request_does_not_call_transport(self) -> None:
        """Invalid account identity is rejected before any RPC request."""

        transport = _FakeTransport(_rpc_response({}))
        result = asyncio.run(
            observe_account_info(
                "not-an-address",
                endpoint="https://rpc.example",
                transport=transport,
            )
        )

        self.assertIsInstance(result, AbstainResult)
        self.assertEqual(transport.calls, [])

    def test_multiple_accounts_share_one_finalized_context_slot(self) -> None:
        """Paper context inputs cannot combine account reads from different slots."""

        second_address = base58.b58encode(b"second".ljust(32, b"s")).decode()
        body = _rpc_response(
            {
                "context": {"slot": 700},
                "value": [_account_value(), _account_value()],
            }
        )
        transport = _FakeTransport(body)

        result = asyncio.run(
            observe_multiple_account_info(
                (ADDRESS, second_address),
                endpoint="https://rpc.example",
                source_id="test-batch-rpc",
                observer_id="test-observer",
                boot_id=BOOT_ID,
                receive_sequence_start=10,
                transport=transport,
            )
        )

        self.assertIsInstance(result, tuple)
        observations = cast("tuple[RawChainObservation, ...]", result)
        self.assertEqual([item.slot for item in observations], [700, 700])
        self.assertEqual([item.receive_sequence for item in observations], [11, 12])
        self.assertEqual(transport.calls[0]["method"], "getMultipleAccounts")
        self.assertEqual(
            transport.calls[0]["params"],
            [
                [ADDRESS, second_address],
                {"commitment": "finalized", "encoding": "base64"},
            ],
        )

    def test_multiple_accounts_rejects_newer_requested_context(self) -> None:
        """A minimum slot is not silently treated as an exact historical read."""

        result = asyncio.run(
            observe_multiple_account_info(
                (ADDRESS,),
                endpoint="https://rpc.example",
                transport=_FakeTransport(
                    _rpc_response(
                        {"context": {"slot": 701}, "value": [_account_value()]}
                    )
                ),
                as_of_slot=700,
            )
        )

        self.assertIsInstance(result, AbstainResult)
        self.assertEqual(
            cast("AbstainResult", result).reason,
            AbstainReason.STALE_STATE,
        )


class _FakeTransport:
    def __init__(self, response_body: bytes) -> None:
        self._response_body = response_body
        self.calls: list[dict[str, object]] = []

    async def __call__(self, _endpoint: str, body: bytes) -> RpcHttpResponse:
        request = cast("dict[str, object]", json.loads(body))
        self.calls.append(request)
        return RpcHttpResponse(status=200, body=self._response_body)


def _account_value() -> dict[str, object]:
    return {
        "data": [base64.b64encode(ACCOUNT_BYTES).decode(), "base64"],
        "owner": OWNER,
    }


def _rpc_response(result: object, *, whitespace: bool = False) -> bytes:
    payload = {"jsonrpc": "2.0", "id": 1, "result": result}
    if whitespace:
        return json.dumps(payload, indent=2).encode()
    return json.dumps(payload, separators=(",", ":")).encode()


if __name__ == "__main__":
    unittest.main()
