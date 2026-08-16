"""Focused tests for the exact Pump paper-context resolver."""

import asyncio
import unittest
from dataclasses import FrozenInstanceError, replace
from typing import TYPE_CHECKING, cast

from rugbot.decision.sizing import EntryLatencySnapshot
from rugbot.domain.amounts import QuoteBaseUnits, Slot, TokenBaseUnits
from rugbot.domain.decisions import AbstainReason, AbstainResult
from rugbot.domain.fees import FeeConfig
from rugbot.execution.paper import PaperExecutionPort
from rugbot.execution.paper_simulator import PaperStress
from rugbot.execution.ports import ExecutionIntent, ExecutionMode
from rugbot.market_state.pump_create import (
    PumpCreateMarketState,
    PumpCreateReserveSnapshot,
)
from rugbot.protocol.pump.bonding_curve_account import (
    PINNED_PUMP_IDL_SHA256,
    PUMP_PROGRAM_ID,
)
from rugbot.protocol.pump.create_decoder import (
    SPL_2022_PROGRAM_ID,
)
from rugbot.protocol.pump.create_event_decoder import SOL_PUBKEY, PumpCreateEvent
from rugbot.protocol.pump.create_state_adapter import PumpCreateMintMetadataProof
from rugbot.protocol.pump.version_registry import (
    PUMP_VERSION_REGISTRY_VERSION,
    PumpProtocolVersionSnapshot,
)
from rugbot.runtime.paper_context import PaperContextInput, resolve_paper_context

if TYPE_CHECKING:
    from rugbot.domain.launches import LaunchCreatedV2

SLOT = Slot(371_337_946)
MINT = "GGYkiUJGopoH9DZUjGFtLiKpXXW5AQovqZ7eVcjEpump"


class RuntimePaperContextTests(unittest.TestCase):
    """Require all exact point-in-time proofs before paper execution."""

    def test_resolves_to_non_submitting_paper_port(self) -> None:
        result = resolve_paper_context(inputs=_context())

        self.assertIsInstance(result, PaperExecutionPort)
        port = cast("PaperExecutionPort", result)
        receipt = asyncio.run(port.submit(_entry_intent()))

        self.assertEqual(receipt.mode, ExecutionMode.PAPER)
        self.assertFalse(receipt.would_submit_transaction)
        self.assertIsNone(receipt.signature)
        self.assertTrue(receipt.accepted)
        self.assertIsNotNone(receipt.simulated_output_base_units)

    def test_missing_protocol_or_mint_proof_abstains(self) -> None:
        cases = (
            replace(_context(), protocol_snapshot=None),
            replace(_context(), mint_metadata=None),
        )

        for context in cases:
            with self.subTest(context=context):
                result = resolve_paper_context(inputs=context)
                self.assert_abstains(result, AbstainReason.MISSING_FEATURE)

    def test_stress_slot_mismatch_abstains(self) -> None:
        stress = replace(
            _paper_stress(),
            latency_snapshot=replace(
                cast("EntryLatencySnapshot", _paper_stress().latency_snapshot),
                as_of_slot=Slot(SLOT + 1),
            ),
        )

        result = resolve_paper_context(inputs=replace(_context(), stress=stress))

        self.assert_abstains(result, AbstainReason.STALE_STATE)

    def test_context_input_is_immutable(self) -> None:
        context = _context()

        with self.assertRaises(FrozenInstanceError):
            context.stress = None  # type: ignore[misc]

    def test_state_and_protocol_slots_must_match(self) -> None:
        protocol = replace(_protocol_snapshot(), as_of_slot=Slot(SLOT + 1))

        result = resolve_paper_context(
            inputs=replace(_context(), protocol_snapshot=protocol)
        )

        self.assert_abstains(result, AbstainReason.STALE_STATE)

    def assert_abstains(self, result: object, reason: AbstainReason) -> None:
        self.assertIsInstance(result, AbstainResult)
        if isinstance(result, AbstainResult):
            self.assertEqual(result.reason, reason)
            self.assertEqual(result.as_of_slot, SLOT)


