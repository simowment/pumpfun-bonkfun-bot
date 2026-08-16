"""Tests for leakage-safe outcome trajectory construction."""

# The fixture factory keeps all malformed-input cases in one place.
# ruff: noqa: PLR0913

from __future__ import annotations

import unittest
from uuid import UUID, uuid4

from rugbot.backtest.outcome_builder import (
    FinalizedOutcomePointInput,
    build_outcome_observation_point,
    build_outcome_trajectory,
)
from rugbot.domain.amounts import QuoteBaseUnits, Slot
from rugbot.domain.decisions import AbstainReason, AbstainResult
from rugbot.domain.observations import RawChainObservation
from rugbot.domain.quotes import ExecutableQuote, QuotePath
from rugbot.models.adverse_event import MarketTrajectoryPoint
from rugbot.models.outcome_labels import OutcomeObservationPoint


class OutcomeBuilderTests(unittest.TestCase):
    """Verify strict point-in-time construction and fail-closed joins."""

    def test_builds_point_from_finalized_state_and_executable_quote(self) -> None:
        point = build_outcome_observation_point(
            point=_input(slot=10, output=900),
            as_of_slot=Slot(20),
        )

        self.assertIsInstance(point, OutcomeObservationPoint)
        if isinstance(point, OutcomeObservationPoint):
            self.assertEqual(point.as_of_slot, Slot(20))
            self.assertEqual(point.slot, Slot(10))
            self.assertEqual(
                point.full_exit_output_quote_base_units, QuoteBaseUnits(900)
            )
            self.assertEqual(
                point.full_exit_execution_cost_quote_base_units, QuoteBaseUnits(7)
            )
            self.assertEqual(point.evidence_ids[1:], ("quote-evidence-10",))

    def test_trajectory_requires_strictly_increasing_slots(self) -> None:
        result = build_outcome_trajectory(
            points=(_input(slot=11, output=900), _input(slot=10, output=800)),
            as_of_slot=Slot(20),
        )

        self._assert_abstains(result, AbstainReason.UNSUPPORTED_PROTOCOL_STATE)

    def test_rejects_future_observation(self) -> None:
        result = build_outcome_observation_point(
            point=_input(slot=21, output=900),
            as_of_slot=Slot(20),
        )

        self._assert_abstains(result, AbstainReason.STALE_STATE)

    def test_rejects_future_market_state_boundary(self) -> None:
        result = build_outcome_observation_point(
            point=_input(slot=10, output=900, market_as_of_slot=21),
            as_of_slot=Slot(20),
        )

        self._assert_abstains(result, AbstainReason.STALE_STATE)

    def test_rejects_future_quote_boundary(self) -> None:
        result = build_outcome_observation_point(
            point=_input(slot=10, output=900, quote_as_of_slot=11),
            as_of_slot=Slot(20),
        )

        self._assert_abstains(result, AbstainReason.STALE_STATE)

    def test_rejects_non_finalized_observation(self) -> None:
        result = build_outcome_observation_point(
            point=_input(slot=10, output=900, commitment="confirmed"),
            as_of_slot=Slot(20),
        )

        self._assert_abstains(result, AbstainReason.STALE_STATE)

    def test_rejects_mismatched_market_event_identity(self) -> None:
        result = build_outcome_observation_point(
            point=_input(slot=10, output=900, market_event_index=2),
            as_of_slot=Slot(20),
        )

        self._assert_abstains(result, AbstainReason.UNSUPPORTED_PROTOCOL_STATE)

    def test_rejects_mutable_evidence_ids(self) -> None:
        point = _input(slot=10, output=900, evidence_ids=["mutable"])  # type: ignore[arg-type]

        result = build_outcome_observation_point(point=point, as_of_slot=Slot(20))

        self._assert_abstains(result, AbstainReason.MISSING_FEATURE)

    def test_rejects_duplicate_evidence_across_trajectory(self) -> None:
        first = _input(slot=10, output=900, evidence_ids=("shared",))
        second = _input(slot=11, output=850, evidence_ids=("shared",))

        result = build_outcome_trajectory(
            points=(first, second),
            as_of_slot=Slot(20),
        )

        self._assert_abstains(result, AbstainReason.UNSUPPORTED_PROTOCOL_STATE)

    def test_derived_observation_id_does_not_use_raw_uuid(self) -> None:
        first = _input(
            slot=10, output=900, raw_id=UUID("00000000-0000-0000-0000-000000000001")
        )
        second = _input(
            slot=10, output=900, raw_id=UUID("00000000-0000-0000-0000-000000000002")
        )

        first_result = build_outcome_observation_point(point=first, as_of_slot=Slot(20))
        second_result = build_outcome_observation_point(
            point=second, as_of_slot=Slot(20)
        )

        self.assertIsInstance(first_result, OutcomeObservationPoint)
        self.assertIsInstance(second_result, OutcomeObservationPoint)
        if isinstance(first_result, OutcomeObservationPoint) and isinstance(
            second_result, OutcomeObservationPoint
        ):
            self.assertEqual(
                first_result.evidence_ids[0], second_result.evidence_ids[0]
            )
            self.assertNotIn(
                str(first.observation.raw_id), first_result.evidence_ids[0]
            )
            self.assertNotIn(
                str(second.observation.raw_id), second_result.evidence_ids[0]
            )

    def test_requires_explicit_boolean_state_fields(self) -> None:
        point = _input(slot=10, output=900, curve_completed=1)  # type: ignore[arg-type]

        result = build_outcome_observation_point(point=point, as_of_slot=Slot(20))

        self._assert_abstains(result, AbstainReason.UNSUPPORTED_PROTOCOL_STATE)

    def test_rejects_missing_executable_quote(self) -> None:
        point = _input(slot=10, output=900, omit_quote=True)

        result = build_outcome_observation_point(point=point, as_of_slot=Slot(20))

        self._assert_abstains(result, AbstainReason.MISSING_FEATURE)

    def test_trajectory_preserves_input_order_and_is_immutable(self) -> None:
        result = build_outcome_trajectory(
            points=(_input(slot=10, output=900), _input(slot=11, output=850)),
            as_of_slot=Slot(20),
        )

        self.assertIsInstance(result, tuple)
        if isinstance(result, tuple):
            self.assertEqual(
                tuple(point.slot for point in result), (Slot(10), Slot(11))
            )
            with self.assertRaises(TypeError):
                result[0].evidence_ids[0] = "changed"  # type: ignore[index]

    def _assert_abstains(
        self,
        result: object,
        reason: AbstainReason,
    ) -> None:
        self.assertIsInstance(result, AbstainResult)
        if isinstance(result, AbstainResult):
            self.assertEqual(result.reason, reason)


