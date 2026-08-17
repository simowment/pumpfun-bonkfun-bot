"""Focused tests for finalized Pump migration observation decoding."""

import json
import unittest
from uuid import uuid4

import base58

from rugbot.domain.decisions import AbstainReason, AbstainResult
from rugbot.domain.migrations import PumpMigrationInstructionEvidence
from rugbot.domain.observations import RawChainObservation
from rugbot.ingest.pump_migration_observation import (
    decode_pump_migration_observation,
)
from rugbot.ingest.rpc_observer import JSON_TRANSACTION_FORMAT
from rugbot.protocol.pump.migration import (
    ASSOCIATED_SPL_PROGRAM_ID,
    MIGRATE_DISCRIMINATOR,
    PUMP_AMM_PROGRAM_ID,
    PUMP_PROGRAM_ID,
    SPL_2022_PROGRAM_ID,
    SPL_PROGRAM_ID,
    SYSTEM_PROGRAM_ID,
    WSOL_MINT_ID,
)

SLOT = 456
SIGNATURE = bytes(range(64))


class PumpMigrationObservationTests(unittest.TestCase):
    """Tests for the finalized raw transaction migration adapter."""

    def test_decodes_exact_migration_and_preserves_raw_payload(self) -> None:
        """The adapter proves accounts from the original static/loaded table."""

        payload = _transaction_payload()
        observation = _observation(payload)

        result = decode_pump_migration_observation(observation)

        self.assertIsInstance(result, PumpMigrationInstructionEvidence)
        if isinstance(result, AbstainResult):
            self.fail(result.message)
        self.assertEqual(result.as_of_slot, SLOT)
        self.assertEqual(result.transaction_index, 7)
        self.assertEqual(result.outer_instruction_index, 0)
        self.assertEqual(result.signature, SIGNATURE)
        self.assertEqual(result.account_indices, tuple(range(24)))
        self.assertEqual(result.program_id_index, 24)
        self.assertEqual(result.base_mint_pubkey, "migration-mint")
        self.assertEqual(result.pool_pubkey, "migration-pool")
        self.assertEqual(result.quote_mint_pubkey, WSOL_MINT_ID)
        self.assertEqual(observation.raw_source_payload, payload)
        self.assertEqual(result.missing_evidence[0], "canonical_pool_artifact")

    def test_no_migration_returns_none(self) -> None:
        """A valid transaction without migration is not a positive event."""

        payload = _transaction_payload(data=b"other-pump-instruction")

        self.assertIsNone(decode_pump_migration_observation(_observation(payload)))

    def test_multiple_migrations_abstain(self) -> None:
        """Ambiguous positive evidence never selects one instruction implicitly."""

        payload = _transaction_payload(
            instructions=[
                _instruction_json(),
                _instruction_json(outer_accounts=list(range(24))),
            ]
        )

        result = decode_pump_migration_observation(_observation(payload))

        self.assert_abstains(result, AbstainReason.UNSUPPORTED_PROTOCOL_STATE)

    def test_duplicate_json_keys_abstain(self) -> None:
        """Raw JSON with duplicate fields is not safely re-decodable."""

        payload = b'{"jsonrpc":"2.0","jsonrpc":"2.0","result":null}'

        result = decode_pump_migration_observation(_observation(payload))

        self.assert_abstains(result, AbstainReason.UNKNOWN_PROTOCOL_STATE)

    def test_wrong_slot_or_signature_abstains(self) -> None:
        """Transaction identity must agree with the immutable observation."""

        payload = _transaction_payload()
        wrong_slot = _observation(payload, slot=SLOT + 1)
        wrong_signature = _observation(
            payload,
            signature=b"x" * len(SIGNATURE),
        )

        slot_result = decode_pump_migration_observation(wrong_slot)
        signature_result = decode_pump_migration_observation(wrong_signature)

        self.assert_abstains(
            slot_result, AbstainReason.STALE_STATE, as_of_slot=SLOT + 1
        )
        self.assert_abstains(
            signature_result,
            AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
        )

    def test_extra_migration_data_abstains(self) -> None:
        """A discriminator prefix is insufficient for the exact IDL layout."""

        payload = _transaction_payload(data=MIGRATE_DISCRIMINATOR + b"extra")

        result = decode_pump_migration_observation(_observation(payload))

        self.assert_abstains(result, AbstainReason.UNSUPPORTED_PROTOCOL_STATE)

    def assert_abstains(
        self,
        result: object,
        reason: AbstainReason,
        *,
        as_of_slot: int = SLOT,
    ) -> None:
        self.assertIsInstance(result, AbstainResult)
        if isinstance(result, AbstainResult):
            self.assertEqual(result.reason, reason)
            self.assertEqual(result.as_of_slot, as_of_slot)


def _observation(
    payload: bytes,
    *,
    slot: int = SLOT,
    signature: bytes = SIGNATURE,
) -> RawChainObservation:
    return RawChainObservation(
        raw_id=uuid4(),
        source_id="test-rpc",
        observer_id="test-observer",
        boot_id=uuid4(),
        receive_sequence=1,
        slot=slot,
        parent_slot=None,
        blockhash=None,
        signature=signature,
        transaction_index=7,
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


def _transaction_payload(
    *,
    data: bytes = MIGRATE_DISCRIMINATOR,
    instructions: list[dict[str, object]] | None = None,
) -> bytes:
    account_pubkeys = _account_pubkeys()
    message_instructions = instructions or [_instruction_json(data=data)]
    envelope = {
        "jsonrpc": "2.0",
        "id": 1,
        "result": {
            "slot": SLOT,
            "meta": {
                "err": None,
                "loadedAddresses": {
                    "writable": [account_pubkeys[24]],
                    "readonly": [account_pubkeys[25]],
                },
            },
            "transaction": {
                "signatures": [base58.b58encode(SIGNATURE).decode("ascii")],
                "message": {
                    "accountKeys": account_pubkeys[:-2],
                    "instructions": message_instructions,
                },
            },
        },
    }
    return json.dumps(envelope, separators=(",", ":")).encode("utf-8")


def _instruction_json(
    *,
    data: bytes = MIGRATE_DISCRIMINATOR,
    outer_accounts: list[int] | None = None,
) -> dict[str, object]:
    return {
        "programIdIndex": 24,
        "accounts": list(range(24)) if outer_accounts is None else outer_accounts,
        "data": base58.b58encode(data).decode("ascii"),
    }


def _account_pubkeys() -> list[str]:
    account_pubkeys = [f"migration-account-{index}" for index in range(26)]
    account_pubkeys[2] = "migration-mint"
    account_pubkeys[9] = "migration-pool"
    account_pubkeys[10] = "migration-pool-authority"
    account_pubkeys[6] = SYSTEM_PROGRAM_ID
    account_pubkeys[7] = SPL_PROGRAM_ID
    account_pubkeys[8] = PUMP_AMM_PROGRAM_ID
    account_pubkeys[14] = WSOL_MINT_ID
    account_pubkeys[19] = SPL_2022_PROGRAM_ID
    account_pubkeys[20] = ASSOCIATED_SPL_PROGRAM_ID
    account_pubkeys[23] = PUMP_PROGRAM_ID
    account_pubkeys[24] = PUMP_PROGRAM_ID
    return account_pubkeys


if __name__ == "__main__":
    unittest.main()
