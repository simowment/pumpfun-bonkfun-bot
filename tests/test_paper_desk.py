"""Paper desk replayed on one real recorded launch (Pump program logs).

``fixtures/paper_desk/pump_launch_logs.json`` holds the ``Program data`` lines
of every confirmed transaction touching one SOL-curve launch for two minutes
after its create, recorded from ``logsSubscribe`` on the Pump program.
"""

import json
from pathlib import Path

import pytest

from rugbot.domain.decisions import AbstainResult
from rugbot.domain.pump_curve import market_cap_lamports
from rugbot.domain.trades import PumpTradeEventProof
from rugbot.ingest.pump.trade_event_decoder import (
    decode_pump_trade_event,
    pump_trade_payloads,
)
from rugbot.interfaces.cli.tracker import main as tracker_cli
from rugbot.runtime.workers.paper_desk import PaperDesk, tx_cost_lamports
from rugbot.storage.config_store import ConfigStore
from rugbot.storage.paper_journal import PaperJournal
from rugbot.tracker.funding_chain import fresh_funded_wallets

FIXTURE = Path(__file__).parent.parent / "fixtures/paper_desk/pump_launch_logs.json"
SLOT_MS = 400
LANDING_SLOTS = 2


def _launch() -> dict:
    return json.loads(FIXTURE.read_text())


def _trades(launch: dict) -> list[PumpTradeEventProof]:
    trades = []
    for notification in launch["notifications"]:
        for _, payload in pump_trade_payloads(notification["logs"]):
            event = decode_pump_trade_event(payload, notification["slot"])
            if not isinstance(event, AbstainResult) and event.mint == launch["mint"]:
                trades.append((notification["slot"], event))
    return trades


def _replay(
    desk: PaperDesk, launch: dict, clock: list[int], notifications: list[dict]
) -> None:
    first_slot = launch["notifications"][0]["slot"]
    for notification in notifications:
        clock[0] = (notification["slot"] - first_slot) * SLOT_MS
        desk.handle_logs(notification["slot"], notification["logs"])
        desk.tick()


@pytest.fixture
def state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.delenv("RUGBOT_DB_PATH", raising=False)
    return tmp_path


def _desk(state: Path, clock: list[int]) -> tuple[PaperDesk, PaperJournal]:
    journal = PaperJournal(state / "journal.sqlite3")
    desk = PaperDesk(
        ConfigStore(state_dir=state).list_trackers(),
        positions_dir=state / "positions",
        journal=journal,
        landing_slots=LANDING_SLOTS,
        clock_ms=lambda: clock[0],
    )
    return desk, journal


def test_creation_tracker_buys_at_landing_slot_and_takes_profit(state: Path) -> None:
    launch = _launch()
    creator = launch["creator"]
    assert (
        tracker_cli(
            [
                "--state-dir", str(state), "add", creator, "--size", "0.1",
                "--tp", "30:50,60:100", "--sl", "25:100", "--trail", "none",
                "--max-mc", "1000",
            ]
        )
        == 0
    )  # fmt: skip
    clock = [0]
    desk, journal = _desk(state, clock)
    # Stop the desk a few slots after the buy and restart it: the restored
    # position must keep being priced and still exit on its rules.
    create_slot = launch["notifications"][0]["slot"]
    early = [n for n in launch["notifications"] if n["slot"] <= create_slot + 10]
    _replay(desk, launch, clock, early)
    assert len(desk.open_positions) == 1
    desk.close()
    journal.close()
    desk, journal = _desk(state, clock)
    assert len(desk.open_positions) == 1
    _replay(desk, launch, clock, launch["notifications"][len(early) :])
    fills = journal.fills(creator)

    buy, *sells = fills
    # The buy fills at the curve state at the end of slot create + landing.
    landing_state = [
        event for slot, event in _trades(launch) if slot <= create_slot + LANDING_SLOTS
    ][-1]
    assert buy.side == "buy"
    assert buy.quote_lamports == 100_000_000
    assert buy.market_cap_lamports == market_cap_lamports(
        landing_state.virtual_sol_reserves_base_units,
        landing_state.virtual_token_reserves_base_units,
    )
    assert [sell.reason for sell in sells] == [
        "take_profit_level_0_triggered",
        "take_profit_level_1_triggered",
    ]
    assert sum(sell.tokens for sell in sells) == buy.tokens
    assert sells[-1].position_closed
    entry_cost = buy.quote_lamports + buy.tx_cost_lamports
    for sell in sells:
        assert sell.pnl_lamports == (
            sell.quote_lamports
            - entry_cost * sell.tokens // buy.tokens
            - sell.tx_cost_lamports
        )
    assert desk.open_positions == {}
    desk.close()

    # A restarted desk knows the coin was bought and holds nothing.
    restarted, _ = _desk(state, clock)
    assert restarted.open_positions == {}
    tracker = ConfigStore(state_dir=state).get_tracker(creator)
    assert buy.tx_cost_lamports == tx_cost_lamports(tracker.config)


