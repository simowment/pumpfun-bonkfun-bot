"""Focused tests for the pure paper round-trip simulator."""

import unittest
from dataclasses import replace
from typing import cast

from rugbot.decision.sizing import EntryLatencySnapshot
from rugbot.domain.amounts import QuoteBaseUnits, Slot, TokenBaseUnits
from rugbot.domain.decisions import AbstainReason, AbstainResult
from rugbot.domain.fees import FeeConfig
from rugbot.domain.quotes import QuotePath
from rugbot.execution.paper import PaperExecutionPort
from rugbot.execution.paper_simulator import (
    PaperRoundTripInputs,
    PaperRoundTripResult,
    PaperRoundTripSimulator,
    PaperStress,
    simulate_paper_round_trip,
)
from rugbot.execution.ports import ExecutionIntent
from rugbot.protocol.pump.bonding_curve_account import (
    PINNED_PUMP_IDL_SHA256,
    PUMP_BONDING_CURVE_ACCOUNT_DECODER_VERSION,
)
from rugbot.protocol.pump.quote_engine import PoolReserves


class PaperRoundTripTests(unittest.IsolatedAsyncioTestCase):
    """Verify complete paper entry and stressed full-exit behavior."""

    def test_accepts_integer_round_trip_with_fees_stress_and_latency(self) -> None:
        result = simulate_paper_round_trip(
            inputs=_inputs(
                stress=PaperStress(
                    latency_snapshot=_latency(),
                    max_entry_latency_ms=200,
                    max_exit_latency_ms=300,
                    entry_slippage_bps=25,
                    entry_impact_bps=50,
                    exit_slippage_bps=40,
                    exit_impact_bps=60,
                )
            )
        )

        self.assertIsInstance(result, PaperRoundTripResult)
        result = cast("PaperRoundTripResult", result)
        self.assertTrue(result.accepted)
        self.assertIsNotNone(result.full_exit_quote)
        self.assertEqual(
            result.stressed_entry_output_base_units,
            TokenBaseUnits(
                result.entry_quote.output_amount_base_units * 9_925 // 10_000
            ),
        )
        exit_quote = result.full_exit_quote
        if exit_quote is None:
            self.fail("full exit quote was unexpectedly missing")
        self.assertEqual(
            result.stressed_full_exit_output_quote_base_units,
            QuoteBaseUnits(exit_quote.output_amount_base_units * 9_900 // 10_000),
        )
        self.assertEqual(
            result.total_fee_quote_base_units,
            result.entry_fee_quote_base_units + result.exit_fee_quote_base_units,
        )
        self.assertEqual(
            result.net_pnl_quote_base_units,
            int(result.stressed_full_exit_output_quote_base_units) - 10_000,
        )
        self.assertEqual(result.entry_latency_ms, 150)
        self.assertEqual(result.exit_latency_ms, 250)
        self.assertGreaterEqual(result.entry_price_impact_bps, 0)
        self.assertGreaterEqual(result.exit_price_impact_bps or 0, 0)
        self.assertFalse(result.entry_receipt.would_submit_transaction)
        self.assertFalse(result.exit_receipt.would_submit_transaction)
        self.assertIsNone(result.entry_receipt.signature)
        self.assertIsNone(result.exit_receipt.signature)

    def test_full_exit_uses_the_complete_stressed_position(self) -> None:
        result = simulate_paper_round_trip(
            inputs=_inputs(
                stress=PaperStress(
                    latency_snapshot=_latency(),
                    max_entry_latency_ms=200,
                    max_exit_latency_ms=300,
                    entry_slippage_bps=100,
                )
            )
        )

        self.assertIsInstance(result, PaperRoundTripResult)
        result = cast("PaperRoundTripResult", result)
        self.assertIsNotNone(result.exit_receipt)
        exit_receipt = result.exit_receipt
        if exit_receipt is None:
            self.fail("exit receipt was unexpectedly missing")
        self.assertEqual(
            exit_receipt.intent_id,
            f"{result.entry_receipt.intent_id}:full-exit",
        )
        self.assertEqual(
            exit_receipt.as_of_slot,
            result.as_of_slot,
        )
        self.assertEqual(
            result.full_exit_quote.input_amount_base_units,
            result.stressed_entry_output_base_units,
        )

    def test_missing_latency_snapshot_abstains(self) -> None:
        result = simulate_paper_round_trip(
            inputs=_inputs(
                stress=PaperStress(
                    latency_snapshot=None,
                    max_entry_latency_ms=200,
                    max_exit_latency_ms=300,
                )
            )
        )

        self.assertIsInstance(result, AbstainResult)
        self.assertEqual(
            cast("AbstainResult", result).reason, AbstainReason.MISSING_FEATURE
        )

    def test_stale_latency_snapshot_abstains(self) -> None:
        result = simulate_paper_round_trip(
            inputs=_inputs(
                stress=PaperStress(
                    latency_snapshot=_latency(as_of_slot=11),
                    max_entry_latency_ms=200,
                    max_exit_latency_ms=300,
                )
            )
        )

        self.assertIsInstance(result, AbstainResult)
        self.assertEqual(
            cast("AbstainResult", result).reason, AbstainReason.STALE_STATE
        )

    def test_slippage_above_intent_tolerance_rejects_entry(self) -> None:
        result = simulate_paper_round_trip(
            inputs=_inputs(
                max_slippage_bps=100,
                stress=PaperStress(
                    latency_snapshot=_latency(),
                    max_entry_latency_ms=200,
                    max_exit_latency_ms=300,
                    entry_slippage_bps=75,
                    entry_impact_bps=50,
                ),
            )
        )

        self.assertIsInstance(result, PaperRoundTripResult)
        result = cast("PaperRoundTripResult", result)
        self.assertFalse(result.accepted)
        self.assertEqual(result.reason_codes, ("entry_slippage_above_tolerance",))
        self.assertIsNone(result.full_exit_quote)
        self.assertFalse(result.entry_receipt.accepted)

    def test_latency_budget_rejects_exit(self) -> None:
        result = simulate_paper_round_trip(
            inputs=_inputs(
                stress=PaperStress(
                    latency_snapshot=_latency(),
                    max_entry_latency_ms=200,
                    max_exit_latency_ms=200,
                )
            )
        )

        self.assertIsInstance(result, PaperRoundTripResult)
        result = cast("PaperRoundTripResult", result)
        self.assertFalse(result.accepted)
        self.assertEqual(result.reason_codes, ("exit_latency_budget_exceeded",))
        self.assertIsNotNone(result.full_exit_quote)
        exit_receipt = result.exit_receipt
        if exit_receipt is None:
            self.fail("exit receipt was unexpectedly missing")
        self.assertFalse(exit_receipt.accepted)

    def test_deterministic_full_exit_failure_is_rejected(self) -> None:
        result = simulate_paper_round_trip(
            inputs=_inputs(
                stress=PaperStress(
                    latency_snapshot=_latency(),
                    max_entry_latency_ms=200,
                    max_exit_latency_ms=300,
                    full_exit_failure_ppm=250_000,
                    failure_probe_ppm=100_000,
                )
            )
        )

        self.assertIsInstance(result, PaperRoundTripResult)
        result = cast("PaperRoundTripResult", result)
        self.assertFalse(result.accepted)
        self.assertEqual(result.reason_codes, ("full_exit_execution_failed",))
        self.assertIsNotNone(result.full_exit_quote)
        self.assertIsNotNone(result.exit_receipt)
        exit_receipt = result.exit_receipt
        if exit_receipt is None:
            self.fail("exit receipt was unexpectedly missing")
        self.assertFalse(exit_receipt.accepted)

    async def test_paper_port_uses_round_trip_acceptance(self) -> None:
        simulator = PaperRoundTripSimulator(
            as_of_slot=10,
            path=QuotePath.PUMP_BONDING_CURVE,
            reserves=_reserves(),
            fee_config=_fee_config(),
            stress=PaperStress(
                latency_snapshot=_latency(),
                max_entry_latency_ms=200,
                max_exit_latency_ms=300,
            ),
        )
        receipt = await PaperExecutionPort(simulator).submit(_intent())

        self.assertTrue(receipt.accepted)
        self.assertFalse(receipt.would_submit_transaction)
        self.assertIsNotNone(receipt.simulated_output_base_units)

    async def test_paper_port_fills_full_sell_after_buy(self) -> None:
        simulator = PaperRoundTripSimulator(
            as_of_slot=10,
            path=QuotePath.PUMP_BONDING_CURVE,
            reserves=_reserves(),
            fee_config=_fee_config(),
            stress=PaperStress(
                latency_snapshot=_latency(),
                max_entry_latency_ms=200,
                max_exit_latency_ms=300,
            ),
        )
        port = PaperExecutionPort(simulator)
        buy = await port.submit(_intent())
        if buy.simulated_output_base_units is None:
            self.fail("paper buy did not produce a position")

        sell = await port.submit(
            replace(
                _intent(),
                intent_id="paper-test-exit",
                side="sell",
                quote_amount_base_units=None,
                base_amount_base_units=buy.simulated_output_base_units,
                reason_codes=("paper_exit",),
            )
        )

        self.assertTrue(sell.accepted)
        self.assertFalse(sell.would_submit_transaction)
        self.assertIsNone(sell.signature)
        self.assertIsNotNone(sell.simulated_output_base_units)


def _inputs(
    *,
    stress: PaperStress | None = None,
    max_slippage_bps: int = 500,
) -> PaperRoundTripInputs:
    return PaperRoundTripInputs(
        as_of_slot=Slot(10),
        path=QuotePath.PUMP_BONDING_CURVE,
        reserves=_reserves(),
        fee_config=_fee_config(),
        entry_intent=_intent(max_slippage_bps=max_slippage_bps),
        stress=stress
        or PaperStress(
            latency_snapshot=_latency(),
            max_entry_latency_ms=200,
            max_exit_latency_ms=300,
        ),
    )


def _intent(*, max_slippage_bps: int = 500) -> ExecutionIntent:
    return ExecutionIntent(
        intent_id="paper-test-entry",
        as_of_slot=Slot(10),
        market_id="mint-test",
        side="buy",
        quote_amount_base_units=QuoteBaseUnits(10_000),
        base_amount_base_units=None,
        max_slippage_bps=max_slippage_bps,
        reason_codes=("known_operator_wallet",),
    )


def _latency(*, as_of_slot: int = 10) -> EntryLatencySnapshot:
    return EntryLatencySnapshot(
        as_of_slot=Slot(as_of_slot),
        latency_snapshot_version="latency-v1",
        p99_entry_latency_ms=100,
        p99_exit_latency_ms=200,
        safety_margin_ms=50,
        evidence_ids=("latency-test",),
    )


def _reserves() -> PoolReserves:
    return PoolReserves(
        virtual_base_reserves=TokenBaseUnits(1_000_000),
        virtual_quote_reserves=QuoteBaseUnits(500_000),
        real_base_reserves=TokenBaseUnits(900_000),
        real_quote_reserves=QuoteBaseUnits(400_000),
        is_complete=False,
        as_of_slot=Slot(10),
        base_decimals=6,
        quote_decimals=9,
        decoder_version=PUMP_BONDING_CURVE_ACCOUNT_DECODER_VERSION,
        idl_hash=PINNED_PUMP_IDL_SHA256,
        program_config_version="pump-global-v1",
    )


def _fee_config() -> FeeConfig:
    return FeeConfig(
        version="paper-test-fees",
        protocol_fee_bps=100,
        creator_fee_bps=25,
        is_known=True,
        program_config_version="pump-global-v1",
        valid_from_slot=Slot(0),
        source_artifact_version="paper-test-fee-artifact",
    )


if __name__ == "__main__":
    unittest.main()
