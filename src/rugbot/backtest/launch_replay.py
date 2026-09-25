"""Event-driven replay of one Pump launch from its complete trade history.

Each launch is replayed trade by trade from pump swap-api fills (slot, SOL
price, wallet, side). Our entry lands at the end of slot ``create + N`` (the
worst in-slot price for a buyer), and every exit trigger (take-profit, stop,
dev/bundle sell, max hold) fills at the curve state ``reaction_slots`` after the
triggering trade, never at the trigger level itself. Fills are priced with the
exact integer bonding-curve quote engine on reserves reconstructed from the
observed SOL price through the constant-product invariant.
"""

from __future__ import annotations

import bisect
import math
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING

from rugbot.backtest.pairs_lab import wilson_interval
from rugbot.domain.fees import FeeConfig
from rugbot.domain.quote_engine import pump_curve_buy_amounts, pump_curve_sell_amounts

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping, Sequence

LAMPORTS_PER_SOL = 1_000_000_000
TOKEN_DECIMALS = 6
TOKEN_SUPPLY_UI = 1_000_000_000
# Pump curve launch state: 30 SOL x 1.073B tokens virtual, 793.1M tokens real.
INITIAL_VIRTUAL_QUOTE = 30 * LAMPORTS_PER_SOL
INITIAL_VIRTUAL_BASE = 1_073_000_000 * 10**TOKEN_DECIMALS
CURVE_INVARIANT = INITIAL_VIRTUAL_QUOTE * INITIAL_VIRTUAL_BASE
# priceSol is SOL per whole token; reserves ratio is lamports per base unit.
PRICE_TO_RESERVE_RATIO = LAMPORTS_PER_SOL // 10**TOKEN_DECIMALS
PUMP_PROGRAM_LABEL = "pump"
PUMP_CURVE_FEE_CONFIG = FeeConfig(
    version="pump-global-v1",
    protocol_fee_bps=95,
    creator_fee_bps=30,
    is_known=True,
    program_config_version="pump-global-v1",
    valid_from_slot=0,
    valid_to_slot=None,
    source_artifact_version="pump-global-v1",
    lp_fee_bps=0,
)
# PumpSwap charges lp + protocol + creator fees on graduated pools.
PUMPSWAP_TOTAL_FEE_BPS = 30
BPS_DENOMINATOR = 10_000
SLOT_INDEX_SLOT_DIGITS = 12

EXIT_TAKE_PROFIT = "take_profit"
EXIT_STOP_LOSS = "stop_loss"
EXIT_DEV_SELL = "dev_sell"
EXIT_MAX_HOLD = "max_hold"
EXIT_END_OF_DATA = "end_of_data"


# Tolerance when matching a coin's curve invariant to the standard curve.
CURVE_INVARIANT_TOLERANCE = 0.01


def nonstandard_curve_reason(
    curve_invariant: int | None, *, mayhem: bool
) -> str | None:
    """Return why a coin cannot be replayed on the standard curve, if it can't.

    Mayhem-mode coins are traded by Pump's protocol agent on inflated virtual
    reserves with almost no real SOL, so standard-curve fills would be fiction.
    """
    if mayhem:
        return "Mayhem-mode coin (protocol agent, no real curve liquidity)"
    if curve_invariant is None:
        return "curve reserves unavailable"
    if (
        abs(curve_invariant - CURVE_INVARIANT)
        > CURVE_INVARIANT * CURVE_INVARIANT_TOLERANCE
    ):
        return "non-standard bonding curve"
    return None


class LaunchReplayError(ValueError):
    """Trade history cannot be narrowed into a replayable launch."""


@dataclass(frozen=True, slots=True)
class LaunchTrade:
    """One finalized fill on a launch, oldest-first."""

    slot: int
    timestamp_s: float
    wallet: str
    is_buy: bool
    price_sol: float
    amount_sol: float
    on_curve: bool


@dataclass(frozen=True, slots=True)
class ReplayCosts:
    """Per-trade execution settings and costs (both legs pay the tx costs)."""

    quote_size_sol: float = 0.1
    entry_delay_slots: int = 2
    reaction_slots: int = 2
    base_fee_sol: float = 0.000005
    priority_fee_sol: float = 0.0005
    jito_tip_sol: float = 0.001


@dataclass(frozen=True, slots=True)
class ExitRule:
    """One exit policy evaluated across launches."""

    take_profit_pct: float | None
    stop_loss_pct: float | None
    exit_on_dev_sell: bool
    max_hold_s: float | None


