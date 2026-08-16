"""Tests for the known-wallet block-0/1 watch decision."""

import asyncio
import json
import unittest
from dataclasses import replace
from pathlib import Path
from typing import cast
from uuid import uuid4

from rugbot.decision.consolidation_protection import ConsolidationSignal
from rugbot.decision.operator_qualification import (
    OperatorQualification,
    QualificationStatus,
    WalletEntityEvidence,
)
from rugbot.decision.playbook_rules import EntryRuleInput
from rugbot.decision.volume_sizing import VolumeSizingRequest
from rugbot.domain.amounts import Slot, TokenBaseUnits
from rugbot.domain.decisions import AbstainReason, AbstainResult
from rugbot.domain.launches import LaunchCreatedV2
from rugbot.domain.observations import RawChainObservation
from rugbot.execution.observe import ObserveExecutionPort
from rugbot.execution.ports import (
    ExecutionIntent,
    ExecutionMode,
    ExecutionReceipt,
)
from rugbot.execution.position_runtime import (
    CalibratedExitEvidence,
    PositionMarketEvidence,
)
from rugbot.graph.entity_resolution import AddressRole
from rugbot.graph.rugger_protection import (
    FreshWalletStatus,
    RuggerProtectionSnapshot,
    WalletFreshnessEvidence,
    WalletTransferRange,
)
from rugbot.graph.wallet_behavior import WalletAssetKind
from rugbot.graph.wallet_churn import (
    OPERATOR_WALLET_CHURN_SNAPSHOT_VERSION,
    OperatorWalletChurnSnapshot,
    WalletChurnAddress,
    WalletChurnStatus,
)
from rugbot.ingest.pump_create_fixture_decode import (
    decode_pump_create_v2_fixture_artifact,
)
from rugbot.runtime.config import (
    CoreSniperConfig,
    TrackingMode,
    parse_sniper_config,
)
from rugbot.runtime.watch import (
    WatchSnipeHandler as RuntimeWatchSnipeHandler,
)
from rugbot.runtime.watch import (
    _default_entry_evidence,
    build_watch_snipe_candidate,
)


def WatchSnipeHandler(**kwargs: object) -> RuntimeWatchSnipeHandler:  # noqa: N802
    """Build a handler with the focused test's finalized qualification proof."""

    config = cast("CoreSniperConfig", kwargs["config"])
    kwargs.setdefault("qualification", _qualification(config.target.id))
    kwargs.setdefault("entity_evidence", _entity_evidence(config.target.id))
    return RuntimeWatchSnipeHandler(**kwargs)


FIXTURE = Path(
    "fixtures/finalized_transactions/pump_create_v2/"
    "4HbY43S9UigSctrfxY5nszgf3ozN1f4kPQYaqaFLZaCDhwa55rauuRmhP85u67U7dBvGFwB5C6stmkH2b1TNxgQh.json"
)


