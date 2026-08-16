"""Fail-closed wallet watch decisions for known operator launches."""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, TypeAlias

from rugbot.decision.consolidation_protection import (
    ConsolidationResult,
    ConsolidationSignal,
    validate_consolidation_signal,
)
from rugbot.decision.playbook_rules import (
    EntryRuleAction,
    EntryRuleInput,
    EntryRuleState,
    evaluate_entry_rules,
)
from rugbot.decision.volume_sizing import (
    VolumeSizingRequest,
    size_volume_liquidity_aware,
)
from rugbot.domain.decisions import AbstainReason, AbstainResult
from rugbot.domain.launches import LaunchCreatedV2
from rugbot.domain.observations import RawChainObservation
from rugbot.execution.ports import (
    ExecutionIntent,
    ExecutionMode,
    ExecutionPort,
    ExecutionReceipt,
)
from rugbot.execution.position_runtime import (
    CalibratedExitEvidence,
    PaperPositionState,
    PositionMarketEvidence,
    advance_paper_position,
)
from rugbot.runtime.config import CoreSniperConfig
from rugbot.runtime.config import ExecutionMode as SniperMode
from rugbot.runtime.matcher import match_launch_target
from rugbot.storage.paper_position_store import (
    PaperPositionStore,
    PaperPositionStoreError,
)

if TYPE_CHECKING:
    from rugbot.decision.operator_qualification import (
        OperatorQualification,
        WalletEntityEvidence,
    )
    from rugbot.graph.rugger_protection import RuggerProtectionSnapshot
    from rugbot.graph.wallet_churn import OperatorWalletChurnSnapshot

WATCH_MAX_SLIPPAGE_BPS = 500
WATCH_DEFAULT_MAX_CONSECUTIVE_LOSSES = 3
WATCH_DEFAULT_BUY_COOLDOWN_SLOTS = 1

LaunchResolution: TypeAlias = LaunchCreatedV2 | AbstainResult | None
LaunchResolver: TypeAlias = Callable[
    [RawChainObservation], LaunchResolution | Awaitable[LaunchResolution]
]
EntryEvidenceResolver: TypeAlias = Callable[
    [LaunchCreatedV2, RawChainObservation],
    EntryRuleInput | AbstainResult | Awaitable[EntryRuleInput | AbstainResult],
]
VolumeSizingResolver: TypeAlias = Callable[
    [LaunchCreatedV2, RawChainObservation], VolumeSizingRequest | AbstainResult
]
PositionEvidenceResolver: TypeAlias = Callable[
    [RawChainObservation, PaperPositionState],
    PositionMarketEvidence
    | AbstainResult
    | None
    | Awaitable[PositionMarketEvidence | AbstainResult | None],
]
PositionPollResolver: TypeAlias = Callable[
    [PaperPositionState, int],
    PositionMarketEvidence
    | AbstainResult
    | None
    | Awaitable[PositionMarketEvidence | AbstainResult | None],
]
ConsolidationSignalResolver: TypeAlias = Callable[
    [RawChainObservation, PaperPositionState], ConsolidationResult
]


@dataclass(frozen=True, slots=True)
class WatchSnipeCandidate:
    """A deterministic, non-signing buy candidate for one watched launch."""

    as_of_slot: int
    launch_id: str
    creator_pubkey: str
    block_transaction_index: int
    intent: ExecutionIntent


ExecutionPortResolver: TypeAlias = Callable[
    [LaunchCreatedV2, RawChainObservation, WatchSnipeCandidate],
    ExecutionPort | AbstainResult | Awaitable[ExecutionPort | AbstainResult],
]