@dataclass(frozen=True, slots=True)
class LaunchProfile:
    """Rule-independent facts about a launch after our realistic entry."""

    mint: str
    create_slot: int
    entry_slot: int
    entry_mc_sol: float
    ath_mc_sol: float
    ath_multiple: float
    seconds_to_ath: float
    first_insider_sell_s: float | None
    floor_mc_after_ath_sol: float
    graduated: bool


@dataclass(frozen=True, slots=True)
class LaunchResult:
    """Outcome of one launch under one exit rule."""

    mint: str
    exit_reason: str
    exit_mc_sol: float
    held_s: float
    net_pnl_sol: float
    fees_sol: float


def market_cap_sol(price_sol: float) -> float:
    """Return the fully diluted market cap in SOL for a Pump token price."""
    return price_sol * TOKEN_SUPPLY_UI


def trades_from_swap_api(
    raw_trades: Iterable[Mapping[str, object]],
) -> list[LaunchTrade]:
    """Narrow pump swap-api trade dicts into ordered ``LaunchTrade`` values.

    Raises:
        LaunchReplayError: When a trade misses a required field.
    """
    trades: list[LaunchTrade] = []
    for raw in raw_trades:
        slot_index = raw.get("slotIndexId")
        wallet = raw.get("userAddress")
        side = raw.get("type")
        stamp = raw.get("timestamp")
        if not (
            isinstance(slot_index, str)
            and len(slot_index) > SLOT_INDEX_SLOT_DIGITS
            and isinstance(wallet, str)
            and side in ("buy", "sell")
            and isinstance(stamp, str)
        ):
            raise LaunchReplayError(f"malformed swap-api trade: {raw!r}")  # noqa: TRY003
        try:
            price = float(Decimal(str(raw.get("priceSol"))))
            amount = float(Decimal(str(raw.get("amountSol"))))
        except (InvalidOperation, ValueError) as error:
            raise LaunchReplayError(f"non-numeric price in trade {raw!r}") from error  # noqa: TRY003
        trades.append(
            LaunchTrade(
                slot=int(slot_index[:SLOT_INDEX_SLOT_DIGITS]),
                timestamp_s=datetime.fromisoformat(stamp).timestamp(),
                wallet=wallet,
                is_buy=side == "buy",
                price_sol=price,
                amount_sol=amount,
                on_curve=raw.get("program") == PUMP_PROGRAM_LABEL,
            )
        )
    trades.sort(key=lambda trade: trade.slot)
    return trades


def _virtual_reserves(price_sol: float) -> tuple[int, int]:
    """Reconstruct ``(virtual_quote, virtual_base)`` from a spot price.

    On the constant-product curve ``quote * base = k`` and the spot price is
    ``quote / base``, so ``quote = sqrt(k * price)``.
    """
    virtual_quote = math.isqrt(
        int(CURVE_INVARIANT * price_sol * PRICE_TO_RESERVE_RATIO)
    )
    return virtual_quote, CURVE_INVARIANT // max(1, virtual_quote)


def _buy_tokens(price_sol: float, quote_lamports: int) -> tuple[int, int]:
    """Return ``(tokens_out, fee_lamports)`` for a curve buy at this price."""
    virtual_quote, virtual_base = _virtual_reserves(price_sol)
    return pump_curve_buy_amounts(
        virtual_quote_reserves=virtual_quote,
        virtual_base_reserves=virtual_base,
        spendable_quote_in=quote_lamports,
        fee_config=PUMP_CURVE_FEE_CONFIG,
    )


def _sell_lamports(trade: LaunchTrade, tokens: int) -> tuple[int, int]:
    """Return ``(proceeds, fee)`` in lamports for selling into this trade's state."""
    if trade.on_curve:
        virtual_quote, virtual_base = _virtual_reserves(trade.price_sol)
        proceeds, fee = pump_curve_sell_amounts(
            virtual_quote_reserves=virtual_quote,
            virtual_base_reserves=virtual_base,
            base_input_amount=tokens,
            fee_config=PUMP_CURVE_FEE_CONFIG,
        )
        # The curve cannot pay out more SOL than it holds.
        return min(proceeds, max(0, virtual_quote - INITIAL_VIRTUAL_QUOTE)), fee
    # Graduated pool: no curve state; price at spot less PumpSwap fees. Pool
    # depth is not modeled, so price impact on exit is understated.
    gross = int(tokens / 10**TOKEN_DECIMALS * trade.price_sol * LAMPORTS_PER_SOL)
    fee = gross * PUMPSWAP_TOTAL_FEE_BPS // BPS_DENOMINATOR
    return gross - fee, fee


