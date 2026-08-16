"""Focused checks for the pure finalized trajectory producer."""

from __future__ import annotations

import ast
import unittest
from dataclasses import replace
from pathlib import Path
from uuid import UUID

from rugbot.backtest.finalized_trade_builder import PumpTradeEventProof
from rugbot.backtest.trajectory_producer import (
    FinalizedPumpTradePoint,
    LaunchOutcomeProduction,
    LaunchTrajectoryMetadata,
    build_launch_outcome,
)
from rugbot.domain.amounts import QuoteBaseUnits, Slot, TokenBaseUnits
from rugbot.domain.decisions import AbstainReason, AbstainResult
from rugbot.domain.fees import FeeConfig
from rugbot.domain.observations import RawChainObservation
from rugbot.models.adverse_event import AdverseEventDetectionConfig
from rugbot.models.outcome_labels import OutcomeLabelConfig
from rugbot.protocol.pump.bonding_curve_account import (
    PINNED_PUMP_IDL_SHA256,
    PUMP_PROGRAM_ID,
)
from rugbot.protocol.pump.create_event_decoder import SOL_PUBKEY
from rugbot.protocol.pump.create_state_adapter import PumpCreateMintMetadataProof
from rugbot.protocol.pump.version_registry import PumpProtocolVersionSnapshot

MODULE = Path("src/rugbot/backtest/trajectory_producer.py")
MINT = "m" * 44
OTHER_MINT = "o" * 44
WALLET = "w" * 44


class TrajectoryProducerTests(unittest.TestCase):
    """Verify composition and fail-closed point-in-time joins."""

    def test_builds_trajectory_detects_collapse_and_labels_launch(self) -> None:
        result = build_launch_outcome(
            launch=_launch(),
            points=(_point(2, 0, 456), _point(3, 1, 900), _point(4, 2, 400)),
            outcome_config=_outcome_config(),
            adverse_config=_adverse_config(),
        )

        self.assertIsInstance(result, LaunchOutcomeProduction)
        if isinstance(result, LaunchOutcomeProduction):
            self.assertEqual(len(result.trajectory), 3)
            self.assertEqual(
                tuple(point.event_index for point in result.trajectory),
                (0, 1, 2),
            )
            self.assertIsNotNone(result.adverse_event)
            self.assertEqual(result.labels.source_point_count, 3)
            self.assertEqual(result.labels.first_material_adverse_event_slot, Slot(4))
            self.assertIn("launch-proof", result.evidence_ids)

    def test_accepts_multiple_events_in_one_slot_by_event_index(self) -> None:
        result = build_launch_outcome(
            launch=_launch(),
            points=(_point(2, 0, 456), _point(2, 1, 900), _point(3, 2, 400)),
            outcome_config=_outcome_config(),
            adverse_config=_adverse_config(),
        )

        self.assertIsInstance(result, LaunchOutcomeProduction)
        if isinstance(result, LaunchOutcomeProduction):
            self.assertEqual(
                tuple((point.slot, point.event_index) for point in result.trajectory),
                ((Slot(2), 0), (Slot(2), 1), (Slot(3), 2)),
            )

    def test_cross_mint_point_abstains(self) -> None:
        result = build_launch_outcome(
            launch=_launch(),
            points=(_point(2, 0, 456, mint=OTHER_MINT),),
            outcome_config=_outcome_config(),
            adverse_config=_adverse_config(),
        )

        self._assert_abstains(result, AbstainReason.STALE_STATE)

    def test_missing_point_protocol_proof_abstains(self) -> None:
        result = build_launch_outcome(
            launch=_launch(),
            points=(replace(_point(2, 0, 456), protocol_snapshot=None),),
            outcome_config=_outcome_config(),
            adverse_config=_adverse_config(),
        )

        self._assert_abstains(result, AbstainReason.UNKNOWN_FEE_CONFIG)

    def test_future_point_abstains_before_quote(self) -> None:
        result = build_launch_outcome(
            launch=_launch(),
            points=(_point(11, 0, 456),),
            outcome_config=_outcome_config(),
            adverse_config=_adverse_config(),
        )

        self._assert_abstains(result, AbstainReason.STALE_STATE)

    def test_duplicate_evidence_abstains(self) -> None:
        result = build_launch_outcome(
            launch=_launch(),
            points=(
                _point(2, 0, 456, evidence_ids=("same-proof",)),
                _point(3, 1, 900, evidence_ids=("same-proof",)),
            ),
            outcome_config=_outcome_config(),
            adverse_config=_adverse_config(),
        )

        self._assert_abstains(result, AbstainReason.UNSUPPORTED_PROTOCOL_STATE)

    def test_module_is_pure_and_integer_only(self) -> None:
        source = MODULE.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(MODULE))
        imported = {
            node.module
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module is not None
        }
        forbidden = (
            "requests",
            "aiohttp",
            "httpx",
            "sqlite",
            "psycopg",
            "rugbot.ingest",
            "rugbot.storage",
            "rugbot.execution",
            "solana",
            "solders",
        )
        self.assertEqual(
            [name for name in imported if name.startswith(forbidden)],
            [],
        )
        self.assertNotIn("float(", source)

    def _assert_abstains(self, result: object, reason: AbstainReason) -> None:
        self.assertIsInstance(result, AbstainResult)
        if isinstance(result, AbstainResult):
            self.assertIs(result.reason, reason)