def test_copy_tracker_exits_on_no_activity(state: Path) -> None:
    launch = _launch()
    create_slot = launch["notifications"][0]["slot"]
    copied = next(
        event.user
        for slot, event in _trades(launch)
        if slot > create_slot and event.is_buy and event.user != launch["creator"]
    )
    assert (
        tracker_cli(
            [
                "--state-dir", str(state), "add", copied, "--mode", "track_buys",
                "--size", "0.05", "--tp", "none", "--sl", "none", "--trail", "none",
                "--no-activity", "20", "--max-age", "0", "--max-mc", "1000",
            ]
        )
        == 0
    )  # fmt: skip
    clock = [0]
    desk, journal = _desk(state, clock)
    _replay(desk, launch, clock, launch["notifications"])
    # Nothing trades for a minute after the recording: the idle rule sells,
    # and the sell fills once the stream has moved past its landing slot.
    last_slot = launch["notifications"][-1]["slot"]
    clock[0] += 60_000
    desk.handle_logs(last_slot + 150, [])
    desk.tick()
    desk.handle_logs(last_slot + 160, [])
    desk.tick()
    fills = journal.fills(copied)
    assert [fill.side for fill in fills][:1] == ["buy"]
    assert fills[-1].side == "sell"
    assert fills[-1].position_closed
    assert "no_activity" in fills[-1].reason
    desk.close()


def _first_buy_then_sell(launch: dict, min_gap_slots: int) -> tuple[str, int, int]:
    """A wallet that bought, then sold at least ``min_gap_slots`` later."""
    first_buy: dict[str, int] = {}
    for slot, event in _trades(launch):
        if event.is_buy:
            first_buy.setdefault(event.user, slot)
        elif event.user in first_buy and slot - first_buy[event.user] >= min_gap_slots:
            return event.user, first_buy[event.user], slot
    pytest.fail("fixture has no buy-then-sell wallet")


def test_copy_sells_mirror_the_tracked_wallet(state: Path) -> None:
    launch = _launch()
    copied, _, sell_slot = _first_buy_then_sell(launch, min_gap_slots=10)
    assert (
        tracker_cli(
            [
                "--state-dir", str(state), "add", copied, "--mode", "track_buys",
                "--size", "0.05", "--tp", "none", "--sl", "none", "--trail", "none",
                "--copy-sells", "all", "--max-age", "0", "--max-mc", "1000",
            ]
        )
        == 0
    )  # fmt: skip
    clock = [0]
    desk, journal = _desk(state, clock)
    _replay(desk, launch, clock, launch["notifications"])
    buy, sell, *_ = journal.fills(copied)
    assert buy.side == "buy"
    assert sell.side == "sell"
    assert sell.reason == "copy_sell"
    assert sell.slot >= sell_slot
    assert sell.tokens == buy.tokens
    assert sell.position_closed
    desk.close()


