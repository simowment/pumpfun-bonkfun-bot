"""Standard Pump bonding-curve model shared by replay and paper trading."""

import math
from typing import Final

from rugbot.domain.amounts import LAMPORTS_PER_SOL
from rugbot.domain.fees import FeeConfig
from rugbot.domain.quote_engine import pump_curve_buy_amounts, pump_curve_sell_amounts

TOKEN_DECIMALS: Final[int] = 6
TOKEN_SUPPLY_UI: Final[int] = 1_000_000_000
TOKEN_SUPPLY_BASE_UNITS: Final[int] = TOKEN_SUPPLY_UI * 10**TOKEN_DECIMALS
# Launch state: 30 SOL x 1.073B tokens virtual, 793.1M tokens real.
INITIAL_VIRTUAL_QUOTE: Final[int] = 30 * LAMPORTS_PER_SOL
INITIAL_VIRTUAL_BASE: Final[int] = 1_073_000_000 * 10**TOKEN_DECIMALS
CURVE_INVARIANT: Final[int] = INITIAL_VIRTUAL_QUOTE * INITIAL_VIRTUAL_BASE
# Replays clamp a price multiple here instead of dividing by a zero price.
MIN_PRICE_MULTIPLE: Final[float] = 0.001
# Tolerance when matching a coin's curve invariant to the standard curve.
CURVE_INVARIANT_TOLERANCE: Final[float] = 0.01
PUMP_CURVE_FEE_CONFIG: Final[FeeConfig] = FeeConfig(
    version="pump-global-v1",
    protocol_fee_bps=95,
    creator_fee_bps=30,
    is_known=True,
    program_config_version="pump-global-v1",
    valid_from_slot=0,
    valid_to_slot=None,
    source_artifact_version="pump-global-v1",
    lp_fee_bps=0,
)


def nonstandard_curve_reason(
    curve_invariant: int | None, *, mayhem: bool
) -> str | None:
    """Return why a coin cannot be traded on the standard curve, if it can't.

    Mayhem-mode coins are traded by Pump's protocol agent on inflated virtual
    reserves with almost no real SOL, so standard-curve fills would be fiction.
    """
    if mayhem:
        return "Mayhem-mode coin (protocol agent, no real curve liquidity)"
    if curve_invariant is None:
        return "curve reserves unavailable"
    if (
        abs(curve_invariant - CURVE_INVARIANT)
        > CURVE_INVARIANT * CURVE_INVARIANT_TOLERANCE
    ):
        return "non-standard bonding curve"
    return None


def reserves_at_multiple(price_multiple: float) -> tuple[int, int]:
    """Virtual ``(quote, base)`` reserves where price = ``price_multiple`` x launch.

    Both sides move along the constant-product invariant, so the curve depth
    (and therefore price impact) matches the real curve at that price.
    """
    root = math.sqrt(max(price_multiple, MIN_PRICE_MULTIPLE))
    return int(INITIAL_VIRTUAL_QUOTE * root), int(INITIAL_VIRTUAL_BASE / root)


def price_multiple(lamports_per_base_unit: float) -> float:
    """Price as a multiple of the launch price (lamports per token base unit)."""
    return lamports_per_base_unit * INITIAL_VIRTUAL_BASE / INITIAL_VIRTUAL_QUOTE


def curve_buy(price_multiple: float, spend_lamports: int) -> tuple[int, int]:
    """``(tokens_out, fee)`` for a buy at ``price_multiple`` x launch price."""
    quote, base = reserves_at_multiple(price_multiple)
    return pump_curve_buy_amounts(
        virtual_quote_reserves=quote,
        virtual_base_reserves=base,
        spendable_quote_in=spend_lamports,
        fee_config=PUMP_CURVE_FEE_CONFIG,
    )


def curve_sell(price_multiple: float, tokens: int) -> tuple[int, int]:
    """``(lamports_out, fee)`` for a sell at ``price_multiple`` x launch price."""
    quote, base = reserves_at_multiple(price_multiple)
    return pump_curve_sell_amounts(
        virtual_quote_reserves=quote,
        virtual_base_reserves=base,
        base_input_amount=tokens,
        fee_config=PUMP_CURVE_FEE_CONFIG,
    )


def market_cap_lamports(virtual_quote: int, virtual_base: int) -> int:
    """Fully diluted market cap in lamports from virtual curve reserves."""
    return virtual_quote * TOKEN_SUPPLY_BASE_UNITS // virtual_base