class WatchSnipeTests(unittest.IsolatedAsyncioTestCase):
    """Verify the watch candidate and execution-port controls."""

    def test_block_zero_and_one_build_deterministic_buy_candidates(self) -> None:
        launch = _launch(position=0)
        config = _config(launch)
        observation = _observation(launch)

        first = build_watch_snipe_candidate(
            config=config,
            launch=launch,
            observation=observation,
            qualification=_qualification(launch.creator_pubkey),
            entity_evidence=_entity_evidence(launch.creator_pubkey),
        )
        second = build_watch_snipe_candidate(
            config=config,
            launch=replace(launch, transaction_index=1),
            observation=replace(observation, transaction_index=1),
            qualification=_qualification(launch.creator_pubkey),
            entity_evidence=_entity_evidence(launch.creator_pubkey),
        )

        self.assertEqual(first.block_transaction_index, 0)
        self.assertEqual(first.intent.side, "buy")
        self.assertEqual(first.intent.quote_amount_base_units, 1_000_000)
        self.assertFalse(
            "watch-snipe" in first.intent.intent_id and "uuid" in first.intent.intent_id
        )
        self.assertEqual(second.block_transaction_index, 1)

    def test_later_position_is_not_a_late_entry(self) -> None:
        launch = _launch(position=2)
        result = build_watch_snipe_candidate(
            config=_config(launch),
            launch=launch,
            observation=_observation(launch),
            qualification=_qualification(launch.creator_pubkey),
            entity_evidence=_entity_evidence(launch.creator_pubkey),
        )

        self.assertIsNone(result)

    def test_observation_mismatch_abstains(self) -> None:
        launch = _launch(position=0)
        result = build_watch_snipe_candidate(
            config=_config(launch),
            launch=launch,
            observation=replace(_observation(launch), slot=launch.as_of_slot + 1),
        )

        self.assertIsInstance(result, AbstainResult)

    def test_matching_wallet_without_qualification_abstains(self) -> None:
        launch = _launch(position=0)
        result = build_watch_snipe_candidate(
            config=_config(launch),
            launch=launch,
            observation=_observation(launch),
        )

        self.assertIsInstance(result, AbstainResult)
        self.assertEqual(result.reason, AbstainReason.MISSING_FEATURE)

    def test_track_buys_abstains_without_finalized_buy_evidence(self) -> None:
        launch = _launch(position=0)
        config = parse_sniper_config(
            f"""target:
  kind: wallet
  id: "{launch.creator_pubkey}"
execution:
  mode: observe
  quote_size_lamports: 1000000
tracking_mode: track_buys
"""
        )

        result = build_watch_snipe_candidate(
            config=config,
            launch=launch,
            observation=_observation(launch),
            qualification=_qualification(launch.creator_pubkey),
            entity_evidence=_entity_evidence(launch.creator_pubkey),
        )

        self.assertIs(config.tracking_mode, TrackingMode.TRACK_BUYS)
        self.assertIsInstance(result, AbstainResult)
        self.assertEqual(result.reason, AbstainReason.UNSUPPORTED_PROTOCOL_STATE)

    async def test_handler_does_not_resolve_launch_for_track_buys(self) -> None:
        launch = _launch(position=0)
        config = parse_sniper_config(
            f"""target:
  kind: wallet
  id: "{launch.creator_pubkey}"
execution:
  mode: observe
  quote_size_lamports: 1000000
tracking_mode: track_buys
"""
        )
        resolved = False

        def resolver(_observation: RawChainObservation) -> LaunchCreatedV2:
            nonlocal resolved
            resolved = True
            return launch

        handler = WatchSnipeHandler(
            config=config,
            resolver=resolver,
            execution_port=ObserveExecutionPort(),
            buy_cooldown_slots=0,
        )

        result = await handler.handle(_observation(launch))

        self.assertIsInstance(result, AbstainResult)
        self.assertEqual(result.reason, AbstainReason.UNSUPPORTED_PROTOCOL_STATE)
        self.assertFalse(resolved)

    def test_creation_tracking_does_not_mark_launch_as_copytrade(self) -> None:
        launch = _launch(position=0)
        evidence = _default_entry_evidence(launch, _observation(launch))

        self.assertFalse(evidence.is_copytrade)

    def test_matching_wallet_without_wallet_entity_binding_abstains(self) -> None:
        launch = _launch(position=0)
        result = build_watch_snipe_candidate(
            config=_config(launch),
            launch=launch,
            observation=_observation(launch),
            qualification=_qualification(launch.creator_pubkey),
            entity_evidence=(
                replace(
                    _entity_evidence(launch.creator_pubkey)[0],
                    wallet="another-wallet",
                ),
            ),
        )

        self.assertIsInstance(result, AbstainResult)
        self.assertEqual(result.reason, AbstainReason.MISSING_FEATURE)

    def test_rotated_creator_requires_point_in_time_fresh_funding(self) -> None:
        launch = _launch(position=0)
        rotated_creator = launch.mint_pubkey
        rotated_launch = replace(launch, creator_pubkey=rotated_creator)
        result = build_watch_snipe_candidate(
            config=_config(launch),
            launch=rotated_launch,
            observation=_observation(rotated_launch),
            qualification=_qualification(launch.creator_pubkey),
            entity_evidence=_entity_evidence(launch.creator_pubkey),
            operator_churn=_rotated_churn(rotated_launch),
            rugger_protection=_rotated_protection(rotated_launch),
        )

        self.assertNotIsInstance(result, AbstainResult)
        self.assertIsNotNone(result)
        self.assertEqual(result.creator_pubkey, rotated_creator)

    def test_rotated_creator_with_unknown_freshness_abstains(self) -> None:
        launch = _launch(position=0)
        rotated_launch = replace(launch, creator_pubkey=launch.mint_pubkey)
        protection = replace(
            _rotated_protection(rotated_launch),
            freshness=(
                replace(
                    _rotated_protection(rotated_launch).freshness[0],
                    status=FreshWalletStatus.UNKNOWN,
                ),
            ),
        )

        result = build_watch_snipe_candidate(
            config=_config(launch),
            launch=rotated_launch,
            observation=_observation(rotated_launch),
            qualification=_qualification(launch.creator_pubkey),
            entity_evidence=_entity_evidence(launch.creator_pubkey),
            operator_churn=_rotated_churn(rotated_launch),
            rugger_protection=protection,
        )

        self.assertIsInstance(result, AbstainResult)
        self.assertEqual(result.reason, AbstainReason.UNKNOWN_PROTOCOL_STATE)

    def test_malformed_protection_snapshot_abstains_before_dereference(self) -> None:
        launch = _launch(position=0)
        rotated_launch = replace(launch, creator_pubkey=launch.mint_pubkey)
        protection = replace(
            _rotated_protection(rotated_launch), freshness=("malformed",)
        )

        result = build_watch_snipe_candidate(
            config=_config(launch),
            launch=rotated_launch,
            observation=_observation(rotated_launch),
            qualification=_qualification(launch.creator_pubkey),
            entity_evidence=_entity_evidence(launch.creator_pubkey),
            operator_churn=_rotated_churn(rotated_launch),
            rugger_protection=protection,
        )

        self.assertIsInstance(result, AbstainResult)
        self.assertEqual(result.reason, AbstainReason.UNKNOWN_PROTOCOL_STATE)

    async def test_observe_handler_records_candidate_without_submission(self) -> None:
        launch = _launch(position=0)
        handler = WatchSnipeHandler(
            config=_config(launch, mode="observe"),
            resolver=lambda _: launch,
            execution_port=ObserveExecutionPort(),
        )

        result = await handler.handle(_observation(launch))

        self.assertIsNone(result)
        self.assertEqual(len(handler.candidates), 1)
        self.assertEqual(len(handler.receipts), 1)
        self.assertFalse(handler.receipts[0].would_submit_transaction)

    async def test_paper_handler_abstains_without_a_simulator(self) -> None:
        launch = _launch(position=0)
        handler = WatchSnipeHandler(
            config=_config(launch),
            resolver=lambda _: launch,
            execution_port=_PaperPortWithoutFill(),
        )

        result = await handler.handle(_observation(launch))

        self.assertIsInstance(result, AbstainResult)
        self.assertEqual(len(handler.candidates), 0)

    async def test_paper_entry_rejects_non_finalized_observation(self) -> None:
        launch = _launch(position=0)
        handler = WatchSnipeHandler(
            config=_config(launch),
            resolver=lambda _: launch,
            execution_port=_FilledPaperPort(),
            volume_sizing_resolver=lambda _launch, observation: _volume_sizing_request(
                observation.slot
            ),
            buy_cooldown_slots=0,
        )

        result = await handler.handle(
            replace(_observation(launch), commitment="confirmed")
        )

        self.assertIsInstance(result, AbstainResult)
        if isinstance(result, AbstainResult):
            self.assertEqual(result.reason, AbstainReason.STALE_STATE)
            self.assertEqual(
                result.message,
                "paper entry requires canonical finalized observation",
            )
        self.assertEqual(handler.candidates, [])

    async def test_async_paper_execution_port_resolver_is_awaited(self) -> None:
        launch = _launch(position=0)
        port = _FilledPaperPort()

        async def resolve_port(*_args: object) -> _FilledPaperPort:
            return port

        handler = WatchSnipeHandler(
            config=_config(launch),
            resolver=lambda _: launch,
            execution_port=_PaperPortWithoutFill(),
            execution_port_resolver=resolve_port,
            volume_sizing_resolver=lambda _launch, observation: _volume_sizing_request(
                observation.slot
            ),
            buy_cooldown_slots=0,
        )

        result = await handler.handle(_observation(launch))

        self.assertIsNone(result)
        self.assertEqual(len(port.intents), 1)

    async def test_default_paper_entry_abstains_without_sizing_evidence(self) -> None:
        launch = _launch(position=0)
        handler = WatchSnipeHandler(
            config=_config(launch),
            resolver=lambda _: launch,
            execution_port=_FilledPaperPort(),
            buy_cooldown_slots=0,
        )

        result = await handler.handle(_observation(launch))

        self.assertIsInstance(result, AbstainResult)
        self.assertEqual(result.reason, AbstainReason.MISSING_FEATURE)
        self.assertEqual(
            result.message,
            "paper entry volume and liquidity evidence is required",
        )
        self.assertEqual(len(handler.candidates), 0)

    async def test_paper_entry_uses_integer_volume_liquidity_aware_size(self) -> None:
        launch = _launch(position=0)
        port = _FilledPaperPort()
        handler = WatchSnipeHandler(
            config=_config(launch),
            resolver=lambda _: launch,
            execution_port=port,
            volume_sizing_resolver=lambda _launch, observation: _volume_sizing_request(
                observation.slot
            ),
            buy_cooldown_slots=0,
        )

        self.assertIsNone(await handler.handle(_observation(launch)))

        self.assertEqual(port.intents[0].quote_amount_base_units, 10_000)
        self.assertIs(type(port.intents[0].quote_amount_base_units), int)

    async def test_paper_handler_advances_position_and_emits_exit(self) -> None:
        launch = _launch(position=0)
        config = parse_sniper_config(
            f'''target:
  kind: wallet
  id: "{launch.creator_pubkey}"
execution:
  mode: paper
  quote_size_lamports: 1000000
rules:
  sell:
    take_profit_levels:
      - trigger_pnl_ppm: 1
        sell_fraction_ppm: 1000000
'''
        )
        port = _FilledPaperPort()
        handler = WatchSnipeHandler(
            config=config,
            resolver=lambda _: launch,
            execution_port=port,
            volume_sizing_resolver=lambda _launch, observation: _volume_sizing_request(
                observation.slot
            ),
            buy_cooldown_slots=0,
        )

        self.assertIsNone(await handler.handle(_observation(launch)))
        result = await handler.handle_position_evidence(
            PositionMarketEvidence(
                as_of_slot=int(launch.as_of_slot) + 1,
                market_id=launch.mint_pubkey,
                current_pnl_ppm=1,
                idle_ms=0,
                executable_exit_capacity_base_units=100,
                calibrated_exit_evidence=CalibratedExitEvidence(
                    as_of_slot=int(launch.as_of_slot),
                    market_id=launch.mint_pubkey,
                    take_profit_pnl_ppm=1,
                ),
            )
        )

        self.assertIsNone(result)
        self.assertEqual(len(port.intents), 2)
        self.assertEqual(port.intents[1].side, "sell")
        self.assertEqual(len(handler.receipts), 2)

    async def test_consolidation_signal_emits_full_paper_exit(self) -> None:
        launch = _launch(position=0)
        config = parse_sniper_config(
            f'''target:
  kind: wallet
  id: "{launch.creator_pubkey}"
execution:
  mode: paper
  quote_size_lamports: 1000000
'''
        )
        port = _FilledPaperPort()
        handler = WatchSnipeHandler(
            config=config,
            resolver=lambda _: launch,
            execution_port=port,
            volume_sizing_resolver=lambda _launch, observation: _volume_sizing_request(
                observation.slot
            ),
            buy_cooldown_slots=0,
        )

        self.assertIsNone(await handler.handle(_observation(launch)))
        result = await handler.handle_position_evidence(
            PositionMarketEvidence(
                as_of_slot=int(launch.as_of_slot) + 1,
                market_id=launch.mint_pubkey,
                current_pnl_ppm=0,
                idle_ms=0,
                executable_exit_capacity_base_units=100,
            ),
            consolidation_signal=ConsolidationSignal(
                as_of_slot=Slot(int(launch.as_of_slot) + 1),
                slot=Slot(int(launch.as_of_slot) + 1),
                transaction_index=0,
                signature=b"consolidation",
                token_mint=launch.mint_pubkey,
                destination_wallet="destination",
                consolidated_base_units=TokenBaseUnits(60),
                consolidated_share_ppm=600_000,
                evidence_ids=("consolidation:1",),
            ),
        )

        self.assertIsNone(result)
        self.assertEqual([intent.side for intent in port.intents], ["buy", "sell"])

    async def test_finalized_observation_progresses_open_paper_position(self) -> None:
        launch = _launch(position=0)
        config = parse_sniper_config(
            f'''target:
  kind: wallet
  id: "{launch.creator_pubkey}"
execution:
  mode: paper
  quote_size_lamports: 1000000
rules:
  sell:
    take_profit_levels:
      - trigger_pnl_ppm: 1
        sell_fraction_ppm: 1000000
'''
        )
        port = _FilledPaperPort()
        handler = WatchSnipeHandler(
            config=config,
            resolver=lambda observation: (
                launch if observation.slot == launch.as_of_slot else None
            ),
            execution_port=port,
            volume_sizing_resolver=lambda _launch, observation: _volume_sizing_request(
                observation.slot
            ),
            position_evidence_resolver=lambda observation, state: (
                PositionMarketEvidence(
                    as_of_slot=observation.slot,
                    market_id=state.market_id,
                    current_pnl_ppm=1,
                    idle_ms=0,
                    executable_exit_capacity_base_units=state.current_position_base_units,
                    calibrated_exit_evidence=CalibratedExitEvidence(
                        as_of_slot=int(launch.as_of_slot),
                        market_id=state.market_id,
                        take_profit_pnl_ppm=1,
                    ),
                )
                if observation.slot > launch.as_of_slot
                else None
            ),
            buy_cooldown_slots=0,
        )

        self.assertIsNone(await handler.handle(_observation(launch)))
        later = replace(
            _observation(launch),
            raw_id=uuid4(),
            receive_sequence=2,
            slot=launch.as_of_slot + 1,
        )

        self.assertIsNone(await handler.handle(later))
        self.assertEqual([intent.side for intent in port.intents], ["buy", "sell"])

    async def test_open_paper_position_abstains_without_finalized_evidence(
        self,
    ) -> None:
        launch = _launch(position=0)
        handler = WatchSnipeHandler(
            config=_config(launch),
            resolver=lambda observation: (
                launch if observation.slot == launch.as_of_slot else None
            ),
            execution_port=_FilledPaperPort(),
            volume_sizing_resolver=lambda _launch, observation: _volume_sizing_request(
                observation.slot
            ),
            buy_cooldown_slots=0,
        )

        self.assertIsNone(await handler.handle(_observation(launch)))
        later = replace(
            _observation(launch),
            raw_id=uuid4(),
            receive_sequence=2,
            slot=launch.as_of_slot + 1,
        )

        result = await handler.handle(later)

        self.assertIsInstance(result, AbstainResult)
        self.assertEqual(result.reason, AbstainReason.MISSING_FEATURE)
        self.assertEqual(
            result.message,
            "finalized position market evidence resolver is required",
        )

    async def test_open_paper_position_rejects_non_finalized_observation(self) -> None:
        launch = _launch(position=0)
        handler = WatchSnipeHandler(
            config=_config(launch),
            resolver=lambda observation: (
                launch if observation.slot == launch.as_of_slot else None
            ),
            execution_port=_FilledPaperPort(),
            volume_sizing_resolver=lambda _launch, observation: _volume_sizing_request(
                observation.slot
            ),
            position_evidence_resolver=lambda observation, state: (
                PositionMarketEvidence(
                    as_of_slot=observation.slot,
                    market_id=state.market_id,
                    current_pnl_ppm=0,
                    idle_ms=0,
                    executable_exit_capacity_base_units=state.current_position_base_units,
                )
            ),
            buy_cooldown_slots=0,
        )

        self.assertIsNone(await handler.handle(_observation(launch)))
        later = replace(
            _observation(launch),
            raw_id=uuid4(),
            receive_sequence=2,
            slot=launch.as_of_slot + 1,
            commitment="confirmed",
        )

        result = await handler.handle(later)

        self.assertIsInstance(result, AbstainResult)
        self.assertEqual(result.reason, AbstainReason.STALE_STATE)

    async def test_watch_applies_configured_entry_rule_and_dip_size(self) -> None:
        launch = _launch(position=0)
        config = parse_sniper_config(
            f"""target:
  kind: wallet
  id: "{launch.creator_pubkey}"
execution:
  mode: observe
  quote_size_lamports: 1000000
rules:
  buy_the_dip:
    levels:
      - drawdown_ppm: 200000
        quote_size_lamports: 500000
"""
        )
        handler = WatchSnipeHandler(
            config=config,
            resolver=lambda _: launch,
            execution_port=ObserveExecutionPort(),
            entry_evidence_resolver=lambda _launch, observation: EntryRuleInput(
                as_of_slot=observation.slot,
                token_mint=launch.mint_pubkey,
                now_ms=0,
                event_time_ms=0,
                is_copytrade=False,
                is_buy_the_dip=True,
                ath_market_cap_quote_base_units=1_000,
                current_market_cap_quote_base_units=700,
            ),
            buy_cooldown_slots=0,
        )

        result = await handler.handle(_observation(launch))

        self.assertIsNone(result)
        self.assertEqual(
            handler.candidates[0].intent.quote_amount_base_units,
            500_000,
        )

    async def test_watch_abstains_when_configured_filter_lacks_evidence(self) -> None:
        launch = _launch(position=0)
        config = parse_sniper_config(
            f"""target:
  kind: wallet
  id: "{launch.creator_pubkey}"
execution:
  mode: observe
  quote_size_lamports: 1000000
rules:
  min_market_cap_quote_base_units: 100
"""
        )
        handler = WatchSnipeHandler(
            config=config,
            resolver=lambda _: launch,
            execution_port=ObserveExecutionPort(),
            buy_cooldown_slots=0,
        )

        result = await handler.handle(_observation(launch))

        self.assertIsInstance(result, AbstainResult)
        self.assertEqual(result.reason, AbstainReason.MISSING_FEATURE)
        self.assertEqual(len(handler.candidates), 0)

    async def test_execution_port_resolver_can_fail_closed_per_observation(
        self,
    ) -> None:
        launch = _launch(position=0)
        handler = WatchSnipeHandler(
            config=_config(launch, mode="observe"),
            resolver=lambda _: launch,
            execution_port=ObserveExecutionPort(),
            execution_port_resolver=lambda *_: AbstainResult(
                reason=AbstainReason.MISSING_FEATURE,
                message="exact paper context unavailable",
                as_of_slot=launch.as_of_slot,
            ),
            buy_cooldown_slots=0,
        )

        result = await handler.handle(_observation(launch))

        self.assertIsInstance(result, AbstainResult)
        self.assertEqual(result.message, "exact paper context unavailable")
        self.assertEqual(len(handler.candidates), 0)

    async def test_root_loss_counter_pauses_all_future_buys(self) -> None:
        launch = _launch(position=0)
        handler = WatchSnipeHandler(
            config=_config(launch, mode="observe"),
            resolver=lambda _: launch,
            execution_port=ObserveExecutionPort(),
            max_consecutive_losses=2,
            buy_cooldown_slots=0,
        )

        self.assertIsNone(
            handler.record_realized_pnl(-1, as_of_slot=int(launch.as_of_slot))
        )
        self.assertIsNone(
            handler.record_realized_pnl(-2, as_of_slot=int(launch.as_of_slot) + 1)
        )
        self.assertEqual(handler.consecutive_losses, 2)
        self.assertTrue(handler.auto_buy_paused)

        result = await handler.handle(_observation(launch))

        self.assertIsInstance(result, AbstainResult)
        self.assertEqual(result.reason, AbstainReason.MISSING_FEATURE)
        self.assertEqual(
            result.message,
            "auto_buy_paused:max_consecutive_losses_reached",
        )
        self.assertEqual(len(handler.candidates), 0)

    async def test_non_loss_resets_counter_but_not_manual_pause(self) -> None:
        launch = _launch(position=0)
        handler = WatchSnipeHandler(
            config=_config(launch, mode="observe"),
            resolver=lambda _: launch,
            execution_port=ObserveExecutionPort(),
            max_consecutive_losses=2,
            buy_cooldown_slots=0,
        )

        handler.record_realized_pnl(-1, as_of_slot=int(launch.as_of_slot))
        handler.record_realized_pnl(-1, as_of_slot=int(launch.as_of_slot) + 1)
        handler.record_realized_pnl(0, as_of_slot=int(launch.as_of_slot) + 2)

        self.assertEqual(handler.consecutive_losses, 0)
        self.assertTrue(handler.auto_buy_paused)
        paused = await handler.handle(_observation(launch))
        self.assertIsInstance(paused, AbstainResult)

        handler.resume_auto_buy()
        self.assertFalse(handler.auto_buy_paused)
        self.assertIsNone(await handler.handle(_observation(launch)))

    async def test_buy_once_abstains_for_the_same_market(self) -> None:
        launch = _launch(position=0)
        handler = WatchSnipeHandler(
            config=_config(launch, mode="observe"),
            resolver=lambda _: launch,
            execution_port=ObserveExecutionPort(),
            buy_cooldown_slots=0,
        )

        self.assertIsNone(await handler.handle(_observation(launch)))
        result = await handler.handle(_observation(launch))

        self.assertIsInstance(result, AbstainResult)
        self.assertEqual(result.reason, AbstainReason.MISSING_FEATURE)
        self.assertEqual(
            result.message,
            "buy_once:market_already_purchased_or_pending",
        )
        self.assertEqual(len(handler.candidates), 1)

    async def test_cooldown_abstains_for_a_different_market(self) -> None:
        first = _launch(position=0)
        second_mint = "11111111111111111111111111111111"
        second = replace(
            first,
            as_of_slot=int(first.as_of_slot) + 1,
            launch_id=second_mint,
            mint_pubkey=second_mint,
            account_role_proofs=_mint_proofs(first, second_mint),
        )
        handler = WatchSnipeHandler(
            config=_config(first, mode="observe"),
            resolver=lambda observation: (
                first if observation.slot == first.as_of_slot else second
            ),
            execution_port=ObserveExecutionPort(),
            buy_cooldown_slots=2,
        )

        self.assertIsNone(await handler.handle(_observation(first)))
        result = await handler.handle(_observation(second))

        self.assertIsInstance(result, AbstainResult)
        self.assertEqual(result.reason, AbstainReason.MISSING_FEATURE)
        self.assertEqual(result.message, "buy_cooldown:wait_1_slots")

    async def test_slot_regression_abstains_even_without_cooldown(self) -> None:
        first = _launch(position=0)
        second_mint = "11111111111111111111111111111111"
        second = replace(
            first,
            as_of_slot=int(first.as_of_slot) - 1,
            launch_id=second_mint,
            mint_pubkey=second_mint,
            account_role_proofs=_mint_proofs(first, second_mint),
        )
        handler = WatchSnipeHandler(
            config=_config(first, mode="observe"),
            resolver=lambda observation: (
                first if observation.slot == first.as_of_slot else second
            ),
            execution_port=ObserveExecutionPort(),
            buy_cooldown_slots=0,
        )

        self.assertIsNone(await handler.handle(_observation(first)))
        result = await handler.handle(_observation(second))

        self.assertIsInstance(result, AbstainResult)
        self.assertEqual(result.reason, AbstainReason.STALE_STATE)
        self.assertEqual(result.message, "buy_cooldown:observation_slot_regressed")

    async def test_pending_buy_reservation_blocks_concurrent_duplicate(self) -> None:
        launch = _launch(position=0)
        port = _BlockingObservePort()
        handler = WatchSnipeHandler(
            config=_config(launch, mode="observe"),
            resolver=lambda _: launch,
            execution_port=port,
            buy_cooldown_slots=0,
        )

        first_task = asyncio.create_task(handler.handle(_observation(launch)))
        await port.started.wait()
        duplicate = await handler.handle(_observation(launch))
        port.release.set()

        self.assertIsInstance(duplicate, AbstainResult)
        self.assertEqual(duplicate.reason, AbstainReason.MISSING_FEATURE)
        self.assertEqual(
            duplicate.message,
            "buy_once:market_already_purchased_or_pending",
        )
        self.assertIsNone(await first_task)
        self.assertEqual(len(handler.candidates), 1)

    async def test_failed_execution_releases_buy_once_reservation(self) -> None:
        launch = _launch(position=0)
        handler = WatchSnipeHandler(
            config=_config(launch, mode="observe"),
            resolver=lambda _: launch,
            execution_port=_FailOnceThenObservePort(),
            buy_cooldown_slots=0,
        )

        first = await handler.handle(_observation(launch))
        second = await handler.handle(_observation(launch))

        self.assertIsInstance(first, AbstainResult)
        self.assertEqual(second, None)
        self.assertEqual(len(handler.candidates), 1)

    def test_invalid_realized_pnl_is_an_explicit_abstention(self) -> None:
        launch = _launch(position=0)
        handler = WatchSnipeHandler(
            config=_config(launch, mode="observe"),
            resolver=lambda _: launch,
            execution_port=ObserveExecutionPort(),
        )

        result = handler.record_realized_pnl(1.0, as_of_slot=1)

        self.assertIsInstance(result, AbstainResult)
        self.assertEqual(result.reason, AbstainReason.MISSING_FEATURE)
        self.assertEqual(
            result.message,
            "invalid_realized_pnl:expected_integer_lamports",
        )


