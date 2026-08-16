"""Focused regression checks for production-to-case adaptation."""

from __future__ import annotations

import unittest
from dataclasses import replace
from unittest.mock import patch

from rugbot.backtest.dataset import FinalizedTrade
from rugbot.backtest.production_case_adapter import (
    FinalizedLaunchCaseProof,
    ProductionEntryFacts,
    assemble_observation_copy_trade_cases,
    assemble_production_copy_trade_cases,
)
from rugbot.backtest.trajectory_producer import (
    LaunchOutcomeProduction,
    LaunchTrajectoryMetadata,
)
from rugbot.decision.operator_qualification import WalletEntityEvidence
from rugbot.domain.amounts import QuoteBaseUnits, Slot, TokenBaseUnits
from rugbot.domain.decisions import AbstainReason, AbstainResult
from rugbot.domain.launches import LaunchCreatedV2
from rugbot.domain.trades import TradeSide
from rugbot.models.adverse_event import AdverseEventDetection
from rugbot.models.outcome_labels import (
    HorizonOutcomeLabel,
    LaunchOutcomeLabels,
    OutcomeObservationPoint,
)


class ProductionCaseAdapterTests(unittest.TestCase):
    """Check pure conversion, delegation, and fail-closed boundaries."""

    def test_observation_proof_handoff_produces_outcomes_before_case_assembly(
        self,
    ) -> None:
        production = _production("launch-a", "mint-a", 10, 15, 14)
        proof = FinalizedLaunchCaseProof(
            launch=production.launch,
            points=(),
            outcome_config=object(),  # type: ignore[arg-type]
            adverse_config=object(),  # type: ignore[arg-type]
            entry_facts=_entry("launch-a", 10),
        )
        with (
            patch(
                "rugbot.backtest.production_case_adapter.build_launch_outcome",
                return_value=production,
            ) as producer,
            patch(
                "rugbot.backtest.production_case_adapter.assemble_production_copy_trade_cases",
                return_value=(),
            ) as assembler,
        ):
            result = assemble_observation_copy_trade_cases(
                launches=(_launch("launch-a", "mint-a", 10),),
                fills=(_fill("launch-a", "mint-a", "wallet-a", 10),),
                entity_evidence=(_entity("launch-a", "wallet-a", 9),),
                observations=(),
                proofs=(proof,),
                as_of_slot=Slot(15),
                entity_id="operator-a",
                regime_id="pump-curve",
            )

        self.assertEqual(result, ())
        producer.assert_called_once_with(
            launch=proof.launch,
            points=proof.points,
            outcome_config=proof.outcome_config,
            adverse_config=proof.adverse_config,
        )
        self.assertEqual(
            assembler.call_args.kwargs["productions"],
            (production,),
        )

    def test_observation_proof_handoff_reports_required_evidence(self) -> None:
        result = assemble_observation_copy_trade_cases(
            launches=(),
            fills=(),
            entity_evidence=(),
            observations=(),
            proofs=(),
            as_of_slot=Slot(15),
            entity_id="operator-a",
            regime_id="pump-curve",
        )

        self.assertIsInstance(result, AbstainResult)
        if isinstance(result, AbstainResult):
            self.assertIs(result.reason, AbstainReason.MISSING_FEATURE)
            self.assertIn("protocol/mint/state proofs", result.message)

    def test_converts_artifacts_and_preserves_delegate_arguments(self) -> None:
        production = _production("launch-a", "mint-a", 10, 15, 14)
        sentinel = ()

        with patch(
            "rugbot.backtest.production_case_adapter.assemble_copy_trade_cases",
            return_value=sentinel,
        ) as assembler:
            result = assemble_production_copy_trade_cases(
                launches=(_launch("launch-a", "mint-a", 10),),
                fills=(_fill("launch-a", "mint-a", "wallet-a", 10),),
                entity_evidence=(_entity("launch-a", "wallet-a", 9),),
                productions=(production,),
                entry_facts=(_entry("launch-a", 10),),
                as_of_slot=Slot(15),
                entity_id="operator-a",
                regime_id="pump-curve",
            )

        self.assertIs(result, sentinel)
        call = assembler.call_args.kwargs
        self.assertEqual(call["as_of_slot"], Slot(15))
        self.assertEqual(call["entity_id"], "operator-a")
        self.assertEqual(call["regime_id"], "pump-curve")
        self.assertIs(call["outcome_labels"][0], production.labels)
        self.assertEqual(call["trajectories"][0].as_of_slot, Slot(15))
        self.assertEqual(call["trajectories"][0].holding_time_ms, 1_900)
        self.assertEqual(
            call["outcomes"][0].realized_net_pnl_quote_base_units,
            QuoteBaseUnits(25),
        )
        self.assertEqual(call["outcomes"][0].completed_slot, Slot(14))

    def test_assembles_prior_production_as_leakage_safe_history(self) -> None:
        result = assemble_production_copy_trade_cases(
            launches=(
                _launch("launch-a", "mint-a", 10),
                _launch("launch-b", "mint-b", 20),
            ),
            fills=(
                _fill("launch-a", "mint-a", "wallet-a", 10),
                _fill("launch-b", "mint-b", "wallet-b", 20),
            ),
            entity_evidence=(
                _entity("launch-a", "wallet-a", 9),
                _entity("launch-b", "wallet-b", 19),
            ),
            productions=(
                _production("launch-a", "mint-a", 10, 15, 14),
                _production("launch-b", "mint-b", 20, 20, 20),
            ),
            entry_facts=(_entry("launch-a", 10), _entry("launch-b", 20)),
            as_of_slot=Slot(20),
            entity_id="operator-a",
            regime_id="pump-curve",
        )

        self.assertIsInstance(result, tuple)
        if isinstance(result, tuple):
            self.assertEqual(len(result), 1)
            self.assertEqual(result[0].entity_id, "operator-a")
            self.assertEqual(result[0].regime_id, "pump-curve")
            self.assertEqual(result[0].as_of_slot, Slot(20))
            self.assertEqual(result[0].history[0].as_of_slot, Slot(15))
            self.assertEqual(
                result[0].history[0].realized_net_pnl_quote_base_units,
                QuoteBaseUnits(25),
            )

    def test_all_censored_horizons_abstain_before_assembly(self) -> None:
        production = _production("launch-a", "mint-a", 10, 15, 14)
        censored = replace(
            production.labels.horizon_labels[0],
            censored=True,
            full_exit_net_pnl_quote_base_units=None,
        )
        production = replace(
            production,
            labels=replace(production.labels, horizon_labels=(censored,)),
        )

        with patch(
            "rugbot.backtest.production_case_adapter.assemble_copy_trade_cases"
        ) as assembler:
            result = _adapt_one(production, _entry("launch-a", 10))

        self.assertIsInstance(result, AbstainResult)
        if isinstance(result, AbstainResult):
            self.assertIs(result.reason, AbstainReason.MISSING_FEATURE)
        assembler.assert_not_called()

    def test_missing_production_trajectory_abstains(self) -> None:
        production = replace(
            _production("launch-a", "mint-a", 10, 15, 14), trajectory=()
        )

        result = _adapt_one(production, _entry("launch-a", 10))

        self.assertIsInstance(result, AbstainResult)
        if isinstance(result, AbstainResult):
            self.assertIs(result.reason, AbstainReason.MISSING_FEATURE)

    def test_entry_evidence_after_production_boundary_abstains(self) -> None:
        production = _production("launch-a", "mint-a", 10, 15, 14)
        result = _adapt_one(production, replace(_entry("launch-a", 10), as_of_slot=16))

        self.assertIsInstance(result, AbstainResult)
        if isinstance(result, AbstainResult):
            self.assertIs(result.reason, AbstainReason.STALE_STATE)

    def test_unjoined_entry_facts_abstain(self) -> None:
        production = _production("launch-a", "mint-a", 10, 15, 14)
        result = _adapt_one(production, _entry("launch-b", 10))

        self.assertIsInstance(result, AbstainResult)
        if isinstance(result, AbstainResult):
            self.assertIs(result.reason, AbstainReason.MISSING_FEATURE)


