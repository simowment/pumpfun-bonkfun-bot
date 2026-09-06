"""Settings DOM integrity: every spec-referenced widget id must exist in the TUI."""

import asyncio
from pathlib import Path

from textual.widgets import DataTable, TabbedContent, TabPane

import rugbot.interfaces.tui.app as tui_mod
from rugbot.discover.ruggers import rank_ruggers
from rugbot.discover.store import ensure_discover_schema, upsert_launch
from rugbot.interfaces.tui.app import RugbotTuiApp
from rugbot.interfaces.tui.settings_spec import (
    BIG_BUY_LEVEL_COUNT,
    DIP_LEVEL_COUNT,
    EXIT_LEVEL_COUNT,
    TRAILING_LEVEL_COUNT,
    build_settings_document,
)
from rugbot.interfaces.tui.widgets import TargetsTable
from rugbot.storage.database import DatabaseManager

INPUT_IDS = [
    "target-wallet",
    "target-alias",
    "snipe-size-sol",
    "priority-fee",
    "jito-tip",
    "max-gas-cap",
    "take-profit-pct",
    "stop-loss-pct",
    "max-slippage",
    "snipe-delay",
    "rule-no-activity",
    "max-entry-mc",
    "min-winrate-pct",
    "rule-max-losses",
    "routing-policy",
    "execution-mode",
    "target-kind",
    # Background full-schema fields
    "compute-unit-limit",
    "loaded-accounts-limit",
    "signer-pubkey",
    "tracking-mode",
    "jito-url",
    "volume-bankroll",
    "volume-independent",
    "volume-impact",
    "strategy-min-volume",
    "strategy-max-creator-pairs",
    "strategy-history-samples",
    "strategy-max-buys-hour",
    "strategy-max-entry-index",
    "strategy-max-deviation",
    "rule-min-mc",
    "rule-max-mc",
    "rule-max-age",
    "rule-cooldown",
]

CHECKBOX_IDS = [
    "execution-mode-live",
    "require-block-zero",
    "require-funding-match",
    "rule-buy-once",
    "strategy-bundle",
    "strategy-double-signature",
    "strategy-prior-zero",
    "strategy-historical",
]


def test_composed_dom_contains_every_settings_widget():  # noqa: C901
    """All ids read/written by the pure settings round-trip must be mounted."""

    async def run() -> None:  # noqa: C901
        app = RugbotTuiApp()
        async with app.run_test(size=(120, 36)):
            mounted_input_ids = {w.id for w in app.query("Input")}
            mounted_checkbox_ids = {w.id for w in app.query("Checkbox")}

            missing_inputs = set(INPUT_IDS) - mounted_input_ids
            assert not missing_inputs, (
                f"missing Input widgets: {sorted(missing_inputs)}"
            )
            missing_checks = set(CHECKBOX_IDS) - mounted_checkbox_ids
            assert not missing_checks, (
                f"missing Checkbox widgets: {sorted(missing_checks)}"
            )

            for i in range(DIP_LEVEL_COUNT):
                for suffix in ("drawdown", "size"):
                    assert f"dip-{i}-{suffix}" in mounted_input_ids
            for i in range(EXIT_LEVEL_COUNT):
                for prefix in ("tp", "sl"):
                    for suffix in ("trigger", "fraction"):
                        assert f"{prefix}-{i}-{suffix}" in mounted_input_ids
            for i in range(TRAILING_LEVEL_COUNT):
                for suffix in ("mc", "drawdown"):
                    assert f"trail-{i}-{suffix}" in mounted_input_ids
            for i in range(BIG_BUY_LEVEL_COUNT):
                for suffix in ("min", "max", "fraction"):
                    assert f"big-{i}-{suffix}" in mounted_input_ids

            # Core layout anchors survive the restructure unchanged.
            app.query_one("#targets-table", TargetsTable)
            for selector in (
                "#launches-table",
                "#positions-table",
                "#nodes-table",
                "#edges-table",
                "#ruggers-table",
            ):
                app.query_one(selector, DataTable)

    asyncio.run(run())


