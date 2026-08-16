"""Pump create_v2 fixture hydration tests."""

import ast
import base64
import json
import struct
import unittest
from pathlib import Path
from typing import cast

import base58
from solders.hash import Hash
from solders.instruction import CompiledInstruction
from solders.message import Message
from solders.pubkey import Pubkey
from solders.signature import Signature
from solders.transaction import VersionedTransaction

from rugbot.domain.decisions import AbstainReason, AbstainResult
from rugbot.domain.launches import LaunchCreatedV2
from rugbot.ingest.pump_create_fixture_decode import (
    decode_pump_create_v2_fixture_artifact,
)
from rugbot.protocol.pump.create_decoder import (
    ASSOCIATED_SPL_PROGRAM_ID,
    CREATE_V2_ACCOUNT_NAMES,
    CREATE_V2_DISCRIMINATOR,
    MAYHEM_PROGRAM_ID,
    PINNED_PUMP_IDL_SHA256,
    PUMP_CREATE_V2_DECODER_VERSION,
    PUMP_PROGRAM_ID,
    SPL_2022_PROGRAM_ID,
    SYSTEM_PROGRAM_ID,
    WSOL_MINT_ID,
)

FIXTURE_MODULE = Path("src/rugbot/ingest/pump_create_fixture_decode.py")
GOLDEN_SIGNATURE = "4HbY43S9UigSctrfxY5nszgf3ozN1f4kPQYaqaFLZaCDhwa55rauuRmhP85u67U7dBvGFwB5C6stmkH2b1TNxgQh"
GOLDEN_CREATE_V2_FIXTURE = Path(
    f"fixtures/finalized_transactions/pump_create_v2/{GOLDEN_SIGNATURE}.json"
)
AS_OF_SLOT = 901
SIGNATURE = base58.b58encode(bytes([7]) * 64).decode("ascii")
OTHER_SIGNATURE = base58.b58encode(bytes([8]) * 64).decode("ascii")
CREATOR_BYTES = bytes(range(32))
FORBIDDEN_IMPORT_PREFIXES = (
    "src.core",
    "src.trading",
    "src.platforms",
    "dotenv",
)


