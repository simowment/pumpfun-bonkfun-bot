"""Pure entry, exit, and loss-counter rules from the playbook.

The module deliberately deals in integer units only.  Configuration parsing is
responsible for converting the playbook's seconds and minutes into
milliseconds; decision inputs then carry an explicit point-in-time boundary.
No rule in this module performs I/O or reads a clock.
"""

# The validators intentionally fail closed through several small branches. The
# module remains pure; these complexity limits do not hide I/O or fallback
# behavior.
# ruff: noqa: C901, PLR0911, PLR0913

from __future__ import annotations

from dataclasses import dataclass, field, replace
from enum import StrEnum
from itertools import pairwise

from rugbot.domain.decisions import AbstainReason, AbstainResult
from rugbot.graph.wallet_behavior import (
    CanonicalBuyEvidence,
    WalletAssetKind,
)

PROBABILITY_PPM_DENOMINATOR = 1_000_000
MAX_DIP_LEVELS = 3
MAX_SELL_LEVELS = 5
MAX_BIG_BUY_LEVELS = 3


class EntryRuleAction(StrEnum):
    """Action returned by the pure entry-rule evaluator."""

    BUY = "buy"
    WAIT = "wait"
    ABSTAIN = "abstain"


class ExitRuleAction(StrEnum):
    """Action returned by the pure exit-rule evaluator."""

    HOLD = "hold"
    SELL = "sell"
    ABSTAIN = "abstain"


@dataclass(frozen=True, slots=True)
class BuyTheDipLevel:
    """One one-shot buy level measured as a drawdown from token ATH."""

    drawdown_ppm: int
    quote_size_lamports: int


@dataclass(frozen=True, slots=True)
class SellLevel:
    """One cumulative position percentage triggered at a PnL threshold."""

    trigger_pnl_ppm: int
    sell_fraction_ppm: int


@dataclass(frozen=True, slots=True)
class BigBuySellLevel:
    """One non-overlapping quote-size range that triggers a partial exit."""

    min_quote_base_units: int
    max_quote_base_units: int
    sell_fraction_ppm: int


@dataclass(frozen=True, slots=True)
class TrailingStopLevel:
    """Trailing stop selected by the current market-cap tier."""

    min_market_cap_quote_base_units: int | None
    drawdown_ppm: int


@dataclass(frozen=True, slots=True)
class SellRules:
    """Multi-level exit rules for one position."""

    take_profit_levels: tuple[SellLevel, ...] = ()
    stop_loss_levels: tuple[SellLevel, ...] = ()
    trailing_levels: tuple[TrailingStopLevel, ...] = ()
    no_activity_timeout_ms: int | None = None
    auto_sell_big_buy_levels: tuple[BigBuySellLevel, ...] = ()


@dataclass(frozen=True, slots=True)
class PlaybookRules:
    """Closed, integer-only rules that can gate one rugger's decisions."""

    snipe_delay_ms: int = 0
    min_market_cap_quote_base_units: int | None = None
    max_market_cap_quote_base_units: int | None = None
    max_token_age_ms: int | None = None
    copytrade_cooldown_ms: int = 0
    buy_only_once: bool = False
    buy_the_dip_levels: tuple[BuyTheDipLevel, ...] = ()
    sell: SellRules = field(default_factory=SellRules)
    max_consecutive_losses: int | None = None


@dataclass(frozen=True, slots=True)
class EntryRuleState:
    """Immutable state carried between entry evaluations.

    ``root_consecutive_losses`` is intentionally stored once for the whole
    root rugger chain.  Child wallets must pass the same state rather than
    maintaining independent counters.
    """

    bought_token_mints: tuple[str, ...] = ()
    last_copytrade_entry_ms: int | None = None
    root_consecutive_losses: int = 0
    dip_filled_levels_by_token: tuple[tuple[str, tuple[int, ...]], ...] = ()


@dataclass(frozen=True, slots=True)
class EntryRuleInput:
    """Point-in-time evidence used by the entry-rule evaluator."""

    as_of_slot: int
    token_mint: str
    now_ms: int
    event_time_ms: int
    is_copytrade: bool
    token_created_time_ms: int | None = None
    market_cap_quote_base_units: int | None = None
    is_buy_the_dip: bool = False
    ath_market_cap_quote_base_units: int | None = None
    current_market_cap_quote_base_units: int | None = None
    dip_filled_level_indices: tuple[int, ...] = ()


@dataclass(frozen=True, slots=True)
class EntryRuleDecision:
    """Entry outcome, including the reason a rule did not pass."""

    action: EntryRuleAction
    as_of_slot: int
    token_mint: str
    quote_size_lamports: int | None
    dip_level_index: int | None
    reason_codes: tuple[str, ...]
    next_state: EntryRuleState


