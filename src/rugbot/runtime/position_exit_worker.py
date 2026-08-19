"""Independent polling worker for durable position exits."""

# Worker validation exposes explicit operator-facing contract errors.
# ruff: noqa: TRY003

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Protocol

from rugbot.decision.playbook_rules import (
    PROBABILITY_PPM_DENOMINATOR,
    ExitRuleAction,
    PlaybookRules,
    SellLevel,
    SellRules,
)
from rugbot.domain.decisions import AbstainResult
from rugbot.execution.ports import ExecutionIntent, ExecutionReceipt
from rugbot.execution.position_runtime import (
    PaperPositionState,
    PositionMarketEvidence,
    advance_paper_position,
)
from rugbot.runtime.risk_gatekeeper import RiskDecision

if TYPE_CHECKING:
    from collections.abc import Mapping

    from rugbot.execution.ports import ExecutionPort

MANUAL_HALF_EXIT_PPM = 500_000
MANUAL_FULL_EXIT_PPM = PROBABILITY_PPM_DENOMINATOR


class PositionStore(Protocol):
    """Durable position operations required by the exit worker."""

    def read_all(self) -> tuple[PaperPositionState, ...]:
        """Read every open position."""

    def get(self, market_id: str) -> PaperPositionState | None:
        """Read one open position."""

    def save(self, state: PaperPositionState) -> None:
        """Persist one position snapshot."""

    def remove(self, market_id: str) -> bool:
        """Remove one fully exited position."""


PositionEvidenceResolver = Callable[
    [PaperPositionState, int],
    Awaitable[PositionMarketEvidence | AbstainResult | None],
]
FinalizedSlotResolver = Callable[[], Awaitable[int]]
PositionRiskEvaluator = Callable[
    [ExecutionIntent, PaperPositionState],
    Awaitable[RiskDecision],
]


@dataclass(frozen=True, slots=True)
class PositionExitResult:
    """Observable outcome for one position during a worker cycle."""

    market_id: str
    action: str
    intent: ExecutionIntent | None
    receipt: ExecutionReceipt | None
    error: str | None


@dataclass(frozen=True, slots=True)
class PositionExitCycle:
    """One independent finalized-slot polling cycle."""

    as_of_slot: int
    results: tuple[PositionExitResult, ...]


class PositionExitWorkerError(ValueError):
    """Raised when worker configuration or manual commands are invalid."""

    @classmethod
    def invalid(cls, message: str) -> PositionExitWorkerError:
        """Build a worker contract error."""

        return cls(message)


