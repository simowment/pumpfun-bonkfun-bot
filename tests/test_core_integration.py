"""Core End-to-End integration suite: Token Resolution, SQLite Tracker, TP Optimizer & TUI Cockpit."""

import asyncio
from datetime import UTC, datetime
from pathlib import Path

import base58
import pytest

from rugbot.backtest.runners.cluster_optimizer import (
    HistoricalTokenSample,
    run_cluster_tp_grid_search,
)
from rugbot.intelligence.token_resolver import resolve_token_or_wallet
from rugbot.interfaces.discord import (
    CockpitHomeView,
    DiscordAdapter,
    PositionActionView,
    QuickBuyView,
    SettingsView,
)
from rugbot.interfaces.tui.app import RugbotTuiApp, TargetsTable
from rugbot.runtime.app import build_ui_runtime
from rugbot.storage.database import DatabaseManager
from rugbot.storage.tracker import SQLiteTrackerRepository
from rugbot.tracker.models import (
    FunderRecord,
    TargetExecutionMode,
    TargetExecutionPolicy,
)


def test_token_and_creator_resolution(monkeypatch: pytest.MonkeyPatch):
    """Verify on-chain token to creator dev resolution via bonding curve PDA."""
    # 1. Direct wallet input
    dev_wallet = "FJz6SLz8CQBmm692kfp6e8s9FZPuqnKX5ZNcr7k5Kadd"
    monkeypatch.setattr(
        "rugbot.intelligence.token_resolver._rpc_call",
        lambda *_args: {"value": None},
    )
    wallet_res = resolve_token_or_wallet(
        dev_wallet,
        custom_label="Alpha Dev",
        rpc_url="https://recorded.invalid",
    )
    assert wallet_res.is_token is False
    assert wallet_res.target_wallet == dev_wallet
    assert wallet_res.default_label == "Alpha Dev"

    # 2. Token input resolved via bonding curve
    mint = "BVGraUKvZydDXSAHydZvHCTFPATvcUTPoKFkocA8pump"
    pump_program = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
    accounts = [
        mint,
        *[f"Account{index}" for index in range(1, 7)],
        dev_wallet,
        *[f"Account{index}" for index in range(8, 14)],
        pump_program,
    ]

    def mock_rpc_call(
        _rpc_url: str,
        method: str,
        params: list[object],
        _transport: object,
    ):
        if method == "getAccountInfo":
            return {"value": {"data": ["mock_data", "base64"]}}
        if method == "getSignaturesForAddress":
            return [
                {"signature": "distractor_sig", "slot": 440374992},
                {"signature": "create_sig", "slot": 440374992},
            ]
        if method == "getTransaction":
            signature = params[0]
            instructions = []
            if signature == "create_sig":
                instructions = [
                    {
                        "programIdIndex": 14,
                        "accounts": list(range(14)),
                        "data": base58.b58encode(
                            bytes([24, 30, 200, 40, 5, 28, 7, 119])
                        ).decode("ascii"),
                    }
                ]
            return {
                "meta": {"loadedAddresses": {"writable": [], "readonly": []}},
                "transaction": {
                    "signatures": [signature],
                    "message": {
                        "accountKeys": accounts,
                        "instructions": instructions,
                    },
                },
            }
        return None

    monkeypatch.setattr("rugbot.intelligence.token_resolver._rpc_call", mock_rpc_call)
    monkeypatch.setattr(
        "rugbot.intelligence.token_resolver.fetch_token_metadata",
        lambda _mint: ("Alpha", "ALPHA", 0.0, 1.0),
    )
    resolved = resolve_token_or_wallet(
        mint,
        custom_label="Token Alpha",
        rpc_url="https://recorded.invalid",
    )
    assert resolved.is_token is True
    assert resolved.target_wallet == dev_wallet
    assert resolved.creation_slot == 440374992
    assert resolved.creation_signature == "create_sig"


