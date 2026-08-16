"""Domain quote contracts shared by protocol and decision layers."""

from dataclasses import dataclass
from enum import Enum

from rugbot.domain.amounts import Slot


class QuotePath(Enum):
    """Supported quote paths for executable quote contracts."""

    PUMP_BONDING_CURVE = "pump_bonding_curve"
    CANONICAL_PUMPSWAP = "canonical_pumpswap"


@dataclass(frozen=True, slots=True)
class ExecutableQuote:
    """Executable quote result from a validated decoded market snapshot."""

    path: QuotePath
    as_of_slot: Slot
    input_amount_base_units: int
    output_amount_base_units: int
    fee_amount_base_units: int
    base_decimals: int
    quote_decimals: int
    fee_config_version: str
    decoder_version: str
    idl_hash: str
    program_config_version: str
