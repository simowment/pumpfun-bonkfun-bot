"""Focused tests for the wallet intelligence Textual surface."""

import unittest
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory

from textual.widgets import Checkbox, DataTable, Input, Static, TabbedContent

from rugbot.runtime.config import load_sniper_config
from rugbot.runtime.wallet_intelligence import (
    WalletIntelligenceReport,
    WalletLaunch,
    WalletLink,
    WalletNode,
)
from rugbot.runtime.wallet_tui import (
    WalletIntelApp,
    format_assessment,
    format_flow,
    format_graph_map,
    format_network_endpoint,
    format_sol,
    launch_matches,
    report_delta,
    short_address,
)


class WalletTuiTests(unittest.IsolatedAsyncioTestCase):
    """Verify display formatting and fail-closed startup behavior."""

    def test_integer_sol_formatting(self) -> None:
        self.assertEqual(format_sol(0), "0")
        self.assertEqual(format_sol(1_000_000_000), "1")
        self.assertEqual(format_sol(1_234_500_000), "1.2345")

    def test_dashboard_helpers_keep_evidence_scannable(self) -> None:
        launch = WalletLaunch(
            slot=100,
            transaction_index=0,
            signature="signature-abcdefghijk",
            mint="mint-abcdefghijk",
            name="name",
            symbol="SYM",
            creator="creator-abcdefghijk",
            position_is_zero_or_one=True,
        )
        edge = WalletLink(
            source="source-abcdefghijk",
            target="target-abcdefghijk",
            transfer_count=2,
            amount_lamports=1_000_000_000,
            first_slot=90,
            last_slot=100,
            evidence_ids=("evidence",),
        )
        report = WalletIntelligenceReport(
            as_of_slot=100,
            target_wallet="target-abcdefghijk",
            history_limit=50,
            scanned_transaction_count=2,
            successful_transaction_count=2,
            first_seen_slot=90,
            last_seen_slot=100,
            launch_count=1,
            direct_linked_wallet_count=1,
            linked_creator_wallet_count=0,
            wallet_switch_candidate=False,
            native_in_lamports=2_000_000_000,
            native_out_lamports=1_000_000_000,
            launches=(launch,),
            nodes=(),
            edges=(edge,),
            warnings=("bounded",),
        )
        newer_launch = replace(launch, signature="new-signature-abcdefghijk")
        newer_edge = replace(edge, target="other-abcdefghijk")
        newer = replace(
            report, launches=(launch, newer_launch), edges=(edge, newer_edge)
        )

        self.assertEqual(short_address("abcdefghijklmnop"), "abcdef...klmnop")
        self.assertTrue(launch_matches(launch, "sym"))
        self.assertFalse(launch_matches(launch, "missing"))
        self.assertEqual(report_delta(report, newer), (1, 1))
        self.assertIn("IN  2 SOL", format_flow(report))
        self.assertIn("<-- DIRECT  source...fghijk", format_graph_map(report))
        self.assertIn("NOT QUALIFIED", format_assessment(report))
        self.assertEqual(
            format_network_endpoint("https://rpc.example/?api-key=secret"),
            "rpc.example",
        )

    async def test_invalid_wallet_is_visible_as_abstention(self) -> None:
        app = WalletIntelApp(
            "not-a-wallet",
            endpoint="https://rpc.example",
            refresh_seconds=60,
        )

        async with app.run_test() as pilot:
            await pilot.pause()
            status = app.query_one("#status", Static)
            self.assertIn("ABSTAIN", str(status.render()))
            self.assertFalse(app.query_one("#wallet-input", Input).has_focus)

    async def test_report_renders_into_all_tables(self) -> None:
        app = WalletIntelApp("not-a-wallet", endpoint="https://rpc.example")
        report = WalletIntelligenceReport(
            as_of_slot=100,
            target_wallet="target",
            history_limit=50,
            scanned_transaction_count=1,
            successful_transaction_count=1,
            first_seen_slot=100,
            last_seen_slot=100,
            launch_count=1,
            direct_linked_wallet_count=1,
            linked_creator_wallet_count=1,
            wallet_switch_candidate=True,
            native_in_lamports=0,
            native_out_lamports=1_000_000_000,
            launches=(
                WalletLaunch(
                    slot=100,
                    transaction_index=0,
                    signature="signature",
                    mint="mint",
                    name="name",
                    symbol="SYM",
                    creator="target",
                    position_is_zero_or_one=True,
                ),
            ),
            nodes=(
                WalletNode(
                    address="target",
                    is_target=True,
                    scanned_transaction_count=1,
                    launch_count=1,
                    first_seen_slot=100,
                    last_seen_slot=100,
                    roles=("target",),
                ),
            ),
            edges=(
                WalletLink(
                    source="target",
                    target="peer",
                    transfer_count=1,
                    amount_lamports=1_000_000_000,
                    first_slot=100,
                    last_slot=100,
                    evidence_ids=("evidence",),
                ),
            ),
            warnings=("bounded",),
        )

        async with app.run_test() as pilot:
            await pilot.pause()
            app._render_report(report)
            self.assertEqual(app.query_one("#launches-table", DataTable).row_count, 1)
            self.assertEqual(app.query_one("#nodes-table", DataTable).row_count, 1)
            self.assertEqual(app.query_one("#edges-table", DataTable).row_count, 1)
            self.assertIn(
                "IN  0 SOL", str(app.query_one("#flow-panel", Static).render())
            )
            self.assertIn("TARGET", str(app.query_one("#graph-map", Static).render()))

            launch_filter = app.query_one("#launch-filter", Input)
            launch_filter.value = "missing"
            app._render_launches(report)
            self.assertEqual(app.query_one("#launches-table", DataTable).row_count, 0)
            launch_filter.value = "sym"
            app._render_launches(report)
            self.assertEqual(app.query_one("#launches-table", DataTable).row_count, 1)

            app.action_show_graph()
            self.assertEqual(app.query_one(TabbedContent).active, "graph-tab")
            app.action_show_launches()
            self.assertEqual(app.query_one(TabbedContent).active, "launches-tab")
            app.action_show_overview()
            self.assertEqual(app.query_one(TabbedContent).active, "overview-tab")
            app.action_focus_wallet()
            await pilot.pause()
            self.assertTrue(app.query_one("#wallet-input", Input).has_focus)

    async def test_settings_expose_playbook_entry_controls(self) -> None:
        """The TUI exposes the strict playbook entry controls."""

        app = WalletIntelApp("not-a-wallet", endpoint="https://rpc.example")
        async with app.run_test() as pilot:
            await pilot.pause()
            for input_id in (
                "target-wallet",
                "snipe-size-sol",
                "take-profit-pct",
                "stop-loss-pct",
                "priority-fee",
                "jito-tip",
                "max-slippage",
                "max-gas-cap",
                "max-entry-mc",
                "min-winrate-pct",
                "snipe-delay",
            ):
                self.assertIsInstance(app.query_one(f"#{input_id}", Input), Input)
            self.assertIsInstance(
                app.query_one("#require-block-zero", Checkbox), Checkbox
            )
            self.assertIsInstance(
                app.query_one("#require-funding-match", Checkbox), Checkbox
            )

    async def test_settings_save_persists_public_setup(self) -> None:
        """The TUI saves the public target and paper execution settings."""

        with TemporaryDirectory() as directory:
            config_path = Path(directory) / "watch.yaml"
            config_path.write_text(
                "target:\n"
                "  kind: wallet\n"
                '  id: "11111111111111111111111111111111"\n'
                "execution:\n"
                "  mode: observe\n"
                "  quote_size_lamports: 1000000\n",
                encoding="utf-8",
            )
            app = WalletIntelApp(
                "11111111111111111111111111111111",
                endpoint="https://rpc.example",
                config_path=config_path,
            )
            async with app.run_test() as pilot:
                await pilot.pause()
                app.query_one("#snipe-size-sol", Input).value = "0.020"
                app.query_one("#max-slippage", Input).value = "750"
                app.query_one("#max-entry-mc", Input).value = "42000"
                app._save_settings()

            saved = load_sniper_config(config_path)
            self.assertEqual(saved.target.id, "11111111111111111111111111111111")
            self.assertEqual(saved.execution.mode.value, "observe")
            self.assertEqual(saved.execution.quote_size_lamports, 20_000_000)
            self.assertEqual(saved.execution.max_slippage_bps, 750)
            self.assertEqual(
                saved.strategy.max_entry_market_cap_quote_base_units,
                42_000,
            )


if __name__ == "__main__":
    unittest.main()