def test_sqlite_tracker_repository_crud(tmp_path: Path):
    """Verify clean SQLite target persistence, cascade deletions, and policies."""
    db = DatabaseManager(tmp_path / "test.db")
    repo = SQLiteTrackerRepository(db)

    dev = "FJz6SLz8CQBmm692kfp6e8s9FZPuqnKX5ZNcr7k5Kadd"
    now_iso = datetime.now(UTC).isoformat()

    # Save Funder & Policy
    repo.save_funder(
        FunderRecord(
            id=0,
            address=dev,
            label="Dev Alpha",
            enabled=True,
            created_at=now_iso,
            last_seen_at=now_iso,
        )
    )
    repo.save_target_execution_policy(
        TargetExecutionPolicy(
            funder_address=dev,
            monitoring_enabled=True,
            execution_mode=TargetExecutionMode.SIMULATED,
            quote_size_lamports=25_000_000,
            take_profit_pnl_ppm=100_000,
            stop_loss_pnl_ppm=-30_000,
            max_slippage_bps=500,
            priority_fee_microlamports=50_000,
            jito_tip_lamports=1_000_000,
            updated_at=now_iso,
        )
    )

    funders = repo.get_funders()
    assert len(funders) == 1
    assert funders[0].address == dev
    assert funders[0].label == "Dev Alpha"

    # Cascade clear
    repo.clear_all_funders()
    assert len(repo.get_funders()) == 0


def test_cluster_tp_grid_optimizer():
    """Verify cluster multi-token Take-Profit optimizer under realistic -75% dump modeling."""
    root_dev = "2r2HuRi1vLzVxXnWAffWfsAMDkQpfG1c23KPDgR4wp5p"
    samples = [
        HistoricalTokenSample(
            mint="Token1",
            symbol="T1",
            creator_wallet=root_dev,
            created_slot=1000,
            created_at=1724000000,
            ath_multiplier=1.95,
            ath_delay_seconds=110,
            rug_delay_seconds=840,
            entry_mc_usd=8500.0,
            peak_mc_usd=16575.0,
        ),
        HistoricalTokenSample(
            mint="Token2",
            symbol="T2",
            creator_wallet="Wallet2",
            created_slot=2000,
            created_at=1724001000,
            ath_multiplier=2.45,
            ath_delay_seconds=75,
            rug_delay_seconds=320,
            entry_mc_usd=12000.0,
            peak_mc_usd=29400.0,
        ),
        HistoricalTokenSample(
            mint="Token3",
            symbol="T3",
            creator_wallet="Wallet2",
            created_slot=3000,
            created_at=1724002000,
            ath_multiplier=1.20,  # Below TP
            ath_delay_seconds=20,
            rug_delay_seconds=45,
            entry_mc_usd=10000.0,
            peak_mc_usd=12000.0,
        ),
    ]

    report = run_cluster_tp_grid_search(
        root_funder=root_dev,
        samples=samples,
        buy_size_sol=0.025,
        realized_dump_loss_pct=0.75,
        jito_tip_sol=0.001,
        gas_fee_sol=0.0005,
    )

    assert report.total_tokens_evaluated == 3
    assert report.cluster_wallets_count == 2
    assert report.optimal_tp_label in {"+50%", "+75%", "+95%"}
    assert report.is_net_profitable is True
    assert report.avg_ath_multiplier == pytest.approx(1.87, abs=0.05)
    assert report.median_ath_multiplier == pytest.approx(1.95, abs=0.05)
    assert report.ath_std_dev > 0.0
    assert report.ath_consistency_pct > 0.0
    assert report.avg_rug_delay_seconds == pytest.approx(401.7, abs=1.0)
    assert report.median_rug_delay_seconds == pytest.approx(320.0, abs=1.0)
    assert report.avg_peak_mc_usd == pytest.approx(19325.0, abs=1.0)
    assert report.avg_rug_mc_usd == pytest.approx(4831.25, abs=1.0)
    assert report.avg_ath_delay_seconds == pytest.approx(68.3, abs=1.0)


