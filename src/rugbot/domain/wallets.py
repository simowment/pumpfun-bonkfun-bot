"""Domain models and lifecycle states for tracked wallets."""

# ruff: noqa: TC001

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from rugbot.domain.amounts import Slot


class WalletStatus(StrEnum):
    """Lifecycle status of a tracked wallet within a funding tree."""

    FUNDER = "funder"
    FUNDED = "funded"
    CREATOR = "creator"
    EXPIRED = "expired"
    IGNORED = "ignored"


@dataclass(frozen=True, slots=True)
class TrackedWallet:
    """In-memory active state of one tracked wallet node."""

    address: str
    root_funder: str
    parent_wallet: str
    depth: int
    status: WalletStatus
    first_seen_slot: Slot
    expires_at_slot: Slot | None = None


__all__ = [
    "TrackedWallet",
    "WalletStatus",
]
