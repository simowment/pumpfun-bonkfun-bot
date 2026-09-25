"""Score wallets across many launches from full trade histories (Bible Method 2).

For every launch, each wallet's first buy is measured in slots after creation
and its realized SOL result is ``sol out - sol in``. Across launches a wallet is
ranked by the 95% Wilson lower bound of its winrate, so a handful of lucky
coins cannot outrank a wallet that wins consistently. Unsold bags count as the
SOL spent: a wallet only wins a launch by taking SOL back out.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass
from typing import TYPE_CHECKING

from rugbot.backtest.pairs_lab import wilson_interval

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

    from rugbot.backtest.launch_replay import LaunchTrade

# Bible: a wallet worth copying enters in block 0 or early block 1.
EARLY_ENTRY_SLOTS = 2


@dataclass(frozen=True, slots=True)
class WalletLaunch:
    """One wallet's activity on one launch."""

    mint: str
    first_buy_slot: int
    entry_delay_slots: int
    sol_in: float
    sol_out: float
    first_sell_s: float | None

    @property
    def realized_pnl_sol(self) -> float:
        """SOL taken out minus SOL put in (unsold tokens count as zero)."""
        return self.sol_out - self.sol_in


@dataclass(frozen=True, slots=True)
class WalletScore:
    """A wallet's record across launches."""

    wallet: str
    launches: tuple[WalletLaunch, ...]
    wins: int
    winrate: float
    winrate_floor: float
    total_pnl_sol: float
    median_entry_delay_slots: float
    early_share: float


def wallet_launches(
    mint: str, trades: Sequence[LaunchTrade]
) -> dict[str, WalletLaunch]:
    """Summarize every buyer's activity on one launch (trades oldest-first)."""
    if not trades:
        return {}
    create_slot = trades[0].slot
    created_at = trades[0].timestamp_s
    buys: dict[str, list[LaunchTrade]] = {}
    sells: dict[str, list[LaunchTrade]] = {}
    for trade in trades:
        (buys if trade.is_buy else sells).setdefault(trade.wallet, []).append(trade)
    return {
        wallet: WalletLaunch(
            mint=mint,
            first_buy_slot=wallet_buys[0].slot,
            entry_delay_slots=wallet_buys[0].slot - create_slot,
            sol_in=sum(trade.amount_sol for trade in wallet_buys),
            sol_out=sum(trade.amount_sol for trade in sells.get(wallet, ())),
            first_sell_s=(
                sells[wallet][0].timestamp_s - created_at if wallet in sells else None
            ),
        )
        for wallet, wallet_buys in buys.items()
    }


def score_wallets(
    per_launch: Iterable[dict[str, WalletLaunch]],
    *,
    min_launches: int,
    min_median_sol_in: float = 0.0,
) -> list[WalletScore]:
    """Rank wallets seen on at least ``min_launches`` launches, most reliable first.

    ``min_median_sol_in`` drops wallets whose typical position is too small to
    copy (volume bots cycling dust).
    """
    by_wallet: dict[str, list[WalletLaunch]] = {}
    for launch in per_launch:
        for wallet, activity in launch.items():
            by_wallet.setdefault(wallet, []).append(activity)
    scores: list[WalletScore] = []
    for wallet, activity in by_wallet.items():
        if (
            len(activity) < min_launches
            or statistics.median(item.sol_in for item in activity) < min_median_sol_in
        ):
            continue
        wins = sum(1 for item in activity if item.realized_pnl_sol > 0)
        floor, _ = wilson_interval(wins, len(activity))
        scores.append(
            WalletScore(
                wallet=wallet,
                launches=tuple(activity),
                wins=wins,
                winrate=wins / len(activity),
                winrate_floor=floor,
                total_pnl_sol=sum(item.realized_pnl_sol for item in activity),
                median_entry_delay_slots=statistics.median(
                    item.entry_delay_slots for item in activity
                ),
                early_share=sum(
                    1
                    for item in activity
                    if item.entry_delay_slots <= EARLY_ENTRY_SLOTS
                )
                / len(activity),
            )
        )
    return sorted(
        scores,
        key=lambda score: (score.winrate_floor, score.total_pnl_sol),
        reverse=True,
    )