class _PaperPortWithoutFill:
    async def submit(self, intent: ExecutionIntent) -> ExecutionReceipt:
        return ExecutionReceipt(
            mode=ExecutionMode.PAPER,
            intent_id=intent.intent_id,
            as_of_slot=intent.as_of_slot,
            accepted=False,
            would_submit_transaction=False,
            signature=None,
            simulated_output_base_units=None,
            estimated_fee_lamports=0,
            message="paper simulator is not configured",
        )


class _FilledPaperPort:
    def __init__(self) -> None:
        self.intents: list[ExecutionIntent] = []

    async def submit(self, intent: ExecutionIntent) -> ExecutionReceipt:
        self.intents.append(intent)
        return ExecutionReceipt(
            mode=ExecutionMode.PAPER,
            intent_id=intent.intent_id,
            as_of_slot=intent.as_of_slot,
            accepted=True,
            would_submit_transaction=False,
            signature=None,
            simulated_output_base_units=100,
            estimated_fee_lamports=1,
            message="paper fill",
        )


class _BlockingObservePort:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self._delegate = ObserveExecutionPort()

    async def submit(self, intent: ExecutionIntent) -> ExecutionReceipt:
        self.started.set()
        await self.release.wait()
        return await self._delegate.submit(intent)


class _FailOnceThenObservePort:
    def __init__(self) -> None:
        self._failed = False
        self._delegate = ObserveExecutionPort()

    async def submit(self, intent: ExecutionIntent) -> ExecutionReceipt:
        if not self._failed:
            self._failed = True
            raise RuntimeError("test execution failure")  # noqa: TRY003
        return await self._delegate.submit(intent)


