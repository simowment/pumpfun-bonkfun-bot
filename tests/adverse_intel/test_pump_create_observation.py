"""Golden tests for finalized RPC Pump create observation decoding."""

import base64
import json
import unittest
from dataclasses import replace
from pathlib import Path
from typing import cast
from uuid import UUID

import base58
from solders.transaction import VersionedTransaction

from rugbot.domain.decisions import AbstainResult
from rugbot.domain.launches import LaunchCreatedV2
from rugbot.domain.observations import RawChainObservation
from rugbot.ingest.pump_create_observation import (
    decode_pump_create_mint_metadata_observation,
    decode_pump_create_v2_observation,
)
from rugbot.ingest.rpc_observer import JSON_TRANSACTION_FORMAT
from rugbot.protocol.pump.create_decoder import SPL_2022_PROGRAM_ID

FIXTURE = Path(
    "fixtures/finalized_transactions/pump_create_v2/"
    "4HbY43S9UigSctrfxY5nszgf3ozN1f4kPQYaqaFLZaCDhwa55rauuRmhP85u67U7dBvGFwB5C6stmkH2b1TNxgQh.json"
)


class PumpCreateObservationTests(unittest.TestCase):
    """Decode only pinned create_v2 instructions from immutable RPC bytes."""

    def test_decodes_golden_finalized_rpc_transaction(self) -> None:
        artifact = _artifact()
        result = decode_pump_create_v2_observation(_observation(artifact))

        self.assertIsInstance(result, LaunchCreatedV2)
        result = cast("LaunchCreatedV2", result)
        self.assertEqual(result.as_of_slot, artifact["as_of_slot"])
        self.assertEqual(result.signature, base58.b58decode(artifact["signature"]))
        self.assertEqual(
            result.mint_pubkey, artifact["create_v2"]["account_pubkeys"][0]
        )
        self.assertEqual(
            result.creator_pubkey,
            artifact["json_parsed_transaction_response"]["transaction"]["message"][
                "accountKeys"
            ][0]["pubkey"],
        )
        self.assertEqual(result.transaction_index, 0)
        self.assertEqual(result.outer_instruction_index, 2)
        self.assertEqual(
            result.missing_evidence,
            ("first_buyer", "transaction_slot_account_state"),
        )

    def test_valid_non_create_transaction_is_ignored(self) -> None:
        artifact = _artifact()
        observation = _observation(artifact)
        envelope = json.loads(observation.raw_source_payload)
        envelope["result"]["transaction"]["message"]["instructions"] = []

        result = decode_pump_create_v2_observation(
            replace(
                observation,
                raw_source_payload=json.dumps(envelope).encode("utf-8"),
            )
        )

        self.assertIsNone(result)

    def test_non_finalized_or_malformed_evidence_abstains(self) -> None:
        observation = _observation(_artifact())

        self.assertIsInstance(
            decode_pump_create_v2_observation(
                replace(observation, commitment="confirmed")
            ),
            AbstainResult,
        )

    def test_decodes_mint_decimals_from_initialize_mint2(self) -> None:
        artifact = _artifact()
        observation = _observation(artifact)
        envelope = json.loads(observation.raw_source_payload)
        message = envelope["result"]["transaction"]["message"]
        meta = envelope["result"]["meta"]
        mint = artifact["create_v2"]["account_pubkeys"][0]
        mint_index = message["accountKeys"].index(mint)
        token_program_index = 15 + 6 + 3
        token_data = base58.b58encode(
            bytes([20, 6]) + bytes(range(32)) + bytes([0])
        ).decode("ascii")
        meta["innerInstructions"] = [
            {
                "index": 2,
                "instructions": [
                    {
                        "programIdIndex": token_program_index,
                        "accounts": [mint_index],
                        "data": token_data,
                    }
                ],
            }
        ]
        enriched = replace(
            observation,
            raw_source_payload=json.dumps(envelope).encode("utf-8"),
        )

        result = decode_pump_create_mint_metadata_observation(
            enriched,
            mint_pubkey=mint,
        )

        self.assertEqual(result.decimals, 6)
        self.assertEqual(result.as_of_slot, artifact["as_of_slot"])
        self.assertEqual(result.owner_program_id, SPL_2022_PROGRAM_ID)

    def test_mint_decimals_require_unique_matching_initialize_mint2(self) -> None:
        artifact = _artifact()
        observation = _observation(artifact)
        envelope = json.loads(observation.raw_source_payload)
        message = envelope["result"]["transaction"]["message"]
        meta = envelope["result"]["meta"]
        mint = artifact["create_v2"]["account_pubkeys"][0]
        mint_index = message["accountKeys"].index(mint)
        token_program_index = 15 + 6 + 3
        token_data = base58.b58encode(
            bytes([20, 6]) + bytes(range(32)) + bytes([0])
        ).decode("ascii")
        inner = {
            "programIdIndex": token_program_index,
            "accounts": [mint_index],
            "data": token_data,
        }
        meta["innerInstructions"] = [
            {"index": 2, "instructions": [inner, inner.copy()]}
        ]

        result = decode_pump_create_mint_metadata_observation(
            replace(
                observation,
                raw_source_payload=json.dumps(envelope).encode("utf-8"),
            ),
            mint_pubkey=mint,
        )

        self.assertIsInstance(result, AbstainResult)
        self.assertIsInstance(
            decode_pump_create_v2_observation(
                replace(observation, raw_source_payload=b'{"jsonrpc":"2.0",')
            ),
            AbstainResult,
        )


def _artifact() -> dict[str, object]:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def _observation(artifact: dict[str, object]) -> RawChainObservation:
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


if __name__ == "__main__":
    unittest.main()
