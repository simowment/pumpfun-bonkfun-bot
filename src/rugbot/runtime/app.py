"""Unified RugbotApp application composition root and facade."""

# ruff: noqa: BLE001, TRY003

from __future__ import annotations

import asyncio
import inspect
import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING
from urllib.parse import urlsplit, urlunsplit

import base58
from sol_trade_sdk.pump import derive_bonding_curve_pda
from sol_trade_sdk.solana.provider_pool import RpcProviderPool
from solders.pubkey import Pubkey

from rugbot.application.commands import COMMAND_REGISTRY, BotCommand, CommandResult
from rugbot.domain.decisions import AbstainResult
from rugbot.domain.entities import TargetRecord
from rugbot.domain.observations import RawChainObservation
from rugbot.ingest.pump.pump_create_observation import (
    decode_pump_create_v2_observation,
)
from rugbot.ingest.pump.pump_stream import PumpPortalLaunchStream
from rugbot.ingest.rpc_observer import observe_address, observe_finalized_transaction
from rugbot.integrations.solscan import (
    SolscanClient,
    SolscanProviderError,
)
from rugbot.intelligence.entity_mint_index import (
    discover_finalized_entity_mints,
    entity_mint_discovery_to_json,
)
from rugbot.intelligence.token_resolver import ResolvedTarget, resolve_token_or_wallet
from rugbot.intelligence.wallet_intelligence import (
    WalletIntelligenceReport,
    abstention_to_json,
    build_wallet_intelligence_report_from_histories,
    report_to_json,
)
from rugbot.runtime.config import (
    SniperConfigError,
    load_provider_settings,
    load_sniper_config,
    resolve_dotenv,
)
from rugbot.runtime.event_bus import EventBus
from rugbot.runtime.workers.tracked_launch_observation import (
    TrackedLaunchObservationProducer,
)
from rugbot.storage.database import DatabaseManager
from rugbot.storage.jsonl_observation_store import JsonlObservationStore
from rugbot.storage.tracker import SQLiteTrackerRepository
from rugbot.tracker.clock import SystemClock
from rugbot.tracker.cluster_graph_model import (
    ClusterIntelligenceModel,
    build_cluster_intelligence_model,
)
from rugbot.tracker.engine import TrackerEngine
from rugbot.tracker.funder_discovery import discover_funder
from rugbot.tracker.models import (
    EntityBackfillRecord,
    EntityBackfillStatus,
    FunderRecord,
    LaunchRecord,
    TargetExecutionMode,
    TargetExecutionPolicy,
    TargetScanRecord,
    TransferRecord,
    WalletRecord,
)
from rugbot.tracker.screener import ScreenerService
from rugbot.tracker.service import TrackerService

SYSTEM_PROGRAM = "11111111111111111111111111111111"
ENTITY_BACKFILL_BATCH_SIZE = 10
ENTITY_BACKFILL_RETRY_SECONDS = 60

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from sol_trade_sdk.solana.provider_pool import RpcHttpTransport

    from rugbot.execution.position_runtime import PaperPositionState
    from rugbot.runtime.sniper_runtime import SniperRuntime
    from rugbot.runtime.workers.sniper_daemon import (
        SniperDaemonService,
        SniperDaemonSnapshot,
    )
    from rugbot.tracker.events import TrackerEvent