def _launch(*, position: int) -> LaunchCreatedV2:
    artifact = json.loads(FIXTURE.read_text(encoding="utf-8"))
    launch = decode_pump_create_v2_fixture_artifact(artifact)
    if not isinstance(launch, LaunchCreatedV2):
        raise TypeError
    return replace(launch, missing_evidence=(), transaction_index=position)


def _config(
    launch: LaunchCreatedV2,
    *,
    mode: str = "paper",
) -> CoreSniperConfig:
    return parse_sniper_config(
        f"""target:
  kind: wallet
  id: "{launch.creator_pubkey}"
execution:
  mode: {mode}
  quote_size_lamports: 1000000
"""
    )


def _volume_sizing_request(as_of_slot: int) -> VolumeSizingRequest:
    return VolumeSizingRequest(
        as_of_slot=as_of_slot,
        requested_quote_base_units=None,
        bankroll_quote_base_units=10_000_000,
        max_bankroll_fraction_ppm=None,
        independent_volume_quote_base_units=400_000,
        max_independent_volume_fraction_ppm=None,
        pool_quote_reserve_base_units=10_000_000,
        pool_token_reserve_base_units=10_000_000,
        max_price_impact_ppm=None,
        max_one_shot_exit_token_base_units=1_000_000,
    )


