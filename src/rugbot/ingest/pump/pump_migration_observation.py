"""Decode finalized Pump migration instructions from immutable RPC evidence."""

# The response envelope is validated field by field so malformed evidence
# cannot be silently converted into migration evidence.
# ruff: noqa: PLR0911

from __future__ import annotations

import json
from collections.abc import Mapping

import base58

from rugbot.domain.account_roles import AccountRoleProof
from rugbot.domain.amounts import Slot
from rugbot.domain.decisions import AbstainReason, AbstainResult
from rugbot.domain.migration import (
    MIGRATE_ACCOUNT_NAMES,
    MIGRATE_DISCRIMINATOR,
    PINNED_PUMP_IDL_SHA256,
    PINNED_PUMP_SWAP_IDL_SHA256,
    PUMP_MIGRATION_DECODER_VERSION,
    PUMP_PROGRAM_ID,
    CompiledPumpMigrationInstruction,
    verify_pump_migration_instruction,
)
from rugbot.domain.migrations import PumpMigrationInstructionEvidence
from rugbot.domain.observations import RawChainObservation
from rugbot.ingest.rpc_observer import JSON_TRANSACTION_FORMAT

PumpMigrationObservationResult = PumpMigrationInstructionEvidence | AbstainResult | None
SIGNATURE_LENGTH = 64


class _DuplicateJsonKeyError(ValueError):
    """Raised when RPC evidence contains duplicate JSON object keys."""


