"""Tests for multi-wallet execution, Quick Buy presets, and Fast Sell."""

import asyncio

from rugbot.domain.amounts import Lamports, Slot
from rugbot.execution.multi_wallet import ExecutionWalletConfig, MultiWalletExecutor
from rugbot.execution.ports import ExecutionIntent, ExecutionMode, ExecutionReceipt
from rugbot.tui.widgets.quick_buy_modal import QuickBuyOrder
from rugbot.tui.widgets.sell_modal import FastSellOrder


class DummyExecutionPort:
    def __init__(self, *, should_accept: bool = True) -> None:
        self.should_accept = should_accept
        self.submitted_intents: list[ExecutionIntent] = []

    async def submit(self, intent: ExecutionIntent) -> ExecutionReceipt:
        self.submitted_intents.append(intent)
        return ExecutionReceipt(
            mode=ExecutionMode.PAPER,
            intent_id=intent.intent_id,
            as_of_slot=intent.as_of_slot,
            accepted=self.should_accept,
            would_submit_transaction=False,
            signature=f"sig_{intent.intent_id}" if self.should_accept else None,
            simulated_output_base_units=500_000,
            estimated_fee_lamports=Lamports(5000),
            message="Accepted" if self.should_accept else "Rejected",
        )


def test_multi_wallet_simultaneous_execution():
    w1 = ExecutionWalletConfig(
        label="Wallet 1", address="Addr111111111111111111111111111111111111111"
    )
    w2 = ExecutionWalletConfig(
        label="Wallet 2", address="Addr222222222222222222222222222222222222222"
    )
    w3 = ExecutionWalletConfig(
        label="Wallet 3",
        address="Addr333333333333333333333333333333333333333",
        enabled=False,
    )

    port1 = DummyExecutionPort(should_accept=True)
    port2 = DummyExecutionPort(should_accept=True)

    executor = MultiWalletExecutor(
        mode=ExecutionMode.PAPER,
        wallets=[w1, w2, w3],
        execution_ports={w1.address: port1, w2.address: port2},
    )

    assert len(executor.wallets) == 3
    assert len(executor.active_wallets) == 2

    # Simultaneous buy dispatch across 2 wallets
    receipt = asyncio.run(
        executor.execute_simultaneous(
            target_wallet_addresses=[w1.address, w2.address],
            market_id="Mint111111111111111111111111111111111111111",
            side="buy",
            amount_lamports_per_wallet=Lamports(100_000_000),  # 0.1 SOL
            max_slippage_bps=1000,
            as_of_slot=Slot(1000),
        )
    )

    assert receipt.successful_count == 2
    assert receipt.failed_count == 0
    assert int(receipt.total_quote_lamports) == 200_000_000
    assert len(port1.submitted_intents) == 1
    assert len(port2.submitted_intents) == 1
    assert port1.submitted_intents[0].quote_amount_base_units == 100_000_000


def test_missing_live_port_abstains_without_fake_submission():
    wallet = ExecutionWalletConfig(
        label="Unwired live wallet", address="AddrLive111111111111111111111111111111111"
    )
    executor = MultiWalletExecutor(
        mode=ExecutionMode.LIVE,
        wallets=[wallet],
    )

    receipt = asyncio.run(
        executor.execute_simultaneous(
            target_wallet_addresses=[wallet.address],
            market_id="Mint111111111111111111111111111111111111111",
            side="buy",
            amount_lamports_per_wallet=Lamports(1_000_000),
            as_of_slot=Slot(10),
        )
    )

    assert receipt.successful_count == 0
    assert receipt.failed_count == 1
    _, execution_receipt = receipt.receipts[0]
    assert execution_receipt.accepted is False
    assert execution_receipt.would_submit_transaction is False
    assert execution_receipt.signature is None


def test_quick_buy_order_creation():
    order = QuickBuyOrder(
        market_id="MintPumpFun111111111111111111111111111111111",
        amount_lamports=Lamports(500_000_000),  # 0.5 SOL
        selected_wallet_addresses=("Addr1", "Addr2"),
        max_slippage_bps=1000,
        priority_tip_lamports=Lamports(2_000_000),
    )
    assert order.amount_lamports == Lamports(500_000_000)
    assert len(order.selected_wallet_addresses) == 2


def test_fast_sell_order_creation():
    order = FastSellOrder(
        market_id="MintPumpFun111111111111111111111111111111111",
        percentage=50,
        selected_wallet_addresses=("Addr1",),
        max_slippage_bps=1500,
        close_ata=False,
    )
    assert order.percentage == 50
    assert order.close_ata is False
