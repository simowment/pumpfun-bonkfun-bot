"""rug_tracker CLI against a real SQLite config store."""

from pathlib import Path

import pytest

from rugbot.decision.playbook_rules import SellLevel
from rugbot.interfaces.cli.tracker import main
from rugbot.runtime.config import ExecutionMode, TrackingMode
from rugbot.storage.config_store import ConfigStore

DEV = "4Sr8W6V1c4jBgQZANL4PAaZwoAzAR6T1mpie2xFFsVq4"
COPIED = "AmyjEXggNhKW54GybHs7VpYEHn4agd8aP28HXc58hxGq"


def test_tracker_lifecycle(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("RUGBOT_DB_PATH", raising=False)

    def run(*args: str) -> int:
        return main(["--state-dir", str(tmp_path), *args])

    assert (
        run(
            "add", DEV, "--group", "burners", "--size", "0.1",
            "--tp", "100:50,300:100", "--sl", "30:100", "--trail", "none",
        )
        == 0
    )  # fmt: skip
    store = ConfigStore(state_dir=tmp_path)
    tracker = store.get_tracker(DEV)
    assert tracker is not None
    assert tracker.config.execution.mode is ExecutionMode.PAPER
    assert tracker.config.execution.quote_size_lamports == 100_000_000
    assert tracker.config.risk.max_buy_lamports >= 100_000_000
    assert tracker.config.rules.sell.take_profit_levels == (
        SellLevel(trigger_pnl_ppm=1_000_000, sell_fraction_ppm=500_000),
        SellLevel(trigger_pnl_ppm=3_000_000, sell_fraction_ppm=1_000_000),
    )
    assert tracker.config.rules.sell.stop_loss_levels == (
        SellLevel(trigger_pnl_ppm=-300_000, sell_fraction_ppm=1_000_000),
    )

    # Rules the engine cannot evaluate are rejected before they are stored.
    assert run("add", DEV) == 1
    assert run("set", DEV, "--tp", "50:50,20:100") == 1
    assert run("set", DEV, "--set", "rules.unknown=1") == 1
    assert store.get_tracker(DEV) == tracker

    assert run("preset", "save", "burner", "--from", DEV) == 0
    assert run("add", COPIED, "--preset", "burner", "--mode", "track_buys") == 0
    copied = store.get_tracker(COPIED)
    assert copied is not None
    assert copied.config.tracking_mode is TrackingMode.TRACK_BUYS
    assert copied.config.rules.sell == tracker.config.rules.sell

    assert run("disable", COPIED) == 0
    assert not store.get_tracker(COPIED).enabled
    assert [item.wallet for item in store.list_trackers("burners")] == [DEV]
    assert run("rm", COPIED) == 0
    assert store.get_tracker(COPIED) is None