@dataclass(slots=True)
class WatchSnipeHandler:
    """Resolve watched observations and route candidates to observe or paper."""

    config: CoreSniperConfig
    resolver: LaunchResolver
    execution_port: ExecutionPort
    qualification: OperatorQualification | None = None
    entity_evidence: tuple[WalletEntityEvidence, ...] | None = None
    operator_churn: OperatorWalletChurnSnapshot | AbstainResult | None = None
    rugger_protection: RuggerProtectionSnapshot | AbstainResult | None = None
    candidates: list[WatchSnipeCandidate] = field(default_factory=list)
    receipts: list[ExecutionReceipt] = field(default_factory=list)
    max_consecutive_losses: int = WATCH_DEFAULT_MAX_CONSECUTIVE_LOSSES
    buy_cooldown_slots: int = WATCH_DEFAULT_BUY_COOLDOWN_SLOTS
    entry_evidence_resolver: EntryEvidenceResolver | None = None
    volume_sizing_resolver: VolumeSizingResolver | None = None
    position_evidence_resolver: PositionEvidenceResolver | None = None
    consolidation_signal_resolver: ConsolidationSignalResolver | None = None
    execution_port_resolver: ExecutionPortResolver | None = None
    position_store: PaperPositionStore | None = None
    consecutive_losses: int = field(default=0, init=False)
    auto_buy_paused: bool = field(default=False, init=False)
    _last_buy_slot: int | None = field(default=None, init=False, repr=False)
    _last_outcome_slot: int | None = field(default=None, init=False, repr=False)
    _bought_market_ids: set[str] = field(default_factory=set, init=False, repr=False)
    _pending_market_ids: set[str] = field(default_factory=set, init=False, repr=False)
    _positions: dict[str, PaperPositionState] = field(
        default_factory=dict, init=False, repr=False
    )
    _entry_rule_state: EntryRuleState = field(
        default_factory=EntryRuleState, init=False, repr=False
    )

    def __post_init__(self) -> None:
        """Validate root-level risk controls before any observation is handled."""

        if type(self.max_consecutive_losses) is not int or (
            self.max_consecutive_losses <= 0
        ):
            raise ValueError(  # noqa: TRY003
                "max_consecutive_losses must be a positive integer"
            )
        if type(self.buy_cooldown_slots) is not int or self.buy_cooldown_slots < 0:
            raise ValueError(  # noqa: TRY003
                "buy_cooldown_slots must be a non-negative integer"
            )
        if self.entry_evidence_resolver is not None and not callable(
            self.entry_evidence_resolver
        ):
            raise ValueError("entry_evidence_resolver must be callable")  # noqa: TRY003
        if self.volume_sizing_resolver is not None and not callable(
            self.volume_sizing_resolver
        ):
            raise ValueError("volume_sizing_resolver must be callable")  # noqa: TRY003
        if self.position_evidence_resolver is not None and not callable(
            self.position_evidence_resolver
        ):
            raise ValueError("position_evidence_resolver must be callable")  # noqa: TRY003
        if self.consolidation_signal_resolver is not None and not callable(
            self.consolidation_signal_resolver
        ):
            raise ValueError(  # noqa: TRY003
                "consolidation_signal_resolver must be callable"
            )
        if self.execution_port_resolver is not None and not callable(
            self.execution_port_resolver
        ):
            raise ValueError("execution_port_resolver must be callable")  # noqa: TRY003
        if self.position_store is not None:
            restored = self.position_store.read_all()
            self._positions.update({state.market_id: state for state in restored})
            self._bought_market_ids.update(state.market_id for state in restored)

    def record_realized_pnl(
        self,
        pnl_lamports: int,
        *,
        as_of_slot: int,
    ) -> AbstainResult | None:
        """Update the global loss counter from one finalized integer PnL result.

        A non-negative result breaks the consecutive-loss streak. Reaching the
        configured threshold pauses new buys, while existing exit handling is
        unaffected because this state is consulted only by ``handle`` before a
        buy execution.
        """

        if type(pnl_lamports) is not int:
            return _risk_abstain(
                "invalid_realized_pnl:expected_integer_lamports",
                as_of_slot=-1,
            )
        if type(as_of_slot) is not int or as_of_slot < 0:
            return _risk_abstain(
                "invalid_realized_pnl:invalid_as_of_slot",
                as_of_slot=-1,
            )
        if self._last_outcome_slot is not None and as_of_slot < self._last_outcome_slot:
            return _abstain(
                AbstainReason.STALE_STATE,
                "realized_pnl:outcome_slot_regressed",
                as_of_slot=as_of_slot,
            )

        self._last_outcome_slot = as_of_slot
        if pnl_lamports < 0:
            self.consecutive_losses = min(
                self.consecutive_losses + 1,
                self.max_consecutive_losses,
            )
            if self.consecutive_losses >= self.max_consecutive_losses:
                self.auto_buy_paused = True
        else:
            self.consecutive_losses = 0
        return None

    def resume_auto_buy(self) -> None:
        """Explicitly clear the global auto-buy pause after operator review."""

        self.auto_buy_paused = False
        self.consecutive_losses = 0

    async def handle(  # noqa: C901, PLR0911, PLR0912
        self, observation: RawChainObservation
    ) -> AbstainResult | None:
        """Handle one immutable observation without submitting a transaction."""

        if type(observation) is not RawChainObservation:
            return _abstain(
                AbstainReason.UNKNOWN_PROTOCOL_STATE,
                "watch handler received malformed observation",
                as_of_slot=-1,
            )
        if self.config.execution.mode in (SniperMode.PAPER, SniperMode.LIVE) and (
            observation.commitment != "finalized"
            or observation.canonical_status != "canonical"
        ):
            return _abstain(
                AbstainReason.STALE_STATE,
                (
                    "paper entry requires canonical finalized observation"
                    if self.config.execution.mode is SniperMode.PAPER
                    else "live entry requires canonical finalized observation"
                ),
                as_of_slot=observation.slot,
            )
        position_error = await self._advance_open_positions(observation)
        if position_error is not None:
            return position_error
        try:
            launch = self.resolver(observation)
            if inspect.isawaitable(launch):
                launch = await launch
        except Exception as error:  # noqa: BLE001
            return _abstain(
                AbstainReason.UNKNOWN_PROTOCOL_STATE,
                f"watch launch resolver failed: {type(error).__name__}",
                as_of_slot=observation.slot,
            )
        if isinstance(launch, AbstainResult):
            return launch
        if launch is None:
            return None

        candidate = build_watch_snipe_candidate(
            config=self.config,
            launch=launch,
            observation=observation,
            qualification=self.qualification,
            entity_evidence=self.entity_evidence,
            operator_churn=self.operator_churn,
            rugger_protection=self.rugger_protection,
        )
        if isinstance(candidate, AbstainResult):
            return candidate
        if candidate is None:
            return None

        entry_decision = await self._evaluate_entry(candidate, observation, launch)
        if isinstance(entry_decision, AbstainResult):
            return entry_decision
        if entry_decision.action is not EntryRuleAction.BUY:
            return _risk_abstain(
                f"entry_rule_{entry_decision.action.value}:"
                f"{','.join(entry_decision.reason_codes)}",
                as_of_slot=candidate.as_of_slot,
            )
        entry_size = entry_decision.quote_size_lamports
        if (
            self.config.execution.mode is SniperMode.PAPER
            or self.volume_sizing_resolver is not None
        ):
            entry_size = self._size_entry(
                launch=launch,
                observation=observation,
                candidate=candidate,
                requested_quote_size=entry_size,
            )
            if isinstance(entry_size, AbstainResult):
                return entry_size
        sized_candidate = _candidate_with_entry_size(candidate, entry_size)
        if isinstance(sized_candidate, AbstainResult):
            return sized_candidate
        candidate = sized_candidate

        risk_error = self._check_buy_risk(candidate)
        if risk_error is not None:
            return risk_error

        execution_port = await self._resolve_execution_port(
            launch=launch,
            observation=observation,
            candidate=candidate,
        )
        if isinstance(execution_port, AbstainResult):
            self._pending_market_ids.discard(candidate.intent.market_id)
            return execution_port
        try:
            receipt = await execution_port.submit(candidate.intent)
        except Exception as error:  # noqa: BLE001
            return _abstain(
                AbstainReason.UNKNOWN_PROTOCOL_STATE,
                f"watch execution port failed: {type(error).__name__}",
                as_of_slot=observation.slot,
            )
        finally:
            self._pending_market_ids.discard(candidate.intent.market_id)
        receipt_error = _validate_receipt(
            receipt=receipt,
            candidate=candidate,
            mode=self.config.execution.mode,
        )
        if receipt_error is not None:
            return receipt_error
        self._bought_market_ids.add(candidate.intent.market_id)
        self._last_buy_slot = candidate.as_of_slot
        self._entry_rule_state = entry_decision.next_state
        self.candidates.append(candidate)
        self.receipts.append(receipt)
        if (
            self.config.execution.mode in (SniperMode.PAPER, SniperMode.LIVE)
            and receipt.simulated_output_base_units is not None
        ):
            position = PaperPositionState(
                as_of_slot=candidate.as_of_slot,
                market_id=candidate.intent.market_id,
                original_position_base_units=receipt.simulated_output_base_units,
                current_position_base_units=receipt.simulated_output_base_units,
            )
            try:
                if self.position_store is not None:
                    self.position_store.save(position)
            except PaperPositionStoreError:
                return _abstain(
                    AbstainReason.UNKNOWN_PROTOCOL_STATE,
                    "position state could not be persisted",
                    as_of_slot=candidate.as_of_slot,
                )
            self._positions[candidate.intent.market_id] = position
        return None

    async def _advance_open_positions(  # noqa: C901, PLR0911, PLR0912
        self,
        observation: RawChainObservation,
    ) -> AbstainResult | None:
        """Advance open positions from this finalized observation."""

        if (
            self.config.execution.mode not in (SniperMode.PAPER, SniperMode.LIVE)
            or not self._positions
        ):
            return None
        if (
            observation.commitment != "finalized"
            or observation.canonical_status != "canonical"
        ):
            return _abstain(
                AbstainReason.STALE_STATE,
                "position evidence requires canonical finalized observation",
                as_of_slot=observation.slot,
            )
        if self.position_evidence_resolver is None:
            return _abstain(
                AbstainReason.MISSING_FEATURE,
                "finalized position market evidence resolver is required",
                as_of_slot=observation.slot,
            )

        for state in tuple(self._positions.values()):
            consolidation_signal = self._resolve_consolidation_signal(
                observation, state
            )
            if isinstance(consolidation_signal, AbstainResult):
                return consolidation_signal
            try:
                evidence = self.position_evidence_resolver(observation, state)
                if inspect.isawaitable(evidence):
                    evidence = await evidence
            except Exception as error:  # noqa: BLE001
                return _abstain(
                    AbstainReason.UNKNOWN_PROTOCOL_STATE,
                    f"position evidence resolver failed: {type(error).__name__}",
                    as_of_slot=observation.slot,
                )
            if isinstance(evidence, AbstainResult):
                return evidence
            if evidence is None:
                continue
            if not isinstance(evidence, PositionMarketEvidence):
                return _abstain(
                    AbstainReason.UNKNOWN_PROTOCOL_STATE,
                    "position evidence resolver returned malformed evidence",
                    as_of_slot=observation.slot,
                )
            if evidence.as_of_slot != observation.slot:
                return _abstain(
                    AbstainReason.STALE_STATE,
                    "position evidence slot does not match its observation",
                    as_of_slot=observation.slot,
                )
            if consolidation_signal is not None:
                evidence = _apply_consolidation_signal(
                    evidence=evidence,
                    signal=consolidation_signal,
                    state=state,
                )
                if isinstance(evidence, AbstainResult):
                    return evidence
            position_error = await self.handle_position_evidence(evidence)
            if position_error is not None:
                return position_error
        return None

    async def poll_open_positions(  # noqa: PLR0911
        self,
        resolver: PositionPollResolver,
        *,
        as_of_slot: int,
    ) -> AbstainResult | None:
        """Evaluate open positions at a newer finalized market slot."""

        if self.config.execution.mode not in (SniperMode.PAPER, SniperMode.LIVE):
            return None
        if type(as_of_slot) is not int or as_of_slot < 0:
            return _abstain(
                AbstainReason.UNKNOWN_PROTOCOL_STATE,
                "position poll slot is malformed",
                as_of_slot=-1,
            )
        for state in tuple(self._positions.values()):
            try:
                evidence = resolver(state, as_of_slot)
                if inspect.isawaitable(evidence):
                    evidence = await evidence
            except Exception as error:  # noqa: BLE001
                return _abstain(
                    AbstainReason.UNKNOWN_PROTOCOL_STATE,
                    f"position poll resolver failed: {type(error).__name__}",
                    as_of_slot=as_of_slot,
                )
            if isinstance(evidence, AbstainResult):
                return evidence
            if evidence is None:
                continue
            if not isinstance(evidence, PositionMarketEvidence):
                return _abstain(
                    AbstainReason.UNKNOWN_PROTOCOL_STATE,
                    "position poll resolver returned malformed evidence",
                    as_of_slot=as_of_slot,
                )
            position_error = await self.handle_position_evidence(evidence)
            if position_error is not None:
                return position_error
        return None

    async def handle_position_evidence(  # noqa: C901, PLR0911, PLR0912
        self,
        evidence: PositionMarketEvidence,
        *,
        consolidation_signal: ConsolidationSignal | None = None,
    ) -> AbstainResult | None:
        """Advance one open paper position and submit any generated sell.

        Market evidence is supplied by the finalized observation caller.  The
        handler owns only the immutable position state and reuses the same
        pure exit rules as backtest replay; it never fetches or signs.
        """

        if self.config.execution.mode not in (SniperMode.PAPER, SniperMode.LIVE):
            return _abstain(
                AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
                "position exits require paper or live execution mode",
                as_of_slot=(
                    evidence.as_of_slot
                    if isinstance(evidence, PositionMarketEvidence)
                    else -1
                ),
            )
        if not isinstance(evidence, PositionMarketEvidence):
            return _abstain(
                AbstainReason.UNKNOWN_PROTOCOL_STATE,
                "position evidence is malformed",
                as_of_slot=-1,
            )
        state = self._positions.get(evidence.market_id)
        if state is None:
            return _abstain(
                AbstainReason.MISSING_FEATURE,
                "no open paper position matches market evidence",
                as_of_slot=evidence.as_of_slot,
            )
        if consolidation_signal is not None:
            evidence = _apply_consolidation_signal(
                evidence=evidence,
                signal=consolidation_signal,
                state=state,
            )
            if isinstance(evidence, AbstainResult):
                return evidence
        decision = advance_paper_position(
            rules=self.config.rules,
            evidence=evidence,
            state=state,
            max_slippage_bps=self.config.execution.max_slippage_bps,
            require_calibrated_exit=False,
        )
        if isinstance(decision, AbstainResult):
            return decision
        if decision.sell_intent is None:
            try:
                if self.position_store is not None:
                    self.position_store.save(decision.next_state)
            except PaperPositionStoreError:
                return _abstain(
                    AbstainReason.UNKNOWN_PROTOCOL_STATE,
                    "position state could not be persisted",
                    as_of_slot=evidence.as_of_slot,
                )
            self._positions[evidence.market_id] = decision.next_state
            return None
        try:
            receipt = await self.execution_port.submit(decision.sell_intent)
        except Exception as error:  # noqa: BLE001
            return _abstain(
                AbstainReason.UNKNOWN_PROTOCOL_STATE,
                f"position exit port failed: {type(error).__name__}",
                as_of_slot=evidence.as_of_slot,
            )
        receipt_error = _validate_receipt(
            receipt=receipt,
            candidate=_candidate_for_exit(decision.sell_intent),
            mode=self.config.execution.mode,
        )
        if receipt_error is not None:
            return receipt_error
        try:
            if self.position_store is not None:
                if decision.next_state.current_position_base_units == 0:
                    self.position_store.remove(evidence.market_id)
                else:
                    self.position_store.save(decision.next_state)
        except PaperPositionStoreError:
            return _abstain(
                AbstainReason.UNKNOWN_PROTOCOL_STATE,
                "position state could not be persisted",
                as_of_slot=evidence.as_of_slot,
            )
        self._positions[evidence.market_id] = decision.next_state
        self.receipts.append(receipt)
        if decision.next_state.current_position_base_units == 0:
            self._positions.pop(evidence.market_id, None)
        return None

    def _resolve_consolidation_signal(
        self,
        observation: RawChainObservation,
        state: PaperPositionState,
    ) -> ConsolidationSignal | AbstainResult | None:
        if self.consolidation_signal_resolver is None:
            return None
        try:
            signal = self.consolidation_signal_resolver(observation, state)
        except Exception as error:  # noqa: BLE001
            return _abstain(
                AbstainReason.UNKNOWN_PROTOCOL_STATE,
                f"consolidation signal resolver failed: {type(error).__name__}",
                as_of_slot=observation.slot,
            )
        if signal is None or isinstance(signal, AbstainResult):
            return signal
        if not isinstance(signal, ConsolidationSignal):
            return _abstain(
                AbstainReason.UNKNOWN_PROTOCOL_STATE,
                "consolidation signal resolver returned malformed evidence",
                as_of_slot=observation.slot,
            )
        return signal

    async def _resolve_execution_port(
        self,
        *,
        launch: LaunchCreatedV2,
        observation: RawChainObservation,
        candidate: WatchSnipeCandidate,
    ) -> ExecutionPort | AbstainResult:
        if self.execution_port_resolver is None:
            return self.execution_port
        try:
            resolved = self.execution_port_resolver(launch, observation, candidate)
            if inspect.isawaitable(resolved):
                resolved = await resolved
        except Exception as error:  # noqa: BLE001
            return _abstain(
                AbstainReason.MISSING_FEATURE,
                f"execution port resolver failed: {type(error).__name__}",
                as_of_slot=observation.slot,
            )
        if isinstance(resolved, AbstainResult):
            return resolved
        if not hasattr(resolved, "submit"):
            return _abstain(
                AbstainReason.UNKNOWN_PROTOCOL_STATE,
                "execution port resolver returned malformed port",
                as_of_slot=observation.slot,
            )
        return resolved

    def _check_buy_risk(
        self,
        candidate: WatchSnipeCandidate,
    ) -> AbstainResult | None:
        """Apply global pause, buy-once, and slot cooldown controls."""

        if self.auto_buy_paused:
            return _risk_abstain(
                "auto_buy_paused:max_consecutive_losses_reached",
                as_of_slot=candidate.as_of_slot,
            )

        market_id = candidate.intent.market_id
        if (
            market_id in self._bought_market_ids
            or market_id in self._pending_market_ids
        ):
            return _risk_abstain(
                "buy_once:market_already_purchased_or_pending",
                as_of_slot=candidate.as_of_slot,
            )

        if self._last_buy_slot is not None:
            if candidate.as_of_slot < self._last_buy_slot:
                return _abstain(
                    AbstainReason.STALE_STATE,
                    "buy_cooldown:observation_slot_regressed",
                    as_of_slot=candidate.as_of_slot,
                )
            elapsed_slots = candidate.as_of_slot - self._last_buy_slot
            remaining_slots = self.buy_cooldown_slots - elapsed_slots
            if remaining_slots > 0:
                return _risk_abstain(
                    f"buy_cooldown:wait_{remaining_slots}_slots",
                    as_of_slot=candidate.as_of_slot,
                )

        self._pending_market_ids.add(market_id)
        return None

    async def _evaluate_entry(
        self,
        candidate: WatchSnipeCandidate,
        observation: RawChainObservation,
        launch: LaunchCreatedV2,
    ) -> object:
        """Apply configured playbook entry rules using point-in-time evidence."""

        try:
            evidence = (
                self.entry_evidence_resolver(launch, observation)
                if self.entry_evidence_resolver is not None
                else _default_entry_evidence(launch, observation)
            )
            if inspect.isawaitable(evidence):
                evidence = await evidence
        except Exception as error:  # noqa: BLE001
            return _abstain(
                AbstainReason.MISSING_FEATURE,
                f"entry evidence resolver failed: {type(error).__name__}",
                as_of_slot=candidate.as_of_slot,
            )
        if isinstance(evidence, AbstainResult):
            return evidence
        decision = evaluate_entry_rules(
            rules=self.config.rules,
            evidence=evidence,
            state=self._entry_rule_state,
            base_quote_size_lamports=self.config.execution.quote_size_lamports,
        )
        return decision

    def _size_entry(  # noqa: PLR0911
        self,
        *,
        launch: LaunchCreatedV2,
        observation: RawChainObservation,
        candidate: WatchSnipeCandidate,
        requested_quote_size: int | None,
    ) -> int | AbstainResult:
        """Apply pure volume/liquidity sizing to one paper entry."""

        if self.volume_sizing_resolver is None:
            return _abstain(
                AbstainReason.MISSING_FEATURE,
                "paper entry volume and liquidity evidence is required",
                as_of_slot=candidate.as_of_slot,
            )
        try:
            request = self.volume_sizing_resolver(launch, observation)
        except Exception as error:  # noqa: BLE001
            return _abstain(
                AbstainReason.MISSING_FEATURE,
                f"volume sizing resolver failed: {type(error).__name__}",
                as_of_slot=candidate.as_of_slot,
            )
        if isinstance(request, AbstainResult):
            return request
        if not isinstance(request, VolumeSizingRequest):
            return _abstain(
                AbstainReason.MISSING_FEATURE,
                "volume sizing resolver returned malformed evidence",
                as_of_slot=candidate.as_of_slot,
            )
        if (
            request.as_of_slot is not None
            and request.as_of_slot != candidate.as_of_slot
        ):
            return _abstain(
                AbstainReason.STALE_STATE,
                "volume sizing evidence slot does not match the entry",
                as_of_slot=candidate.as_of_slot,
            )
        policy = self.config.volume_sizing
        sized = size_volume_liquidity_aware(
            replace(
                request,
                requested_quote_base_units=requested_quote_size,
                max_bankroll_fraction_ppm=policy.max_bankroll_fraction_ppm,
                max_independent_volume_fraction_ppm=(
                    policy.max_independent_volume_fraction_ppm
                ),
                max_price_impact_ppm=policy.max_price_impact_ppm,
            )
        )
        if isinstance(sized, AbstainResult):
            return sized
        return int(sized.quote_size_base_units)