def _input(
    *,
    slot: int,
    output: int,
    raw_id: UUID | None = None,
    commitment: str = "finalized",
    market_as_of_slot: int | None = None,
    quote_as_of_slot: int | None = None,
    market_event_index: int | None = None,
    evidence_ids: tuple[str, ...] | list[str] | None = None,
    curve_completed: bool = False,
    migration_observed: bool = False,
    full_exit_quote: ExecutableQuote | None = None,
    omit_quote: bool = False,
) -> FinalizedOutcomePointInput:
    selected_raw_id = raw_id or uuid4()
    event_index = slot if market_event_index is None else market_event_index
    observation = RawChainObservation(
        raw_id=selected_raw_id,
        source_id="fixture",
        observer_id="test",
        boot_id=UUID("00000000-0000-0000-0000-000000000010"),
        receive_sequence=slot,
        slot=slot,
        parent_slot=slot - 1,
        blockhash=b"blockhash",
        signature=bytes([slot % 251 + 1]) * 64,
        transaction_index=0,
        outer_instruction_index=0,
        inner_instruction_group_index=None,
        inner_instruction_index=None,
        stack_height=None,
        event_ordinal=slot,
        commitment=commitment,  # type: ignore[arg-type]
        canonical_status="canonical",
        received_wall_ns=slot,
        received_monotonic_ns=slot,
        program_id=b"program",
        account_pubkey=None,
        account_owner_program_id=None,
        raw_transaction=b"transaction",
        raw_transaction_format="json",
        raw_account_data=None,
        account_write_version=None,
        source_update_kind="transaction",
        raw_source_status=None,
        raw_source_payload=b"payload",
        decoder_name=None,
        decoder_version=None,
        idl_hash=None,
    )
    market_slot = slot
    market_state = MarketTrajectoryPoint(
        as_of_slot=Slot(slot if market_as_of_slot is None else market_as_of_slot),
        slot=Slot(market_slot),
        event_index=event_index,
        elapsed_ms=slot * 100,
        price_quote_base_units_per_token_base_unit_ppm=1_000 + output,
        real_quote_reserves_base_units=QuoteBaseUnits(10_000),
        curve_progress_ppm=500_000,
    )
    quote = (
        None
        if omit_quote
        else (
            full_exit_quote
            or ExecutableQuote(
                path=QuotePath.PUMP_BONDING_CURVE,
                as_of_slot=Slot(slot if quote_as_of_slot is None else quote_as_of_slot),
                input_amount_base_units=1_000,
                output_amount_base_units=output,
                fee_amount_base_units=7,
                base_decimals=6,
                quote_decimals=9,
                fee_config_version="fees",
                decoder_version="decoder",
                idl_hash="idl",
                program_config_version="config",
            )
        )
    )
    selected_evidence_ids = evidence_ids or (f"quote-evidence-{slot}",)
    return FinalizedOutcomePointInput(
        observation=observation,
        market_state=market_state,
        full_exit_quote=quote,  # type: ignore[arg-type]
        curve_completed=curve_completed,
        migration_observed=migration_observed,
        evidence_ids=selected_evidence_ids,  # type: ignore[arg-type]
    )


if __name__ == "__main__":
    unittest.main()
