"""Pure exit-rule arithmetic: a 100% level must leave no rounding dust."""

from rugbot.decision.playbook_rules import (
    ExitRuleInput,
    ExitRuleState,
    PlaybookRules,
    SellLevel,
    SellRules,
    evaluate_exit_rules,
)

RULES = PlaybookRules(
    sell=SellRules(
        take_profit_levels=(
            SellLevel(trigger_pnl_ppm=100_000, sell_fraction_ppm=333_333),
            SellLevel(trigger_pnl_ppm=200_000, sell_fraction_ppm=1_000_000),
        )
    )
)


def test_final_level_sells_everything_left() -> None:
    original = 1_000_000_001
    first = evaluate_exit_rules(
        rules=RULES,
        evidence=ExitRuleInput(
            as_of_slot=1, current_pnl_ppm=150_000, peak_pnl_ppm=150_000
        ),
        state=ExitRuleState(),
        current_position_base_units=original,
        original_position_base_units=original,
    )
    remaining = original - first.sell_amount_base_units
    second = evaluate_exit_rules(
        rules=RULES,
        evidence=ExitRuleInput(
            as_of_slot=2, current_pnl_ppm=250_000, peak_pnl_ppm=250_000
        ),
        state=first.next_state,
        current_position_base_units=remaining,
        original_position_base_units=original,
    )
    assert second.sell_amount_base_units == remaining