def _default_entry_evidence(
    launch: LaunchCreatedV2,
    observation: RawChainObservation,
) -> EntryRuleInput:
    """Build launch-time evidence when no market-state provider is configured."""

    event_time_ms = observation.received_wall_ns // 1_000_000
    return EntryRuleInput(
        as_of_slot=observation.slot,
        token_mint=launch.mint_pubkey,
        now_ms=event_time_ms,
        event_time_ms=event_time_ms,
        is_copytrade=True,
        token_created_time_ms=event_time_ms,
    )


def _candidate_with_entry_size(
    candidate: WatchSnipeCandidate,
    quote_size_lamports: int | None,
) -> WatchSnipeCandidate | AbstainResult:
    if type(quote_size_lamports) is not int or quote_size_lamports <= 0:
        return _abstain(
            AbstainReason.MISSING_FEATURE,
            "entry rule did not produce a positive quote size",
            as_of_slot=candidate.as_of_slot,
        )
    return replace(
        candidate,
        intent=replace(
            candidate.intent,
            quote_amount_base_units=quote_size_lamports,
        ),
    )


def build_watch_snipe_candidate(  # noqa: PLR0911, PLR0913
    *,
    config: CoreSniperConfig,
    launch: object,
    observation: RawChainObservation,
    qualification: OperatorQualification | None = None,
    entity_evidence: tuple[WalletEntityEvidence, ...] | None = None,
    operator_churn: OperatorWalletChurnSnapshot | AbstainResult | None = None,
    rugger_protection: RuggerProtectionSnapshot | AbstainResult | None = None,
) -> WatchSnipeCandidate | AbstainResult | None:
    """Build a block-0/1 candidate from a proven launch and raw observation.

    ``None`` means the watched wallet did not produce a candidate in the
    configured block positions. ``AbstainResult`` means the evidence is not
    safe to interpret. This function is pure and performs no I/O.
    """

    if type(observation) is not RawChainObservation:
        return _abstain(
            AbstainReason.UNKNOWN_PROTOCOL_STATE,
            "watch observation is malformed",
            as_of_slot=-1,
        )
    if type(launch) is not LaunchCreatedV2:
        return _abstain(
            AbstainReason.UNKNOWN_PROTOCOL_STATE,
            "watch launch evidence is malformed",
            as_of_slot=observation.slot,
        )
    provenance_error = _validate_observation_alignment(
        launch=launch,
        observation=observation,
    )
    if provenance_error is not None:
        return provenance_error

    match = match_launch_target(
        config=config,
        launch=launch,
        qualification=qualification,
        entity_evidence=entity_evidence,
        operator_churn=operator_churn,
        rugger_protection=rugger_protection,
    )
    if isinstance(match, AbstainResult):
        return match
    if match is False:
        return None
    block_transaction_index = launch.transaction_index
    if block_transaction_index > config.strategy.max_entry_transaction_index:
        return None

    intent = ExecutionIntent(
        intent_id=(
            f"watch-snipe:pump_fun:{launch.mint_pubkey}:"
            f"{launch.as_of_slot}:{block_transaction_index}"
        ),
        as_of_slot=launch.as_of_slot,
        market_id=launch.mint_pubkey,
        side="buy",
        quote_amount_base_units=config.execution.quote_size_lamports,
        base_amount_base_units=None,
        max_slippage_bps=config.execution.max_slippage_bps,
        reason_codes=("known_operator_wallet", "block_position_0_or_1"),
    )
    return WatchSnipeCandidate(
        as_of_slot=launch.as_of_slot,
        launch_id=launch.launch_id,
        creator_pubkey=launch.creator_pubkey,
        block_transaction_index=block_transaction_index,
        intent=intent,
    )


