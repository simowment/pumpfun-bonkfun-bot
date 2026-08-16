"""Golden and fail-closed tests for Pump create reserve reconstruction."""

import base64
import hashlib
import json
import unittest
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any, cast
from uuid import UUID

import base58
from solders.transaction import VersionedTransaction

from rugbot.domain.decisions import AbstainReason, AbstainResult
from rugbot.domain.observations import RawChainObservation
from rugbot.ingest.pump_create_observation import (
    decode_pump_create_market_state_observation,
)
from rugbot.ingest.rpc_observer import JSON_TRANSACTION_FORMAT
from rugbot.market_state.pump_create import PumpCreateMarketState
from rugbot.protocol.pump.create_event_decoder import (
    CREATE_EVENT_DISCRIMINATOR,
    PumpCreateEvent,
    decode_pump_create_event_logs,
)

FIXTURE = Path(
    "fixtures/finalized_transactions/pump_create_v2/"
    "4HbY43S9UigSctrfxY5nszgf3ozN1f4kPQYaqaFLZaCDhwa55rauuRmhP85u67U7dBvGFwB5C6stmkH2b1TNxgQh.json"
)


class PumpCreateReserveTests(unittest.TestCase):
    """Reconstruct only reserves proven by both external and CPI evidence."""

    def test_reconstructs_golden_initial_reserves_at_create_point(self) -> None:
        """The finalized golden create event yields exact integer reserves."""

        artifact = _artifact()
        result = decode_pump_create_market_state_observation(
            _observation_with_logs(artifact)
        )

        self.assertIsInstance(result, PumpCreateMarketState)
        result = cast("PumpCreateMarketState", result)
        reserves = result.reserves
        self.assertEqual(reserves.as_of_slot, artifact["as_of_slot"])
        self.assertEqual(
            reserves.mint_pubkey, artifact["create_v2"]["account_pubkeys"][0]
        )
        self.assertEqual(reserves.virtual_token_reserves, 1_073_000_000_000_000)
        self.assertEqual(reserves.virtual_quote_reserves, 30_000_000_000)
        self.assertEqual(reserves.real_token_reserves, 793_100_000_000_000)
        self.assertEqual(reserves.real_quote_reserves, 0)
        self.assertEqual(reserves.token_total_supply, 1_000_000_000_000_000)
        self.assertFalse(reserves.complete)
        self.assertEqual(reserves.transaction_index, 0)
        self.assertEqual(reserves.outer_instruction_index, 2)
        self.assertEqual(
            reserves.event_log_index,
            next(
                index
                for index, value in enumerate(
                    artifact["base64_transaction_response"]["meta"]["logMessages"]
                )
                if value.startswith("Program data: G3")
            ),
        )
        self.assertEqual(
            reserves.source_signature,
            result.launch.signature,
        )
        self.assertEqual(
            result.create_event.raw_data_sha256,
            hashlib.sha256(result.create_event.raw_data).hexdigest(),
        )

    def test_missing_cpi_event_abstains(self) -> None:
        """External create identity without CPI reserves cannot produce state."""

        artifact = _artifact()
        result = decode_pump_create_market_state_observation(
            _observation_with_logs(
                artifact,
                log_mutator=lambda logs: [
                    value for value in logs if not value.startswith("Program data: G3")
                ],
            )
        )

        self.assert_abstains(result, AbstainReason.MISSING_FEATURE)

    def test_event_without_create_invocation_context_abstains(self) -> None:
        """A matching event without its external invocation is not sufficient."""

        artifact = _artifact()
        result = decode_pump_create_market_state_observation(
            _observation_with_logs(
                artifact,
                log_mutator=lambda logs: [
                    value
                    for value in logs
                    if value != "Program log: Instruction: CreateV2"
                ],
            )
        )

        self.assert_abstains(result, AbstainReason.UNKNOWN_PROTOCOL_STATE)

    def test_truncated_create_event_abstains(self) -> None:
        """A create discriminator with an incomplete payload is not decoded."""

        artifact = _artifact()

        def truncate(logs: list[str]) -> list[str]:
            mutated = list(logs)
            event_index = next(
                index
                for index, value in enumerate(mutated)
                if value.startswith("Program data: G3")
            )
            mutated[event_index] = "Program data: " + base64.b64encode(
                CREATE_EVENT_DISCRIMINATOR + b"\x00"
            ).decode("ascii")
            return mutated

        result = decode_pump_create_market_state_observation(
            _observation_with_logs(artifact, log_mutator=truncate)
        )

        self.assert_abstains(result, AbstainReason.UNSUPPORTED_PROTOCOL_STATE)

    def test_cpi_mint_mismatch_abstains(self) -> None:
        """A valid event for another mint cannot be joined to the create."""

        artifact = _artifact()

        def change_mint(logs: list[str]) -> list[str]:
            mutated = list(logs)
            event_index = next(
                index
                for index, value in enumerate(mutated)
                if value.startswith("Program data: G3")
            )
            encoded = mutated[event_index].split(":", 1)[1].strip()
            raw_data = bytearray(base64.b64decode(encoded, validate=True))
            offset = _first_pubkey_offset(raw_data)
            raw_data[offset] ^= 1
            mutated[event_index] = "Program data: " + base64.b64encode(raw_data).decode(
                "ascii"
            )
            return mutated

        result = decode_pump_create_market_state_observation(
            _observation_with_logs(artifact, log_mutator=change_mint)
        )

        self.assert_abstains(result, AbstainReason.UNSUPPORTED_PROTOCOL_STATE)

    def test_event_string_lengths_are_variable(self) -> None:
        """The pinned fields are exact while Anchor strings remain variable."""

        artifact = _artifact()
        event_log = next(
            value
            for value in artifact["base64_transaction_response"]["meta"]["logMessages"]
            if value.startswith("Program data: G3")
        )
        raw_data = bytearray(
            base64.b64decode(event_log.split(":", 1)[1].strip(), validate=True)
        )
        uri_length_offset = _uri_length_offset(raw_data)
        uri_start = uri_length_offset + 4
        original_uri_length = int.from_bytes(
            raw_data[uri_length_offset:uri_start], "little"
        )
        shorter_data = (
            bytes(raw_data[:uri_length_offset])
            + (1).to_bytes(4, "little")
            + b"u"
            + bytes(raw_data[uri_start + original_uri_length :])
        )

        result = decode_pump_create_event_logs(
            ["Program data: " + base64.b64encode(shorter_data).decode("ascii")],
            as_of_slot=artifact["as_of_slot"],
        )

        self.assertIsInstance(result, PumpCreateEvent)
        result = cast("PumpCreateEvent", result)
        self.assertEqual(result.uri, "u")

    def assert_abstains(self, result: object, reason: AbstainReason) -> None:
        self.assertIsInstance(result, AbstainResult)
        if isinstance(result, AbstainResult):
            self.assertEqual(result.reason, reason)
            self.assertEqual(result.as_of_slot, _artifact()["as_of_slot"])


