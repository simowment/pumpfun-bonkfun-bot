"""Focused regression checks for the pure TradeEvent trajectory producer."""

from __future__ import annotations

import unittest
from uuid import UUID

from rugbot.backtest.finalized_trade_builder import PumpTradeEventProof
from rugbot.backtest.trade_event_trajectory import (
    TradeEventTrajectoryMetadataProof,
    TradeEventTrajectorySource,
    build_trade_event_trajectory,
    build_trade_event_trajectory_point,
)
from rugbot.domain.amounts import QuoteBaseUnits, Slot, TokenBaseUnits
from rugbot.domain.decisions import AbstainReason, AbstainResult
from rugbot.domain.fees import FeeConfig
from rugbot.domain.observations import RawChainObservation
from rugbot.protocol.pump.bonding_curve_account import (
    PINNED_PUMP_IDL_SHA256,
    PUMP_PROGRAM_ID,
)
from rugbot.protocol.pump.create_event_decoder import SOL_PUBKEY
from rugbot.protocol.pump.create_state_adapter import PumpCreateMintMetadataProof
from rugbot.protocol.pump.version_registry import PumpProtocolVersionSnapshot

MINT = "m" * 44
WALLET = "w" * 44
SIGNATURE = b"s" * 64


class TradeEventTrajectoryTests(unittest.TestCase):
    """Check that trajectory construction is proof-backed and fail-closed."""

    def test_builds_market_point_and_full_exit_quote(self) -> None:
        result = build_trade_event_trajectory_point(
            source=_source(slot=7, event_index=0),
            as_of_slot=Slot(10),
        )

        self.assertFalse(isinstance(result, AbstainResult))
        if isinstance(result, AbstainResult):
            self.fail(result.message)
        self.assertEqual(result.market_state.slot, Slot(7))
        self.assertEqual(result.market_state.elapsed_ms, 0)
        self.assertEqual(
            result.market_state.price_quote_base_units_per_token_base_unit_ppm,
            456 * 1_000_000 // 123,
        )
        self.assertEqual(
            result.market_state.real_quote_reserves_base_units,
            QuoteBaseUnits(1_000_000_000),
        )
        self.assertGreater(result.full_exit_quote.output_amount_base_units, 0)
        self.assertIsInstance(result.full_exit_quote.input_amount_base_units, int)

    def test_trajectory_orders_by_slot_and_event_index(self) -> None:
        result = build_trade_event_trajectory(
            sources=(
                _source(slot=7, event_index=0),
                _source(slot=8, event_index=1, timestamp=101),
            ),
            as_of_slot=Slot(10),
        )

        self.assertIsInstance(result, tuple)
        if isinstance(result, tuple):
            self.assertEqual(
                tuple(point.market_state.slot for point in result),
                (Slot(7), Slot(8)),
            )

    def test_missing_fee_proof_abstains(self) -> None:
        source = _source(slot=7, event_index=0)
        source = TradeEventTrajectorySource(
            observation=source.observation,
            event=source.event,
            metadata=TradeEventTrajectoryMetadataProof(
                as_of_slot=Slot(7),
                event_index=0,
                trajectory_start_timestamp=100,
                curve_completed=False,
                migration_observed=False,
                full_exit_base_amount_base_units=TokenBaseUnits(123),
                protocol_snapshot=None,
                mint_metadata=_metadata(7),
                evidence_ids=("event",),
            ),
        )

        result = build_trade_event_trajectory_point(
            source=source,
            as_of_slot=Slot(10),
        )

        self._assert_abstains(result, AbstainReason.UNKNOWN_FEE_CONFIG)

    def test_missing_or_zero_reserve_state_abstains(self) -> None:
        source = _source(slot=7, event_index=0, real_sol_reserves=0)

        result = build_trade_event_trajectory_point(
            source=source,
            as_of_slot=Slot(10),
        )

        self._assert_abstains(result, AbstainReason.UNKNOWN_PROTOCOL_STATE)

    def test_missing_mint_metadata_abstains(self) -> None:
        source = _source(slot=7, event_index=0, missing_mint_metadata=True)

        result = build_trade_event_trajectory_point(
            source=source,
            as_of_slot=Slot(10),
        )

        self._assert_abstains(result, AbstainReason.MISSING_FEATURE)

    def _assert_abstains(self, result: object, reason: AbstainReason) -> None:
        self.assertIsInstance(result, AbstainResult)
        if isinstance(result, AbstainResult):
            self.assertIs(result.reason, reason)