class PumpCreateFixtureDecodeTests(unittest.TestCase):
    """Tests for harvested create_v2 fixture hydration."""

    def test_decodes_golden_finalized_create_v2_fixture(self) -> None:
        """The harvested finalized create_v2 fixture decodes exactly."""

        artifact = json.loads(GOLDEN_CREATE_V2_FIXTURE.read_text(encoding="utf-8"))

        result = decode_pump_create_v2_fixture_artifact(artifact)

        self.assertIsInstance(result, LaunchCreatedV2)
        if isinstance(result, AbstainResult):
            self.fail(result.message)
        self.assertEqual(result.as_of_slot, 430584458)
        self.assertEqual(result.signature, bytes(base58.b58decode(GOLDEN_SIGNATURE)))
        self.assertEqual(result.decoder_version, PUMP_CREATE_V2_DECODER_VERSION)
        self.assertEqual(result.idl_hash, PINNED_PUMP_IDL_SHA256)
        self.assertEqual(result.program_id, PUMP_PROGRAM_ID)
        self.assertEqual(result.program_id_index, 11)
        self.assertEqual(result.outer_instruction_index, 2)
        self.assertIsNone(result.transaction_index)
        self.assertEqual(result.account_indices, _golden_account_indices())
        self.assertEqual(result.account_pubkeys, _golden_account_pubkeys())
        self.assertEqual(result.account_role_proofs, _golden_account_role_proofs())
        self.assertEqual(result.required_account_names, CREATE_V2_ACCOUNT_NAMES)
        self.assertEqual(
            result.actor_role_proofs,
            (
                (
                    "fee_payer",
                    0,
                    "CvoPbuS2AghzVBYJx7HfQGhALiqif4YwWgHvXmhehuJZ",
                    (
                        f"fixture:{GOLDEN_SIGNATURE}:jsonParsed.message.accountKeys[0]",
                        f"fixture:{GOLDEN_SIGNATURE}:base64.message.account_keys[0]",
                    ),
                    "solana-message-fee-payer-v1",
                ),
            ),
        )
        self.assertEqual(
            result.launch_id,
            "GGYkiUJGopoH9DZUjGFtLiKpXXW5AQovqZ7eVcjEpump",
        )
        self.assertEqual(result.name, "NUTCOIN")
        self.assertEqual(result.symbol, "NUT")
        self.assertEqual(
            result.uri,
            "https://metadata.j7tracker.io/metadata/585fa92b23d94433.json",
        )
        self.assertFalse(result.is_mayhem_mode)
        self.assertTrue(result.is_cashback_enabled)
        self.assertEqual(
            result.user_pubkey,
            "CvoPbuS2AghzVBYJx7HfQGhALiqif4YwWgHvXmhehuJZ",
        )
        self.assertEqual(
            result.creator_pubkey,
            "CvoPbuS2AghzVBYJx7HfQGhALiqif4YwWgHvXmhehuJZ",
        )
        self.assertEqual(
            result.fee_payer_pubkey,
            "CvoPbuS2AghzVBYJx7HfQGhALiqif4YwWgHvXmhehuJZ",
        )
        self.assertIsNone(result.first_buyer_pubkey)
        self.assertEqual(
            result.missing_evidence,
            ("first_buyer", "transaction_slot_account_state"),
        )
        self.assertEqual(result.mint_account_index, 1)
        self.assertEqual(result.mint_authority_account_index, 23)
        self.assertEqual(result.bonding_curve_account_index, 8)
        self.assertEqual(result.associated_bonding_curve_account_index, 7)
        self.assertEqual(result.global_account_index, 27)
        self.assertEqual(result.user_account_index, 0)
        self.assertEqual(result.system_program_account_index, 10)
        self.assertEqual(result.token_program_account_index, 24)
        self.assertEqual(result.associated_token_program_account_index, 14)
        self.assertEqual(result.mayhem_program_account_index, 15)
        self.assertEqual(result.global_params_account_index, 21)
        self.assertEqual(result.quote_vault_account_index, 18)
        self.assertEqual(result.mayhem_state_account_index, 5)
        self.assertEqual(result.mayhem_token_vault_account_index, 9)
        self.assertEqual(result.event_authority_account_index, 29)
        self.assertEqual(result.base_token_program_pubkey, SPL_2022_PROGRAM_ID)
        self.assertEqual(result.quote_asset, "SOL")
        self.assertEqual(result.quote_mint_pubkey, WSOL_MINT_ID)
        self.assertEqual(result.quote_token_program_pubkey, SYSTEM_PROGRAM_ID)
        self.assertFalse(result.transaction_slot_account_state_available)

    def test_decodes_fixture_artifact_with_fee_payer_proof(self) -> None:
        """A finalized fixture artifact can drive the pinned launch decoder."""

        result = decode_pump_create_v2_fixture_artifact(_artifact())

        self.assertIsInstance(result, LaunchCreatedV2)
        if isinstance(result, AbstainResult):
            self.fail(result.message)
        self.assertEqual(result.as_of_slot, AS_OF_SLOT)
        self.assertEqual(result.instruction_name, "create_v2")
        self.assertEqual(result.fee_payer_pubkey, _fee_payer())
        self.assertEqual(result.fee_payer_account_index, 0)
        self.assertIsNone(result.first_buyer_pubkey)
        self.assertEqual(
            result.missing_evidence,
            ("first_buyer", "transaction_slot_account_state"),
        )
        self.assertEqual(result.actor_role_proofs[0][0], "fee_payer")
        self.assertEqual(
            result.actor_role_proofs[0][3],
            (
                f"fixture:{SIGNATURE}:jsonParsed.message.accountKeys[0]",
                f"fixture:{SIGNATURE}:base64.message.account_keys[0]",
            ),
        )
        self.assertEqual(
            result.actor_role_proofs[0][4],
            "solana-message-fee-payer-v1",
        )
        self.assertEqual(result.program_id_index, 16)
        self.assertEqual(result.mint_pubkey, _account_pubkeys()[0])
        self.assertEqual(result.user_pubkey, _account_pubkeys()[5])

    def test_wrong_idl_hash_abstains_before_decoding(self) -> None:
        """Fixture IDL provenance must match the pinned decoder."""

        artifact = _artifact({"pump_idl_sha256": "wrong"})

        result = decode_pump_create_v2_fixture_artifact(artifact)

        self.assert_abstains(result, AbstainReason.DECODER_MISMATCH)

    def test_instruction_pubkey_mismatch_abstains(self) -> None:
        """Instruction-local pubkeys must agree with compiled account indices."""

        artifact = _artifact()
        create_v2 = dict(cast("dict[str, object]", artifact["create_v2"]))
        account_pubkeys = list(cast("list[str]", create_v2["account_pubkeys"]))
        account_pubkeys[5] = _pubkey(210)
        create_v2["account_pubkeys"] = account_pubkeys
        artifact["create_v2"] = create_v2

        result = decode_pump_create_v2_fixture_artifact(artifact)

        self.assert_abstains(result, AbstainReason.UNSUPPORTED_PROTOCOL_STATE)

    def test_compiled_indices_mismatch_abstains(self) -> None:
        """Stored compiled indices must be re-decodable from transaction bytes."""

        artifact = _artifact()
        create_v2 = dict(cast("dict[str, object]", artifact["create_v2"]))
        create_v2["account_indices"] = list(range(16))
        artifact["create_v2"] = create_v2

        result = decode_pump_create_v2_fixture_artifact(artifact)

        self.assert_abstains(result, AbstainReason.UNSUPPORTED_PROTOCOL_STATE)

    def test_data_mismatch_abstains(self) -> None:
        """Stored instruction data must match the raw transaction bytes."""

        artifact = _artifact()
        create_v2 = dict(cast("dict[str, object]", artifact["create_v2"]))
        create_v2["data_base58"] = base58.b58encode(b"wrong-data").decode("ascii")
        artifact["create_v2"] = create_v2

        result = decode_pump_create_v2_fixture_artifact(artifact)

        self.assert_abstains(result, AbstainReason.UNSUPPORTED_PROTOCOL_STATE)

    def test_failed_transaction_meta_abstains(self) -> None:
        """Failed finalized transactions cannot hydrate launch evidence."""

        artifact = _artifact()
        json_response = dict(
            cast("dict[str, object]", artifact["json_parsed_transaction_response"])
        )
        json_response["meta"] = {"err": {"InstructionError": [0, "Custom"]}}
        artifact["json_parsed_transaction_response"] = json_response
        base64_response = dict(
            cast("dict[str, object]", artifact["base64_transaction_response"])
        )
        base64_response["meta"] = {"err": {"InstructionError": [0, "Custom"]}}
        artifact["base64_transaction_response"] = base64_response

        result = decode_pump_create_v2_fixture_artifact(artifact)

        self.assert_abstains(result, AbstainReason.UNSUPPORTED_PROTOCOL_STATE)

    def test_missing_meta_err_abstains(self) -> None:
        """Successful transaction evidence requires an explicit err field."""

        artifact = _artifact()
        json_response = dict(
            cast("dict[str, object]", artifact["json_parsed_transaction_response"])
        )
        json_response["meta"] = {}
        artifact["json_parsed_transaction_response"] = json_response
        base64_response = dict(
            cast("dict[str, object]", artifact["base64_transaction_response"])
        )
        base64_response["meta"] = {}
        artifact["base64_transaction_response"] = base64_response

        result = decode_pump_create_v2_fixture_artifact(artifact)

        self.assert_abstains(result, AbstainReason.UNSUPPORTED_PROTOCOL_STATE)

    def test_invalid_signature_abstains(self) -> None:
        """Fixture signatures need valid byte provenance."""

        artifact = _artifact({"signature": "not a base58 signature"})

        result = decode_pump_create_v2_fixture_artifact(artifact)

        self.assert_abstains(result, AbstainReason.UNSUPPORTED_PROTOCOL_STATE)

    def test_tampered_valid_signature_abstains(self) -> None:
        """Top-level signature must match parsed and raw transaction signatures."""

        artifact = _artifact({"signature": OTHER_SIGNATURE})

        result = decode_pump_create_v2_fixture_artifact(artifact)

        self.assert_abstains(result, AbstainReason.UNSUPPORTED_PROTOCOL_STATE)

    def test_finalized_slot_seen_before_as_of_slot_abstains(self) -> None:
        """Finalized provenance cannot precede the decoded transaction slot."""

        artifact = _artifact({"finalized_slot_seen": AS_OF_SLOT - 1})

        result = decode_pump_create_v2_fixture_artifact(artifact)

        self.assert_abstains(result, AbstainReason.UNSUPPORTED_PROTOCOL_STATE)

    def test_tampered_parsed_static_keyspace_abstains(self) -> None:
        """Parsed account keys cannot relabel non-fee raw static keys."""

        artifact = _artifact()
        fake_mint = _pubkey(210)
        json_response = dict(
            cast("dict[str, object]", artifact["json_parsed_transaction_response"])
        )
        transaction = dict(cast("dict[str, object]", json_response["transaction"]))
        message = dict(cast("dict[str, object]", transaction["message"]))
        account_keys = list(cast("list[str]", message["accountKeys"]))
        account_keys[1] = fake_mint
        message["accountKeys"] = account_keys
        transaction["message"] = message
        json_response["transaction"] = transaction
        artifact["json_parsed_transaction_response"] = json_response
        create_v2 = dict(cast("dict[str, object]", artifact["create_v2"]))
        instruction_pubkeys = list(cast("list[str]", create_v2["account_pubkeys"]))
        instruction_pubkeys[0] = fake_mint
        create_v2["account_pubkeys"] = instruction_pubkeys
        artifact["create_v2"] = create_v2

        result = decode_pump_create_v2_fixture_artifact(artifact)

        self.assert_abstains(result, AbstainReason.UNSUPPORTED_PROTOCOL_STATE)

    def test_json_parsed_full_keys_support_indices_beyond_static_raw_keys(
        self,
    ) -> None:
        """Parsed full keys can prove indices beyond raw static keys."""

        artifact = _artifact(static_account_count=10)

        result = decode_pump_create_v2_fixture_artifact(artifact)

        self.assertIsInstance(result, LaunchCreatedV2)
        if isinstance(result, AbstainResult):
            self.fail(result.message)
        self.assertEqual(result.program_id_index, 16)
        self.assertEqual(result.event_authority_account_index, 15)

    def test_missing_raw_loaded_addresses_abstains_when_indices_need_them(
        self,
    ) -> None:
        """Raw keyspace must cover every used loaded-address index."""

        artifact = _artifact(static_account_count=10, include_loaded_addresses=False)

        result = decode_pump_create_v2_fixture_artifact(artifact)

        self.assert_abstains(result, AbstainReason.UNSUPPORTED_PROTOCOL_STATE)

    def test_missing_json_parsed_account_keys_abstains(self) -> None:
        """Instruction-local account pubkeys alone are insufficient."""

        artifact = _artifact({"json_parsed_transaction_response": {"slot": AS_OF_SLOT}})

        result = decode_pump_create_v2_fixture_artifact(artifact)

        self.assert_abstains(result, AbstainReason.UNSUPPORTED_PROTOCOL_STATE)

    def test_wrong_base64_encoding_marker_abstains(self) -> None:
        """Raw transaction artifact must explicitly be base64 encoded."""

        artifact = _artifact()
        base64_response = dict(
            cast("dict[str, object]", artifact["base64_transaction_response"])
        )
        transaction_payload = list(cast("list[str]", base64_response["transaction"]))
        transaction_payload[1] = "json"
        base64_response["transaction"] = transaction_payload
        artifact["base64_transaction_response"] = base64_response

        result = decode_pump_create_v2_fixture_artifact(artifact)

        self.assert_abstains(result, AbstainReason.UNSUPPORTED_PROTOCOL_STATE)

    def test_raw_fee_payer_mismatch_abstains(self) -> None:
        """Raw and parsed message evidence must agree on fee payer."""

        full_keys = (_pubkey(201), *_account_pubkeys())
        artifact = _artifact()
        artifact["base64_transaction_response"] = _base64_transaction_response(
            full_keys=full_keys,
            instruction_indices=tuple(range(1, 17)),
            data=_create_data(),
            static_account_count=len(full_keys),
            include_loaded_addresses=True,
        )

        result = decode_pump_create_v2_fixture_artifact(artifact)

        self.assert_abstains(result, AbstainReason.UNSUPPORTED_PROTOCOL_STATE)

    def test_wrong_harvester_version_abstains(self) -> None:
        """Only the accepted fixture harvester contract can hydrate."""

        artifact = _artifact({"harvester_version": "wrong"})

        result = decode_pump_create_v2_fixture_artifact(artifact)

        self.assert_abstains(result, AbstainReason.DECODER_MISMATCH)

    def test_slot_mismatch_abstains(self) -> None:
        """Parsed and raw transaction responses must match artifact slot."""

        artifact = _artifact({"json_parsed_transaction_response": _json_response(900)})

        result = decode_pump_create_v2_fixture_artifact(artifact)

        self.assert_abstains(result, AbstainReason.UNSUPPORTED_PROTOCOL_STATE)

    def test_hydrator_does_not_import_trading_or_signer_paths(self) -> None:
        """Fixture hydration remains non-trading and non-signing."""

        source = FIXTURE_MODULE.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(FIXTURE_MODULE))
        violations = [
            imported_name
            for imported_name in _imported_module_names(tree)
            if imported_name.startswith(FORBIDDEN_IMPORT_PREFIXES)
        ]

        self.assertEqual(violations, [])
        self.assertNotIn("PRIVATE_KEY", source)

    def assert_abstains(self, result: object, reason: AbstainReason) -> None:
        self.assertIsInstance(result, AbstainResult)
        if isinstance(result, AbstainResult):
            self.assertEqual(result.reason, reason)


