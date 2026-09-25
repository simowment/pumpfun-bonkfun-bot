"""Multi-wallet signer pool for stealth execution.

Rotates through a pool of buyer addresses / keypairs to prevent front-running
and copytrading bots from clustering and frontrunning a single static wallet.
"""

# ruff: noqa: TRY003

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, Final

from rugbot.discover.cabal import validate_solana_address
from rugbot.utils.logger import get_logger

if TYPE_CHECKING:
    from collections.abc import Sequence

logger = get_logger(__name__)

DEFAULT_MAX_CONCURRENT_PER_WALLET: Final[int] = 3


class RotationPolicy(Enum):
    """Strategy for selecting the next buyer wallet."""

    ROUND_ROBIN = "round_robin"
    LEAST_RECENTLY_USED = "least_recently_used"


@dataclass
class ExecutionWallet:
    """One managed execution wallet address."""

    address: str
    active_trades_count: int = 0
    last_used_time: float = 0.0
    label: str = ""


class WalletPoolError(RuntimeError):
    """Raised when no execution wallets are available or configured."""


class WalletPool:
    """Manages a pool of execution wallets with rotation and concurrency bounds."""

    def __init__(
        self,
        wallets: Sequence[str | ExecutionWallet] = (),
        *,
        policy: RotationPolicy = RotationPolicy.ROUND_ROBIN,
        max_concurrent_per_wallet: int = DEFAULT_MAX_CONCURRENT_PER_WALLET,
    ) -> None:
        self.policy = policy
        self.max_concurrent_per_wallet = max_concurrent_per_wallet
        self._wallets: list[ExecutionWallet] = []
        self._current_index: int = 0

        for w in wallets:
            if isinstance(w, ExecutionWallet):
                validate_solana_address(w.address)
                self._wallets.append(w)
            else:
                addr = validate_solana_address(w)
                self._wallets.append(ExecutionWallet(address=addr))

    @classmethod
    def from_env(cls, env_var: str = "RUGBOT_BUYER_WALLETS") -> WalletPool:
        """Initialize wallet pool from a comma-separated list of addresses in an env var."""
        val = os.environ.get(env_var, "").strip()
        if not val:
            # Fallback default simulation wallet address if none specified
            default_addr = "11111111111111111111111111111111"
            return cls(
                wallets=[ExecutionWallet(address=default_addr, label="default_sim")]
            )

        addresses = [a.strip() for a in val.split(",") if a.strip()]
        return cls(wallets=addresses)

    def add_wallet(self, address: str, label: str = "") -> None:
        """Add an execution wallet address to the pool."""
        canonical = validate_solana_address(address)
        if not any(w.address == canonical for w in self._wallets):
            self._wallets.append(ExecutionWallet(address=canonical, label=label))

    @property
    def total_wallets(self) -> int:
        """Return the count of wallets in the pool."""
        return len(self._wallets)

    def acquire_wallet(self) -> ExecutionWallet:
        """Select the next execution wallet according to rotation policy."""
        if not self._wallets:
            raise WalletPoolError(
                "Wallet pool is empty; configure at least 1 buyer wallet."
            )

        now = time.time()

        # Find eligible wallets not exceeding concurrency bounds
        eligible = [
            w
            for w in self._wallets
            if w.active_trades_count < self.max_concurrent_per_wallet
        ]
        if not eligible:
            # Fallback to the wallet with least active trades
            chosen = min(self._wallets, key=lambda w: w.active_trades_count)
        elif self.policy == RotationPolicy.LEAST_RECENTLY_USED:
            chosen = min(eligible, key=lambda w: w.last_used_time)
        else:  # ROUND_ROBIN
            self._current_index = (self._current_index + 1) % len(eligible)
            chosen = eligible[self._current_index]

        chosen.last_used_time = now
        chosen.active_trades_count += 1
        return chosen

    def release_wallet(self, address: str) -> None:
        """Decrement active trade counter for the released wallet address."""
        for w in self._wallets:
            if w.address == address:
                w.active_trades_count = max(0, w.active_trades_count - 1)
                break


__all__ = [
    "DEFAULT_MAX_CONCURRENT_PER_WALLET",
    "ExecutionWallet",
    "RotationPolicy",
    "WalletPool",
    "WalletPoolError",
]
