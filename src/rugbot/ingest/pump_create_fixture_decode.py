"""Hydrate harvested Pump create_v2 fixtures into decoded launch evidence."""

import base64
import binascii
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import cast

import base58
from solders.transaction import VersionedTransaction

from rugbot.domain.account_roles import AccountRoleProof
from rugbot.domain.amounts import Slot
from rugbot.domain.decisions import AbstainReason, AbstainResult
from rugbot.domain.launches import (
    LaunchActorProof,
    LaunchActorRole,
    LaunchCreatedV2,
)
from rugbot.protocol.pump.create_decoder import (
    CREATE_V2_ACCOUNT_NAMES,
    PINNED_PUMP_IDL_SHA256,
    PUMP_CREATE_V2_DECODER_VERSION,
    PUMP_PROGRAM_ID,
    CompiledPumpCreateV2Instruction,
    decode_pump_create_v2_instruction,
)

PUMP_CREATE_V2_FIXTURE_HYDRATOR_VERSION = "pump-create-v2-fixture-hydrator-v1"
PUMP_CREATE_V2_FIXTURE_HARVESTER_VERSION = "pump-create-v2-fixture-harvest-v1"
SIGNATURE_SIZE = 64
HARVEST_SCHEMA_VERSION = 1
BASE64_TRANSACTION_PAYLOAD_MIN_ITEMS = 2

FixtureDecodeResult = LaunchCreatedV2 | AbstainResult


def decode_pump_create_v2_fixture_artifact(
    artifact: Mapping[str, object],
    *,
    decoder_version: str = PUMP_CREATE_V2_DECODER_VERSION,
    hydrator_version: str = PUMP_CREATE_V2_FIXTURE_HYDRATOR_VERSION,
) -> FixtureDecodeResult:
    """Decode a harvested Pump create_v2 fixture artifact.

    Args:
        artifact: Immutable fixture artifact emitted by
            `pump_fixture_harvest`.
        decoder_version: Pinned create_v2 decoder version.
        hydrator_version: Version of this artifact hydrator.

    Returns:
        LaunchCreatedV2 when the fixture proves the pinned layout, otherwise an
        AbstainResult. This hydrator performs no RPC and never loads signing
        keys; it only re-decodes already captured finalized fixture evidence.
    """

    artifact_error = _validate_artifact_versions(
        artifact=artifact,
        hydrator_version=hydrator_version,
    )
    if artifact_error is not None:
        return artifact_error

    fixture = _fixture_context(artifact, hydrator_version)
    if isinstance(fixture, AbstainResult):
        return fixture

    instruction = _compiled_instruction_from_fixture(fixture)
    if isinstance(instruction, AbstainResult):
        return instruction

    return decode_pump_create_v2_instruction(
        instruction,
        idl_hash=fixture.idl_hash,
        decoder_version=decoder_version,
    )


@dataclass(frozen=True, slots=True)
class _FixtureContext:
    """Internal typed fixture context without exposing a public contract."""

    as_of_slot: Slot
    signature: bytes
    signature_text: str
    idl_hash: str
    create_v2: Mapping[str, object]
    transaction: VersionedTransaction
    account_pubkeys: tuple[str, ...]
    raw_account_pubkeys: tuple[str, ...]
    hydrator_version: str


@dataclass(frozen=True, slots=True)
class _FixtureHeader:
    """Validated fixture header fields."""

    as_of_slot: Slot
    signature: bytes
    signature_text: str
    create_v2: Mapping[str, object]


@dataclass(frozen=True, slots=True)
class _FixtureKeyspaces:
    """Parsed and raw account keyspaces proven by fixture evidence."""

    parsed_account_pubkeys: tuple[str, ...]
    raw_account_pubkeys: tuple[str, ...]


