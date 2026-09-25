"""Tests for the on-chain insider cabal intelligence pipeline.

Covers:
1. Address validation and early buyer extraction.
2. Transitive clustering by funding origin with N >= 10 token histories.
3. CabalStore persistence and querying.
4. Multi-factor signal filtering (token age, liquidity, confluence, conviction sizing).
5. Stealth WalletPool rotation and concurrency tracking.
6. CabalExecutor entry, TP ladder, and trailing stop lifecycle.
7. CabalPipeline integration, alerting, and CLI subcommands.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from rugbot.analysis.wallet_registry import WalletRegistry
from rugbot.discover.cabal import (
    CabalCluster,
    CabalDiscoveryError,
    CabalStore,
    EarlyBuyerRecord,
    cluster_early_buyers,
    extract_early_buyers,
    sync_clusters_to_stores,
    validate_solana_address,
)
from rugbot.execution.cabal_executor import CabalExecutor
from rugbot.execution.wallet_pool import RotationPolicy, WalletPool
from rugbot.integrations.pumpfun import PumpPortalStream
from rugbot.intelligence.signal_filter import (
    SignalDecision,
    SignalFilter,
    SignalFilterConfig,
)
from rugbot.interfaces.cli.cabal import (
    _replay_candlestick_series,
    build_parser,
    main,
    run_backtest,
    run_list,
    run_trades,
)
from rugbot.runtime.cabal_pipeline import (
    CabalPipeline,
    PositionSizingConfig,
    SizingMode,
    send_discord_cabal_alert,
    send_telegram_cabal_alert,
)
from rugbot.storage.database import DatabaseManager
from rugbot.storage.tracker import SQLiteTrackerRepository

# Canonical 32-byte base58 addresses for testing
ADDR_FUNDER = "11111111111111111111111111111111"
ADDR_BUYER_1 = "4Nd1mBQtrMJVYVfKf2PJy9NZd2A2bbCeZW5DhN9jbjAo"
ADDR_BUYER_2 = "8FRcba74rn4Eb4eA4x4gQ2dG6q2u5qN7FwH9C2m1P3vR"
ADDR_BUYER_3 = "F2reRYPQUPkagQy7t5VaZgtiHqXNFQ91ej15GRypump"
MINT_WINNER_A = "So11111111111111111111111111111111111111112"
MINT_WINNER_B = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"


def test_validate_solana_address() -> None:
    """Validate 32-byte base58 address checks."""
    assert validate_solana_address(ADDR_BUYER_1) == ADDR_BUYER_1
    with pytest.raises(CabalDiscoveryError):
        validate_solana_address("invalid_address_not_base58")
    with pytest.raises(CabalDiscoveryError):
        validate_solana_address("short")


def test_extract_early_buyers() -> None:
    """Extract early buyers from trade API response in chronological order."""
    mock_client = MagicMock()
    # Trades from API are newest-first
    mock_client.fetch_trades.return_value = {
        "trades": [
            {
                "type": "buy",
                "userAddress": ADDR_BUYER_2,
                "amountSol": 2.5,
                "timestamp": "2026-09-13T20:00:03Z",
            },
            {
                "type": "sell",
                "userAddress": ADDR_BUYER_1,
                "amountSol": 1.0,
                "timestamp": "2026-09-13T20:00:02Z",
            },
            {
                "type": "buy",
                "userAddress": ADDR_BUYER_1,
                "amountSol": 5.0,
                "timestamp": "2026-09-13T20:00:01Z",
            },
        ]
    }

    buyers = extract_early_buyers(mock_client, MINT_WINNER_A, buyer_limit=5)
    assert len(buyers) == 2
    # Oldest first: ADDR_BUYER_1 first, then ADDR_BUYER_2
    assert buyers[0].wallet == ADDR_BUYER_1
    assert buyers[0].amount_sol == 5.0
    assert buyers[1].wallet == ADDR_BUYER_2
    assert buyers[1].amount_sol == 2.5


def test_cluster_early_buyers_transitive_funder() -> None:
    """Cluster wallets by common funding source and compute N >= 10 history metrics."""
    buyers_token_a = [
        EarlyBuyerRecord(
            wallet=ADDR_BUYER_1, mint=MINT_WINNER_A, amount_sol=3.0, timestamp="t1"
        ),
        EarlyBuyerRecord(
            wallet=ADDR_BUYER_2, mint=MINT_WINNER_A, amount_sol=4.0, timestamp="t1"
        ),
    ]
    buyers_token_b = [
        EarlyBuyerRecord(
            wallet=ADDR_BUYER_1, mint=MINT_WINNER_B, amount_sol=5.0, timestamp="t2"
        ),
        EarlyBuyerRecord(
            wallet=ADDR_BUYER_2, mint=MINT_WINNER_B, amount_sol=2.0, timestamp="t2"
        ),
    ]

    mock_client = MagicMock()
    # Mock historical tokens for funder
    mock_client.fetch_user_created_coins.return_value = {
        "coins": [
            {
                "mint": f"token_{i}",
                "market_cap": 5000,
                "ath_market_cap": 25000,
                "ath": 5.0,
            }
            for i in range(12)
        ]
    }

    with patch(
        "rugbot.discover.cabal.find_outbound_funding_edge",
        return_value=(ADDR_FUNDER, 1.5, "sig_123"),
    ):
        clusters = cluster_early_buyers(
            [(MINT_WINNER_A, buyers_token_a), (MINT_WINNER_B, buyers_token_b)],
            min_winners=2,
            client=mock_client,
        )

    assert len(clusters) == 1
    cluster = clusters[0]
    assert cluster.funder == ADDR_FUNDER
    assert ADDR_BUYER_1 in cluster.wallets
    assert ADDR_BUYER_2 in cluster.wallets
    assert cluster.token_count >= 12  # N >= 10 tokens requirement satisfied
    assert cluster.median_ath >= 5.0
    assert cluster.winrate_2x == 100.0
    assert cluster.winrate_5x == 100.0
    assert cluster.typical_buy_sol > 0.0


def test_cabal_store_roundtrip(tmp_path: Path) -> None:
    """Test SQLite store persistence and querying."""
    db_path = tmp_path / "cabal_test.sqlite3"
    store = CabalStore(db_path=db_path)

    cluster = CabalCluster(
        cluster_id="cabal-001-test",
        funder=ADDR_FUNDER,
        wallets=frozenset([ADDR_BUYER_1, ADDR_BUYER_2]),
        winner_tokens=frozenset([MINT_WINNER_A]),
        token_count=15,
        median_ath=4.5,
        mean_ath=5.2,
        winrate_2x=80.0,
        winrate_5x=40.0,
        avg_time_to_peak_sec=140.0,
        typical_buy_sol=3.5,
        tokens=({"mint": "t1", "ath": 4.5},),
        discovered_at="2026-09-13T20:00:00Z",
    )

    count = store.save_clusters([cluster])
    assert count == 1

    loaded = store.list_clusters()
    assert len(loaded) == 1
    assert loaded[0].cluster_id == "cabal-001-test"
    assert loaded[0].token_count == 15
    assert loaded[0].median_ath == 4.5
    assert loaded[0].funder == ADDR_FUNDER

    by_id = store.get_cluster("cabal-001-test")
    assert by_id is not None
    assert by_id.cluster_id == "cabal-001-test"

    by_funder = store.get_cluster(ADDR_FUNDER)
    assert by_funder is not None
    assert by_funder.funder == ADDR_FUNDER


def test_signal_filter_gates() -> None:
    """Test signal quality filters: token age, liquidity, confluence, and conviction."""
    sig_filter = SignalFilter(
        config=SignalFilterConfig(
            max_token_age_seconds=300.0,
            min_liquidity_usd=5000.0,
            confluence_window_seconds=30.0,
            min_conviction_ratio=0.5,
        )
    )

    dummy_cluster = CabalCluster(
        cluster_id="cabal-001",
        funder=ADDR_FUNDER,
        wallets=frozenset([ADDR_BUYER_1, ADDR_BUYER_2]),
        winner_tokens=frozenset([MINT_WINNER_A]),
        token_count=10,
        median_ath=3.0,
        mean_ath=3.0,
        winrate_2x=50.0,
        winrate_5x=20.0,
        avg_time_to_peak_sec=120.0,
        typical_buy_sol=5.0,  # Historical typical buy = 5 SOL
        tokens=(),
        discovered_at="2026-09-13T20:00:00Z",
    )

    now = 10000.0

    # Test 1: Token too old (>300s)
    dec_old = sig_filter.evaluate_signal(
        mint=MINT_WINNER_A,
        wallet=ADDR_BUYER_1,
        amount_sol=5.0,
        token_created_time=now - 500.0,  # 500s old
        current_liquidity_usd=10000.0,
        cabal_cluster=dummy_cluster,
        current_time=now,
    )
    assert dec_old.action == "ABSTAIN"
    assert any("token_too_old" in r for r in dec_old.reasons)

    # Test 2: Insufficient liquidity (<$5k)
    dec_illiquid = sig_filter.evaluate_signal(
        mint=MINT_WINNER_A,
        wallet=ADDR_BUYER_1,
        amount_sol=5.0,
        token_created_time=now - 60.0,
        current_liquidity_usd=2000.0,  # Below $5k
        cabal_cluster=dummy_cluster,
        current_time=now,
    )
    assert dec_illiquid.action == "ABSTAIN"
    assert any("insufficient_liquidity" in r for r in dec_illiquid.reasons)

    # Test 3: Low-conviction spray (0.1 SOL vs 5.0 SOL typical = 2% < 50%)
    dec_spray = sig_filter.evaluate_signal(
        mint=MINT_WINNER_A,
        wallet=ADDR_BUYER_1,
        amount_sol=0.1,  # Spray
        token_created_time=now - 60.0,
        current_liquidity_usd=10000.0,
        cabal_cluster=dummy_cluster,
        current_time=now,
    )
    assert dec_spray.action == "ABSTAIN"
    assert any("low_conviction_spray" in r for r in dec_spray.reasons)

    # Test 4: Valid high-conviction buy passes
    dec_buy = sig_filter.evaluate_signal(
        mint=MINT_WINNER_A,
        wallet=ADDR_BUYER_1,
        amount_sol=5.0,  # 100% conviction
        token_created_time=now - 60.0,
        current_liquidity_usd=15000.0,
        cabal_cluster=dummy_cluster,
        current_time=now,
    )
    assert dec_buy.action == "BUY"
    assert dec_buy.confluence_count == 1
    assert dec_buy.score >= 1.0

    # Test 5: Confluence multi-buy (ADDR_BUYER_2 buys same token 5s later)
    dec_confluence = sig_filter.evaluate_signal(
        mint=MINT_WINNER_A,
        wallet=ADDR_BUYER_2,
        amount_sol=4.5,
        token_created_time=now - 55.0,
        current_liquidity_usd=20000.0,
        cabal_cluster=dummy_cluster,
        current_time=now + 5.0,
    )
    assert dec_confluence.action == "BUY"
    assert dec_confluence.confluence_count == 2
    assert dec_confluence.score > dec_buy.score  # Confluence multiplier applied


def test_wallet_pool_rotation() -> None:
    """Test stealth wallet pool acquisition, rotation, and release."""
    pool = WalletPool(
        wallets=[ADDR_BUYER_1, ADDR_BUYER_2],
        policy=RotationPolicy.ROUND_ROBIN,
        max_concurrent_per_wallet=2,
    )
    assert pool.total_wallets == 2

    w1 = pool.acquire_wallet()
    w2 = pool.acquire_wallet()
    assert w1.address != w2.address  # Rotated between the two wallets

    # Release w1 and verify concurrency tracking
    assert w1.active_trades_count == 1
    pool.release_wallet(w1.address)
    assert w1.active_trades_count == 0


def test_cabal_executor_lifecycle() -> None:
    """Test CabalExecutor entry, take-profit ladder, and trailing stop exits."""

    async def _run() -> None:
        pool = WalletPool(wallets=[ADDR_BUYER_1])
        executor = CabalExecutor(
            wallet_pool=pool,
            trailing_stop_pct=15.0,
            tp_levels=((100.0, 0.5),),  # Sell 50% at 2x (+100%)
        )

        # 1. Enter Position
        entry_price = 0.0001
        pos, _ = await executor.enter_position(
            mint=MINT_WINNER_A,
            cabal_cluster_id="cabal-001",
            amount_sol=0.5,
            initial_price_sol=entry_price,
        )
        assert pos.mint == MINT_WINNER_A
        assert not pos.is_closed
        assert pos.remaining_tokens > 0

        # 2. Price hits 2x (0.0002) -> TP tier triggers
        pos_tp, receipt_tp = await executor.update_price_tick(pos.position_id, 0.0002)
        assert receipt_tp is not None
        assert pos_tp.remaining_tokens < pos.token_amount  # 50% sold
        assert pos_tp.realized_pnl_sol > 0.0
        assert not pos_tp.is_closed

        # 3. Price drops 20% from peak 0.0002 -> Trailing stop triggers
        pos_exit, receipt_exit = await executor.update_price_tick(
            pos.position_id, 0.00016
        )
        assert receipt_exit is not None
        assert pos_exit.is_closed
        assert pos_exit.exit_reason == "trailing_stop"
        assert pos_exit.remaining_tokens == 0.0

    asyncio.run(_run())


def test_cabal_pipeline_execution(tmp_path: Path) -> None:
    """Test full pipeline trade handling, gating, and paper execution."""

    async def _run() -> None:
        db_path = tmp_path / "pipeline_test.sqlite3"
        store = CabalStore(db_path=db_path)

        cluster = CabalCluster(
            cluster_id="cabal-001",
            funder=ADDR_FUNDER,
            wallets=frozenset([ADDR_BUYER_1]),
            winner_tokens=frozenset([MINT_WINNER_A]),
            token_count=10,
            median_ath=3.0,
            mean_ath=3.0,
            winrate_2x=70.0,
            winrate_5x=30.0,
            avg_time_to_peak_sec=120.0,
            typical_buy_sol=2.0,
            tokens=(),
            discovered_at="2026-09-13T20:00:00Z",
        )
        store.save_clusters([cluster])

        pipeline = CabalPipeline(
            store=store,
            signal_filter=SignalFilter(),
            executor=CabalExecutor(wallet_pool=WalletPool(wallets=[ADDR_BUYER_2])),
        )
        pipeline.reload_clusters()

        now = time.time()
        # High-conviction buy: passes filter and enters paper trade
        dec, pos = await pipeline.handle_detected_trade(
            mint=MINT_WINNER_A,
            buyer_wallet=ADDR_BUYER_1,
            amount_sol=2.0,
            token_created_time=now - 30.0,
            market_cap_usd=10000.0,
            initial_price_sol=0.00003,
        )
        assert dec.action == "BUY"
        assert pos is not None
        assert pos.mint == MINT_WINNER_A
        assert len(pipeline.executor.active_positions) == 1

    asyncio.run(_run())


def test_cli_parser_and_list(tmp_path: Path) -> None:
    """Test CLI parser and instant list command."""
    parser = build_parser()
    args_discover = parser.parse_args(["discover", "--min-winners", "3"])
    assert args_discover.command == "discover"
    assert args_discover.min_winners == 3

    args_list = parser.parse_args(["list", "--db", str(tmp_path / "db.sqlite3")])
    assert args_list.command == "list"
    # Should run cleanly and return 0 even when db empty
    assert run_list(args_list) == 0


def test_alert_functions_fail_soft() -> None:
    """Test webhook and telegram alert helpers fail soft without throwing."""
    decision = SignalDecision(
        action="BUY",
        score=2.0,
        confluence_count=1,
        conviction_ratio=1.0,
        reasons=(),
        mint=MINT_WINNER_A,
        wallet=ADDR_BUYER_1,
        amount_sol=1.0,
    )
    # Empty webhooks return False cleanly without network errors
    assert not send_discord_cabal_alert("", decision, None)
    assert not send_telegram_cabal_alert("", "", decision, None)


def test_profitable_preset_gating() -> None:
    """Verify that profitable preset strictly enforces confluence, conviction, and sizing."""
    cfg = SignalFilterConfig.profitable_preset()
    assert cfg.require_confluence is True
    assert cfg.min_confluence_wallets == 2
    assert cfg.min_buy_sol == 0.10
    assert cfg.min_cluster_typical_buy_sol == 0.10
    assert cfg.min_conviction_ratio == 0.80

    filt = SignalFilter(config=cfg)
    now = 10000.0

    good_cluster = CabalCluster(
        cluster_id="cabal-good",
        funder=ADDR_FUNDER,
        wallets=frozenset([ADDR_BUYER_1, ADDR_BUYER_2]),
        winner_tokens=frozenset([MINT_WINNER_A]),
        token_count=10,
        median_ath=3.0,
        mean_ath=3.0,
        winrate_2x=70.0,
        winrate_5x=30.0,
        avg_time_to_peak_sec=120.0,
        typical_buy_sol=0.50,
        tokens=(),
        discovered_at="2026-09-13T20:00:00Z",
    )

    dust_cluster = CabalCluster(
        cluster_id="cabal-dust",
        funder=ADDR_FUNDER,
        wallets=frozenset([ADDR_BUYER_3]),
        winner_tokens=frozenset([MINT_WINNER_A]),
        token_count=10,
        median_ath=3.0,
        mean_ath=3.0,
        winrate_2x=70.0,
        winrate_5x=30.0,
        avg_time_to_peak_sec=120.0,
        typical_buy_sol=0.001,  # Dust sprayer cluster
        tokens=(),
        discovered_at="2026-09-13T20:00:00Z",
    )

    # 1. Solo buy with require_confluence=True -> Abstains (insufficient confluence)
    dec_solo = filt.evaluate_signal(
        mint=MINT_WINNER_A,
        wallet=ADDR_BUYER_1,
        amount_sol=0.50,
        token_created_time=now - 30.0,
        current_liquidity_usd=10000.0,
        cabal_cluster=good_cluster,
        current_time=now,
    )
    assert dec_solo.action == "ABSTAIN"
    assert any("insufficient_confluence" in r for r in dec_solo.reasons)

    # 2. Confluent buy from 2nd wallet within 30s -> PASSES (action == BUY)
    dec_conf = filt.evaluate_signal(
        mint=MINT_WINNER_A,
        wallet=ADDR_BUYER_2,
        amount_sol=0.50,
        token_created_time=now - 25.0,
        current_liquidity_usd=15000.0,
        cabal_cluster=good_cluster,
        current_time=now + 5.0,
    )
    assert dec_conf.action == "BUY"
    assert dec_conf.confluence_count == 2

    # 3. Dust cluster -> Abstains (dust_cluster_excluded)
    dec_dust = filt.evaluate_signal(
        mint=MINT_WINNER_A,
        wallet=ADDR_BUYER_3,
        amount_sol=0.50,
        token_created_time=now - 20.0,
        current_liquidity_usd=15000.0,
        cabal_cluster=dust_cluster,
        current_time=now + 10.0,
    )
    assert dec_dust.action == "ABSTAIN"
    assert any("dust_cluster_excluded" in r for r in dec_dust.reasons)

    # 4. Micro buy (< 0.10 SOL) -> Abstains (below_min_buy_sol)
    dec_micro = filt.evaluate_signal(
        mint=MINT_WINNER_A,
        wallet=ADDR_BUYER_1,
        amount_sol=0.02,
        token_created_time=now - 20.0,
        current_liquidity_usd=15000.0,
        cabal_cluster=good_cluster,
        current_time=now + 10.0,
    )
    assert dec_micro.action == "ABSTAIN"
    assert any("below_min_buy_sol" in r for r in dec_micro.reasons)


def test_insider_dump_adverse_exit(tmp_path: Path) -> None:
    """Verify that when an insider dumps, open positions in that mint liquidate immediately."""

    async def _run() -> None:
        db_path = tmp_path / "dump_test.sqlite3"
        store = CabalStore(db_path=db_path)
        cluster = CabalCluster(
            cluster_id="cabal-001",
            funder=ADDR_FUNDER,
            wallets=frozenset([ADDR_BUYER_1, ADDR_BUYER_2]),
            winner_tokens=frozenset([MINT_WINNER_A]),
            token_count=10,
            median_ath=3.0,
            mean_ath=3.0,
            winrate_2x=70.0,
            winrate_5x=30.0,
            avg_time_to_peak_sec=120.0,
            typical_buy_sol=0.50,
            tokens=(),
            discovered_at="2026-09-13T20:00:00Z",
        )
        store.save_clusters([cluster])

        pool = WalletPool(wallets=[ADDR_BUYER_3])
        pipeline = CabalPipeline(
            store=store,
            signal_filter=SignalFilter(config=SignalFilterConfig(min_buy_sol=0.1)),
            executor=CabalExecutor(wallet_pool=pool),
        )
        pipeline.reload_clusters()

        # 1. Enter paper trade for MINT_WINNER_A
        pos, _ = await pipeline.executor.enter_position(
            mint=MINT_WINNER_A,
            cabal_cluster_id="cabal-001",
            amount_sol=0.50,
            initial_price_sol=0.0001,
        )
        assert not pos.is_closed

        # 2. Insider ADDR_BUYER_1 dumps tokens
        sell_payload = {
            "txType": "sell",
            "mint": MINT_WINNER_A,
            "traderPublicKey": ADDR_BUYER_1,
            "solAmount": 0.40,
            "tokenAmount": 5000,
        }
        await pipeline.handle_stream_payload(sell_payload)

        # 3. Position must be closed immediately with reason insider_dump_detected
        closed_pos = pipeline.executor.get_position(pos.position_id)
        assert closed_pos is not None
        assert closed_pos.is_closed is True
        assert closed_pos.exit_reason == "insider_dump_detected"

    asyncio.run(_run())


def test_position_sizing_modes() -> None:
    """Verify position sizing models: fixed, proportional, balance percentage, and caps."""
    # 1. FIXED mode
    fixed_cfg = PositionSizingConfig(mode=SizingMode.FIXED, fixed_size_sol=0.30)
    assert (
        fixed_cfg.calculate_entry_size(target_buy_sol=2.0, available_balance_sol=5.0)
        == 0.30
    )

    # 2. PROPORTIONAL mode (50% of insider buy)
    prop_cfg = PositionSizingConfig(
        mode=SizingMode.PROPORTIONAL, copy_ratio=0.50, max_position_sol=1.00
    )
    assert (
        prop_cfg.calculate_entry_size(target_buy_sol=1.2, available_balance_sol=5.0)
        == 0.60
    )
    # Ceiling test: 5.0 SOL * 50% = 2.5 SOL, capped at 1.00 SOL
    assert (
        prop_cfg.calculate_entry_size(target_buy_sol=5.0, available_balance_sol=5.0)
        == 1.00
    )

    # 3. BALANCE_PCT mode (10% of portfolio)
    bal_cfg = PositionSizingConfig(
        mode=SizingMode.BALANCE_PCT, balance_pct=10.0, max_position_sol=1.00
    )
    assert (
        bal_cfg.calculate_entry_size(target_buy_sol=1.0, available_balance_sol=4.0)
        == 0.40
    )

    # 4. Gas reserve protection: Balance = 0.20 SOL, gas reserve = 0.05 SOL -> spendable = 0.15 SOL
    gas_cfg = PositionSizingConfig(
        mode=SizingMode.FIXED, fixed_size_sol=0.50, gas_reserve_sol=0.05
    )
    assert (
        gas_cfg.calculate_entry_size(target_buy_sol=1.0, available_balance_sol=0.20)
        == 0.15
    )

    # 5. CLI parser args validation
    parser = build_parser()
    args = parser.parse_args(
        [
            "dryrun",
            "--size-mode",
            "proportional",
            "--copy-ratio",
            "0.40",
            "--max-size-sol",
            "0.80",
        ]
    )
    assert args.size_mode == "proportional"
    assert args.copy_ratio == 0.40
    assert args.max_size_sol == 0.80


def test_cabal_store_executions_and_history_cli(tmp_path: Path) -> None:
    """Verify bot execution recording, retrieval, and cabal history CLI."""
    db_path = tmp_path / "exec_test.sqlite3"
    store = CabalStore(db_path=db_path)

    # 1. Initially empty
    assert len(store.list_executions()) == 0

    # 2. Record mock active position
    executor = CabalExecutor(store=store)

    async def _run() -> None:
        pos, _ = await executor.enter_position(
            mint=MINT_WINNER_A,
            cabal_cluster_id="cabal-001",
            amount_sol=0.25,
            initial_price_sol=0.0001,
        )
        assert pos.position_id in [e["position_id"] for e in store.list_executions()]

        # Close position
        await executor.exit_position(
            pos.position_id,
            current_price_sol=0.0002,
            reason="take_profit_2x",
        )
        closed_exec = store.get_execution(pos.position_id)
        assert closed_exec is not None
        assert closed_exec["is_closed"] == 1
        assert closed_exec["exit_reason"] == "take_profit_2x"
        assert closed_exec["realized_pnl_sol"] > 0

    asyncio.run(_run())

    # 3. CLI trades commands
    parser = build_parser()
    args_bot = parser.parse_args(["trades", "--db", str(db_path)])
    assert run_trades(args_bot) == 0

    args_cabal = parser.parse_args(["trades", "--cabal", "--db", str(db_path)])
    assert run_trades(args_cabal) == 0


def test_pumpportal_stream_api_key_and_notice() -> None:
    """Verify PumpPortalStream formats API key into connection URL and handles status notices."""
    # 1. URL without API key
    s1 = PumpPortalStream(ws_url="wss://pumpportal.fun/api/data", api_key="")
    assert s1._ws_url == "wss://pumpportal.fun/api/data"

    # 2. URL with explicit API key
    s2 = PumpPortalStream(ws_url="wss://pumpportal.fun/api/data", api_key="my_test_key")
    assert s2._ws_url == "wss://pumpportal.fun/api/data?api-key=my_test_key"

    # 3. URL that already has query params
    s3 = PumpPortalStream(
        ws_url="wss://pumpportal.fun/api/data?foo=bar", api_key="my_test_key"
    )
    assert s3._ws_url == "wss://pumpportal.fun/api/data?foo=bar&api-key=my_test_key"


def test_cabal_pipeline_dual_ingestion_and_callbacks() -> None:
    """Verify CabalPipeline trade callbacks, signature deduplication, and insider dump alerts."""
    pipeline = CabalPipeline()
    evaluated_events: list[tuple[SignalDecision, Any, dict[str, Any]]] = []
    dump_events: list[tuple[str, str, Any]] = []

    def _on_eval(decision: SignalDecision, pos: Any, payload: dict[str, Any]) -> None:
        evaluated_events.append((decision, pos, payload))

    def _on_dump(trader: str, mint: str, closed_pos: Any) -> None:
        dump_events.append((trader, mint, closed_pos))

    pipeline.on_trade_evaluated = _on_eval
    pipeline.on_insider_dump = _on_dump

    async def _test() -> None:
        # Simulate incoming buy trade
        payload_buy = {
            "txType": "buy",
            "mint": MINT_WINNER_A,
            "traderPublicKey": ADDR_BUYER_1,
            "solAmount": 1.5,
            "tokenAmount": 100000.0,
            "signature": "sig_buy_12345",
            "source": "helius_rpc",
        }
        dec, _pos = await pipeline.handle_stream_payload(payload_buy)
        assert dec is not None
        assert "sig_buy_12345" in pipeline._seen_signatures
        assert len(evaluated_events) == 1
        assert evaluated_events[0][2]["source"] == "helius_rpc"

        # Simulate duplicate signature should be tracked
        await pipeline.handle_stream_payload(payload_buy)
        assert "sig_buy_12345" in pipeline._seen_signatures

    asyncio.run(_test())


def test_cabal_replay_candlestick_series(tmp_path: Path) -> None:
    """Verify historical replay against simulated OHLC candles triggers TP and trailing stop."""
    db_path = tmp_path / "replay_test.sqlite3"
    store = CabalStore(db_path=db_path)
    executor = CabalExecutor(
        store=store,
        trailing_stop_pct=15.0,
        tp_levels=((100.0, 0.50),),
        paper_balance_sol=2.00,
    )
    pipeline = CabalPipeline(store=store, executor=executor)

    mock_client = MagicMock()
    mock_client.fetch_token.return_value = {
        "symbol": "MOCKWIN",
        "name": "Mock Winner",
        "mint": MINT_WINNER_A,
    }
    # 3 real-format candles:
    # 0: open at 0.0001
    # 1: pump to 0.00022 (> 2.0x TP)
    # 2: peaks at 0.00025, drops to 0.00018 (peak drop 28% > 15% trail)
    mock_client.fetch_candlesticks.return_value = [
        {
            "timestamp": 100,
            "open": 0.0001,
            "high": 0.00012,
            "low": 0.00009,
            "close": 0.00011,
        },
        {
            "timestamp": 160,
            "open": 0.00011,
            "high": 0.00022,
            "low": 0.00019,
            "close": 0.00020,
        },
        {
            "timestamp": 220,
            "open": 0.00020,
            "high": 0.00025,
            "low": 0.00018,
            "close": 0.00019,
        },
    ]

    parser = build_parser()
    args = parser.parse_args(
        ["backtest", "--mint", MINT_WINNER_A, "--size-sol", "0.25", "--trail", "15.0"]
    )

    async def _test() -> None:
        result = await _replay_candlestick_series(
            pipeline, mock_client, MINT_WINNER_A, args
        )
        assert result is not None
        assert result["symbol"] == "MOCKWIN"
        assert result["candles_count"] == 3
        assert result["entry_price"] == 0.0001
        assert result["peak_price"] == 0.00025
        assert result["ath_multiplier"] == 2.5
        assert result["exit_reason"] == "trailing_stop"
        assert result["realized_pnl_sol"] > 0.0
        assert result["roi_pct"] > 0.0

    asyncio.run(_test())


def test_cabal_cli_routing(tmp_path: Path) -> None:
    """Verify canonical cabal CLI commands: discover, list, dryrun, backtest, trades."""
    db_path = tmp_path / "cli_test.sqlite3"
    parser = build_parser()

    # 1. backtest subcommand parsing
    args_backtest = parser.parse_args(
        ["backtest", "--limit", "2", "--db", str(db_path)]
    )
    assert args_backtest.command == "backtest"
    assert args_backtest.limit == 2

    # 2. dryrun subcommand parsing
    args_dryrun = parser.parse_args(["dryrun", "--seconds", "10", "--db", str(db_path)])
    assert args_dryrun.command == "dryrun"
    assert args_dryrun.seconds == 10

    # 3. Empty DB backtest should return 1 cleanly with error message
    assert run_backtest(args_backtest) == 1
    assert main(["backtest", "--limit", "2", "--db", str(db_path)]) == 1

    # 4. trades subcommand parsing and flags
    args_trades = parser.parse_args(
        ["trades", "--copy", "1", "--csv", "trades.csv", "--db", str(db_path)]
    )
    assert args_trades.command == "trades"
    assert args_trades.copy_mint == 1
    assert args_trades.csv == "trades.csv"

    # 5. list subcommand parsing
    args_list = parser.parse_args(["list", "--db", str(db_path)])
    assert args_list.command == "list"
    assert run_list(args_list) == 0

    # 6. discover subcommand parsing
    args_discover = parser.parse_args(["discover", "--min-winners", "3"])
    assert args_discover.command == "discover"
    assert args_discover.min_winners == 3

    # 7. dryrun discover flag
    args_dryrun_disc = parser.parse_args(["dryrun", "--discover", "--seconds", "5"])
    assert args_dryrun_disc.command == "dryrun"
    assert args_dryrun_disc.discover is True


def test_sync_clusters_to_stores(tmp_path: Path) -> None:
    """Verify clusters cross-sync to WalletRegistry and SQLiteTrackerRepository."""
    reg_path = tmp_path / "registry.sqlite3"
    tracker_path = tmp_path / "tracker.db"

    cluster = CabalCluster(
        cluster_id="cabal-test-01",
        funder=ADDR_FUNDER,
        wallets=frozenset([ADDR_BUYER_1, ADDR_BUYER_2]),
        winner_tokens=frozenset([MINT_WINNER_A]),
        token_count=1,
        median_ath=3.5,
        mean_ath=3.5,
        winrate_2x=100.0,
        winrate_5x=50.0,
        avg_time_to_peak_sec=45.0,
        typical_buy_sol=0.25,
        tokens=(),
        discovered_at="2026-09-15T08:00:00Z",
    )

    wallets_synced, funders_synced = sync_clusters_to_stores(
        [cluster],
        registry_path=reg_path,
        tracker_db_path=tracker_path,
    )
    assert wallets_synced == 2
    assert funders_synced == 1

    registry = WalletRegistry(reg_path)
    assert registry.count() == 2
    assert registry.get(ADDR_BUYER_1) is not None
    assert registry.get(ADDR_BUYER_2) is not None
    registry.close()

    db_mgr = DatabaseManager(tracker_path)
    repo = SQLiteTrackerRepository(db_mgr)
    funder_record = repo.get_funder(ADDR_FUNDER)
    assert funder_record is not None
    assert funder_record.label == "cabal:cabal-test-01"
    db_mgr.close()


def test_cabal_pipeline_reloads_wallets_from_registry(tmp_path: Path) -> None:
    """Verify CabalPipeline.reload_clusters incorporates enabled wallets from WalletRegistry."""
    cabal_db = tmp_path / "cabal.sqlite3"
    reg_db = tmp_path / "registry.sqlite3"

    store = CabalStore(cabal_db)
    cluster = CabalCluster(
        cluster_id="cabal-001",
        funder=ADDR_FUNDER,
        wallets=frozenset([ADDR_BUYER_1]),
        winner_tokens=frozenset(),
        token_count=1,
        median_ath=2.0,
        mean_ath=2.0,
        winrate_2x=50.0,
        winrate_5x=0.0,
        avg_time_to_peak_sec=30.0,
        typical_buy_sol=0.10,
        tokens=(),
        discovered_at="2026-09-15T08:00:00Z",
    )
    store.save_clusters([cluster], sync_stores=False)

    registry = WalletRegistry(reg_db)
    registry.add(ADDR_BUYER_2, quote_sol=0.30)
    registry.close()

    pipeline = CabalPipeline(store=store)
    tracked_count = pipeline.reload_clusters(registry_path=reg_db)
    assert tracked_count == 2
    assert ADDR_BUYER_1 in pipeline.tracked_wallets
    assert ADDR_BUYER_2 in pipeline.tracked_wallets


def test_cabal_executor_live_mode_fail_closed() -> None:
    """CabalExecutor must fail closed per AGENTS.md §7 if live_mode is enabled without keys."""
    with pytest.raises(RuntimeError, match="Live execution requested"):
        CabalExecutor(live_mode=True)
