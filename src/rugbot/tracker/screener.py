"""Fail-closed developer nomination and simulated tracking enrollment."""

from __future__ import annotations

import asyncio
import threading
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from enum import StrEnum
from typing import TYPE_CHECKING

from rugbot.backtest.cli import run_rpc_dataset
from rugbot.backtest.dataset import FinalizedBacktestResult
from rugbot.backtest.evaluation import BacktestSplit
from rugbot.domain.decisions import AbstainReason, AbstainResult
from rugbot.ingest.rpc_observer import observe_finalized_transaction
from rugbot.intelligence.gmgn_creator_history import fetch_gmgn_creator_history
from rugbot.intelligence.token_resolver import resolve_token_or_wallet
from rugbot.tracker.models import (
    TargetExecutionMode,
    TargetExecutionPolicy,
)
from rugbot.utils.logger import get_logger

if TYPE_CHECKING:
    from rugbot.backtest.runners.cluster_optimizer import HistoricalTokenSample
    from rugbot.domain.observations import RawChainObservation
    from rugbot.ingest.pump.pump_stream import PumpPortalLaunchNotification
    from rugbot.tracker.service import TrackerService

logger = get_logger(__name__)

MISSING_COMPLETED_EVIDENCE = (
    "Pending: finalized completed outcomes and point-in-time entity evidence "
    "are required before autonomous qualification."
)
MIN_AUTONOMOUS_HISTORY = 10
MIN_ADVERSE_LAUNCHES = 2
MIN_ADVERSE_RATE_PPM = 500_000
MAX_AUTONOMOUS_TRANSACTIONS = 50
PAPER_QUOTE_SIZE_LAMPORTS = 25_000_000
FINALIZATION_TIMEOUT_SECONDS = 60.0


class ScreenerCandidateStatus(StrEnum):
    """Review lifecycle state for a discovered developer candidate."""

    QUALIFIED = "QUALIFIED"
    PENDING_REVIEW = "PENDING_REVIEW"
    ACCEPTED = "ACCEPTED"
    REJECTED = "REJECTED"


@dataclass(frozen=True, slots=True)
class ScreenerCandidate:
    """One developer cluster candidate surfaced for operator review."""

    token_mint: str
    token_symbol: str
    token_name: str
    creator_wallet: str
    root_funder: str
    cluster_token_count: int
    winrate_pct: float
    optimal_tp_label: str
    optimal_tp_multiplier: float
    optimal_net_ev_sol: float
    is_bible_qualified: bool
    qualification_reason: str
    status: ScreenerCandidateStatus
    discovered_at: str
    bundle_wallets_count: int = 0
    samples: tuple[HistoricalTokenSample, ...] = field(default_factory=tuple)


