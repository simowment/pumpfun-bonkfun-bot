"""Axiom Trade integration and URL formatting.

Axiom Trade routes meme tokens using the pool, pair, or bonding curve
address rather than the raw mint:
``https://axiom.trade/meme/<pool_address>?chain=sol&chains=sol,robinhood,eth``
"""

from __future__ import annotations

from typing import Final

from sol_trade_sdk.pump import derive_bonding_curve_pda
from solders.pubkey import Pubkey

AXIOM_MEME_BASE_URL: Final[str] = "https://axiom.trade/meme"
AXIOM_QUERY_PARAMS: Final[str] = "chain=sol&chains=sol,robinhood,eth"


def resolve_axiom_target(
    mint: str,
    *,
    pool_address: str | None = None,
    meta: dict[str, object] | None = None,
) -> str:
    """Resolve the pool, pair, or bonding-curve address for Axiom Trade routing.

    Args:
        mint: Base-58 token mint address.
        pool_address: Optional explicit pool or pair address.
        meta: Optional token metadata dictionary from Pump.fun or DEX API.

    Returns:
        The target address string to embed in the Axiom meme URL path.
    """
    if pool_address and pool_address.strip():
        return pool_address.strip()

    if meta:
        pump_swap = meta.get("pump_swap_pool")
        if isinstance(pump_swap, str) and pump_swap.strip():
            return pump_swap.strip()
        if meta.get("complete"):
            pool = meta.get("pool_address")
            if isinstance(pool, str) and pool.strip():
                return pool.strip()
        bonding_curve = meta.get("bonding_curve")
        if isinstance(bonding_curve, str) and bonding_curve.strip():
            return bonding_curve.strip()

    try:
        pda, _ = derive_bonding_curve_pda(Pubkey.from_string(mint))
        return str(pda)
    except Exception:
        return mint


def build_axiom_url(
    mint: str,
    *,
    pool_address: str | None = None,
    meta: dict[str, object] | None = None,
) -> str:
    """Build the canonical Axiom Trade URL for a Solana token.

    Args:
        mint: Base-58 token mint address.
        pool_address: Optional explicit pool or pair address.
        meta: Optional token metadata dictionary from Pump.fun or DEX API.

    Returns:
        Full Axiom URL with the query parameters for Solana.
    """
    target = resolve_axiom_target(mint, pool_address=pool_address, meta=meta)
    return f"{AXIOM_MEME_BASE_URL}/{target}?{AXIOM_QUERY_PARAMS}"


__all__ = [
    "AXIOM_MEME_BASE_URL",
    "AXIOM_QUERY_PARAMS",
    "build_axiom_url",
    "resolve_axiom_target",
]