def _artifact(
    overrides: dict[str, object] | None = None,
    *,
    static_account_count: int | None = None,
    include_loaded_addresses: bool = True,
) -> dict[str, object]:
    full_keys = _full_message_pubkeys()
    instruction_indices = tuple(range(1, 17))
    data = _create_data()
    base64_response = _base64_transaction_response(
        full_keys=full_keys,
        instruction_indices=instruction_indices,
        data=data,
        static_account_count=static_account_count or len(full_keys),
        include_loaded_addresses=include_loaded_addresses,
    )
    artifact = {
        "schema_version": 1,
        "harvester_version": "pump-create-v2-fixture-harvest-v1",
        "signature": SIGNATURE,
        "as_of_slot": AS_OF_SLOT,
        "finalized_slot_seen": AS_OF_SLOT + 10,
        "pump_program_id": PUMP_PROGRAM_ID,
        "pump_idl_sha256": PINNED_PUMP_IDL_SHA256,
        "create_v2": {
            "instruction_index": 0,
            "program_id": PUMP_PROGRAM_ID,
            "account_pubkeys": list(_account_pubkeys()),
            "account_indices": list(instruction_indices),
            "program_id_index": 16,
            "data_base58": base58.b58encode(data).decode("ascii"),
        },
        "json_parsed_transaction_response": _json_response(AS_OF_SLOT),
        "base64_transaction_response": base64_response,
    }
    if overrides:
        artifact.update(overrides)
    return artifact


