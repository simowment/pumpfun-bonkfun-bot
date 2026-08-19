"""Tests for the persisted wallet PnL curve panel."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from textual.app import App, ComposeResult
from textual.widgets import Static

from rugbot.tui.widgets.pnl import WalletPnlHistory, WalletPnlPanel, WalletPnlPoint

_WALLET = "11111111111111111111111111111111"


class _PnlHost(App[None]):
    """Minimal Textual host for the PnL widget integration test."""

    def compose(self) -> ComposeResult:
        yield WalletPnlPanel(id="wallet-pnl-panel")


class WalletPnlTests(unittest.IsolatedAsyncioTestCase):
    """Verify integer-safe persistence and visible curve rendering."""

    def test_history_derives_net_delta_from_first_balance(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            history = WalletPnlHistory(Path(directory) / "wallet_pnl.jsonl")
            first = history.record_balance(
                _WALLET, 2_000_000_000, observed_at_epoch=100
            )
            second = history.record_balance(
                _WALLET, 2_250_000_000, observed_at_epoch=101
            )
            third = history.record_balance(
                _WALLET, 1_750_000_000, observed_at_epoch=102
            )

            self.assertEqual(first.net_pnl_lamports, 0)
            self.assertEqual(second.net_pnl_lamports, 250_000_000)
            self.assertEqual(third.net_pnl_lamports, -250_000_000)
            self.assertEqual(len(history.read(_WALLET)), 3)

    async def test_panel_renders_both_curves_and_disclosure(self) -> None:
        points = (
            WalletPnlPoint(_WALLET, 100, 2_000_000_000, 2_000_000_000, 0),
            WalletPnlPoint(_WALLET, 101, 2_250_000_000, 2_250_000_000, 250_000_000),
        )
        app = _PnlHost()
        async with app.run_test(size=(100, 20)) as pilot:
            await pilot.pause()
            panel = app.query_one("#wallet-pnl-panel", WalletPnlPanel)
            panel.update_history(_WALLET, points)
            rendered = str(app.query_one("#pnl-content", Static).render())
            self.assertIn("EQUITY", rendered)
            self.assertIn("NET PNL", rendered)
            self.assertIn("deposits/withdrawals included", rendered)
            self.assertGreaterEqual(len(panel.points), 2)


if __name__ == "__main__":
    unittest.main()
