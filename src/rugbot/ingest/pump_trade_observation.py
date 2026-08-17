"""Decode pinned Pump trade instructions from immutable RPC observations."""

# Explicit parsing branches fail closed at the first unproven field.
# ruff: noqa: PLR0911

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence

import base58

from rugbot.domain.account_roles import AccountRoleProof
from rugbot.domain.decisions import AbstainReason, AbstainResult
from rugbot.domain.observations import RawChainObservation
from rugbot.domain.trades import PumpTradeInstructionEvidence
from rugbot.protocol.pump.trade_decoder import (
    BUY_ACCOUNT_NAMES,
    BUY_DISCRIMINATOR,
    BUY_V2_ACCOUNT_NAMES,
    BUY_V2_DISCRIMINATOR,
    PINNED_PUMP_IDL_SHA256,
    PUMP_PROGRAM_ID,
    SELL_ACCOUNT_NAMES,
    SELL_DISCRIMINATOR,
    SELL_V2_ACCOUNT_NAMES,
    SELL_V2_DISCRIMINATOR,
    CompiledPumpInstruction,
    decode_pump_trade_instruction,
)

PumpTradeObservationResult = tuple[PumpTradeInstructionEvidence, ...] | AbstainResult

# V2 layouts are imported from the pure decoder so ingestion and replay use
# exactly the same pinned account contract.


def decode_pump_trade_observation(
    observation: RawChainObservation,
) -> PumpTradeObservationResult:
    """Decode all supported outer Pump trades in one finalized observation.

    The account table is resolved from the exact transaction payload,
    including versioned ``loadedAddresses``.  Unsupported instructions are
    ignored; malformed evidence for a claimed Pump trade abstains.
    Inner instructions are not labeled until their invocation context can be
    proven without ambiguity.
    """

    validation = _validate_observation(observation)
    if validation is not None:
        return validation
    transaction = _load_transaction(observation)
    if isinstance(transaction, AbstainResult):
        return transaction
    message, account_pubkeys = transaction
    raw_instructions = message.get("instructions")
    if not isinstance(raw_instructions, list):
        return _abstain(
            AbstainReason.MISSING_FEATURE,
            "transaction instructions are missing",
            observation.slot,
        )

    decoded: list[PumpTradeInstructionEvidence] = []
    for outer_index, raw_instruction in enumerate(raw_instructions):
        compiled = _compiled_instruction(
            raw_instruction,
            observation=observation,
            account_pubkeys=account_pubkeys,
            outer_index=outer_index,
        )
        if isinstance(compiled, AbstainResult):
            return compiled
        if compiled is None:
            continue
        result = decode_pump_trade_instruction(
            compiled,
            idl_hash=PINNED_PUMP_IDL_SHA256,
        )
        if isinstance(result, AbstainResult):
            return result
        decoded.append(result)
    return tuple(decoded)


def _validate_observation(
    observation: object,
) -> AbstainResult | None:
    if type(observation) is not RawChainObservation:
        return _abstain(
            AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
            "Pump trade observation is malformed",
            -1,
        )
    if (
        observation.commitment != "finalized"
        or observation.canonical_status != "canonical"
        or observation.source_update_kind != "transaction"
        or not isinstance(observation.raw_source_payload, bytes)
        or observation.signature is None
        or observation.transaction_index is None
    ):
        return _abstain(
            AbstainReason.STALE_STATE,
            "Pump trade decoder requires finalized transaction evidence",
            observation.slot,
        )
    return None


def _load_transaction(
    observation: RawChainObservation,
) -> tuple[Mapping[str, object], tuple[str, ...]] | AbstainResult:
    try:
        envelope = json.loads(observation.raw_source_payload or b"")
    except (UnicodeDecodeError, json.JSONDecodeError):
        return _abstain(
            AbstainReason.UNKNOWN_PROTOCOL_STATE,
            "Pump trade observation contains invalid JSON",
            observation.slot,
        )
    if not isinstance(envelope, Mapping) or envelope.get("jsonrpc") != "2.0":
        return _abstain(
            AbstainReason.UNKNOWN_PROTOCOL_STATE,
            "Pump trade observation envelope is malformed",
            observation.slot,
        )
    result = envelope.get("result")
    if not isinstance(result, Mapping) or result.get("slot") != observation.slot:
        return _abstain(
            AbstainReason.STALE_STATE,
            "Pump trade observation slot does not match payload",
            observation.slot,
        )
    transaction = result.get("transaction")
    meta = result.get("meta")
    if not isinstance(transaction, Mapping) or not isinstance(meta, Mapping):
        return _abstain(
            AbstainReason.MISSING_FEATURE,
            "Pump trade transaction metadata is incomplete",
            observation.slot,
        )
    if meta.get("err") is not None:
        return _abstain(
            AbstainReason.UNKNOWN_PROTOCOL_STATE,
            "failed finalized transaction cannot produce Pump instructions",
            observation.slot,
        )
    message = transaction.get("message")
    if not isinstance(message, Mapping):
        return _abstain(
            AbstainReason.MISSING_FEATURE,
            "Pump trade transaction message is missing",
            observation.slot,
        )
    account_pubkeys = _account_pubkeys(message, meta, observation.slot)
    if isinstance(account_pubkeys, AbstainResult):
        return account_pubkeys
    signatures = transaction.get("signatures")
    expected = base58.b58encode(observation.signature or b"").decode("ascii")
    if not isinstance(signatures, list) or not signatures or signatures[0] != expected:
        return _abstain(
            AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
            "Pump trade transaction signature does not match observation",
            observation.slot,
        )
    return message, account_pubkeys