def _context() -> PaperContextInput:
    return PaperContextInput(
        market_state=PumpCreateMarketState(
            launch=cast("LaunchCreatedV2", object()),
            create_event=cast("PumpCreateEvent", object()),
            reserves=_create_snapshot(),
        ),
        protocol_snapshot=_protocol_snapshot(),
        mint_metadata=_mint_metadata(),
        stress=_paper_stress(),
    )


def _create_snapshot() -> PumpCreateReserveSnapshot:
    return PumpCreateReserveSnapshot(
        as_of_slot=SLOT,
        mint_pubkey=MINT,
        bonding_curve_pubkey="GjcTf82RaMLVtjxa4aGMNBT2enax2Y6YMJ58DPLFsf1E",
        creator_pubkey="FQMB44WiMCobmBN53e7izj6ZfK4XfBr6y9qoc3PdqQN6",
        quote_mint_pubkey=SOL_PUBKEY,
        token_program_pubkey=SPL_2022_PROGRAM_ID,
        virtual_token_reserves=TokenBaseUnits(1_073_000_000_000_000),
        virtual_quote_reserves=QuoteBaseUnits(30_000_000_000),
        real_token_reserves=TokenBaseUnits(793_100_000_000_000),
        real_quote_reserves=QuoteBaseUnits(0),
        token_total_supply=TokenBaseUnits(1_000_000_000_000_000),
        complete=False,
        is_mayhem_mode=False,
        is_cashback_enabled=False,
        source_signature=b"s" * 64,
        transaction_index=0,
        outer_instruction_index=2,
        event_log_index=27,
        event_raw_data_sha256="a" * 64,
    )


def _mint_metadata() -> PumpCreateMintMetadataProof:
    return PumpCreateMintMetadataProof(
        as_of_slot=SLOT,
        base_mint_pubkey=MINT,
        quote_mint_pubkey=SOL_PUBKEY,
        base_decimals=6,
        quote_decimals=9,
        source_artifact="finalized-mint-account-fixture",
    )


def _protocol_snapshot() -> PumpProtocolVersionSnapshot:
    return PumpProtocolVersionSnapshot(
        as_of_slot=SLOT,
        program_id=PUMP_PROGRAM_ID,
        idl_hash=PINNED_PUMP_IDL_SHA256,
        global_config_hash="fixture-global-config-hash",
        program_config_version="pump-global-v1",
        fee_config=FeeConfig(
            version="pump-fees-fixture",
            protocol_fee_bps=100,
            creator_fee_bps=25,
            is_known=True,
            program_config_version="pump-global-v1",
            valid_from_slot=Slot(SLOT - 1),
            valid_to_slot=None,
            source_artifact_version="fee-fixture",
        ),
        program_config_source_artifact_version="program-config-fixture",
        fee_source_artifact_version="fee-fixture",
        registry_version=PUMP_VERSION_REGISTRY_VERSION,
    )


def _paper_stress() -> PaperStress:
    return PaperStress(
        latency_snapshot=EntryLatencySnapshot(
            as_of_slot=SLOT,
            latency_snapshot_version="fixture-latency",
            p99_entry_latency_ms=100,
            p99_exit_latency_ms=150,
            safety_margin_ms=25,
            evidence_ids=("fixture-latency-evidence",),
        ),
        max_entry_latency_ms=200,
        max_exit_latency_ms=250,
        entry_slippage_bps=25,
        exit_slippage_bps=25,
    )


def _entry_intent() -> ExecutionIntent:
    return ExecutionIntent(
        intent_id="runtime-paper-entry",
        as_of_slot=SLOT,
        market_id=MINT,
        side="buy",
        quote_amount_base_units=QuoteBaseUnits(1_000_000),
        base_amount_base_units=None,
        max_slippage_bps=500,
        reason_codes=("runtime_paper_context",),
    )


if __name__ == "__main__":
    unittest.main()
