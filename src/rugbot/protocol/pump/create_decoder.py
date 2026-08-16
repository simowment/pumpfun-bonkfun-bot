"""Pure Pump create_v2 launch decoder for pinned IDL evidence."""

from dataclasses import dataclass
from struct import unpack_from

import base58

from rugbot.domain.account_roles import AccountRoleProof
from rugbot.domain.amounts import Slot
from rugbot.domain.decisions import AbstainReason, AbstainResult
from rugbot.domain.launches import (
    LaunchActorProof,
    LaunchActorRole,
    LaunchCreatedV2,
)

PUMP_PROGRAM_ID = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
SYSTEM_PROGRAM_ID = "11111111111111111111111111111111"
SPL_2022_PROGRAM_ID = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"
ASSOCIATED_SPL_PROGRAM_ID = "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL"
MAYHEM_PROGRAM_ID = "MAyhSmzXzV1pTf7LsNkrNwkWKTo4ougAJ1PPg47MD4e"
WSOL_MINT_ID = "So11111111111111111111111111111111111111112"
PINNED_PUMP_IDL_SHA256 = (
    "662f9afea2feb1a4318852b65d4c1f642f1fdae8d3c9228478efd01d42dfa41d"
)
PUMP_CREATE_V2_DECODER_VERSION = "pump-create-v2-instruction-v1"
CREATE_V2_DISCRIMINATOR = bytes([214, 144, 76, 236, 95, 139, 49, 180])
DISCRIMINATOR_SIZE = 8
U32_SIZE = 4
PUBKEY_SIZE = 32
BOOL_SIZE = 1

CREATE_V2_ACCOUNT_NAMES = (
    "mint",
    "mint_authority",
    "bonding_curve",
    "associated_bonding_curve",
    "global",
    "user",
    "system_program",
    "token_program",
    "associated_token_program",
    "mayhem_program_id",
    "global_params",
    "sol_vault",
    "mayhem_state",
    "mayhem_token_vault",
    "event_authority",
    "program",
)
FIXED_ACCOUNT_PUBKEYS = {
    "system_program": SYSTEM_PROGRAM_ID,
    "token_program": SPL_2022_PROGRAM_ID,
    "associated_token_program": ASSOCIATED_SPL_PROGRAM_ID,
    "mayhem_program_id": MAYHEM_PROGRAM_ID,
    "program": PUMP_PROGRAM_ID,
}


@dataclass(frozen=True, slots=True)
class CompiledPumpCreateV2Instruction:
    """Protocol-neutral compiled create_v2 instruction envelope."""

    as_of_slot: Slot
    program_id: str
    program_id_index: int | None
    account_indices: tuple[int, ...]
    account_pubkeys: tuple[str, ...] | None
    account_role_proofs: tuple[AccountRoleProof, ...]
    data: bytes
    transaction_index: int | None
    outer_instruction_index: int
    signature: bytes | None = None
    actor_role_proofs: tuple[LaunchActorProof, ...] = ()
    transaction_slot_account_state_available: bool = False
    inner_instruction_group_index: int | None = None
    inner_instruction_index: int | None = None


@dataclass(frozen=True, slots=True)
class _CreateV2Args:
    name: str
    symbol: str
    uri: str
    creator_pubkey: str
    is_mayhem_mode: bool
    is_cashback_enabled: bool


CreateV2DecodeResult = LaunchCreatedV2 | AbstainResult


