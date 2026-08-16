"""Tests for the immutable, non-signing paper position lifecycle."""

import unittest
from dataclasses import FrozenInstanceError, replace
from typing import Any, cast

from rugbot.decision.playbook_rules import (
    ExitRuleAction,
    PlaybookRules,
    SellLevel,
    SellRules,
    TrailingStopLevel,
)
from rugbot.domain.amounts import QuoteBaseUnits, Slot, TokenBaseUnits
from rugbot.domain.decisions import AbstainReason, AbstainResult
from rugbot.execution.ports import validate_execution_intent
from rugbot.execution.position_runtime import (
    CalibratedExitEvidence,
    PaperPositionState,
    PositionMarketEvidence,
    PositionRuntimeDecision,
    advance_paper_position,
)

MARKET_ID = "test-market"


class PositionRuntimeTests(unittest.TestCase):
    """Verify stateful exit rules without RPC, signing, or submission."""

    def test_tp_levels_emit_sell_intents_and_preserve_state(self) -> None:
        rules = PlaybookRules(
            sell=SellRules(
                take_profit_levels=(
                    SellLevel(100_000, 500_000),
                    SellLevel(300_000, 1_000_000),
                )
            )
        )
        initial = _state()

        first = advance_paper_position(
            rules=rules,
            evidence=_evidence(slot=11, pnl=100_000),
            state=initial,
            max_slippage_bps=500,
        )

        self.assertIsInstance(first, PositionRuntimeDecision)
        first = cast("PositionRuntimeDecision", first)
        self.assertEqual(first.action, ExitRuleAction.SELL)
        self.assertIsNotNone(first.sell_intent)
        self.assertEqual(first.sell_intent.base_amount_base_units, 50)
        self.assertEqual(first.sell_intent.side, "sell")
        self.assertIsNone(first.sell_intent.quote_amount_base_units)
        self.assertIsNone(validate_execution_intent(first.sell_intent))
        self.assertEqual(first.next_state.current_position_base_units, 50)
        self.assertEqual(first.next_state.peak_pnl_ppm, 100_000)
        self.assertEqual(initial.current_position_base_units, 100)

        second = advance_paper_position(
            rules=rules,
            evidence=_evidence(slot=12, pnl=300_000, capacity=50),
            state=first.next_state,
            max_slippage_bps=500,
        )

        self.assertIsInstance(second, PositionRuntimeDecision)
        second = cast("PositionRuntimeDecision", second)
        self.assertEqual(second.sell_intent.base_amount_base_units, 50)
        self.assertEqual(second.next_state.current_position_base_units, 0)
        self.assertEqual(second.next_state.emitted_sell_intent_count, 2)
        self.assertNotEqual(first.sell_intent.intent_id, second.sell_intent.intent_id)

    def test_hold_updates_peak_then_trailing_stop_exits(self) -> None:
        rules = PlaybookRules(
            sell=SellRules(trailing_levels=(TrailingStopLevel(None, 200_000),))
        )

        hold = advance_paper_position(
            rules=rules,
            evidence=_evidence(slot=11, pnl=600_000),
            state=_state(),
            max_slippage_bps=300,
        )

        self.assertIsInstance(hold, PositionRuntimeDecision)
        hold = cast("PositionRuntimeDecision", hold)
        self.assertEqual(hold.action, ExitRuleAction.HOLD)
        self.assertIsNone(hold.sell_intent)
        self.assertEqual(hold.next_state.peak_pnl_ppm, 600_000)

        trailing = advance_paper_position(
            rules=rules,
            evidence=_evidence(slot=12, pnl=350_000),
            state=hold.next_state,
            max_slippage_bps=300,
        )

        self.assertIsInstance(trailing, PositionRuntimeDecision)
        trailing = cast("PositionRuntimeDecision", trailing)
        self.assertEqual(trailing.action, ExitRuleAction.SELL)
        self.assertEqual(trailing.sell_intent.base_amount_base_units, 100)
        self.assertIn("trailing_stop_level_0_triggered", trailing.reason_codes)

    def test_stop_loss_and_no_activity_rules_emit_full_sell(self) -> None:
        stop_loss = advance_paper_position(
            rules=PlaybookRules(
                sell=SellRules(stop_loss_levels=(SellLevel(-200_000, 1_000_000),))
            ),
            evidence=_evidence(slot=11, pnl=-200_000),
            state=_state(),
            max_slippage_bps=500,
        )
        inactive = advance_paper_position(
            rules=PlaybookRules(sell=SellRules(no_activity_timeout_ms=30_000)),
            evidence=_evidence(slot=11, pnl=0, idle_ms=30_000),
            state=_state(),
            max_slippage_bps=500,
        )

        self.assertIsInstance(stop_loss, PositionRuntimeDecision)
        self.assertEqual(stop_loss.sell_intent.base_amount_base_units, 100)
        self.assertIn("stop_loss_level_0_triggered", stop_loss.reason_codes)
        self.assertIsInstance(inactive, PositionRuntimeDecision)
        self.assertEqual(inactive.sell_intent.base_amount_base_units, 100)
        self.assertIn("no_activity_timeout", inactive.reason_codes)

    def test_calibrated_threshold_replaces_static_tp_and_sl(self) -> None:
        rules = PlaybookRules(
            sell=SellRules(
                take_profit_levels=(SellLevel(100_000, 500_000),),
                stop_loss_levels=(SellLevel(-100_000, 1_000_000),),
            )
        )
        below_threshold = advance_paper_position(
            rules=rules,
            evidence=_evidence(
                slot=11,
                pnl=100_000,
                calibration=CalibratedExitEvidence(
                    as_of_slot=Slot(11),
                    market_id=MARKET_ID,
                    take_profit_pnl_ppm=300_000,
                ),
            ),
            state=_state(),
            max_slippage_bps=500,
            require_calibrated_exit=True,
        )
        at_threshold = advance_paper_position(
            rules=rules,
            evidence=_evidence(
                slot=12,
                pnl=300_000,
                calibration=CalibratedExitEvidence(
                    as_of_slot=Slot(12),
                    market_id=MARKET_ID,
                    take_profit_pnl_ppm=300_000,
                ),
            ),
            state=_state(),
            max_slippage_bps=500,
            require_calibrated_exit=True,
        )

        self.assertIsInstance(below_threshold, PositionRuntimeDecision)
        self.assertEqual(below_threshold.action, ExitRuleAction.HOLD)
        self.assertIsInstance(at_threshold, PositionRuntimeDecision)
        self.assertEqual(at_threshold.sell_intent.base_amount_base_units, 100)
        self.assertIn("take_profit_level_0_triggered", at_threshold.reason_codes)

    def test_required_calibration_abstains_without_static_fallback(self) -> None:
        result = advance_paper_position(
            rules=PlaybookRules(
                sell=SellRules(
                    take_profit_levels=(SellLevel(100_000, 1_000_000),),
                )
            ),
            evidence=_evidence(slot=11, pnl=100_000),
            state=_state(),
            max_slippage_bps=500,
            require_calibrated_exit=True,
        )

        self.assertIsInstance(result, AbstainResult)
        self.assertEqual(result.reason, AbstainReason.MISSING_FEATURE)

    def test_malformed_and_adverse_calibration_fail_closed(self) -> None:
        malformed = advance_paper_position(
            rules=PlaybookRules(),
            evidence=replace(
                _evidence(slot=11, pnl=0),
                calibrated_exit_evidence=cast("Any", object()),
            ),
            state=_state(),
            max_slippage_bps=500,
            require_calibrated_exit=True,
        )
        adverse = advance_paper_position(
            rules=PlaybookRules(),
            evidence=_evidence(
                slot=11,
                pnl=-500_000,
                calibration=CalibratedExitEvidence(
                    as_of_slot=Slot(11),
                    market_id=MARKET_ID,
                    take_profit_pnl_ppm=300_000,
                    adverse_event_slot=Slot(11),
                ),
            ),
            state=_state(),
            max_slippage_bps=500,
            require_calibrated_exit=True,
        )

        self.assertIsInstance(malformed, AbstainResult)
        self.assertEqual(malformed.reason, AbstainReason.UNSUPPORTED_PROTOCOL_STATE)
        self.assertIsInstance(adverse, PositionRuntimeDecision)
        self.assertEqual(adverse.sell_intent.base_amount_base_units, 100)
        self.assertEqual(adverse.reason_codes, ("calibrated_adverse_event",))

    def test_required_full_exit_capacity_is_fail_closed_even_on_hold(self) -> None:
        missing = advance_paper_position(
            rules=PlaybookRules(),
            evidence=_evidence(slot=11, pnl=0, capacity=None),
            state=_state(),
            max_slippage_bps=500,
        )
        insufficient = advance_paper_position(
            rules=PlaybookRules(),
            evidence=_evidence(slot=11, pnl=0, capacity=99),
            state=_state(),
            max_slippage_bps=500,
        )

        self.assertEqual(missing.reason, AbstainReason.MISSING_FEATURE)
        self.assertEqual(
            insufficient.reason,
            AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
        )

    def test_optional_full_capacity_still_guards_emitted_sell_amount(self) -> None:
        rules = PlaybookRules(
            sell=SellRules(take_profit_levels=(SellLevel(100_000, 500_000),))
        )
        hold = advance_paper_position(
            rules=PlaybookRules(),
            evidence=_evidence(slot=11, pnl=0, capacity=None),
            state=_state(),
            max_slippage_bps=500,
            require_full_exit_capacity=False,
        )
        missing_sell_capacity = advance_paper_position(
            rules=rules,
            evidence=_evidence(slot=11, pnl=100_000, capacity=None),
            state=_state(),
            max_slippage_bps=500,
            require_full_exit_capacity=False,
        )
        insufficient_sell_capacity = advance_paper_position(
            rules=rules,
            evidence=_evidence(slot=11, pnl=100_000, capacity=49),
            state=_state(),
            max_slippage_bps=500,
            require_full_exit_capacity=False,
        )

        self.assertIsInstance(hold, PositionRuntimeDecision)
        self.assertIsInstance(missing_sell_capacity, AbstainResult)
        self.assertEqual(
            missing_sell_capacity.reason,
            AbstainReason.MISSING_FEATURE,
        )
        self.assertIsInstance(insufficient_sell_capacity, AbstainResult)

    def test_identity_slot_and_integer_evidence_are_validated(self) -> None:
        mismatched = advance_paper_position(
            rules=PlaybookRules(),
            evidence=replace(_evidence(slot=11, pnl=0), market_id="other"),
            state=_state(),
            max_slippage_bps=500,
        )
        stale = advance_paper_position(
            rules=PlaybookRules(),
            evidence=_evidence(slot=10, pnl=0),
            state=_state(),
            max_slippage_bps=500,
        )
        malformed = advance_paper_position(
            rules=PlaybookRules(),
            evidence=replace(
                _evidence(slot=11, pnl=0),
                current_pnl_ppm=cast("Any", 0.5),
            ),
            state=_state(),
            max_slippage_bps=500,
        )

        self.assertEqual(mismatched.reason, AbstainReason.DECODER_MISMATCH)
        self.assertEqual(stale.reason, AbstainReason.STALE_STATE)
        self.assertEqual(malformed.reason, AbstainReason.UNSUPPORTED_PROTOCOL_STATE)

    def test_state_and_evidence_are_frozen(self) -> None:
        state = _state()
        evidence = _evidence(slot=11, pnl=0)

        with self.assertRaises(FrozenInstanceError):
            state.current_position_base_units = TokenBaseUnits(1)  # type: ignore[misc]
        with self.assertRaises(FrozenInstanceError):
            evidence.current_pnl_ppm = 1  # type: ignore[misc]


def _state() -> PaperPositionState:
    return PaperPositionState(
        as_of_slot=Slot(10),
        market_id=MARKET_ID,
        original_position_base_units=TokenBaseUnits(100),
        current_position_base_units=TokenBaseUnits(100),
    )


def _evidence(
    *,
    slot: int,
    pnl: int,
    capacity: int | None = 100,
    idle_ms: int = 0,
    calibration: CalibratedExitEvidence | None = None,
) -> PositionMarketEvidence:
    return PositionMarketEvidence(
        as_of_slot=Slot(slot),
        market_id=MARKET_ID,
        current_pnl_ppm=pnl,
        idle_ms=idle_ms,
        executable_exit_capacity_base_units=(
            None if capacity is None else TokenBaseUnits(capacity)
        ),
        current_market_cap_quote_base_units=QuoteBaseUnits(1_000_000),
        calibrated_exit_evidence=calibration,
    )


if __name__ == "__main__":
    unittest.main()