def _validate_artifact_versions(
    *,
    artifact: Mapping[str, object],
    hydrator_version: str,
) -> AbstainResult | None:
    as_of_slot = _artifact_slot_or_unknown(artifact)
    if not isinstance(hydrator_version, str) or not hydrator_version:
        return _abstain(
            reason=AbstainReason.DECODER_MISMATCH,
            message="create_v2 fixture hydrator_version is required",
            as_of_slot=as_of_slot,
        )
    if artifact.get("schema_version") != HARVEST_SCHEMA_VERSION:
        return _abstain(
            reason=AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
            message="fixture schema_version is unsupported",
            as_of_slot=as_of_slot,
        )
    if artifact.get("harvester_version") != PUMP_CREATE_V2_FIXTURE_HARVESTER_VERSION:
        return _abstain(
            reason=AbstainReason.DECODER_MISMATCH,
            message="fixture harvester_version is not accepted",
            as_of_slot=as_of_slot,
        )
    if artifact.get("pump_program_id") != PUMP_PROGRAM_ID:
        return _abstain(
            reason=AbstainReason.UNKNOWN_PROTOCOL_STATE,
            message="fixture pump_program_id is not the pinned Pump program",
            as_of_slot=as_of_slot,
        )
    if artifact.get("pump_idl_sha256") != PINNED_PUMP_IDL_SHA256:
        return _abstain(
            reason=AbstainReason.DECODER_MISMATCH,
            message="fixture Pump IDL hash does not match the pinned decoder",
            as_of_slot=as_of_slot,
        )
    return None


def _fixture_context(
    artifact: Mapping[str, object],
    hydrator_version: str,
) -> _FixtureContext | AbstainResult:
    header = _fixture_header(artifact)
    if isinstance(header, AbstainResult):
        return header

    base64_response = artifact.get("base64_transaction_response")
    transaction = _versioned_transaction(base64_response)
    if isinstance(transaction, AbstainResult):
        return transaction

    provenance_error = _validate_fixture_provenance(
        artifact=artifact,
        header=header,
        transaction=transaction,
    )
    if provenance_error is not None:
        return provenance_error

    slot_error = _validate_fixture_slots(
        artifact=artifact,
        as_of_slot=header.as_of_slot,
    )
    if slot_error is not None:
        return slot_error

    keyspaces = _fixture_keyspaces(
        artifact=artifact,
        transaction=transaction,
        as_of_slot=header.as_of_slot,
    )
    if isinstance(keyspaces, AbstainResult):
        return keyspaces

    return _FixtureContext(
        as_of_slot=header.as_of_slot,
        signature=header.signature,
        signature_text=header.signature_text,
        idl_hash=cast("str", artifact["pump_idl_sha256"]),
        create_v2=header.create_v2,
        transaction=transaction,
        account_pubkeys=keyspaces.parsed_account_pubkeys,
        raw_account_pubkeys=keyspaces.raw_account_pubkeys,
        hydrator_version=hydrator_version,
    )


def _fixture_keyspaces(
    *,
    artifact: Mapping[str, object],
    transaction: VersionedTransaction,
    as_of_slot: Slot,
) -> _FixtureKeyspaces | AbstainResult:
    parsed_account_pubkeys = _json_parsed_account_pubkeys(
        artifact.get("json_parsed_transaction_response"),
        as_of_slot,
    )
    if isinstance(parsed_account_pubkeys, AbstainResult):
        return parsed_account_pubkeys

    raw_account_pubkeys = _raw_full_account_pubkeys(
        transaction=transaction,
        base64_response=artifact.get("base64_transaction_response"),
        as_of_slot=as_of_slot,
    )
    if isinstance(raw_account_pubkeys, AbstainResult):
        return raw_account_pubkeys

    fee_payer_error = _validate_raw_fee_payer(
        raw_account_pubkeys=raw_account_pubkeys,
        parsed_account_pubkeys=parsed_account_pubkeys,
        as_of_slot=as_of_slot,
    )
    if fee_payer_error is not None:
        return fee_payer_error

    return _FixtureKeyspaces(
        parsed_account_pubkeys=parsed_account_pubkeys,
        raw_account_pubkeys=raw_account_pubkeys,
    )


def _fixture_header(artifact: Mapping[str, object]) -> _FixtureHeader | AbstainResult:
    as_of_slot = _required_slot(artifact.get("as_of_slot"))
    if as_of_slot is None:
        return _unsupported(
            "fixture as_of_slot must be a non-negative integer", Slot(0)
        )

    signature_text = artifact.get("signature")
    signature = _decode_signature(signature_text)
    if signature is None:
        return _unsupported("fixture signature is not valid base58 bytes", as_of_slot)

    create_v2 = artifact.get("create_v2")
    if not isinstance(create_v2, Mapping):
        return _unsupported("fixture create_v2 object is required", as_of_slot)

    return _FixtureHeader(
        as_of_slot=as_of_slot,
        signature=signature,
        signature_text=cast("str", signature_text),
        create_v2=cast("Mapping[str, object]", create_v2),
    )


