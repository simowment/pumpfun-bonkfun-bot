"""Focused tests for the playbook configuration and pure rule path."""

import unittest

from rugbot.decision.playbook_rules import (
    BigBuySellLevel,
    BuyTheDipLevel,
    EntryRuleAction,
    EntryRuleInput,
    EntryRuleState,
    ExitRuleAction,
    ExitRuleInput,
    ExitRuleState,
    PlaybookRules,
    RootLossCounterState,
    SellLevel,
    SellRules,
    TrailingStopLevel,
    advance_root_loss_counter,
    evaluate_entry_rules,
    evaluate_exit_rules,
)
from rugbot.domain.decisions import AbstainReason, AbstainResult
from rugbot.graph.wallet_behavior import CanonicalBuyEvidence, WalletAssetKind
from rugbot.runtime.config import SniperConfigError, parse_sniper_config

WALLET = "11111111111111111111111111111111"
TEST_TOKEN_MINT = "test-token-mint"  # noqa: S105


class PlaybookConfigTests(unittest.TestCase):
    """Verify strict integer parsing for the requested playbook surface."""

    def test_parses_all_requested_rules_and_converts_durations(self) -> None:
        config = parse_sniper_config(
            f"""target:
  kind: wallet
  id: "{WALLET}"
execution:
  mode: paper
  quote_size_lamports: 1000000
rules:
  snipe_delay_seconds: 5
  min_market_cap_quote_base_units: 5000
  max_market_cap_quote_base_units: 50000
  max_token_age_minutes: 10
  follow_cooldown_seconds: 30
  buy_only_once: true
  buy_the_dip:
    levels:
      - drawdown_ppm: 200000
        quote_size_lamports: 500000
      - drawdown_ppm: 400000
        quote_size_lamports: 1000000
  sell:
    take_profit_levels:
      - trigger_pnl_ppm: 100000
        sell_fraction_ppm: 500000
    stop_loss_levels:
      - trigger_pnl_ppm: -300000
        sell_fraction_ppm: 1000000
    trailing_levels:
      - min_market_cap_quote_base_units: null
        drawdown_ppm: 200000
    no_activity_seconds: 30
  max_consecutive_losses: 10
"""
        )

        self.assertEqual(config.rules.snipe_delay_ms, 5_000)
        self.assertEqual(config.rules.max_token_age_ms, 600_000)
        self.assertEqual(config.rules.copytrade_cooldown_ms, 30_000)
        self.assertEqual(len(config.rules.buy_the_dip_levels), 2)
        self.assertEqual(config.rules.sell.no_activity_timeout_ms, 30_000)
        self.assertEqual(config.rules.max_consecutive_losses, 10)

    def test_rejects_float_rules_unknown_keys_and_invalid_level_count(self) -> None:
        base = f"""target:
  kind: wallet
  id: "{WALLET}"
execution:
  mode: observe
  quote_size_lamports: 1000000
"""
        with self.assertRaises(SniperConfigError):
            parse_sniper_config(base + "rules:\n  snipe_delay_seconds: 1.5\n")
        with self.assertRaises(SniperConfigError):
            parse_sniper_config(base + "rules:\n  made_up: 1\n")
        with self.assertRaises(SniperConfigError):
            parse_sniper_config(
                base
                + "rules:\n  buy_the_dip:\n    levels:\n"
                + "      - {drawdown_ppm: 1, quote_size_lamports: 1}\n" * 4
            )


