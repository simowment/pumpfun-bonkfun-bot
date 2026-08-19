"""Typed events produced by the deterministic tracker engine."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any


@dataclass(frozen=True, slots=True, kw_only=True)
class TrackerEvent:
    """Base contract for all tracker domain events."""

    event_type: str
    root_funder: str
    wallet: str
    timestamp: int = field(default_factory=lambda: int(datetime.now(UTC).timestamp()))
    data: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True, kw_only=True)
class FunderAdded(TrackerEvent):
    """Triggered when a new root funder is registered for surveillance."""

    event_type: str = "funder_added"


@dataclass(frozen=True, slots=True, kw_only=True)
class WalletFunded(TrackerEvent):
    """Triggered when a descendant wallet receives verifiable SOL from a tracked node."""

    event_type: str = "wallet_funded"


@dataclass(frozen=True, slots=True, kw_only=True)
class TransferDetected(TrackerEvent):
    """Triggered on any verified SOL transfer between tracked nodes."""

    event_type: str = "transfer_detected"


@dataclass(frozen=True, slots=True, kw_only=True)
class LaunchDetected(TrackerEvent):
    """Triggered when a tracked wallet creates a token on Pump.fun."""

    event_type: str = "launch_detected"


@dataclass(frozen=True, slots=True, kw_only=True)
class WalletExpired(TrackerEvent):
    """Triggered when a descendant wallet exceeds its active TTL window."""

    event_type: str = "wallet_expired"


@dataclass(frozen=True, slots=True, kw_only=True)
class PathDepthLimitReached(TrackerEvent):
    """Triggered when a transfer would exceed max_depth."""

    event_type: str = "path_depth_limit_reached"


@dataclass(frozen=True, slots=True, kw_only=True)
class DecisionEvent(TrackerEvent):
    """Canonical domain qualification and execution decision event (PASS, SKIP, EXEC, FAIL)."""

    event_type: str = "decision_event"
    kind: str = "PASS"  # PASS, SKIP, EXEC, FAIL
    token_symbol: str = ""
    token_mint: str = ""
    reason: str = ""
    market_cap_usd: float | None = None
    winrate_pct: float | None = None
    block_number: int | None = None
    latency_summary: str | None = None
    order_size_sol: float | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class PathStopped(TrackerEvent):
    """Triggered when a transfer touches a blocked intermediary or exchange."""

    event_type: str = "path_stopped"
