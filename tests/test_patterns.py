"""Pure-logic tests for launch pattern grouping."""

from rugbot.backtest.launch_replay import LaunchTrade
from rugbot.discover.patterns import (
    KIND_CABAL,
    KIND_CREATOR,
    KIND_INSIDER,
    find_patterns,
)


def _buy(slot: int, wallet: str) -> LaunchTrade:
    return LaunchTrade(
        slot=slot,
        timestamp_s=float(slot),
        wallet=wallet,
        is_buy=True,
        price_sol=3e-8,
        amount_sol=0.5,
        on_curve=True,
    )


def _launch(creator: str, block0: list[str], late: list[str]) -> tuple:
    trades = [_buy(100, creator)] + [_buy(100, w) for w in block0]
    trades += [_buy(105, w) for w in late]
    return creator, trades


def test_patterns_group_creator_insider_and_pair() -> None:
    launches = {
        "m1": _launch("devA", ["x", "y"], ["late"]),
        "m2": _launch("devB", ["x", "y"], ["late"]),
        "m3": _launch("devA", ["x"], []),
    }
    found = {
        (p.kind, p.members): set(p.mints)
        for p in find_patterns(launches, min_launches=2)
    }
    assert found[(KIND_CREATOR, ("devA",))] == {"m1", "m3"}
    assert found[(KIND_INSIDER, ("x",))] == {"m1", "m2", "m3"}
    # y's launches equal the pair's, so the pair adds nothing and is dropped.
    assert (KIND_CABAL, ("x", "y")) not in found
    # Late buyers and the dev's own buy are never insiders.
    assert (KIND_INSIDER, ("late",)) not in found
    assert (KIND_INSIDER, ("devA",)) not in found
