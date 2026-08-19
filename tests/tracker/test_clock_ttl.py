"""Deterministic unit tests for Clock protocol and instantaneous TTL expiration."""

from __future__ import annotations

from rugbot.protocol.solana.models import SolTransfer
from rugbot.tracker.clock import FakeClock
from rugbot.tracker.engine import TrackerEngine
from rugbot.tracker.events import WalletExpired
from rugbot.tracker.models import TrackerConfig, WalletStatus


def test_fake_clock_instant_ttl_expiration() -> None:
    """Test instantaneous, deterministic TTL expiration using FakeClock."""
    clock = FakeClock("2026-08-17T20:00:00Z")
    config = TrackerConfig(max_depth=3, descendant_ttl_seconds=86400)  # 24 hours
    engine = TrackerEngine(config=config, clock=clock)

    funder = "FaBGrHWjcJ8vKnbgUtsdpZjvF7YAAajtQTWmmEHiKtQr"
    wallet_a = "8JuM4a91Mxxk39v8Lz119q2mNkLs88PqZaA77889901"

    # 1. Register funder at 20:00:00
    engine.add_funder(funder)

    # 2. Transfer from funder -> wallet_a at 20:00:00
    transfer = SolTransfer(
        signature="sig_1",
        instruction_index=0,
        slot=100,
        timestamp=clock.timestamp(),
        sender=funder,
        recipient=wallet_a,
        lamports=3_200_000_000,
    )
    events = engine.handle_transfer(transfer)
    assert len(events) == 2  # TransferDetected + WalletFunded
    assert engine.get_tracked_wallet(wallet_a).status == WalletStatus.FUNDED

    # 3. Advance time by 23 hours 59 minutes (86,340 seconds)
    clock.advance(86340)
    expired_events = engine.expire_wallets()
    assert len(expired_events) == 0
    assert engine.get_tracked_wallet(wallet_a).status == WalletStatus.FUNDED

    # 4. Advance time by 2 more minutes (+86,460 total seconds) -> Wallet should expire
    clock.advance(120)
    expired_events = engine.expire_wallets()
    assert len(expired_events) == 1
    assert isinstance(expired_events[0], WalletExpired)
    assert expired_events[0].wallet == wallet_a
    assert engine.get_tracked_wallet(wallet_a).status == WalletStatus.EXPIRED


def test_reactivation_after_expiration() -> None:
    """Test that a new transfer from a tracked parent reactivates an expired wallet."""
    clock = FakeClock("2026-08-17T20:00:00Z")
    engine = TrackerEngine(TrackerConfig(descendant_ttl_seconds=3600), clock=clock)

    funder = "FUNDER_1"
    wallet_a = "WALLET_A"

    engine.add_funder(funder)
    engine.handle_transfer(
        SolTransfer("sig_1", 0, 100, clock.timestamp(), funder, wallet_a, 1_000_000_000)
    )

    # Advance beyond TTL
    clock.advance(4000)
    engine.expire_wallets()
    assert engine.get_tracked_wallet(wallet_a).status == WalletStatus.EXPIRED

    # New transfer arrives from funder -> reactivates wallet_a
    events = engine.handle_transfer(
        SolTransfer("sig_2", 0, 200, clock.timestamp(), funder, wallet_a, 1_000_000_000)
    )
    assert any(e.event_type == "transfer_detected" for e in events)
    assert engine.get_tracked_wallet(wallet_a).status == WalletStatus.FUNDED
