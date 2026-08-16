"""Trade instruction evidence domain objects."""

from dataclasses import dataclass
from enum import Enum

from rugbot.domain.amounts import QuoteBaseUnits, Slot, TokenBaseUnits


class TradeSide(Enum):
    """Trade direction observed in a protocol instruction."""

    BUY = "buy"
    SELL = "sell"


@dataclass(frozen=True, slots=True)
class PumpTradeInstructionEvidence:
    """Decoded Pump trade instruction evidence before fill reconstruction.

    This is not a complete executed trade event. It preserves the instruction
    arguments and account indices that are known from the pinned IDL. Actual
    executed amounts, balance changes, fees, and reserve transitions require
    finalized transaction metadata and transaction-slot account state.
    """

    as_of_slot: Slot
    program_id: str
    program_id_index: int | None
    signature: bytes | None
    instruction_name: str
    side: TradeSide
    account_indices: tuple[int, ...]
    account_pubkeys: tuple[str, ...] | None
    account_role_proofs: tuple[tuple[str, str], ...]
    required_account_names: tuple[str, ...]
    remaining_account_indices: tuple[int, ...]
    transaction_index: int | None
    outer_instruction_index: int
    inner_instruction_group_index: int | None
    inner_instruction_index: int | None
    mint_account_index: int
    bonding_curve_account_index: int
    associated_bonding_curve_account_index: int
    associated_user_account_index: int
    user_account_index: int
    token_program_account_index: int
    fee_config_account_index: int
    fee_program_account_index: int
    base_amount_base_units: TokenBaseUnits | None
    quote_amount_base_units: QuoteBaseUnits | None
    max_quote_cost_base_units: QuoteBaseUnits | None
    min_base_output_base_units: TokenBaseUnits | None
    min_quote_output_base_units: QuoteBaseUnits | None
    track_volume: bool | None
    transaction_slot_account_state_available: bool
    missing_evidence: tuple[str, ...]
    decoder_version: str
    idl_hash: str


@dataclass(frozen=True, slots=True)
class PumpSwapTradeInstructionEvidence:
    """Decoded PumpSwap instruction evidence before fill reconstruction."""

    as_of_slot: Slot
    program_id: str
    program_id_index: int | None
    signature: bytes | None
    instruction_name: str
    side: TradeSide
    account_indices: tuple[int, ...]
    account_pubkeys: tuple[str, ...] | None
    account_role_proofs: tuple[tuple[str, str], ...]
    required_account_names: tuple[str, ...]
    remaining_account_indices: tuple[int, ...]
    transaction_index: int | None
    outer_instruction_index: int
    inner_instruction_group_index: int | None
    inner_instruction_index: int | None
    pool_account_index: int
    user_account_index: int
    global_config_account_index: int
    base_mint_account_index: int
    quote_mint_account_index: int
    pool_base_token_account_index: int
    pool_quote_token_account_index: int
    base_token_program_account_index: int
    quote_token_program_account_index: int
    fee_config_account_index: int
    fee_program_account_index: int
    base_amount_base_units: TokenBaseUnits | None
    quote_amount_base_units: QuoteBaseUnits | None
    max_quote_cost_base_units: QuoteBaseUnits | None
    min_base_output_base_units: TokenBaseUnits | None
    min_quote_output_base_units: QuoteBaseUnits | None
    track_volume: bool | None
    transaction_slot_account_state_available: bool
    missing_evidence: tuple[str, ...]
    decoder_version: str
    idl_hash: str


@dataclass(frozen=True, slots=True)
class PumpSwapTradeEventEvidence:
    """Executed PumpSwap event decoded from finalized transaction logs.

    The buy and sell event layouts expose different amount names.  The
    normalized amount fields preserve their direction: ``base_amount`` is
    base out on buys and base in on sells; ``quote_amount`` is quote in on
    buys and quote out on sells.  The raw event remains available for audit.
    """

    as_of_slot: Slot
    signature: bytes
    event_index: int
    side: TradeSide
    timestamp: int
    pool: str
    user: str
    base_amount_base_units: TokenBaseUnits
    quote_amount_base_units: QuoteBaseUnits
    user_quote_amount_base_units: QuoteBaseUnits
    pool_base_reserves_base_units: TokenBaseUnits
    pool_quote_reserves_base_units: QuoteBaseUnits
    virtual_quote_reserves_base_units: QuoteBaseUnits
    lp_fee_basis_points: int
    lp_fee_base_units: QuoteBaseUnits
    protocol_fee_basis_points: int
    protocol_fee_base_units: QuoteBaseUnits
    creator_fee_basis_points: int
    creator_fee_base_units: QuoteBaseUnits
    instruction_name: str
    encoded_event: bytes