def decode_pump_migration_observation(
    observation: RawChainObservation,
) -> PumpMigrationObservationResult:
    """Decode one exact finalized Pump migration from raw getTransaction JSON.

    The returned evidence keeps the observation slot, signature, transaction
    index, instruction index, account indices, and resolved account pubkeys.
    The original raw JSON remains owned by ``observation.raw_source_payload``
    and is never reconstructed or replaced by this decoder.

    Returns:
        Verified migration evidence, ``None`` when no Pump migration is
        present, or an abstention for malformed or ambiguous evidence.
    """

    validation = _validate_observation(observation)
    if validation is not None:
        return validation
    try:
        envelope = json.loads(
            observation.raw_source_payload,
            object_pairs_hook=_strict_json_object,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
        return _abstain(
            AbstainReason.UNKNOWN_PROTOCOL_STATE,
            "Pump migration observation contains invalid JSON",
            observation.slot,
        )

    transaction = _load_transaction(envelope, observation)
    if isinstance(transaction, AbstainResult):
        return transaction
    message, account_pubkeys = transaction
    instructions = message.get("instructions")
    if not isinstance(instructions, list):
        return _abstain(
            AbstainReason.MISSING_FEATURE,
            "Pump migration transaction instructions are missing",
            observation.slot,
        )

    matches: list[CompiledPumpMigrationInstruction] = []
    for outer_index, raw_instruction in enumerate(instructions):
        candidate = _compiled_instruction(
            raw_instruction,
            observation=observation,
            account_pubkeys=account_pubkeys,
            outer_index=outer_index,
        )
        if isinstance(candidate, AbstainResult):
            return candidate
        if candidate is not None:
            matches.append(candidate)

    if not matches:
        return None
    if len(matches) != 1:
        return _abstain(
            AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
            "transaction contains multiple Pump migration instructions",
            observation.slot,
        )

    return verify_pump_migration_instruction(
        matches[0],
        pump_idl_hash=PINNED_PUMP_IDL_SHA256,
        pump_swap_idl_hash=PINNED_PUMP_SWAP_IDL_SHA256,
        decoder_version=PUMP_MIGRATION_DECODER_VERSION,
    )


def _validate_observation(observation: object) -> AbstainResult | None:
    if type(observation) is not RawChainObservation:
        return _abstain(
            AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
            "Pump migration observation is malformed",
            -1,
        )
    if type(observation.slot) is not int or observation.slot < 0:
        return _abstain(
            AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
            "Pump migration observation slot is invalid",
            observation.slot,
        )
    if (
        observation.commitment != "finalized"
        or observation.canonical_status != "canonical"
        or observation.source_update_kind != "transaction"
        or observation.raw_transaction_format != JSON_TRANSACTION_FORMAT
        or type(observation.raw_source_payload) is not bytes
    ):
        return _abstain(
            AbstainReason.STALE_STATE,
            "Pump migration decoder requires finalized raw getTransaction evidence",
            observation.slot,
        )
    if (
        type(observation.transaction_index) is not int
        or observation.transaction_index < 0
        or type(observation.signature) is not bytes
        or len(observation.signature) != SIGNATURE_LENGTH
    ):
        return _abstain(
            AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
            "Pump migration transaction identity is incomplete",
            observation.slot,
        )
    return None


def _load_transaction(
    envelope: object,
    observation: RawChainObservation,
) -> tuple[Mapping[str, object], tuple[str, ...]] | AbstainResult:
    if not isinstance(envelope, Mapping) or envelope.get("jsonrpc") != "2.0":
        return _abstain(
            AbstainReason.UNKNOWN_PROTOCOL_STATE,
            "Pump migration observation envelope is malformed",
            observation.slot,
        )
    result = envelope.get("result")
    if (
        not isinstance(result, Mapping)
        or type(result.get("slot")) is not int
        or result.get("slot") != observation.slot
    ):
        return _abstain(
            AbstainReason.STALE_STATE,
            "Pump migration payload slot does not match observation",
            observation.slot,
        )
    meta = result.get("meta")
    transaction = result.get("transaction")
    if not isinstance(meta, Mapping) or not isinstance(transaction, Mapping):
        return _abstain(
            AbstainReason.MISSING_FEATURE,
            "Pump migration transaction metadata is incomplete",
            observation.slot,
        )
    if meta.get("err") is not None:
        return _abstain(
            AbstainReason.UNKNOWN_PROTOCOL_STATE,
            "failed finalized transaction cannot produce Pump migration evidence",
            observation.slot,
        )

    signatures = transaction.get("signatures")
    expected_signature = base58.b58encode(observation.signature).decode("ascii")
    if (
        not isinstance(signatures, list)
        or not signatures
        or any(type(item) is not str for item in signatures)
        or signatures[0] != expected_signature
    ):
        return _abstain(
            AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
            "Pump migration transaction signature does not match observation",
            observation.slot,
        )
    message = transaction.get("message")
    if not isinstance(message, Mapping):
        return _abstain(
            AbstainReason.MISSING_FEATURE,
            "Pump migration transaction message is missing",
            observation.slot,
        )
    account_pubkeys = _account_pubkeys(message, meta, observation.slot)
    if isinstance(account_pubkeys, AbstainResult):
        return account_pubkeys
    return message, account_pubkeys


def _compiled_instruction(
    raw_instruction: object,
    *,
    observation: RawChainObservation,
    account_pubkeys: tuple[str, ...],
    outer_index: int,
) -> CompiledPumpMigrationInstruction | AbstainResult | None:
    if not isinstance(raw_instruction, Mapping):
        return _abstain(
            AbstainReason.UNKNOWN_PROTOCOL_STATE,
            "Pump migration transaction instruction is malformed",
            observation.slot,
        )
    program_id_index = raw_instruction.get("programIdIndex")
    if type(program_id_index) is not int or not 0 <= program_id_index < len(
        account_pubkeys
    ):
        return _abstain(
            AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
            "Pump migration program index is malformed",
            observation.slot,
        )
    if account_pubkeys[program_id_index] != PUMP_PROGRAM_ID:
        return None

    encoded_data = raw_instruction.get("data")
    if type(encoded_data) is not str:
        return _abstain(
            AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
            "Pump migration instruction data is malformed",
            observation.slot,
        )
    try:
        data = base58.b58decode(encoded_data)
    except ValueError:
        return _abstain(
            AbstainReason.UNKNOWN_PROTOCOL_STATE,
            "Pump migration instruction data is not base58",
            observation.slot,
        )
    if data[: len(MIGRATE_DISCRIMINATOR)] != MIGRATE_DISCRIMINATOR:
        return None

    raw_accounts = raw_instruction.get("accounts")
    if not isinstance(raw_accounts, list) or any(
        type(index) is not int for index in raw_accounts
    ):
        return _abstain(
            AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
            "Pump migration account indices are malformed",
            observation.slot,
        )
    account_indices = tuple(raw_accounts)
    if any(index < 0 or index >= len(account_pubkeys) for index in account_indices):
        return _abstain(
            AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
            "Pump migration account index is out of bounds",
            observation.slot,
        )
    role_proofs = tuple(
        AccountRoleProof(
            name=name,
            pubkey=account_pubkeys[account_indices[position]],
        )
        for position, name in enumerate(MIGRATE_ACCOUNT_NAMES)
        if position < len(account_indices)
    )
    return CompiledPumpMigrationInstruction(
        as_of_slot=Slot(observation.slot),
        program_id=PUMP_PROGRAM_ID,
        program_id_index=program_id_index,
        account_indices=account_indices,
        account_pubkeys=account_pubkeys,
        account_role_proofs=role_proofs,
        data=data,
        transaction_index=observation.transaction_index,
        outer_instruction_index=outer_index,
        signature=observation.signature,
    )


def _account_pubkeys(
    message: Mapping[str, object],
    meta: Mapping[str, object],
    as_of_slot: int,
) -> tuple[str, ...] | AbstainResult:
    static = message.get("accountKeys")
    if not isinstance(static, list) or any(type(item) is not str for item in static):
        return _abstain(
            AbstainReason.UNKNOWN_PROTOCOL_STATE,
            "Pump migration transaction account keys are malformed",
            as_of_slot,
        )
    loaded = meta.get("loadedAddresses")
    if loaded is None:
        return tuple(static)
    if not isinstance(loaded, Mapping):
        return _abstain(
            AbstainReason.UNKNOWN_PROTOCOL_STATE,
            "Pump migration loaded transaction addresses are malformed",
            as_of_slot,
        )
    if "readonly" in loaded and "readOnly" in loaded:
        return _abstain(
            AbstainReason.UNKNOWN_PROTOCOL_STATE,
            "Pump migration loaded address casing is ambiguous",
            as_of_slot,
        )
    writable = loaded.get("writable", ())
    readonly = loaded.get("readonly", loaded.get("readOnly", ()))
    if (
        not isinstance(writable, list)
        or not isinstance(readonly, list)
        or any(type(item) is not str for item in (*writable, *readonly))
    ):
        return _abstain(
            AbstainReason.UNKNOWN_PROTOCOL_STATE,
            "Pump migration loaded transaction addresses are malformed",
            as_of_slot,
        )
    return (*static, *writable, *readonly)


def _strict_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateJsonKeyError
        result[key] = value
    return result


def _abstain(
    reason: AbstainReason,
    message: str,
    as_of_slot: int,
) -> AbstainResult:
    return AbstainResult(reason=reason, message=message, as_of_slot=as_of_slot)


__all__ = [
    "PumpMigrationObservationResult",
    "decode_pump_migration_observation",
]