def test_tui_operator_workflow(tmp_path: Path):
    """Verify live TUI event-loop, tab navigation, backtesting (B), and clear (C)."""

    async def _run():
        db_path = tmp_path / "rugbot.db"
        db = DatabaseManager(db_path)
        repo = SQLiteTrackerRepository(db)

        dev = "2r2HuRi1vLzVxXnWAffWfsAMDkQpfG1c23KPDgR4wp5p"
        now_iso = datetime.now(UTC).isoformat()
        repo.save_funder(
            FunderRecord(
                id=0,
                address=dev,
                label="Cluster Alpha",
                enabled=True,
                created_at=now_iso,
                last_seen_at=now_iso,
            )
        )
        repo.save_target_execution_policy(
            TargetExecutionPolicy(
                funder_address=dev,
                monitoring_enabled=True,
                execution_mode=TargetExecutionMode.SIMULATED,
                quote_size_lamports=25_000_000,
                take_profit_pnl_ppm=100_000,
                stop_loss_pnl_ppm=-30_000,
                max_slippage_bps=500,
                priority_fee_microlamports=50_000,
                jito_tip_lamports=1_000_000,
                updated_at=now_iso,
            )
        )

        app = RugbotTuiApp(state_dir=tmp_path)
        async with app.run_test(size=(120, 36)) as pilot:
            await pilot.pause()

            # 1. Verify Target rendered on Dashboard
            table = app.query_one("#targets-table", TargetsTable)
            assert dev in table._targets

            # 2. Test Cluster Graph tab
            await pilot.press("f")
            await pilot.pause()

            # 3. Test Backtest key 'B'
            await pilot.press("1")
            await pilot.pause()
            await pilot.press("b")
            await pilot.pause()

            # 4. Test Clear key 'C'
            await pilot.press("c")
            await pilot.pause()
            assert len(repo.get_funders()) == 0

    asyncio.run(_run())


def test_discord_adapter_trading_suite(tmp_path: Path):
    """Verify Discord Cockpit, Settings, interactive views, and dynamic configuration."""
    core = build_ui_runtime(state_dir=tmp_path)
    adapter = DiscordAdapter(
        core,
        token="MOCK_TEST_TOKEN",  # noqa: S106
        channel_id=1451464982389592237,
        preset_p1_sol=0.010,
        preset_p2_sol=0.025,
        preset_p3_sol=0.050,
        slippage_bps=1000,
        jito_tip_sol=0.001,
        routing_mode="jito",
    )

    assert adapter.channel_id == 1451464982389592237
    adapter.set_channel(123456789012345678)
    assert adapter.channel_id == 123456789012345678

    # 1. Embed tests
    cockpit_embed = adapter.build_cockpit_embed()
    assert "RUGBOT TRADING COCKPIT" in cockpit_embed.title
    assert "JITO BUNDLE" in cockpit_embed.description

    settings_embed = adapter.build_settings_embed()
    assert "SETTINGS" in settings_embed.title

    wallet_embed = adapter.build_wallet_embed()
    assert "TRADING WALLET" in wallet_embed.title

    pos_embed, pos_views = adapter.build_positions_embed()
    assert "Positions" in pos_embed.title
    assert isinstance(pos_views, list)

    targets_embed = adapter.build_targets_embed()
    assert "Targets" in targets_embed.title

    screener_embed = adapter.build_screener_embed()
    assert "Candidate" in screener_embed.title

    # 2. Interactive Views tests
    cockpit_view = CockpitHomeView(adapter)
    assert len(cockpit_view.children) >= 6

    settings_view = SettingsView(adapter)
    assert len(settings_view.children) >= 6

    qb_view = QuickBuyView(
        core,
        "CXXpHyiwAzuwwxD9aGJCA3L6gjjJ4wMXoGYYjczKpump",
        preset_p1=adapter.preset_p1_sol,
        preset_p2=adapter.preset_p2_sol,
        preset_p3=adapter.preset_p3_sol,
    )
    assert len(qb_view.children) >= 4

    pos_action_view = PositionActionView(
        core, "CXXpHyiwAzuwwxD9aGJCA3L6gjjJ4wMXoGYYjczKpump"
    )
    assert len(pos_action_view.children) == 3