@dataclass(frozen=True, slots=True)
class ExitRuleState:
    """Immutable position-exit state used to make levels one-shot."""

    filled_take_profit_level_indices: tuple[int, ...] = ()
    filled_stop_loss_level_indices: tuple[int, ...] = ()
    filled_big_buy_level_indices: tuple[int, ...] = ()
    exited_fraction_ppm: int = 0


@dataclass(frozen=True, slots=True)
class ExitRuleInput:
    """Point-in-time position evidence used by the exit evaluator."""

    as_of_slot: int
    current_pnl_ppm: int
    peak_pnl_ppm: int
    current_market_cap_quote_base_units: int | None = None
    idle_ms: int = 0
    token_mint: str | None = None
    big_buy_evidence: CanonicalBuyEvidence | None = None


@dataclass(frozen=True, slots=True)
class ExitRuleDecision:
    """Exit outcome and the exact portion of the position to sell."""

    action: ExitRuleAction
    as_of_slot: int
    sell_amount_base_units: int
    sell_fraction_ppm: int
    triggered_level_index: int | None
    reason_codes: tuple[str, ...]
    next_state: ExitRuleState


@dataclass(frozen=True, slots=True)
class RootLossCounterState:
    """Consecutive-loss state identified by the root rugger entity."""

    root_id: str
    consecutive_losses: int = 0


def evaluate_entry_rules(
    *,
    rules: PlaybookRules,
    evidence: EntryRuleInput,
    state: EntryRuleState,
    base_quote_size_lamports: int,
) -> EntryRuleDecision | AbstainResult:
    """Evaluate the playbook buy filters without reading external state.

    Delay and an unhit dip level return ``WAIT``.  A configured filter that
    rejects an otherwise well-formed opportunity returns ``ABSTAIN`` with a
    stable reason code.  Malformed evidence returns ``AbstainResult``.
    """

    validation_error = _validate_entry_inputs(
        rules=rules,
        evidence=evidence,
        state=state,
        base_quote_size_lamports=base_quote_size_lamports,
    )
    if validation_error is not None:
        return validation_error

    if evidence.now_ms < evidence.event_time_ms + rules.snipe_delay_ms:
        return _entry_decision(
            EntryRuleAction.WAIT,
            evidence,
            state,
            None,
            None,
            ("snipe_delay_active",),
        )

    if evidence.is_copytrade:
        age_error = _copytrade_age_error(rules, evidence, state)
        if age_error is not None:
            return age_error
        cooldown_error = _copytrade_cooldown_error(rules, evidence, state)
        if cooldown_error is not None:
            return cooldown_error
        if rules.buy_only_once and evidence.token_mint in state.bought_token_mints:
            return _entry_decision(
                EntryRuleAction.ABSTAIN,
                evidence,
                state,
                None,
                None,
                ("buy_only_once_already_bought",),
            )

    market_cap_error = _market_cap_error(rules, evidence, state)
    if market_cap_error is not None:
        return market_cap_error

    if state.root_consecutive_losses >= (rules.max_consecutive_losses or 0) > 0:
        return _entry_decision(
            EntryRuleAction.ABSTAIN,
            evidence,
            state,
            None,
            None,
            ("max_consecutive_losses_reached",),
        )

    if evidence.is_buy_the_dip:
        dip_result = _evaluate_dip_levels(rules, evidence, state)
        if isinstance(dip_result, AbstainResult):
            return dip_result
        return dip_result

    next_state = _state_after_buy(evidence, state)
    return _entry_decision(
        EntryRuleAction.BUY,
        evidence,
        next_state,
        base_quote_size_lamports,
        None,
        _entry_pass_reasons(rules, evidence),
    )