def test_settings_round_trip_on_default_widget_values():
    """Default widget values must build a document without SniperConfigError."""
    defaults = {
        "target-wallet": "",
        "target-kind": "wallet",
        "tracking-mode": "new_token_creations",
        "execution-mode": "observe",
        "snipe-size-sol": "0.010",
        "max-slippage": "500",
        "priority-fee": "50000",
        "jito-tip": "0.0010",
        "routing-policy": "jito",
        "compute-unit-limit": "400000",
        "loaded-accounts-limit": "128000",
        "signer-pubkey": "",
        "jito-url": "",
        "volume-bankroll": "100000",
        "volume-independent": "25000",
        "volume-impact": "100000",
        "strategy-min-volume": "30000000000",
        "strategy-max-creator-pairs": "10",
        "strategy-history-samples": "10",
        "strategy-min-winrate": "40.0",
        "min-winrate-pct": "40.0",
        "strategy-max-buys-hour": "1",
        "strategy-max-entry-index": "1",
        "max-entry-mc": "15000",
        "strategy-max-deviation": "250000",
        "rule-min-mc": "",
        "rule-max-mc": "",
        "rule-max-age": "0",
        "rule-cooldown": "0",
        "rule-max-losses": "3",
        "snipe-delay": "0",
        "take-profit-pct": "100.0",
        "stop-loss-pct": "-30.0",
        "rule-no-activity": "0",
        "require-block-zero": True,
        "require-funding-match": True,
        "execution-mode-live": False,
        "rule-buy-once": False,
        "strategy-bundle": False,
        "strategy-double-signature": False,
        "strategy-prior-zero": False,
        "strategy-historical": False,
    }
    document = build_settings_document(defaults)
    assert document["execution"]["mode"] == "observe"
    assert document["execution"]["quote_size_lamports"] == 10_000_000
    assert (
        document["rules"]["sell"]["take_profit_levels"][0]["trigger_pnl_ppm"]
        == 1_000_000
    )
    assert (
        document["rules"]["sell"]["stop_loss_levels"][0]["trigger_pnl_ppm"] == -300_000
    )


_RUGGER_DEV = "CvoPbuS2AghzVBYJx7HfQGhALiqif4YwWgHvXmhehuJZ"


def test_ruggers_tab_renders_ranked_rows(tmp_path: Path, monkeypatch) -> None:
    """The '7: Ruggers' tab mounts a DataTable that renders ranked entities.

    Read-only (§7): the tab surfaces the bible-qualified entity ranking from the
    headless discover DB (``.state/discover/rugbot.db``, CWD-relative) and
    recommends arming; it never auto-arms. Ranking runs in a thread worker, so
    the test drains workers before asserting rows.
    """

    monkeypatch.chdir(tmp_path)
    discover_dir = tmp_path / ".state" / "discover"
    discover_dir.mkdir(parents=True, exist_ok=True)
    db = DatabaseManager(discover_dir / "rugbot.db")
    ensure_discover_schema(db)
    for index, slot in enumerate([100, 200, 300], start=1):
        upsert_launch(
            db,
            mint=f"MNT{index}",
            creator=_RUGGER_DEV,
            created_signature=f"sig{index}",
            created_slot=slot,
            created_at=f"2026-08-26T08:{index * 10:02d}:00+00:00",
        )
    db.close()

    # Mocked RPC: honest no-rpc in-window ranking keeps the test network-free.
    monkeypatch.setattr(
        tui_mod,
        "rank_ruggers",
        lambda **kwargs: rank_ruggers(use_rpc=False, **kwargs),
    )

    async def run() -> None:
        app = RugbotTuiApp()
        async with app.run_test(size=(120, 36)) as pilot:
            # The tab and its ranking table are mounted in the DOM.
            app.query_one("#ruggers-tab", TabPane)
            table = app.query_one("#ruggers-table", DataTable)

            # The bound action switches to the tab and ranks from the seeded DB.
            app.action_show_ruggers()
            assert app.query_one(TabbedContent).active == "ruggers-tab"
            await pilot.pause()
            worker = next((w for w in app.workers if w.name == "ruggers_rank"), None)
            if worker is not None:
                await worker.wait()
                await pilot.pause()
            assert table.row_count >= 1
            assert any(str(row_key.value) == _RUGGER_DEV for row_key in table.rows)

    asyncio.run(run())
