"""Focused checks for the bounded copy-trade case assembler."""

import unittest
from dataclasses import replace

from rugbot.backtest.case_builder import (
    CopyTradeTrajectoryArtifact,
    assemble_copy_trade_cases,
)
from rugbot.backtest.dataset import FinalizedTrade
from rugbot.decision.operator_qualification import (
    CompletedLaunchOutcome,
    WalletEntityEvidence,
)
from rugbot.domain.amounts import QuoteBaseUnits, Slot, TokenBaseUnits
from rugbot.domain.decisions import AbstainReason, AbstainResult
from rugbot.domain.launches import LaunchCreatedV2
from rugbot.domain.trades import TradeSide
from rugbot.models.outcome_labels import (
    LaunchOutcomeLabels,
    OutcomeObservationPoint,
)


class CaseBuilderTests(unittest.TestCase):
    """Check joins and point-in-time rejection without invoking RPC."""

    def test_assembles_target_and_prior_history_at_target_boundary(self) -> None:
        result = assemble_copy_trade_cases(**_inputs())

        self.assertIsInstance(result, tuple)
        if isinstance(result, tuple):
            self.assertEqual(len(result), 1)
            self.assertEqual(result[0].history[0].launch_id, "launch-a")
            self.assertEqual(result[0].history[0].as_of_slot, 15)
            self.assertEqual(result[0].wallet, "wallet-b")

    def test_missing_target_label_abstains(self) -> None:
        inputs = _inputs()
        inputs["outcome_labels"] = (inputs["outcome_labels"][0],)

        result = assemble_copy_trade_cases(**inputs)

        self.assertIsInstance(result, AbstainResult)
        if isinstance(result, AbstainResult):
            self.assertIs(result.reason, AbstainReason.MISSING_FEATURE)

    def test_future_entity_evidence_abstains(self) -> None:
        inputs = _inputs()
        evidence = inputs["entity_evidence"]
        inputs["entity_evidence"] = (
            evidence[0],
            replace(evidence[1], as_of_slot=Slot(21)),
        )

        result = assemble_copy_trade_cases(**inputs)

        self.assertIsInstance(result, AbstainResult)
        if isinstance(result, AbstainResult):
            self.assertIs(result.reason, AbstainReason.MISSING_FEATURE)

    def test_ambiguous_best_entity_wallet_abstains(self) -> None:
        inputs = _inputs()
        inputs["entity_evidence"] = (
            *inputs["entity_evidence"],
            replace(inputs["entity_evidence"][1], wallet="wallet-c"),
        )

        result = assemble_copy_trade_cases(**inputs)

        self.assertIsInstance(result, AbstainResult)
        if isinstance(result, AbstainResult):
            self.assertIs(result.reason, AbstainReason.UNSUPPORTED_PROTOCOL_STATE)


def _inputs() -> dict[str, object]:
    launches = (_launch("launch-a", "mint-a", 10), _launch("launch-b", "mint-b", 20))
    trajectories = (
        _trajectory("launch-a", "mint-a", 15, 10),
        _trajectory("launch-b", "mint-b", 20, 20),
    )
    labels = (_labels("launch-a", "mint-a", 15), _labels("launch-b", "mint-b", 20))
    return {
        "launches": launches,
        "fills": (
            _fill("launch-a", "mint-a", "wallet-a", 10),
            _fill("launch-b", "mint-b", "wallet-b", 20),
        ),
        "entity_evidence": (
            _entity("launch-a", "wallet-a", 9),
            _entity("launch-b", "wallet-b", 19),
        ),
        "trajectories": trajectories,
        "outcomes": (_outcome("launch-a", 10, 14, 15),),
        "outcome_labels": labels,
        "as_of_slot": Slot(20),
        "entity_id": "operator-a",
        "regime_id": "pump-bonding-curve",
    }


def _launch(launch_id: str, mint: str, slot: int) -> LaunchCreatedV2:
    accounts = (mint, "account", "wallet")
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
        bonding_curve_pubkey="account",
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


def _fill(launch_id: str, mint: str, wallet: str, slot: int) -> FinalizedTrade:
    return FinalizedTrade(
        as_of_slot=Slot(slot),
        launch_id=launch_id,
        token_mint=mint,
        wallet=wallet,
        side=TradeSide.BUY,
        slot=Slot(slot),
        transaction_index=0,
        signature=f"sig-{launch_id}".encode(),
        base_amount_base_units=TokenBaseUnits(100),
        quote_amount_base_units=QuoteBaseUnits(10),
        execution_cost_quote_base_units=QuoteBaseUnits(1),
        evidence_ids=(f"fill:{launch_id}",),
    )


def _entity(launch_id: str, wallet: str, as_of: int) -> WalletEntityEvidence:
    return WalletEntityEvidence(
        as_of_slot=Slot(as_of),
        observed_slot=Slot(as_of),
        entity_id="operator-a",
        launch_id=launch_id,
        wallet=wallet,
        entity_probability_ppm=900_000,
        evidence_ids=(f"entity:{launch_id}",),
    )


def _trajectory(
    launch_id: str, mint: str, as_of: int, slot: int
) -> CopyTradeTrajectoryArtifact:
    point = OutcomeObservationPoint(
        as_of_slot=Slot(as_of),
        slot=Slot(slot),
        event_index=0,
        elapsed_ms=1,
        price_quote_base_units_per_token_base_unit_ppm=1_000_000,
        full_exit_output_quote_base_units=QuoteBaseUnits(20),
        full_exit_execution_cost_quote_base_units=QuoteBaseUnits(1),
        curve_progress_ppm=100,
        curve_completed=False,
        migration_observed=False,
        evidence_ids=(f"point:{launch_id}",),
    )
    return CopyTradeTrajectoryArtifact(
        as_of_slot=Slot(as_of),
        launch_id=launch_id,
        token_mint=mint,
        launch_time_ms=slot * 1_000,
        entry_market_cap_quote_base_units=QuoteBaseUnits(100),
        wallet_buy_elapsed_ms=1,
        holding_time_ms=2,
        trajectory=(point,),
        evidence_ids=(f"trajectory:{launch_id}",),
    )


def _labels(launch_id: str, mint: str, as_of: int) -> LaunchOutcomeLabels:
    return LaunchOutcomeLabels(
        as_of_slot=Slot(as_of),
        launch_id=launch_id,
        token_mint=mint,
        labeler_version="labels",
        first_material_adverse_event_slot=None,
        first_material_adverse_event_elapsed_ms=None,
        max_executable_full_position_net_profit_before_adverse_event=QuoteBaseUnits(10),
        horizon_labels=(),
        source_point_count=1,
        evidence_ids=(f"labels:{launch_id}",),
        reason_codes=("built",),
    )


def _outcome(
    launch_id: str, launch_slot: int, completed_slot: int, as_of: int
) -> CompletedLaunchOutcome:
    return CompletedLaunchOutcome(
        as_of_slot=Slot(as_of),
        entity_id="operator-a",
        launch_id=launch_id,
        launch_slot=Slot(launch_slot),
        completed_slot=Slot(completed_slot),
        completed=True,
        realized_net_pnl_quote_base_units=QuoteBaseUnits(5),
        peak_net_pnl_quote_base_units=QuoteBaseUnits(10),
        adverse_event_observed=True,
        evidence_ids=(f"outcome:{launch_id}",),
    )


if __name__ == "__main__":
    unittest.main()
