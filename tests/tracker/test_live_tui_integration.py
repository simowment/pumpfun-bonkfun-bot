"""End-to-End Live Integration Tests for Rugbot TUI, Tracker Pipeline, and Execution State."""

# ruff: noqa: S106

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from textual.widgets import Button, Checkbox, Footer, Input, Static, TabbedContent

from rugbot.runtime.config import load_sniper_config
from rugbot.tracker.events import LaunchDetected, WalletFunded
from rugbot.tracker.models import (
    FundingHop,
    FundingPath,
    TargetExecutionMode,
)
from rugbot.tui.app import RugbotTuiApp
from rugbot.tui.widgets.activity import ActivityItem, LiveActivityView
from rugbot.tui.widgets.execution_rail import ExecutionCard
from rugbot.tui.widgets.header import CompactHeader
from rugbot.tui.widgets.modal import DetailInspectModal
from rugbot.tui.widgets.pnl import WalletPnlPanel
from rugbot.tui.widgets.targets_table import TargetsTable


class LiveTuiIntegrationTests(unittest.IsolatedAsyncioTestCase):
    """Full end-to-end integration tests verifying domain events, UI state, and user actions."""

    async def test_full_live_tui_integration_lifecycle(self) -> None:  # noqa: PLR0915
        with tempfile.TemporaryDirectory() as tmp_dir:
            config_path = Path(tmp_dir) / "watch.yaml"
            config_path.write_text(
                "target:\n"
                "  kind: wallet\n"
                "  id: FaBGrHWjcJ8vKnbgUtsdpZjvF7YAAajtQTWmmEHiKtQr\n"
                "execution:\n"
                "  mode: observe\n"
                "  quote_size_lamports: 10000000\n",
                encoding="utf-8",
            )
            app = RugbotTuiApp(
                state_dir=Path(tmp_dir),
                config_path=config_path,
                refresh_seconds=99999,
            )

            # Seed an active root funder
            funder_addr = "FaBGrHWjcJ8vKnbgUtsdpZjvF7YAAajtQTWmmEHiKtQr"
            app._service.add_funder(funder_addr, label="Primary Funder")

            try:
                async with app.run_test(size=(140, 45)) as pilot:
                    await pilot.pause(0.5)

                    # 1. Verify Header Integration
                    header = app.query_one("#compact-header", CompactHeader)
                    self.assertGreaterEqual(header.active_targets_count, 1)
                    pnl_panel = app.query_one("#wallet-pnl-panel", WalletPnlPanel)
                    self.assertEqual(pnl_panel.points, ())

                    # 2. Simulate Live Ingestion of On-Chain Domain Events
                    target_dev = "8Ks234K89kQPzGXZLcbepump99837123"
                    app._event_bus.publish(
                        WalletFunded(
                            timestamp=1700000000,
                            root_funder=funder_addr,
                            wallet=target_dev,
                            data={
                                "lamports": 5_000_000_000,
                                "signature": "sig_fund_1",
                            },
                        )
                    )
                    await pilot.pause(0.3)

                    app._event_bus.publish(
                        LaunchDetected(
                            timestamp=1700000010,
                            root_funder=funder_addr,
                            wallet=target_dev,
                            data={
                                "symbol": "ALPHA",
                                "name": "Alpha Token",
                                "mint": "AlphaMint1111111111111111111111111111111111",
                                "signature": "sig_launch_1",
                            },
                        )
                    )
                    await pilot.pause(0.3)

                    # 3. Verify Live Activity View received the streamed domain events
                    activity_view = app.query_one(
                        "#live-activity-view", LiveActivityView
                    )
                    self.assertGreaterEqual(len(activity_view._items), 2)

                    # 4. Verify Target Profile inspection in Targets Table
                    targets_table = app.query_one("#targets-table", TargetsTable)
                    target = targets_table.get_selected_target()
                    self.assertIsNotNone(target)

                    # 5. Tracker and sniper stay in separate workflow regions.
                    self.assertIsNotNone(app.query_one("#dashboard-layout"))
                    self.assertIsNotNone(app.query_one("#wallet-risk-panel"))
                    app.action_show_sniper()
                    await pilot.pause(0.1)
                    self.assertIsNotNone(app.query_one("#sniper-execution-col"))
                    self.assertIsNotNone(app.query_one("#sniper-positions-col"))
                    rail = app.query_one("#execution-card", ExecutionCard)
                    self.assertNotIn("EXECUTION CANDIDATE", rail._render_content())

                    # 6. Pause/resume remain keyboard actions, not duplicated footer buttons.
                    app.action_pause_target()
                    await pilot.pause(0.1)
                    updated_target = targets_table.get_selected_target()
                    self.assertIsNotNone(updated_target)
                    assert updated_target is not None
                    self.assertIsNotNone(updated_target.policy)
                    assert updated_target.policy is not None
                    self.assertFalse(updated_target.policy.monitoring_enabled)

                    app.action_pause_target()
                    await pilot.pause(0.1)
                    resumed_target = targets_table.get_selected_target()
                    self.assertIsNotNone(resumed_target)
                    assert resumed_target is not None
                    self.assertIsNotNone(resumed_target.policy)
                    assert resumed_target.policy is not None
                    self.assertTrue(resumed_target.policy.monitoring_enabled)

                    # 7. Dry-run and live remain explicit mode actions in the rail.
                    app.action_toggle_dry_run()
                    await pilot.pause(0.1)
                    dry_run_target = targets_table.get_selected_target()
                    assert dry_run_target is not None
                    assert dry_run_target.policy is not None
                    self.assertEqual(
                        dry_run_target.policy.execution_mode,
                        TargetExecutionMode.SIMULATED,
                    )
                    self.assertIn("DRY RUN", rail._render_content())

                    app.action_toggle_live_trading()
                    await pilot.pause(0.1)
                    live_target = targets_table.get_selected_target()
                    assert live_target is not None
                    assert live_target.policy is not None
                    self.assertEqual(
                        live_target.policy.execution_mode,
                        TargetExecutionMode.LIVE,
                    )
                    self.assertFalse(app._enable_live)
                    self.assertNotIn("EXIT ALL", rail._render_content())

                    # 8. Backtester exposes historical evidence, never fabricated PnL.
                    app.action_run_backtest()
                    await pilot.pause(0.1)
                    self.assertEqual(
                        app.query_one(TabbedContent).active, "launches-tab"
                    )
                    app.action_show_tracker()
                    await pilot.pause(0.3)

                    # 9. Add Target uses the real keyboard workflow and a blank form.
                    await pilot.press("a")
                    await pilot.pause(0.3)
                    self.assertEqual(
                        app.query_one(TabbedContent).active,
                        "settings-tab",
                    )
                    self.assertEqual(app.query_one("#target-wallet", Input).value, "")
                    await pilot.press("escape")
                    await pilot.pause(0.3)

                    # 10. No position means no exit action and no fabricated close event.
                    app.action_exit_position()
                    await pilot.pause(0.1)
                    self.assertEqual(
                        app.query_one(TabbedContent).active, "overview-tab"
                    )
                    self.assertNotIn("EXIT ALL", rail._render_content())

                    # 11. Integration Test: Settings Tab configuration and persistence
                    app.query_one(TabbedContent).active = "settings-tab"
                    await pilot.pause(0.1)
                    app.query_one("#target-wallet", Input).value = funder_addr
                    app.query_one("#snipe-size-sol", Input).value = "0.050"
                    app.query_one("#take-profit-pct", Input).value = "150.0"
                    app.query_one("#stop-loss-pct", Input).value = "-25.0"
                    app.query_one("#jito-tip", Input).value = "0.0020"
                    app.query_one("#execution-mode-live", Checkbox).value = False
                    await pilot.click("#save-settings-btn")
                    await pilot.pause(0.1)

                    # Verify in-memory and disk persistence
                    saved_config = load_sniper_config(config_path)
                    self.assertEqual(
                        saved_config.execution.quote_size_lamports,
                        50_000_000,
                    )
                    self.assertFalse((Path(tmp_dir) / "settings.json").exists())
            finally:
                app._db.close()

    async def test_footer_shortcuts_span_full_width(self) -> None:
        """Footer shortcuts must spread across the full line, not clump bottom-left."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            config_path = Path(tmp_dir) / "watch.yaml"
            config_path.write_text(
                "target:\n"
                "  kind: wallet\n"
                "  id: FaBGrHWjcJ8vKnbgUtsdpZjvF7YAAajtQTWmmEHiKtQr\n"
                "execution:\n"
                "  mode: observe\n"
                "  quote_size_lamports: 10000000\n",
                encoding="utf-8",
            )
            app = RugbotTuiApp(
                state_dir=Path(tmp_dir),
                config_path=config_path,
                refresh_seconds=99999,
            )
            try:
                async with app.run_test(size=(140, 45)) as pilot:
                    await pilot.pause(0.5)

                    footer = app.query_one("#footer-actions-bar", Static)
                    self.assertTrue(footer.display)
                    self.assertEqual(footer.size.height, 2)
            finally:
                app._db.close()

    async def test_detail_inspect_modal_with_funding_path(self) -> None:
        """Verify DetailInspectModal composes without AttributeError on FundingPath."""
        item = ActivityItem(
            row_id="test_row_1",
            timestamp=1700000000,
            event_type="LAUNCH",
            root_funder="FaBGrHWjcJ8vKnbgUtsdpZjvF7YAAajtQTWmmEHiKtQr",
            target_wallet="8Ks234K89kQPzGXZLcbepump99837123",
            token_symbol="TEST",
            token_name="Test Token",
            token_mint="TestMint111111111111111111111111111111111",
            amount_lamports=3_000_000_000,
            hops=2,
            signature="test_sig_1",
        )
        path = FundingPath(
            root_funder="FaBGrHWjcJ8vKnbgUtsdpZjvF7YAAajtQTWmmEHiKtQr",
            creator_wallet="8Ks234K89kQPzGXZLcbepump99837123",
            hops=(
                FundingHop(
                    from_wallet="FaBGrHWjcJ8vKnbgUtsdpZjvF7YAAajtQTWmmEHiKtQr",
                    to_wallet="8Ks234K89kQPzGXZLcbepump99837123",
                    amount_lamports=3_000_000_000,
                    amount_sol=3.0,
                    signature="test_sig_1",
                    timestamp=1700000000,
                    depth=1,
                ),
            ),
            total_depth=1,
            last_funding_timestamp=1700000000,
            launch_timestamp=1700000050,
            time_to_launch_seconds=50,
        )
        modal = DetailInspectModal(item=item, path=path)
        details = modal._render_details()
        self.assertIn("Timing:", details)
        self.assertIn("50s from root funding", details)
        self.assertIn("TestMint111111111111111111111111111111111", details)

        # Test link button handling
        with patch("webbrowser.open") as mock_open:
            btn_axiom = Button("Axiom", id="btn-link-axiom")
            modal.on_button_pressed(Button.Pressed(btn_axiom))
            mock_open.assert_called_once_with(
                "https://axiom.trade/token/TestMint111111111111111111111111111111111"
            )