def _observation(launch: LaunchCreatedV2) -> RawChainObservation:
    return RawChainObservation(
        raw_id=uuid4(),
        source_id="test-watch",
        observer_id="test-observer",
        boot_id=uuid4(),
        receive_sequence=1,
        slot=launch.as_of_slot,
        parent_slot=None,
        blockhash=None,
        signature=launch.signature,
        transaction_index=launch.transaction_index,
        outer_instruction_index=launch.outer_instruction_index,
        inner_instruction_group_index=None,
        inner_instruction_index=None,
        stack_height=None,
        event_ordinal=None,
        commitment="finalized",
        canonical_status="canonical",
        received_wall_ns=1,
        received_monotonic_ns=1,
        program_id=None,
        account_pubkey=None,
        account_owner_program_id=None,
        raw_transaction=None,
        raw_transaction_format=None,
        raw_account_data=None,
        account_write_version=None,
        source_update_kind="transaction",
        raw_source_status=None,
        raw_source_payload=b"{}",
        decoder_name=None,
        decoder_version=None,
        idl_hash=None,
    )


def _mint_proofs(
    launch: LaunchCreatedV2,
    mint_pubkey: str,
) -> tuple[tuple[str, str], ...]:
    return tuple(
        (name, mint_pubkey if name == "mint" else pubkey)
        for name, pubkey in launch.account_role_proofs
    )