def _base64_transaction_response(
    *,
    full_keys: tuple[str, ...],
    instruction_indices: tuple[int, ...],
    data: bytes,
    static_account_count: int,
    include_loaded_addresses: bool,
) -> dict[str, object]:
    static_keys = tuple(
        Pubkey.from_string(pubkey) for pubkey in full_keys[:static_account_count]
    )
    instruction = CompiledInstruction(
        16,
        data,
        bytes(instruction_indices),
    )
    message = Message.new_with_compiled_instructions(
        1,
        0,
        0,
        list(static_keys),
        Hash.default(),
        [instruction],
    )
    transaction = VersionedTransaction.populate(
        message,
        [Signature.from_bytes(bytes([7]) * 64)],
    )
    response: dict[str, object] = {
        "slot": AS_OF_SLOT,
        "meta": {"err": None},
        "transaction": [
            base64.b64encode(bytes(transaction)).decode("ascii"),
            "base64",
        ],
    }
    if include_loaded_addresses and static_account_count < len(full_keys):
        loaded = full_keys[static_account_count:]
        response["meta"] = {
            "err": None,
            "loadedAddresses": {
                "writable": loaded,
                "readonly": [],
            },
        }
    return response


def _json_response(slot: int) -> dict[str, object]:
    return {
        "slot": slot,
        "meta": {"err": None},
        "transaction": {
            "message": {
                "accountKeys": list(_full_message_pubkeys()),
            },
            "signatures": [SIGNATURE],
        },
    }