class PlaybookEntryRuleTests(unittest.TestCase):
    """Verify delay, copy filters, market caps, dips, and loss gates."""

    def test_delay_waits_then_buy_and_reports_reasons(self) -> None:
        rules = PlaybookRules(snipe_delay_ms=5_000)
        early = evaluate_entry_rules(
            rules=rules,
            evidence=_entry(now_ms=4_999),
            state=EntryRuleState(),
            base_quote_size_lamports=100,
        )
        self.assertEqual(early.action, EntryRuleAction.WAIT)
        self.assertEqual(early.reason_codes, ("snipe_delay_active",))

        ready = evaluate_entry_rules(
            rules=rules,
            evidence=_entry(now_ms=5_000),
            state=EntryRuleState(),
            base_quote_size_lamports=100,
        )
        self.assertEqual(ready.action, EntryRuleAction.BUY)
        self.assertIn("snipe_delay_elapsed", ready.reason_codes)

    def test_market_cap_filter_abstains_without_measurement_or_outside_range(
        self,
    ) -> None:
        rules = PlaybookRules(
            min_market_cap_quote_base_units=500,
            max_market_cap_quote_base_units=1_000,
        )
        missing = evaluate_entry_rules(
            rules=rules,
            evidence=_entry(),
            state=EntryRuleState(),
            base_quote_size_lamports=100,
        )
        self.assertIsInstance(missing, AbstainResult)
        self.assertEqual(missing.reason, AbstainReason.MISSING_FEATURE)

        below = evaluate_entry_rules(
            rules=rules,
            evidence=_entry(market_cap=499),
            state=EntryRuleState(),
            base_quote_size_lamports=100,
        )
        self.assertEqual(below.action, EntryRuleAction.ABSTAIN)
        self.assertEqual(below.reason_codes, ("market_cap_below_minimum",))

    def test_copytrade_age_cooldown_and_buy_only_once_are_distinct(self) -> None:
        rules = PlaybookRules(
            max_token_age_ms=10_000,
            copytrade_cooldown_ms=5_000,
            buy_only_once=True,
        )
        cooldown = evaluate_entry_rules(
            rules=rules,
            evidence=_entry(now_ms=3_000, copytrade=True, created_ms=0),
            state=EntryRuleState(last_copytrade_entry_ms=1_000),
            base_quote_size_lamports=100,
        )
        self.assertEqual(cooldown.action, EntryRuleAction.ABSTAIN)
        self.assertIn("copytrade_cooldown_active", cooldown.reason_codes)

        too_old = evaluate_entry_rules(
            rules=rules,
            evidence=_entry(now_ms=20_000, copytrade=True, created_ms=0),
            state=EntryRuleState(),
            base_quote_size_lamports=100,
        )
        self.assertIn("max_token_age_exceeded", too_old.reason_codes)

        once = evaluate_entry_rules(
            rules=rules,
            evidence=_entry(copytrade=True, created_ms=0),
            state=EntryRuleState(bought_token_mints=(TEST_TOKEN_MINT,)),
            base_quote_size_lamports=100,
        )
        self.assertIn("buy_only_once_already_bought", once.reason_codes)

    def test_dip_levels_are_one_shot_and_use_integer_drawdown(self) -> None:
        rules = PlaybookRules(
            buy_the_dip_levels=(
                BuyTheDipLevel(200_000, 50),
                BuyTheDipLevel(400_000, 100),
            )
        )
        first = evaluate_entry_rules(
            rules=rules,
            evidence=_entry(
                dip=True,
                ath_market_cap=1_000,
                current_market_cap=700,
            ),
            state=EntryRuleState(),
            base_quote_size_lamports=10,
        )
        self.assertEqual(first.action, EntryRuleAction.BUY)
        self.assertEqual(first.dip_level_index, 0)
        self.assertEqual(first.quote_size_lamports, 50)

        same_level = evaluate_entry_rules(
            rules=rules,
            evidence=_entry(dip=True, ath_market_cap=1_000, current_market_cap=700),
            state=first.next_state,
            base_quote_size_lamports=10,
        )
        self.assertEqual(same_level.action, EntryRuleAction.WAIT)

        second = evaluate_entry_rules(
            rules=rules,
            evidence=_entry(dip=True, ath_market_cap=1_000, current_market_cap=500),
            state=first.next_state,
            base_quote_size_lamports=10,
        )
        self.assertEqual(second.dip_level_index, 1)
        self.assertEqual(second.quote_size_lamports, 100)

    def test_dip_evidence_is_carried_into_next_state(self) -> None:
        rules = PlaybookRules(
            buy_the_dip_levels=(
                BuyTheDipLevel(200_000, 50),
                BuyTheDipLevel(400_000, 100),
            )
        )
        result = evaluate_entry_rules(
            rules=rules,
            evidence=_entry(
                dip=True,
                ath_market_cap=1_000,
                current_market_cap=500,
                dip_filled=(0,),
            ),
            state=EntryRuleState(),
            base_quote_size_lamports=10,
        )
        self.assertEqual(result.dip_level_index, 1)
        self.assertEqual(
            result.next_state.dip_filled_levels_by_token,
            ((TEST_TOKEN_MINT, (0, 1)),),
        )

        repeated = evaluate_entry_rules(
            rules=rules,
            evidence=_entry(
                dip=True,
                ath_market_cap=1_000,
                current_market_cap=500,
            ),
            state=result.next_state,
            base_quote_size_lamports=10,
        )
        self.assertEqual(repeated.action, EntryRuleAction.WAIT)

    def test_rejected_entry_preserves_state(self) -> None:
        state = EntryRuleState(
            bought_token_mints=(TEST_TOKEN_MINT,),
            last_copytrade_entry_ms=1_000,
        )
        result = evaluate_entry_rules(
            rules=PlaybookRules(max_market_cap_quote_base_units=100),
            evidence=_entry(market_cap=101, copytrade=True),
            state=state,
            base_quote_size_lamports=100,
        )
        self.assertEqual(result.next_state, state)

    def test_dip_uses_current_market_cap_for_configured_bounds(self) -> None:
        result = evaluate_entry_rules(
            rules=PlaybookRules(
                min_market_cap_quote_base_units=500,
                max_market_cap_quote_base_units=600,
                buy_the_dip_levels=(BuyTheDipLevel(400_000, 50),),
            ),
            evidence=_entry(
                dip=True,
                ath_market_cap=1_000,
                current_market_cap=500,
            ),
            state=EntryRuleState(),
            base_quote_size_lamports=10,
        )
        self.assertEqual(result.action, EntryRuleAction.BUY)

    def test_root_max_loss_gate_and_win_reset(self) -> None:
        rules = PlaybookRules(max_consecutive_losses=2)
        blocked = evaluate_entry_rules(
            rules=rules,
            evidence=_entry(),
            state=EntryRuleState(root_consecutive_losses=2),
            base_quote_size_lamports=100,
        )
        self.assertIn("max_consecutive_losses_reached", blocked.reason_codes)

        state = RootLossCounterState(root_id="root")
        state = advance_root_loss_counter(state=state, net_pnl_lamports=-1)
        state = advance_root_loss_counter(state=state, net_pnl_lamports=-1)
        reset = advance_root_loss_counter(state=state, net_pnl_lamports=0)
        self.assertEqual(reset.consecutive_losses, 0)