def _validate_fixture_provenance(
    *,
    artifact: Mapping[str, object],
    header: _FixtureHeader,
    transaction: VersionedTransaction,
) -> AbstainResult | None:
    finalized_slot_seen = _required_slot(artifact.get("finalized_slot_seen"))
    if finalized_slot_seen is None or int(finalized_slot_seen) < int(header.as_of_slot):
        return _unsupported(
            "fixture finalized_slot_seen must be at or after as_of_slot",
            header.as_of_slot,
        )

    json_error = _validate_json_parsed_signature(
        artifact.get("json_parsed_transaction_response"),
        header,
    )
    if json_error is not None:
        return json_error
    return _validate_raw_signature(transaction, header)


def _validate_json_parsed_signature(
    json_response: object,
    header: _FixtureHeader,
) -> AbstainResult | None:
    if not isinstance(json_response, Mapping):
        return _unsupported("jsonParsed response must be an object", header.as_of_slot)
    transaction = json_response.get("transaction")
    if not isinstance(transaction, Mapping):
        return _unsupported(
            "jsonParsed transaction object is required",
            header.as_of_slot,
        )
    signatures = _tuple_of_strings(transaction.get("signatures"))
    if signatures is None or not signatures:
        return _unsupported(
            "jsonParsed transaction signatures are required",
            header.as_of_slot,
        )
    if signatures[0] != header.signature_text:
        return _unsupported(
            "fixture signature does not match jsonParsed transaction signature",
            header.as_of_slot,
        )
    return None


def _validate_raw_signature(
    transaction: VersionedTransaction,
    header: _FixtureHeader,
) -> AbstainResult | None:
    signatures = tuple(str(signature) for signature in transaction.signatures)
    if not signatures:
        return _unsupported(
            "raw transaction signatures are required", header.as_of_slot
        )
    if signatures[0] != header.signature_text:
        return _unsupported(
            "fixture signature does not match raw transaction signature",
            header.as_of_slot,
        )
    return None


def _compiled_instruction_from_fixture(
    fixture: _FixtureContext,
) -> CompiledPumpCreateV2Instruction | AbstainResult:
    instruction_index = _required_non_negative_int(
        fixture.create_v2.get("instruction_index")
    )
    if instruction_index is None:
        return _unsupported(
            "fixture create_v2 instruction_index must be non-negative",
            fixture.as_of_slot,
        )
    if instruction_index >= len(fixture.transaction.message.instructions):
        return _unsupported(
            "fixture create_v2 instruction_index is outside transaction",
            fixture.as_of_slot,
        )

    compiled = fixture.transaction.message.instructions[instruction_index]
    compiled_accounts = tuple(int(index) for index in compiled.accounts)
    program_id_index = int(compiled.program_id_index)
    data = bytes(compiled.data)

    evidence_error = _validate_create_v2_fixture_evidence(
        fixture=fixture,
        compiled_accounts=compiled_accounts,
        program_id_index=program_id_index,
        data=data,
    )
    if evidence_error is not None:
        return evidence_error

    return CompiledPumpCreateV2Instruction(
        as_of_slot=fixture.as_of_slot,
        program_id=PUMP_PROGRAM_ID,
        program_id_index=program_id_index,
        account_indices=compiled_accounts,
        account_pubkeys=fixture.account_pubkeys,
        account_role_proofs=_account_role_proofs(fixture, compiled_accounts),
        data=data,
        transaction_index=None,
        outer_instruction_index=instruction_index,
        signature=fixture.signature,
        actor_role_proofs=(_fee_payer_proof(fixture),),
        transaction_slot_account_state_available=False,
    )


def _validate_create_v2_fixture_evidence(
    *,
    fixture: _FixtureContext,
    compiled_accounts: tuple[int, ...],
    program_id_index: int,
    data: bytes,
) -> AbstainResult | None:
    evidence = fixture.create_v2
    if evidence.get("program_id") != PUMP_PROGRAM_ID:
        return _unsupported(
            "fixture create_v2 program_id is unsupported",
            fixture.as_of_slot,
        )
    if _tuple_of_ints(evidence.get("account_indices")) != compiled_accounts:
        return _unsupported(
            "fixture create_v2 account_indices do not match transaction bytes",
            fixture.as_of_slot,
        )
    if _required_non_negative_int(evidence.get("program_id_index")) != program_id_index:
        return _unsupported(
            "fixture create_v2 program_id_index does not match transaction bytes",
            fixture.as_of_slot,
        )
    if _decoded_data(evidence.get("data_base58")) != data:
        return _unsupported(
            "fixture create_v2 data does not match transaction bytes",
            fixture.as_of_slot,
        )
    keyspace_error = _validate_full_keyspace_covers_instruction(
        fixture=fixture,
        compiled_accounts=compiled_accounts,
        program_id_index=program_id_index,
    )
    if keyspace_error is not None:
        return keyspace_error
    return _validate_instruction_pubkey_evidence(
        fixture=fixture,
        compiled_accounts=compiled_accounts,
    )