class RugbotApp:
    """Unified application facade exposing tracker, screener, and sniper services to any interface."""

    def __init__(  # noqa: PLR0913
        self,
        *,
        engine: TrackerEngine,
        repository: SQLiteTrackerRepository,
        event_bus: EventBus,
        service: TrackerService,
        database: DatabaseManager,
        screener: ScreenerService | None = None,
        launch_observation: TrackedLaunchObservationProducer | None = None,
        sniper_runtime: SniperRuntime | None = None,
        sniper_daemon: SniperDaemonService | None = None,
        owns_sniper: bool = False,
        endpoint: str | None = None,
        fallback_endpoints: tuple[str, ...] = (),
        solscan_api_key: str | None = None,
        state_dir: Path,
    ) -> None:
        self._engine = engine
        self._repository = repository
        self._event_bus = event_bus
        self._service = service
        self._database = database
        self._screener = screener or ScreenerService(tracker_service=service)
        if self._screener.tracker_service is None:
            self._screener.tracker_service = service
        self._launch_observation = launch_observation
        self._sniper_runtime = sniper_runtime
        self._sniper_daemon = sniper_daemon
        self._owns_sniper = owns_sniper
        self._endpoint = endpoint
        self._fallback_endpoints = fallback_endpoints
        self._solscan_api_key = solscan_api_key
        self._state_dir = state_dir
        self._backfill_tasks: dict[str, asyncio.Task[None]] = {}
        self._backfill_locks: dict[str, asyncio.Lock] = {}
        self._closed = False

    @property
    def screener(self) -> ScreenerService:
        """Return the real-time token and developer cluster screener."""
        return self._screener

    @property
    def engine(self) -> TrackerEngine:
        """Return the in-memory tracker engine."""
        return self._engine

    @property
    def repository(self) -> SQLiteTrackerRepository:
        """Return the canonical tracker repository."""
        return self._repository

    @property
    def event_bus(self) -> EventBus:
        """Return the shared event bus."""
        return self._event_bus

    @property
    def service(self) -> TrackerService:
        """Return the tracker service owning engine mutations and persistence."""
        return self._service

    @property
    def sniper_runtime(self) -> SniperRuntime | None:
        """Return the optional sniper runtime composition container."""
        return self._sniper_runtime

    @property
    def sniper_daemon(self) -> SniperDaemonService | None:
        """Return the attached sniper daemon service."""
        return self._sniper_daemon

    async def start(self) -> None:
        """Start background producers, observers, and the sniper daemon."""
        if self._launch_observation is not None:
            await self._launch_observation.start()
        if self._sniper_daemon is not None and self._owns_sniper:
            await self._sniper_daemon.start()
        for backfill in self._repository.get_incomplete_entity_backfills():
            self._schedule_entity_backfill(backfill.query)

    async def close(self) -> None:
        """Release background tasks, close the observation producer, and shut down sniper."""
        if self._closed:
            return
        self._closed = True
        tasks = tuple(self._backfill_tasks.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        if self._launch_observation is not None:
            await self._launch_observation.stop()
        if self._sniper_daemon is not None and self._owns_sniper:
            await self._sniper_daemon.stop()
        self._database.close()

    async def stop(self) -> None:
        """Alias for close() on application teardown."""
        await self.close()

    @property
    def observation_status(self) -> str:
        """Return the honest tracked-launch transport status."""
        if self._launch_observation is None:
            return "unconfigured"
        return self._launch_observation.status.value

    @property
    def observed_addresses(self) -> tuple[str, ...]:
        """Return addresses currently watched for their next launch."""
        if self._launch_observation is None:
            return ()
        return self._launch_observation.active_addresses

    async def refresh_observation(self) -> None:
        """Reconcile the launch observer after target enrollment changes."""
        if self._launch_observation is not None:
            await self._launch_observation.refresh()

    async def analyze_wallet(
        self, query: str, *, max_transactions: int = 100
    ) -> CommandResult:
        """Resolve and scan a token or wallet without persisting a target."""
        if self._endpoint is None:
            return CommandResult(ok=False, message="SOLANA_RPC_HTTP is required")
        normalized_query = query.strip()
        existing = self._repository.get_entity_backfill(normalized_query)
        try:
            if existing is None:
                resolved = await asyncio.to_thread(
                    resolve_token_or_wallet,
                    normalized_query,
                    rpc_url=self._endpoint,
                    fallback_endpoints=self._fallback_endpoints,
                )
                identity = {
                    "input": normalized_query,
                    "is_token": resolved.is_token,
                    "resolved_creator": resolved.target_wallet,
                    "root_funder": resolved.root_funder,
                    "scan_wallet": resolved.target_wallet,
                    "token_name": resolved.name,
                    "token_symbol": resolved.symbol,
                    "bonding_curve": resolved.bonding_curve,
                }
                scan_wallet = resolved.target_wallet
            else:
                scan_wallet = existing.wallet
                identity = self._cached_identity(existing) or {
                    "input": normalized_query,
                    "is_token": normalized_query != scan_wallet,
                    "resolved_creator": scan_wallet,
                    "root_funder": None,
                    "scan_wallet": scan_wallet,
                    "token_name": None,
                    "token_symbol": None,
                }
            if identity["is_token"] is True:
                initial_resolution = (
                    resolved
                    if existing is None
                    else ResolvedTarget(
                        input_address=normalized_query,
                        target_wallet=scan_wallet,
                        is_token=True,
                        name=(
                            str(identity["token_name"])
                            if identity.get("token_name")
                            else None
                        ),
                        symbol=(
                            str(identity["token_symbol"])
                            if identity.get("token_symbol")
                            else None
                        ),
                        bonding_curve=(
                            str(identity["bonding_curve"])
                            if identity.get("bonding_curve")
                            else None
                        ),
                    )
                )
                (
                    creation,
                    observation,
                    creation_message,
                ) = await self._cache_explicit_token_creation(initial_resolution)
                if creation is not None and observation is not None:
                    identity.update(
                        {
                            "token_name": creation.name,
                            "token_symbol": creation.symbol,
                            "target_creation_signature": creation.creation_signature,
                            "target_creation_slot": observation.slot,
                            "target_creation_status": "finalized",
                            "bonding_curve": creation.bonding_curve,
                        }
                    )
                else:
                    identity["target_creation_status"] = "unavailable"
                    identity["target_creation_message"] = creation_message
            result = await self._run_entity_backfill_batch(
                query=normalized_query,
                wallet=scan_wallet,
                requested_transactions=max_transactions,
                identity=identity,
            )
        except (OSError, RuntimeError, ValueError) as error:
            return CommandResult(ok=False, message=str(error))
        self._schedule_entity_backfill(normalized_query)
        return result

    async def _cache_explicit_token_creation(  # noqa: C901, PLR0911, PLR0912
        self,
        resolution: ResolvedTarget,
    ) -> tuple[
        ResolvedTarget | None,
        RawChainObservation | None,
        str | None,
    ]:
        """Index a mint creation with Solscan and prove it through finalized RPC."""

        if self._endpoint is None:
            return None, None, "SOLANA_RPC_HTTP is required for creation confirmation"
        creation = resolution
        solscan_error: str | None = None
        if creation.creation_signature is None and self._solscan_api_key is not None:
            try:
                candidate = await asyncio.to_thread(
                    SolscanClient(self._solscan_api_key).token_creation,
                    creation.input_address,
                )
                creation = ResolvedTarget(
                    input_address=candidate.mint,
                    target_wallet=candidate.creator,
                    is_token=True,
                    symbol=candidate.symbol,
                    name=candidate.name,
                    creation_signature=candidate.transaction_signature,
                    bonding_curve=creation.bonding_curve,
                )
            except (OSError, ValueError, SolscanProviderError) as error:
                solscan_error = str(error)
        if creation.creation_signature is None:
            try:
                creation = await asyncio.to_thread(
                    resolve_token_or_wallet,
                    creation.input_address,
                    rpc_url=self._endpoint,
                    fallback_endpoints=self._fallback_endpoints,
                )
            except (OSError, RuntimeError, ValueError) as error:
                message = f"targeted RPC mint lookup unavailable: {error}"
                if solscan_error is not None:
                    message += f"; Solscan index unavailable: {solscan_error}"
                return None, None, message
        if creation.target_wallet != resolution.target_wallet:
            return None, None, "indexed creator conflicts with finalized account"
        if creation.creation_signature is None:
            return None, None, "targeted mint lookup omitted the creation signature"
        mint = resolution.input_address
        creator = resolution.target_wallet
        store = JsonlObservationStore(
            self._state_dir / "entity_cache" / f"{creator}.jsonl"
        )
        transport = RpcProviderPool((self._endpoint, *self._fallback_endpoints))
        observation = await observe_finalized_transaction(
            creation.creation_signature,
            expected_slot=None,
            endpoint=self._endpoint,
            source_id="solscan-token-meta-finalized-rpc",
            observer_id="entity-target-creation",
            receive_sequence=len(store.read_all()) + 1,
            transport=transport,
        )
        if not isinstance(observation, RawChainObservation):
            message = (
                observation.message
                if isinstance(observation, AbstainResult)
                else "target creation transaction did not finalize successfully"
            )
            return None, None, message
        decoded = decode_pump_create_v2_observation(observation)
        if isinstance(decoded, AbstainResult):
            return None, None, decoded.message
        if decoded is None:
            return None, None, "indexed transaction is not a Pump create_v2"
        if decoded.mint_pubkey != mint or decoded.creator_pubkey != creator:
            return None, None, "indexed creation conflicts with finalized Pump evidence"
        existing_observation = next(
            (
                item
                for item in store.read_all()
                if item.signature == observation.signature
            ),
            None,
        )
        if existing_observation is not None:
            if (
                existing_observation.slot != observation.slot
                or existing_observation.transaction_index
                != observation.transaction_index
                or existing_observation.raw_source_payload
                != observation.raw_source_payload
            ):
                return None, None, "cached creation evidence conflicts by signature"
            return creation, existing_observation, None
        store.append(observation)
        return creation, observation, None

    async def _run_entity_backfill_batch(
        self,
        *,
        query: str,
        wallet: str,
        requested_transactions: int,
        identity: dict[str, object],
    ) -> CommandResult:
        """Fetch, persist, and analyze one bounded finalized-history batch."""

        lock = self._backfill_locks.setdefault(query, asyncio.Lock())
        async with lock:
            current = self._repository.get_entity_backfill(query)
            now = datetime.now(UTC).isoformat()
            created_at = current.created_at if current is not None else now
            requested = max(
                requested_transactions,
                current.requested_transactions if current is not None else 0,
            )
            store = JsonlObservationStore(
                self._state_dir / "entity_cache" / f"{wallet}.jsonl"
            )
            cached_observations = store.read_all()
            cached_count = len(cached_observations)
            if (
                current is not None
                and current.status is EntityBackfillStatus.COMPLETE
                and current.cached_transactions < current.requested_transactions
                and cached_count == current.cached_transactions
                and self._cached_report_has_entity_mints(current)
                and current.report_json is not None
            ):
                return self._command_from_cached_report(current)
            if (
                current is not None
                and current.status is EntityBackfillStatus.COMPLETE
                and cached_count >= requested
                and cached_count == current.cached_transactions
                and self._cached_report_has_entity_mints(current)
                and current.report_json is not None
            ):
                return self._command_from_cached_report(current)
            if cached_count >= requested:
                base = current or EntityBackfillRecord(
                    query=query,
                    wallet=wallet,
                    requested_transactions=requested,
                    cached_transactions=cached_count,
                    before_signature=None,
                    status=EntityBackfillStatus.RUNNING,
                    message="cached history ready for analysis",
                    report_json=None,
                    created_at=created_at,
                    updated_at=now,
                )
                return await self._persist_cached_report(
                    current=base,
                    observations=tuple(cached_observations),
                    identity=identity,
                    before_signature=base.before_signature,
                    status=EntityBackfillStatus.COMPLETE,
                    message=(
                        f"finalized history cached: {requested}/{requested}"
                        " + targeted creation evidence"
                        if cached_count > requested
                        else f"finalized history cached: {cached_count}/{requested}"
                    ),
                )
            running = EntityBackfillRecord(
                query=query,
                wallet=wallet,
                requested_transactions=requested,
                cached_transactions=cached_count,
                before_signature=(
                    current.before_signature if current is not None else None
                ),
                status=EntityBackfillStatus.RUNNING,
                message=f"backfill running: {cached_count}/{requested} cached",
                report_json=current.report_json if current is not None else None,
                created_at=created_at,
                updated_at=now,
            )
            self._repository.save_entity_backfill(running)
            remaining = requested - cached_count
            batch_size = min(ENTITY_BACKFILL_BATCH_SIZE, max(remaining, 1))
            transport = RpcProviderPool(
                (self._endpoint, *self._fallback_endpoints),
                minimum_interval_seconds=0.125,
            )
            observations = await observe_address(
                wallet,
                endpoint=self._endpoint,
                source_id="entity-history-backfill",
                observer_id="entity-history-backfill",
                receive_sequence_start=cached_count,
                max_signatures=batch_size,
                max_transactions=batch_size,
                max_pages=1,
                before_signature=running.before_signature,
                transport=transport,
                observation_store=store,
                standard_history_only=True,
            )
            cached_observations = store.read_all()
            cached_count = len(cached_observations)
            if not isinstance(observations, tuple):
                return await self._persist_paused_backfill(
                    current=running,
                    observations=tuple(cached_observations),
                    identity=identity,
                    message=observations.message,
                )

            before_signature = running.before_signature
            if observations and observations[-1].signature is not None:
                before_signature = base58.b58encode(observations[-1].signature).decode(
                    "ascii"
                )
            complete = cached_count >= requested or not observations
            status = (
                EntityBackfillStatus.COMPLETE
                if complete
                else EntityBackfillStatus.RUNNING
            )
            if complete and cached_count < requested:
                message = (
                    "finalized history exhausted: all "
                    f"{cached_count} available transactions cached"
                )
            elif complete:
                message = (
                    f"finalized history cached: {requested}/{requested}"
                    " + targeted creation evidence"
                    if cached_count > requested
                    else f"finalized history cached: {cached_count}/{requested}"
                )
            else:
                message = f"backfill running: {cached_count}/{requested} cached"
            return await self._persist_cached_report(
                current=running,
                observations=tuple(cached_observations),
                identity=identity,
                before_signature=before_signature,
                status=status,
                message=message,
            )

    async def _persist_paused_backfill(
        self,
        *,
        current: EntityBackfillRecord,
        observations: tuple[RawChainObservation, ...],
        identity: dict[str, object],
        message: str,
    ) -> CommandResult:
        """Keep all durable evidence and expose a resumable provider pause."""

        before_signature = current.before_signature
        oldest_observation = min(
            observations,
            key=lambda item: (item.slot, item.transaction_index or -1),
            default=None,
        )
        if oldest_observation is not None and oldest_observation.signature is not None:
            before_signature = base58.b58encode(oldest_observation.signature).decode(
                "ascii"
            )
        if observations:
            return await self._persist_cached_report(
                current=current,
                observations=observations,
                identity=identity,
                before_signature=before_signature,
                status=EntityBackfillStatus.RATE_LIMITED,
                message=message,
            )
        paused = EntityBackfillRecord(
            query=current.query,
            wallet=current.wallet,
            requested_transactions=current.requested_transactions,
            cached_transactions=0,
            before_signature=before_signature,
            status=EntityBackfillStatus.RATE_LIMITED,
            message=message,
            report_json=current.report_json,
            created_at=current.created_at,
            updated_at=datetime.now(UTC).isoformat(),
        )
        self._repository.save_entity_backfill(paused)
        if current.report_json is not None:
            return self._command_from_cached_report(paused)
        data = {"identity": identity, "backfill": self._backfill_json(paused)}
        return CommandResult(ok=False, message=message, data=data)

    async def _persist_cached_report(  # noqa: PLR0913
        self,
        *,
        current: EntityBackfillRecord,
        observations: tuple[RawChainObservation, ...],
        identity: dict[str, object],
        before_signature: str | None,
        status: EntityBackfillStatus,
        message: str,
    ) -> CommandResult:
        """Rebuild the entity report exclusively from durable observations."""

        deduplicated = self._deduplicate_observations(observations)
        ordered = tuple(
            sorted(
                deduplicated,
                key=lambda item: (item.slot, item.transaction_index or -1),
                reverse=True,
            )
        )
        report = await build_wallet_intelligence_report_from_histories(
            current.wallet,
            histories={current.wallet: ordered},
            history_limits={
                current.wallet: max(current.requested_transactions, len(ordered))
            },
            endpoint=self._endpoint,
            warnings=(
                "linked wallet histories are pending staged backfill",
                *(
                    (str(identity["target_creation_message"]),)
                    if identity.get("target_creation_status") == "unavailable"
                    and identity.get("target_creation_message")
                    else ()
                ),
            ),
        )
        if not isinstance(report, WalletIntelligenceReport):
            failed = EntityBackfillRecord(
                query=current.query,
                wallet=current.wallet,
                requested_transactions=current.requested_transactions,
                cached_transactions=len(ordered),
                before_signature=before_signature,
                status=EntityBackfillStatus.FAILED,
                message=report.message,
                report_json=current.report_json,
                created_at=current.created_at,
                updated_at=datetime.now(UTC).isoformat(),
            )
            self._repository.save_entity_backfill(failed)
            return CommandResult(
                ok=False,
                message=report.message,
                data=abstention_to_json(report),
            )

        data = report_to_json(report)
        if status is EntityBackfillStatus.COMPLETE:
            entity_mints = await discover_finalized_entity_mints(
                target_wallet=current.wallet,
                graph_wallets=tuple(
                    node.address for node in report.nodes if not node.is_target
                ),
                endpoint=self._endpoint,
                fallback_endpoints=self._fallback_endpoints,
            )
            data["entity_mints"] = entity_mint_discovery_to_json(entity_mints)
            data["entity_mint_discovery"] = {
                "status": "complete" if not entity_mints.warnings else "partial",
                "wallets_checked": len(report.nodes),
                "finalized_mint_count": len(entity_mints.mints),
                "warnings": list(entity_mints.warnings),
            }
        else:
            data["entity_mints"] = []
            data["entity_mint_discovery"] = {
                "status": "pending",
                "wallets_checked": 0,
                "finalized_mint_count": 0,
                "warnings": [],
            }
        evidence = data.get("rug_evidence")
        if not isinstance(evidence, dict):
            raise TypeError("wallet evidence response is malformed")
        data["identity"] = identity
        data["tracking_address"] = current.wallet
        saved = EntityBackfillRecord(
            query=current.query,
            wallet=current.wallet,
            requested_transactions=current.requested_transactions,
            cached_transactions=len(ordered),
            before_signature=before_signature,
            status=status,
            message=message,
            report_json=None,
            created_at=current.created_at,
            updated_at=datetime.now(UTC).isoformat(),
        )
        data["backfill"] = self._backfill_json(saved)
        saved = EntityBackfillRecord(
            query=saved.query,
            wallet=saved.wallet,
            requested_transactions=saved.requested_transactions,
            cached_transactions=saved.cached_transactions,
            before_signature=saved.before_signature,
            status=saved.status,
            message=saved.message,
            report_json=json.dumps(data, separators=(",", ":"), sort_keys=True),
            created_at=saved.created_at,
            updated_at=saved.updated_at,
        )
        self._repository.save_entity_backfill(saved)
        self._persist_target_scan(
            query=current.query,
            identity=identity,
            data=data,
            scan_ok=True,
            message=message,
        )
        return CommandResult(ok=True, message=message, data=data)

    def _schedule_entity_backfill(self, query: str) -> None:
        """Continue one incomplete durable backfill without blocking the UI."""

        task = self._backfill_tasks.get(query)
        if self._closed or (task is not None and not task.done()):
            return
        self._backfill_tasks[query] = asyncio.create_task(
            self._resume_entity_backfill(query),
            name=f"entity-backfill:{query}",
        )

    async def _resume_entity_backfill(self, query: str) -> None:
        """Resume persisted batches until complete or the runtime closes."""

        while not self._closed:
            current = self._repository.get_entity_backfill(query)
            if current is None or current.status in {
                EntityBackfillStatus.COMPLETE,
                EntityBackfillStatus.FAILED,
            }:
                return
            identity = self._cached_identity(current) or {
                "input": query,
                "is_token": query != current.wallet,
                "resolved_creator": current.wallet,
                "root_funder": None,
                "scan_wallet": current.wallet,
                "token_name": None,
                "token_symbol": None,
            }
            await asyncio.sleep(
                ENTITY_BACKFILL_RETRY_SECONDS
                if current.status is EntityBackfillStatus.RATE_LIMITED
                else 1
            )
            await self._run_entity_backfill_batch(
                query=query,
                wallet=current.wallet,
                requested_transactions=current.requested_transactions,
                identity=identity,
            )

    @staticmethod
    def _cached_identity(backfill: EntityBackfillRecord) -> dict[str, object] | None:
        if backfill.report_json is None:
            return None
        payload = json.loads(backfill.report_json)
        if not isinstance(payload, dict):
            raise TypeError("cached entity report is malformed")
        identity = payload.get("identity")
        if not isinstance(identity, dict):
            return None
        return identity

    @staticmethod
    def _backfill_json(backfill: EntityBackfillRecord) -> dict[str, object]:
        requested = backfill.requested_transactions
        history_exhausted = (
            backfill.status is EntityBackfillStatus.COMPLETE
            and backfill.cached_transactions < requested
        )
        message = (
            "finalized history exhausted: all "
            f"{backfill.cached_transactions} available transactions cached"
            if history_exhausted
            else (
                f"finalized history cached: {requested}/{requested}"
                " + targeted creation evidence"
                if backfill.status is EntityBackfillStatus.COMPLETE
                and backfill.cached_transactions > requested
                else backfill.message
            )
        )
        return {
            "status": backfill.status.value,
            "cached_transactions": backfill.cached_transactions,
            "history_transactions": min(
                backfill.cached_transactions,
                requested,
            ),
            "requested_transactions": requested,
            "progress_percent": (
                100
                if backfill.status is EntityBackfillStatus.COMPLETE
                else min(
                    100,
                    round(100 * backfill.cached_transactions / requested),
                )
            ),
            "history_exhausted": history_exhausted,
            "resumable": backfill.status
            in {
                EntityBackfillStatus.PENDING,
                EntityBackfillStatus.RUNNING,
                EntityBackfillStatus.RATE_LIMITED,
            },
            "message": message,
            "updated_at": backfill.updated_at,
        }

    def _command_from_cached_report(
        self, backfill: EntityBackfillRecord
    ) -> CommandResult:
        if backfill.report_json is None:
            raise ValueError("cached entity report is missing")
        data = json.loads(backfill.report_json)
        if not isinstance(data, dict):
            raise TypeError("cached entity report is malformed")
        self._add_cached_pump_bonding_curves(data)
        backfill_data = self._backfill_json(backfill)
        data["backfill"] = backfill_data
        return CommandResult(
            ok=True,
            message=str(backfill_data["message"]),
            data=data,
        )

    @staticmethod
    def _add_cached_pump_bonding_curves(data: dict[str, object]) -> None:
        """Add deterministic Pump market identities to older cached UI rows."""

        for collection_name in ("entity_mints", "launches", "linked_launches"):
            collection = data.get(collection_name)
            if collection is None:
                continue
            if not isinstance(collection, list):
                raise TypeError(f"cached {collection_name} is malformed")
            for row in collection:
                if not isinstance(row, dict) or not isinstance(row.get("mint"), str):
                    raise TypeError(f"cached {collection_name} row is malformed")
                if row.get("bonding_curve") is None:
                    mint = Pubkey.from_string(row["mint"])
                    row["bonding_curve"] = str(derive_bonding_curve_pda(mint)[0])

        identity = data.get("identity")
        if isinstance(identity, dict) and identity.get("is_token") is True:
            mint = identity.get("input")
            if not isinstance(mint, str):
                raise TypeError("cached token identity is malformed")
            if identity.get("bonding_curve") is None:
                identity["bonding_curve"] = str(
                    derive_bonding_curve_pda(Pubkey.from_string(mint))[0]
                )

    @staticmethod
    def _cached_report_has_entity_mints(backfill: EntityBackfillRecord) -> bool:
        """Return whether the cached report contains entity-wide mint discovery."""

        if backfill.report_json is None:
            return False
        payload = json.loads(backfill.report_json)
        return isinstance(payload, dict) and isinstance(
            payload.get("entity_mints"), list
        )

    @staticmethod
    def _deduplicate_observations(
        observations: tuple[RawChainObservation, ...],
    ) -> tuple[RawChainObservation, ...]:
        """Collapse identical provider copies of one finalized signature."""

        unique: dict[bytes | object, RawChainObservation] = {}
        for observation in observations:
            key: bytes | object = (
                observation.signature
                if observation.signature is not None
                else observation.raw_id
            )
            previous = unique.get(key)
            if previous is None:
                unique[key] = observation
                continue
            if (
                previous.slot != observation.slot
                or previous.transaction_index != observation.transaction_index
                or previous.raw_source_payload != observation.raw_source_payload
            ):
                raise ValueError("cached finalized evidence conflicts by signature")
        return tuple(unique.values())

    def _persist_target_scan(
        self,
        *,
        query: str,
        identity: dict[str, object],
        data: dict[str, object],
        scan_ok: bool,
        message: str,
    ) -> None:
        """Persist a bounded summary without treating old graph data as live."""
        evidence_value = data.get("rug_evidence")
        evidence = evidence_value if isinstance(evidence_value, dict) else {}
        timestamp = datetime.now(UTC).isoformat()
        symbol = identity.get("token_symbol")
        name = identity.get("token_name")
        launch_count = evidence.get("launch_count", 0)
        linked_launch_count = evidence.get("linked_launch_count", 0)
        repeat_bundler_mint_count = evidence.get("repeat_bundler_mint_count", 0)
        tracking_address = data.get("tracking_address", identity.get("scan_wallet"))
        self._repository.save_target_scan(
            TargetScanRecord(
                query=query,
                tracking_address=(
                    tracking_address if isinstance(tracking_address, str) else None
                ),
                token_symbol=symbol if isinstance(symbol, str) else None,
                token_name=name if isinstance(name, str) else None,
                scan_ok=scan_ok,
                launch_count=(launch_count if type(launch_count) is int else 0),
                linked_launch_count=(
                    linked_launch_count if type(linked_launch_count) is int else 0
                ),
                repeat_bundler_mint_count=(
                    repeat_bundler_mint_count
                    if type(repeat_bundler_mint_count) is int
                    else 0
                ),
                message=message,
                first_scanned_at=timestamp,
                last_scanned_at=timestamp,
            )
        )

    def subscribe(
        self,
        event_type: str | Callable[[TrackerEvent], None],
        handler: Callable[[TrackerEvent], None] | None = None,
    ) -> Callable[[], None]:
        """Subscribe a callback to the unified event stream."""
        return self._event_bus.subscribe(event_type, handler)

    def execute(self, cmd: BotCommand) -> CommandResult:
        """Route a synchronous BotCommand through the universal COMMAND_REGISTRY."""
        handler = COMMAND_REGISTRY.get(cmd.name)
        if handler is None:
            return CommandResult(ok=False, message=f"unknown command: {cmd.name}")
        res = handler(self, cmd)
        if inspect.isawaitable(res):
            raise RuntimeError(f"command {cmd.name} requires async execution")
        return res

    async def aexecute(self, cmd: BotCommand) -> CommandResult:
        """Route an async or sync BotCommand through the universal COMMAND_REGISTRY."""
        handler = COMMAND_REGISTRY.get(cmd.name)
        if handler is None:
            return CommandResult(ok=False, message=f"unknown command: {cmd.name}")
        res = handler(self, cmd)
        if inspect.isawaitable(res):
            return await res
        return res

    def watch(self, funder_address: str, label: str = "") -> CommandResult:
        """Register a new root funder for descendant tracking."""
        try:
            self._service.add_funder(funder_address, label=label)
            return CommandResult(ok=True, message=f"watching {funder_address}")
        except Exception as exc:
            return CommandResult(ok=False, message=str(exc))

    def unwatch(self, funder_address: str) -> CommandResult:
        """Disable descendant tracking for a root funder."""
        try:
            self._service.remove_funder(funder_address)
            return CommandResult(ok=True, message=f"unwatched {funder_address}")
        except Exception as exc:
            return CommandResult(ok=False, message=str(exc))

    def get_funder(self, funder_address: str) -> FunderRecord | None:
        """Return a registered funder record by address."""
        return self._repository.get_funder(funder_address)

    def get_funders(self) -> list[FunderRecord]:
        """Return all registered root funders."""
        return self._repository.get_funders()

    def funders(self) -> list[FunderRecord]:
        """Alias for get_funders."""
        return self._repository.get_funders()

    def get_wallet(self, address: str) -> WalletRecord | None:
        """Return a tracked wallet node by address."""
        return self._repository.get_wallet(address)

    def get_wallets(self) -> list[WalletRecord]:
        """Return all tracked descendant wallet nodes."""
        return self._repository.get_wallets()

    def wallets(self) -> list[WalletRecord]:
        """Alias for get_wallets."""
        return self._repository.get_wallets()

    def get_descendant_wallets(self, funder_address: str) -> list[WalletRecord]:
        """Return all descendant wallets funded by a root funder."""
        return self._repository.get_wallets_by_root_funder(funder_address)

    def get_launch(self, mint: str) -> LaunchRecord | None:
        """Return a tracked launch record by mint address."""
        return self._repository.get_launch(mint)

    def get_launches(self) -> list[LaunchRecord]:
        """Return all tracked token creation events."""
        return self._repository.get_launches()

    def launches(self) -> list[LaunchRecord]:
        """Alias for get_launches."""
        return self._repository.get_launches()

    def get_launches_for_funder(self, funder_address: str) -> list[LaunchRecord]:
        """Return all launches attributed to a root funder."""
        return self._repository.get_launches_by_root_funder(funder_address)

    def get_transfers(self) -> list[TransferRecord]:
        """Return all observed funding transfer edges."""
        return self._repository.get_transfers()

    def transfers(self) -> list[TransferRecord]:
        """Alias for get_transfers."""
        return self._repository.get_transfers()

    def get_transfers_for_funder(self, funder_address: str) -> list[TransferRecord]:
        """Return all transfers belonging to a root funder tree."""
        return self._repository.get_transfers_by_root_funder(funder_address)

    def get_summary_stats(self) -> dict[str, int]:
        """Return counts of tracked funders, wallets, launches, and transfers."""
        return {
            "funders_count": len(self._repository.get_funders()),
            "wallets_count": len(self._repository.get_wallets()),
            "launches_count": len(self._repository.get_launches()),
            "transfers_count": len(self._repository.get_transfers()),
        }

    def targets(self) -> list[TargetRecord]:
        """Return all target entities with their execution policy and performance."""
        funders = self._repository.get_funders()
        records: list[TargetRecord] = []
        for f in funders:
            policy = self._repository.get_target_execution_policy(f.address)
            launches = self._repository.get_launches_by_root_funder(f.address)
            records.append(
                TargetRecord(
                    address=f.address,
                    label=f.label or "Target Dev",
                    policy=policy,
                    launches_count=len(launches),
                )
            )
        return records

    def target_scans(self) -> list[TargetScanRecord]:
        """Return persistent target scan summaries, newest first."""
        return list(self._repository.get_target_scans())

    def cached_entity_report(self, query: str) -> CommandResult:
        """Return the latest durable entity report without provider access."""

        backfill = self._repository.get_entity_backfill(query.strip())
        if backfill is None or backfill.report_json is None:
            return CommandResult(ok=False, message="no cached entity report")
        return self._command_from_cached_report(backfill)

    def get_target_execution_policy(
        self, funder_address: str
    ) -> TargetExecutionPolicy | None:
        """Return the target execution policy assigned to a funder."""
        return self._repository.get_target_execution_policy(funder_address)

    def save_target_execution_policy(self, policy: TargetExecutionPolicy) -> None:
        """Persist or update a target execution policy."""
        self._repository.save_target_execution_policy(policy)

    def set_target_mode(
        self, funder_address: str, mode: TargetExecutionMode
    ) -> CommandResult:
        """Set the target execution mode (OFF, SIMULATED, LIVE)."""
        policy = self._repository.get_target_execution_policy(funder_address)
        if policy is None:
            return CommandResult(ok=False, message=f"target {funder_address} not found")
        updated = TargetExecutionPolicy(
            funder_address=policy.funder_address,
            monitoring_enabled=policy.monitoring_enabled
            if mode != TargetExecutionMode.OFF
            else False,
            execution_mode=mode,
            quote_size_lamports=policy.quote_size_lamports,
            take_profit_pnl_ppm=policy.take_profit_pnl_ppm,
            stop_loss_pnl_ppm=policy.stop_loss_pnl_ppm,
            max_slippage_bps=policy.max_slippage_bps,
            priority_fee_microlamports=policy.priority_fee_microlamports,
            jito_tip_lamports=policy.jito_tip_lamports,
            updated_at=datetime.now(UTC).isoformat(),
        )
        self._repository.save_target_execution_policy(updated)
        return CommandResult(ok=True, message=f"target mode set to {mode.value}")

    def toggle_kill_switch(self) -> CommandResult:
        """Toggle the daemon safety kill switch."""
        if self._sniper_daemon is None:
            return CommandResult(ok=False, message="sniper daemon not attached")
        active = self._sniper_daemon.toggle_kill_switch()
        return CommandResult(ok=True, message=f"kill switch active={active}")

    def snapshot(self) -> SniperDaemonSnapshot | None:
        """Return point-in-time snapshot of the sniper daemon."""
        if self._sniper_daemon is None:
            return None
        return self._sniper_daemon.snapshot()

    def positions(self) -> tuple[PaperPositionState, ...]:
        """Return all open positions."""
        if self._sniper_daemon is None:
            return ()
        return self._sniper_daemon.open_positions

    async def sell(self, market_id: str, exit_ppm: int) -> CommandResult:
        """Execute a manual position exit."""
        if self._sniper_daemon is None:
            return CommandResult(ok=False, message="sniper daemon not attached")
        try:
            await self._sniper_daemon.exit_position_manual(market_id, exit_ppm)
            return CommandResult(ok=True, message=f"exited position {market_id}")
        except Exception as exc:
            return CommandResult(ok=False, message=str(exc))

    async def discover(self, mint_or_wallet: str) -> CommandResult:
        """Discover the root funder behind a mint or wallet."""
        try:
            result = await discover_funder(
                mint_or_wallet,
                repository=self._repository,
                endpoint=self._endpoint,
                fallback_endpoints=self._fallback_endpoints,
            )
            return CommandResult(
                ok=True, message=f"funder: {result.root_funder}", data=result
            )
        except Exception as exc:
            return CommandResult(ok=False, message=str(exc))

    def get_cluster_intel(
        self, root_funder: str, root_label: str | None = None
    ) -> ClusterIntelligenceModel:
        """Build a complete cluster intelligence model."""
        return build_cluster_intelligence_model(
            self._repository, root_funder, root_label=root_label
        )

    def get_cluster_intelligence(
        self, root_funder: str, root_label: str | None = None
    ) -> ClusterIntelligenceModel:
        """Build a complete cluster intelligence model (alias)."""
        return build_cluster_intelligence_model(
            self._repository, root_funder, root_label=root_label
        )


def build_ui_runtime(  # noqa: PLR0913
    *,
    state_dir: Path,
    wallet: str | None = None,
    config_path: Path | None = None,
    sniper_runtime: SniperRuntime | None = None,
    sniper_daemon: SniperDaemonService | None = None,
    endpoint: str | None = None,
    fallback_endpoints: tuple[str, ...] | None = None,
    websocket_endpoint: str | None = None,
    transport: RpcHttpTransport | None = None,
) -> RugbotApp:
    """Build the unified RugbotApp runtime."""
    resolve_dotenv()
    if sniper_daemon is not None and sniper_runtime is not None:
        raise ValueError("inject either sniper_daemon or sniper_runtime, not both")
    daemon = sniper_runtime.daemon if sniper_runtime is not None else sniper_daemon

    db = DatabaseManager(state_dir / "rugbot.db")
    repository = SQLiteTrackerRepository(db)
    engine = TrackerEngine(clock=SystemClock())
    event_bus = EventBus()
    service = TrackerService(engine, repository, event_bus)
    providers = load_provider_settings()
    resolved_endpoint = endpoint or providers.rpc_http
    resolved_fallback_endpoints = (
        providers.rpc_http_fallbacks
        if fallback_endpoints is None
        else fallback_endpoints
    )
    resolved_transport = (
        transport
        if transport is not None
        else (
            RpcProviderPool((resolved_endpoint, *resolved_fallback_endpoints))
            if resolved_endpoint and resolved_fallback_endpoints
            else None
        )
    )
    screener = ScreenerService(
        tracker_service=service,
        endpoint=resolved_endpoint,
        fallback_endpoints=resolved_fallback_endpoints,
    )
    resolved_websocket_endpoint = websocket_endpoint or _resolve_websocket_endpoint(
        resolved_endpoint
    )
    launch_observation = None
    if resolved_endpoint:
        launch_observation = TrackedLaunchObservationProducer(
            service=service,
            repository=repository,
            endpoint=resolved_endpoint,
            websocket_endpoint=resolved_websocket_endpoint,
            pumpportal_stream=PumpPortalLaunchStream(),
            global_launch_handler=screener.nominate_live_launch,
            transport=resolved_transport,
        )

    app = RugbotApp(
        engine=engine,
        repository=repository,
        event_bus=event_bus,
        service=service,
        database=db,
        screener=screener,
        launch_observation=launch_observation,
        sniper_runtime=sniper_runtime,
        sniper_daemon=daemon,
        owns_sniper=False,
        endpoint=resolved_endpoint,
        fallback_endpoints=resolved_fallback_endpoints,
        solscan_api_key=providers.solscan_api_key,
        state_dir=state_dir,
    )
    if config_path is not None:
        _seed_configured_target(repository, service, config_path)
    elif wallet is not None and repository.get_funder(wallet) is None:
        service.add_funder(wallet, label="Configured target")
    return app


def _resolve_websocket_endpoint(http_endpoint: str | None) -> str | None:
    wss_env = load_provider_settings().rpc_websocket
    if wss_env:
        return wss_env
    if not http_endpoint:
        return None
    parsed = urlsplit(http_endpoint)
    scheme = "wss" if parsed.scheme == "https" else "ws"
    return urlunsplit(
        (scheme, parsed.netloc, parsed.path, parsed.query, parsed.fragment)
    )


def _normalize_execution_mode(mode: object) -> TargetExecutionMode:
    if isinstance(mode, TargetExecutionMode):
        return mode
    val = str(mode).lower()
    if val in ("observe", "off"):
        return TargetExecutionMode.OFF
    if val in ("live",):
        return TargetExecutionMode.LIVE
    return TargetExecutionMode.SIMULATED


def _seed_configured_target(
    repository: SQLiteTrackerRepository, service: TrackerService, config_path: Path
) -> None:
    try:
        cfg = load_sniper_config(config_path)
    except SniperConfigError:
        return
    funder = getattr(cfg.target, "funder_address", None) or getattr(
        cfg.target, "id", None
    )
    if not funder or funder == SYSTEM_PROGRAM:
        return
    if repository.get_funder(funder) is None:
        service.add_funder(funder, label="Configured target")
    raw_mode = getattr(cfg.target, "execution_mode", None) or (
        cfg.execution.mode.value if hasattr(cfg, "execution") else "simulated"
    )
    exec_mode = _normalize_execution_mode(raw_mode)
    quote_size = getattr(cfg.target, "quote_size_lamports", None) or (
        cfg.execution.quote_size_lamports if hasattr(cfg, "execution") else 10_000_000
    )
    policy = TargetExecutionPolicy(
        funder_address=funder,
        monitoring_enabled=getattr(cfg.target, "monitoring_enabled", True),
        execution_mode=exec_mode,
        quote_size_lamports=quote_size,
        take_profit_pnl_ppm=getattr(cfg.target, "take_profit_pnl_ppm", 1_000_000),
        stop_loss_pnl_ppm=getattr(cfg.target, "stop_loss_pnl_ppm", -300_000),
        max_slippage_bps=getattr(cfg.target, "max_slippage_bps", 500),
        priority_fee_microlamports=getattr(
            cfg.target, "priority_fee_microlamports", 50_000
        ),
        jito_tip_lamports=getattr(cfg.target, "jito_tip_lamports", 1_000_000),
        updated_at=datetime.now(UTC).isoformat(),
    )
    repository.save_target_execution_policy(policy)


__all__ = [
    "RugbotApp",
    "build_ui_runtime",
]