class LaunchReplay:
    """Replay one launch: realistic entry once, then any number of exit rules."""

    def __init__(  # noqa: PLR0913 - keyword-only replay inputs
        self,
        mint: str,
        *,
        create_slot: int,
        creator: str,
        trades: Sequence[LaunchTrade],
        costs: ReplayCosts,
        entry_slot: int | None = None,
        signal_wallets: frozenset[str] | None = None,
    ) -> None:
        """Fix the entry for a launch.

        Args:
            mint: Coin mint.
            create_slot: Creation slot.
            creator: Creator wallet.
            trades: Full oldest-first trade history.
            costs: Execution settings and costs.
            entry_slot: Slot our buy lands in; defaults to create + entry delay
                (sniping). Copy-trading passes leader buy slot + delay.
            signal_wallets: Wallets whose sells trigger the sell-signal exit;
                defaults to the creator and block-0 buyers (insiders).

        Raises:
            LaunchReplayError: When no trade exists at or after the entry slot.
        """
        self.mint = mint
        self.costs = costs
        self._trades = list(trades)
        self._slots = [trade.slot for trade in self._trades]
        self._insiders = signal_wallets or frozenset(
            {creator}
            | {
                trade.wallet
                for trade in self._trades
                if trade.slot == create_slot and trade.is_buy
            }
        )
        if entry_slot is None:
            entry_slot = create_slot + costs.entry_delay_slots
        self._entry_index = bisect.bisect_right(self._slots, entry_slot) - 1
        if self._entry_index < 0 or self._entry_index == len(self._trades) - 1:
            raise LaunchReplayError(f"{mint}: no trading after entry slot")  # noqa: TRY003
        entry = self._trades[self._entry_index]
        self._entry = entry
        self._quote_lamports = int(costs.quote_size_sol * LAMPORTS_PER_SOL)
        self._tokens, self._buy_fee = _buy_tokens(entry.price_sol, self._quote_lamports)
        self.profile = self._profile(create_slot, entry_slot)

    def _profile(self, create_slot: int, entry_slot: int) -> LaunchProfile:
        after = self._trades[self._entry_index + 1 :]
        ath_trade = max(after, key=lambda trade: trade.price_sol)
        after_ath = [trade for trade in after if trade.slot >= ath_trade.slot]
        created_at = self._trades[0].timestamp_s
        insider_sell = next(
            (
                trade
                for trade in self._trades
                if not trade.is_buy and trade.wallet in self._insiders
            ),
            None,
        )
        return LaunchProfile(
            mint=self.mint,
            create_slot=create_slot,
            entry_slot=entry_slot,
            entry_mc_sol=market_cap_sol(self._entry.price_sol),
            ath_mc_sol=market_cap_sol(ath_trade.price_sol),
            ath_multiple=ath_trade.price_sol / self._entry.price_sol,
            seconds_to_ath=ath_trade.timestamp_s - self._entry.timestamp_s,
            first_insider_sell_s=(
                insider_sell.timestamp_s - created_at if insider_sell else None
            ),
            floor_mc_after_ath_sol=market_cap_sol(min(t.price_sol for t in after_ath)),
            graduated=any(not trade.on_curve for trade in after),
        )

    def _fill_after(self, trigger: LaunchTrade) -> LaunchTrade:
        """Curve state when our exit lands, ``reaction_slots`` after a trigger."""
        index = bisect.bisect_right(
            self._slots, trigger.slot + self.costs.reaction_slots
        )
        return self._trades[max(index - 1, self._entry_index + 1)]

    def run(self, rule: ExitRule) -> LaunchResult:
        """Replay the launch under one exit rule."""
        entry_price = self._entry.price_sol
        tp_price = (
            entry_price * (1 + rule.take_profit_pct / 100)
            if rule.take_profit_pct is not None
            else math.inf
        )
        sl_price = (
            entry_price * (1 - rule.stop_loss_pct / 100)
            if rule.stop_loss_pct is not None
            else -math.inf
        )
        exit_trade = self._trades[-1]
        reason = EXIT_END_OF_DATA
        for trade in self._trades[self._entry_index + 1 :]:
            if (
                rule.max_hold_s is not None
                and trade.timestamp_s - self._entry.timestamp_s >= rule.max_hold_s
            ):
                exit_trade, reason = trade, EXIT_MAX_HOLD
                break
            if trade.price_sol >= tp_price:
                exit_trade, reason = self._fill_after(trade), EXIT_TAKE_PROFIT
                break
            if (
                rule.exit_on_dev_sell
                and not trade.is_buy
                and trade.wallet in self._insiders
            ):
                exit_trade, reason = self._fill_after(trade), EXIT_DEV_SELL
                break
            if trade.price_sol <= sl_price:
                exit_trade, reason = self._fill_after(trade), EXIT_STOP_LOSS
                break
        proceeds, sell_fee = _sell_lamports(exit_trade, self._tokens)
        tx_costs = 2 * int(
            (
                self.costs.base_fee_sol
                + self.costs.priority_fee_sol
                + self.costs.jito_tip_sol
            )
            * LAMPORTS_PER_SOL
        )
        net = proceeds - self._quote_lamports - tx_costs
        return LaunchResult(
            mint=self.mint,
            exit_reason=reason,
            exit_mc_sol=market_cap_sol(exit_trade.price_sol),
            held_s=exit_trade.timestamp_s - self._entry.timestamp_s,
            net_pnl_sol=net / LAMPORTS_PER_SOL,
            fees_sol=(self._buy_fee + sell_fee + tx_costs) / LAMPORTS_PER_SOL,
        )