def _validate_instruction_pubkey_evidence(
    *,
    fixture: _FixtureContext,
    compiled_accounts: tuple[int, ...],
) -> AbstainResult | None:
    instruction_pubkeys = _tuple_of_strings(fixture.create_v2.get("account_pubkeys"))
    if instruction_pubkeys is None:
        return _unsupported(
            "fixture create_v2 account_pubkeys are required",
            fixture.as_of_slot,
        )
    if len(instruction_pubkeys) != len(compiled_accounts):
        return _unsupported(
            "fixture create_v2 account_pubkeys do not match account_indices",
            fixture.as_of_slot,
        )
    if any(index >= len(fixture.account_pubkeys) for index in compiled_accounts):
        return _unsupported(
            "fixture compiled account index is outside full account keys",
            fixture.as_of_slot,
        )
    for position, account_index in enumerate(compiled_accounts):
        if fixture.account_pubkeys[account_index] != instruction_pubkeys[position]:
            return _unsupported(
                "fixture instruction account pubkey proof mismatch",
                fixture.as_of_slot,
            )
    return None


def _account_role_proofs(
    fixture: _FixtureContext,
    compiled_accounts: tuple[int, ...],
) -> tuple[AccountRoleProof, ...]:
    return tuple(
        AccountRoleProof(
            name=name,
            pubkey=fixture.account_pubkeys[compiled_accounts[position]],
        )
        for position, name in enumerate(CREATE_V2_ACCOUNT_NAMES)
    )


def _fee_payer_proof(fixture: _FixtureContext) -> LaunchActorProof:
    return LaunchActorProof(
        as_of_slot=fixture.as_of_slot,
        role=LaunchActorRole.FEE_PAYER,
        account_index=0,
        pubkey=fixture.account_pubkeys[0],
        evidence_ids=(
            f"fixture:{fixture.signature_text}:jsonParsed.message.accountKeys[0]",
            f"fixture:{fixture.signature_text}:base64.message.account_keys[0]",
        ),
        source_version="solana-message-fee-payer-v1",
    )


def _validate_fixture_slots(
    *,
    artifact: Mapping[str, object],
    as_of_slot: Slot,
) -> AbstainResult | None:
    json_error = _validate_transaction_response_status(
        response=artifact.get("json_parsed_transaction_response"),
        as_of_slot=as_of_slot,
        label="jsonParsed",
    )
    if json_error is not None:
        return json_error
    return _validate_transaction_response_status(
        response=artifact.get("base64_transaction_response"),
        as_of_slot=as_of_slot,
        label="base64",
    )


def _json_parsed_account_pubkeys(
    json_response: object,
    as_of_slot: Slot,
) -> tuple[str, ...] | AbstainResult:
    message = _transaction_message(json_response)
    if message is None:
        return _unsupported("jsonParsed transaction message is required", as_of_slot)
    account_keys = message.get("accountKeys")
    if not isinstance(account_keys, Sequence) or isinstance(account_keys, str):
        return _unsupported(
            "jsonParsed message.accountKeys are required",
            as_of_slot,
        )
    result = []
    for account_key in account_keys:
        pubkey = _account_key_pubkey(account_key)
        if pubkey is None:
            return _unsupported(
                "jsonParsed message.accountKeys are malformed",
                as_of_slot,
            )
        result.append(pubkey)
    if not result:
        return _unsupported(
            "jsonParsed message.accountKeys are required",
            as_of_slot,
        )
    return tuple(result)