def _qualification(wallet: str) -> OperatorQualification:
    return OperatorQualification(
        status=QualificationStatus.QUALIFIED,
        as_of_slot=0,
        entity_id="operator-a",
        sample_count=3,
        win_count=2,
        win_rate_ppm=666_666,
        expectancy_quote_base_units=1,
        average_peak_pnl_quote_base_units=1,
        adverse_launch_count=2,
        adverse_rate_ppm=666_666,
        repeated_adverse_behavior=True,
        matched_wallet_count=1,
        reason_codes=("operator_qualified",),
        evidence_ids=(f"entity:{wallet}",),
    )


def _entity_evidence(wallet: str) -> tuple[WalletEntityEvidence, ...]:
    return (
        WalletEntityEvidence(
            as_of_slot=0,
            observed_slot=0,
            entity_id="operator-a",
            launch_id="historical-launch",
            wallet=wallet,
            entity_probability_ppm=900_000,
            evidence_ids=(f"entity:{wallet}",),
        ),
    )


def _rotated_churn(launch: LaunchCreatedV2) -> OperatorWalletChurnSnapshot:
    slot = int(launch.as_of_slot)
    return OperatorWalletChurnSnapshot(
        as_of_slot=Slot(slot),
        entity_id="operator-a",
        churn_snapshot_version=OPERATOR_WALLET_CHURN_SNAPSHOT_VERSION,
        current_profile_version="profile-current",
        previous_profile_version="profile-previous",
        previous_as_of_slot=Slot(slot - 1),
        current_active_address_count=1,
        previous_active_address_count=1,
        new_address_count=1,
        retained_address_count=0,
        retired_address_count=1,
        new_high_risk_role_count=1,
        retained_role_change_count=0,
        address_turnover_ppm=1_000_000,
        new_addresses=(
            _churn_address(launch.creator_pubkey, launch, WalletChurnStatus.NEW),
        ),
        retained_addresses=(),
        retired_addresses=(
            _churn_address(launch.user_pubkey, launch, WalletChurnStatus.RETIRED),
        ),
        evidence_ids=("churn:1",),
        reason_codes=("operator_wallet_churn_snapshot_built",),
    )