def _source(
    *,
    slot: int,
    event_index: int,
    timestamp: int = 100,
    real_sol_reserves: int = 1_000_000_000,
    missing_mint_metadata: bool = False,
) -> TradeEventTrajectorySource:
    observation = RawChainObservation(
        raw_id=UUID("00000000-0000-0000-0000-000000000001"),
        source_id="fixture",
        observer_id="fixture",
        boot_id=UUID("00000000-0000-0000-0000-000000000002"),
        receive_sequence=slot,
        slot=slot,
        parent_slot=slot - 1,
        blockhash=None,
        signature=SIGNATURE,
        transaction_index=0,
        outer_instruction_index=0,
        inner_instruction_group_index=None,
        inner_instruction_index=None,
        stack_height=None,
        event_ordinal=event_index,
        commitment="finalized",
        canonical_status="canonical",
        received_wall_ns=slot,
        received_monotonic_ns=slot,
        program_id=PUMP_PROGRAM_ID.encode("ascii"),
        account_pubkey=None,
        account_owner_program_id=None,
        raw_transaction=None,
        raw_transaction_format=None,
        raw_account_data=None,
        account_write_version=None,
        source_update_kind="transaction",
        raw_source_status=None,
        raw_source_payload=b"finalized",
        decoder_name=None,
        decoder_version=None,
        idl_hash=None,
    )
    selected_metadata = None if missing_mint_metadata else _metadata(slot)
    event = PumpTradeEventProof(
        mint=MINT,
        user=WALLET,
        sol_amount_base_units=456,
        token_amount_base_units=123,
        is_buy=True,
        instruction_name="buy",
        timestamp=timestamp,
        virtual_sol_reserves_base_units=1_000_000_000,
        virtual_token_reserves_base_units=1_000_000_000,
        real_sol_reserves_base_units=real_sol_reserves,
        real_token_reserves_base_units=1_000_000_000,
        protocol_fee_base_units=1,
        creator_fee_base_units=1,
        cashback_base_units=0,
        encoded_event=b"event",
    )
    return TradeEventTrajectorySource(
        observation=observation,
        event=event,
        metadata=TradeEventTrajectoryMetadataProof(
            as_of_slot=Slot(slot),
            event_index=event_index,
            trajectory_start_timestamp=100,
            curve_completed=False,
            migration_observed=False,
            full_exit_base_amount_base_units=TokenBaseUnits(123),
            protocol_snapshot=_protocol_snapshot(slot),
            mint_metadata=selected_metadata,
            evidence_ids=(f"event-{slot}",),
        ),
    )


def _metadata(slot: int) -> PumpCreateMintMetadataProof:
    return PumpCreateMintMetadataProof(
        as_of_slot=Slot(slot),
        base_mint_pubkey=MINT,
        quote_mint_pubkey=SOL_PUBKEY,
        base_decimals=6,
        quote_decimals=9,
        source_artifact="finalized-mint-account",
    )


def _protocol_snapshot(slot: int) -> PumpProtocolVersionSnapshot:
    fee = FeeConfig(
        version="pump-fee",
        protocol_fee_bps=100,
        creator_fee_bps=100,
        is_known=True,
        program_config_version="pump-global-v1",
        valid_from_slot=Slot(0),
        source_artifact_version="fee-registry",
    )
    return PumpProtocolVersionSnapshot(
        as_of_slot=Slot(slot),
        program_id=PUMP_PROGRAM_ID,
        idl_hash=PINNED_PUMP_IDL_SHA256,
        global_config_hash="global-config",
        program_config_version="pump-global-v1",
        fee_config=fee,
        program_config_source_artifact_version="program-registry",
        fee_source_artifact_version="fee-registry",
        registry_version="pump-version-registry-v1",
    )


if __name__ == "__main__":
    unittest.main()
