"""Single-wallet sniper orchestration for known target launches."""

# This boundary translates dependency failures into operator-visible outcomes.
# ruff: noqa: TRY003

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from enum import StrEnum
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from solders.pubkey import Pubkey

from rugbot.execution.ports import ExecutionIntent, ExecutionReceipt
from rugbot.execution.position_runtime import PaperPositionState
from rugbot.runtime.position_exit_worker import (
    MANUAL_FULL_EXIT_PPM,
    MANUAL_HALF_EXIT_PPM,
    PositionExitResult,
    PositionExitWorker,
)
from rugbot.runtime.risk_gatekeeper import (
    ExecutionCostBudget,
    RiskDecision,
    RiskGatekeeper,
    RiskLimits,
    RiskSnapshot,
)
from rugbot.storage.transaction_state import TransactionIntentRecord, TransactionState
from rugbot.tracker.models import (
    TargetExecutionMode,
    TargetExecutionPolicy,
)
from rugbot.utils.logger import get_logger

if TYPE_CHECKING:
    from collections.abc import Mapping

    from rugbot.execution.ports import ExecutionPort
    from rugbot.runtime.position_exit_worker import (
        FinalizedSlotResolver,
        PositionEvidenceResolver,
        PositionStore,
    )

logger = get_logger(__name__)


class SniperStage(StrEnum):
    """Operator-visible daemon state."""

    IDLE = "IDLE"
    CANDIDATE = "CANDIDATE"
    PENDING = "PENDING"
    POSITION = "POSITION"
    FAILED = "FAILED"


@dataclass(frozen=True, slots=True)
class ProcessedTargetLaunch:
    """Minimal processed-commitment evidence needed by the sniper hot path."""

    target_id: str
    market_id: str
    signature: str
    slot: int


@dataclass(frozen=True, slots=True)
class SniperLaunchResult:
    """One observable launch handling outcome."""

    stage: SniperStage
    message: str
    intent: ExecutionIntent | None
    receipt: ExecutionReceipt | None
    risk_decision: RiskDecision | None


@dataclass(frozen=True, slots=True)
class SniperDaemonSnapshot:
    """Current real daemon state projected to local operator clients."""

    stage: SniperStage
    kill_switch_active: bool
    message: str
    intent_id: str | None
    market_id: str | None
    open_positions: tuple[PaperPositionState, ...]
    risk_snapshot: RiskSnapshot | None
    max_exposure_lamports: int


class TargetPolicyStore(Protocol):
    """Persisted target-policy operations required by the daemon."""

    def get_target_execution_policy(
        self,
        funder_address: str,
    ) -> TargetExecutionPolicy | None:
        """Read one target-local execution policy."""

    def save_target_execution_policy(self, policy: TargetExecutionPolicy) -> None:
        """Persist one target-local execution policy."""


@runtime_checkable
class RecoverableExecutionPort(Protocol):
    """Execution port capable of resolving durable restart state."""

    async def submit(self, intent: ExecutionIntent) -> ExecutionReceipt:
        """Submit one execution intent."""

    async def recover_pending(self) -> tuple[TransactionIntentRecord, ...]:
        """Resolve durable non-terminal transactions."""


RiskSnapshotResolver = Callable[[ExecutionIntent], Awaitable[RiskSnapshot]]
CostBudgetResolver = Callable[[TargetExecutionPolicy], Awaitable[ExecutionCostBudget]]


class SniperDaemonError(ValueError):
    """Raised when daemon configuration or operator commands are malformed."""


