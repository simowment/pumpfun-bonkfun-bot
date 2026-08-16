"""Focused tests for the bounded HTTP-only RPC observer."""

import asyncio
import json
import unittest
from collections.abc import Mapping
from typing import TYPE_CHECKING, cast
from uuid import UUID

import base58

from rugbot.domain.decisions import AbstainReason, AbstainResult
from rugbot.ingest.rpc_observer import (
    JSON_TRANSACTION_FORMAT,
    AddressHistoryCursor,
    RpcHttpResponse,
    observe_address,
)
from rugbot.runtime.observation_loop import RpcAddressObservationSource

if TYPE_CHECKING:
    from rugbot.domain.observations import RawChainObservation

ADDRESS = base58.b58encode(b"address".ljust(32, b"a")).decode()
SIGNATURE_ONE = base58.b58encode(b"signature-one".ljust(64, b"x")).decode()
SIGNATURE_TWO = base58.b58encode(b"signature-two".ljust(64, b"x")).decode()
SIGNATURE_THREE = base58.b58encode(b"signature-three".ljust(64, b"x")).decode()
BOOT_ID = UUID("00000000-0000-0000-0000-000000000001")


class RpcObserverTests(unittest.TestCase):
    """Tests for bounded finalized JSON-RPC observation."""

    def test_emits_bounded_finalized_observations_and_preserves_response_bytes(
        self,
    ) -> None:
        """Only the configured transaction bound is fetched and retained exactly."""

        transaction_body = _rpc_response(
            {
                "slot": 500,
                "transaction": {
                    "signatures": [SIGNATURE_ONE, SIGNATURE_TWO],
                },
                "meta": {"err": None},
            },
            whitespace=True,
        )
        transport = _FakeTransport(
            {
                "getSlot": _rpc_response(500),
                "getSignaturesForAddress": [
                    _rpc_response(
                        [
                            {
                                "signature": SIGNATURE_ONE,
                                "slot": 500,
                                "confirmationStatus": "finalized",
                            },
                            {
                                "signature": SIGNATURE_TWO,
                                "slot": 500,
                                "confirmationStatus": "finalized",
                            },
                        ]
                    ),
                    _rpc_response([]),
                ],
                "getTransaction": transaction_body,
                "getBlock": _block_response(SIGNATURE_ONE, SIGNATURE_TWO),
            }
        )

        result = asyncio.run(
            observe_address(
                ADDRESS,
                endpoint="https://rpc.example",
                source_id="test-rpc",
                observer_id="test-observer",
                boot_id=BOOT_ID,
                max_signatures=2,
                max_transactions=2,
                transport=transport,
            )
        )

        self.assertIsInstance(result, tuple)
        observations = cast("tuple", result)
        self.assertEqual(len(observations), 2)
        observation = observations[0]
        self.assertEqual(observation.slot, 500)
        self.assertEqual(observation.signature, base58.b58decode(SIGNATURE_ONE))
        self.assertEqual(observation.commitment, "finalized")
        self.assertEqual(observation.canonical_status, "canonical")
        self.assertEqual(observation.source_update_kind, "transaction")
        self.assertEqual(observation.raw_source_payload, transaction_body)
        self.assertEqual(observation.raw_transaction, transaction_body)
        self.assertEqual(observation.raw_transaction_format, JSON_TRANSACTION_FORMAT)
        self.assertEqual(
            [call["method"] for call in transport.calls],
            [
                "getSlot",
                "getSignaturesForAddress",
                "getTransaction",
                "getBlock",
                "getTransaction",
            ],
        )
        self.assertEqual(
            transport.calls[1]["params"],
            [
                ADDRESS,
                {"commitment": "finalized", "limit": 2},
            ],
        )
        self.assertEqual(
            transport.calls[2]["params"][1],
            {
                "commitment": "finalized",
                "encoding": "json",
                "maxSupportedTransactionVersion": 0,
            },
        )
        self.assertEqual(
            transport.calls[3]["params"][1],
            {
                "commitment": "finalized",
                "maxSupportedTransactionVersion": 0,
                "rewards": False,
                "transactionDetails": "full",
            },
        )

        with self.assertRaises(AttributeError):
            observation.slot = 501

    def test_helius_history_paginates_indices_and_hydrates_canonical_rpc_bodies(
        self,
    ) -> None:
        """Helius discovers bounded indices; finalized RPC remains canonical."""

        transport = _FakeTransport(
            {
                "getSlot": _rpc_response(500),
                "getTransactionsForAddress": [
                    _rpc_response(
                        {
                            "data": [_helius_full_item(SIGNATURE_ONE, 500, 7)],
                            "paginationToken": "500:1",
                        }
                    ),
                    _rpc_response(
                        {
                            "data": [_helius_full_item(SIGNATURE_TWO, 500, 8)],
                            "paginationToken": None,
                        }
                    ),
                ],
            }
        )

        result = asyncio.run(
            observe_address(
                ADDRESS,
                endpoint="https://mainnet.helius-rpc.com/?api-key=test",
                max_signatures=1,
                max_transactions=2,
                max_pages=2,
                start_slot=500,
                end_slot=500,
                transport=transport,
            )
        )

        self.assertIsInstance(result, tuple)
        observations = cast("tuple", result)
        self.assertEqual([item.transaction_index for item in observations], [7, 8])
        self.assertEqual(
            [item.raw_source_payload is not None for item in observations],
            [True, True],
        )
        self.assertEqual(
            [call["method"] for call in transport.calls],
            [
                "getSlot",
                "getTransactionsForAddress",
                "getTransactionsForAddress",
            ],
        )
        self.assertEqual(
            transport.calls[1]["params"],
            [
                ADDRESS,
                {
                    "commitment": "finalized",
                    "filters": {"slot": {"gte": 500, "lte": 500}},
                    "limit": 1,
                    "sortOrder": "desc",
                    "transactionDetails": "full",
                },
            ],
        )
        self.assertEqual(transport.calls[2]["params"][1]["paginationToken"], "500:1")

    def test_malformed_helius_history_abstains_before_hydration(self) -> None:
        """A missing Helius transaction index is unknown protocol state."""

        transport = _FakeTransport(
            {
                "getSlot": _rpc_response(500),
                "getTransactionsForAddress": _rpc_response(
                    {
                        "data": [
                            {
                                "signature": SIGNATURE_ONE,
                                "slot": 500,
                                "confirmationStatus": "finalized",
                            }
                        ],
                        "paginationToken": None,
                    }
                ),
            }
        )

        result = asyncio.run(
            observe_address(
                ADDRESS,
                endpoint="https://mainnet.helius-rpc.com/?api-key=test",
                start_slot=500,
                end_slot=500,
                transport=transport,
            )
        )

        self.assertIsInstance(result, AbstainResult)
        self.assertEqual(
            cast("AbstainResult", result).reason,
            AbstainReason.UNKNOWN_PROTOCOL_STATE,
        )
        self.assertEqual(
            [call["method"] for call in transport.calls],
            ["getSlot", "getTransactionsForAddress"],
        )

    def test_helius_slot_cursor_below_window_proves_completion(self) -> None:
        """A cursor below the inclusive floor proves the filtered window is complete."""

        transport = _FakeTransport(
            {
                "getSlot": _rpc_response(600),
                "getTransactionsForAddress": _rpc_response(
                    {
                        "data": [_helius_full_item(SIGNATURE_ONE, 500, 7)],
                        "paginationToken": "499:0",
                    }
                ),
            }
        )

        result = asyncio.run(
            observe_address(
                ADDRESS,
                endpoint="https://mainnet.helius-rpc.com/?api-key=test",
                max_signatures=1,
                max_transactions=1,
                max_pages=1,
                start_slot=500,
                end_slot=500,
                transport=transport,
            )
        )

        self.assertIsInstance(result, tuple)
        observations = cast("tuple", result)
        self.assertEqual(len(observations), 1)
        self.assertEqual(observations[0].slot, 500)
        self.assertEqual(
            [call["method"] for call in transport.calls],
            ["getSlot", "getTransactionsForAddress"],
        )

    def test_missing_transaction_evidence_abstains_without_partial_observations(
        self,
    ) -> None:
        """A pruned or unavailable finalized transaction fails closed."""

        transport = _FakeTransport(
            {
                "getSlot": _rpc_response(500),
                "getSignaturesForAddress": [
                    _rpc_response(
                        [
                            {
                                "signature": SIGNATURE_ONE,
                                "slot": 500,
                                "confirmationStatus": "finalized",
                            }
                        ]
                    ),
                    _rpc_response([]),
                ],
                "getTransaction": _rpc_response(None),
            }
        )

        result = asyncio.run(
            observe_address(
                ADDRESS,
                endpoint="https://rpc.example",
                boot_id=BOOT_ID,
                max_signatures=1,
                max_transactions=1,
                transport=transport,
            )
        )

        self.assertIsInstance(result, AbstainResult)
        abstention = cast("AbstainResult", result)
        self.assertEqual(abstention.reason, AbstainReason.MISSING_FEATURE)
        self.assertEqual(abstention.as_of_slot, 500)
        self.assertEqual(len(transport.calls), 3)

    def test_missing_execution_metadata_abstains_without_partial_observations(
        self,
    ) -> None:
        """A transaction with null metadata cannot prove execution state."""

        transport = _FakeTransport(
            {
                "getSlot": _rpc_response(500),
                "getSignaturesForAddress": [
                    _rpc_response(
                        [
                            {
                                "signature": SIGNATURE_ONE,
                                "slot": 500,
                                "confirmationStatus": "finalized",
                            }
                        ]
                    )
                ],
                "getTransaction": _rpc_response(
                    {
                        "slot": 500,
                        "transaction": {"signatures": [SIGNATURE_ONE]},
                        "meta": None,
                    }
                ),
            }
        )

        result = asyncio.run(
            observe_address(
                ADDRESS,
                endpoint="https://rpc.example",
                boot_id=BOOT_ID,
                max_signatures=1,
                max_transactions=1,
                transport=transport,
            )
        )

        self.assertIsInstance(result, AbstainResult)
        abstention = cast("AbstainResult", result)
        self.assertEqual(abstention.reason, AbstainReason.MISSING_FEATURE)
        self.assertEqual(abstention.as_of_slot, 500)
        self.assertEqual(
            [call["method"] for call in transport.calls],
            ["getSlot", "getSignaturesForAddress", "getTransaction"],
        )

    def test_finalized_history_slot_can_advance_after_initial_slot_read(self) -> None:
        """A healthy finalized RPC is not rejected for one-slot read skew."""

        transport = _FakeTransport(
            {
                "getSlot": _rpc_response(500),
                "getSignaturesForAddress": [
                    _rpc_response(
                        [
                            {
                                "signature": SIGNATURE_ONE,
                                "slot": 501,
                                "confirmationStatus": "finalized",
                            }
                        ]
                    ),
                    _rpc_response([]),
                ],
                "getTransaction": _rpc_response(
                    {
                        "slot": 501,
                        "transaction": {"signatures": [SIGNATURE_ONE]},
                        "meta": {"err": None},
                    }
                ),
                "getBlock": _block_response(SIGNATURE_ONE),
            }
        )

        result = asyncio.run(
            observe_address(
                ADDRESS,
                endpoint="https://rpc.example",
                boot_id=BOOT_ID,
                max_signatures=1,
                max_transactions=1,
                transport=transport,
            )
        )

        self.assertIsInstance(result, tuple)
        self.assertEqual(cast("tuple", result)[0].slot, 501)

    def test_cursor_is_sent_and_boundary_signature_is_not_replayed(self) -> None:
        """A supplied history cursor bounds the next finalized poll."""

        transport = _FakeTransport(
            {
                "getSlot": _rpc_response(500),
                "getSignaturesForAddress": _rpc_response(
                    [
                        {
                            "signature": SIGNATURE_ONE,
                            "slot": 500,
                            "confirmationStatus": "finalized",
                        }
                    ]
                ),
            }
        )

        result = asyncio.run(
            observe_address(
                ADDRESS,
                endpoint="https://rpc.example",
                max_signatures=1,
                max_transactions=1,
                cursor=AddressHistoryCursor(
                    address=ADDRESS,
                    source_id="solana-http-rpc",
                    until_signature=SIGNATURE_ONE,
                    receive_sequence=1,
                ),
                transport=transport,
            )
        )

        self.assertEqual(result, ())
        self.assertEqual(
            transport.calls[1]["params"],
            [
                ADDRESS,
                {
                    "commitment": "finalized",
                    "limit": 1,
                    "until": SIGNATURE_ONE,
                },
            ],
        )

    def test_cursor_overflow_abstains_instead_of_skipping_history(self) -> None:
        """More new signatures than the transaction bound fail closed."""

        transport = _FakeTransport(
            {
                "getSlot": _rpc_response(500),
                "getSignaturesForAddress": _rpc_response(
                    [
                        {
                            "signature": SIGNATURE_ONE,
                            "slot": 500,
                            "confirmationStatus": "finalized",
                        },
                        {
                            "signature": SIGNATURE_TWO,
                            "slot": 499,
                            "confirmationStatus": "finalized",
                        },
                        {
                            "signature": SIGNATURE_THREE,
                            "slot": 498,
                            "confirmationStatus": "finalized",
                        },
                    ]
                ),
            }
        )

        result = asyncio.run(
            observe_address(
                ADDRESS,
                endpoint="https://rpc.example",
                max_signatures=3,
                max_transactions=1,
                cursor=AddressHistoryCursor(
                    address=ADDRESS,
                    source_id="solana-http-rpc",
                    until_signature=SIGNATURE_THREE,
                    receive_sequence=1,
                ),
                transport=transport,
            )
        )

        self.assertIsInstance(result, AbstainResult)
        self.assertEqual(
            cast("AbstainResult", result).reason,
            AbstainReason.UNKNOWN_PROTOCOL_STATE,
        )
        self.assertEqual(len(transport.calls), 2)

    def test_failed_finalized_transaction_is_skipped_without_observation(self) -> None:
        """A finalized execution error cannot become a state transition."""

        transport = _FakeTransport(
            {
                "getSlot": _rpc_response(500),
                "getSignaturesForAddress": [
                    _rpc_response(
                        [
                            {
                                "signature": SIGNATURE_ONE,
                                "slot": 500,
                                "confirmationStatus": "finalized",
                            }
                        ]
                    ),
                    _rpc_response([]),
                ],
                "getTransaction": _rpc_response(
                    {
                        "slot": 500,
                        "transaction": {"signatures": [SIGNATURE_ONE]},
                        "meta": {"err": {"InstructionError": [0, "Custom"]}},
                    }
                ),
            }
        )

        result = asyncio.run(
            observe_address(
                ADDRESS,
                endpoint="https://rpc.example",
                max_signatures=1,
                max_transactions=1,
                transport=transport,
            )
        )

        self.assertEqual(result, ())
        self.assertEqual(len(transport.calls), 3)

    def test_failed_transaction_does_not_block_following_success(self) -> None:
        """Known failed attempts are skipped while later finalized evidence is kept."""

        successful_body = _rpc_response(
            {
                "slot": 500,
                "transaction": {"signatures": [SIGNATURE_TWO]},
                "meta": {"err": None},
            }
        )
        transport = _FakeTransport(
            {
                "getSlot": _rpc_response(500),
                "getSignaturesForAddress": [
                    _rpc_response(
                        [
                            {
                                "signature": SIGNATURE_ONE,
                                "slot": 500,
                                "confirmationStatus": "finalized",
                            },
                            {
                                "signature": SIGNATURE_TWO,
                                "slot": 500,
                                "confirmationStatus": "finalized",
                            },
                        ]
                    ),
                    _rpc_response([]),
                ],
                "getTransaction": [
                    _rpc_response(
                        {
                            "slot": 500,
                            "transaction": {"signatures": [SIGNATURE_ONE]},
                            "meta": {"err": {"InstructionError": [0, "Custom"]}},
                        }
                    ),
                    successful_body,
                ],
                "getBlock": _block_response(SIGNATURE_TWO),
            }
        )

        result = asyncio.run(
            observe_address(
                ADDRESS,
                endpoint="https://rpc.example",
                max_signatures=2,
                max_transactions=2,
                transport=transport,
            )
        )

        self.assertIsInstance(result, tuple)
        observations = cast("tuple", result)
        self.assertEqual(len(observations), 1)
        self.assertEqual(observations[0].signature, base58.b58decode(SIGNATURE_TWO))

    def test_non_finalized_history_is_rejected_before_transaction_fetch(self) -> None:
        """The observer does not trust a history row without finalized status."""

        transport = _FakeTransport(
            {
                "getSlot": _rpc_response(500),
                "getSignaturesForAddress": _rpc_response(
                    [
                        {
                            "signature": SIGNATURE_ONE,
                            "slot": 500,
                            "confirmationStatus": "confirmed",
                        }
                    ]
                ),
            }
        )

        result = asyncio.run(
            observe_address(
                ADDRESS,
                endpoint="https://rpc.example",
                boot_id=BOOT_ID,
                max_signatures=1,
                max_transactions=1,
                transport=transport,
            )
        )

        self.assertIsInstance(result, AbstainResult)
        abstention = cast("AbstainResult", result)
        self.assertEqual(abstention.reason, AbstainReason.UNKNOWN_PROTOCOL_STATE)
        self.assertEqual(abstention.as_of_slot, 500)
        self.assertEqual(len(transport.calls), 2)

    def test_invalid_limits_and_address_abstain_before_transport(self) -> None:
        """Invalid input cannot widen the observer or trigger an RPC call."""

        transport = _FakeTransport({})

        valid_limit_transport = _FakeTransport(
            {
                "getSlot": _rpc_response(500),
                "getSignaturesForAddress": _rpc_response([]),
            }
        )
        valid_limit = asyncio.run(
            observe_address(
                ADDRESS,
                endpoint="https://rpc.example",
                max_signatures=1000,
                max_transactions=1000,
                transport=valid_limit_transport,
            )
        )

        invalid_limit = asyncio.run(
            observe_address(
                ADDRESS,
                endpoint="https://rpc.example",
                max_signatures=1001,
                transport=transport,
            )
        )
        invalid_address = asyncio.run(
            observe_address(
                "not-an-address",
                endpoint="https://rpc.example",
                transport=transport,
            )
        )

        self.assertIsInstance(invalid_limit, AbstainResult)
        self.assertIsInstance(invalid_address, AbstainResult)
        self.assertEqual(valid_limit, ())
        self.assertEqual(transport.calls, [])

    def test_non_http_endpoint_abstains_before_transport(self) -> None:
        """Only HTTPS endpoints are inside the observer boundary."""

        transport = _FakeTransport({})

        for endpoint in ("http://rpc.example", "wss://rpc.example"):
            with self.subTest(endpoint=endpoint):
                result = asyncio.run(
                    observe_address(
                        ADDRESS,
                        endpoint=endpoint,
                        transport=transport,
                    )
                )

                self.assertIsInstance(result, AbstainResult)
        self.assertEqual(transport.calls, [])


