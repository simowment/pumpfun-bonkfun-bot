"""Pure Pump migration instruction verifier for pinned IDL evidence."""

from dataclasses import dataclass

from rugbot.domain.account_roles import AccountRoleProof
from rugbot.domain.amounts import Slot
from rugbot.domain.decisions import AbstainReason, AbstainResult
from rugbot.domain.migrations import PumpMigrationInstructionEvidence

PUMP_PROGRAM_ID = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
PUMP_AMM_PROGRAM_ID = "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA"
SYSTEM_PROGRAM_ID = "11111111111111111111111111111111"
SPL_PROGRAM_ID = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
SPL_2022_PROGRAM_ID = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"
ASSOCIATED_SPL_PROGRAM_ID = "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL"
WSOL_MINT_ID = "So11111111111111111111111111111111111111112"
PINNED_PUMP_IDL_SHA256 = (
    "b90bc471327f671449271d5d1d42354d1fae6f5a06502f5834459a3108138e49"
)
PINNED_PUMP_SWAP_IDL_SHA256 = (
    "da268f6f26a1e89fa83ec47f1db7dbff8ce16f96564a683fad00353e1bf19443"
)
PUMP_MIGRATION_DECODER_VERSION = "pump-migration-instruction-v1"
MIGRATE_DISCRIMINATOR = bytes([155, 234, 231, 146, 236, 158, 162, 30])
DISCRIMINATOR_SIZE = 8

MIGRATE_ACCOUNT_NAMES = (
    "global",
    "withdraw_authority",
    "mint",
    "bonding_curve",
    "associated_bonding_curve",
    "user",
    "system_program",
    "token_program",
    "pump_amm",
    "pool",
    "pool_authority",
    "pool_authority_mint_account",
    "pool_authority_wsol_account",
    "amm_global_config",
    "wsol_mint",
    "lp_mint",
    "user_pool_token_account",
    "pool_base_token_account",
    "pool_quote_token_account",
    "token_2022_program",
    "associated_token_program",
    "pump_amm_event_authority",
    "event_authority",
    "program",
)
FIXED_ACCOUNT_PUBKEYS = {
    "system_program": SYSTEM_PROGRAM_ID,
    "token_program": SPL_PROGRAM_ID,
    "pump_amm": PUMP_AMM_PROGRAM_ID,
    "wsol_mint": WSOL_MINT_ID,
    "token_2022_program": SPL_2022_PROGRAM_ID,
    "associated_token_program": ASSOCIATED_SPL_PROGRAM_ID,
    "program": PUMP_PROGRAM_ID,
}
PROVENANCE_EVIDENCE_NAMES = (
    "canonical_pool_artifact",
    "transaction_slot_account_state_artifact",
    "migration_pda_derivation_artifact",
    "pump_swap_pool_config_artifact",
    "mint_pair_artifact",
)


@dataclass(frozen=True, slots=True)
class CompiledPumpMigrationInstruction:
    """Protocol-neutral compiled migration instruction envelope."""

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
    inner_instruction_group_index: int | None = None
    inner_instruction_index: int | None = None


MigrationVerificationResult = PumpMigrationInstructionEvidence | AbstainResult


def verify_pump_migration_instruction(
    instruction: CompiledPumpMigrationInstruction,
    *,
    pump_idl_hash: str,
    pump_swap_idl_hash: str,
    decoder_version: str = PUMP_MIGRATION_DECODER_VERSION,
) -> MigrationVerificationResult:
    """Verify migration instruction evidence without publishing a pool.

    Args:
        instruction: Compiled instruction from finalized transaction evidence.
        pump_idl_hash: SHA-256 of the Pump IDL used to authorize the verifier.
        pump_swap_idl_hash: SHA-256 of the PumpSwap IDL used for AMM scope.
        decoder_version: Version of this verifier.

    Returns:
        PumpMigrationInstructionEvidence or AbstainResult. This function is pure
        and does not derive PDAs, call RPC, query databases, or fetch metadata.
    """

    context_error = _validate_context(
        instruction=instruction,
        pump_idl_hash=pump_idl_hash,
        pump_swap_idl_hash=pump_swap_idl_hash,
        decoder_version=decoder_version,
    )
    if context_error is not None:
        return context_error

    layout_error = _validate_layout(instruction)
    if layout_error is not None:
        return layout_error

    fixed_account_error = _validate_fixed_account_pubkeys(instruction)
    if fixed_account_error is not None:
        return fixed_account_error

    role_proof_error = _validate_account_role_proofs(instruction)
    if role_proof_error is not None:
        return role_proof_error

    return _build_migration_evidence(
        instruction=instruction,
        pump_idl_hash=pump_idl_hash,
        pump_swap_idl_hash=pump_swap_idl_hash,
        decoder_version=decoder_version,
    )


