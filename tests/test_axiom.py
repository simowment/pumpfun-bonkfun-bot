"""Unit tests for Axiom Trade URL and pool resolution."""

from rugbot.integrations.axiom import (
    AXIOM_MEME_BASE_URL,
    AXIOM_QUERY_PARAMS,
    build_axiom_url,
    resolve_axiom_target,
)

MINT = "DGH4iQd1wZn5y85ynHFCdd3dU6MBaygHRh46VEWLpump"
PUMP_SWAP_POOL = "EXBiZWZbPkY8Zbtco9rfbQwDAcBiERoEF7G78Rr5rYhe"
EXPECTED_BONDING_CURVE = "97hJBRHvCnxHZGtQr7VffKvzCbb7j7wnNpmJYKhw5Ydh"


def test_resolve_axiom_target_explicit_pool() -> None:
    """Explicit pool address takes priority over everything."""
    target = resolve_axiom_target(MINT, pool_address=PUMP_SWAP_POOL)
    assert target == PUMP_SWAP_POOL


def test_resolve_axiom_target_meta_graduated() -> None:
    """Metadata with pump_swap_pool routes to the AMM pool address."""
    meta = {
        "pump_swap_pool": PUMP_SWAP_POOL,
        "bonding_curve": EXPECTED_BONDING_CURVE,
        "complete": True,
    }
    target = resolve_axiom_target(MINT, meta=meta)
    assert target == PUMP_SWAP_POOL


def test_resolve_axiom_target_meta_on_curve() -> None:
    """Metadata for active curve routes to bonding_curve address."""
    meta = {
        "bonding_curve": EXPECTED_BONDING_CURVE,
        "complete": False,
    }
    target = resolve_axiom_target(MINT, meta=meta)
    assert target == EXPECTED_BONDING_CURVE


def test_resolve_axiom_target_fallback_derivation() -> None:
    """Without metadata or pool, deterministically derive bonding curve PDA."""
    target = resolve_axiom_target(MINT)
    assert target == EXPECTED_BONDING_CURVE


def test_build_axiom_url_format() -> None:
    """Build Axiom URL conforms to canonical query params and meme path."""
    url = build_axiom_url(MINT, pool_address=PUMP_SWAP_POOL)
    expected = f"{AXIOM_MEME_BASE_URL}/{PUMP_SWAP_POOL}?{AXIOM_QUERY_PARAMS}"
    assert url == expected
    assert "chain=sol&chains=sol,robinhood,eth" in url
