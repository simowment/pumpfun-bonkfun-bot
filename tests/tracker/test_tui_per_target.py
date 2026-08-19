"""Operator integration test for target-local execution policies."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from textual.widgets import Button, Footer, Input, Static, TabbedContent

from rugbot.tracker.models import TargetExecutionMode
from rugbot.tui.app import RugbotTuiApp
from rugbot.tui.widgets.execution_rail import ExecutionCard
from rugbot.tui.widgets.targets_table import TargetsTable


class PerTargetWorkflowTests(unittest.IsolatedAsyncioTestCase):
    """Verify a target policy is edited without mutating watcher defaults."""

    async def test_settings_save_selected_target_policy_only(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            state_dir = Path(temporary_directory)
            config_path = state_dir / "watch.yaml"
            config_path.write_text(
                "target:\n"
                "  kind: wallet\n"
                "  id: FaBGrHWjcJ8vKnbgUtsdpZjvF7YAAajtQTWmmEHiKtQr\n"
                "execution:\n"
                "  mode: observe\n"
                "  quote_size_lamports: 10000000\n",
                encoding="utf-8",
            )
            watcher_config_before = config_path.read_bytes()
            app = RugbotTuiApp(
                state_dir=state_dir,
                config_path=config_path,
                refresh_seconds=99_999,
            )
            try:
                async with app.run_test(size=(120, 36)) as pilot:
                    await pilot.press("e")
                    await pilot.pause()
                    self.assertEqual(
                        app.query_one(TabbedContent).active,
                        "settings-tab",
                    )

                    target_wallet = "So11111111111111111111111111111111111111112"
                    app.query_one("#target-wallet", Input).value = target_wallet
                    app.query_one("#snipe-size-sol", Input).value = "0.025"
                    app.query_one("#take-profit-pct", Input).value = "125"
                    app.query_one("#stop-loss-pct", Input).value = "-25"
                    app.query_one("#max-slippage", Input).value = "750"
                    app.query_one("#priority-fee", Input).value = "125000"
                    app.query_one("#jito-tip", Input).value = "0.002"
                    app.query_one("#execution-mode", Input).value = "simulation"

                    await pilot.click("#save-target-policy-btn")
                    await pilot.pause()

                    policy = app._repository.get_target_execution_policy(target_wallet)
                    self.assertIsNotNone(policy)
                    assert policy is not None
                    self.assertEqual(
                        policy.execution_mode, TargetExecutionMode.SIMULATED
                    )
                    self.assertEqual(policy.quote_size_lamports, 25_000_000)
                    self.assertEqual(policy.take_profit_pnl_ppm, 1_250_000)
                    self.assertEqual(policy.stop_loss_pnl_ppm, -250_000)
                    self.assertEqual(policy.max_slippage_bps, 750)
                    self.assertEqual(policy.priority_fee_microlamports, 125_000)
                    self.assertEqual(policy.jito_tip_lamports, 2_000_000)
                    self.assertEqual(config_path.read_bytes(), watcher_config_before)

                    targets = app.query_one("#targets-table", TargetsTable)
                    targets._selected_target_address = target_wallet
                    selected = targets.get_selected_target()
                    self.assertIsNotNone(selected)
                    assert selected is not None
                    targets.post_message(TargetsTable.TargetSelected(selected))
                    await pilot.pause()
                    rail = app.query_one("#execution-card", ExecutionCard)
                    self.assertIn("0.025 SOL", rail._render_content())
            finally:
                app._db.close()

    async def test_footer_shortcuts_drive_operator_workflow(self) -> None:
        """Exercise the visible shortcuts through Textual's real event loop."""
        with tempfile.TemporaryDirectory() as temporary_directory:
            state_dir = Path(temporary_directory)
            config_path = state_dir / "watch.yaml"
            target_wallet = "FaBGrHWjcJ8vKnbgUtsdpZjvF7YAAajtQTWmmEHiKtQr"
            config_path.write_text(
                "target:\n"
                "  kind: wallet\n"
                f"  id: {target_wallet}\n"
                "execution:\n"
                "  mode: observe\n"
                "  quote_size_lamports: 10000000\n",
                encoding="utf-8",
            )
            app = RugbotTuiApp(
                state_dir=state_dir,
                config_path=config_path,
                refresh_seconds=99_999,
            )
            try:
                async with app.run_test(size=(120, 36)) as pilot:
                    await pilot.pause()
                    footer = app.query_one("#app-footer", Footer)
                    self.assertIsNotNone(footer)

                    await pilot.press("p")
                    await pilot.pause()
                    policy = app._repository.get_target_execution_policy(target_wallet)
                    self.assertIsNotNone(policy)
                    assert policy is not None
                    self.assertFalse(policy.monitoring_enabled)

                    await pilot.press("p")
                    await pilot.pause()
                    policy = app._repository.get_target_execution_policy(target_wallet)
                    assert policy is not None
                    self.assertTrue(policy.monitoring_enabled)

                    await pilot.press("e")
                    await pilot.pause()
                    self.assertEqual(
                        app.query_one(TabbedContent).active,
                        "settings-tab",
                    )
                    self.assertEqual(
                        app.query_one("#target-wallet", Input).value,
                        target_wallet,
                    )
                    await pilot.press("ctrl+s")
                    await pilot.pause()
                    self.assertIsNotNone(
                        app._repository.get_target_execution_policy(target_wallet)
                    )

                    # Click Back to Dashboard button from settings
                    back_btn = app.query_one("#settings-back-btn", Button)
                    await pilot.click(back_btn)
                    await pilot.pause()
                    self.assertEqual(app.query_one(TabbedContent).active, "overview-tab")

                    # Switch to Dev History tab via key 2
                    await pilot.press("2")
                    await pilot.pause()
                    self.assertEqual(app.query_one(TabbedContent).active, "launches-tab")

                    # Click Back to Dashboard button from Dev History
                    back_btn = app.query_one("#launches-back-btn", Button)
                    await pilot.click(back_btn)
                    await pilot.pause()
                    self.assertEqual(app.query_one(TabbedContent).active, "overview-tab")

                    await pilot.press("escape")
                    await pilot.press("a")
                    await pilot.pause()
                    self.assertEqual(
                        app.query_one(TabbedContent).active,
                        "settings-tab",
                    )
                    self.assertEqual(app.query_one("#target-wallet", Input).value, "")

                    navigation = (
                        ("f2", "launches-tab"),
                        ("f3", "positions-tab"),
                        ("f4", "settings-tab"),
                        ("f1", "overview-tab"),
                    )
                    for shortcut, expected_tab in navigation:
                        await pilot.press(shortcut)
                        await pilot.pause()
                        self.assertEqual(
                            app.query_one(TabbedContent).active,
                            expected_tab,
                        )
            finally:
                app._db.close()