class SniperDaemonService:
    """Own one wallet's launch, execution, recovery, and exit lifecycle."""

    def __init__(  # noqa: PLR0913
        self,
        *,
        policy_store: TargetPolicyStore,
        position_store: PositionStore,
        execution_ports: Mapping[str, ExecutionPort],
        risk_limits: RiskLimits,
        risk_snapshot_resolver: RiskSnapshotResolver,
        cost_budget_resolver: CostBudgetResolver,
        finalized_slot_resolver: FinalizedSlotResolver,
        evidence_resolver: PositionEvidenceResolver,
        exit_poll_interval_seconds: float = 1.0,
    ) -> None:
        """Validate and retain the daemon's canonical local dependencies."""

        required_modes = {
            TargetExecutionMode.SIMULATED.value,
            TargetExecutionMode.LIVE.value,
        }
        if not execution_ports or any(
            mode not in required_modes for mode in execution_ports
        ):
            raise SniperDaemonError("execution ports contain an unsupported mode")
        if not callable(risk_snapshot_resolver) or not callable(cost_budget_resolver):
            raise SniperDaemonError("risk resolvers must be callable")
        self._policy_store = policy_store
        self._position_store = position_store
        self._execution_ports = dict(execution_ports)
        self._risk_gatekeeper = RiskGatekeeper(risk_limits)
        self._risk_limits = risk_limits
        self._risk_snapshot_resolver = risk_snapshot_resolver
        self._cost_budget_resolver = cost_budget_resolver
        self._finalized_slot_resolver = finalized_slot_resolver
        self._exit_worker = PositionExitWorker(
            store=position_store,
            execution_ports=execution_ports,
            finalized_slot_resolver=finalized_slot_resolver,
            evidence_resolver=evidence_resolver,
            risk_evaluator=self._evaluate_exit_risk,
            poll_interval_seconds=exit_poll_interval_seconds,
        )
        self._kill_switch_active = False
        self._market_locks: dict[str, asyncio.Lock] = {}
        self._stop_event: asyncio.Event | None = None
        self._exit_task: asyncio.Task[None] | None = None
        self._stage = SniperStage.IDLE
        self._message = "waiting for a processed launch"
        self._intent_id: str | None = None
        self._market_id: str | None = None
        self._risk_snapshot: RiskSnapshot | None = None

    async def start(self) -> None:
        """Recover durable transactions, then start the independent exit worker."""

        if self._exit_task is not None and not self._exit_task.done():
            return
        recovered_ports: set[int] = set()
        for port in self._execution_ports.values():
            if id(port) in recovered_ports:
                continue
            recovered_ports.add(id(port))
            if isinstance(port, RecoverableExecutionPort):
                records = await port.recover_pending()
                self._restore_reconciled_buys(records)
        self._stop_event = asyncio.Event()
        self._exit_task = asyncio.create_task(
            self._exit_worker.run_forever(self._stop_event),
            name="sniper_position_exit_worker",
        )

    async def stop(self) -> None:
        """Stop the worker after its current bounded cycle."""

        if self._stop_event is not None:
            self._stop_event.set()
        if self._exit_task is not None:
            await self._exit_task
        self._exit_task = None
        self._stop_event = None

    def snapshot(self) -> SniperDaemonSnapshot:
        """Return current daemon and durable position facts for the TUI."""

        return SniperDaemonSnapshot(
            stage=self._stage,
            kill_switch_active=self._kill_switch_active,
            message=self._message,
            intent_id=self._intent_id,
            market_id=self._market_id,
            open_positions=self._position_store.read_all(),
            risk_snapshot=self._risk_snapshot,
            max_exposure_lamports=self._risk_limits.max_exposure_lamports,
        )

    async def refresh_wallet_risk(self, target_id: str) -> RiskSnapshot:
        """Refresh non-mutating wallet risk telemetry for an operator client."""

        policy = self._policy_store.get_target_execution_policy(target_id)
        if policy is None:
            raise SniperDaemonError("target has no persisted execution policy")
        probe = ExecutionIntent(
            intent_id=f"risk-snapshot:{target_id}",
            as_of_slot=0,
            market_id=target_id,
            side="buy",
            quote_amount_base_units=policy.quote_size_lamports,
            base_amount_base_units=None,
            max_slippage_bps=policy.max_slippage_bps,
            reason_codes=("operator_risk_snapshot",),
        )
        snapshot = await self._risk_snapshot_resolver(probe)
        if not isinstance(snapshot, RiskSnapshot):
            raise SniperDaemonError("risk resolver returned malformed evidence")
        self._risk_snapshot = replace(
            snapshot,
            kill_switch_active=(
                snapshot.kill_switch_active or self._kill_switch_active
            ),
        )
        return self._risk_snapshot

    async def handle_processed_launch(
        self,
        launch: ProcessedTargetLaunch,
        *,
        current_processed_slot: int,
    ) -> SniperLaunchResult:
        """Run the minimal known-target entry path at processed commitment."""

        _validate_launch(launch, current_processed_slot)
        market_lock = self._market_locks.setdefault(launch.market_id, asyncio.Lock())
        async with market_lock:
            return await self._handle_locked_launch(launch, current_processed_slot)

    async def manual_sell(
        self,
        market_id: str,
        *,
        fraction_ppm: int,
    ) -> PositionExitResult:
        """Execute an operator-requested 50% or 100% risk reduction."""

        if fraction_ppm not in (MANUAL_HALF_EXIT_PPM, MANUAL_FULL_EXIT_PPM):
            raise SniperDaemonError("manual sell must be 50% or 100%")
        as_of_slot = await self._finalized_slot_resolver()
        result = await self._exit_worker.execute_manual_exit(
            market_id,
            sell_fraction_ppm=fraction_ppm,
            as_of_slot=as_of_slot,
        )
        self._set_snapshot(
            SniperStage.POSITION
            if self._position_store.get(market_id)
            else SniperStage.IDLE,
            result.error or "manual sell accepted",
            intent_id=result.intent.intent_id if result.intent else None,
            market_id=market_id,
        )
        return result

    def toggle_kill_switch(self) -> bool:
        """Toggle new-entry blocking without disabling exits."""

        self._kill_switch_active = not self._kill_switch_active
        self._message = (
            "kill switch blocks new buys; exits remain active"
            if self._kill_switch_active
            else "kill switch cleared"
        )
        return self._kill_switch_active

    def set_target_mode(
        self,
        target_id: str,
        mode: TargetExecutionMode,
    ) -> TargetExecutionPolicy:
        """Persist the selected target's execution mode."""

        if not isinstance(mode, TargetExecutionMode):
            raise SniperDaemonError("target mode is malformed")
        policy = self._policy_store.get_target_execution_policy(target_id)
        if policy is None:
            raise SniperDaemonError("target has no persisted execution policy")
        updated = replace(
            policy,
            execution_mode=mode,
            updated_at=datetime.now(UTC).isoformat(),
        )
        self._policy_store.save_target_execution_policy(updated)
        return updated

    async def _handle_locked_launch(  # noqa: C901, PLR0911
        self,
        launch: ProcessedTargetLaunch,
        current_processed_slot: int,
    ) -> SniperLaunchResult:
        policy = self._policy_store.get_target_execution_policy(launch.target_id)
        if policy is None:
            return self._result(SniperStage.IDLE, "target has no execution policy")
        if not policy.monitoring_enabled:
            return self._result(SniperStage.IDLE, "target monitoring is paused")
        if policy.execution_mode is TargetExecutionMode.OFF:
            return self._result(SniperStage.IDLE, "target execution mode is off")
        if current_processed_slot - launch.slot > 1:
            return self._result(SniperStage.IDLE, "stale launch rejected")
        if self._position_store.get(launch.market_id) is not None:
            return self._result(SniperStage.IDLE, "market already has an open position")
        execution_port = self._execution_ports.get(policy.execution_mode.value)
        if execution_port is None:
            return self._result(
                SniperStage.FAILED,
                "target execution mode has no configured port",
            )
        intent = _buy_intent(launch, policy)
        self._set_snapshot(
            SniperStage.CANDIDATE,
            "known target launch passed freshness and duplicate checks",
            intent_id=intent.intent_id,
            market_id=launch.market_id,
        )
        try:
            snapshot, cost_budget = await asyncio.gather(
                self._risk_snapshot_resolver(intent),
                self._cost_budget_resolver(policy),
            )
        except Exception as error:
            logger.exception("sniper risk evidence resolution failed")
            return self._result(
                SniperStage.FAILED,
                f"risk evidence failed: {type(error).__name__}",
                intent=intent,
            )
        if not isinstance(snapshot, RiskSnapshot) or not isinstance(
            cost_budget, ExecutionCostBudget
        ):
            return self._result(
                SniperStage.FAILED,
                "risk resolver returned malformed evidence",
                intent=intent,
            )
        snapshot = replace(
            snapshot,
            kill_switch_active=(
                snapshot.kill_switch_active or self._kill_switch_active
            ),
        )
        self._risk_snapshot = snapshot
        risk_decision = self._risk_gatekeeper.evaluate(
            intent,
            snapshot=snapshot,
            cost_budget=cost_budget,
        )
        if not risk_decision.allowed:
            return self._result(
                SniperStage.FAILED,
                risk_decision.message,
                intent=intent,
                risk_decision=risk_decision,
            )
        self._set_snapshot(
            SniperStage.PENDING,
            "execution intent passed risk checks",
            intent_id=intent.intent_id,
            market_id=launch.market_id,
        )
        try:
            receipt = await execution_port.submit(intent)
        except Exception as error:
            logger.exception("sniper execution submission failed")
            return self._result(
                SniperStage.FAILED,
                f"execution submission failed: {type(error).__name__}",
                intent=intent,
                risk_decision=risk_decision,
            )
        receipt_output = _validated_receipt_output(intent, receipt)
        if type(receipt_output) is str:
            return self._result(
                SniperStage.FAILED,
                receipt_output,
                intent=intent,
                receipt=receipt,
                risk_decision=risk_decision,
            )
        self._position_store.save(
            PaperPositionState(
                as_of_slot=receipt.as_of_slot,
                market_id=launch.market_id,
                target_id=launch.target_id,
                execution_mode=policy.execution_mode.value,
                original_position_base_units=receipt_output,
                current_position_base_units=receipt_output,
                entry_quote_lamports=policy.quote_size_lamports,
                entry_cost_lamports=int(receipt.estimated_fee_lamports or 0),
                take_profit_pnl_ppm=policy.take_profit_pnl_ppm,
                stop_loss_pnl_ppm=policy.stop_loss_pnl_ppm,
                max_slippage_bps=policy.max_slippage_bps,
            )
        )
        return self._result(
            SniperStage.POSITION,
            "entry accepted and durable position opened",
            intent=intent,
            receipt=receipt,
            risk_decision=risk_decision,
        )

    async def _evaluate_exit_risk(
        self,
        intent: ExecutionIntent,
        position: PaperPositionState,
    ) -> RiskDecision:
        policy = self._policy_store.get_target_execution_policy(position.target_id)
        if policy is None:
            raise SniperDaemonError("position target policy is unavailable")
        snapshot, cost_budget = await asyncio.gather(
            self._risk_snapshot_resolver(intent),
            self._cost_budget_resolver(policy),
        )
        if not isinstance(snapshot, RiskSnapshot) or not isinstance(
            cost_budget, ExecutionCostBudget
        ):
            raise SniperDaemonError("exit risk resolver returned malformed evidence")
        snapshot = replace(
            snapshot,
            kill_switch_active=(
                snapshot.kill_switch_active or self._kill_switch_active
            ),
        )
        self._risk_snapshot = snapshot
        return self._risk_gatekeeper.evaluate(
            intent,
            snapshot=snapshot,
            cost_budget=cost_budget,
        )

    def _restore_reconciled_buys(
        self,
        records: tuple[TransactionIntentRecord, ...],
    ) -> None:
        for record in records:
            if (
                record.state is not TransactionState.RECONCILED
                or record.side != "buy"
                or self._position_store.get(record.market_id) is not None
            ):
                continue
            target_id = _target_reason(record.reason_codes)
            policy = self._policy_store.get_target_execution_policy(target_id)
            if (
                policy is None
                or type(record.token_delta_base_units) is not int
                or record.token_delta_base_units <= 0
                or type(record.landed_slot) is not int
                or type(record.quote_amount_base_units) is not int
                or record.quote_amount_base_units <= 0
            ):
                raise SniperDaemonError(
                    "reconciled buy cannot restore its durable position"
                )
            self._position_store.save(
                PaperPositionState(
                    as_of_slot=record.landed_slot,
                    market_id=record.market_id,
                    target_id=target_id,
                    execution_mode=TargetExecutionMode.LIVE.value,
                    original_position_base_units=record.token_delta_base_units,
                    current_position_base_units=record.token_delta_base_units,
                    entry_quote_lamports=int(record.quote_amount_base_units),
                    entry_cost_lamports=sum(
                        value
                        for value in (
                            record.network_fee_lamports,
                            record.jito_tip_lamports,
                            record.ata_rent_lamports,
                            record.protocol_fee_lamports,
                        )
                        if value is not None
                    ),
                    take_profit_pnl_ppm=policy.take_profit_pnl_ppm,
                    stop_loss_pnl_ppm=policy.stop_loss_pnl_ppm,
                    max_slippage_bps=policy.max_slippage_bps,
                )
            )

    def _result(
        self,
        stage: SniperStage,
        message: str,
        *,
        intent: ExecutionIntent | None = None,
        receipt: ExecutionReceipt | None = None,
        risk_decision: RiskDecision | None = None,
    ) -> SniperLaunchResult:
        self._set_snapshot(
            stage,
            message,
            intent_id=intent.intent_id if intent else None,
            market_id=intent.market_id if intent else None,
        )
        return SniperLaunchResult(stage, message, intent, receipt, risk_decision)

    def _set_snapshot(
        self,
        stage: SniperStage,
        message: str,
        *,
        intent_id: str | None,
        market_id: str | None,
    ) -> None:
        self._stage = stage
        self._message = message
        self._intent_id = intent_id
        self._market_id = market_id


