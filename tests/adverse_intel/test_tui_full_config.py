"""Integration coverage for the complete TUI watcher configuration editor."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from textual.widgets import Checkbox, Input

from rugbot.runtime.config import load_sniper_config
from rugbot.tui.app import RugbotTuiApp


class CompleteTuiConfigTests(unittest.IsolatedAsyncioTestCase):
    """Verify every strict watcher configuration group can be edited in the TUI."""

    async def test_all_config_groups_round_trip_without_private_key(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config_path = root / "watch.yaml"
            config_path.write_text(
                "target:\n"
                "  kind: wallet\n"
                "  id: 11111111111111111111111111111111\n"
                "execution:\n"
                "  mode: observe\n"
                "  quote_size_lamports: 1000000\n",
                encoding="utf-8",
            )
            app = RugbotTuiApp(
                config_path=config_path,
                state_dir=root / "state",
                refresh_seconds=99999,
            )
            try:
                async with app.run_test(size=(160, 60)) as pilot:
                    await pilot.pause(0.1)
                    values = {
                        "target-kind": "wallet",
                        "tracking-mode": "track_buys",
                        "execution-mode": "simulation",
                        "target-wallet": "11111111111111111111111111111111",
                        "snipe-size-sol": "0.002",
                        "max-slippage": "750",
                        "priority-fee": "80000",
                        "jito-tip": "0.0015",
                        "routing-policy": "jito",
                        "compute-unit-limit": "450000",
                        "loaded-accounts-limit": "256000",
                        "signer-pubkey": "11111111111111111111111111111111",
                        "jito-url": "https://block-engine.example",
                        "volume-bankroll": "120000",
                        "volume-independent": "30000",
                        "volume-impact": "150000",
                        "strategy-min-volume": "1000000",
                        "strategy-max-creator-pairs": "8",
                        "strategy-history-samples": "12",
                        "strategy-max-buys-hour": "2",
                        "strategy-max-entry-index": "1",
                        "max-entry-mc": "25000",
                        "strategy-max-deviation": "200000",
                        "rule-min-mc": "1000",
                        "rule-max-mc": "50000000",
                        "rule-max-age": "2",
                        "rule-cooldown": "30",
                        "rule-max-losses": "4",
                        "snipe-delay": "1",
                        "rule-no-activity": "120",
                        "take-profit-pct": "10",
                        "stop-loss-pct": "-20",
                        "min-winrate-pct": "55",
                    }
                    for widget_id, value in values.items():
                        app.query_one(f"#{widget_id}", Input).value = value

                    for widget_id in (
                        "strategy-bundle",
                        "strategy-double-signature",
                        "strategy-prior-zero",
                        "strategy-historical",
                        "rule-buy-once",
                    ):
                        app.query_one(f"#{widget_id}", Checkbox).value = True

                    for index, drawdown in enumerate((100000, 200000, 300000)):
                        app.query_one(f"#dip-{index}-drawdown", Input).value = str(
                            drawdown
                        )
                        app.query_one(f"#dip-{index}-size", Input).value = str(
                            1000 * (index + 1)
                        )
                    for index, trigger in enumerate(
                        (100000, 200000, 300000, 400000, 500000)
                    ):
                        app.query_one(f"#tp-{index}-trigger", Input).value = str(
                            trigger
                        )
                        app.query_one(f"#tp-{index}-fraction", Input).value = str(
                            200000 * (index + 1)
                        )
                    for index, trigger in enumerate(
                        (-500000, -400000, -300000, -200000, -100000)
                    ):
                        app.query_one(f"#sl-{index}-trigger", Input).value = str(
                            trigger
                        )
                        app.query_one(f"#sl-{index}-fraction", Input).value = str(
                            200000 * (index + 1)
                        )
                    for index, minimum in enumerate(
                        ("", "1000", "2000", "3000", "4000")
                    ):
                        app.query_one(f"#trail-{index}-mc", Input).value = minimum
                        app.query_one(f"#trail-{index}-drawdown", Input).value = str(
                            100000 + index * 10000
                        )
                    for index in range(3):
                        app.query_one(f"#big-{index}-min", Input).value = str(
                            index * 1000 + 1
                        )
                        app.query_one(f"#big-{index}-max", Input).value = str(
                            (index + 1) * 1000
                        )
                        app.query_one(f"#big-{index}-fraction", Input).value = str(
                            200000 + index * 100000
                        )

                    app._save_settings()
                    await pilot.pause(0.1)

                saved = load_sniper_config(config_path)
                self.assertEqual(saved.tracking_mode.value, "track_buys")
                self.assertEqual(saved.execution.mode.value, "simulation")
                self.assertEqual(saved.execution.quote_size_lamports, 2_000_000)
                self.assertEqual(saved.volume_sizing.max_price_impact_ppm, 150_000)
                self.assertEqual(saved.strategy.history_sample_count, 12)
                self.assertTrue(saved.strategy.require_historical_qualification)
                self.assertEqual(len(saved.rules.buy_the_dip_levels), 3)
                self.assertEqual(len(saved.rules.sell.take_profit_levels), 5)
                self.assertEqual(len(saved.rules.sell.stop_loss_levels), 5)
                self.assertEqual(len(saved.rules.sell.trailing_levels), 5)
                self.assertEqual(len(saved.rules.sell.auto_sell_big_buy_levels), 3)

                saved_text = config_path.read_text(encoding="utf-8").lower()
                self.assertNotIn("private_key", saved_text)
                self.assertNotIn("secret", saved_text)
            finally:
                app._db.close()


if __name__ == "__main__":
    unittest.main()
