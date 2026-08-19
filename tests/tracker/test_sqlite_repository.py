"""Unit tests for SQLiteTrackerRepository and DatabaseManager."""

from __future__ import annotations

from pathlib import Path

import pytest

from rugbot.storage.database import DatabaseManager
from rugbot.storage.tracker import SQLiteTrackerRepository
from rugbot.tracker.models import (
    FunderRecord,
    LaunchRecord,
    TargetExecutionMode,
    TargetExecutionPolicy,
    TransferRecord,
    WalletRecord,
    WalletStatus,
)
from rugbot.tracker.queries import build_funding_path


@pytest.fixture
def repo(tmp_path: Path) -> SQLiteTrackerRepository:
    db = DatabaseManager(tmp_path / "test_rugbot.db")
    return SQLiteTrackerRepository(db)


def test_funder_crud(repo: SQLiteTrackerRepository) -> None:
    funder = FunderRecord(
        id=None,
        address="Funder111",
        label="serial-dev",
        enabled=True,
        created_at="2026-08-17T20:00:00+00:00",
        last_seen_at="2026-08-17T20:00:00+00:00",
    )
    repo.save_funder(funder)

    fetched = repo.get_funder("Funder111")
    assert fetched is not None
    assert fetched.label == "serial-dev"
    assert fetched.enabled is True

    repo.enable_funder("Funder111", enabled=False)
    fetched_disabled = repo.get_funder("Funder111")
    assert fetched_disabled.enabled is False


def test_target_execution_policy_is_persisted_per_funder(
    repo: SQLiteTrackerRepository,
) -> None:
    """A target policy uses exact base units and does not share state with other funders."""
    repo.save_funder(
        FunderRecord(
            id=None,
            address="FunderPolicy",
            label="policy target",
            enabled=True,
            created_at="2026-08-17T20:00:00+00:00",
            last_seen_at="2026-08-17T20:00:00+00:00",
        )
    )
    policy = TargetExecutionPolicy(
        funder_address="FunderPolicy",
        monitoring_enabled=True,
        execution_mode=TargetExecutionMode.SIMULATED,
        quote_size_lamports=25_000_000,
        take_profit_pnl_ppm=1_250_000,
        stop_loss_pnl_ppm=-250_000,
        max_slippage_bps=750,
        priority_fee_microlamports=125_000,
        jito_tip_lamports=2_000_000,
        updated_at="2026-08-17T20:00:00+00:00",
    )

    repo.save_target_execution_policy(policy)

    assert repo.get_target_execution_policy("FunderPolicy") == policy
    assert repo.get_target_execution_policy("OtherFunder") is None


def test_path_reconstruction_from_repo(repo: SQLiteTrackerRepository) -> None:
    # 1. Setup F -> A -> B
    funder = "FunderRoot"
    wallet_a = "WalletA"
    wallet_b = "WalletB"
    mint = "TokenMintXYZ"

    repo.save_funder(
        FunderRecord(
            None, funder, "root", True, "2026-08-17T20:00:00", "2026-08-17T20:00:00"
        )
    )
    repo.save_wallet(
        WalletRecord(
            funder,
            funder,
            None,
            0,
            WalletStatus.FUNDER,
            "2026-08-17T20:00:00",
            None,
            "2026-08-17T20:00:00",
        )
    )
    repo.save_wallet(
        WalletRecord(
            wallet_a,
            funder,
            funder,
            1,
            WalletStatus.FUNDED,
            "2026-08-17T20:01:00",
            "2026-08-18T20:01:00",
            "2026-08-17T20:01:00",
        )
    )
    repo.save_wallet(
        WalletRecord(
            wallet_b,
            funder,
            wallet_a,
            2,
            WalletStatus.CREATOR,
            "2026-08-17T20:02:00",
            "2026-08-18T20:02:00",
            "2026-08-17T20:02:00",
        )
    )

    repo.save_transfer(
        TransferRecord(
            "tx1", 0, 100, 1700000000, funder, wallet_a, 3_200_000_000, 3.2, funder, 1
        )
    )
    repo.save_transfer(
        TransferRecord(
            "tx2",
            0,
            105,
            1700000060,
            wallet_a,
            wallet_b,
            3_180_000_000,
            3.18,
            funder,
            2,
        )
    )

    repo.save_launch(
        LaunchRecord(
            mint,
            wallet_b,
            funder,
            "DOGE2",
            "Doge 2",
            "tx_launch",
            110,
            1700000107,
            2,
            "tx2",
            3_180_000_000,
            1700000060,
        )
    )

    path = build_funding_path(wallet_b, repo)
    assert path is not None
    assert path.root_funder == funder
    assert path.creator_wallet == wallet_b
    assert path.total_depth == 2
    assert len(path.hops) == 2
    assert path.hops[0].from_wallet == funder
    assert path.hops[0].to_wallet == wallet_a
    assert path.hops[1].from_wallet == wallet_a
    assert path.hops[1].to_wallet == wallet_b
    assert path.time_to_launch_seconds == 47