def test_buy_on_dev_sell_enters_after_the_wallet_sells(state: Path) -> None:
    launch = _launch()
    seller, _, sell_slot = _first_buy_then_sell(launch, min_gap_slots=10)
    assert (
        tracker_cli(
            [
                "--state-dir", str(state), "add", seller, "--mode", "buy_on_dev_sell",
                "--size", "0.05", "--max-age", "0", "--max-mc", "1000",
            ]
        )
        == 0
    )  # fmt: skip
    clock = [0]
    desk, journal = _desk(state, clock)
    _replay(desk, launch, clock, launch["notifications"])
    buy = journal.fills(seller)[0]
    assert buy.side == "buy"
    assert buy.reason == "dev_sell"
    assert buy.slot >= sell_slot
    desk.close()


FUNDING_FIXTURE = (
    Path(__file__).parent.parent
    / "fixtures/finalized_transactions/native_funding/hub_funds_fresh_dev.json"
)
# Recorded: the hub that paid 2 SOL to a brand-new wallet which then created hive.
HUB = "7rtCHffCHNZrKb78FKVNeYF2ysFhSNnH5SyrJDKaCMs3"
FUNDED_DEV = "CoaX7BJWN8sdxQdHtbrdbRpnUhn4MxFadzWdo8CpS5jR"


def test_fresh_funded_wallets_reads_a_real_hub_transfer() -> None:
    result = json.loads(FUNDING_FIXTURE.read_text())["result"]
    assert fresh_funded_wallets(result, source=HUB) == [(FUNDED_DEV, 2_000_000_000)]
    # The funded wallet did not pay anyone: seen from it there is nothing.
    assert fresh_funded_wallets(result, source=FUNDED_DEV) == []


@pytest.mark.parametrize(
    ("funded_lamports", "armed_for_ms", "bought"),
    [
        (2_000_000_000, 0, True),
        (1_000_000_000, 0, False),  # below the tracker's range
        (2_000_000_000, 61_000, False),  # arming expired before the create
    ],
)
def test_funded_wallet_creations_buys_only_an_armed_wallets_create(
    state: Path, *, funded_lamports: int, armed_for_ms: int, bought: bool
) -> None:
    launch = _launch()
    assert (
        tracker_cli(
            [
                "--state-dir", str(state), "add", HUB,
                "--mode", "funded_wallet_creations", "--fund-min", "1.5",
                "--fund-max", "2.5", "--arm-minutes", "1", "--size", "0.1",
                "--max-mc", "1000",
            ]
        )
        == 0
    )  # fmt: skip
    clock = [-armed_for_ms]
    desk, journal = _desk(state, clock)
    armed = desk.arm(HUB, launch["creator"], funded_lamports)
    assert bool(armed) == (funded_lamports == 2_000_000_000)
    _replay(desk, launch, clock, launch["notifications"])
    fills = journal.fills(HUB)
    assert bool(fills) == bought
    if bought:
        assert fills[0].side == "buy"
        assert fills[0].reason == "create"
    desk.close()


def test_wallets_armed_below_a_funded_wallet_are_bought(state: Path) -> None:
    launch = _launch()
    relay = FUNDED_DEV  # the funded wallet; the recorded creator sits below it
    assert (
        tracker_cli(
            [
                "--state-dir", str(state), "add", HUB,
                "--mode", "funded_wallet_creations", "--fund-min", "1.5",
                "--fund-max", "2.5", "--arm-minutes", "1", "--hops", "9",
                "--size", "0.1", "--max-mc", "1000",
            ]
        )
        == 0
    )  # fmt: skip
    clock = [0]
    desk, journal = _desk(state, clock)
    # Nothing is armed below a wallet that is not itself armed.
    assert desk.arm_below(HUB, relay, [launch["creator"]]) == []
    assert desk.arm(HUB, relay, 2_000_000_000)
    assert desk.arm_below(HUB, relay, [relay, launch["creator"]])
    assert desk.armed_until(launch["creator"]) == desk.armed_until(relay)
    _replay(desk, launch, clock, launch["notifications"])
    buy = journal.fills(HUB)[0]
    assert (buy.side, buy.reason) == ("buy", "create")
    desk.close()