def evaluate_exit_rules(
    *,
    rules: PlaybookRules,
    evidence: ExitRuleInput,
    state: ExitRuleState,
    current_position_base_units: int,
    original_position_base_units: int,
) -> ExitRuleDecision | AbstainResult:
    """Evaluate TP, SL, trailing, and inactivity rules for one position."""

    validation_error = _validate_exit_inputs(
        rules=rules,
        evidence=evidence,
        state=state,
        current_position_base_units=current_position_base_units,
        original_position_base_units=original_position_base_units,
    )
    if validation_error is not None:
        return validation_error

    sell = _full_exit_for_no_activity(
        rules, evidence, state, current_position_base_units
    )
    if sell is not None:
        return sell

    big_buy = _big_buy_trigger(rules, evidence, state)
    if big_buy is not None:
        index, level = big_buy
        return _partial_exit(
            evidence=evidence,
            state=state,
            level_index=index,
            level=SellLevel(
                trigger_pnl_ppm=0,
                sell_fraction_ppm=level.sell_fraction_ppm,
            ),
            level_kind="big_buy",
            current_position_base_units=current_position_base_units,
            original_position_base_units=original_position_base_units,
        )

    stop_loss = _triggered_level(
        rules.sell.stop_loss_levels,
        state.filled_stop_loss_level_indices,
        lambda level: evidence.current_pnl_ppm <= level.trigger_pnl_ppm,
    )
    if stop_loss is not None:
        index, level = stop_loss
        return _partial_exit(
            evidence=evidence,
            state=state,
            level_index=index,
            level=level,
            level_kind="stop_loss",
            current_position_base_units=current_position_base_units,
            original_position_base_units=original_position_base_units,
        )

    trailing = _trailing_trigger(rules, evidence)
    if isinstance(trailing, AbstainResult):
        return trailing
    if trailing is not None:
        index, level = trailing
        trailing_level = SellLevel(
            trigger_pnl_ppm=evidence.peak_pnl_ppm - level.drawdown_ppm,
            sell_fraction_ppm=PROBABILITY_PPM_DENOMINATOR,
        )
        return _partial_exit(
            evidence=evidence,
            state=state,
            level_index=index,
            level=trailing_level,
            level_kind="trailing_stop",
            current_position_base_units=current_position_base_units,
            original_position_base_units=original_position_base_units,
        )

    take_profit = _triggered_level(
        rules.sell.take_profit_levels,
        state.filled_take_profit_level_indices,
        lambda level: evidence.current_pnl_ppm >= level.trigger_pnl_ppm,
    )
    if take_profit is not None:
        index, level = take_profit
        return _partial_exit(
            evidence=evidence,
            state=state,
            level_index=index,
            level=level,
            level_kind="take_profit",
            current_position_base_units=current_position_base_units,
            original_position_base_units=original_position_base_units,
        )

    return ExitRuleDecision(
        action=ExitRuleAction.HOLD,
        as_of_slot=evidence.as_of_slot,
        sell_amount_base_units=0,
        sell_fraction_ppm=state.exited_fraction_ppm,
        triggered_level_index=None,
        reason_codes=("no_exit_trigger",),
        next_state=state,
    )


def advance_root_loss_counter(
    *, state: RootLossCounterState, net_pnl_lamports: int
) -> RootLossCounterState | AbstainResult:
    """Update one root counter; a win anywhere in the chain resets it."""

    if not isinstance(state, RootLossCounterState) or not state.root_id:
        return _abstain(
            AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
            "root loss-counter state is malformed",
            -1,
        )
    if type(net_pnl_lamports) is not int or state.consecutive_losses < 0:
        return _abstain(
            AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
            "root loss-counter values are malformed",
            -1,
        )
    next_count = state.consecutive_losses + 1 if net_pnl_lamports < 0 else 0
    return replace(state, consecutive_losses=next_count)


def _validate_entry_inputs(
    *,
    rules: PlaybookRules,
    evidence: EntryRuleInput,
    state: EntryRuleState,
    base_quote_size_lamports: int,
) -> AbstainResult | None:
    if not isinstance(rules, PlaybookRules) or not isinstance(evidence, EntryRuleInput):
        return _abstain(
            AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
            "entry rules input is malformed",
            -1,
        )
    if (
        not isinstance(state, EntryRuleState)
        or type(base_quote_size_lamports) is not int
    ):
        return _abstain(
            AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
            "entry state is malformed",
            evidence.as_of_slot,
        )
    if not _non_negative_int(evidence.as_of_slot) or not evidence.token_mint:
        return _abstain(
            AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
            "entry identity is malformed",
            evidence.as_of_slot,
        )
    if not _non_negative_int(evidence.now_ms) or not _non_negative_int(
        evidence.event_time_ms
    ):
        return _abstain(
            AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
            "entry timestamps are malformed",
            evidence.as_of_slot,
        )
    if evidence.now_ms < evidence.event_time_ms:
        return _abstain(
            AbstainReason.STALE_STATE,
            "entry clock precedes source event",
            evidence.as_of_slot,
        )
    if (
        type(evidence.is_copytrade) is not bool
        or type(evidence.is_buy_the_dip) is not bool
    ):
        return _abstain(
            AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
            "entry mode flags are malformed",
            evidence.as_of_slot,
        )
    if base_quote_size_lamports <= 0 or state.root_consecutive_losses < 0:
        return _abstain(
            AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
            "entry amounts are malformed",
            evidence.as_of_slot,
        )
    return _validate_rules(rules, evidence.as_of_slot)