def _validate_context(
    *,
    instruction: CompiledPumpMigrationInstruction,
    pump_idl_hash: str,
    pump_swap_idl_hash: str,
    decoder_version: str,
) -> AbstainResult | None:
    failure = _first_context_failure(
        instruction=instruction,
        pump_idl_hash=pump_idl_hash,
        pump_swap_idl_hash=pump_swap_idl_hash,
        decoder_version=decoder_version,
    )
    if failure is None:
        return None
    reason, message = failure
    return _abstain(reason=reason, message=message, as_of_slot=instruction.as_of_slot)


def _first_context_failure(
    *,
    instruction: CompiledPumpMigrationInstruction,
    pump_idl_hash: str,
    pump_swap_idl_hash: str,
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
            pump_idl_hash != PINNED_PUMP_IDL_SHA256,
            AbstainReason.DECODER_MISMATCH,
            "Pump IDL hash does not match the pinned verifier",
        ),
        (
            pump_swap_idl_hash != PINNED_PUMP_SWAP_IDL_SHA256,
            AbstainReason.DECODER_MISMATCH,
            "PumpSwap IDL hash does not match the pinned verifier",
        ),
        (
            decoder_version != PUMP_MIGRATION_DECODER_VERSION,
            AbstainReason.DECODER_MISMATCH,
            "decoder_version does not match the pinned migration verifier",
        ),
        (
            instruction.data != MIGRATE_DISCRIMINATOR,
            AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
            "instruction data is not the pinned migrate discriminator",
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
            "resolved account_pubkeys are required to prove migration layout",
        ),
        (
            instruction.program_id_index is None,
            AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
            "program_id_index is required to prove migration layout",
        ),
    )
    for failed, reason, message in checks:
        if failed:
            return reason, message
    return _account_key_failure(instruction)


def _account_key_failure(
    instruction: CompiledPumpMigrationInstruction,
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
    instruction: CompiledPumpMigrationInstruction,
) -> AbstainResult | None:
    if len(instruction.account_indices) != len(MIGRATE_ACCOUNT_NAMES):
        return _abstain(
            reason=AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
            message="migrate account count does not match pinned IDL",
            as_of_slot=instruction.as_of_slot,
        )
    return None


def _validate_fixed_account_pubkeys(
    instruction: CompiledPumpMigrationInstruction,
) -> AbstainResult | None:
    account_pubkeys = instruction.account_pubkeys
    if account_pubkeys is None:
        return _abstain(
            reason=AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
            message="resolved account_pubkeys are required to prove migration layout",
            as_of_slot=instruction.as_of_slot,
        )
    for account_name, expected_pubkey in FIXED_ACCOUNT_PUBKEYS.items():
        compiled_index = _account_index(instruction, account_name)
        if account_pubkeys[compiled_index] != expected_pubkey:
            return _abstain(
                reason=AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
                message=f"migrate {account_name} account does not match IDL",
                as_of_slot=instruction.as_of_slot,
            )
    return None