def _full_message_pubkeys() -> tuple[str, ...]:
    return (_fee_payer(), *_account_pubkeys())


def _golden_account_indices() -> tuple[int, ...]:
    return (1, 23, 8, 7, 27, 0, 10, 24, 14, 15, 21, 18, 5, 9, 29, 11)


def _golden_account_pubkeys() -> tuple[str, ...]:
    return (
        "CvoPbuS2AghzVBYJx7HfQGhALiqif4YwWgHvXmhehuJZ",
        "GGYkiUJGopoH9DZUjGFtLiKpXXW5AQovqZ7eVcjEpump",
        "co4qggHSh6U3zT9NdqUCaW2kUYB29Ta7kMot9yfohem",
        "3umQ6KH8ah1zpKvA543c8jUzBHhvGKFSWw5Qf5cMKJys",
        "5ZbyNv3fr464sPaZ4GRUd14DhGUFygf7mBdXU7oiEVgm",
        "7mMka23CLiP8D9nu2YWYeSidNFkzT7WWHoqe8jwvqNyv",
        "AStRAnpi6kFrKypragExgeRoJ1QnKH7pbSjLAKQVWUum",
        "B63fy1dZC3FJu1b6m5EWJ3MNHgukM4j8AxRG8WujVdoQ",
        "GjcTf82RaMLVtjxa4aGMNBT2enax2Y6YMJ58DPLFsf1E",
        "Gwz23pDuNbsvd8QJyyfHV9EeRKVUSBRhy8wfqcLpvcfK",
        SYSTEM_PROGRAM_ID,
        PUMP_PROGRAM_ID,
        "ComputeBudget111111111111111111111111111111",
        "6zSo8Z25VVS3yqfqjDgMgf7nWHnfhVMEnEyVtx16tJQ5",
        ASSOCIATED_SPL_PROGRAM_ID,
        MAYHEM_PROGRAM_ID,
        "devAAvkxwyogNgy4z7R3n1ADUvJkmzy4qszDF6UiAcM",
        "62qc2CNXwrYqQScmEdiZFFAnJR262PxWEuNQtxfafNgV",
        "BwWK17cbHxwWBKZkUYvzxLcNQ1YVyaFezduWbtm2de6s",
        "GXPFM2caqTtQYC2cJ5yJRi9VDkpsYZXzYdwYpGnLmtDL",
        "Hq2wp8uJ9jCPsYgNHex8RtqdvMPfVGoYwjvF1ATiwn2Y",
        "13ec7XdrjF3h3YcqBTFDSReRcUFwbCnJaAQspM4j6DDJ",
        "SysvarRent111111111111111111111111111111111",
        "TSLvdd1pWpHVjahSpsvCXUbgwsL3JAcvokwaKt1eokM",
        SPL_2022_PROGRAM_ID,
        "jitodontfrontB1111111Dep1oyedUsingJ7Tracker",
        "pfeeUxB6jkeY1Hxd7CsFCAjcbHA9rWtchMGdZ6VojVZ",
        "4wTV1YmiEkRvAtNtsSGPtUrqRYQMe5SKy2uB4Jjaxnjf",
        "8Wf5TiAheLUqBrKXeYg2JtAFFMWtKdG2BSFgqUcPVwTt",
        "Ce6TQqeHC9p8KetsN6JsjHK7UTZk7nasjjnr7XxXp9F1",
    )


