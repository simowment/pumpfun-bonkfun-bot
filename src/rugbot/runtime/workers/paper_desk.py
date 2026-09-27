"""Paper-trade every enabled tracker from one Pump program log stream.

One ``logsSubscribe`` on the Pump program carries every create and trade with
exact post-trade reserves, so a single socket serves any number of trackers:

* a tracked dev's create (``new_token_creations``), a tracked wallet's buy
  (``track_buys``) or a tracked dev's sell (``buy_on_dev_sell``) opens a
  pending entry, gated by the tracker's entry rules;
* a tracked wallet selling a coin its tracker holds is mirrored when the
  tracker's ``copy_sells`` is ``all`` or ``percent``;
* every trade of a held coin marks its positions to market through the
  canonical exit rules (multi-level TP/SL, trailing stop, no-activity).

A decision taken in slot ``s`` fills at the curve state at the end of slot
``s + landing_slots`` (the worst in-slot price, as in the launch backtest),
with the fee rates that coin's trades actually paid. Our own fill does not move
the simulated curve. Nothing is signed or submitted: fills go to the journal.
"""

from __future__ import annotations

import dataclasses
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from rugbot.decision.playbook_rules import (
    CopySellMode,
    EntryRuleAction,
    EntryRuleInput,
    EntryRuleState,
    evaluate_entry_rules,
)
from rugbot.domain.amounts import LAMPORTS_PER_SOL, PPM_SCALE, Slot, TokenBaseUnits
from rugbot.domain.decisions import AbstainResult
from rugbot.domain.fees import BASE_SIGNATURE_FEE_LAMPORTS, FeeConfig
from rugbot.domain.pump_curve import (
    PUMP_CURVE_FEE_CONFIG,
    market_cap_lamports,
    nonstandard_curve_reason,
)
from rugbot.domain.quote_engine import pump_curve_buy_amounts, pump_curve_sell_amounts
from rugbot.execution.position_runtime import (
    PaperPositionState,
    PositionMarketEvidence,
    advance_paper_position,
)
from rugbot.ingest.pump.create_event_decoder import (
    SOL_PUBKEY,
    PumpCreateEvent,
    decode_pump_create_event_logs,
)
from rugbot.ingest.pump.trade_event_decoder import (
    decode_pump_trade_event,
    pump_trade_payloads,
)
from rugbot.runtime.config import ExecutionMode, TrackingMode
from rugbot.storage.paper_journal import MarketSnapshot, PaperFill
from rugbot.storage.sqlite_state_store import SqliteStateStore

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence
    from pathlib import Path

    from rugbot.domain.trades import PumpTradeEventProof
    from rugbot.runtime.config import CoreSniperConfig
    from rugbot.storage.config_store import Tracker
    from rugbot.storage.paper_journal import PaperJournal

DEFAULT_LANDING_SLOTS = 2
SLOT_MS = 400
# Which tracked-wallet trade opens an entry, per tracking mode.
WALLET_TRIGGERS = {
    TrackingMode.TRACK_BUYS: (True, "copy_buy"),
    TrackingMode.BUY_ON_DEV_SELL: (False, "dev_sell"),
}
MICROLAMPORTS_PER_LAMPORT = 1_000_000
JITO_ROUTING = "jito"
BUY = "buy"
SELL = "sell"
GRADUATED = "graduated"


@dataclass(slots=True)
class CurveMarket:
    """Latest bonding-curve state of one coin, from its most recent event."""

    slot: int
    virtual_sol: int
    virtual_token: int
    fee: FeeConfig
    last_trade_ms: int
    created_ms: int | None = None
    complete: bool = False

    @property
    def market_cap(self) -> int:
        """Fully diluted market cap in lamports."""
        return market_cap_lamports(self.virtual_sol, self.virtual_token)


@dataclass(slots=True)
class PendingOrder:
    """A paper order decided, or still waiting on its entry rules, not filled."""

    tracker: str
    mint: str
    side: str
    reason: str
    event_ms: int
    is_copytrade: bool = False
    fill_slot: int | None = None
    tokens: int = 0
    closes_position: bool = False