def _validate_exit_inputs(
    *,
    rules: PlaybookRules,
    evidence: ExitRuleInput,
    state: ExitRuleState,
    current_position_base_units: int,
    original_position_base_units: int,
) -> AbstainResult | None:
    if not isinstance(rules, PlaybookRules) or not isinstance(evidence, ExitRuleInput):
        return _abstain(
            AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
            "exit rules input is malformed",
            -1,
        )
    if not isinstance(state, ExitRuleState):
        return _abstain(
            AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
            "exit state is malformed",
            evidence.as_of_slot,
        )
    if not _non_negative_int(evidence.as_of_slot) or not _non_negative_int(
        evidence.idle_ms
    ):
        return _abstain(
            AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
            "exit timing is malformed",
            evidence.as_of_slot,
        )
    if evidence.token_mint is not None and not isinstance(evidence.token_mint, str):
        return _abstain(
            AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
            "exit token identity is malformed",
            evidence.as_of_slot,
        )
    if evidence.big_buy_evidence is not None:
        provenance_error = _validate_big_buy_evidence(
            evidence.big_buy_evidence,
            evidence.as_of_slot,
            evidence.token_mint,
        )
        if provenance_error is not None:
            return provenance_error
    if (
        type(evidence.current_pnl_ppm) is not int
        or type(evidence.peak_pnl_ppm) is not int
    ):
        return _abstain(
            AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
            "exit PnL is malformed",
            evidence.as_of_slot,
        )
    if evidence.peak_pnl_ppm < evidence.current_pnl_ppm:
        return _abstain(
            AbstainReason.STALE_STATE,
            "exit peak is below current PnL",
            evidence.as_of_slot,
        )
    if (
        type(current_position_base_units) is not int
        or type(original_position_base_units) is not int
    ):
        return _abstain(
            AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
            "exit position is malformed",
            evidence.as_of_slot,
        )
    if not 0 < current_position_base_units <= original_position_base_units:
        return _abstain(
            AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
            "exit position size is invalid",
            evidence.as_of_slot,
        )
    if not 0 <= state.exited_fraction_ppm <= PROBABILITY_PPM_DENOMINATOR:
        return _abstain(
            AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
            "exit fraction is invalid",
            evidence.as_of_slot,
        )
    return _validate_rules(rules, evidence.as_of_slot)


def _validate_rules(rules: PlaybookRules, as_of_slot: int) -> AbstainResult | None:
    integer_fields = (
        rules.snipe_delay_ms,
        rules.copytrade_cooldown_ms,
        rules.max_token_age_ms,
        rules.min_market_cap_quote_base_units,
        rules.max_market_cap_quote_base_units,
        rules.max_consecutive_losses,
    )
    if any(value is not None and type(value) is not int for value in integer_fields):
        return _abstain(
            AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
            "rule values are not integers",
            as_of_slot,
        )
    if rules.snipe_delay_ms < 0 or rules.copytrade_cooldown_ms < 0:
        return _abstain(
            AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
            "rule timing is negative",
            as_of_slot,
        )
    if any(value is not None and value < 0 for value in integer_fields):
        return _abstain(
            AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
            "rule limits are negative",
            as_of_slot,
        )
    if (
        rules.min_market_cap_quote_base_units is not None
        and rules.max_market_cap_quote_base_units is not None
        and rules.min_market_cap_quote_base_units
        > rules.max_market_cap_quote_base_units
    ):
        return _abstain(
            AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
            "market-cap bounds are inverted",
            as_of_slot,
        )
    if rules.max_consecutive_losses is not None and rules.max_consecutive_losses <= 0:
        return _abstain(
            AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
            "max losses must be positive",
            as_of_slot,
        )
    if type(rules.buy_only_once) is not bool:
        return _abstain(
            AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
            "buy-only-once is malformed",
            as_of_slot,
        )
    if len(rules.buy_the_dip_levels) > MAX_DIP_LEVELS:
        return _abstain(
            AbstainReason.UNSUPPORTED_PROTOCOL_STATE, "too many dip levels", as_of_slot
        )
    if not _valid_dip_levels(rules.buy_the_dip_levels):
        return _abstain(
            AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
            "dip levels are malformed",
            as_of_slot,
        )
    return _validate_sell_rules(rules.sell, as_of_slot)


def _validate_sell_rules(sell: SellRules, as_of_slot: int) -> AbstainResult | None:
    if not isinstance(sell, SellRules):
        return _abstain(
            AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
            "sell rules are malformed",
            as_of_slot,
        )
    if (
        len(sell.take_profit_levels) > MAX_SELL_LEVELS
        or len(sell.stop_loss_levels) > MAX_SELL_LEVELS
        or len(sell.trailing_levels) > MAX_SELL_LEVELS
    ):
        return _abstain(
            AbstainReason.UNSUPPORTED_PROTOCOL_STATE, "too many sell levels", as_of_slot
        )
    if not _valid_sell_levels(
        sell.take_profit_levels, positive=True
    ) or not _valid_sell_levels(sell.stop_loss_levels, positive=False):
        return _abstain(
            AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
            "sell levels are malformed",
            as_of_slot,
        )
    if not _valid_trailing_levels(sell.trailing_levels):
        return _abstain(
            AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
            "trailing levels are malformed",
            as_of_slot,
        )
    if len(sell.auto_sell_big_buy_levels) > MAX_BIG_BUY_LEVELS:
        return _abstain(
            AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
            "too many big-buy levels",
            as_of_slot,
        )
    if not _valid_big_buy_levels(sell.auto_sell_big_buy_levels):
        return _abstain(
            AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
            "big-buy levels are malformed",
            as_of_slot,
        )
    if sell.no_activity_timeout_ms is not None and (
        type(sell.no_activity_timeout_ms) is not int or sell.no_activity_timeout_ms <= 0
    ):
        return _abstain(
            AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
            "no-activity timeout is invalid",
            as_of_slot,
        )
    return None


