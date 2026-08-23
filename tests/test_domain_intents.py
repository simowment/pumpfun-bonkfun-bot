"""Comprehensive tests for domain intents, idempotency, and RugbotCore facade queries."""

from __future__ import annotations

import asyncio
from pathlib import Path

from rugbot.domain.amounts import Lamports, Slot
from rugbot.domain.intents import (
    BuyIntent,
    ChainCommitment,
    EconomicLifecycleState,
    ExitIntent,
    compute_buy_intent_id,
    compute_exit_intent_id,
)
from rugbot.runtime.app import build_ui_runtime
from rugbot.tracker.models import (
    LaunchRecord,
    TargetExecutionMode,
    TargetExecutionPolicy,
    TransferRecord,
    WalletRecord,
    WalletStatus,
)


def test_buy_intent_idempotent_hashing() -> None:
    """Verify compute_buy_intent_id produces identical deterministic hashes for matching launches."""
    id1 = compute_buy_intent_id(
        target_id="83t4Poe7iZ543Q14jR4vQvJjHkn928Zk395u63LRL8f1",
        mint="DOGE69E9CqsGL5uXPASB853f87ox8nZVgW7ucoeYMC4pump",
        launch_signature="4SU3jkLsm92kLqP41jKsd831jkLsm92kLqP41jKsd831jkLsm92kLqP41jKsd831",
        instruction_index=0,
        amount_lamports=100_000_000,
    )
    id2 = compute_buy_intent_id(
        target_id="83t4Poe7iZ543Q14jR4vQvJjHkn928Zk395u63LRL8f1",
        mint="DOGE69E9CqsGL5uXPASB853f87ox8nZVgW7ucoeYMC4pump",
        launch_signature="4SU3jkLsm92kLqP41jKsd831jkLsm92kLqP41jKsd831jkLsm92kLqP41jKsd831",
        instruction_index=0,
        amount_lamports=100_000_000,
    )
    assert id1 == id2
    assert id1.startswith("buy-")

    # Different quote size produces distinct intent ID
    id3 = compute_buy_intent_id(
        target_id="83t4Poe7iZ543Q14jR4vQvJjHkn928Zk395u63LRL8f1",
        mint="DOGE69E9CqsGL5uXPASB853f87ox8nZVgW7ucoeYMC4pump",
        launch_signature="4SU3jkLsm92kLqP41jKsd831jkLsm92kLqP41jKsd831jkLsm92kLqP41jKsd831",
        instruction_index=0,
        amount_lamports=200_000_000,
    )
    assert id1 != id3


def test_exit_intent_hashing() -> None:
    """Verify compute_exit_intent_id produces deterministic hashes."""
    id1 = compute_exit_intent_id(
        position_id="pos-123",
        fraction_ppm=500_000,
        trigger_slot=312450000,
    )
    id2 = compute_exit_intent_id(
        position_id="pos-123",
        fraction_ppm=500_000,
        trigger_slot=312450000,
    )
    assert id1 == id2
    assert id1.startswith("exit-")


def test_buy_intent_lifecycle_states() -> None:
    """Verify BuyIntent fields and commitment enum values."""
    intent = BuyIntent(
        id="buy-test-1",
        target_id="83t4Poe7iZ543Q14jR4vQvJjHkn928Zk395u63LRL8f1",
        mint="DOGE69E9CqsGL5uXPASB853f87ox8nZVgW7ucoeYMC4pump",
        launch_signature="sig123",
        instruction_index=0,
        amount_lamports=Lamports(50_000_000),
        max_slippage_bps=250,
        priority_fee_microlamports=50_000,
        jito_tip_lamports=Lamports(1_000_000),
        created_at_slot=Slot(312450000),
        created_at_timestamp=1724000100,
        state=EconomicLifecycleState.INTENT_CREATED,
        tx_signature=None,
        chain_commitment=ChainCommitment.CONFIRMED,
    )
    assert intent.state == EconomicLifecycleState.INTENT_CREATED
    assert intent.chain_commitment == ChainCommitment.CONFIRMED


def test_exit_intent_contract() -> None:
    """Verify ExitIntent dataclass defaults and properties."""
    exit_order = ExitIntent(
        id="exit-test-1",
        position_id="pos-1",
        market_id="mint-123",
        fraction_ppm=1_000_000,
        reason="take_profit_hit",
        created_at_slot=Slot(312450100),
        created_at_timestamp=1724000200,
    )
    assert exit_order.state == EconomicLifecycleState.EXIT_INTENT
    assert exit_order.fraction_ppm == 1_000_000