def _golden_account_role_proofs() -> tuple[tuple[str, str], ...]:
    return (
        ("mint", "GGYkiUJGopoH9DZUjGFtLiKpXXW5AQovqZ7eVcjEpump"),
        ("mint_authority", "TSLvdd1pWpHVjahSpsvCXUbgwsL3JAcvokwaKt1eokM"),
        ("bonding_curve", "GjcTf82RaMLVtjxa4aGMNBT2enax2Y6YMJ58DPLFsf1E"),
        (
            "associated_bonding_curve",
            "B63fy1dZC3FJu1b6m5EWJ3MNHgukM4j8AxRG8WujVdoQ",
        ),
        ("global", "4wTV1YmiEkRvAtNtsSGPtUrqRYQMe5SKy2uB4Jjaxnjf"),
        ("user", "CvoPbuS2AghzVBYJx7HfQGhALiqif4YwWgHvXmhehuJZ"),
        ("system_program", SYSTEM_PROGRAM_ID),
        ("token_program", SPL_2022_PROGRAM_ID),
        ("associated_token_program", ASSOCIATED_SPL_PROGRAM_ID),
        ("mayhem_program_id", MAYHEM_PROGRAM_ID),
        ("global_params", "13ec7XdrjF3h3YcqBTFDSReRcUFwbCnJaAQspM4j6DDJ"),
        ("sol_vault", "BwWK17cbHxwWBKZkUYvzxLcNQ1YVyaFezduWbtm2de6s"),
        ("mayhem_state", "7mMka23CLiP8D9nu2YWYeSidNFkzT7WWHoqe8jwvqNyv"),
        (
            "mayhem_token_vault",
            "Gwz23pDuNbsvd8QJyyfHV9EeRKVUSBRhy8wfqcLpvcfK",
        ),
        ("event_authority", "Ce6TQqeHC9p8KetsN6JsjHK7UTZk7nasjjnr7XxXp9F1"),
        ("program", PUMP_PROGRAM_ID),
    )