def _churn_address(
    address: str,
    launch: LaunchCreatedV2,
    status: WalletChurnStatus,
) -> WalletChurnAddress:
    return WalletChurnAddress(
        as_of_slot=Slot(int(launch.as_of_slot)),
        entity_id="operator-a",
        address=address,
        status=status,
        membership_probability_ppm=900_000,
        same_controller_probability_ppm=900_000,
        cooperating_probability_ppm=0,
        roles=(AddressRole.CREATOR,),
        high_risk_role_count=1,
        evidence_ids=(f"churn:{address}",),
        model_version="model-v1",
    )


def _rotated_protection(launch: LaunchCreatedV2) -> RuggerProtectionSnapshot:
    slot = int(launch.as_of_slot)
    creator = launch.creator_pubkey
    return RuggerProtectionSnapshot(
        as_of_slot=Slot(slot),
        target_wallet=launch.user_pubkey,
        roles=(),
        transfer_ranges=(
            WalletTransferRange(
                as_of_slot=Slot(slot),
                source_wallet=launch.user_pubkey,
                destination_wallet=creator,
                asset_kind=WalletAssetKind.NATIVE,
                asset_id="SOL",
                first_slot=Slot(slot - 1),
                last_slot=Slot(slot - 1),
                transfer_count=1,
                amount_base_units=1,
                evidence_ids=("transfer:1",),
            ),
        ),
        multi_hops=(),
        freshness=(
            WalletFreshnessEvidence(
                as_of_slot=Slot(slot),
                wallet=creator,
                first_observed_slot=Slot(slot - 1),
                age_slots=1,
                status=FreshWalletStatus.PROVEN,
                evidence_ids=("history:creator",),
            ),
        ),
        reason_codes=("direct_transfer_ranges_observed", "fresh_wallets_proven"),
    )


if __name__ == "__main__":
    unittest.main()
