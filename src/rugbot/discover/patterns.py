"""Group recorded launches into repeatable patterns (who launches, who is in block 0).

A pattern is a set of launches sharing one identity:

* ``creator``: the same dev wallet created them (serial deployer, Type 1);
* ``insider``: the same non-dev wallet bought in their creation slot;
* ``cabal``: the same pair of non-dev wallets both bought in their creation slot.

Pure: input is each launch's creator and oldest-first trades; output maps a
pattern key to the launches it covers. Scoring happens elsewhere.
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from rugbot.backtest.launch_replay import LaunchTrade

KIND_CREATOR = "creator"
KIND_INSIDER = "insider"
KIND_CABAL = "cabal"


@dataclass(frozen=True, slots=True)
class Pattern:
    """Launches sharing one creator, block-0 insider, or block-0 pair."""

    kind: str
    members: tuple[str, ...]
    mints: tuple[str, ...]


def block0_buyers(creator: str, trades: Sequence[LaunchTrade]) -> frozenset[str]:
    """Non-dev wallets that bought in the launch's creation slot."""
    if not trades:
        return frozenset()
    create_slot = trades[0].slot
    return frozenset(
        trade.wallet
        for trade in trades
        if trade.slot == create_slot and trade.is_buy and trade.wallet != creator
    )


def find_patterns(
    launches: Mapping[str, tuple[str, Sequence[LaunchTrade]]], *, min_launches: int
) -> list[Pattern]:
    """Return every creator, insider and pair pattern covering enough launches."""
    groups: dict[tuple[str, tuple[str, ...]], list[str]] = {}
    for mint, (creator, trades) in launches.items():
        insiders = sorted(block0_buyers(creator, trades))
        keys = [(KIND_CREATOR, (creator,))]
        keys += [(KIND_INSIDER, (wallet,)) for wallet in insiders]
        keys += [(KIND_CABAL, pair) for pair in itertools.combinations(insiders, 2)]
        for key in keys:
            groups.setdefault(key, []).append(mint)
    insider_mints = {
        members[0]: set(mints)
        for (kind, members), mints in groups.items()
        if kind == KIND_INSIDER
    }
    return [
        Pattern(kind=kind, members=members, mints=tuple(mints))
        for (kind, members), mints in groups.items()
        if len(mints) >= min_launches
        # A pair covering exactly one member's launches adds nothing.
        and not (
            kind == KIND_CABAL
            and any(insider_mints[member] == set(mints) for member in members)
        )
    ]
