"""Focused tests for the wallet intelligence Textual surface."""

import unittest
from dataclasses import replace

from textual.widgets import Checkbox, DataTable, Input, Static, TabbedContent

from rugbot.runtime.wallet_intelligence import (
    WalletIntelligenceReport,
    WalletLaunch,
    WalletLink,
    WalletNode,
)
from rugbot.runtime.wallet_tui import (
    WalletIntelApp,
    format_flow,
    format_graph_map,
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
        self.assertIn("<- source...fghijk", format_graph_map(report))

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
            app._render_report(report)  # noqa: SLF001
            self.assertEqual(app.query_one("#launches-table", DataTable).row_count, 1)
            self.assertEqual(app.query_one("#nodes-table", DataTable).row_count, 1)
            self.assertEqual(app.query_one("#edges-table", DataTable).row_count, 1)
            self.assertIn(
                "IN  0 SOL", str(app.query_one("#flow-panel", Static).render())
            )
            self.assertIn("TARGET", str(app.query_one("#graph-map", Static).render()))

            launch_filter = app.query_one("#launch-filter", Input)
            launch_filter.value = "missing"
            app._render_launches(report)  # noqa: SLF001
            self.assertEqual(app.query_one("#launches-table", DataTable).row_count, 0)
            launch_filter.value = "sym"
            app._render_launches(report)  # noqa: SLF001
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
                "snipe-delay",
                "min-mc",
                "max-mc",
                "max-age",
                "follow-cooldown",
                "max-losses",
            ):
                self.assertIsInstance(app.query_one(f"#{input_id}", Input), Input)
            self.assertIsInstance(app.query_one("#buy-once", Checkbox), Checkbox)


if __name__ == "__main__":
    unittest.main()