def _validate_observation_alignment(
    *,
    launch: LaunchCreatedV2,
    observation: RawChainObservation,
) -> AbstainResult | None:
    if launch.as_of_slot != observation.slot:
        return _abstain(
            AbstainReason.STALE_STATE,
            "launch and observation slots do not match",
            as_of_slot=observation.slot,
        )
    if launch.transaction_index != observation.transaction_index:
        return _abstain(
            AbstainReason.UNKNOWN_PROTOCOL_STATE,
            "launch and observation transaction positions do not match",
            as_of_slot=observation.slot,
        )
    if (
        launch.signature is not None
        and observation.signature is not None
        and launch.signature != observation.signature
    ):
        return _abstain(
            AbstainReason.UNKNOWN_PROTOCOL_STATE,
            "launch and observation signatures do not match",
            as_of_slot=observation.slot,
        )
    return None


def _apply_consolidation_signal(
    *,
    evidence: PositionMarketEvidence,
    signal: ConsolidationSignal,
    state: PaperPositionState | None,
) -> PositionMarketEvidence | AbstainResult:
    """Translate a finalized consolidation event into an adverse exit trigger."""

    if state is None or not isinstance(signal, ConsolidationSignal):
        return _abstain(
            AbstainReason.UNKNOWN_PROTOCOL_STATE,
            "consolidation signal is malformed for the open position",
            as_of_slot=evidence.as_of_slot,
        )
    signal_error = validate_consolidation_signal(
        signal,
        market_id=state.market_id,
        as_of_slot=evidence.as_of_slot,
    )
    if signal_error is not None:
        return signal_error
    existing = evidence.calibrated_exit_evidence
    take_profit_pnl_ppm = existing.take_profit_pnl_ppm if existing is not None else 1
    return replace(
        evidence,
        calibrated_exit_evidence=CalibratedExitEvidence(
            as_of_slot=signal.as_of_slot,
            market_id=state.market_id,
            take_profit_pnl_ppm=take_profit_pnl_ppm,
            adverse_event_slot=signal.slot,
        ),
    )


