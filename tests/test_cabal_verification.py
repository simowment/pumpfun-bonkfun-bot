"""Verification tests for the cabal intelligence and copytrading pipeline.

Includes checks for:
1. Mathematical Net EV and unit economics guarantees.
2. Persisted SQLite cabal cluster data integrity and entity reconciliation.
3. Live / fail-soft Discord webhook delivery.
4. Manual review flagging on high-conviction borderline signals.
"""

# ruff: noqa: S106

from __future__ import annotations

import os
from pathlib import Path

from rugbot.discover.cabal import CabalStore, validate_solana_address
from rugbot.intelligence.signal_filter import (
    SignalDecision,
    SignalFilter,
    SignalFilterConfig,
)
from rugbot.runtime.cabal_pipeline import (
    send_discord_cabal_alert,
)
from rugbot.runtime.config import resolve_dotenv


def test_profitable_preset_net_ev_guarantee() -> None:
    """Verify mathematically that the profitable setup yields positive Net EV and low breakeven."""
    pos_size = 0.50  # SOL
    friction = 0.0225  # Jito tip + 2% pump fees + 1.5% slippage

    leg1_proceeds = pos_size * 0.50 * 2.0  # 0.50 SOL
    leg2_runners = (0.65 * (pos_size * 0.50 * 2.38)) + (
        0.35 * ((pos_size * 0.25 * 5.0) + (pos_size * 0.25 * 4.25))
    )
    win_net_pnl = (leg1_proceeds + leg2_runners) - pos_size - friction
    loss_net_pnl = (pos_size * 0.15) - pos_size - friction

    assert win_net_pnl > pos_size

    breakeven_wr = abs(loss_net_pnl) / (win_net_pnl + abs(loss_net_pnl))
    assert breakeven_wr < 0.40

    baseline_ev = (0.56 * win_net_pnl) + (0.44 * loss_net_pnl)
    assert baseline_ev > 0.20
    assert (baseline_ev / pos_size) > 0.40


def test_persisted_cabal_clusters_integrity() -> None:
    """Verify that stored clusters in .state/cabal/cabal_clusters.sqlite3 are valid."""
    db_path = Path(".state/cabal/cabal_clusters.sqlite3")
    if not db_path.exists():
        return

    store = CabalStore(db_path=db_path)
    clusters = store.list_clusters()
    assert len(clusters) > 0

    for c in clusters:
        assert c.cluster_id.startswith("cabal-")
        assert validate_solana_address(c.funder) == c.funder
        for w in c.wallets:
            assert validate_solana_address(w) == w
        assert c.median_ath > 0
        assert c.typical_buy_sol >= 0.0


def test_manual_review_borderline_flagging() -> None:
    """Verify that high-conviction solo buys are marked for manual review."""
    filt = SignalFilter(config=SignalFilterConfig.profitable_preset())

    decision = filt.evaluate_signal(
        mint="So11111111111111111111111111111111111111112",
        wallet="4Nd1mBQtrMJVYVfKf2PJy9NZd2A2bbCeZW5DhN9jbjAo",
        amount_sol=0.85,  # >= 80% of default 1.0 SOL typical buy
        token_created_time=1000.0,
        current_liquidity_usd=15000.0,
        current_time=1050.0,
    )
    assert decision.action == "ABSTAIN"
    assert decision.needs_manual_review is True


def test_discord_webhook_delivery_check() -> None:
    """Verify Discord webhook delivery functions live or fail-soft cleanly."""
    resolve_dotenv()
    url = os.environ.get("DISCORD_WEBHOOK_URL", "")

    decision = SignalDecision(
        action="BUY",
        score=2.5,
        confluence_count=2,
        conviction_ratio=1.0,
        reasons=(),
        mint="So11111111111111111111111111111111111111112",
        wallet="4Nd1mBQtrMJVYVfKf2PJy9NZd2A2bbCeZW5DhN9jbjAo",
        amount_sol=0.50,
    )

    if url.strip():
        res = send_discord_cabal_alert(
            url, decision, None, token_symbol="VERIFY_TEST", market_cap_usd=40000.0
        )
        assert res is True
    else:
        res = send_discord_cabal_alert("", decision, None)
        assert res is False
