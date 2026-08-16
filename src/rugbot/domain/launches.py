"""Launch evidence domain objects."""

from dataclasses import dataclass
from enum import Enum

from rugbot.domain.amounts import Slot


class LaunchActorRole(Enum):
    """Actor roles observed around a launch but not all present in the IDL."""

    FEE_PAYER = "fee_payer"
    FIRST_BUYER = "first_buyer"


@dataclass(frozen=True, slots=True)
class LaunchActorProof:
    """Caller-supplied provenance for non-IDL launch actor roles."""

    as_of_slot: Slot
    role: LaunchActorRole
    account_index: int
    pubkey: str
    evidence_ids: tuple[str, ...]
    source_version: str


@dataclass(frozen=True, slots=True)
class LaunchCreatedV2:
    """Decoded Pump create_v2 launch evidence from a pinned instruction layout.

    This object labels only the accounts and arguments proven by finalized
    transaction evidence supplied to the decoder. It does not fetch metadata,
    write profiles, or infer that creator, submitter, fee payer, and first buyer
    are controlled by the same actor.
    """

    as_of_slot: Slot
    launch_id: str
    program_id: str
    program_id_index: int
    signature: bytes | None
    instruction_name: str
    creation_instruction_type: str
    account_indices: tuple[int, ...]
    account_pubkeys: tuple[str, ...]
    account_role_proofs: tuple[tuple[str, str], ...]
    actor_role_proofs: tuple[tuple[str, int, str, tuple[str, ...], str], ...]
    required_account_names: tuple[str, ...]
    transaction_index: int | None
    outer_instruction_index: int
    inner_instruction_group_index: int | None
    inner_instruction_index: int | None
    mint_account_index: int
    mint_pubkey: str
    mint_authority_account_index: int
    bonding_curve_account_index: int
    bonding_curve_pubkey: str
    associated_bonding_curve_account_index: int
    global_account_index: int
    user_account_index: int
    user_pubkey: str
    creator_pubkey: str
    fee_payer_account_index: int | None
    fee_payer_pubkey: str | None
    first_buyer_account_index: int | None
    first_buyer_pubkey: str | None
    system_program_account_index: int
    token_program_account_index: int
    base_token_program_pubkey: str
    associated_token_program_account_index: int
    mayhem_program_account_index: int
    global_params_account_index: int
    quote_vault_account_index: int
    quote_asset: str
    quote_mint_pubkey: str
    quote_token_program_pubkey: str
    mayhem_state_account_index: int
    mayhem_token_vault_account_index: int
    event_authority_account_index: int
    name: str
    symbol: str
    uri: str
    is_mayhem_mode: bool
    is_cashback_enabled: bool
    transaction_slot_account_state_available: bool
    missing_evidence: tuple[str, ...]
    decoder_version: str
    idl_hash: str