def decode_pump_create_v2_instruction(
    instruction: CompiledPumpCreateV2Instruction,
    *,
    idl_hash: str,
    decoder_version: str = PUMP_CREATE_V2_DECODER_VERSION,
) -> CreateV2DecodeResult:
    """Decode Pump create_v2 launch evidence from a pinned IDL layout.

    Args:
        instruction: Compiled instruction envelope from finalized transaction
            evidence.
        idl_hash: SHA-256 of the Pump IDL used to authorize the decoder.
        decoder_version: Version of this decoder.

    Returns:
        LaunchCreatedV2 on supported create_v2 layouts, otherwise AbstainResult.
        This function is pure and performs no RPC, database, metadata, signer, or
        trading access.
    """

    context_error = _validate_context(
        instruction=instruction,
        idl_hash=idl_hash,
        decoder_version=decoder_version,
    )
    if context_error is not None:
        return context_error

    for validation in (
        _validate_layout,
        _validate_fixed_account_pubkeys,
        _validate_account_role_proofs,
        _validate_actor_role_proofs,
    ):
        validation_error = validation(instruction)
        if validation_error is not None:
            return validation_error

    decoded_args = _decode_args(instruction)
    if isinstance(decoded_args, AbstainResult):
        return decoded_args

    return _build_launch(
        instruction=instruction,
        args=decoded_args,
        idl_hash=idl_hash,
        decoder_version=decoder_version,
    )


def _validate_context(
    *,
    instruction: CompiledPumpCreateV2Instruction,
    idl_hash: str,
    decoder_version: str,
) -> AbstainResult | None:
    failure = _first_context_failure(
        instruction=instruction,
        idl_hash=idl_hash,
        decoder_version=decoder_version,
    )
    if failure is None:
        return None
    reason, message = failure
    return _abstain(reason=reason, message=message, as_of_slot=instruction.as_of_slot)


def _first_context_failure(
    *,
    instruction: CompiledPumpCreateV2Instruction,
    idl_hash: str,
    decoder_version: str,
) -> tuple[AbstainReason, str] | None:
    checks = (
        (
            type(instruction.as_of_slot) is not int or instruction.as_of_slot < 0,
            AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
            "as_of_slot must be a non-negative integer",
        ),
        (
            instruction.program_id != PUMP_PROGRAM_ID,
            AbstainReason.UNKNOWN_PROTOCOL_STATE,
            "instruction program_id is not the pinned Pump program",
        ),
        (
            idl_hash != PINNED_PUMP_IDL_SHA256,
            AbstainReason.DECODER_MISMATCH,
            "Pump IDL hash does not match the pinned create_v2 decoder",
        ),
        (
            decoder_version != PUMP_CREATE_V2_DECODER_VERSION,
            AbstainReason.DECODER_MISMATCH,
            "decoder_version does not match the pinned create_v2 decoder",
        ),
        (
            len(instruction.data) < DISCRIMINATOR_SIZE,
            AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
            "instruction data is shorter than discriminator",
        ),
        (
            instruction.data[:DISCRIMINATOR_SIZE] != CREATE_V2_DISCRIMINATOR,
            AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
            "instruction data is not the pinned create_v2 discriminator",
        ),
        (
            instruction.outer_instruction_index < 0,
            AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
            "outer_instruction_index must be non-negative",
        ),
        (
            any(index < 0 for index in instruction.account_indices),
            AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
            "account indices must be non-negative",
        ),
        (
            instruction.account_pubkeys is None,
            AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
            "resolved account_pubkeys are required to prove create_v2 layout",
        ),
        (
            instruction.program_id_index is None,
            AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
            "program_id_index is required to prove create_v2 layout",
        ),
    )
    for failed, reason, message in checks:
        if failed:
            return reason, message
    return _account_key_failure(instruction)


def _account_key_failure(
    instruction: CompiledPumpCreateV2Instruction,
) -> tuple[AbstainReason, str] | None:
    account_pubkeys = instruction.account_pubkeys
    if account_pubkeys is None:
        return None
    if any(index >= len(account_pubkeys) for index in instruction.account_indices):
        return (
            AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
            "account index is outside supplied account_pubkeys",
        )
    program_id_index = instruction.program_id_index
    if program_id_index is None:
        return None
    if program_id_index < 0 or program_id_index >= len(account_pubkeys):
        return (
            AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
            "program_id_index is outside supplied account_pubkeys",
        )
    if account_pubkeys[program_id_index] != instruction.program_id:
        return (
            AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
            "program_id_index does not resolve to instruction program_id",
        )
    return None