class ScreenerService:
    """Thread-safe candidate review queue and on-chain screener."""

    def __init__(
        self,
        max_candidates: int = 200,
        tracker_service: TrackerService | None = None,
        endpoint: str | None = None,
    ) -> None:
        self._max_candidates = max_candidates
        self._tracker_service = tracker_service
        self._endpoint = endpoint
        self._candidates: dict[str, ScreenerCandidate] = {}
        self._lock = threading.Lock()
        self._queue: asyncio.Queue[PumpPortalLaunchNotification] = asyncio.Queue(
            maxsize=max_candidates
        )
        self._queued_creators: set[str] = set()
        self._worker: asyncio.Task[None] | None = None

    async def start(self) -> None:
        """Start the bounded autonomous qualification worker when RPC is configured."""

        if self._endpoint and self._worker is None:
            self._worker = asyncio.create_task(self._run_qualification_worker())

    async def stop(self) -> None:
        """Stop the autonomous qualification worker."""

        if self._worker is None:
            return
        self._worker.cancel()
        try:
            await self._worker
        except asyncio.CancelledError:
            pass
        self._worker = None

    @property
    def tracker_service(self) -> TrackerService | None:
        """Return the attached tracker service."""
        return self._tracker_service

    @tracker_service.setter
    def tracker_service(self, service: TrackerService) -> None:
        """Attach or update the tracker service."""
        self._tracker_service = service

    def get_candidates(
        self, *, status: ScreenerCandidateStatus | None = None
    ) -> tuple[ScreenerCandidate, ...]:
        """Fetch candidates in reverse chronological order, optionally filtered by status."""
        with self._lock:
            items = list(self._candidates.values())
        items.sort(key=lambda c: c.discovered_at, reverse=True)
        if status is not None:
            return tuple(item for item in items if item.status == status)
        return tuple(items)

    def get_candidate(self, address: str) -> ScreenerCandidate | None:
        """Find candidate by dev wallet or master funder address."""
        with self._lock:
            return self._candidates.get(address)

    def scan_and_evaluate(
        self, query: str, *, custom_label: str | None = None
    ) -> ScreenerCandidate:
        """Resolve a token or wallet and nominate it without inventing outcomes."""
        resolved = resolve_token_or_wallet(query, custom_label=custom_label)
        dev_wallet = resolved.target_wallet
        root_funder = resolved.root_funder or dev_wallet
        candidate = ScreenerCandidate(
            token_mint=resolved.input_address if resolved.is_token else dev_wallet,
            token_symbol=resolved.symbol or "DEV",
            token_name=resolved.name or resolved.default_label,
            creator_wallet=dev_wallet,
            root_funder=root_funder,
            cluster_token_count=0,
            winrate_pct=0.0,
            optimal_tp_label="UNPROVEN",
            optimal_tp_multiplier=1.0,
            optimal_net_ev_sol=0.0,
            is_bible_qualified=False,
            qualification_reason=MISSING_COMPLETED_EVIDENCE,
            status=ScreenerCandidateStatus.PENDING_REVIEW,
            discovered_at=datetime.now(UTC).isoformat(),
            bundle_wallets_count=len(resolved.bundle_wallets),
        )
        self._store_candidate(candidate)
        return candidate

    def _queue_notification(self, notification: PumpPortalLaunchNotification) -> None:
        """Queue each creator once without allowing an unbounded global backlog."""

        creator = notification.creator_pubkey
        if self._worker is None or creator in self._queued_creators:
            return
        try:
            self._queue.put_nowait(notification)
        except asyncio.QueueFull:
            logger.warning("Autonomous screener queue is full; skipped %s", creator)
            return
        self._queued_creators.add(creator)

    async def _run_qualification_worker(self) -> None:
        """Qualify queued creators serially to bound public-provider load."""

        while True:
            notification = await self._queue.get()
            creator = notification.creator_pubkey
            try:
                await self._qualify_notification(notification)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Autonomous qualification failed for %s", creator)
                self._set_pending_reason(
                    creator, "Qualification failed; inspect the runtime log."
                )
            finally:
                self._queued_creators.discard(creator)
                self._queue.task_done()

    async def _qualify_notification(
        self, notification: PumpPortalLaunchNotification
    ) -> None:
        """Run provider nomination, finalized reconstruction, and OOS qualification."""

        if self._endpoint is None:
            return
        history = await fetch_gmgn_creator_history(notification.creator_pubkey)
        if isinstance(history, AbstainResult):
            self._set_pending_reason(
                notification.creator_pubkey,
                f"Creator-history nomination unavailable: {history.message}",
            )
            return
        if history.total_created_count < MIN_AUTONOMOUS_HISTORY:
            self._set_pending_reason(
                notification.creator_pubkey,
                "Insufficient indexed launch history for finalized qualification: "
                f"{history.total_created_count}/{MIN_AUTONOMOUS_HISTORY}.",
            )
            return
        observation = await self._wait_for_finalized_notification(notification)
        if observation is None:
            return
        result = await run_rpc_dataset(
            operator_wallet=notification.creator_pubkey,
            endpoint=self._endpoint,
            start_slot=0,
            end_slot=observation.slot,
            max_transactions=MAX_AUTONOMOUS_TRANSACTIONS,
            fixed_entry_quote_base_units=PAPER_QUOTE_SIZE_LAMPORTS,
            min_history_launch_count=MIN_AUTONOMOUS_HISTORY,
        )
        if isinstance(result, AbstainResult):
            self._set_pending_reason(
                notification.creator_pubkey,
                f"Finalized qualification abstained: {result.message}",
            )
            return
        if not isinstance(result, FinalizedBacktestResult):
            self._set_pending_reason(
                notification.creator_pubkey,
                "Finalized qualification produced no out-of-sample result.",
            )
            return
        self._apply_finalized_result(notification.creator_pubkey, result)

    async def _wait_for_finalized_notification(
        self, notification: PumpPortalLaunchNotification
    ) -> RawChainObservation | None:
        """Hydrate the nomination signature into exact finalized RPC evidence."""

        if self._endpoint is None:
            return None
        try:
            async with asyncio.timeout(FINALIZATION_TIMEOUT_SECONDS):
                while True:
                    observation = await observe_finalized_transaction(
                        notification.signature,
                        expected_slot=None,
                        endpoint=self._endpoint,
                        source_id="autonomous-screener",
                    )
                    if isinstance(observation, AbstainResult):
                        if observation.reason in (
                            AbstainReason.MISSING_FEATURE,
                            AbstainReason.STALE_STATE,
                        ):
                            await asyncio.sleep(1.0)
                            continue
                        self._set_pending_reason(
                            notification.creator_pubkey,
                            f"Finalization abstained: {observation.message}",
                        )
                        return None
                    if observation is not None:
                        return observation
        except TimeoutError:
            self._set_pending_reason(
                notification.creator_pubkey,
                "Launch signature did not finalize within 60 seconds.",
            )
        return None

    def _apply_finalized_result(
        self, creator: str, result: FinalizedBacktestResult
    ) -> None:
        """Auto-enroll only a profitable OOS result with repeated adverse history."""

        cases = sorted(result.dataset.cases, key=lambda item: item.decision_slot)
        history = cases[-1].history if cases else ()
        adverse_count = sum(
            sample.adverse_event_elapsed_ms is not None for sample in history
        )
        adverse_rate_ppm = adverse_count * 1_000_000 // len(history) if history else 0
        test_metrics = next(
            (
                metrics
                for metrics in result.report.split_metrics
                if metrics.split is BacktestSplit.TEST
            ),
            None,
        )
        profitable_oos = (
            test_metrics is not None
            and test_metrics.attempted_trade_count > 0
            and test_metrics.net_pnl_attempted_quote_base_units > 0
        )
        repeated_adverse = (
            adverse_count >= MIN_ADVERSE_LAUNCHES
            and adverse_rate_ppm >= MIN_ADVERSE_RATE_PPM
        )
        if len(history) < MIN_AUTONOMOUS_HISTORY or not repeated_adverse:
            self._set_pending_reason(
                creator,
                "Finalized history did not prove repeated adverse behavior: "
                f"{adverse_count}/{len(history)} launches.",
            )
            return
        if not profitable_oos:
            self._set_pending_reason(
                creator,
                "Repeated adverse behavior was observed, but the OOS paper route "
                "was not net-profitable.",
            )
            return
        candidate = self.get_candidate(creator)
        if candidate is None:
            return
        win_count = sum(
            sample.realized_net_pnl_quote_base_units > 0 for sample in history
        )
        winrate = win_count * 100.0 / len(history)
        tp_ppm = self._selected_take_profit_ppm(result)
        if tp_ppm <= 0:
            self._set_pending_reason(
                creator,
                "Finalized OOS route did not prove a positive take-profit threshold.",
            )
            return
        qualified = replace(
            candidate,
            cluster_token_count=len(history),
            winrate_pct=winrate,
            optimal_tp_label=f"+{tp_ppm / 10_000:.0f}%",
            optimal_tp_multiplier=1.0 + tp_ppm / 1_000_000,
            optimal_net_ev_sol=(
                test_metrics.net_pnl_attempted_quote_base_units / 1_000_000_000
            ),
            is_bible_qualified=True,
            qualification_reason=(
                "Qualified from finalized point-in-time outcomes: "
                f"{adverse_count}/{len(history)} adverse; profitable OOS paper route."
            ),
            status=ScreenerCandidateStatus.QUALIFIED,
        )
        self._store_candidate(qualified)
        if self._tracker_service is not None:
            self.accept_candidate(creator, self._tracker_service)

    @staticmethod
    def _selected_take_profit_ppm(result: FinalizedBacktestResult) -> int:
        """Extract the canonical paper strategy's selected take-profit threshold."""

        for launch in reversed(result.evaluated_launches):
            for reason in launch.reason_codes:
                if reason.startswith("copy_trade_tp_") and reason.endswith("_ppm"):
                    value = reason.removeprefix("copy_trade_tp_").removesuffix("_ppm")
                    if value.isdigit():
                        return int(value)
        return 0

    def _set_pending_reason(self, creator: str, reason: str) -> None:
        """Persist an explicit fail-closed reason for one nominated creator."""

        with self._lock:
            candidate = self._candidates.get(creator)
            if candidate is None:
                return
            updated = replace(
                candidate,
                qualification_reason=reason,
                status=ScreenerCandidateStatus.PENDING_REVIEW,
            )
            self._candidates[creator] = updated
            if updated.root_funder != creator:
                self._candidates[updated.root_funder] = updated

    def nominate_live_launch(
        self, notification: PumpPortalLaunchNotification
    ) -> ScreenerCandidate:
        """Nominate one schema-validated provider trigger without trusting it as history."""
        candidate = ScreenerCandidate(
            token_mint=notification.mint_pubkey,
            token_symbol="LIVE",  # noqa: S106
            token_name="Finalization pending",  # noqa: S106
            creator_wallet=notification.creator_pubkey,
            root_funder=notification.creator_pubkey,
            cluster_token_count=0,
            winrate_pct=0.0,
            optimal_tp_label="UNPROVEN",
            optimal_tp_multiplier=1.0,
            optimal_net_ev_sol=0.0,
            is_bible_qualified=False,
            qualification_reason=MISSING_COMPLETED_EVIDENCE,
            status=ScreenerCandidateStatus.PENDING_REVIEW,
            discovered_at=datetime.now(UTC).isoformat(),
        )
        self._store_candidate(candidate)
        self._queue_notification(notification)
        return candidate

    def accept_candidate(
        self, address: str, tracker_service: TrackerService
    ) -> ScreenerCandidate | None:
        """Approve a candidate, enlisting the dev/master funder and configuring execution policy."""
        with self._lock:
            candidate = self._candidates.get(address)
            if candidate is None:
                return None
            candidate = replace(candidate, status=ScreenerCandidateStatus.ACCEPTED)
            self._candidates[address] = candidate
            if candidate.root_funder != address:
                self._candidates[candidate.root_funder] = candidate

        funder_addr = candidate.root_funder
        dev_addr = candidate.creator_wallet

        # Save root funder
        tracker_service.add_funder(
            funder_addr,
            label=f"Dev of ${candidate.token_symbol} ({candidate.optimal_tp_label})",
        )

        # Set optimal execution policy
        optimal_tp_ppm = int((candidate.optimal_tp_multiplier - 1.0) * 1_000_000)
        policy = TargetExecutionPolicy(
            funder_address=funder_addr,
            monitoring_enabled=True,
            execution_mode=TargetExecutionMode.SIMULATED,
            quote_size_lamports=PAPER_QUOTE_SIZE_LAMPORTS,
            take_profit_pnl_ppm=optimal_tp_ppm,
            stop_loss_pnl_ppm=-300_000,  # -30% SL
            max_slippage_bps=500,
            priority_fee_microlamports=50_000,
            jito_tip_lamports=1_000_000,
            updated_at=datetime.now(UTC).isoformat(),
        )
        tracker_service.save_target_execution_policy(policy)

        # If root funder is distinct, also monitor the creator wallet
        if dev_addr != funder_addr:
            tracker_service.add_funder(
                dev_addr,
                label=f"${candidate.token_symbol} Dev Child",
            )

        return candidate

    def reject_candidate(self, address: str) -> ScreenerCandidate | None:
        """Mark a candidate as rejected/dismissed."""
        with self._lock:
            candidate = self._candidates.get(address)
            if candidate is not None:
                candidate = replace(candidate, status=ScreenerCandidateStatus.REJECTED)
                self._candidates[address] = candidate
                if candidate.root_funder != address:
                    self._candidates[candidate.root_funder] = candidate
            return candidate

    def clear(self) -> None:
        """Clear candidate queue."""
        with self._lock:
            self._candidates.clear()

    def _store_candidate(self, candidate: ScreenerCandidate) -> None:
        """Store one candidate under its creator and root-funder identities."""

        with self._lock:
            self._candidates[candidate.creator_wallet] = candidate
            if candidate.root_funder != candidate.creator_wallet:
                self._candidates[candidate.root_funder] = candidate
            while len(self._candidates) > self._max_candidates:
                oldest_key = min(
                    self._candidates,
                    key=lambda key: self._candidates[key].discovered_at,
                )
                del self._candidates[oldest_key]


__all__ = [
    "MISSING_COMPLETED_EVIDENCE",
    "ScreenerCandidate",
    "ScreenerCandidateStatus",
    "ScreenerService",
]
