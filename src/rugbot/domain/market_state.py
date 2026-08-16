"""Market-state snapshots derived from artifact-backed protocol evidence."""

from dataclasses import dataclass

from rugbot.domain.amounts import QuoteBaseUnits, Slot, TokenBaseUnits


@dataclass(frozen=True, slots=True)
class PumpBondingCurveAccountSnapshot:
    """Decoded Pump bonding-curve account state for one point in time."""

    as_of_slot: Slot
    account_pubkey: str
    owner_program_id: str
    virtual_token_reserves: TokenBaseUnits
    virtual_sol_reserves: QuoteBaseUnits
    real_token_reserves: TokenBaseUnits
    real_sol_reserves: QuoteBaseUnits
    token_total_supply: TokenBaseUnits
    complete: bool
    creator: bytes
    is_mayhem_mode: bool
    is_cashback_coin: bool
    base_decimals: int
    quote_decimals: int
    base_mint: str
    quote_mint: str
    raw_account_data_sha256: str
    account_data_length: int
    trailing_zero_padding_length: int
    decoder_version: str
    idl_hash: str
    program_config_version: str
    layout_artifact_version: str
    source_artifact_version: str
