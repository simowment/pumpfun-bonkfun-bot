"""Execution port boundary contract."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from rugbot.core.models.address import Address
    from rugbot.core.models.order import OrderIntent, OrderSide, TradeReceipt
    from rugbot.core.models.quote import ExecutionQuote
    from rugbot.core.models.token import TokenAmount


class ExecutionPort(ABC):
    """Port responsible for quoting, simulating, and broadcasting trades."""

    @abstractmethod
    async def get_quote(
        self,
        target_token: Address,
        side: OrderSide,
        amount_in: TokenAmount,
    ) -> ExecutionQuote:
        """Calculate expected output, price impact, and minimum output."""
        raise NotImplementedError

    @abstractmethod
    async def execute(self, intent: OrderIntent) -> TradeReceipt:
        """Execute a buy or sell order (live, paper, or dry-run)."""
        raise NotImplementedError
