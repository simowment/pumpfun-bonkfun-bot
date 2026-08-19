"""Pure unit tests for the decoupled TrackerEngine."""

from __future__ import annotations

from rugbot.protocol.pump.models import TokenLaunch
from rugbot.protocol.solana.models import SolTransfer
from rugbot.tracker.engine import TrackerEngine
from rugbot.tracker.events import LaunchDetected, PathDepthLimitReached, PathStopped
from rugbot.tracker.models import TrackerConfig, WalletStatus


def test_pure_engine_direct_path() -> None:
    """FUNDER -> transfer SOL -> A -> CREATE TOKEN."""
    engine = TrackerEngine()
    funder = "FUNDER_ROOT"
    wallet_a = "WALLET_A"
    mint = "MINT_TOKEN_XYZ"

    # 1. Add funder
    engine.add_funder(funder, label="serial-dev")
    assert engine.is_tracked(funder)

    # 2. Transfer from funder -> A
    transfer = SolTransfer(
        signature="sig_transfer_1",
        instruction_index=0,
        slot=1000,
        timestamp=1700000000,
        sender=funder,
        recipient=wallet_a,
        lamports=3_200_000_000,
    )
    events = engine.handle_transfer(transfer)
    assert len(events) == 2
    assert engine.is_tracked(wallet_a)
    assert engine.get_tracked_wallet(wallet_a).depth == 1

    # 3. Launch from A
    launch = TokenLaunch(
        signature="sig_create_1",
        slot=1010,
        timestamp=1700000047,
        creator=wallet_a,
        mint=mint,
        symbol="DOGE2",
        name="Dogecoin 2.0",
    )
    launch_events = engine.handle_launch(launch)
    assert len(launch_events) == 1
    assert isinstance(launch_events[0], LaunchDetected)
    assert launch_events[0].root_funder == funder
    assert launch_events[0].wallet == wallet_a
    assert engine.get_tracked_wallet(wallet_a).status == WalletStatus.CREATOR


def test_pure_engine_multi_hop_path() -> None:
    """FUNDER -> transfer SOL -> A -> transfer SOL -> B -> CREATE TOKEN."""
    engine = TrackerEngine(TrackerConfig(max_depth=3))
    funder = "FUNDER_ROOT"
    wallet_a = "WALLET_A"
    wallet_b = "WALLET_B"
    mint = "MINT_TOKEN_PEPE"

    engine.add_funder(funder)

    # Hop 1: F -> A
    engine.handle_transfer(
        SolTransfer("tx1", 0, 100, 1000, funder, wallet_a, 5_000_000_000)
    )
    assert engine.get_tracked_wallet(wallet_a).depth == 1

    # Hop 2: A -> B
    events_hop2 = engine.handle_transfer(
        SolTransfer("tx2", 0, 110, 1060, wallet_a, wallet_b, 4_900_000_000)
    )
    assert len(events_hop2) == 2
    assert engine.get_tracked_wallet(wallet_b).depth == 2
    assert engine.get_tracked_wallet(wallet_b).root_funder == funder

    # Launch by B
    launch_events = engine.handle_launch(
        TokenLaunch("tx3", 120, 1120, wallet_b, mint, "PEPE2", "Pepe 2")
    )
    assert len(launch_events) == 1
    assert launch_events[0].root_funder == funder
    assert launch_events[0].wallet == wallet_b


def test_depth_limit_exceeded() -> None:
    """Tree halts at max_depth."""
    engine = TrackerEngine(TrackerConfig(max_depth=2))
    funder = "F"
    w1, w2, w3 = "W1", "W2", "W3"

    engine.add_funder(funder)
    engine.handle_transfer(
        SolTransfer("tx1", 0, 100, 100, funder, w1, 1_000_000_000)
    )  # Depth 1
    engine.handle_transfer(
        SolTransfer("tx2", 0, 101, 101, w1, w2, 900_000_000)
    )  # Depth 2

    # Attempt depth 3
    events = engine.handle_transfer(
        SolTransfer("tx3", 0, 102, 102, w2, w3, 800_000_000)
    )
    assert len(events) == 1
    assert isinstance(events[0], PathDepthLimitReached)
    assert not engine.is_tracked(w3)


def test_blocked_intermediary() -> None:
    """Blocked intermediary stops funding tree progression."""
    blocked = "EXCHANGE_HOT_WALLET"
    engine = TrackerEngine(TrackerConfig(blocked_intermediaries=frozenset([blocked])))
    funder = "FUNDER"
    engine.add_funder(funder)

    events = engine.handle_transfer(
        SolTransfer("tx1", 0, 100, 100, funder, blocked, 10_000_000_000)
    )
    assert len(events) == 1
    assert isinstance(events[0], PathStopped)
    assert not engine.is_tracked(blocked)