class PlaybookExitRuleTests(unittest.TestCase):
    """Verify multi-exit, trailing, and inactivity behavior."""

    def test_multi_tp_sells_cumulative_fractions(self) -> None:
        rules = PlaybookRules(
            sell=SellRules(
                take_profit_levels=(
                    SellLevel(100_000, 500_000),
                    SellLevel(300_000, 1_000_000),
                ),
                stop_loss_levels=(SellLevel(-300_000, 1_000_000),),
            )
        )
        first = evaluate_exit_rules(
            rules=rules,
            evidence=ExitRuleInput(
                as_of_slot=1, current_pnl_ppm=100_000, peak_pnl_ppm=100_000
            ),
            state=ExitRuleState(),
            current_position_base_units=100,
            original_position_base_units=100,
        )
        self.assertEqual(first.action, ExitRuleAction.SELL)
        self.assertEqual(first.sell_amount_base_units, 50)
        second = evaluate_exit_rules(
            rules=rules,
            evidence=ExitRuleInput(
                as_of_slot=2, current_pnl_ppm=300_000, peak_pnl_ppm=300_000
            ),
            state=first.next_state,
            current_position_base_units=50,
            original_position_base_units=100,
        )
        self.assertEqual(second.sell_amount_base_units, 50)
        self.assertIn("take_profit_level_1_triggered", second.reason_codes)

    def test_trailing_overrides_tp_and_no_activity_is_full_exit(self) -> None:
        rules = PlaybookRules(
            sell=SellRules(
                take_profit_levels=(SellLevel(100_000, 500_000),),
                trailing_levels=(TrailingStopLevel(None, 200_000),),
                no_activity_timeout_ms=30_000,
            )
        )
        trailing = evaluate_exit_rules(
            rules=rules,
            evidence=ExitRuleInput(
                as_of_slot=1, current_pnl_ppm=300_000, peak_pnl_ppm=600_000
            ),
            state=ExitRuleState(),
            current_position_base_units=100,
            original_position_base_units=100,
        )
        self.assertIn("trailing_stop_level_0_triggered", trailing.reason_codes)
        self.assertEqual(trailing.sell_amount_base_units, 100)

        inactive = evaluate_exit_rules(
            rules=rules,
            evidence=ExitRuleInput(
                as_of_slot=2, current_pnl_ppm=0, peak_pnl_ppm=0, idle_ms=30_000
            ),
            state=ExitRuleState(),
            current_position_base_units=100,
            original_position_base_units=100,
        )
        self.assertIn("no_activity_timeout", inactive.reason_codes)

    def test_take_profit_is_ignored_when_no_trailing_tier_matches(self) -> None:
        rules = PlaybookRules(
            sell=SellRules(
                take_profit_levels=(SellLevel(100_000, 500_000),),
                trailing_levels=(TrailingStopLevel(1_000, 200_000),),
            )
        )
        result = evaluate_exit_rules(
            rules=rules,
            evidence=ExitRuleInput(
                as_of_slot=1,
                current_pnl_ppm=100_000,
                peak_pnl_ppm=100_000,
                current_market_cap_quote_base_units=500,
            ),
            state=ExitRuleState(),
            current_position_base_units=100,
            original_position_base_units=100,
        )
        self.assertEqual(result.action, ExitRuleAction.HOLD)
        self.assertIn("trailing_stop_active_no_trigger", result.reason_codes)

    def test_big_buy_ranges_scale_out_once_and_then_escalate(self) -> None:
        rules = PlaybookRules(
            sell=SellRules(
                auto_sell_big_buy_levels=(
                    BigBuySellLevel(200, 300, 500_000),
                    BigBuySellLevel(400, 1_000, 1_000_000),
                )
            )
        )
        first = evaluate_exit_rules(
            rules=rules,
            evidence=ExitRuleInput(
                as_of_slot=1,
                current_pnl_ppm=0,
                peak_pnl_ppm=0,
                token_mint=TEST_TOKEN_MINT,
                big_buy_evidence=_big_buy(250),
            ),
            state=ExitRuleState(),
            current_position_base_units=100,
            original_position_base_units=100,
        )
        self.assertEqual(first.action, ExitRuleAction.SELL)
        self.assertEqual(first.sell_amount_base_units, 50)
        self.assertIn("big_buy_level_0_triggered", first.reason_codes)

        repeated = evaluate_exit_rules(
            rules=rules,
            evidence=ExitRuleInput(
                as_of_slot=2,
                current_pnl_ppm=0,
                peak_pnl_ppm=0,
                token_mint=TEST_TOKEN_MINT,
                big_buy_evidence=_big_buy(250),
            ),
            state=first.next_state,
            current_position_base_units=50,
            original_position_base_units=100,
        )
        self.assertEqual(repeated.action, ExitRuleAction.HOLD)

        second = evaluate_exit_rules(
            rules=rules,
            evidence=ExitRuleInput(
                as_of_slot=3,
                current_pnl_ppm=0,
                peak_pnl_ppm=0,
                token_mint=TEST_TOKEN_MINT,
                big_buy_evidence=_big_buy(500),
            ),
            state=repeated.next_state,
            current_position_base_units=50,
            original_position_base_units=100,
        )
        self.assertEqual(second.action, ExitRuleAction.SELL)
        self.assertEqual(second.sell_amount_base_units, 50)
        self.assertIn("big_buy_level_1_triggered", second.reason_codes)

    def test_big_buy_without_matching_provenance_abstains(self) -> None:
        rules = PlaybookRules(
            sell=SellRules(
                auto_sell_big_buy_levels=(BigBuySellLevel(200, 300, 500_000),)
            )
        )
        result = evaluate_exit_rules(
            rules=rules,
            evidence=ExitRuleInput(
                as_of_slot=1,
                current_pnl_ppm=0,
                peak_pnl_ppm=0,
                token_mint=TEST_TOKEN_MINT,
                big_buy_evidence=_big_buy(250, evidence_ids=()),
            ),
            state=ExitRuleState(),
            current_position_base_units=100,
            original_position_base_units=100,
        )
        self.assertIsInstance(result, AbstainResult)
        if isinstance(result, AbstainResult):
            self.assertIs(result.reason, AbstainReason.MISSING_FEATURE)

    def test_big_buy_ranges_must_be_non_overlapping(self) -> None:
        result = evaluate_exit_rules(
            rules=PlaybookRules(
                sell=SellRules(
                    auto_sell_big_buy_levels=(
                        BigBuySellLevel(200, 400, 500_000),
                        BigBuySellLevel(400, 600, 1_000_000),
                    )
                )
            ),
            evidence=ExitRuleInput(as_of_slot=1, current_pnl_ppm=0, peak_pnl_ppm=0),
            state=ExitRuleState(),
            current_position_base_units=100,
            original_position_base_units=100,
        )
        self.assertIsInstance(result, AbstainResult)
        if isinstance(result, AbstainResult):
            self.assertIs(result.reason, AbstainReason.UNSUPPORTED_PROTOCOL_STATE)


