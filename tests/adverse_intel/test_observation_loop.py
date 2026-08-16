"""Parity tests for the shared online and offline observation loop."""

import asyncio
import json
import tempfile
import unittest
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path
from typing import cast
from uuid import UUID

import base58

from rugbot.domain.decisions import AbstainReason, AbstainResult
from rugbot.domain.observations import RawChainObservation
from rugbot.ingest.checkpoints import SourceCheckpoint
from rugbot.ingest.observation_pipeline import DurableObservationIngestor
from rugbot.ingest.rpc_observer import RpcHttpResponse
from rugbot.runtime.observation_loop import (
    JsonlReplayObservationSource,
    MemoryObservationSource,
    RpcAddressObservationSource,
    SharedObservationLoop,
)
from rugbot.storage.handled_evidence_ledger import (
    JsonlHandledEvidenceLedger,
)
from rugbot.storage.jsonl_observation_store import (
    JsonlObservationStore,
    observation_identity,
)

ADDRESS = base58.b58encode(b"address".ljust(32, b"a")).decode()
SIGNATURE = base58.b58encode(b"signature".ljust(64, b"x")).decode()
SIGNATURE_TWO = base58.b58encode(b"signature-two".ljust(64, b"x")).decode()


class ObservationLoopTests(unittest.TestCase):
    """Verify source-specific adapters share one processing path."""

    def test_online_and_jsonl_replay_have_identical_downstream_behavior(self) -> None:
        """RPC and replay sources produce the same loop report and inputs."""

        transport = _FakeTransport(
            {
                "getSlot": _rpc_response(500),
                "getSignaturesForAddress": [
                    _rpc_response(
                        [
                            {
                                "signature": SIGNATURE,
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
                        "transaction": {"signatures": [SIGNATURE]},
                        "meta": {"err": None},
                    }
                ),
                "getBlock": _rpc_response(
                    {"transactions": [{"transaction": {"signatures": [SIGNATURE]}}]}
                ),
            }
        )

        with tempfile.TemporaryDirectory() as directory:
            online_path = Path(directory) / "online.jsonl"
            source = RpcAddressObservationSource(
                address=ADDRESS,
                endpoint="https://rpc.example",
                transport=transport,
                max_signatures=1,
                max_transactions=1,
            )
            online_batch = asyncio.run(source.read())
            self.assertIsInstance(online_batch, tuple)
            observations = cast("tuple[RawChainObservation, ...]", online_batch)
            self.assertEqual(len(observations), 1)

            online_handler = _RecordingHandler()
            online_loop = _loop(online_path, Path(directory) / "online.handled.jsonl")
            online_report = asyncio.run(
                online_loop.run_once(
                    source=MemoryObservationSource(observations), handler=online_handler
                )
            )
            source.acknowledge(observations)

            replay_handler = _RecordingHandler()
            replay_loop = _loop(online_path, Path(directory) / "replay.handled.jsonl")
            replay_report = asyncio.run(
                replay_loop.run_once(
                    source=JsonlReplayObservationSource(online_path),
                    handler=replay_handler,
                )
            )

            self.assertEqual(online_report.as_of_slot, replay_report.as_of_slot)
            self.assertEqual(online_report.observed_count, replay_report.observed_count)
            self.assertEqual(online_report.handled_count, replay_report.handled_count)
            self.assertEqual(online_report.evidence_ids, replay_report.evidence_ids)
            self.assertEqual(online_report.persisted_count, 1)
            self.assertEqual(replay_report.persisted_count, 0)
            self.assertEqual(online_handler.observations, replay_handler.observations)
            self.assertEqual(online_report.handled_count, 1)
            self.assertEqual(online_report.as_of_slot, 500)

    def test_online_cursor_restores_from_shared_loop_raw_store_on_restart(self) -> None:
        """A restarted source resumes after the newest durable transaction."""

        with tempfile.TemporaryDirectory() as directory:
            raw_path = Path(directory) / "observations.jsonl"
            first_transport = _FakeTransport(
                {
                    "getSlot": _rpc_response(500),
                    "getSignaturesForAddress": [
                        _rpc_response(
                            [
                                {
                                    "signature": SIGNATURE,
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
                            "transaction": {"signatures": [SIGNATURE]},
                            "meta": {"err": None},
                        }
                    ),
                    "getBlock": _rpc_response(
                        {"transactions": [{"transaction": {"signatures": [SIGNATURE]}}]}
                    ),
                }
            )
            first_source = RpcAddressObservationSource(
                address=ADDRESS,
                endpoint="https://rpc.example",
                source_id="restart-source",
                transport=first_transport,
                max_signatures=1,
                max_transactions=1,
            )
            first_batch = asyncio.run(first_source.read())
            self.assertIsInstance(first_batch, tuple)
            first_observations = cast("tuple[RawChainObservation, ...]", first_batch)
            self.assertEqual(len(first_observations), 1)

            loop = _loop(raw_path, Path(directory) / "handled.jsonl")
            report = asyncio.run(
                loop.run_once(
                    MemoryObservationSource(first_observations), _RecordingHandler()
                )
            )
            self.assertTrue(report.accepted)
            first_source.acknowledge(first_observations)

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
                        _rpc_response(
                            [
                                {
                                    "signature": SIGNATURE,
                                    "slot": 500,
                                    "confirmationStatus": "finalized",
                                }
                            ]
                        ),
                    ],
                    "getTransaction": _rpc_response(
                        {
                            "slot": 501,
                            "transaction": {"signatures": [SIGNATURE_TWO]},
                            "meta": {"err": None},
                        }
                    ),
                    "getBlock": _rpc_response(
                        {
                            "transactions": [
                                {"transaction": {"signatures": [SIGNATURE_TWO]}}
                            ]
                        }
                    ),
                }
            )
            restarted = RpcAddressObservationSource(
                address=ADDRESS,
                endpoint="https://rpc.example",
                source_id="restart-source",
                raw_observation_path=raw_path,
                handled_ledger=JsonlHandledEvidenceLedger(
                    Path(directory) / "handled.jsonl"
                ),
                transport=second_transport,
                max_signatures=1,
                max_transactions=1,
            )
            second_batch = asyncio.run(restarted.read())

            self.assertIsInstance(second_batch, tuple)
            second_observations = cast("tuple[RawChainObservation, ...]", second_batch)
            self.assertEqual(second_observations[0].receive_sequence, 2)
            self.assertEqual(second_transport.calls[1]["params"][1]["until"], SIGNATURE)

    def test_malformed_durable_cursor_state_abstains_before_rpc(self) -> None:
        """Malformed raw state prevents a restarted source from polling."""

        with tempfile.TemporaryDirectory() as directory:
            raw_path = Path(directory) / "observations.jsonl"
            raw_path.write_text("{malformed", encoding="utf-8")
            transport = _FakeTransport({})
            source = RpcAddressObservationSource(
                address=ADDRESS,
                endpoint="https://rpc.example",
                raw_observation_path=raw_path,
                handled_ledger=JsonlHandledEvidenceLedger(
                    Path(directory) / "handled.jsonl"
                ),
                transport=transport,
            )

            result = asyncio.run(source.read())

            self.assertIsInstance(result, AbstainResult)
            self.assertEqual(
                cast("AbstainResult", result).reason,
                AbstainReason.UNKNOWN_PROTOCOL_STATE,
            )
            self.assertEqual(transport.calls, [])

    def test_malformed_handled_cursor_state_abstains_before_rpc(self) -> None:
        """Malformed handled state prevents a restarted source from polling."""

        with tempfile.TemporaryDirectory() as directory:
            raw_path = Path(directory) / "observations.jsonl"
            handled_path = Path(directory) / "handled.jsonl"
            probe = RpcAddressObservationSource(
                address=ADDRESS,
                endpoint="https://rpc.example",
                transport=_FakeTransport({}),
            )
            JsonlObservationStore(raw_path).append(
                _observation_with_source(probe.source_id)
            )
            handled_path.write_text("not-json\n", encoding="utf-8")
            transport = _FakeTransport({})
            source = RpcAddressObservationSource(
                address=ADDRESS,
                endpoint="https://rpc.example",
                raw_observation_path=raw_path,
                handled_ledger=JsonlHandledEvidenceLedger(handled_path),
                transport=transport,
            )

            result = asyncio.run(source.read())

            self.assertIsInstance(result, AbstainResult)
            self.assertEqual(
                cast("AbstainResult", result).reason,
                AbstainReason.UNKNOWN_PROTOCOL_STATE,
            )
            self.assertEqual(transport.calls, [])

    def test_online_source_keeps_receive_sequence_across_polls(self) -> None:
        """Repeated online polls retain one source boot and monotonic sequence."""

        transport = _FakeTransport(
            {
                "getSlot": _rpc_response(500),
                "getSignaturesForAddress": [
                    _rpc_response(
                        [
                            {
                                "signature": SIGNATURE,
                                "slot": 500,
                                "confirmationStatus": "finalized",
                            }
                        ]
                    ),
                    _rpc_response(
                        [
                            {
                                "signature": SIGNATURE_TWO,
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
                        "transaction": {"signatures": [SIGNATURE, SIGNATURE_TWO]},
                        "meta": {"err": None},
                    }
                ),
                "getBlock": _rpc_response(
                    {
                        "transactions": [
                            {"transaction": {"signatures": [SIGNATURE, SIGNATURE_TWO]}}
                        ]
                    }
                ),
            }
        )
        source = RpcAddressObservationSource(
            address=ADDRESS,
            endpoint="https://rpc.example",
            transport=transport,
            max_signatures=1,
            max_transactions=1,
        )

        first = asyncio.run(source.read())
        source.acknowledge(cast("tuple[RawChainObservation, ...]", first))
        second = asyncio.run(source.read())

        first_observation = cast("tuple[RawChainObservation, ...]", first)[0]
        second_observation = cast("tuple[RawChainObservation, ...]", second)[0]
        self.assertEqual(first_observation.receive_sequence, 1)
        self.assertEqual(second_observation.receive_sequence, 2)
        self.assertEqual(first_observation.boot_id, second_observation.boot_id)

    def test_failed_handler_keeps_online_batch_pending(self) -> None:
        """A failed batch is retried before the source cursor advances."""

        transport = _FakeTransport(
            {
                "getSlot": _rpc_response(500),
                "getSignaturesForAddress": [
                    _rpc_response(
                        [
                            {
                                "signature": SIGNATURE,
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
                        "transaction": {"signatures": [SIGNATURE]},
                        "meta": {"err": None},
                    }
                ),
                "getBlock": _rpc_response(
                    {"transactions": [{"transaction": {"signatures": [SIGNATURE]}}]}
                ),
            }
        )
        source = RpcAddressObservationSource(
            address=ADDRESS,
            endpoint="https://rpc.example",
            transport=transport,
            max_signatures=1,
            max_transactions=1,
        )
        first = asyncio.run(source.read())
        first_batch = cast("tuple[RawChainObservation, ...]", first)

        with tempfile.TemporaryDirectory() as directory:
            loop = _loop(
                Path(directory) / "raw.jsonl", Path(directory) / "handled.jsonl"
            )
            failed = asyncio.run(loop.run_once(source, _FailingHandler()))
            retried = asyncio.run(loop.run_once(source, _RecordingHandler()))

        self.assertFalse(failed.accepted)
        self.assertTrue(retried.accepted)
        self.assertEqual(first_batch[0].receive_sequence, 1)
        self.assertEqual(source.cursor.receive_sequence, 1)
        self.assertEqual(len(transport.calls), 4)

    def test_restart_ignores_raw_rows_without_handled_identity(self) -> None:
        """Raw persistence alone cannot advance a restarted online source."""

        with tempfile.TemporaryDirectory() as directory:
            raw_path = Path(directory) / "raw.jsonl"
            handled_path = Path(directory) / "handled.jsonl"
            transport = _FakeTransport(
                {
                    "getSlot": _rpc_response(500),
                    "getSignaturesForAddress": _rpc_response(
                        [
                            {
                                "signature": SIGNATURE,
                                "slot": 500,
                                "confirmationStatus": "finalized",
                            }
                        ]
                    ),
                    "getTransaction": _rpc_response(
                        {
                            "slot": 500,
                            "transaction": {"signatures": [SIGNATURE]},
                            "meta": {"err": None},
                        }
                    ),
                    "getBlock": _rpc_response(
                        {"transactions": [{"transaction": {"signatures": [SIGNATURE]}}]}
                    ),
                }
            )
            setup = RpcAddressObservationSource(
                address=ADDRESS,
                endpoint="https://rpc.example",
                transport=transport,
                max_signatures=1,
                max_transactions=1,
            )
            result = asyncio.run(setup.read())
            observation = cast("tuple[RawChainObservation, ...]", result)[0]
            JsonlObservationStore(raw_path).append(observation)

            restarted = RpcAddressObservationSource(
                address=ADDRESS,
                endpoint="https://rpc.example",
                raw_observation_path=raw_path,
                handled_ledger=JsonlHandledEvidenceLedger(handled_path),
                transport=transport,
                max_signatures=1,
                max_transactions=1,
            )
            self.assertIsNone(restarted.cursor)
            self.assertEqual(asyncio.run(restarted.read())[0].receive_sequence, 1)
            self.assertNotIn("until", transport.calls[-3]["params"][1])

    def test_address_bound_source_id_blocks_ambiguous_restore(self) -> None:
        """Evidence bound to one address cannot restore another address."""

        other_address = base58.b58encode(b"other-address".ljust(32, b"b")).decode()
        with tempfile.TemporaryDirectory() as directory:
            raw_path = Path(directory) / "raw.jsonl"
            handled_path = Path(directory) / "handled.jsonl"
            transport = _FakeTransport(
                {
                    "getSlot": _rpc_response(500),
                    "getSignaturesForAddress": _rpc_response([]),
                }
            )
            first = RpcAddressObservationSource(
                address=ADDRESS,
                endpoint="https://rpc.example",
                raw_observation_path=raw_path,
                handled_ledger=JsonlHandledEvidenceLedger(handled_path),
                transport=transport,
            )
            observation = _observation_with_source(first.source_id)
            JsonlObservationStore(raw_path).append(observation)
            JsonlHandledEvidenceLedger(handled_path).append(
                observation_identity(observation)
            )

            other = RpcAddressObservationSource(
                address=other_address,
                endpoint="https://rpc.example",
                raw_observation_path=raw_path,
                handled_ledger=JsonlHandledEvidenceLedger(handled_path),
                transport=transport,
            )
            self.assertIsNone(other.cursor)
            self.assertEqual(asyncio.run(other.read()), ())
            self.assertNotIn("until", transport.calls[-1]["params"][1])

    def test_duplicate_evidence_is_not_handled_twice(self) -> None:
        """Durable identity deduplication prevents duplicate online decisions."""

        observation = _observation()
        handler = _RecordingHandler()
        with tempfile.TemporaryDirectory() as directory:
            report = asyncio.run(
                _loop(
                    Path(directory) / "raw.jsonl",
                    Path(directory) / "handled.jsonl",
                ).run_once(
                    source=MemoryObservationSource((observation, observation)),
                    handler=handler,
                )
            )

        self.assertEqual(report.persisted_count, 1)
        self.assertEqual(report.duplicate_count, 1)
        self.assertEqual(report.handled_count, 1)
        self.assertEqual(len(handler.observations), 1)

    def test_handled_evidence_survives_loop_restart(self) -> None:
        """A restarted loop does not handle a durable identity again."""

        observation = _observation()
        with tempfile.TemporaryDirectory() as directory:
            raw_path = Path(directory) / "raw.jsonl"
            ledger_path = Path(directory) / "handled.jsonl"
            first_handler = _RecordingHandler()
            first_report = asyncio.run(
                _loop(raw_path, ledger_path).run_once(
                    source=MemoryObservationSource((observation,)),
                    handler=first_handler,
                )
            )
            second_handler = _RecordingHandler()
            second_report = asyncio.run(
                _loop(raw_path, ledger_path).run_once(
                    source=MemoryObservationSource((observation,)),
                    handler=second_handler,
                )
            )

        self.assertTrue(first_report.accepted)
        self.assertEqual(first_report.handled_count, 1)
        self.assertEqual(second_report.persisted_count, 0)
        self.assertEqual(second_report.duplicate_count, 1)
        self.assertEqual(second_report.handled_count, 0)
        self.assertEqual(second_handler.observations, [])

    def test_source_abstention_does_not_reach_handler(self) -> None:
        """Unknown online state remains an abstention with no fallback path."""

        handler = _RecordingHandler()
        result = AbstainResult(
            reason=AbstainReason.UNKNOWN_PROTOCOL_STATE,
            message="incomplete finalized evidence",
            as_of_slot=500,
        )
        report = asyncio.run(
            _loop(Path("raw.jsonl"), Path("handled.jsonl")).run_once(
                source=_AbstainingSource(result),
                handler=handler,
            )
        )

        self.assertFalse(report.accepted)
        self.assertEqual(report.abstention, result)
        self.assertEqual(handler.observations, [])

    def test_malformed_handled_ledger_abstains_before_handler(self) -> None:
        """Malformed restart state cannot silently trigger downstream handling."""

        handler = _RecordingHandler()
        with tempfile.TemporaryDirectory() as directory:
            ledger_path = Path(directory) / "handled.jsonl"
            ledger_path.write_text("not-json\n", encoding="utf-8")
            report = asyncio.run(
                _loop(Path(directory) / "raw.jsonl", ledger_path).run_once(
                    source=MemoryObservationSource((_observation(),)),
                    handler=handler,
                )
            )

        self.assertFalse(report.accepted)
        self.assertEqual(report.persisted_count, 0)
        self.assertEqual(report.handled_count, 0)
        self.assertEqual(handler.observations, [])


class _RecordingHandler:
    def __init__(self) -> None:
        self.observations: list[RawChainObservation] = []

    async def handle(self, observation: RawChainObservation) -> None:
        self.observations.append(observation)


class _FailingHandler:
    async def handle(self, _observation: RawChainObservation) -> None:
        raise RuntimeError


class _AbstainingSource:
    def __init__(self, result: AbstainResult) -> None:
        self._result = result

    async def read(self) -> AbstainResult:
        return self._result


class _NoopCheckpointWriter:
    def save(self, _checkpoint: SourceCheckpoint) -> None:
        return None


class _FakeTransport:
    def __init__(self, responses: Mapping[str, bytes | list[bytes]]) -> None:
        self._responses = dict(responses)
        self.calls: list[dict[str, object]] = []

    async def __call__(self, _endpoint: str, body: bytes) -> RpcHttpResponse:
        request = cast("dict[str, object]", json.loads(body))
        self.calls.append(request)
        method = cast("str", request["method"])
        response = self._responses[method]
        if isinstance(response, list):
            if not response:
                raise AssertionError
            response = response.pop(0)
        return RpcHttpResponse(status=200, body=response)


def _ingestor(path: Path) -> DurableObservationIngestor:
    return DurableObservationIngestor(
        observation_store=JsonlObservationStore(path),
        checkpoint_writer=_NoopCheckpointWriter(),
    )


def _loop(raw_path: Path, ledger_path: Path) -> SharedObservationLoop:
    return SharedObservationLoop(
        _ingestor(raw_path),
        JsonlHandledEvidenceLedger(ledger_path),
    )


def _observation() -> RawChainObservation:
    return RawChainObservation(
        raw_id=UUID("00000000-0000-0000-0000-000000000020"),
        source_id="test-source",
        observer_id="test-observer",
        boot_id=UUID("00000000-0000-0000-0000-000000000001"),
        receive_sequence=1,
        slot=100,
        parent_slot=99,
        blockhash=None,
        signature=None,
        transaction_index=None,
        outer_instruction_index=None,
        inner_instruction_group_index=None,
        inner_instruction_index=None,
        stack_height=None,
        event_ordinal=1,
        commitment="finalized",
        canonical_status="canonical",
        received_wall_ns=1,
        received_monotonic_ns=1,
        program_id=None,
        account_pubkey=None,
        account_owner_program_id=None,
        raw_transaction=None,
        raw_transaction_format=None,
        raw_account_data=None,
        account_write_version=None,
        source_update_kind="slot",
        raw_source_status=None,
        raw_source_payload=b"slot",
        decoder_name=None,
        decoder_version=None,
        idl_hash=None,
    )


def _observation_with_source(source_id: str) -> RawChainObservation:
    """Build one address-bound transaction observation for cursor fixtures."""

    signature = b"signature".ljust(64, b"x")
    return replace(
        _observation(),
        source_id=source_id,
        signature=signature,
        raw_transaction=b"transaction",
        raw_transaction_format="json",
        source_update_kind="transaction",
        raw_source_payload=b"transaction",
    )


def _rpc_response(result: object) -> bytes:
    return json.dumps(
        {"jsonrpc": "2.0", "id": 1, "result": result},
        separators=(",", ":"),
    ).encode()


if __name__ == "__main__":
    unittest.main()