def _validate_layout(
    instruction: CompiledPumpCreateV2Instruction,
) -> AbstainResult | None:
    if len(instruction.account_indices) != len(CREATE_V2_ACCOUNT_NAMES):
        return _abstain(
            reason=AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
            message="create_v2 account count does not match pinned IDL",
            as_of_slot=instruction.as_of_slot,
        )
    return None


def _validate_fixed_account_pubkeys(
    instruction: CompiledPumpCreateV2Instruction,
) -> AbstainResult | None:
    account_pubkeys = instruction.account_pubkeys
    if account_pubkeys is None:
        return _abstain(
            reason=AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
            message="resolved account_pubkeys are required to prove create_v2 layout",
            as_of_slot=instruction.as_of_slot,
        )
    for account_name, expected_pubkey in FIXED_ACCOUNT_PUBKEYS.items():
        compiled_index = _account_index(instruction, account_name)
        if account_pubkeys[compiled_index] != expected_pubkey:
            return _abstain(
                reason=AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
                message=f"create_v2 {account_name} account does not match IDL",
                as_of_slot=instruction.as_of_slot,
            )
    return None


def _validate_account_role_proofs(
    instruction: CompiledPumpCreateV2Instruction,
) -> AbstainResult | None:
    account_pubkeys = instruction.account_pubkeys
    if account_pubkeys is None:
        return _abstain(
            reason=AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
            message="resolved account_pubkeys are required to prove create_v2 layout",
            as_of_slot=instruction.as_of_slot,
        )

    proof_by_name: dict[str, str] = {}
    for proof in instruction.account_role_proofs:
        if proof.name in proof_by_name:
            return _abstain(
                reason=AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
                message="duplicate create_v2 account role proof",
                as_of_slot=instruction.as_of_slot,
            )
        proof_by_name[proof.name] = proof.pubkey

    required_names = set(CREATE_V2_ACCOUNT_NAMES)
    if set(proof_by_name) != required_names:
        return _abstain(
            reason=AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
            message="create_v2 account role proof set is incomplete",
            as_of_slot=instruction.as_of_slot,
        )

    for account_name in CREATE_V2_ACCOUNT_NAMES:
        compiled_index = _account_index(instruction, account_name)
        if account_pubkeys[compiled_index] != proof_by_name[account_name]:
            return _abstain(
                reason=AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
                message="create_v2 account role proof mismatch",
                as_of_slot=instruction.as_of_slot,
            )
    return None


def _validate_actor_role_proofs(
    instruction: CompiledPumpCreateV2Instruction,
) -> AbstainResult | None:
    account_pubkeys = instruction.account_pubkeys
    if account_pubkeys is None:
        return _abstain(
            reason=AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
            message="resolved account_pubkeys are required to prove actor roles",
            as_of_slot=instruction.as_of_slot,
        )

    seen_roles: set[LaunchActorRole] = set()
    for proof in instruction.actor_role_proofs:
        proof_error = _validate_actor_role_proof(
            proof=proof,
            account_pubkeys=account_pubkeys,
            instruction=instruction,
        )
        if proof_error is not None:
            return proof_error
        if proof.role in seen_roles:
            return _abstain(
                reason=AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
                message="duplicate launch actor role proof",
                as_of_slot=instruction.as_of_slot,
            )
        seen_roles.add(proof.role)
    return None


def _validate_actor_role_proof(
    *,
    proof: LaunchActorProof,
    account_pubkeys: tuple[str, ...],
    instruction: CompiledPumpCreateV2Instruction,
) -> AbstainResult | None:
    identity_error = _validate_actor_proof_identity(proof, instruction)
    if identity_error is not None:
        return identity_error
    account_error = _validate_actor_proof_account(
        proof=proof,
        account_pubkeys=account_pubkeys,
        instruction=instruction,
    )
    if account_error is not None:
        return account_error
    return _validate_actor_proof_provenance(proof, instruction)