class _FakeTransport:
    def __init__(self, responses: Mapping[str, bytes | list[bytes]]) -> None:
        self._responses = dict(responses)
        self.calls: list[dict[str, object]] = []

    async def __call__(self, _endpoint: str, body: bytes) -> RpcHttpResponse:
        request = cast("dict[str, object]", json.loads(body))
        self.calls.append(request)
        method = cast("str", request["method"])
        response = self._responses.get(method)
        if response is None:
            raise AssertionError
        if isinstance(response, list):
            if not response:
                raise AssertionError
            response_body = response.pop(0)
        else:
            response_body = response
        return RpcHttpResponse(status=200, body=response_body)


def _rpc_response(result: object, *, whitespace: bool = False) -> bytes:
    if whitespace:
        return json.dumps(
            {"jsonrpc": "2.0", "id": 1, "result": result},
            indent=2,
        ).encode()
    return json.dumps(
        {"jsonrpc": "2.0", "id": 1, "result": result},
        separators=(",", ":"),
    ).encode()


def _helius_full_item(
    signature: str,
    slot: int,
    transaction_index: int,
) -> dict[str, object]:
    return {
        "slot": slot,
        "transactionIndex": transaction_index,
        "transaction": {"signatures": [signature], "message": {"instructions": []}},
        "meta": {"err": None},
        "version": 0,
        "blockTime": 1,
    }