def _account_pubkeys() -> tuple[str, ...]:
    pubkeys = [_pubkey(index + 1) for index in range(len(CREATE_V2_ACCOUNT_NAMES))]
    fixed_positions = {
        6: SYSTEM_PROGRAM_ID,
        7: SPL_2022_PROGRAM_ID,
        8: ASSOCIATED_SPL_PROGRAM_ID,
        9: MAYHEM_PROGRAM_ID,
        15: PUMP_PROGRAM_ID,
    }
    for index, pubkey in fixed_positions.items():
        pubkeys[index] = pubkey
    return tuple(pubkeys)


def _fee_payer() -> str:
    return _pubkey(200)


def _pubkey(seed: int) -> str:
    return str(Pubkey.from_bytes(bytes([seed]) * 32))


def _create_data() -> bytes:
    return (
        CREATE_V2_DISCRIMINATOR
        + _string("Fixture Coin")
        + _string("FIX")
        + _string("ipfs://fixture")
        + CREATOR_BYTES
        + b"\x00"
        + b"\x01"
    )


def _string(value: str) -> bytes:
    encoded = value.encode("utf-8")
    return struct.pack("<I", len(encoded)) + encoded


def _imported_module_names(tree: ast.AST) -> list[str]:
    names: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            names.append(node.module)
    return names


if __name__ == "__main__":
    unittest.main()