def test_rugbot_core_facade_queries(tmp_path: object) -> None:
    """Verify RugbotCore provides comprehensive, decoupled query/mutation methods."""

    async def _run() -> None:
        state_dir = Path(str(tmp_path))
        core = build_ui_runtime(state_dir=state_dir, endpoint="")

        try:
            root_addr = "83t4Poe7iZ543Q14jR4vQvJjHkn928Zk395u63LRL8f1"
            sat_addr = "6jRSplJ892kd831jkLsm92kLqP41jKsd831jkLJXF5V9"

            # 1. Watch / unwatch facade
            watch_res = core.watch(root_addr, label="Alpha Dev")
            assert watch_res.ok is True

            funder = core.get_funder(root_addr)
            assert funder is not None
            assert funder.address == root_addr
            assert funder.label == "Alpha Dev"

            # 2. Save and query target execution policy
            policy = TargetExecutionPolicy(
                funder_address=root_addr,
                monitoring_enabled=True,
                execution_mode=TargetExecutionMode.SIMULATED,
                quote_size_lamports=50_000_000,
                take_profit_pnl_ppm=500_000,
                stop_loss_pnl_ppm=-200_000,
                max_slippage_bps=300,
                priority_fee_microlamports=100_000,
                jito_tip_lamports=1_000_000,
                updated_at="2026-08-22T02:00:00",
            )
            core.save_target_execution_policy(policy)
            saved_policy = core.get_target_execution_policy(root_addr)
            assert saved_policy is not None
            assert saved_policy.quote_size_lamports == 50_000_000
            assert saved_policy.execution_mode == TargetExecutionMode.SIMULATED

            # 3. Seed wallet, transfer, launch in repository directly for query test
            core._repository.save_wallet(
                WalletRecord(
                    address=sat_addr,
                    root_funder=root_addr,
                    parent_wallet=root_addr,
                    depth=1,
                    status=WalletStatus.FUNDED,
                    discovered_at="2026-08-22T02:05:00",
                    expires_at=None,
                    last_active_at="2026-08-22T02:10:00",
                )
            )
            core._repository.save_transfer(
                TransferRecord(
                    signature="sig-transfer-1",
                    instruction_index=0,
                    slot=312450000,
                    timestamp=1724000100,
                    from_wallet=root_addr,
                    to_wallet=sat_addr,
                    amount_lamports=1_000_000_000,
                    amount_sol=1.0,
                    root_funder=root_addr,
                    depth=1,
                )
            )
            core._repository.save_launch(
                LaunchRecord(
                    mint="MINT123456789012345678901234567890123456789",
                    creator_wallet=sat_addr,
                    root_funder=root_addr,
                    symbol="PEPE",
                    name="PepeCoin",
                    created_signature="sig-launch-1",
                    created_slot=312450050,
                    created_at=1724000150,
                    depth=1,
                    funding_signature="sig-transfer-1",
                    funding_amount_lamports=1_000_000_000,
                    funding_timestamp=1724000100,
                )
            )

            # 4. Facade queries
            wallets = core.wallets()
            assert len(wallets) >= 1
            assert len(core.funders()) >= 1

            descendants = core.get_descendant_wallets(root_addr)
            assert len(descendants) == 1
            assert descendants[0].address == sat_addr

            launches = core.get_launches_for_funder(root_addr)
            assert len(launches) == 1
            assert launches[0].symbol == "PEPE"

            transfers = core.get_transfers_for_funder(root_addr)
            assert len(transfers) == 1
            assert transfers[0].signature == "sig-transfer-1"

            stats = core.get_summary_stats()
            assert stats["funders_count"] >= 1
            assert stats["wallets_count"] >= 1
            assert stats["launches_count"] >= 1

            # 5. Cluster intelligence projection via core
            intel = core.get_cluster_intelligence(root_addr, root_label="Alpha Dev")
            assert intel.root_address == root_addr
            assert len(intel.discovered_wallets) >= 2
            assert root_addr in intel.dossiers
            assert sat_addr in intel.dossiers

        finally:
            await core.close()

    asyncio.run(_run())
