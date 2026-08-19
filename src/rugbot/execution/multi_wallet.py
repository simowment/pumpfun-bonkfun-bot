"""Multi-wallet concurrent execution manager for simultaneous dispatch."""

# ruff: noqa: PLR0913

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import TYPE_CHECKING

from rugbot.domain.amounts import Lamports, Slot
from rugbot.execution.ports import (
    ExecutionIntent,
    ExecutionMode,
    ExecutionReceipt,
    non_submitting_receipt,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

    from rugbot.execution.ports import ExecutionPort

DEFAULT_SLOT = Slot(0)


@dataclass(frozen=True, slots=True)
class ExecutionWalletConfig:
    """Configuration for one execution wallet."""

    label: str
    address: str
    private_key: str | None = None
    enabled: bool = True


@dataclass(frozen=True, slots=True)
class MultiWalletReceipt:
    """Aggregated execution receipt across multiple wallets."""

    market_id: str
    side: str
    total_quote_lamports: Lamports
    total_base_units: int
    receipts: tuple[tuple[str, ExecutionReceipt], ...]
    successful_count: int
    failed_count: int


class MultiWalletExecutor:
    """Coordinates simultaneous execution across multiple configured wallets."""

    def __init__(
        self,
        *,
        mode: ExecutionMode = ExecutionMode.PAPER,
        wallets: Sequence[ExecutionWalletConfig] | None = None,
        execution_ports: dict[str, ExecutionPort] | None = None,
    ) -> None:
        """Initialize multi-wallet executor with execution ports."""
        self._mode = mode
        self._wallets = list(wallets or [])
        self._execution_ports = dict(execution_ports or {})

    @property
    def wallets(self) -> tuple[ExecutionWalletConfig, ...]:
        """Return registered execution wallets."""
        return tuple(self._wallets)

    @property
    def active_wallets(self) -> tuple[ExecutionWalletConfig, ...]:
        """Return only enabled execution wallets."""
        return tuple(w for w in self._wallets if w.enabled)

    def add_wallet(
        self, wallet: ExecutionWalletConfig, port: ExecutionPort | None = None
    ) -> None:
        """Register a new execution wallet and optional port."""
        self._wallets.append(wallet)
        if port is not None:
            self._execution_ports[wallet.address] = port

    def register_port(self, address: str, port: ExecutionPort) -> None:
        """Register an execution port for a specific wallet address."""
        self._execution_ports[address] = port

    async def execute_simultaneous(
        self,
        *,
        target_wallet_addresses: Sequence[str],
        market_id: str,
        side: str,
        amount_lamports_per_wallet: Lamports | None = None,
        base_units_per_wallet: int | None = None,
        max_slippage_bps: int = 1000,
        as_of_slot: Slot = DEFAULT_SLOT,
        reason_codes: tuple[str, ...] = ("manual_quick_trade",),
    ) -> MultiWalletReceipt:
        """Dispatch simultaneous buy/sell orders across all specified wallets."""
        tasks: list[asyncio.Task[ExecutionReceipt]] = []
        selected_addresses: list[str] = []

        for address in target_wallet_addresses:
            port = self._execution_ports.get(address)
            intent = ExecutionIntent(
                intent_id=f"multi_{side}_{address[:8]}_{as_of_slot}",
                as_of_slot=as_of_slot,
                market_id=market_id,
                side="buy" if side == "buy" else "sell",
                quote_amount_base_units=(
                    int(amount_lamports_per_wallet)
                    if amount_lamports_per_wallet is not None
                    else None
                ),
                base_amount_base_units=base_units_per_wallet,
                max_slippage_bps=max_slippage_bps,
                reason_codes=reason_codes,
            )

            if port is None:

                async def _missing_port_submit(
                    it: ExecutionIntent = intent,
                ) -> ExecutionReceipt:
                    return non_submitting_receipt(
                        mode=self._mode,
                        intent=it,
                        estimated_fee_lamports=Lamports(0),
                        message="execution port is not configured; trade abstained",
                    )

                tasks.append(asyncio.create_task(_missing_port_submit()))
            else:
                tasks.append(asyncio.create_task(port.submit(intent)))
            selected_addresses.append(address)

        if not tasks:
            return MultiWalletReceipt(
                market_id=market_id,
                side=side,
                total_quote_lamports=Lamports(0),
                total_base_units=0,
                receipts=(),
                successful_count=0,
                failed_count=0,
            )

        results = await asyncio.gather(*tasks, return_exceptions=True)
        paired_receipts: list[tuple[str, ExecutionReceipt]] = []
        success_count = 0
        fail_count = 0

        for addr, res in zip(selected_addresses, results, strict=False):
            if isinstance(res, ExecutionReceipt):
                paired_receipts.append((addr, res))
                if res.accepted:
                    success_count += 1
                else:
                    fail_count += 1
            else:
                fail_receipt = non_submitting_receipt(
                    mode=self._mode,
                    intent=None,
                    message=f"Execution failed with exception: {res}",
                )
                paired_receipts.append((addr, fail_receipt))
                fail_count += 1

        total_quote = Lamports(int(amount_lamports_per_wallet or 0) * success_count)
        total_base = (base_units_per_wallet or 0) * success_count

        return MultiWalletReceipt(
            market_id=market_id,
            side=side,
            total_quote_lamports=total_quote,
            total_base_units=total_base,
            receipts=tuple(paired_receipts),
            successful_count=success_count,
            failed_count=fail_count,
        )
