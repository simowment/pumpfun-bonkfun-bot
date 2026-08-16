"""Pump migration evidence domain objects."""

from dataclasses import dataclass

from rugbot.domain.amounts import Slot


@dataclass(frozen=True, slots=True)
class PumpMigrationInstructionEvidence:
    """Verified Pump migration instruction evidence before pool publication.

    This object proves only that a finalized instruction matched the pinned
    Pump migration layout and supplied role/pubkey evidence. It is not a
    canonical PumpSwap pool artifact unless `is_canonical_pool_verified` is
    true and no required provenance evidence is missing.
    """

    as_of_slot: Slot
    program_id: str
    program_id_index: int
    signature: bytes | None
    instruction_name: str
    account_indices: tuple[int, ...]
    account_pubkeys: tuple[str, ...]
    account_role_proofs: tuple[tuple[str, str], ...]
    transaction_index: int | None
    outer_instruction_index: int
    inner_instruction_group_index: int | None
    inner_instruction_index: int | None
    mint_account_index: int
    bonding_curve_account_index: int
    pool_account_index: int
    pool_authority_account_index: int
    pool_base_token_account_index: int
    pool_quote_token_account_index: int
    pump_amm_account_index: int
    wsol_mint_account_index: int
    token_program_account_index: int
    token_2022_program_account_index: int
    associated_token_program_account_index: int
    base_mint_pubkey: str
    quote_mint_pubkey: str
    pool_pubkey: str
    pool_authority_pubkey: str
    pump_amm_program_id: str
    is_canonical_pool_verified: bool
    missing_evidence: tuple[str, ...]
    decoder_version: str
    pump_idl_hash: str
    pump_swap_idl_hash: str
