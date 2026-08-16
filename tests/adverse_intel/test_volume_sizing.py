"""Tests for conservative volume and liquidity aware sizing."""

import unittest
from dataclasses import FrozenInstanceError, replace
from typing import cast

from rugbot.decision.volume_sizing import (
    ConservativeVolumeSize,
    VolumeSizingRequest,
    size_volume_liquidity_aware,
)
from rugbot.domain.decisions import AbstainReason, AbstainResult


class VolumeSizingTests(unittest.TestCase):
    """Prove that every selected size remains under every hard cap."""

    def test_selects_minimum_cap_and_reports_all_integer_bounds(self) -> None:
        result = size_volume_liquidity_aware(
            _request(
                requested_quote_base_units=1_000_000,
                bankroll_quote_base_units=2_000_000,
                max_bankroll_fraction_ppm=100_000,
                independent_volume_quote_base_units=10_000_000,
                max_independent_volume_fraction_ppm=100_000,
            )
        )

        self.assertIsInstance(result, ConservativeVolumeSize)
        result = cast("ConservativeVolumeSize", result)
        self.assertEqual(result.quote_size_base_units, 200_000)
        self.assertEqual(result.bankroll_cap_quote_base_units, 200_000)
        self.assertEqual(result.independent_volume_cap_quote_base_units, 1_000_000)
        self.assertEqual(result.limiting_constraints, ("bankroll",))
        self.assertLessEqual(result.projected_price_impact_ppm, 100_000)
        self.assertLessEqual(result.expected_token_output_base_units, 1_000_000)

    def test_independent_volume_cap_excludes_unusable_reported_volume(self) -> None:
        result = size_volume_liquidity_aware(
            _request(
                independent_volume_quote_base_units=400_000,
                max_independent_volume_fraction_ppm=25_000,
            )
        )

        self.assertIsInstance(result, ConservativeVolumeSize)
        result = cast("ConservativeVolumeSize", result)
        self.assertEqual(result.quote_size_base_units, 10_000)
        self.assertEqual(result.limiting_constraints, ("independent_volume",))

    def test_exact_cpmm_price_impact_caps_input(self) -> None:
        result = size_volume_liquidity_aware(
            _request(
                requested_quote_base_units=200_000,
                pool_quote_reserve_base_units=1_000_000,
                pool_token_reserve_base_units=1_000_000,
                max_price_impact_ppm=210_000,
                max_one_shot_exit_token_base_units=1_000_000,
            )
        )

        self.assertIsInstance(result, ConservativeVolumeSize)
        result = cast("ConservativeVolumeSize", result)
        self.assertEqual(result.quote_size_base_units, 100_000)
        self.assertEqual(result.price_impact_cap_quote_base_units, 100_000)
        self.assertEqual(result.projected_price_impact_ppm, 210_000)
        self.assertEqual(result.limiting_constraints, ("price_impact",))

    def test_one_shot_full_exit_capacity_caps_entry_token_output(self) -> None:
        result = size_volume_liquidity_aware(
            _request(
                requested_quote_base_units=500,
                pool_quote_reserve_base_units=1_000,
                pool_token_reserve_base_units=1_000,
                max_price_impact_ppm=1_000_000,
                max_one_shot_exit_token_base_units=90,
            )
        )

        self.assertIsInstance(result, ConservativeVolumeSize)
        result = cast("ConservativeVolumeSize", result)
        self.assertEqual(result.quote_size_base_units, 100)
        self.assertEqual(result.expected_token_output_base_units, 90)
        self.assertEqual(result.full_exit_cap_quote_base_units, 100)
        self.assertEqual(result.limiting_constraints, ("full_exit",))

    def test_requested_size_is_never_increased(self) -> None:
        result = size_volume_liquidity_aware(_request(requested_quote_base_units=123))

        self.assertIsInstance(result, ConservativeVolumeSize)
        result = cast("ConservativeVolumeSize", result)
        self.assertEqual(result.quote_size_base_units, 123)
        self.assertEqual(result.limiting_constraints, ("requested",))

    def test_large_integer_inputs_are_deterministic_without_float_rounding(
        self,
    ) -> None:
        request = _request(
            requested_quote_base_units=10**24,
            bankroll_quote_base_units=10**25,
            independent_volume_quote_base_units=10**26,
            pool_quote_reserve_base_units=10**27,
            pool_token_reserve_base_units=10**30,
            max_one_shot_exit_token_base_units=10**28,
        )

        first = size_volume_liquidity_aware(request)
        second = size_volume_liquidity_aware(request)

        self.assertEqual(first, second)
        self.assertIsInstance(first, ConservativeVolumeSize)

    def test_missing_values_fail_closed(self) -> None:
        fields = (
            "as_of_slot",
            "requested_quote_base_units",
            "bankroll_quote_base_units",
            "max_bankroll_fraction_ppm",
            "independent_volume_quote_base_units",
            "max_independent_volume_fraction_ppm",
            "pool_quote_reserve_base_units",
            "pool_token_reserve_base_units",
            "max_price_impact_ppm",
            "max_one_shot_exit_token_base_units",
        )
        for field_name in fields:
            with self.subTest(field_name=field_name):
                result = size_volume_liquidity_aware(
                    replace(_request(), **{field_name: None})
                )
                self.assertIsInstance(result, AbstainResult)
                result = cast("AbstainResult", result)
                self.assertEqual(result.reason, AbstainReason.MISSING_FEATURE)

    def test_invalid_numeric_values_and_bool_fail_closed(self) -> None:
        cases = (
            {"as_of_slot": -1},
            {"requested_quote_base_units": 0},
            {"bankroll_quote_base_units": -1},
            {"pool_quote_reserve_base_units": 0},
            {"pool_token_reserve_base_units": 0},
            {"independent_volume_quote_base_units": -1},
            {"max_one_shot_exit_token_base_units": -1},
            {"max_bankroll_fraction_ppm": 1_000_001},
            {"max_independent_volume_fraction_ppm": -1},
            {"max_price_impact_ppm": 1_000_001},
            {"requested_quote_base_units": True},
            {"max_price_impact_ppm": 0.5},
        )
        for changes in cases:
            with self.subTest(changes=changes):
                result = size_volume_liquidity_aware(replace(_request(), **changes))
                self.assertIsInstance(result, AbstainResult)
                result = cast("AbstainResult", result)
                self.assertEqual(
                    result.reason, AbstainReason.UNSUPPORTED_PROTOCOL_STATE
                )

    def test_zero_effective_cap_abstains_instead_of_rounding_up(self) -> None:
        cases = (
            {"max_bankroll_fraction_ppm": 0},
            {"independent_volume_quote_base_units": 0},
            {"max_independent_volume_fraction_ppm": 0},
            {"max_price_impact_ppm": 0},
            {"max_one_shot_exit_token_base_units": 0},
        )
        for changes in cases:
            with self.subTest(changes=changes):
                result = size_volume_liquidity_aware(replace(_request(), **changes))
                self.assertIsInstance(result, AbstainResult)
                result = cast("AbstainResult", result)
                self.assertEqual(
                    result.reason, AbstainReason.UNSUPPORTED_PROTOCOL_STATE
                )

    def test_request_and_result_are_immutable(self) -> None:
        request = _request()
        result = size_volume_liquidity_aware(request)
        self.assertIsInstance(result, ConservativeVolumeSize)
        result = cast("ConservativeVolumeSize", result)

        with self.assertRaises(FrozenInstanceError):
            request.requested_quote_base_units = 1  # type: ignore[misc]
        with self.assertRaises(FrozenInstanceError):
            result.quote_size_base_units = 1  # type: ignore[misc]

    def test_wrong_request_type_abstains(self) -> None:
        result = size_volume_liquidity_aware(None)  # type: ignore[arg-type]

        self.assertIsInstance(result, AbstainResult)
        result = cast("AbstainResult", result)
        self.assertEqual(result.reason, AbstainReason.MISSING_FEATURE)
        self.assertEqual(result.as_of_slot, 0)


def _request(**changes: object) -> VolumeSizingRequest:
    defaults: dict[str, object] = {
        "as_of_slot": 123,
        "requested_quote_base_units": 1_000_000,
        "bankroll_quote_base_units": 100_000_000,
        "max_bankroll_fraction_ppm": 100_000,
        "independent_volume_quote_base_units": 100_000_000,
        "max_independent_volume_fraction_ppm": 100_000,
        "pool_quote_reserve_base_units": 100_000_000,
        "pool_token_reserve_base_units": 100_000_000,
        "max_price_impact_ppm": 100_000,
        "max_one_shot_exit_token_base_units": 1_000_000,
    }
    defaults.update(changes)
    return VolumeSizingRequest(**defaults)  # type: ignore[arg-type]


if __name__ == "__main__":
    unittest.main()