def _valid_dip_levels(levels: tuple[BuyTheDipLevel, ...]) -> bool:
    return (
        type(levels) is tuple
        and all(
            isinstance(level, BuyTheDipLevel)
            and type(level.drawdown_ppm) is int
            and type(level.quote_size_lamports) is int
            and 0 < level.drawdown_ppm <= PROBABILITY_PPM_DENOMINATOR
            and level.quote_size_lamports > 0
            for level in levels
        )
        and all(
            left.drawdown_ppm < right.drawdown_ppm for left, right in pairwise(levels)
        )
    )


def _valid_sell_levels(levels: tuple[SellLevel, ...], *, positive: bool) -> bool:
    return (
        type(levels) is tuple
        and all(
            isinstance(level, SellLevel)
            and type(level.trigger_pnl_ppm) is int
            and type(level.sell_fraction_ppm) is int
            and (level.trigger_pnl_ppm > 0 if positive else level.trigger_pnl_ppm < 0)
            and 0 < level.sell_fraction_ppm <= PROBABILITY_PPM_DENOMINATOR
            for level in levels
        )
        and all(
            left.trigger_pnl_ppm < right.trigger_pnl_ppm
            for left, right in pairwise(levels)
        )
    )


def _valid_trailing_levels(levels: tuple[TrailingStopLevel, ...]) -> bool:
    return (
        type(levels) is tuple
        and all(
            isinstance(level, TrailingStopLevel)
            and (
                level.min_market_cap_quote_base_units is None
                or (
                    type(level.min_market_cap_quote_base_units) is int
                    and level.min_market_cap_quote_base_units >= 0
                )
            )
            and type(level.drawdown_ppm) is int
            and 0 < level.drawdown_ppm <= PROBABILITY_PPM_DENOMINATOR
            for level in levels
        )
        and all(
            (
                left.min_market_cap_quote_base_units is None
                or right.min_market_cap_quote_base_units is None
                or left.min_market_cap_quote_base_units
                < right.min_market_cap_quote_base_units
            )
            for left, right in pairwise(levels)
        )
        and sum(level.min_market_cap_quote_base_units is None for level in levels) <= 1
        and all(
            index == 0
            for index, level in enumerate(levels)
            if level.min_market_cap_quote_base_units is None
        )
    )


def _valid_big_buy_levels(levels: tuple[BigBuySellLevel, ...]) -> bool:
    return (
        type(levels) is tuple
        and all(
            isinstance(level, BigBuySellLevel)
            and type(level.min_quote_base_units) is int
            and type(level.max_quote_base_units) is int
            and type(level.sell_fraction_ppm) is int
            and 0 <= level.min_quote_base_units < level.max_quote_base_units
            and 0 < level.sell_fraction_ppm <= PROBABILITY_PPM_DENOMINATOR
            for level in levels
        )
        and all(
            left.max_quote_base_units < right.min_quote_base_units
            for left, right in pairwise(levels)
        )
    )


def _validate_big_buy_evidence(
    evidence: CanonicalBuyEvidence,
    as_of_slot: int,
    token_mint: str | None,
) -> AbstainResult | None:
    if not isinstance(evidence, CanonicalBuyEvidence):
        return _abstain(
            AbstainReason.MISSING_FEATURE,
            "big-buy provenance type is incomplete",
            as_of_slot,
        )
    if (
        not _non_negative_int(evidence.as_of_slot)
        or not _non_negative_int(evidence.slot)
        or evidence.as_of_slot > as_of_slot
        or evidence.slot > evidence.as_of_slot
        or not _non_negative_int(evidence.transaction_index)
        or not _non_negative_int(evidence.event_index)
        or not isinstance(evidence.signature, bytes)
        or not evidence.signature
        or not _valid_evidence_ids(evidence.evidence_ids)
        or not isinstance(evidence.wallet, str)
        or not evidence.wallet
        or not isinstance(evidence.token_mint, str)
        or not evidence.token_mint
        or token_mint is None
        or token_mint != evidence.token_mint
        or evidence.quote_asset_kind
        not in (WalletAssetKind.NATIVE, WalletAssetKind.QUOTE)
        or not isinstance(evidence.quote_asset_id, str)
        or not evidence.quote_asset_id
        or type(evidence.base_amount_base_units) is not int
        or evidence.base_amount_base_units <= 0
        or type(evidence.quote_amount_base_units) is not int
        or evidence.quote_amount_base_units <= 0
    ):
        return _abstain(
            AbstainReason.MISSING_FEATURE,
            "big-buy provenance is incomplete or mismatched",
            as_of_slot,
        )
    return None