def _validate_actor_proof_identity(
    proof: LaunchActorProof,
    instruction: CompiledPumpCreateV2Instruction,
) -> AbstainResult | None:
    if not isinstance(proof, LaunchActorProof):
        return _abstain(
            reason=AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
            message="launch actor proof type is unsupported",
            as_of_slot=instruction.as_of_slot,
        )
    if proof.as_of_slot != instruction.as_of_slot:
        return _abstain(
            reason=AbstainReason.STALE_STATE,
            message="launch actor proof uses a different as_of_slot",
            as_of_slot=instruction.as_of_slot,
        )
    if proof.role not in (LaunchActorRole.FEE_PAYER, LaunchActorRole.FIRST_BUYER):
        return _abstain(
            reason=AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
            message="launch actor proof role is unsupported",
            as_of_slot=instruction.as_of_slot,
        )
    return None


def _validate_actor_proof_account(
    *,
    proof: LaunchActorProof,
    account_pubkeys: tuple[str, ...],
    instruction: CompiledPumpCreateV2Instruction,
) -> AbstainResult | None:
    if not _non_negative_int(proof.account_index) or proof.account_index >= len(
        account_pubkeys
    ):
        return _abstain(
            reason=AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
            message=f"{proof.role.value} account index is outside account_pubkeys",
            as_of_slot=instruction.as_of_slot,
        )
    if account_pubkeys[proof.account_index] != proof.pubkey:
        return _abstain(
            reason=AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
            message=f"{proof.role.value} actor proof pubkey mismatch",
            as_of_slot=instruction.as_of_slot,
        )
    return None


def _validate_actor_proof_provenance(
    proof: LaunchActorProof,
    instruction: CompiledPumpCreateV2Instruction,
) -> AbstainResult | None:
    if not _valid_evidence_ids(proof.evidence_ids):
        return _abstain(
            reason=AbstainReason.MISSING_FEATURE,
            message=f"{proof.role.value} actor proof evidence_ids are required",
            as_of_slot=instruction.as_of_slot,
        )
    if not isinstance(proof.source_version, str) or not proof.source_version:
        return _abstain(
            reason=AbstainReason.DECODER_MISMATCH,
            message=f"{proof.role.value} actor proof source_version is required",
            as_of_slot=instruction.as_of_slot,
        )
    return None


def _decode_args(
    instruction: CompiledPumpCreateV2Instruction,
) -> _CreateV2Args | AbstainResult:
    offset = DISCRIMINATOR_SIZE
    decoded_strings = _decode_create_v2_strings(instruction, offset)
    if isinstance(decoded_strings, AbstainResult):
        return decoded_strings
    name, symbol, uri, offset = decoded_strings

    decoded_tail = _decode_create_v2_tail(instruction, offset)
    if isinstance(decoded_tail, AbstainResult):
        return decoded_tail
    creator_pubkey, is_mayhem_mode, is_cashback_enabled = decoded_tail

    return _CreateV2Args(
        name=name,
        symbol=symbol,
        uri=uri,
        creator_pubkey=creator_pubkey,
        is_mayhem_mode=is_mayhem_mode,
        is_cashback_enabled=is_cashback_enabled,
    )


def _decode_create_v2_strings(
    instruction: CompiledPumpCreateV2Instruction,
    offset: int,
) -> tuple[str, str, str, int] | AbstainResult:
    decoded_name = _decode_string(instruction.data, offset)
    if decoded_name is None:
        return _unsupported_arg(instruction, "name string is truncated or invalid")
    name, offset = decoded_name

    decoded_symbol = _decode_string(instruction.data, offset)
    if decoded_symbol is None:
        return _unsupported_arg(instruction, "symbol string is truncated or invalid")
    symbol, offset = decoded_symbol

    decoded_uri = _decode_string(instruction.data, offset)
    if decoded_uri is None:
        return _unsupported_arg(instruction, "uri string is truncated or invalid")
    uri, offset = decoded_uri

    return name, symbol, uri, offset