def _compiled_instruction(  # noqa: C901
    raw_instruction: object,
    *,
    observation: RawChainObservation,
    account_pubkeys: tuple[str, ...],
    outer_index: int,
) -> CompiledPumpInstruction | AbstainResult | None:
    if not isinstance(raw_instruction, Mapping):
        return _abstain(
            AbstainReason.UNKNOWN_PROTOCOL_STATE,
            "Pump transaction instruction is malformed",
            observation.slot,
        )
    program_id_index = raw_instruction.get("programIdIndex")
    accounts = raw_instruction.get("accounts")
    encoded_data = raw_instruction.get("data")
    if type(program_id_index) is not int or not 0 <= program_id_index < len(
        account_pubkeys
    ):
        return _abstain(
            AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
            "Pump instruction program index is malformed",
            observation.slot,
        )
    if account_pubkeys[program_id_index] != PUMP_PROGRAM_ID:
        return None
    if not isinstance(accounts, list) or any(
        type(item) is not int for item in accounts
    ):
        return _abstain(
            AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
            "Pump instruction account indices are malformed",
            observation.slot,
        )
    if any(item < 0 or item >= len(account_pubkeys) for item in accounts):
        return _abstain(
            AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
            "Pump instruction account index is out of bounds",
            observation.slot,
        )
    if not isinstance(encoded_data, str):
        return _abstain(
            AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
            "Pump instruction data is missing",
            observation.slot,
        )
    try:
        data = base58.b58decode(encoded_data)
    except ValueError:
        return _abstain(
            AbstainReason.UNKNOWN_PROTOCOL_STATE,
            "Pump instruction data is not base58",
            observation.slot,
        )
    discriminator = data[:8]
    if discriminator not in (
        BUY_DISCRIMINATOR,
        SELL_DISCRIMINATOR,
        BUY_V2_DISCRIMINATOR,
        SELL_V2_DISCRIMINATOR,
    ):
        return None
    names = _account_names(discriminator)
    if discriminator in (BUY_V2_DISCRIMINATOR, SELL_V2_DISCRIMINATOR):
        if len(accounts) != len(names):
            return _abstain(
                AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
                "Pump v2 trade instruction account layout is not exact",
                observation.slot,
            )
    elif len(accounts) < len(names):
        return _abstain(
            AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
            "Pump trade instruction account layout is incomplete",
            observation.slot,
        )
    role_proofs = tuple(
        AccountRoleProof(name=name, pubkey=account_pubkeys[accounts[index]])
        for index, name in enumerate(names)
    )
    return CompiledPumpInstruction(
        as_of_slot=observation.slot,
        program_id=PUMP_PROGRAM_ID,
        account_indices=tuple(accounts),
        data=data,
        transaction_index=observation.transaction_index,
        outer_instruction_index=outer_index,
        program_id_index=program_id_index,
        account_pubkeys=account_pubkeys,
        account_role_proofs=role_proofs,
        signature=observation.signature,
        transaction_slot_account_state_available=False,
    )


def _account_names(discriminator: bytes) -> tuple[str, ...]:
    if discriminator == BUY_DISCRIMINATOR:
        return BUY_ACCOUNT_NAMES
    if discriminator == SELL_DISCRIMINATOR:
        return SELL_ACCOUNT_NAMES
    if discriminator == BUY_V2_DISCRIMINATOR:
        return BUY_V2_ACCOUNT_NAMES
    return SELL_V2_ACCOUNT_NAMES


def _account_pubkeys(
    message: Mapping[str, object],
    meta: Mapping[str, object],
    as_of_slot: int,
) -> tuple[str, ...] | AbstainResult:
    static = message.get("accountKeys")
    if not isinstance(static, list) or any(
        not isinstance(item, str) for item in static
    ):
        return _abstain(
            AbstainReason.UNKNOWN_PROTOCOL_STATE,
            "Pump transaction account keys are malformed",
            as_of_slot,
        )
    loaded = meta.get("loadedAddresses")
    if loaded is None:
        return tuple(static)
    if not isinstance(loaded, Mapping):
        return _abstain(
            AbstainReason.UNKNOWN_PROTOCOL_STATE,
            "Pump loaded transaction addresses are malformed",
            as_of_slot,
        )
    writable = loaded.get("writable", ())
    readonly = loaded.get("readonly", loaded.get("readOnly", ()))
    if (
        not _sequence(writable)
        or not _sequence(readonly)
        or any(not isinstance(item, str) for item in (*writable, *readonly))
    ):
        return _abstain(
            AbstainReason.UNKNOWN_PROTOCOL_STATE,
            "Pump loaded transaction address lists are malformed",
            as_of_slot,
        )
    return (*static, *writable, *readonly)


def _sequence(value: object) -> bool:
    return isinstance(value, Sequence) and not isinstance(
        value,
        (str, bytes, bytearray),
    )


def _abstain(
    reason: AbstainReason,
    message: str,
    as_of_slot: int,
) -> AbstainResult:
    return AbstainResult(reason=reason, message=message, as_of_slot=as_of_slot)


__all__ = ["PumpTradeObservationResult", "decode_pump_trade_observation"]
