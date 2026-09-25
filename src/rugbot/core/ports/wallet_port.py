"""Wallet port boundary contract."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from rugbot.core.models.address import Address
    from rugbot.core.models.token import TokenAmount


class WalletPort(ABC):
    """Port responsible for account balances, addresses, and transaction signing."""

    @abstractmethod
    async def get_native_balance(self) -> TokenAmount:
        """Fetch native currency balance (SOL on Solana, ETH on Robinhood Chain)."""
        raise NotImplementedError

    @abstractmethod
    async def get_token_balance(self, target_token: Address) -> TokenAmount:
        """Fetch specific token balance for the configured wallet."""
        raise NotImplementedError

    @property
    @abstractmethod
    def public_address(self) -> Address:
        """Return the public address of the configured wallet."""
        raise NotImplementedError