def _adapt_one(
    production: LaunchOutcomeProduction, facts: ProductionEntryFacts
) -> object:
    return assemble_production_copy_trade_cases(
        launches=(_launch("launch-a", "mint-a", 10),),
        fills=(_fill("launch-a", "mint-a", "wallet-a", 10),),
        entity_evidence=(_entity("launch-a", "wallet-a", 9),),
        productions=(production,),
        entry_facts=(facts,),
        as_of_slot=Slot(15),
        entity_id="operator-a",
        regime_id="pump-curve",
    )


def _production(
    launch_id: str,
    mint: str,
    launch_slot: int,
    as_of_slot: int,
    completed_slot: int,
) -> LaunchOutcomeProduction:
    points = (
        _point(launch_id, as_of_slot, launch_slot, 100, 0),
        _point(launch_id, as_of_slot, completed_slot, 2_000, 1),
    )
    horizon = HorizonOutcomeLabel(
        as_of_slot=Slot(as_of_slot),
        launch_id=launch_id,
        token_mint=mint,
        horizon_ms=2_000,
        censored=False,
        last_observed_slot=Slot(completed_slot),
        last_observed_elapsed_ms=2_000,
        adverse_event_observed=False,
        curve_completed=False,
        migration_observed=False,
        drawdown_ppm=0,
        recovery_ppm=0,
        full_exit_net_pnl_quote_base_units=25,
        labeler_version="labels",
        evidence_ids=tuple(
            identifier for point in points for identifier in point.evidence_ids
        ),
    )
    labels = LaunchOutcomeLabels(
        as_of_slot=Slot(as_of_slot),
        launch_id=launch_id,
        token_mint=mint,
        labeler_version="labels",
        first_material_adverse_event_slot=None,
        first_material_adverse_event_elapsed_ms=None,
        max_executable_full_position_net_profit_before_adverse_event=30,
        horizon_labels=(horizon,),
        source_point_count=len(points),
        evidence_ids=horizon.evidence_ids,
        reason_codes=("built",),
    )
    launch = LaunchTrajectoryMetadata(
        launch_id=launch_id,
        token_mint=mint,
        launch_slot=Slot(launch_slot),
        launch_timestamp=100,
        full_exit_base_amount_base_units=TokenBaseUnits(100),
        evidence_ids=(f"launch-proof:{launch_id}",),
    )
    detection = AdverseEventDetection(
        as_of_slot=Slot(as_of_slot),
        event=None,
        reason_codes=("no-event",),
    )
    return LaunchOutcomeProduction(
        launch=launch,
        trajectory=points,
        adverse_detection=detection,
        adverse_event=None,
        labels=labels,
        evidence_ids=(launch.evidence_ids[0], *labels.evidence_ids),
    )


