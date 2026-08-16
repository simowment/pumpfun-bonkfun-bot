"""Integration tests for exact Pump create-state quote adaptation."""

import unittest
from dataclasses import replace
from typing import cast

from rugbot.decision.sizing import EntryLatencySnapshot
from rugbot.domain.amounts import QuoteBaseUnits, Slot, TokenBaseUnits
from rugbot.domain.decisions import AbstainReason, AbstainResult
from rugbot.domain.fees import FeeConfig
from rugbot.domain.quotes import QuotePath
from rugbot.execution.paper_simulator import (
    PaperRoundTripInputs,
    PaperRoundTripResult,
    PaperStress,
    simulate_paper_round_trip,
)
from rugbot.execution.ports import ExecutionIntent
from rugbot.market_state.pump_create import PumpCreateReserveSnapshot
from rugbot.protocol.pump.bonding_curve_account import (
    PINNED_PUMP_IDL_SHA256,
    PUMP_BONDING_CURVE_ACCOUNT_DECODER_VERSION,
    PUMP_PROGRAM_ID,
)
from rugbot.protocol.pump.create_decoder import (
    PUMP_CREATE_V2_DECODER_VERSION,
    SPL_2022_PROGRAM_ID,
)
from rugbot.protocol.pump.create_event_decoder import SOL_PUBKEY
from rugbot.protocol.pump.create_state_adapter import (
    PumpCreateMintMetadataProof,
    pump_create_snapshot_to_pool_reserves,
)
from rugbot.protocol.pump.quote_engine import PoolReserves
from rugbot.protocol.pump.version_registry import (
    PUMP_VERSION_REGISTRY_VERSION,
    PumpProtocolVersionSnapshot,
)

SLOT = Slot(371_337_946)
MINT = "GGYkiUJGopoH9DZUjGFtLiKpXXW5AQovqZ7eVcjEpump"