def _observation_with_logs(
    artifact: dict[str, Any],
    *,
    log_mutator: Callable[[list[str]], list[str]] | None = None,
) -> RawChainObservation:
    observation = _observation(artifact)
    envelope = json.loads(observation.raw_source_payload)
    logs = list(artifact["base64_transaction_response"]["meta"]["logMessages"])
    if log_mutator is not None:
        logs = log_mutator(logs)
    envelope["result"]["meta"]["logMessages"] = logs
    return replace(
        observation,
        raw_source_payload=json.dumps(envelope, separators=(",", ":")).encode("utf-8"),
    )


def _artifact() -> dict[str, Any]:
    return cast("dict[str, Any]", json.loads(FIXTURE.read_text(encoding="utf-8")))


def _observation(artifact: dict[str, Any]) -> RawChainObservation:
    base64_response = artifact["base64_transaction_response"]
    transaction = VersionedTransaction.from_bytes(
        base64.b64decode(base64_response["transaction"][0])
    )
    message = {
        "accountKeys": [str(pubkey) for pubkey in transaction.message.account_keys],
        "instructions": [
            {
                "programIdIndex": instruction.program_id_index,
                "accounts": list(instruction.accounts),
                "data": base58.b58encode(instruction.data).decode("ascii"),
            }
            for instruction in transaction.message.instructions
        ],
    }
    envelope = {
        "jsonrpc": "2.0",
        "id": 1,
        "result": {
            "slot": artifact["as_of_slot"],
            "meta": {
                "err": None,
                "loadedAddresses": base64_response["meta"]["loadedAddresses"],
            },
            "transaction": {
                "signatures": [artifact["signature"]],
                "message": message,
            },
        },
    }
    payload = json.dumps(envelope, separators=(",", ":")).encode("utf-8")
    return RawChainObservation(
        raw_id=UUID("00000000-0000-0000-0000-000000000001"),
        source_id="test-rpc",
        observer_id="test-observer",
        boot_id=UUID("00000000-0000-0000-0000-000000000002"),
        receive_sequence=1,
        slot=artifact["as_of_slot"],
        parent_slot=None,
        blockhash=None,
        signature=base58.b58decode(artifact["signature"]),
        transaction_index=0,
        outer_instruction_index=None,
        inner_instruction_group_index=None,
        inner_instruction_index=None,
        stack_height=None,
        event_ordinal=None,
        commitment="finalized",
        canonical_status="canonical",
        received_wall_ns=1,
        received_monotonic_ns=1,
        program_id=None,
        account_pubkey=None,
        account_owner_program_id=None,
        raw_transaction=payload,
        raw_transaction_format=JSON_TRANSACTION_FORMAT,
        raw_account_data=None,
        account_write_version=None,
        source_update_kind="transaction",
        raw_source_status=None,
        raw_source_payload=payload,
        decoder_name=None,
        decoder_version=None,
        idl_hash=None,
    )


def _first_pubkey_offset(raw_data: bytes) -> int:
    offset = len(CREATE_EVENT_DISCRIMINATOR)
    for _ in range(3):
        string_length = int.from_bytes(raw_data[offset : offset + 4], "little")
        offset += 4 + string_length
    return offset


def _uri_length_offset(raw_data: bytes) -> int:
    offset = len(CREATE_EVENT_DISCRIMINATOR)
    for _ in range(2):
        string_length = int.from_bytes(raw_data[offset : offset + 4], "little")
        offset += 4 + string_length
    return offset


if __name__ == "__main__":
    unittest.main()