def _validate_account_role_proofs(
    instruction: CompiledPumpMigrationInstruction,
) -> AbstainResult | None:
    account_pubkeys = instruction.account_pubkeys
    if account_pubkeys is None:
        return _abstain(
            reason=AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
            message="resolved account_pubkeys are required to prove migration layout",
            as_of_slot=instruction.as_of_slot,
        )

    proof_by_name: dict[str, str] = {}
    for proof in instruction.account_role_proofs:
        if proof.name in proof_by_name:
            return _abstain(
                reason=AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
                message="duplicate migration account role proof",
                as_of_slot=instruction.as_of_slot,
            )
        proof_by_name[proof.name] = proof.pubkey

    required_names = set(MIGRATE_ACCOUNT_NAMES)
    if set(proof_by_name) != required_names:
        return _abstain(
            reason=AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
            message="migrate account role proof set is incomplete",
            as_of_slot=instruction.as_of_slot,
        )

    for account_name in MIGRATE_ACCOUNT_NAMES:
        compiled_index = _account_index(instruction, account_name)
        if account_pubkeys[compiled_index] != proof_by_name[account_name]:
            return _abstain(
                reason=AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
                message="migrate account role proof mismatch",
                as_of_slot=instruction.as_of_slot,
            )
    return None


def _build_migration_evidence(
    *,
    instruction: CompiledPumpMigrationInstruction,
    pump_idl_hash: str,
    pump_swap_idl_hash: str,
    decoder_version: str,
) -> PumpMigrationInstructionEvidence:
    missing_evidence = _missing_provenance_evidence(instruction)
    account_pubkeys = instruction.account_pubkeys
    if account_pubkeys is None:
        raise AssertionError

    return PumpMigrationInstructionEvidence(
        as_of_slot=instruction.as_of_slot,
        program_id=instruction.program_id,
        program_id_index=int(instruction.program_id_index),
        signature=instruction.signature,
        instruction_name="migrate",
        account_indices=instruction.account_indices,
        account_pubkeys=account_pubkeys,
        account_role_proofs=tuple(
            (proof.name, proof.pubkey) for proof in instruction.account_role_proofs
        ),
        transaction_index=instruction.transaction_index,
        outer_instruction_index=instruction.outer_instruction_index,
        inner_instruction_group_index=instruction.inner_instruction_group_index,
        inner_instruction_index=instruction.inner_instruction_index,
        mint_account_index=_account_index(instruction, "mint"),
        bonding_curve_account_index=_account_index(instruction, "bonding_curve"),
        pool_account_index=_account_index(instruction, "pool"),
        pool_authority_account_index=_account_index(instruction, "pool_authority"),
        pool_base_token_account_index=_account_index(
            instruction, "pool_base_token_account"
        ),
        pool_quote_token_account_index=_account_index(
            instruction, "pool_quote_token_account"
        ),
        pump_amm_account_index=_account_index(instruction, "pump_amm"),
        wsol_mint_account_index=_account_index(instruction, "wsol_mint"),
        token_program_account_index=_account_index(instruction, "token_program"),
        token_2022_program_account_index=_account_index(
            instruction, "token_2022_program"
        ),
        associated_token_program_account_index=_account_index(
            instruction, "associated_token_program"
        ),
        base_mint_pubkey=account_pubkeys[_account_index(instruction, "mint")],
        quote_mint_pubkey=account_pubkeys[_account_index(instruction, "wsol_mint")],
        pool_pubkey=account_pubkeys[_account_index(instruction, "pool")],
        pool_authority_pubkey=account_pubkeys[
            _account_index(instruction, "pool_authority")
        ],
        pump_amm_program_id=account_pubkeys[_account_index(instruction, "pump_amm")],
        is_canonical_pool_verified=False,
        missing_evidence=missing_evidence,
        decoder_version=decoder_version,
        pump_idl_hash=pump_idl_hash,
        pump_swap_idl_hash=pump_swap_idl_hash,
    )


def _missing_provenance_evidence(
    instruction: CompiledPumpMigrationInstruction,
) -> tuple[str, ...]:
    del instruction
    return PROVENANCE_EVIDENCE_NAMES


def _account_index(
    instruction: CompiledPumpMigrationInstruction,
    account_name: str,
) -> int:
    position = MIGRATE_ACCOUNT_NAMES.index(account_name)
    return instruction.account_indices[position]


def _abstain(
    *,
    reason: AbstainReason,
    message: str,
    as_of_slot: Slot,
) -> AbstainResult:
    return AbstainResult(
        reason=reason,
        message=message,
        as_of_slot=int(as_of_slot),
    )
