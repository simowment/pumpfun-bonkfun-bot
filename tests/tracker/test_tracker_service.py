"""Unit tests for the integrated TrackerService."""

from __future__ import annotations

from pathlib import Path

import pytest

from rugbot.protocol.pump.models import TokenLaunch
from rugbot.protocol.solana.models import SolTransfer
from rugbot.runtime.event_bus import EventBus
from rugbot.runtime.tracker_service import TrackerService
from rugbot.storage.database import DatabaseManager
from rugbot.storage.tracker import SQLiteTrackerRepository
from rugbot.tracker.clock import FakeClock
from rugbot.tracker.engine import TrackerEngine
from rugbot.tracker.models import TrackerConfig, WalletStatus


@pytest.fixture
def service_setup(
    tmp_path: Path,
) -> tuple[TrackerService, SQLiteTrackerRepository, FakeClock, list[object]]:
    db = DatabaseManager(tmp_path / "test_rugbot.db")
    repo = SQLiteTrackerRepository(db)
    clock = FakeClock("2026-08-17T20:00:00Z")
    engine = TrackerEngine(config=TrackerConfig(max_depth=3), clock=clock)
    event_bus = EventBus()
    events_received: list[object] = []
    event_bus.subscribe("*", events_received.append)

    service = TrackerService(engine=engine, repository=repo, event_bus=event_bus)
    return service, repo, clock, events_received


def test_service_full_lifecycle(
    service_setup: tuple[
        TrackerService, SQLiteTrackerRepository, FakeClock, list[object]
    ],
) -> None:
    service, repo, clock, events_received = service_setup
    funder = "FunderFaBGrH"
    wallet_a = "Wallet8JuM"
    mint = "MintDoge2"

    # 1. Add funder
    service.add_funder(funder, label="serial-rugger")
    assert repo.get_funder(funder) is not None
    assert len(events_received) == 1

    # 2. Transfer F -> A
    service.handle_transfer(
        SolTransfer(
            "tx_f_a", 0, 100, clock.timestamp(), funder, wallet_a, 3_200_000_000
        )
    )
    assert repo.get_wallet(wallet_a) is not None
    assert repo.get_wallet(wallet_a).status == WalletStatus.FUNDED
    assert len(events_received) == 3  # FunderAdded, TransferDetected, WalletFunded

    # 3. Launch from A
    clock.advance(47)
    service.handle_launch(
        TokenLaunch(
            "tx_launch", 110, clock.timestamp(), wallet_a, mint, "DOGE2", "Dogecoin 2"
        )
    )
    assert repo.get_launch(mint) is not None
    assert repo.get_wallet(wallet_a).status == WalletStatus.CREATOR
    # A launch is observed, but no qualification result is fabricated.
    assert len(events_received) == 4
    assert not any(
        getattr(event, "event_type", "") == "decision_event"
        for event in events_received
    )


def test_service_cold_start_hydration(tmp_path: Path) -> None:
    db_path = tmp_path / "test_cold_start.db"
    db = DatabaseManager(db_path)
    repo = SQLiteTrackerRepository(db)
    clock = FakeClock("2026-08-17T20:00:00Z")

    # Populate DB with a funder and descendant
    service1 = TrackerService(TrackerEngine(clock=clock), repo)
    service1.add_funder("FUNDER_PERSIST", label="persisted")
    service1.handle_transfer(
        SolTransfer(
            "tx1",
            0,
            100,
            clock.timestamp(),
            "FUNDER_PERSIST",
            "WALLET_PERSIST",
            1_000_000_000,
        )
    )

    # Create fresh engine and service pointing to the same DB
    engine2 = TrackerEngine(clock=clock)
    assert not engine2.is_tracked("FUNDER_PERSIST")
    assert not engine2.is_tracked("WALLET_PERSIST")

    service2 = TrackerService(engine2, repo)
    # State should now be hydrated from SQLite!
    assert service2.engine.is_tracked("FUNDER_PERSIST")
    assert service2.engine.is_tracked("WALLET_PERSIST")
    assert service2.engine.get_tracked_wallet("WALLET_PERSIST").depth == 1