def _buy_intent(
    launch: ProcessedTargetLaunch,
    policy: TargetExecutionPolicy,
) -> ExecutionIntent:
    return ExecutionIntent(
        intent_id=(
            f"launch:{launch.signature}:{launch.market_id}:{launch.target_id}:buy"
        ),
        as_of_slot=launch.slot,
        market_id=launch.market_id,
        side="buy",
        quote_amount_base_units=policy.quote_size_lamports,
        base_amount_base_units=None,
        max_slippage_bps=policy.max_slippage_bps,
        reason_codes=(
            "known_target_processed_launch",
            f"target:{launch.target_id}",
        ),
    )


def _validate_launch(launch: object, current_processed_slot: object) -> None:
    if not isinstance(launch, ProcessedTargetLaunch):
        raise SniperDaemonError("processed launch is malformed")
    for field_name, value in (
        ("target_id", launch.target_id),
        ("market_id", launch.market_id),
    ):
        try:
            Pubkey.from_string(value)
        except (TypeError, ValueError) as error:
            raise SniperDaemonError(
                f"{field_name} is not a Solana public key"
            ) from error
    if type(launch.signature) is not str or not launch.signature:
        raise SniperDaemonError("launch signature is required")
    if type(launch.slot) is not int or launch.slot < 0:
        raise SniperDaemonError("launch slot is malformed")
    if type(current_processed_slot) is not int or current_processed_slot < launch.slot:
        raise SniperDaemonError("current processed slot is malformed")