def _decode_create_v2_tail(
    instruction: CompiledPumpCreateV2Instruction,
    offset: int,
) -> tuple[str, bool, bool] | AbstainResult:
    if offset + PUBKEY_SIZE + BOOL_SIZE + BOOL_SIZE != len(instruction.data):
        return _unsupported_arg(
            instruction,
            "create_v2 argument length does not match pinned IDL",
        )

    creator_pubkey = _pubkey_to_base58(instruction.data[offset : offset + PUBKEY_SIZE])
    offset += PUBKEY_SIZE

    is_mayhem_mode = _decode_bool(instruction.data[offset])
    if is_mayhem_mode is None:
        return _unsupported_arg(instruction, "is_mayhem_mode bool is unsupported")
    offset += BOOL_SIZE

    is_cashback_enabled = _decode_bool(instruction.data[offset])
    if is_cashback_enabled is None:
        return _unsupported_arg(
            instruction,
            "is_cashback_enabled bool is unsupported",
        )

    return creator_pubkey, is_mayhem_mode, is_cashback_enabled


def _decode_string(data: bytes, offset: int) -> tuple[str, int] | None:
    if offset + U32_SIZE > len(data):
        return None
    string_length = int(unpack_from("<I", data, offset)[0])
    value_offset = offset + U32_SIZE
    next_offset = value_offset + string_length
    if next_offset > len(data):
        return None
    try:
        return data[value_offset:next_offset].decode("utf-8"), next_offset
    except UnicodeDecodeError:
        return None


def _decode_bool(value: int) -> bool | None:
    if value == 0:
        return False
    if value == 1:
        return True
    return None


def _pubkey_to_base58(value: bytes) -> str:
    return base58.b58encode(value).decode("ascii")


def _unsupported_arg(
    instruction: CompiledPumpCreateV2Instruction,
    message: str,
) -> AbstainResult:
    return _abstain(
        reason=AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
        message=message,
        as_of_slot=instruction.as_of_slot,
    )