def tx_cost_lamports(config: CoreSniperConfig) -> int:
    """Network cost of one transaction under a tracker's execution settings."""
    execution = config.execution
    priority = (
        execution.compute_unit_limit
        * execution.priority_fee_microlamports
        // MICROLAMPORTS_PER_LAMPORT
    )
    tip = execution.jito_tip_lamports if execution.routing_policy == JITO_ROUTING else 0
    return BASE_SIGNATURE_FEE_LAMPORTS + priority + tip


class PaperDesk:
    """Paper execution for every enabled tracker, fed by Pump program logs."""

    def __init__(
        self,
        trackers: Sequence[Tracker],
        *,
        positions_dir: Path,
        journal: PaperJournal,
        landing_slots: int = DEFAULT_LANDING_SLOTS,
        clock_ms: Callable[[], int] | None = None,
    ) -> None:
        self._positions_dir = positions_dir
        self._journal = journal
        self._landing_slots = landing_slots
        self._clock_ms = clock_ms or (lambda: time.time_ns() // 1_000_000)
        self._stores: dict[str, SqliteStateStore] = {}
        self._positions: dict[tuple[str, str], PaperPositionState] = {}
        self._entry_state: dict[str, EntryRuleState] = {}
        self._realized: dict[tuple[str, str], int] = {}
        # (total entry cost incl. tx cost, original tokens) per open position
        self._entry_costs: dict[tuple[str, str], tuple[int, int]] = {}
        self._markets: dict[str, CurveMarket] = {}
        self._pending: list[PendingOrder] = []
        self._reported_skips: set[tuple[str, str, str]] = set()
        # Tokens each tracked wallet holds per coin, from trades seen this session.
        self._wallet_tokens: dict[tuple[str, str], int] = {}
        self._slot = 0
        self._trackers: dict[str, Tracker] = {}
        self.set_trackers(trackers)

    @property
    def open_positions(self) -> dict[tuple[str, str], PaperPositionState]:
        """Open positions keyed by ``(tracker, mint)``."""
        return dict(self._positions)

    def set_trackers(self, trackers: Sequence[Tracker]) -> None:
        """Replace the active tracker set; restores each new tracker's state."""

        self._trackers = {tracker.wallet: tracker for tracker in trackers}
        for wallet in self._trackers:
            if wallet in self._stores:
                continue
            store = SqliteStateStore(self._positions_dir / f"{wallet}.sqlite3")
            self._stores[wallet] = store
            for state in store.read_all():
                self._positions[(wallet, state.market_id)] = state
                self._entry_costs[(wallet, state.market_id)] = (
                    state.entry_quote_lamports + state.entry_cost_lamports,
                    state.original_position_base_units,
                )
            self._entry_state[wallet] = self._restored_entry_state(wallet)
        saved = self._journal.markets()
        for _, mint in self._positions:
            if mint not in self._markets and mint in saved:
                snapshot = saved[mint]
                self._markets[mint] = CurveMarket(
                    slot=snapshot.slot,
                    virtual_sol=snapshot.virtual_sol,
                    virtual_token=snapshot.virtual_token,
                    fee=dataclasses.replace(
                        PUMP_CURVE_FEE_CONFIG,
                        protocol_fee_bps=snapshot.protocol_fee_bps,
                        creator_fee_bps=snapshot.creator_fee_bps,
                    ),
                    last_trade_ms=snapshot.last_trade_ms,
                )

    def close(self) -> None:
        """Close every position store."""

        for store in self._stores.values():
            store.close()

    def handle_logs(self, slot: int, logs: Sequence[str]) -> list[str]:
        """Process one successful transaction's Pump logs; return event lines."""

        self._slot = max(self._slot, slot)
        now = self._clock_ms()
        lines: list[str] = []
        create = decode_pump_create_event_logs(logs, as_of_slot=Slot(slot))
        triggers: list[PendingOrder] = []
        if isinstance(create, PumpCreateEvent):
            triggers += self._on_create(create, slot, now, lines)
        for _, payload in pump_trade_payloads(logs):
            event = decode_pump_trade_event(payload, slot)
            if isinstance(event, AbstainResult):
                continue
            if event.user in self._trackers:
                triggers += self._wallet_triggers(event, slot, now, lines)
            if event.mint in self._markets:
                lines += self._on_trade(event, slot, now)
            if event.user in self._trackers:
                self._copy_sell(event)
        for order in triggers:
            self._pending.append(order)
            lines += self._advance_entry(order, now)
        return lines

    def tick(self) -> list[str]:
        """Fill orders whose slot passed without trades; run no-activity exits."""

        now = self._clock_ms()
        lines: list[str] = []
        for order in [item for item in self._pending if item.fill_slot is None]:
            lines += self._advance_entry(order, now)
        # One slot of slack: that slot's trades may still be in flight.
        for mint in {order.mint for order in self._pending}:
            lines += self._fill_due(mint, before_slot=self._slot - 1, now=now)
        for tracker, mint in list(self._positions):
            market = self._markets.get(mint)
            if market is not None:
                lines += self._mark(tracker, mint, market, self._slot, now)
        return lines

    def _on_create(
        self, create: PumpCreateEvent, slot: int, now: int, lines: list[str]
    ) -> list[PendingOrder]:
        trackers = [
            wallet
            for wallet, tracker in self._trackers.items()
            if wallet == create.creator_pubkey
            and tracker.config.tracking_mode is TrackingMode.NEW_TOKEN_CREATIONS
        ]
        if not trackers:
            return []
        skip = _unsupported_reason(
            create.quote_mint_pubkey,
            create.virtual_sol_reserves * create.virtual_token_reserves,
            mayhem=create.is_mayhem_mode,
        )
        label = f"CREATE {create.symbol} {create.mint_pubkey}"
        if skip is not None:
            lines += [
                _line(now, wallet, f"{label} skipped: {skip}") for wallet in trackers
            ]
            return []
        self._markets[create.mint_pubkey] = CurveMarket(
            slot=slot,
            virtual_sol=create.virtual_sol_reserves,
            virtual_token=create.virtual_token_reserves,
            fee=PUMP_CURVE_FEE_CONFIG,
            last_trade_ms=now,
            created_ms=now,
        )
        lines += [_line(now, wallet, label) for wallet in trackers]
        return [
            PendingOrder(wallet, create.mint_pubkey, BUY, "create", event_ms=now)
            for wallet in trackers
        ]

    def _wallet_triggers(
        self, event: PumpTradeEventProof, slot: int, now: int, lines: list[str]
    ) -> list[PendingOrder]:
        trigger = WALLET_TRIGGERS.get(self._trackers[event.user].config.tracking_mode)
        if trigger is None or trigger[0] != event.is_buy:
            return []
        orders = [
            PendingOrder(
                event.user, event.mint, BUY, trigger[1], event_ms=now, is_copytrade=True
            )
        ]
        skip = _unsupported_reason(
            event.quote_mint,
            event.virtual_sol_reserves_base_units
            * event.virtual_token_reserves_base_units,
            mayhem=event.mayhem_mode,
        )
        if skip is not None:
            for order in orders:
                lines += self._skip_line(order, skip, now)
            return []
        if event.mint not in self._markets:
            self._markets[event.mint] = CurveMarket(
                slot=slot,
                virtual_sol=event.virtual_sol_reserves_base_units,
                virtual_token=event.virtual_token_reserves_base_units,
                fee=_event_fee(event),
                last_trade_ms=now,
            )
        return orders

    def _on_trade(self, event: PumpTradeEventProof, slot: int, now: int) -> list[str]:
        mint = event.mint
        lines = self._fill_due(mint, before_slot=slot, now=now)
        market = self._markets[mint]
        market.slot = slot
        market.virtual_sol = event.virtual_sol_reserves_base_units
        market.virtual_token = event.virtual_token_reserves_base_units
        market.fee = _event_fee(event)
        market.last_trade_ms = now
        market.complete = event.real_token_reserves_base_units == 0
        if market.complete:
            return lines + self._close_all(mint, market, now)
        holders = [tracker for tracker, held in self._positions if held == mint]
        if holders:
            self._save_market(mint, market)
        for tracker in holders:
            lines += self._mark(tracker, mint, market, slot, now)
        return lines

    def _advance_entry(self, order: PendingOrder, now: int) -> list[str]:
        tracker = self._trackers.get(order.tracker)
        market = self._markets[order.mint]
        if tracker is None:
            self._pending.remove(order)
            return []
        config = tracker.config
        decision = evaluate_entry_rules(
            rules=config.rules,
            evidence=EntryRuleInput(
                as_of_slot=market.slot,
                token_mint=order.mint,
                now_ms=now,
                event_time_ms=order.event_ms,
                is_copytrade=order.is_copytrade,
                token_created_time_ms=market.created_ms,
                market_cap_quote_base_units=market.market_cap,
                current_market_cap_quote_base_units=market.market_cap,
            ),
            state=self._entry_state[order.tracker],
            base_quote_size_lamports=config.execution.quote_size_lamports,
        )
        if isinstance(decision, AbstainResult):
            skip = decision.message
        elif decision.action is EntryRuleAction.WAIT:
            return []
        elif decision.action is not EntryRuleAction.BUY:
            skip = ",".join(decision.reason_codes)
        elif (order.tracker, order.mint) in self._positions:
            skip = "already holding"
        elif self._open_count(order.tracker) >= config.risk.max_open_positions:
            skip = f"max open positions ({config.risk.max_open_positions})"
        else:
            self._entry_state[order.tracker] = decision.next_state
            order.fill_slot = self._slot + self._landing_slots
            return []
        self._pending.remove(order)
        return self._skip_line(order, skip, now)

    def _skip_line(self, order: PendingOrder, skip: str, now: int) -> list[str]:
        """Report a skipped entry once per tracker, coin and reason."""
        key = (order.tracker, order.mint, skip)
        if key in self._reported_skips:
            return []
        self._reported_skips.add(key)
        return [
            _line(now, order.tracker, f"{order.reason} {order.mint} skipped: {skip}")
        ]

    def _save_market(self, mint: str, market: CurveMarket) -> None:
        self._journal.save_market(
            MarketSnapshot(
                mint=mint,
                slot=market.slot,
                virtual_sol=market.virtual_sol,
                virtual_token=market.virtual_token,
                protocol_fee_bps=market.fee.protocol_fee_bps,
                creator_fee_bps=market.fee.creator_fee_bps,
                last_trade_ms=market.last_trade_ms,
            )
        )

    def _open_count(self, tracker: str) -> int:
        held = sum(1 for wallet, _ in self._positions if wallet == tracker)
        buying = sum(
            1
            for order in self._pending
            if order.tracker == tracker and order.side == BUY and order.fill_slot
        )
        return held + buying

    def _fill_due(self, mint: str, *, before_slot: int, now: int) -> list[str]:
        market = self._markets.get(mint)
        if market is None:
            return []
        due = [
            order
            for order in self._pending
            if order.mint == mint
            and order.fill_slot is not None
            and order.fill_slot < before_slot
        ]
        lines: list[str] = []
        for order in due:
            self._pending.remove(order)
            lines.append(self._fill(order, market, now))
        return lines

    def _fill(self, order: PendingOrder, market: CurveMarket, now: int) -> str:
        config = self._trackers[order.tracker].config
        tx_cost = tx_cost_lamports(config)
        if order.side == BUY:
            spend = config.execution.quote_size_lamports
            tokens, fee = pump_curve_buy_amounts(
                virtual_quote_reserves=market.virtual_sol,
                virtual_base_reserves=market.virtual_token,
                spendable_quote_in=spend,
                fee_config=market.fee,
            )
            if tokens <= 0:
                return _line(now, order.tracker, f"BUY {order.mint} got 0 tokens")
            sell = config.rules.sell
            state = PaperPositionState(
                as_of_slot=Slot(market.slot),
                market_id=order.mint,
                target_id=order.tracker,
                execution_mode=ExecutionMode.PAPER.value,
                original_position_base_units=TokenBaseUnits(tokens),
                current_position_base_units=TokenBaseUnits(tokens),
                entry_quote_lamports=spend,
                entry_cost_lamports=tx_cost,
                take_profit_pnl_ppm=(
                    sell.take_profit_levels[0].trigger_pnl_ppm
                    if sell.take_profit_levels
                    else None
                ),
                stop_loss_pnl_ppm=(
                    sell.stop_loss_levels[0].trigger_pnl_ppm
                    if sell.stop_loss_levels
                    else None
                ),
                max_slippage_bps=config.execution.max_slippage_bps,
            )
            self._positions[(order.tracker, order.mint)] = state
            self._stores[order.tracker].save(state)
            self._realized[(order.tracker, order.mint)] = 0
            self._entry_costs[(order.tracker, order.mint)] = (spend + tx_cost, tokens)
            self._save_market(order.mint, market)
            quote, filled_tokens, fee_paid, pnl, closed = (
                spend,
                tokens,
                fee,
                None,
                False,
            )
            text = f"BUY {order.mint} {spend / LAMPORTS_PER_SOL:.3f} SOL"
        else:
            proceeds, fee = pump_curve_sell_amounts(
                virtual_quote_reserves=market.virtual_sol,
                virtual_base_reserves=market.virtual_token,
                base_input_amount=order.tokens,
                fee_config=market.fee,
            )
            pnl = proceeds - self._slice_cost(order) - tx_cost
            key = (order.tracker, order.mint)
            self._realized[key] = self._realized.get(key, 0) + pnl
            if order.closes_position:
                self._record_close(order.tracker, self._realized.pop(key))
                del self._entry_costs[key]
            quote, filled_tokens, fee_paid = proceeds, order.tokens, fee
            closed = order.closes_position
            text = (
                f"SELL {order.mint} {order.reason} "
                f"{proceeds / LAMPORTS_PER_SOL:.3f} SOL "
                f"pnl {pnl / LAMPORTS_PER_SOL:+.4f}"
            )
        self._journal.record(
            PaperFill(
                at_ms=now,
                tracker=order.tracker,
                mint=order.mint,
                side=order.side,
                slot=market.slot,
                quote_lamports=quote,
                tokens=filled_tokens,
                curve_fee_lamports=fee_paid,
                tx_cost_lamports=tx_cost,
                market_cap_lamports=market.market_cap,
                reason=order.reason,
                pnl_lamports=pnl,
                position_closed=closed,
            )
        )
        return _line(
            now,
            order.tracker,
            f"{text} @ mc {market.market_cap / LAMPORTS_PER_SOL:.1f} SOL "
            f"(slot {market.slot})",
        )

    def _slice_cost(self, order: PendingOrder) -> int:
        entry = self._entry_costs[(order.tracker, order.mint)]
        return entry[0] * order.tokens // entry[1]

    def _mark(
        self, tracker: str, mint: str, market: CurveMarket, slot: int, now: int
    ) -> list[str]:
        state = self._positions[(tracker, mint)]
        if slot <= state.as_of_slot:
            return []
        config = self._trackers[tracker].config
        proceeds, _ = pump_curve_sell_amounts(
            virtual_quote_reserves=market.virtual_sol,
            virtual_base_reserves=market.virtual_token,
            base_input_amount=state.current_position_base_units,
            fee_config=market.fee,
        )
        # PnL of the remaining slice, without rounding its cost share to zero.
        cost_scaled = (
            state.entry_quote_lamports + state.entry_cost_lamports
        ) * state.current_position_base_units
        proceeds_scaled = proceeds * state.original_position_base_units
        outcome = advance_paper_position(
            rules=config.rules,
            evidence=PositionMarketEvidence(
                as_of_slot=Slot(slot),
                market_id=mint,
                current_pnl_ppm=(proceeds_scaled - cost_scaled)
                * PPM_SCALE
                // cost_scaled,
                idle_ms=max(0, now - market.last_trade_ms),
                executable_exit_capacity_base_units=state.current_position_base_units,
                current_market_cap_quote_base_units=market.market_cap,
            ),
            state=state,
            max_slippage_bps=config.execution.max_slippage_bps,
        )
        if isinstance(outcome, AbstainResult):
            key = (tracker, mint, outcome.message)
            if key in self._reported_skips:
                return []
            self._reported_skips.add(key)
            return [
                _line(now, tracker, f"{mint} exit rules abstain: {outcome.message}")
            ]
        if outcome.sell_intent is None:
            self._positions[(tracker, mint)] = outcome.next_state
            self._stores[tracker].save(outcome.next_state)
            return []
        tokens = outcome.sell_intent.base_amount_base_units or 0
        return self._queue_sell(
            tracker, mint, outcome.next_state, tokens, ",".join(outcome.reason_codes)
        )

    def _copy_sell(self, event: PumpTradeEventProof) -> None:
        """Mirror a tracked wallet's sell of a coin its tracker holds."""

        key = (event.user, event.mint)
        before = self._wallet_tokens.get(key, 0)
        change = (
            event.token_amount_base_units
            if event.is_buy
            else -min(before, event.token_amount_base_units)
        )
        self._wallet_tokens[key] = before + change
        sell = self._trackers[event.user].config.rules.sell
        if (
            event.is_buy
            or key not in self._positions
            or sell.copy_sells is (CopySellMode.OFF)
        ):
            return
        state = self._positions[key]
        held = state.current_position_base_units
        # Mirror the share sold when their balance is known; otherwise sell all.
        tokens = (
            held * event.token_amount_base_units // before
            if sell.copy_sells is CopySellMode.PERCENT
            and before > event.token_amount_base_units
            else held
        )
        if tokens > 0:
            self._queue_sell(
                event.user,
                event.mint,
                dataclasses.replace(
                    state, current_position_base_units=TokenBaseUnits(held - tokens)
                ),
                tokens,
                "copy_sell",
                delay_slots=-(-sell.copy_sell_delay_ms // SLOT_MS),
            )

    def _queue_sell(  # noqa: PLR0913
        self,
        tracker: str,
        mint: str,
        next_state: PaperPositionState,
        tokens: int,
        reason: str,
        *,
        delay_slots: int = 0,
    ) -> list[str]:
        closes = next_state.current_position_base_units == 0
        if closes:
            del self._positions[(tracker, mint)]
            self._stores[tracker].remove(mint)
        else:
            self._positions[(tracker, mint)] = next_state
            self._stores[tracker].save(next_state)
        self._pending.append(
            PendingOrder(
                tracker,
                mint,
                SELL,
                reason,
                event_ms=self._clock_ms(),
                fill_slot=self._slot + self._landing_slots + delay_slots,
                tokens=tokens,
                closes_position=closes,
            )
        )
        return []

    def _close_all(self, mint: str, market: CurveMarket, now: int) -> list[str]:
        """Exit every position of a coin whose curve just completed.

        After graduation its trades move to PumpSwap, which this stream does
        not carry, so positions exit at the final curve state.
        """
        lines: list[str] = []
        for tracker, held in list(self._positions):
            if held != mint:
                continue
            state = self._positions[(tracker, mint)]
            self._queue_sell(
                tracker,
                mint,
                dataclasses.replace(
                    state, current_position_base_units=TokenBaseUnits(0)
                ),
                state.current_position_base_units,
                GRADUATED,
            )
        for order in [item for item in self._pending if item.mint == mint]:
            self._pending.remove(order)
            lines.append(
                self._fill(order, market, now)
                if order.side == SELL
                else _line(now, order.tracker, f"{order.reason} {mint} {GRADUATED}")
            )
        return lines

    def _record_close(self, tracker: str, total_pnl: int) -> None:
        state = self._entry_state[tracker]
        losses = state.root_consecutive_losses + 1 if total_pnl < 0 else 0
        self._entry_state[tracker] = dataclasses.replace(
            state, root_consecutive_losses=losses
        )

    def _restored_entry_state(self, tracker: str) -> EntryRuleState:
        fills = self._journal.fills(tracker)
        bought = tuple(dict.fromkeys(fill.mint for fill in fills if fill.side == BUY))
        losses = 0
        position_pnl: dict[str, int] = {}
        for fill in fills:
            if fill.side != SELL or fill.pnl_lamports is None:
                continue
            position_pnl[fill.mint] = position_pnl.get(fill.mint, 0) + fill.pnl_lamports
            if fill.position_closed:
                losses = losses + 1 if position_pnl.pop(fill.mint) < 0 else 0
        return EntryRuleState(bought_token_mints=bought, root_consecutive_losses=losses)


def _unsupported_reason(quote_mint: str, invariant: int, *, mayhem: bool) -> str | None:
    """Why a coin cannot be paper-traded on the SOL curve, or ``None``."""
    if quote_mint != SOL_PUBKEY:
        return "non-SOL quote"
    return nonstandard_curve_reason(invariant, mayhem=mayhem)


def _event_fee(event: PumpTradeEventProof) -> FeeConfig:
    return dataclasses.replace(
        PUMP_CURVE_FEE_CONFIG,
        protocol_fee_bps=event.protocol_fee_basis_points,
        creator_fee_bps=event.creator_fee_basis_points,
    )


def _line(now_ms: int, tracker: str, text: str) -> str:
    clock = datetime.fromtimestamp(now_ms / 1000, UTC).strftime("%H:%M:%S")
    return f"{clock} {tracker[:6]} {text}"
