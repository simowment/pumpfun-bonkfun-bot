"""Focused tests for the pure core behavior feature reducer."""

import unittest
from dataclasses import FrozenInstanceError, dataclass
from typing import Any, cast

from rugbot.domain.amounts import QuoteBaseUnits, Slot, TokenBaseUnits
from rugbot.domain.decisions import AbstainReason, AbstainResult
from rugbot.models.core_behavior_features import (
    CoreBehaviorExitInput,
    CoreBehaviorFeatureInputs,
    CoreBehaviorFeatureSnapshot,
    CoreBehaviorFlowInput,
    CoreBehaviorMarketInput,
    reduce_core_behavior_features,
)


class CoreBehaviorFeatureReducerTests(unittest.TestCase):
    """Behavior and fail-closed tests for the core feature reducer."""

    def test_reduces_integer_features_at_one_point_in_time(self) -> None:
        result = reduce_core_behavior_features(inputs=_inputs())

        self.assertIsInstance(result, CoreBehaviorFeatureSnapshot)
        result = cast("CoreBehaviorFeatureSnapshot", result)
        self.assertEqual(result.as_of_slot, 110)
        self.assertEqual(result.elapsed_slots, 10)
        self.assertEqual(result.curve_progress_ppm, 250_000)
        self.assertEqual(result.buy_sell_imbalance_ppm, 500_000)
        self.assertEqual(result.independent_buyer_count, 7)
        self.assertEqual(result.operator_inventory_share_ppm, 250_000)
        self.assertEqual(result.linked_wallet_sell_pressure_ppm, 300_000)
        self.assertEqual(
            result.absorbable_external_volume_quote_base_units,
            1_200,
        )
        self.assertEqual(result.executable_exit_capacity_base_units, 500)

    def test_signed_imbalance_is_symmetric_and_integer_only(self) -> None:
        result = reduce_core_behavior_features(
            inputs=_inputs(
                _InputOverrides(
                    buy_volume=1,
                    sell_volume=2,
                    linked_sell_volume=2,
                )
            )
        )

        self.assertIsInstance(result, CoreBehaviorFeatureSnapshot)
        result = cast("CoreBehaviorFeatureSnapshot", result)
        self.assertEqual(result.buy_sell_imbalance_ppm, -333_333)
        self.assertEqual(result.linked_wallet_sell_pressure_ppm, 1_000_000)

    def test_buy_only_flow_has_zero_linked_sell_pressure(self) -> None:
        result = reduce_core_behavior_features(
            inputs=_inputs(_InputOverrides(sell_volume=0, linked_sell_volume=0))
        )

        self.assertIsInstance(result, CoreBehaviorFeatureSnapshot)
        result = cast("CoreBehaviorFeatureSnapshot", result)
        self.assertEqual(result.buy_sell_imbalance_ppm, 1_000_000)
        self.assertEqual(result.linked_wallet_sell_pressure_ppm, 0)

    def test_snapshot_is_immutable(self) -> None:
        result = reduce_core_behavior_features(inputs=_inputs())

        self.assertIsInstance(result, CoreBehaviorFeatureSnapshot)
        with self.assertRaises(FrozenInstanceError):
            cast("Any", result).elapsed_slots = 99

    def test_nested_slot_mismatch_abstains(self) -> None:
        result = reduce_core_behavior_features(
            inputs=_inputs(_InputOverrides(flow_as_of_slot=109))
        )

        self.assert_abstains(result, AbstainReason.STALE_STATE, 110)

    def test_future_launch_slot_abstains_without_leaking_future_state(self) -> None:
        result = reduce_core_behavior_features(
            inputs=_inputs(_InputOverrides(launch_slot=111))
        )

        self.assert_abstains(result, AbstainReason.STALE_STATE, 110)

    def test_untrusted_component_abstains(self) -> None:
        result = reduce_core_behavior_features(
            inputs=_inputs(_InputOverrides(flow_trusted=False))
        )

        self.assert_abstains(result, AbstainReason.UNKNOWN_PROTOCOL_STATE, 110)

    def test_unknown_trust_state_abstains(self) -> None:
        result = reduce_core_behavior_features(
            inputs=_inputs(_InputOverrides(flow_trusted=cast("Any", None)))
        )

        self.assert_abstains(result, AbstainReason.UNKNOWN_PROTOCOL_STATE, 110)

    def test_malformed_nested_input_abstains(self) -> None:
        inputs = _inputs()
        malformed = CoreBehaviorFeatureInputs(
            as_of_slot=inputs.as_of_slot,
            market=cast("Any", object()),
            flow=inputs.flow,
            exit=inputs.exit,
        )

        result = reduce_core_behavior_features(inputs=malformed)

        self.assert_abstains(
            result,
            AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
            110,
        )

    def test_missing_nested_input_abstains(self) -> None:
        inputs = _inputs()
        missing = CoreBehaviorFeatureInputs(
            as_of_slot=inputs.as_of_slot,
            market=inputs.market,
            flow=cast("Any", None),
            exit=inputs.exit,
        )

        result = reduce_core_behavior_features(inputs=missing)

        self.assert_abstains(result, AbstainReason.MISSING_FEATURE, 110)

    def test_missing_denominator_abstains(self) -> None:
        result = reduce_core_behavior_features(
            inputs=_inputs(_InputOverrides(external_supply=0))
        )

        self.assert_abstains(result, AbstainReason.MISSING_FEATURE, 110)

    def test_contradictory_linked_sell_volume_abstains(self) -> None:
        result = reduce_core_behavior_features(
            inputs=_inputs(_InputOverrides(sell_volume=100, linked_sell_volume=101))
        )

        self.assert_abstains(
            result,
            AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
            110,
        )

    def test_float_amount_abstains(self) -> None:
        result = reduce_core_behavior_features(
            inputs=_inputs(_InputOverrides(buy_volume=cast("Any", 1.5)))
        )

        self.assert_abstains(
            result,
            AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
            110,
        )

    def test_zero_total_flow_abstains(self) -> None:
        result = reduce_core_behavior_features(
            inputs=_inputs(_InputOverrides(buy_volume=0, sell_volume=0))
        )

        self.assert_abstains(result, AbstainReason.MISSING_FEATURE, 110)

    def assert_abstains(
        self,
        result: object,
        reason: AbstainReason,
        as_of_slot: int,
    ) -> None:
        self.assertIsInstance(result, AbstainResult)
        result = cast("AbstainResult", result)
        self.assertIs(result.reason, reason)
        self.assertEqual(result.as_of_slot, as_of_slot)