class PositionExitWorker:
    """Poll and execute exits without depending on new launch notifications."""

    def __init__(  # noqa: PLR0913
        self,
        *,
        store: PositionStore,
        execution_ports: Mapping[str, ExecutionPort],
        finalized_slot_resolver: FinalizedSlotResolver,
        evidence_resolver: PositionEvidenceResolver,
        risk_evaluator: PositionRiskEvaluator,
        poll_interval_seconds: float = 0.4,
    ) -> None:
        """Validate and retain the worker's canonical dependencies."""

        if poll_interval_seconds <= 0:
            raise PositionExitWorkerError.invalid("poll interval must be positive")
        if (
            not callable(finalized_slot_resolver)
            or not callable(evidence_resolver)
            or not callable(risk_evaluator)
        ):
            raise PositionExitWorkerError.invalid("position resolvers must be callable")
        if not execution_ports or any(
            type(mode) is not str or not mode for mode in execution_ports
        ):
            raise PositionExitWorkerError.invalid("execution ports are malformed")
        self._store = store
        self._execution_ports = dict(execution_ports)
        self._finalized_slot_resolver = finalized_slot_resolver
        self._evidence_resolver = evidence_resolver
        self._risk_evaluator = risk_evaluator
        self._poll_interval_seconds = poll_interval_seconds

    async def run_once(self) -> PositionExitCycle:
        """Evaluate every persisted position at one newer finalized slot."""

        as_of_slot = await self._finalized_slot_resolver()
        if type(as_of_slot) is not int or as_of_slot < 0:
            raise PositionExitWorkerError.invalid("finalized slot is malformed")
        positions = self._store.read_all()
        if not positions:
            return PositionExitCycle(as_of_slot=as_of_slot, results=())
        resolved = await asyncio.gather(
            *(self._resolve_evidence(position, as_of_slot) for position in positions)
        )
        results: list[PositionExitResult] = []
        for position, evidence in zip(positions, resolved, strict=True):
            if isinstance(evidence, str):
                results.append(_error_result(position.market_id, evidence))
                continue
            if evidence is None:
                results.append(
                    PositionExitResult(
                        market_id=position.market_id,
                        action="no_evidence",
                        intent=None,
                        receipt=None,
                        error=None,
                    )
                )
                continue
            results.append(await self._advance(position, evidence))
        return PositionExitCycle(as_of_slot=as_of_slot, results=tuple(results))

    async def run_forever(self, stop_event: asyncio.Event) -> None:
        """Poll until explicitly stopped, independently of the launch watcher."""

        while not stop_event.is_set():
            await self.run_once()
            try:
                await asyncio.wait_for(
                    stop_event.wait(),
                    timeout=self._poll_interval_seconds,
                )
            except TimeoutError:
                continue

    async def execute_manual_exit(
        self,
        market_id: str,
        *,
        sell_fraction_ppm: int,
        as_of_slot: int,
    ) -> PositionExitResult:
        """Execute an operator-requested 50% or 100% position reduction."""

        if sell_fraction_ppm not in (MANUAL_HALF_EXIT_PPM, MANUAL_FULL_EXIT_PPM):
            raise PositionExitWorkerError.invalid("manual exit must be 50% or 100%")
        state = self._store.get(market_id)
        if state is None:
            raise PositionExitWorkerError.invalid("manual exit position was not found")
        if type(as_of_slot) is not int or as_of_slot <= state.as_of_slot:
            raise PositionExitWorkerError.invalid(
                "manual exit slot must advance position state"
            )
        if sell_fraction_ppm == MANUAL_FULL_EXIT_PPM:
            sell_amount = int(state.current_position_base_units)
        else:
            sell_amount = max(1, int(state.current_position_base_units) // 2)
        intent = ExecutionIntent(
            intent_id=(
                f"{market_id}:{as_of_slot}:manual-sell:"
                f"{state.emitted_sell_intent_count}:{sell_fraction_ppm}"
            ),
            as_of_slot=as_of_slot,
            market_id=market_id,
            side="sell",
            quote_amount_base_units=None,
            base_amount_base_units=sell_amount,
            max_slippage_bps=state.max_slippage_bps,
            reason_codes=(
                "manual_exit_50"
                if sell_fraction_ppm == MANUAL_HALF_EXIT_PPM
                else "manual_exit_100",
            ),
        )
        remaining = int(state.current_position_base_units) - sell_amount
        next_state = replace(
            state,
            as_of_slot=as_of_slot,
            current_position_base_units=remaining,
            emitted_sell_intent_count=state.emitted_sell_intent_count + 1,
            exit_rule_state=replace(
                state.exit_rule_state,
                exited_fraction_ppm=(
                    (int(state.original_position_base_units) - remaining)
                    * PROBABILITY_PPM_DENOMINATOR
                    // int(state.original_position_base_units)
                ),
            ),
        )
        return await self._submit_and_persist(intent, next_state)

    async def _resolve_evidence(
        self,
        position: PaperPositionState,
        as_of_slot: int,
    ) -> PositionMarketEvidence | None | str:
        try:
            evidence = await self._evidence_resolver(position, as_of_slot)
        except Exception as error:  # noqa: BLE001
            return f"evidence resolver failed: {type(error).__name__}"
        if isinstance(evidence, AbstainResult):
            return evidence.message
        if evidence is not None and not isinstance(evidence, PositionMarketEvidence):
            return "evidence resolver returned malformed evidence"
        return evidence

    async def _advance(
        self,
        state: PaperPositionState,
        evidence: PositionMarketEvidence,
    ) -> PositionExitResult:
        decision = advance_paper_position(
            rules=_rules_for_position(state),
            evidence=evidence,
            state=state,
            max_slippage_bps=state.max_slippage_bps,
            require_full_exit_capacity=True,
            require_calibrated_exit=False,
        )
        if isinstance(decision, AbstainResult):
            return _error_result(state.market_id, decision.message)
        if decision.action is ExitRuleAction.HOLD:
            self._store.save(decision.next_state)
            return PositionExitResult(
                market_id=state.market_id,
                action=decision.action.value,
                intent=None,
                receipt=None,
                error=None,
            )
        if decision.sell_intent is None:
            return _error_result(state.market_id, "sell decision omitted its intent")
        return await self._submit_and_persist(
            decision.sell_intent,
            decision.next_state,
        )

    async def _submit_and_persist(
        self,
        intent: ExecutionIntent,
        next_state: PaperPositionState,
    ) -> PositionExitResult:
        execution_port = self._execution_ports.get(next_state.execution_mode)
        if execution_port is None:
            return _error_result(
                intent.market_id,
                "position execution mode has no configured port",
                intent=intent,
            )
        try:
            risk_decision = await self._risk_evaluator(intent, next_state)
        except Exception as error:  # noqa: BLE001
            return _error_result(
                intent.market_id,
                f"position exit risk check failed: {type(error).__name__}",
                intent=intent,
            )
        if risk_decision.allowed is not True:
            return _error_result(
                intent.market_id,
                risk_decision.message,
                intent=intent,
            )
        try:
            receipt = await execution_port.submit(intent)
        except Exception as error:  # noqa: BLE001
            return _error_result(
                intent.market_id,
                f"position exit submission failed: {type(error).__name__}",
                intent=intent,
            )
        if (
            not isinstance(receipt, ExecutionReceipt)
            or receipt.accepted is not True
            or receipt.intent_id != intent.intent_id
            or receipt.as_of_slot != intent.as_of_slot
        ):
            return _error_result(
                intent.market_id,
                "position exit receipt was not accepted",
                intent=intent,
                receipt=receipt if isinstance(receipt, ExecutionReceipt) else None,
            )
        if next_state.current_position_base_units == 0:
            self._store.remove(intent.market_id)
        else:
            self._store.save(next_state)
        return PositionExitResult(
            market_id=intent.market_id,
            action="sell",
            intent=intent,
            receipt=receipt,
            error=None,
        )


def _error_result(
    market_id: str,
    error: str,
    *,
    intent: ExecutionIntent | None = None,
    receipt: ExecutionReceipt | None = None,
) -> PositionExitResult:
    return PositionExitResult(
        market_id=market_id,
        action="error",
        intent=intent,
        receipt=receipt,
        error=error,
    )


def _rules_for_position(state: PaperPositionState) -> PlaybookRules:
    full_exit = PROBABILITY_PPM_DENOMINATOR
    take_profit_levels = (
        (
            SellLevel(
                trigger_pnl_ppm=state.take_profit_pnl_ppm,
                sell_fraction_ppm=full_exit,
            ),
        )
        if state.take_profit_pnl_ppm is not None
        else ()
    )
    stop_loss_levels = (
        (
            SellLevel(
                trigger_pnl_ppm=state.stop_loss_pnl_ppm,
                sell_fraction_ppm=full_exit,
            ),
        )
        if state.stop_loss_pnl_ppm is not None
        else ()
    )
    return PlaybookRules(
        sell=SellRules(
            take_profit_levels=take_profit_levels,
            stop_loss_levels=stop_loss_levels,
        )
    )


__all__ = [
    "MANUAL_FULL_EXIT_PPM",
    "MANUAL_HALF_EXIT_PPM",
    "PositionExitCycle",
    "PositionExitResult",
    "PositionExitWorker",
    "PositionExitWorkerError",
]