def _point(
    launch_id: str, as_of_slot: int, slot: int, elapsed_ms: int, event_index: int
) -> OutcomeObservationPoint:
    return OutcomeObservationPoint(
        as_of_slot=Slot(as_of_slot),
        slot=Slot(slot),
        event_index=event_index,
        elapsed_ms=elapsed_ms,
        price_quote_base_units_per_token_base_unit_ppm=1_000_000,
        full_exit_output_quote_base_units=QuoteBaseUnits(40),
        full_exit_execution_cost_quote_base_units=QuoteBaseUnits(5),
        curve_progress_ppm=100,
        curve_completed=False,
        migration_observed=False,
        evidence_ids=(f"point:{launch_id}:{event_index}",),
    )


def _entry(launch_id: str, as_of_slot: int) -> ProductionEntryFacts:
    return ProductionEntryFacts(
        as_of_slot=Slot(as_of_slot),
        launch_id=launch_id,
        entry_market_cap_quote_base_units=QuoteBaseUnits(100),
        wallet_buy_elapsed_ms=100,
        evidence_ids=(f"entry:{launch_id}",),
    )


def _fill(launch_id: str, mint: str, wallet: str, slot: int) -> FinalizedTrade:
    return FinalizedTrade(
        as_of_slot=Slot(slot),
        launch_id=launch_id,
        token_mint=mint,
        wallet=wallet,
        side=TradeSide.BUY,
        slot=Slot(slot),
        transaction_index=0,
        signature=f"sig:{launch_id}".encode(),
        base_amount_base_units=TokenBaseUnits(100),
        quote_amount_base_units=QuoteBaseUnits(10),
        execution_cost_quote_base_units=QuoteBaseUnits(1),
        evidence_ids=(f"fill:{launch_id}",),
    )


def _entity(launch_id: str, wallet: str, as_of_slot: int) -> WalletEntityEvidence:
    return WalletEntityEvidence(
        as_of_slot=Slot(as_of_slot),
        observed_slot=Slot(as_of_slot),
        entity_id="operator-a",
        launch_id=launch_id,
        wallet=wallet,
        entity_probability_ppm=900_000,
        evidence_ids=(f"entity:{launch_id}",),
    )


def _launch(launch_id: str, mint: str, slot: int) -> LaunchCreatedV2:
    accounts = (mint, "curve", "wallet")
    return LaunchCreatedV2(
        as_of_slot=Slot(slot),
        launch_id=launch_id,
        program_id="pump-program",
        program_id_index=0,
        signature=launch_id.encode(),
        instruction_name="create_v2",
        creation_instruction_type="create_v2",
        account_indices=(0, 1, 2),
        account_pubkeys=accounts,
        account_role_proofs=(),
        actor_role_proofs=(),
        required_account_names=(),
        transaction_index=0,
        outer_instruction_index=0,
        inner_instruction_group_index=None,
        inner_instruction_index=None,
        mint_account_index=0,
        mint_pubkey=mint,
        mint_authority_account_index=0,
        bonding_curve_account_index=1,
        bonding_curve_pubkey="curve",
        associated_bonding_curve_account_index=1,
        global_account_index=1,
        user_account_index=2,
        user_pubkey="wallet",
        creator_pubkey="creator",
        fee_payer_account_index=None,
        fee_payer_pubkey=None,
        first_buyer_account_index=None,
        first_buyer_pubkey=None,
        system_program_account_index=1,
        token_program_account_index=1,
        base_token_program_pubkey="token-program",  # noqa: S106
        associated_token_program_account_index=1,
        mayhem_program_account_index=1,
        global_params_account_index=1,
        quote_vault_account_index=1,
        quote_asset="SOL",
        quote_mint_pubkey="So11111111111111111111111111111111111111112",
        quote_token_program_pubkey="system",  # noqa: S106
        mayhem_state_account_index=1,
        mayhem_token_vault_account_index=1,
        event_authority_account_index=1,
        name=launch_id,
        symbol="T",
        uri="uri",
        is_mayhem_mode=False,
        is_cashback_enabled=False,
        transaction_slot_account_state_available=True,
        missing_evidence=(),
        decoder_version="decoder",
        idl_hash="idl",
    )


if __name__ == "__main__":
    unittest.main()