@dataclass(frozen=True, slots=True)
class _InputOverrides:
    """Optional test overrides for the valid point-in-time fixture."""

    as_of_slot: int = 110
    launch_slot: int = 100
    flow_as_of_slot: int = 110
    curve_progress: int = 250_000
    buy_volume: int = 900
    sell_volume: int = 300
    independent_buyers: int = 7
    operator_inventory: int = 250
    external_supply: int = 1_000
    linked_sell_volume: int = 90
    absorbable_external_volume: int = 1_200
    exit_capacity: int = 500
    flow_trusted: bool = True


def _inputs(overrides: _InputOverrides | None = None) -> CoreBehaviorFeatureInputs:
    """Build one valid fixture with explicit integer point-in-time inputs."""

    values = overrides or _InputOverrides()
    return CoreBehaviorFeatureInputs(
        as_of_slot=Slot(values.as_of_slot),
        market=CoreBehaviorMarketInput(
            as_of_slot=Slot(values.as_of_slot),
            launch_slot=Slot(values.launch_slot),
            curve_progress_ppm=values.curve_progress,
            trusted=True,
        ),
        flow=CoreBehaviorFlowInput(
            as_of_slot=Slot(values.flow_as_of_slot),
            buy_volume_quote_base_units=QuoteBaseUnits(values.buy_volume),
            sell_volume_quote_base_units=QuoteBaseUnits(values.sell_volume),
            independent_buyer_count=values.independent_buyers,
            operator_inventory_base_units=TokenBaseUnits(values.operator_inventory),
            external_circulating_supply_base_units=TokenBaseUnits(
                values.external_supply
            ),
            linked_wallet_sell_volume_quote_base_units=QuoteBaseUnits(
                values.linked_sell_volume
            ),
            absorbable_external_volume_quote_base_units=QuoteBaseUnits(
                values.absorbable_external_volume
            ),
            trusted=values.flow_trusted,
        ),
        exit=CoreBehaviorExitInput(
            as_of_slot=Slot(values.as_of_slot),
            position_base_units=TokenBaseUnits(500),
            executable_exit_capacity_base_units=TokenBaseUnits(values.exit_capacity),
            trusted=True,
        ),
    )