def _raw_full_account_pubkeys(
    *,
    transaction: VersionedTransaction,
    base64_response: object,
    as_of_slot: Slot,
) -> tuple[str, ...] | AbstainResult:
    static_pubkeys = tuple(str(pubkey) for pubkey in transaction.message.account_keys)
    if not static_pubkeys:
        return _unsupported("raw transaction account keys are required", as_of_slot)
    loaded_pubkeys = _loaded_address_pubkeys(base64_response, as_of_slot)
    if isinstance(loaded_pubkeys, AbstainResult):
        return loaded_pubkeys
    return (*static_pubkeys, *loaded_pubkeys)


def _loaded_address_pubkeys(
    base64_response: object,
    as_of_slot: Slot,
) -> tuple[str, ...] | AbstainResult:
    if not isinstance(base64_response, Mapping):
        return _unsupported("base64 transaction response must be an object", as_of_slot)
    meta = base64_response.get("meta")
    if not isinstance(meta, Mapping):
        return _unsupported("base64 transaction meta is required", as_of_slot)
    loaded_addresses = meta.get("loadedAddresses")
    if loaded_addresses is None:
        return ()
    if not isinstance(loaded_addresses, Mapping):
        return _unsupported("loadedAddresses must be an object", as_of_slot)
    writable = _tuple_of_strings(loaded_addresses.get("writable"))
    readonly = _tuple_of_strings(
        loaded_addresses.get("readonly", loaded_addresses.get("readOnly"))
    )
    if writable is None or readonly is None:
        return _unsupported("loadedAddresses entries must be string arrays", as_of_slot)
    return (*writable, *readonly)


def _validate_raw_fee_payer(
    *,
    raw_account_pubkeys: tuple[str, ...],
    parsed_account_pubkeys: tuple[str, ...],
    as_of_slot: Slot,
) -> AbstainResult | None:
    if raw_account_pubkeys[0] != parsed_account_pubkeys[0]:
        return _unsupported(
            "raw and jsonParsed fee payer account keys disagree",
            as_of_slot,
        )
    return None


def _transaction_message(response: object) -> Mapping[str, object] | None:
    if not isinstance(response, Mapping):
        return None
    transaction = response.get("transaction")
    if not isinstance(transaction, Mapping):
        return None
    message = transaction.get("message")
    if not isinstance(message, Mapping):
        return None
    return cast("Mapping[str, object]", message)


def _account_key_pubkey(value: object) -> str | None:
    if isinstance(value, str) and value:
        return value
    if not isinstance(value, Mapping):
        return None
    pubkey = value.get("pubkey")
    if isinstance(pubkey, str) and pubkey:
        return pubkey
    return None


def _transaction_response_slot(response: object) -> Slot | None:
    if not isinstance(response, Mapping):
        return None
    return _required_slot(response.get("slot"))


def _validate_transaction_response_status(
    *,
    response: object,
    as_of_slot: Slot,
    label: str,
) -> AbstainResult | None:
    if _transaction_response_slot(response) != as_of_slot:
        return _unsupported(
            f"fixture {label} transaction response slot must match as_of_slot",
            as_of_slot,
        )
    if not isinstance(response, Mapping):
        return _unsupported(f"fixture {label} response must be an object", as_of_slot)
    meta = response.get("meta")
    if not isinstance(meta, Mapping):
        return _unsupported(f"fixture {label} response meta is required", as_of_slot)
    if "err" not in meta or meta["err"] is not None:
        return _unsupported(
            f"fixture {label} transaction must be successful",
            as_of_slot,
        )
    return None


def _validate_full_keyspace_covers_instruction(
    *,
    fixture: _FixtureContext,
    compiled_accounts: tuple[int, ...],
    program_id_index: int,
) -> AbstainResult | None:
    bounds_error = _validate_keyspace_bounds(
        fixture=fixture,
        compiled_accounts=compiled_accounts,
        program_id_index=program_id_index,
    )
    if bounds_error is not None:
        return bounds_error
    equality_error = _validate_used_keyspace_equality(
        fixture=fixture,
        compiled_accounts=compiled_accounts,
        program_id_index=program_id_index,
    )
    if equality_error is not None:
        return equality_error
    if fixture.account_pubkeys[program_id_index] != PUMP_PROGRAM_ID:
        return _unsupported(
            "fixture program_id_index does not resolve to Pump program",
            fixture.as_of_slot,
        )
    return None