def _entry(  # noqa: PLR0913
    *,
    now_ms: int = 0,
    copytrade: bool = False,
    created_ms: int | None = None,
    market_cap: int | None = None,
    dip: bool = False,
    ath_market_cap: int | None = None,
    current_market_cap: int | None = None,
    dip_filled: tuple[int, ...] = (),
) -> EntryRuleInput:
    return EntryRuleInput(
        as_of_slot=1,
        token_mint=TEST_TOKEN_MINT,
        now_ms=now_ms,
        event_time_ms=0,
        is_copytrade=copytrade,
        token_created_time_ms=created_ms,
        market_cap_quote_base_units=market_cap,
        is_buy_the_dip=dip,
        ath_market_cap_quote_base_units=ath_market_cap,
        current_market_cap_quote_base_units=current_market_cap,
        dip_filled_level_indices=dip_filled,
    )


def _big_buy(
    quote_amount: int,
    *,
    evidence_ids: tuple[str, ...] = ("buy:1",),
) -> CanonicalBuyEvidence:
    return CanonicalBuyEvidence(
        as_of_slot=1,
        slot=1,
        transaction_index=0,
        event_index=0,
        signature=b"buy-signature",
        evidence_ids=evidence_ids,
        wallet=WALLET,
        token_mint=TEST_TOKEN_MINT,
        base_amount_base_units=1,
        quote_asset_kind=WalletAssetKind.NATIVE,
        quote_asset_id="SOL",
        quote_amount_base_units=quote_amount,
    )


if __name__ == "__main__":
    unittest.main()