def _block_response(*signatures: str) -> bytes:
    return _rpc_response(
        {"transactions": [{"transaction": {"signatures": list(signatures)}}]}
    )


class PaginationGapTests(unittest.TestCase):
    """Verify bounded history never turns an incomplete poll into data."""

    def test_bootstrap_uses_newest_bounded_window(self) -> None:
        """Bootstrap takes only the newest configured transaction window."""

        transport = _FakeTransport(
            {
                "getSlot": _rpc_response(500),
                "getSignaturesForAddress": [
                    _rpc_response(
                        [
                            {
                                "signature": SIGNATURE_ONE,
                                "slot": 500,
                                "confirmationStatus": "finalized",
                            },
                            {
                                "signature": SIGNATURE_TWO,
                                "slot": 500,
                                "confirmationStatus": "finalized",
                            },
                        ]
                    ),
                ],
                "getTransaction": _rpc_response(
                    {
                        "slot": 500,
                        "transaction": {
                            "signatures": [
                                SIGNATURE_ONE,
                                SIGNATURE_TWO,
                                SIGNATURE_THREE,
                            ]
                        },
                        "meta": {"err": None},
                    }
                ),
                "getBlock": _block_response(
                    SIGNATURE_ONE,
                    SIGNATURE_TWO,
                    SIGNATURE_THREE,
                ),
            }
        )

        result = asyncio.run(
            observe_address(
                ADDRESS,
                endpoint="https://rpc.example",
                max_signatures=2,
                max_transactions=2,
                max_pages=2,
                transport=transport,
            )
        )

        self.assertIsInstance(result, tuple)
        self.assertEqual(len(cast("tuple", result)), 2)
        self.assertEqual(
            transport.calls[1]["params"][1],
            {
                "commitment": "finalized",
                "limit": 2,
            },
        )
        self.assertEqual(
            [call["method"] for call in transport.calls],
            [
                "getSlot",
                "getSignaturesForAddress",
                "getTransaction",
                "getBlock",
                "getTransaction",
            ],
        )

    def test_slot_window_paginates_past_newer_history(self) -> None:
        """A slot window is complete only after pagination crosses its floor."""

        transport = _FakeTransport(
            {
                "getSlot": _rpc_response(600),
                "getSignaturesForAddress": [
                    _rpc_response(
                        [
                            {
                                "signature": SIGNATURE_THREE,
                                "slot": 600,
                                "confirmationStatus": "finalized",
                            },
                            {
                                "signature": SIGNATURE_TWO,
                                "slot": 500,
                                "confirmationStatus": "finalized",
                            },
                        ]
                    ),
                    _rpc_response(
                        [
                            {
                                "signature": SIGNATURE_ONE,
                                "slot": 499,
                                "confirmationStatus": "finalized",
                            }
                        ]
                    ),
                ],
                "getTransaction": _rpc_response(
                    {
                        "slot": 500,
                        "transaction": {"signatures": [SIGNATURE_TWO]},
                        "meta": {"err": None},
                    }
                ),
                "getBlock": _block_response(SIGNATURE_TWO),
            }
        )

        result = asyncio.run(
            observe_address(
                ADDRESS,
                endpoint="https://rpc.example",
                max_signatures=2,
                max_transactions=2,
                max_pages=2,
                start_slot=500,
                end_slot=500,
                transport=transport,
            )
        )

        self.assertIsInstance(result, tuple)
        observations = cast("tuple", result)
        self.assertEqual(len(observations), 1)
        self.assertEqual(observations[0].signature, base58.b58decode(SIGNATURE_TWO))
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
        self.assertEqual(transport.calls[2]["params"][1]["before"], SIGNATURE_TWO)

    def test_page_budget_exhaustion_abstains_before_transaction_fetch(self) -> None:
        """A full final page without proof of completion is a hard abstention."""

        page = _rpc_response(
            [
                {
                    "signature": SIGNATURE_ONE,
                    "slot": 500,
                    "confirmationStatus": "finalized",
                },
                {
                    "signature": SIGNATURE_TWO,
                    "slot": 499,
                    "confirmationStatus": "finalized",
                },
            ]
        )
        transport = _FakeTransport(
            {
                "getSlot": _rpc_response(500),
                "getSignaturesForAddress": page,
            }
        )

        result = asyncio.run(
            observe_address(
                ADDRESS,
                endpoint="https://rpc.example",
                max_signatures=2,
                max_transactions=2,
                max_pages=1,
                cursor=AddressHistoryCursor(
                    address=ADDRESS,
                    source_id="solana-http-rpc",
                    until_signature=SIGNATURE_THREE,
                    receive_sequence=0,
                ),
                transport=transport,
            )
        )

        self.assertIsInstance(result, AbstainResult)
        self.assertEqual(
            cast("AbstainResult", result).reason, AbstainReason.UNKNOWN_PROTOCOL_STATE
        )
        self.assertEqual(
            [call["method"] for call in transport.calls],
            ["getSlot", "getSignaturesForAddress"],
        )

    def test_online_cursor_can_restart_without_replaying_history(self) -> None:
        """A copied typed cursor resumes at the next finalized signature."""

        first_transport = _FakeTransport(
            {
                "getSlot": _rpc_response(500),
                "getSignaturesForAddress": [
                    _rpc_response(
                        [
                            {
                                "signature": SIGNATURE_ONE,
                                "slot": 500,
                                "confirmationStatus": "finalized",
                            }
                        ]
                    ),
                    _rpc_response([]),
                ],
                "getTransaction": _rpc_response(
                    {
                        "slot": 500,
                        "transaction": {"signatures": [SIGNATURE_ONE]},
                        "meta": {"err": None},
                    }
                ),
                "getBlock": _block_response(SIGNATURE_ONE),
            }
        )
        source = RpcAddressObservationSource(
            address=ADDRESS,
            endpoint="https://rpc.example",
            max_signatures=1,
            max_transactions=1,
            transport=first_transport,
        )
        first = asyncio.run(source.read())
        source.acknowledge(cast("tuple[RawChainObservation, ...]", first))
        cursor = source.cursor

        self.assertIsNotNone(cursor)
        self.assertEqual(cursor.until_signature, SIGNATURE_ONE)
        self.assertEqual(cursor.receive_sequence, 1)

        second_transport = _FakeTransport(
            {
                "getSlot": _rpc_response(501),
                "getSignaturesForAddress": [
                    _rpc_response(
                        [
                            {
                                "signature": SIGNATURE_TWO,
                                "slot": 501,
                                "confirmationStatus": "finalized",
                            }
                        ]
                    ),
                    _rpc_response([]),
                ],
                "getTransaction": _rpc_response(
                    {
                        "slot": 501,
                        "transaction": {"signatures": [SIGNATURE_TWO]},
                        "meta": {"err": None},
                    }
                ),
                "getBlock": _block_response(SIGNATURE_TWO),
            }
        )
        restarted = RpcAddressObservationSource(
            address=ADDRESS,
            endpoint="https://rpc.example",
            max_signatures=1,
            max_transactions=1,
            cursor=cursor,
            transport=second_transport,
        )
        second = asyncio.run(restarted.read())

        self.assertIsInstance(first, tuple)
        self.assertIsInstance(second, tuple)
        self.assertEqual(cast("tuple", second)[0].receive_sequence, 2)
        self.assertEqual(second_transport.calls[1]["params"][1]["until"], SIGNATURE_ONE)


if __name__ == "__main__":
    unittest.main()