def _validated_receipt_output(
    intent: ExecutionIntent,
    receipt: object,
) -> int | str:
    if not isinstance(receipt, ExecutionReceipt):
        return "execution port returned a malformed receipt"
    if not receipt.accepted:
        return receipt.message
    if receipt.intent_id != intent.intent_id or receipt.as_of_slot != intent.as_of_slot:
        return "execution receipt identity does not match the intent"
    if (
        type(receipt.simulated_output_base_units) is not int
        or receipt.simulated_output_base_units <= 0
    ):
        return "accepted buy receipt omitted the acquired token amount"
    return receipt.simulated_output_base_units


def _target_reason(reason_codes: tuple[str, ...]) -> str:
    targets = tuple(
        reason.removeprefix("target:")
        for reason in reason_codes
        if reason.startswith("target:")
    )
    if len(targets) != 1:
        raise SniperDaemonError("recovered intent has no unique target identity")
    try:
        Pubkey.from_string(targets[0])
    except ValueError as error:
        raise SniperDaemonError("recovered target identity is malformed") from error
    return targets[0]


__all__ = [
    "ProcessedTargetLaunch",
    "SniperDaemonError",
    "SniperDaemonService",
    "SniperDaemonSnapshot",
    "SniperLaunchResult",
    "SniperStage",
]