class PumpCreateStateAdapterTests(unittest.TestCase):
    """Require exact create and point-in-time provenance before quoting."""

    def test_exact_create_state_executes_quote_and_full_paper_exit(self) -> None:
        protocol = _protocol_snapshot()
        adapted = _adapt(protocol=protocol)

        self.assertIsInstance(adapted, PoolReserves)
        reserves = cast("PoolReserves", adapted)
        self.assertEqual(reserves.virtual_base_reserves, 1_073_000_000_000_000)
        self.assertEqual(reserves.virtual_quote_reserves, 30_000_000_000)
        self.assertEqual(reserves.real_base_reserves, 793_100_000_000_000)
        self.assertEqual(reserves.real_quote_reserves, 0)
        self.assertEqual(
            reserves.decoder_version,
            PUMP_BONDING_CURVE_ACCOUNT_DECODER_VERSION,
        )

        result = simulate_paper_round_trip(
            inputs=PaperRoundTripInputs(
                as_of_slot=SLOT,
                path=QuotePath.PUMP_BONDING_CURVE,
                reserves=reserves,
                fee_config=protocol.fee_config,
                entry_intent=_entry_intent(),
                stress=_paper_stress(),
            )
        )

        self.assertIsInstance(result, PaperRoundTripResult)
        result = cast("PaperRoundTripResult", result)
        self.assertTrue(result.accepted)
        self.assertTrue(result.entry_receipt.accepted)
        self.assertFalse(result.entry_receipt.would_submit_transaction)
        self.assertIsNotNone(result.full_exit_quote)
        self.assertIsNotNone(result.exit_receipt)
        self.assertTrue(result.exit_receipt and result.exit_receipt.accepted)
        self.assertFalse(
            result.exit_receipt and result.exit_receipt.would_submit_transaction
        )

    def test_missing_required_provenance_abstains(self) -> None:
        cases = (
            {"protocol_snapshot": None},
            {"mint_metadata": None},
            {"create_decoder_version": None},
            {"create_idl_hash": None},
        )

        for overrides in cases:
            with self.subTest(overrides=overrides):
                result = _adapt(**overrides)
                self.assert_abstains(result, AbstainReason.MISSING_FEATURE)

    def test_mismatched_or_stale_provenance_abstains(self) -> None:
        stale_protocol = replace(_protocol_snapshot(), as_of_slot=Slot(SLOT + 1))
        wrong_idl_protocol = replace(_protocol_snapshot(), idl_hash="0" * 64)
        stale_metadata = replace(_mint_metadata(), as_of_slot=Slot(SLOT + 1))
        wrong_mint_metadata = replace(_mint_metadata(), base_mint_pubkey="other")
        cases = (
            ({"protocol_snapshot": stale_protocol}, AbstainReason.STALE_STATE),
            (
                {"protocol_snapshot": wrong_idl_protocol},
                AbstainReason.DECODER_MISMATCH,
            ),
            ({"mint_metadata": stale_metadata}, AbstainReason.STALE_STATE),
            (
                {"mint_metadata": wrong_mint_metadata},
                AbstainReason.UNKNOWN_PROTOCOL_STATE,
            ),
            (
                {"create_decoder_version": "other-decoder"},
                AbstainReason.DECODER_MISMATCH,
            ),
            ({"create_idl_hash": "0" * 64}, AbstainReason.DECODER_MISMATCH),
        )

        for overrides, reason in cases:
            with self.subTest(overrides=overrides):
                result = _adapt(**overrides)
                self.assert_abstains(result, reason)

    def test_malformed_exact_snapshot_fields_abstain(self) -> None:
        cases = (
            replace(_create_snapshot(), source_signature=b"short"),
            replace(_create_snapshot(), event_raw_data_sha256="not-a-sha256"),
            replace(_create_snapshot(), virtual_quote_reserves=QuoteBaseUnits(0)),
            replace(_create_snapshot(), real_quote_reserves=QuoteBaseUnits(1)),
            replace(_create_snapshot(), complete=True),
        )

        for snapshot in cases:
            with self.subTest(snapshot=snapshot):
                result = _adapt(snapshot=snapshot)
                self.assertIsInstance(result, AbstainResult)

    def assert_abstains(self, result: object, reason: AbstainReason) -> None:
        self.assertIsInstance(result, AbstainResult)
        if isinstance(result, AbstainResult):
            self.assertEqual(result.reason, reason)
            self.assertEqual(result.as_of_slot, SLOT)


def _adapt(  # noqa: PLR0913
    *,
    snapshot: PumpCreateReserveSnapshot | None = None,
    protocol: PumpProtocolVersionSnapshot | None = None,
    protocol_snapshot: PumpProtocolVersionSnapshot | None | object = "default",
    mint_metadata: PumpCreateMintMetadataProof | None | object = "default",
    create_decoder_version: str | None = PUMP_CREATE_V2_DECODER_VERSION,
    create_idl_hash: str | None = PINNED_PUMP_IDL_SHA256,
) -> PoolReserves | AbstainResult:
    selected_protocol = (
        protocol or _protocol_snapshot()
        if protocol_snapshot == "default"
        else protocol_snapshot
    )
    selected_metadata = (
        _mint_metadata() if mint_metadata == "default" else mint_metadata
    )
    return pump_create_snapshot_to_pool_reserves(
        snapshot or _create_snapshot(),
        protocol_snapshot=cast(
            "PumpProtocolVersionSnapshot | None",
            selected_protocol,
        ),
        mint_metadata=cast("PumpCreateMintMetadataProof | None", selected_metadata),
        create_decoder_version=create_decoder_version,
        create_idl_hash=create_idl_hash,
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


def _entry_intent() -> ExecutionIntent:
    return ExecutionIntent(
        intent_id="create-state-paper-entry",
        as_of_slot=SLOT,
        market_id=MINT,
        side="buy",
        quote_amount_base_units=QuoteBaseUnits(1_000_000),
        base_amount_base_units=None,
        max_slippage_bps=500,
        reason_codes=("exact_create_state",),
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


if __name__ == "__main__":
    unittest.main()