def _validate_keyspace_bounds(
    *,
    fixture: _FixtureContext,
    compiled_accounts: tuple[int, ...],
    program_id_index: int,
) -> AbstainResult | None:
    parsed_key_count = len(fixture.account_pubkeys)
    raw_key_count = len(fixture.raw_account_pubkeys)
    if program_id_index >= parsed_key_count:
        return _unsupported(
            "fixture program_id_index is outside full account keys",
            fixture.as_of_slot,
        )
    if program_id_index >= raw_key_count:
        return _unsupported(
            "fixture program_id_index is outside raw account keys",
            fixture.as_of_slot,
        )
    if any(index >= parsed_key_count for index in compiled_accounts):
        return _unsupported(
            "fixture compiled account index is outside full account keys",
            fixture.as_of_slot,
        )
    if any(index >= raw_key_count for index in compiled_accounts):
        return _unsupported(
            "fixture compiled account index is outside raw account keys",
            fixture.as_of_slot,
        )
    return None


def _validate_used_keyspace_equality(
    *,
    fixture: _FixtureContext,
    compiled_accounts: tuple[int, ...],
    program_id_index: int,
) -> AbstainResult | None:
    for account_index in (*compiled_accounts, program_id_index):
        if (
            fixture.raw_account_pubkeys[account_index]
            != fixture.account_pubkeys[account_index]
        ):
            return _unsupported(
                "fixture raw and jsonParsed account keyspace disagree",
                fixture.as_of_slot,
            )
    return None


def _versioned_transaction(
    base64_response: object,
) -> VersionedTransaction | AbstainResult:
    as_of_slot = _response_slot_or_unknown(base64_response)
    transaction_bytes = _raw_transaction_bytes(base64_response)
    if transaction_bytes is None:
        return _unsupported(
            "base64 transaction bytes are required",
            as_of_slot,
        )
    try:
        return VersionedTransaction.from_bytes(transaction_bytes)
    except ValueError:
        return _unsupported("base64 transaction bytes are invalid", as_of_slot)


def _raw_transaction_bytes(base64_response: object) -> bytes | None:
    if not isinstance(base64_response, Mapping):
        return None
    encoded_transaction = base64_response.get("transaction")
    if not isinstance(encoded_transaction, Sequence) or isinstance(
        encoded_transaction, str
    ):
        return None
    if len(encoded_transaction) < BASE64_TRANSACTION_PAYLOAD_MIN_ITEMS:
        return None
    first_item = encoded_transaction[0]
    encoding = encoded_transaction[1]
    if not isinstance(first_item, str) or encoding != "base64":
        return None
    try:
        return base64.b64decode(first_item, validate=True)
    except (ValueError, binascii.Error):
        return None


def _decoded_data(value: object) -> bytes | None:
    if not isinstance(value, str):
        return None
    try:
        return base58.b58decode(value)
    except ValueError:
        return None


def _decode_signature(value: object) -> bytes | None:
    decoded = _decoded_data(value)
    if decoded is None or len(decoded) != SIGNATURE_SIZE:
        return None
    return decoded


def _required_slot(value: object) -> Slot | None:
    if type(value) is int and value >= 0:
        return Slot(value)
    return None


def _required_non_negative_int(value: object) -> int | None:
    if type(value) is int and value >= 0:
        return value
    return None


def _tuple_of_ints(value: object) -> tuple[int, ...] | None:
    if not isinstance(value, Sequence) or isinstance(value, str):
        return None
    result = []
    for item in value:
        if type(item) is not int or item < 0:
            return None
        result.append(item)
    return tuple(result)


def _tuple_of_strings(value: object) -> tuple[str, ...] | None:
    if not isinstance(value, Sequence) or isinstance(value, str):
        return None
    result = []
    for item in value:
        if not isinstance(item, str) or not item:
            return None
        result.append(item)
    return tuple(result)


def _artifact_slot_or_unknown(artifact: Mapping[str, object]) -> Slot:
    return _required_slot(artifact.get("as_of_slot")) or Slot(0)


def _response_slot_or_unknown(response: object) -> Slot:
    if not isinstance(response, Mapping):
        return Slot(0)
    return _required_slot(response.get("slot")) or Slot(0)


def _unsupported(message: str, as_of_slot: Slot) -> AbstainResult:
    return _abstain(
        reason=AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
        message=message,
        as_of_slot=as_of_slot,
    )


def _abstain(
    *,
    reason: AbstainReason,
    message: str,
    as_of_slot: Slot,
) -> AbstainResult:
    return AbstainResult(reason=reason, message=message, as_of_slot=int(as_of_slot))