# Take-profit levels (percent) and stops evaluated when optimizing an entity's
# exit. The dev-sell exit is kept only as a comparison: serial ruggers flip
# within seconds while the coin keeps running, so it is not a default stop.
TAKE_PROFIT_GRID_PCT = (25.0, 50.0, 75.0, 100.0, 150.0, 200.0, 300.0, 500.0)
STOP_LOSS_GRID_PCT = (None, 30.0, 50.0)
MAX_HOLD_GRID_S = (None, 300.0, 1800.0)


@dataclass(frozen=True, slots=True)
class RuleSummary:
    """Aggregate performance of one exit rule across an entity's launches."""

    rule: ExitRule
    samples: int
    wins: int
    winrate: float
    net_ev_sol: float
    net_total_sol: float
    roi_pct: float
    fees_sol: float
    worst_loss_sol: float
    conservative_ev_sol: float
    ev_without_best_sol: float
    results: tuple[LaunchResult, ...]


def default_exit_rules() -> list[ExitRule]:
    """Return the TP x stop x hold grid plus the dev-sell comparison rule."""
    rules = [
        ExitRule(
            take_profit_pct=tp,
            stop_loss_pct=sl,
            exit_on_dev_sell=False,
            max_hold_s=hold,
        )
        for tp in TAKE_PROFIT_GRID_PCT
        for sl in STOP_LOSS_GRID_PCT
        for hold in MAX_HOLD_GRID_S
    ]
    rules.append(ExitRule(None, None, exit_on_dev_sell=True, max_hold_s=None))
    return rules


def _conservative_ev(pnls: Sequence[float]) -> float:
    """Bible EV with the winrate at its 95% Wilson lower bound.

    ``EV = p * avg_win - (1 - p) * avg_loss``. A take-profit that only a few
    outliers reach has a wide interval, so its lower-bound winrate (and EV)
    drops; a level many launches reach keeps most of its EV.
    """
    wins = [pnl for pnl in pnls if pnl > 0]
    losses = [-pnl for pnl in pnls if pnl <= 0]
    win_floor, _ = wilson_interval(len(wins), len(pnls))
    avg_win = sum(wins) / len(wins) if wins else 0.0
    avg_loss = sum(losses) / len(losses) if losses else 0.0
    return win_floor * avg_win - (1 - win_floor) * avg_loss


def summarize_rules(
    replays: Sequence[LaunchReplay], rules: Sequence[ExitRule]
) -> list[RuleSummary]:
    """Evaluate every rule on every launch, best conservative EV first."""
    summaries: list[RuleSummary] = []
    for rule in rules:
        results = tuple(replay.run(rule) for replay in replays)
        if not results:
            continue
        wins = sum(1 for result in results if result.net_pnl_sol > 0)
        net_total = sum(result.net_pnl_sol for result in results)
        stake = sum(replay.costs.quote_size_sol for replay in replays)
        pnls = sorted(result.net_pnl_sol for result in results)
        summaries.append(
            RuleSummary(
                rule=rule,
                samples=len(results),
                wins=wins,
                winrate=wins / len(results),
                net_ev_sol=net_total / len(results),
                net_total_sol=net_total,
                roi_pct=100 * net_total / stake,
                fees_sol=sum(result.fees_sol for result in results),
                worst_loss_sol=pnls[0],
                conservative_ev_sol=_conservative_ev(pnls),
                ev_without_best_sol=(
                    sum(pnls[:-1]) / (len(pnls) - 1) if len(pnls) > 1 else 0.0
                ),
                results=results,
            )
        )
    return sorted(
        summaries, key=lambda summary: summary.conservative_ev_sol, reverse=True
    )
