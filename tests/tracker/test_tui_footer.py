"""Operator integration tests for persistent contextual TUI shortcuts."""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path

from rich.text import Text
from textual.widgets import Static, TabbedContent

from rugbot.tui.app import RugbotTuiApp


class _FooterTestApp(RugbotTuiApp):
    async def _poll_observation_worker(self) -> None:
        await asyncio.Event().wait()


class TuiFooterTests(unittest.IsolatedAsyncioTestCase):
    """Drive the actual Textual event loop at required terminal sizes."""

    async def test_footer_survives_80x24_and_keyboard_navigation(self) -> None:
        await self._exercise_footer((80, 24), expect_search=False)

    async def test_footer_uses_extra_width_at_120x36(self) -> None:
        await self._exercise_footer((120, 36), expect_search=True)

    async def _exercise_footer(
        self,
        size: tuple[int, int],
        *,
        expect_search: bool,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            config_path = root / "watch.yaml"
            config_path.write_text(
                "target:\n"
                "  kind: wallet\n"
                "  id: FaBGrHWjcJ8vKnbgUtsdpZjvF7YAAajtQTWmmEHiKtQr\n"
                "execution:\n"
                "  mode: observe\n"
                "  quote_size_lamports: 10000000\n",
                encoding="utf-8",
            )
            app = _FooterTestApp(
                config_path=config_path,
                state_dir=root / "state",
                refresh_seconds=99_999,
            )

            async with app.run_test(size=size) as pilot:
                await pilot.pause()
                footer = app.query_one("#footer-actions-bar", Static)
                self.assertTrue(footer.display)
                self.assertEqual(footer.size.height, 2)
                tracker_text = _plain_text(footer.render())
                self.assertIn("F1", tracker_text)
                self.assertIn("F2", tracker_text)
                self.assertIn("F3", tracker_text)
                self.assertIn("Q", tracker_text)
                self.assertEqual("SEARCH" in tracker_text, expect_search)

                await pilot.press("f3")
                await pilot.pause()
                self.assertEqual(
                    app.query_one(TabbedContent).active,
                    "positions-tab",
                )
                sniper_text = _plain_text(footer.render())
                self.assertIn("DRY RUN", sniper_text)
                self.assertIn("REQUEST LIVE", sniper_text)

                await pilot.press("escape")
                await pilot.pause()
                self.assertEqual(
                    app.query_one(TabbedContent).active,
                    "overview-tab",
                )


def _plain_text(rendered: object) -> str:
    if isinstance(rendered, Text):
        return rendered.plain
    return str(rendered)


if __name__ == "__main__":
    unittest.main()