def _validate_receipt(  # noqa: PLR0911
    *,
    receipt: object,
    candidate: WatchSnipeCandidate,
    mode: SniperMode,
) -> AbstainResult | None:
    if type(receipt) is not ExecutionReceipt:
        return _abstain(
            AbstainReason.UNKNOWN_PROTOCOL_STATE,
            "watch execution port returned malformed receipt",
            as_of_slot=candidate.as_of_slot,
        )
    expected_mode = ExecutionMode(mode.value)
    if (
        receipt.mode is not expected_mode
        or receipt.intent_id != candidate.intent.intent_id
    ):
        return _abstain(
            AbstainReason.UNKNOWN_PROTOCOL_STATE,
            "watch execution receipt identity mismatch",
            as_of_slot=candidate.as_of_slot,
        )
    if receipt.as_of_slot != candidate.as_of_slot:
        return _abstain(
            AbstainReason.UNKNOWN_PROTOCOL_STATE,
            "watch execution receipt slot mismatch",
            as_of_slot=candidate.as_of_slot,
        )
    if mode is SniperMode.LIVE:
        if (
            not receipt.accepted
            or not receipt.would_submit_transaction
            or not receipt.signature
        ):
            return _abstain(
                AbstainReason.UNKNOWN_PROTOCOL_STATE,
                "live execution receipt did not prove a submitted fill",
                as_of_slot=candidate.as_of_slot,
            )
        return None
    if receipt.would_submit_transaction or receipt.signature is not None:
        return _abstain(
            AbstainReason.UNKNOWN_PROTOCOL_STATE,
            "non-live execution receipt claimed transaction submission",
            as_of_slot=candidate.as_of_slot,
        )
    if mode is SniperMode.PAPER and not receipt.accepted:
        return _abstain(
            AbstainReason.MISSING_FEATURE,
            "paper watch candidate was not simulated",
            as_of_slot=candidate.as_of_slot,
        )
    return None


def _candidate_for_exit(intent: ExecutionIntent) -> WatchSnipeCandidate:
    """Adapt a generated sell intent to the shared receipt validator."""

    return WatchSnipeCandidate(
        as_of_slot=intent.as_of_slot,
        launch_id=f"position:{intent.market_id}",
        creator_pubkey="position",
        block_transaction_index=0,
        intent=intent,
    )


def _abstain(
    reason: AbstainReason,
    message: str,
    *,
    as_of_slot: int,
) -> AbstainResult:
    return AbstainResult(reason=reason, message=message, as_of_slot=as_of_slot)


def _risk_abstain(message: str, *, as_of_slot: int) -> AbstainResult:
    """Return a stable, explicit abstention for a runtime risk gate."""

    return _abstain(
        AbstainReason.MISSING_FEATURE,
        message,
        as_of_slot=as_of_slot,
    )
