"""Tests for Cross-Entity Bundler Registry persistence and serialization."""

from __future__ import annotations

from pathlib import Path

from rugbot.intelligence.bundle_analysis import cross_entity_bundles_to_json
from rugbot.storage.database import DatabaseManager
from rugbot.storage.tracker import SQLiteTrackerRepository
from rugbot.tracker.models import BundleParticipationRecord


def test_bundle_participations_sqlite_crud(tmp_path: Path) -> None:
    """Verify SQLite persistence, deduplication, and creator-exclusion querying."""
    db = DatabaseManager(tmp_path / "test_tracker.db")
    repo = SQLiteTrackerRepository(db)

    wallet_a = "BundlerWalletA111111111111111111111111111111"
    wallet_b = "BundlerWalletB222222222222222222222222222222"
    creator_1 = "CreatorDev111111111111111111111111111111111"
    creator_2 = "CreatorDev222222222222222222222222222222222"

    records = (
        BundleParticipationRecord(
            bundler_wallet=wallet_a,
            mint="MintAlpha1111111111111111111111111111111111",
            creator=creator_1,
            creation_slot=1000,
            buy_signature="sig_a_alpha_1",
            transaction_index=2,
            max_sol_cost_lamports=50_000_000,
        ),
        BundleParticipationRecord(
            bundler_wallet=wallet_a,
            mint="MintBeta22222222222222222222222222222222222",
            creator=creator_2,
            creation_slot=2000,
            buy_signature="sig_a_beta_1",
            transaction_index=3,
            max_sol_cost_lamports=75_000_000,
        ),
        BundleParticipationRecord(
            bundler_wallet=wallet_b,
            mint="MintBeta22222222222222222222222222222222222",
            creator=creator_2,
            creation_slot=2000,
            buy_signature="sig_b_beta_1",
            transaction_index=4,
            max_sol_cost_lamports=100_000_000,
        ),
    )

    # Save records
    repo.save_bundle_participations(records)

    # Duplicate save (should be ignored by INSERT OR IGNORE without error)
    repo.save_bundle_participations(records)

    # Query wallet_a excluding creator_1 -> should find creator_2 participation
    results_a = repo.get_bundle_participations((wallet_a,), exclude_creator=creator_1)
    assert len(results_a) == 1
    assert results_a[0].bundler_wallet == wallet_a
    assert results_a[0].creator == creator_2
    assert results_a[0].mint == "MintBeta22222222222222222222222222222222222"
    assert results_a[0].buy_signature == "sig_a_beta_1"
    assert results_a[0].max_sol_cost_lamports == 75_000_000

    # Query wallet_a and wallet_b excluding creator_1 -> both found on creator_2
    results_both = repo.get_bundle_participations(
        (wallet_a, wallet_b), exclude_creator=creator_1
    )
    assert len(results_both) == 2
    assert {r.bundler_wallet for r in results_both} == {wallet_a, wallet_b}

    # Query excluding creator_2 -> only wallet_a on creator_1 returned
    results_ex_c2 = repo.get_bundle_participations(
        (wallet_a, wallet_b), exclude_creator=creator_2
    )
    assert len(results_ex_c2) == 1
    assert results_ex_c2[0].bundler_wallet == wallet_a
    assert results_ex_c2[0].creator == creator_1

    # Empty query tuple returns empty tuple
    assert repo.get_bundle_participations((), exclude_creator=creator_1) == ()


def test_cross_entity_bundles_to_json_grouping() -> None:
    """Verify JSON serialization and aggregation by bundler wallet."""
    # Empty case
    empty_json = cross_entity_bundles_to_json(())
    assert empty_json == {
        "wallet_count": 0,
        "linked_entity_count": 0,
        "wallets": [],
    }

    # Multi-wallet multi-creator case
    wallet_1 = "Wallet1111111111111111111111111111111111111"
    wallet_2 = "Wallet2222222222222222222222222222222222222"
    creator_x = "CreatorX11111111111111111111111111111111111"
    creator_y = "CreatorY22222222222222222222222222222222222"

    participations = (
        BundleParticipationRecord(
            bundler_wallet=wallet_1,
            mint="Mint1",
            creator=creator_x,
            creation_slot=100,
            buy_signature="sig1",
            transaction_index=1,
            max_sol_cost_lamports=10_000_000,
        ),
        BundleParticipationRecord(
            bundler_wallet=wallet_1,
            mint="Mint2",
            creator=creator_y,
            creation_slot=200,
            buy_signature="sig2",
            transaction_index=2,
            max_sol_cost_lamports=20_000_000,
        ),
        BundleParticipationRecord(
            bundler_wallet=wallet_2,
            mint="Mint3",
            creator=creator_y,
            creation_slot=300,
            buy_signature="sig3",
            transaction_index=3,
            max_sol_cost_lamports=30_000_000,
        ),
    )

    out = cross_entity_bundles_to_json(participations)
    assert out["wallet_count"] == 2
    assert out["linked_entity_count"] == 2  # creator_x, creator_y

    wallets = out["wallets"]
    assert isinstance(wallets, list)
    assert len(wallets) == 2

    # wallet_1 has 2 buys across 2 external creators
    w1 = next(w for w in wallets if w["wallet"] == wallet_1)
    assert w1["external_creator_count"] == 2
    assert sorted(w1["external_creators"]) == sorted([creator_x, creator_y])
    assert len(w1["buys"]) == 2

    # wallet_2 has 1 buy across 1 external creator
    w2 = next(w for w in wallets if w["wallet"] == wallet_2)
    assert w2["external_creator_count"] == 1
    assert w2["external_creators"] == [creator_y]
    assert len(w2["buys"]) == 1