def _valid_evidence_ids(evidence_ids: object) -> bool:
    return (
        isinstance(evidence_ids, tuple)
        and bool(evidence_ids)
        and all(isinstance(value, str) and bool(value) for value in evidence_ids)
        and len(set(evidence_ids)) == len(evidence_ids)
    )


def _copytrade_age_error(
    rules: PlaybookRules, evidence: EntryRuleInput, state: EntryRuleState
) -> EntryRuleDecision | AbstainResult | None:
    if rules.max_token_age_ms is None:
        return None
    if evidence.token_created_time_ms is None:
        return _abstain(
            AbstainReason.MISSING_FEATURE,
            "token age is required for copytrade",
            evidence.as_of_slot,
        )
    if (
        type(evidence.token_created_time_ms) is not int
        or evidence.token_created_time_ms < 0
    ):
        return _abstain(
            AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
            "token creation time is malformed",
            evidence.as_of_slot,
        )
    age_ms = evidence.now_ms - evidence.token_created_time_ms
    if age_ms < 0:
        return _abstain(
            AbstainReason.STALE_STATE,
            "token creation is after entry observation",
            evidence.as_of_slot,
        )
    if age_ms > rules.max_token_age_ms:
        return _entry_decision(
            EntryRuleAction.ABSTAIN,
            evidence,
            state,
            None,
            None,
            ("max_token_age_exceeded",),
        )
    return None


def _copytrade_cooldown_error(
    rules: PlaybookRules, evidence: EntryRuleInput, state: EntryRuleState
) -> EntryRuleDecision | AbstainResult | None:
    if rules.copytrade_cooldown_ms <= 0 or state.last_copytrade_entry_ms is None:
        return None
    if (
        type(state.last_copytrade_entry_ms) is not int
        or state.last_copytrade_entry_ms < 0
    ):
        return _abstain(
            AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
            "copytrade cooldown state is malformed",
            evidence.as_of_slot,
        )
    if evidence.now_ms < state.last_copytrade_entry_ms:
        return _abstain(
            AbstainReason.STALE_STATE,
            "copytrade entry precedes cooldown state",
            evidence.as_of_slot,
        )
    if evidence.now_ms - state.last_copytrade_entry_ms < rules.copytrade_cooldown_ms:
        return _entry_decision(
            EntryRuleAction.ABSTAIN,
            evidence,
            state,
            None,
            None,
            ("copytrade_cooldown_active",),
        )
    return None


def _market_cap_error(
    rules: PlaybookRules, evidence: EntryRuleInput, state: EntryRuleState
) -> EntryRuleDecision | AbstainResult | None:
    if (
        rules.min_market_cap_quote_base_units is None
        and rules.max_market_cap_quote_base_units is None
    ):
        return None
    market_cap = evidence.market_cap_quote_base_units
    if (
        evidence.is_buy_the_dip
        and evidence.current_market_cap_quote_base_units is not None
    ):
        market_cap = evidence.current_market_cap_quote_base_units
    if market_cap is None:
        return _abstain(
            AbstainReason.MISSING_FEATURE,
            "configured market-cap filter is unmeasurable",
            evidence.as_of_slot,
        )
    if type(market_cap) is not int or market_cap < 0:
        return _abstain(
            AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
            "market cap is malformed",
            evidence.as_of_slot,
        )
    if (
        rules.min_market_cap_quote_base_units is not None
        and market_cap < rules.min_market_cap_quote_base_units
    ):
        return _entry_decision(
            EntryRuleAction.ABSTAIN,
            evidence,
            state,
            None,
            None,
            ("market_cap_below_minimum",),
        )
    if (
        rules.max_market_cap_quote_base_units is not None
        and market_cap > rules.max_market_cap_quote_base_units
    ):
        return _entry_decision(
            EntryRuleAction.ABSTAIN,
            evidence,
            state,
            None,
            None,
            ("market_cap_above_maximum",),
        )
    return None