def _build_launch(
    *,
    instruction: CompiledPumpCreateV2Instruction,
    args: _CreateV2Args,
    idl_hash: str,
    decoder_version: str,
) -> LaunchCreatedV2:
    account_pubkeys = instruction.account_pubkeys
    if account_pubkeys is None or instruction.program_id_index is None:
        raise AssertionError
    mint_index = _account_index(instruction, "mint")
    bonding_curve_index = _account_index(instruction, "bonding_curve")
    user_index = _account_index(instruction, "user")
    fee_payer_proof = _actor_proof(instruction, LaunchActorRole.FEE_PAYER)
    first_buyer_proof = _actor_proof(instruction, LaunchActorRole.FIRST_BUYER)

    return LaunchCreatedV2(
        as_of_slot=instruction.as_of_slot,
        launch_id=account_pubkeys[mint_index],
        program_id=instruction.program_id,
        program_id_index=instruction.program_id_index,
        signature=instruction.signature,
        instruction_name="create_v2",
        creation_instruction_type="create_v2",
        account_indices=instruction.account_indices,
        account_pubkeys=account_pubkeys,
        account_role_proofs=tuple(
            (proof.name, proof.pubkey) for proof in instruction.account_role_proofs
        ),
        actor_role_proofs=tuple(
            (
                proof.role.value,
                proof.account_index,
                proof.pubkey,
                proof.evidence_ids,
                proof.source_version,
            )
            for proof in instruction.actor_role_proofs
        ),
        required_account_names=CREATE_V2_ACCOUNT_NAMES,
        transaction_index=instruction.transaction_index,
        outer_instruction_index=instruction.outer_instruction_index,
        inner_instruction_group_index=instruction.inner_instruction_group_index,
        inner_instruction_index=instruction.inner_instruction_index,
        mint_account_index=mint_index,
        mint_pubkey=account_pubkeys[mint_index],
        mint_authority_account_index=_account_index(instruction, "mint_authority"),
        bonding_curve_account_index=bonding_curve_index,
        bonding_curve_pubkey=account_pubkeys[bonding_curve_index],
        associated_bonding_curve_account_index=_account_index(
            instruction,
            "associated_bonding_curve",
        ),
        global_account_index=_account_index(instruction, "global"),
        user_account_index=user_index,
        user_pubkey=account_pubkeys[user_index],
        creator_pubkey=args.creator_pubkey,
        fee_payer_account_index=_actor_account_index(fee_payer_proof),
        fee_payer_pubkey=_actor_pubkey(fee_payer_proof),
        first_buyer_account_index=_actor_account_index(first_buyer_proof),
        first_buyer_pubkey=_actor_pubkey(first_buyer_proof),
        system_program_account_index=_account_index(instruction, "system_program"),
        token_program_account_index=_account_index(instruction, "token_program"),
        base_token_program_pubkey=account_pubkeys[
            _account_index(instruction, "token_program")
        ],
        associated_token_program_account_index=_account_index(
            instruction,
            "associated_token_program",
        ),
        mayhem_program_account_index=_account_index(instruction, "mayhem_program_id"),
        global_params_account_index=_account_index(instruction, "global_params"),
        quote_vault_account_index=_account_index(instruction, "sol_vault"),
        quote_asset="SOL",
        quote_mint_pubkey=WSOL_MINT_ID,
        quote_token_program_pubkey=SYSTEM_PROGRAM_ID,
        mayhem_state_account_index=_account_index(instruction, "mayhem_state"),
        mayhem_token_vault_account_index=_account_index(
            instruction,
            "mayhem_token_vault",
        ),
        event_authority_account_index=_account_index(instruction, "event_authority"),
        name=args.name,
        symbol=args.symbol,
        uri=args.uri,
        is_mayhem_mode=args.is_mayhem_mode,
        is_cashback_enabled=args.is_cashback_enabled,
        transaction_slot_account_state_available=(
            instruction.transaction_slot_account_state_available
        ),
        missing_evidence=_missing_evidence(instruction),
        decoder_version=decoder_version,
        idl_hash=idl_hash,
    )


def _missing_evidence(
    instruction: CompiledPumpCreateV2Instruction,
) -> tuple[str, ...]:
    missing = []
    if _actor_proof(instruction, LaunchActorRole.FEE_PAYER) is None:
        missing.append("fee_payer")
    if _actor_proof(instruction, LaunchActorRole.FIRST_BUYER) is None:
        missing.append("first_buyer")
    if not instruction.transaction_slot_account_state_available:
        missing.append("transaction_slot_account_state")
    return tuple(missing)


def _actor_proof(
    instruction: CompiledPumpCreateV2Instruction,
    role: LaunchActorRole,
) -> LaunchActorProof | None:
    for proof in instruction.actor_role_proofs:
        if proof.role is role:
            return proof
    return None


def _actor_account_index(proof: LaunchActorProof | None) -> int | None:
    if proof is None:
        return None
    return proof.account_index


def _actor_pubkey(proof: LaunchActorProof | None) -> str | None:
    if proof is None:
        return None
    return proof.pubkey


def _account_index(
    instruction: CompiledPumpCreateV2Instruction,
    account_name: str,
) -> int:
    position = CREATE_V2_ACCOUNT_NAMES.index(account_name)
    return instruction.account_indices[position]


def _valid_evidence_ids(evidence_ids: object) -> bool:
    return (
        type(evidence_ids) is tuple
        and bool(evidence_ids)
        and all(
            isinstance(evidence_id, str) and evidence_id for evidence_id in evidence_ids
        )
    )


def _non_negative_int(value: object) -> bool:
    return type(value) is int and value >= 0


def _abstain(
    *,
    reason: AbstainReason,
    message: str,
    as_of_slot: Slot,
) -> AbstainResult:
    return AbstainResult(reason=reason, message=message, as_of_slot=int(as_of_slot))
