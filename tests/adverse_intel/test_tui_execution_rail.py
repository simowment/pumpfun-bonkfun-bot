"""Integration tests for the state-driven execution rail."""

from __future__ import annotations

import unittest

from textual.app import App, ComposeResult

from rugbot.tracker.models import (
    TargetExecutionMode,
    TargetExecutionPolicy,
    TargetRecord,
)
from rugbot.tui.widgets.activity import ActivityItem
from rugbot.tui.widgets.execution_rail import ExecutionCard
from rugbot.tui.widgets.inspector import OperatorStage


class _RailHost(App[None]):
    """Minimal Textual host for the execution rail."""

    def compose(self) -> ComposeResult:
        yield ExecutionCard(id="execution-card")


class ExecutionRailTests(unittest.IsolatedAsyncioTestCase):
    """Verify contextual rendering and action visibility."""

    async def test_actions_follow_execution_state(self) -> None:
        target = TargetRecord(
            address="TargetDev111111111111111111111111111111111",
            label="Dev",
            policy=TargetExecutionPolicy(
                funder_address="TargetDev111111111111111111111111111111111",
                monitoring_enabled=True,
                execution_mode=TargetExecutionMode.SIMULATED,
                quote_size_lamports=10_000_000,
                take_profit_pnl_ppm=1_000_000,
                stop_loss_pnl_ppm=-300_000,
                max_slippage_bps=500,
                priority_fee_microlamports=50_000,
                jito_tip_lamports=1_000_000,
                updated_at="2026-08-19T00:00:00+00:00",
            ),
        )
        candidate = ActivityItem(
            row_id="launch-1",
            timestamp=1,
            event_type="LAUNCH",
            root_funder=target.address,
            target_wallet=target.address,
            token_symbol="TEST",  # noqa: S106
            market_cap_usd=4200.0,
            block_number=0,
            signal="QUALIFIED",
        )

        app = _RailHost()
        async with app.run_test(size=(45, 30)) as pilot:
            await pilot.pause()
            rail = app.query_one("#execution-card", ExecutionCard)
            rail.update_target(target)
            self.assertNotIn("EXIT ALL", rail._render_content())
            self.assertTrue(app.query_one("#btn-exec-edit").display)
            self.assertFalse(app.query_one("#btn-exec-exit").display)

            rail.update_item(candidate)
            await pilot.pause()
            self.assertEqual(rail.stage, OperatorStage.CANDIDATE)
            self.assertIn("EXECUTION CANDIDATE", rail._render_content())
            self.assertTrue(app.query_one("#btn-exec-simulate").display)
            self.assertFalse(app.query_one("#btn-exec-exit").display)

            rail.set_stage(OperatorStage.POSITION_OPEN)
            await pilot.pause()
            self.assertIn("LIVE POSITION", rail._render_content())
            self.assertTrue(app.query_one("#btn-exec-exit").display)
            self.assertTrue(app.query_one("#btn-exec-sell50").display)


if __name__ == "__main__":
    unittest.main()