def _evaluate_dip_levels(
    rules: PlaybookRules,
    evidence: EntryRuleInput,
    state: EntryRuleState,
) -> EntryRuleDecision | AbstainResult:
    if not rules.buy_the_dip_levels:
        return _entry_decision(
            EntryRuleAction.ABSTAIN,
            evidence,
            state,
            None,
            None,
            ("buy_the_dip_not_configured",),
        )
    ath = evidence.ath_market_cap_quote_base_units
    current = evidence.current_market_cap_quote_base_units
    if ath is None or current is None:
        return _abstain(
            AbstainReason.MISSING_FEATURE,
            "buy-the-dip requires ATH and current market cap",
            evidence.as_of_slot,
        )
    if type(ath) is not int or type(current) is not int or ath <= 0 or current < 0:
        return _abstain(
            AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
            "buy-the-dip market caps are malformed",
            evidence.as_of_slot,
        )
    drawdown = max(0, (ath - current) * PROBABILITY_PPM_DENOMINATOR // ath)
    filled = set(evidence.dip_filled_level_indices)
    filled.update(_state_dip_level_indices(state, evidence.token_mint))
    for index, level in enumerate(rules.buy_the_dip_levels):
        if index not in filled and drawdown >= level.drawdown_ppm:
            next_state = _state_after_buy(
                evidence,
                state,
                dip_level_index=index,
                dip_filled_level_indices=tuple(filled),
            )
            return _entry_decision(
                EntryRuleAction.BUY,
                evidence,
                next_state,
                level.quote_size_lamports,
                index,
                (f"buy_the_dip_level_{index}_reached",),
            )
    return _entry_decision(
        EntryRuleAction.WAIT,
        evidence,
        state,
        None,
        None,
        ("buy_the_dip_level_not_reached",),
    )


def _trailing_trigger(
    rules: PlaybookRules, evidence: ExitRuleInput
) -> tuple[int, TrailingStopLevel] | AbstainResult | None:
    if not rules.sell.trailing_levels:
        return None
    market_cap = evidence.current_market_cap_quote_base_units
    has_market_cap_tier = any(
        level.min_market_cap_quote_base_units is not None
        for level in rules.sell.trailing_levels
    )
    if market_cap is None and has_market_cap_tier:
        default = next(
            (
                level
                for level in rules.sell.trailing_levels
                if level.min_market_cap_quote_base_units is None
            ),
            None,
        )
        if default is None:
            return _abstain(
                AbstainReason.MISSING_FEATURE,
                "trailing market-cap tier is unmeasurable",
                evidence.as_of_slot,
            )
        selected = (rules.sell.trailing_levels.index(default), default)
    elif market_cap is None:
        default = next(
            (
                (index, level)
                for index, level in enumerate(rules.sell.trailing_levels)
                if level.min_market_cap_quote_base_units is None
            ),
            None,
        )
        if default is None:
            return _abstain(
                AbstainReason.MISSING_FEATURE,
                "trailing market cap is required",
                evidence.as_of_slot,
            )
        selected = default
    else:
        if type(market_cap) is not int or market_cap < 0:
            return _abstain(
                AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
                "trailing market cap is malformed",
                evidence.as_of_slot,
            )
        candidates = tuple(
            (index, level)
            for index, level in enumerate(rules.sell.trailing_levels)
            if level.min_market_cap_quote_base_units is None
            or level.min_market_cap_quote_base_units <= market_cap
        )
        if not candidates:
            return None
        selected = candidates[-1]
    index, level = selected
    return (
        (index, level)
        if evidence.peak_pnl_ppm - evidence.current_pnl_ppm >= level.drawdown_ppm
        else None
    )


def _big_buy_trigger(
    rules: PlaybookRules,
    evidence: ExitRuleInput,
    state: ExitRuleState,
) -> tuple[int, BigBuySellLevel] | None:
    if not rules.sell.auto_sell_big_buy_levels or evidence.big_buy_evidence is None:
        return None
    amount = evidence.big_buy_evidence.quote_amount_base_units
    for index, level in enumerate(rules.sell.auto_sell_big_buy_levels):
        if (
            index not in state.filled_big_buy_level_indices
            and level.min_quote_base_units <= amount <= level.max_quote_base_units
        ):
            return index, level
    return None


def _full_exit_for_no_activity(
    rules: PlaybookRules,
    evidence: ExitRuleInput,
    state: ExitRuleState,
    current_position_base_units: int,
) -> ExitRuleDecision | None:
    timeout = rules.sell.no_activity_timeout_ms
    if timeout is None or evidence.idle_ms < timeout:
        return None
    next_state = replace(state, exited_fraction_ppm=PROBABILITY_PPM_DENOMINATOR)
    return ExitRuleDecision(
        action=ExitRuleAction.SELL,
        as_of_slot=evidence.as_of_slot,
        sell_amount_base_units=current_position_base_units,
        sell_fraction_ppm=PROBABILITY_PPM_DENOMINATOR,
        triggered_level_index=None,
        reason_codes=("no_activity_timeout",),
        next_state=next_state,
    )


def _triggered_level(
    levels: tuple[SellLevel, ...],
    filled_indices: tuple[int, ...],
    predicate: object,
) -> tuple[int, SellLevel] | None:
    if not callable(predicate):
        return None
    for index, level in enumerate(levels):
        if index not in filled_indices and predicate(level):
            return index, level
    return None


def _partial_exit(
    *,
    evidence: ExitRuleInput,
    state: ExitRuleState,
    level_index: int,
    level: SellLevel,
    level_kind: str,
    current_position_base_units: int,
    original_position_base_units: int,
) -> ExitRuleDecision:
    target = max(state.exited_fraction_ppm, level.sell_fraction_ppm)
    delta_fraction = target - state.exited_fraction_ppm
    amount = min(
        current_position_base_units,
        max(
            1,
            original_position_base_units
            * delta_fraction
            // PROBABILITY_PPM_DENOMINATOR,
        ),
    )
    if level_kind == "take_profit":
        next_state = replace(
            state,
            filled_take_profit_level_indices=(
                *state.filled_take_profit_level_indices,
                level_index,
            ),
            exited_fraction_ppm=target,
        )
    elif level_kind == "stop_loss":
        next_state = replace(
            state,
            filled_stop_loss_level_indices=(
                *state.filled_stop_loss_level_indices,
                level_index,
            ),
            exited_fraction_ppm=target,
        )
    elif level_kind == "big_buy":
        next_state = replace(
            state,
            filled_big_buy_level_indices=(
                *state.filled_big_buy_level_indices,
                level_index,
            ),
            exited_fraction_ppm=target,
        )
    else:
        next_state = replace(state, exited_fraction_ppm=target)
    return ExitRuleDecision(
        action=ExitRuleAction.SELL,
        as_of_slot=evidence.as_of_slot,
        sell_amount_base_units=amount,
        sell_fraction_ppm=target,
        triggered_level_index=level_index,
        reason_codes=(f"{level_kind}_level_{level_index}_triggered",),
        next_state=next_state,
    )


def _state_after_buy(
    evidence: EntryRuleInput,
    state: EntryRuleState,
    dip_level_index: int | None = None,
    dip_filled_level_indices: tuple[int, ...] | None = None,
) -> EntryRuleState:
    bought = state.bought_token_mints
    if evidence.token_mint not in bought:
        bought = (*bought, evidence.token_mint)
    last_copytrade = (
        evidence.now_ms if evidence.is_copytrade else state.last_copytrade_entry_ms
    )
    dip_levels = state.dip_filled_levels_by_token
    if dip_level_index is not None:
        current_levels = set(_state_dip_level_indices(state, evidence.token_mint))
        if dip_filled_level_indices is not None:
            current_levels.update(dip_filled_level_indices)
        current_levels.add(dip_level_index)
        dip_levels = tuple(
            entry for entry in dip_levels if entry[0] != evidence.token_mint
        )
        dip_levels = (*dip_levels, (evidence.token_mint, tuple(sorted(current_levels))))
    return replace(
        state,
        bought_token_mints=bought,
        last_copytrade_entry_ms=last_copytrade,
        dip_filled_levels_by_token=dip_levels,
    )


def _state_dip_level_indices(state: EntryRuleState, token_mint: str) -> tuple[int, ...]:
    for mint, indices in state.dip_filled_levels_by_token:
        if mint == token_mint:
            return indices
    return ()


def _entry_pass_reasons(
    rules: PlaybookRules, evidence: EntryRuleInput
) -> tuple[str, ...]:
    reasons = ["entry_rules_passed", "snipe_delay_elapsed"]
    if (
        rules.min_market_cap_quote_base_units is not None
        or rules.max_market_cap_quote_base_units is not None
    ):
        reasons.append("market_cap_in_range")
    if evidence.is_copytrade:
        if rules.max_token_age_ms is not None:
            reasons.append("token_age_within_limit")
        if rules.copytrade_cooldown_ms > 0:
            reasons.append("copytrade_cooldown_elapsed")
    return tuple(reasons)


def _entry_decision(
    action: EntryRuleAction,
    evidence: EntryRuleInput,
    state: EntryRuleState,
    quote_size_lamports: int | None,
    dip_level_index: int | None,
    reason_codes: tuple[str, ...],
) -> EntryRuleDecision:
    return EntryRuleDecision(
        action=action,
        as_of_slot=evidence.as_of_slot,
        token_mint=evidence.token_mint,
        quote_size_lamports=quote_size_lamports,
        dip_level_index=dip_level_index,
        reason_codes=reason_codes,
        next_state=state,
    )


def _non_negative_int(value: object) -> bool:
    return type(value) is int and value >= 0


def _abstain(reason: AbstainReason, message: str, as_of_slot: int) -> AbstainResult:
    return AbstainResult(reason=reason, message=message, as_of_slot=as_of_slot)


__all__ = [
    "BigBuySellLevel",
    "BuyTheDipLevel",
    "EntryRuleAction",
    "EntryRuleDecision",
    "EntryRuleInput",
    "EntryRuleState",
    "ExitRuleAction",
    "ExitRuleDecision",
    "ExitRuleInput",
    "ExitRuleState",
    "PlaybookRules",
    "RootLossCounterState",
    "SellLevel",
    "SellRules",
    "TrailingStopLevel",
    "advance_root_loss_counter",
    "evaluate_entry_rules",
    "evaluate_exit_rules",
]