def _launch() -> LaunchTrajectoryMetadata:
    return LaunchTrajectoryMetadata(
        launch_id="launch-1",
        token_mint=MINT,
        launch_slot=Slot(1),
        launch_timestamp=100,
        full_exit_base_amount_base_units=TokenBaseUnits(123),
        evidence_ids=("launch-proof",),
    )


def _point(  # noqa: PLR0913
    slot: int,
    event_index: int,
    sol_amount: int,
    *,
    mint: str = MINT,
    protocol_snapshot: PumpProtocolVersionSnapshot | None = None,
    evidence_ids: tuple[str, ...] | None = None,
) -> FinalizedPumpTradePoint:
    return FinalizedPumpTradePoint(
        observation=_observation(slot),
        event=PumpTradeEventProof(
            mint=mint,
            user=WALLET,
            sol_amount_base_units=sol_amount,
            token_amount_base_units=123,
            is_buy=True,
            instruction_name="buy",
            timestamp=100 + (slot - 2),
            virtual_sol_reserves_base_units=1_000_000_000,
            virtual_token_reserves_base_units=1_000_000_000,
            real_sol_reserves_base_units=1_000_000_000,
            real_token_reserves_base_units=1_000_000_000,
            protocol_fee_base_units=1,
            creator_fee_base_units=1,
            protocol_fee_basis_points=100,
            creator_fee_basis_points=100,
            cashback_base_units=0,
            encoded_event=f"event-{slot}-{event_index}".encode("ascii"),
        ),
        event_index=event_index,
        protocol_snapshot=protocol_snapshot or _protocol_snapshot(slot),
        mint_metadata=_mint_metadata(slot, mint),
        curve_completed=False,
        migration_observed=False,
        evidence_ids=evidence_ids or (f"trade-proof-{slot}-{event_index}",),
    )


def _observation(slot: int) -> RawChainObservation:
    return RawChainObservation(
        raw_id=UUID(f"00000000-0000-0000-0000-{slot:012d}"),
        source_id="fixture",
        observer_id="fixture",
        boot_id=UUID("00000000-0000-0000-0000-000000000002"),
        receive_sequence=slot,
        slot=slot,
        parent_slot=slot - 1,
        blockhash=None,
        signature=bytes([slot]) * 64,
        transaction_index=slot,
        outer_instruction_index=0,
        inner_instruction_group_index=None,
        inner_instruction_index=None,
        stack_height=None,
        event_ordinal=None,
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


def _mint_metadata(slot: int, mint: str) -> PumpCreateMintMetadataProof:
    return PumpCreateMintMetadataProof(
        as_of_slot=Slot(slot),
        base_mint_pubkey=mint,
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


def _outcome_config() -> OutcomeLabelConfig:
    return OutcomeLabelConfig(
        as_of_slot=Slot(10),
        launch_id="launch-1",
        token_mint=MINT,
        labeler_version="outcome-labeler",
        horizon_ms=(1_000, 2_000),
        entry_total_cost_quote_base_units=QuoteBaseUnits(10),
    )


def _adverse_config() -> AdverseEventDetectionConfig:
    return AdverseEventDetectionConfig(
        as_of_slot=Slot(10),
        token_mint=MINT,
        detector_version="adverse-detector",
        min_peak_price_ppm=1_000_000,
        min_drawdown_ppm=500_000,
        recovery_window_ms=2_000,
    )


if __name__ == "__main__":
    unittest.main()
