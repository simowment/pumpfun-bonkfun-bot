"""Bounded finalized evidence acquisition for one operator and its mints.

This module is deliberately an acquisition boundary.  It fetches immutable
finalized transaction observations and uses the pinned Pump create decoder to
discover launch mints that are explicitly attributed to the target wallet.
It does not construct account state, fills, outcomes, or copy-trade cases.
Those require additional point-in-time proofs and remain the responsibility of
the pure backtest pipeline.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

import base58

from rugbot.domain.decisions import AbstainReason, AbstainResult
from rugbot.domain.observations import RawChainObservation
from rugbot.ingest.pump.pump_create_observation import (
    decode_pump_create_mint_metadata_observation,
    decode_pump_create_v2_observation,
)
from rugbot.ingest.pump.pump_migration_observation import (
    decode_pump_migration_observation,
)
from rugbot.ingest.pump.pump_swap_event_observation import (
    decode_pump_swap_events_observation,
)
from rugbot.ingest.rpc_observer import (
    MAX_PAGES,
    MAX_TRANSACTIONS,
    RpcObservationResult,
    observe_address,
)
from rugbot.storage.jsonl_observation_store import observation_identity

if TYPE_CHECKING:
    from sol_trade_sdk.solana.provider_pool import RpcHttpTransport

    from rugbot.backtest.dataset import FinalizedTrade
    from rugbot.domain.amounts import Slot
    from rugbot.domain.launches import LaunchCreatedV2
    from rugbot.domain.metadata_resolver import (
        PumpFinalizedMintMetadataEvidence,
    )
    from rugbot.domain.migrations import PumpMigrationInstructionEvidence
    from rugbot.domain.trades import PumpSwapTradeEventEvidence


DEFAULT_MAX_TRANSACTIONS_PER_ADDRESS = MAX_TRANSACTIONS
DEFAULT_MAX_LAUNCH_MINTS = 20
MAX_LAUNCH_MINTS = 100
SOLANA_PUBKEY_BYTES = 32


@dataclass(frozen=True, slots=True)
class FinalizedRpcCaseAcquisition:
    """Immutable finalized evidence collected for one target wallet.

    ``launch_mints`` contains caller-supplied mints together with mints found
    in a pinned ``create_v2`` transaction whose explicit actor proof names the
    target wallet.  Caller-supplied mints are observed but are not treated as
    wallet-attributed launches merely because they were supplied.

    ``launches`` contains only decoded, finalized ``LaunchCreatedV2`` values
    from the target wallet history.  No account state, protocol snapshot,
    executed fill, or outcome is synthesized here. ``migration_events``
    contains only migrations proven by the pinned finalized transaction
    decoder; it is not treated as a canonical pool-state proof by itself.
    """

    operator_wallet: str
    as_of_slot: Slot
    launch_mints: tuple[str, ...]
    launches: tuple[LaunchCreatedV2, ...]
    mint_metadata: tuple[PumpFinalizedMintMetadataEvidence, ...]
    observations: tuple[RawChainObservation, ...]
    pump_swap_events: tuple[PumpSwapTradeEventEvidence, ...] = ()
    migration_events: tuple[PumpMigrationInstructionEvidence, ...] = ()


RpcCaseAcquisitionResult = FinalizedRpcCaseAcquisition | AbstainResult


def build_rpc_case_proofs(  # noqa: PLR0913
    *,
    acquisition: FinalizedRpcCaseAcquisition,
    trades: tuple[FinalizedTrade, ...],
    as_of_slot: Slot,
    fixed_entry_quote_base_units: int = 1_000_000,
    horizon_ms: int = 0,
    labeler_version: str = "pump-trade-event-outcome",
    detector_version: str = "pump-trade-event-collapse",
) -> object:
    """Build typed case proofs from finalized acquisition evidence."""

    from rugbot.backtest.cases.rpc_case_builder import (  # noqa: PLC0415
        build_rpc_case_proofs as build,
    )

    return build(
        acquisition=acquisition,
        trades=trades,
        as_of_slot=as_of_slot,
        fixed_entry_quote_base_units=fixed_entry_quote_base_units,
        horizon_ms=horizon_ms,
        labeler_version=labeler_version,
        detector_version=detector_version,
    )


async def acquire_finalized_rpc_case_observations(  # noqa: C901, PLR0911, PLR0912, PLR0913, PLR0915
    *,
    operator_wallet: str,
    endpoint: str,
    start_slot: Slot = 0,
    as_of_slot: Slot,
    launch_mints: tuple[str, ...] = (),
    max_transactions_per_address: int = DEFAULT_MAX_TRANSACTIONS_PER_ADDRESS,
    max_launch_mints: int = DEFAULT_MAX_LAUNCH_MINTS,
    max_pages: int = MAX_PAGES,
    source_id: str = "solana-http-rpc",
    observer_id: str = "backtest-rpc-case-acquisition",
    transport: RpcHttpTransport | None = None,
) -> RpcCaseAcquisitionResult:
    """Acquire one bounded, finalized wallet-and-mint evidence bundle.

    The target wallet is observed first.  Pinned ``create_v2`` decoding is
    used only to discover additional mints when the decoded launch explicitly
    names ``operator_wallet`` as creator, user, fee payer, or first buyer.
    Every selected mint is then observed through the same finalized HTTP
    observer.  All observations are merged by the repository's canonical
    identity, which deliberately excludes ingestion UUIDs and runtime timing.

    ``as_of_slot`` is an inclusive point-in-time cutoff.  A successful result
    carries exactly that cutoff, and every abstention produced by this helper
    is rebased to it.  The helper never falls back to current account state or
    guesses a missing protocol layout.
    """

    validation = _validate_request(
        operator_wallet=operator_wallet,
        endpoint=endpoint,
        start_slot=start_slot,
        as_of_slot=as_of_slot,
        launch_mints=launch_mints,
        max_transactions_per_address=max_transactions_per_address,
        max_launch_mints=max_launch_mints,
        max_pages=max_pages,
        source_id=source_id,
        observer_id=observer_id,
    )
    cutoff = _safe_slot(as_of_slot)
    if validation is not None:
        return validation

    explicit_mints = _canonical_mints(launch_mints)
    if isinstance(explicit_mints, AbstainResult):
        return _at_cutoff(explicit_mints, cutoff)
    if len(explicit_mints) > max_launch_mints:
        return _abstain(
            AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
            "launch mint set exceeds the acquisition bound",
            cutoff,
        )

    observations_by_identity: dict[object, RawChainObservation] = {}
    mint_metadata_by_mint: dict[str, PumpFinalizedMintMetadataEvidence] = {}
    operator_result = await _observe(
        address=operator_wallet,
        endpoint=endpoint,
        start_slot=start_slot,
        as_of_slot=cutoff,
        max_transactions_per_address=max_transactions_per_address,
        max_pages=max_pages,
        source_id=source_id,
        observer_id=f"{observer_id}:operator",
        transport=transport,
    )
    if isinstance(operator_result, AbstainResult):
        return operator_result
    merge_error = _merge_observations(
        observations_by_identity,
        operator_result,
        as_of_slot=cutoff,
    )
    if merge_error is not None:
        return merge_error

    decoded_launches: dict[str, LaunchCreatedV2] = {}
    for observation in operator_result:
        decoded = decode_pump_create_v2_observation(observation)
        if isinstance(decoded, AbstainResult):
            return _at_cutoff(decoded, cutoff)
        if decoded is None or not _launch_mentions_wallet(decoded, operator_wallet):
            continue
        metadata_error = _record_mint_metadata(
            mint_metadata_by_mint,
            observation,
            mint_pubkey=decoded.mint_pubkey,
            as_of_slot=cutoff,
        )
        if metadata_error is not None:
            return metadata_error
        launch_error = _validate_launch(decoded, observation, cutoff)
        if launch_error is not None:
            return launch_error
        existing = decoded_launches.get(decoded.mint_pubkey)
        if existing is not None and existing.launch_id != decoded.launch_id:
            return _abstain(
                AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
                "target wallet history contains ambiguous launches for one mint",
                cutoff,
            )
        decoded_launches[decoded.mint_pubkey] = decoded

    selected_mints = tuple(sorted(set(explicit_mints).union(decoded_launches)))
    if len(selected_mints) > max_launch_mints:
        return _abstain(
            AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
            "launch mint set exceeds the acquisition bound",
            cutoff,
        )

    for mint_index, mint in enumerate(selected_mints):
        mint_result = await _observe(
            address=mint,
            endpoint=endpoint,
            start_slot=start_slot,
            as_of_slot=cutoff,
            max_transactions_per_address=max_transactions_per_address,
            max_pages=max_pages,
            source_id=source_id,
            observer_id=f"{observer_id}:mint:{mint_index}",
            transport=transport,
        )
        if isinstance(mint_result, AbstainResult):
            return mint_result
        merge_error = _merge_observations(
            observations_by_identity,
            mint_result,
            as_of_slot=cutoff,
        )
        if merge_error is not None:
            return merge_error
        for observation in mint_result:
            decoded = decode_pump_create_v2_observation(observation)
            if isinstance(decoded, AbstainResult):
                return _at_cutoff(decoded, cutoff)
            if decoded is None or not _launch_mentions_wallet(decoded, operator_wallet):
                continue
            metadata_error = _record_mint_metadata(
                mint_metadata_by_mint,
                observation,
                mint_pubkey=decoded.mint_pubkey,
                as_of_slot=cutoff,
            )
            if metadata_error is not None:
                return metadata_error
            launch_error = _validate_launch(decoded, observation, cutoff)
            if launch_error is not None:
                return launch_error
            existing = decoded_launches.get(decoded.mint_pubkey)
            if existing is not None and existing.launch_id != decoded.launch_id:
                return _abstain(
                    AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
                    "target wallet history contains ambiguous launches for one mint",
                    cutoff,
                )
            decoded_launches[decoded.mint_pubkey] = decoded

    observations = tuple(
        sorted(observations_by_identity.values(), key=_observation_key)
    )
    pump_swap_events: list[PumpSwapTradeEventEvidence] = []
    migration_events: list[PumpMigrationInstructionEvidence] = []
    for observation in observations:
        decoded_events = decode_pump_swap_events_observation(observation)
        if isinstance(decoded_events, AbstainResult):
            if decoded_events.reason is AbstainReason.MISSING_FEATURE:
                continue
            return _at_cutoff(decoded_events, cutoff)
        pump_swap_events.extend(decoded_events)
        migration = decode_pump_migration_observation(observation)
        if isinstance(migration, AbstainResult):
            if migration.reason is AbstainReason.MISSING_FEATURE:
                continue
            return _at_cutoff(migration, cutoff)
        if migration is not None:
            migration_events.append(migration)
    return FinalizedRpcCaseAcquisition(
        operator_wallet=operator_wallet,
        as_of_slot=cutoff,
        launch_mints=selected_mints,
        launches=tuple(sorted(decoded_launches.values(), key=_launch_key)),
        mint_metadata=tuple(
            sorted(mint_metadata_by_mint.values(), key=lambda item: item.mint_pubkey)
        ),
        observations=observations,
        pump_swap_events=tuple(sorted(pump_swap_events, key=_pump_swap_event_key)),
        migration_events=tuple(
            sorted(
                migration_events,
                key=lambda item: (
                    int(item.as_of_slot),
                    item.transaction_index
                    if item.transaction_index is not None
                    else -1,
                    item.outer_instruction_index,
                ),
            )
        ),
    )


async def _observe(  # noqa: PLR0913
    *,
    address: str,
    endpoint: str,
    start_slot: int,
    as_of_slot: int,
    max_transactions_per_address: int,
    max_pages: int,
    source_id: str,
    observer_id: str,
    transport: RpcHttpTransport | None,
) -> tuple[RawChainObservation, ...] | AbstainResult:
    result: RpcObservationResult = await observe_address(
        address,
        endpoint=endpoint,
        source_id=source_id,
        observer_id=observer_id,
        max_signatures=max_transactions_per_address,
        max_transactions=max_transactions_per_address,
        max_pages=max_pages,
        start_slot=start_slot,
        end_slot=as_of_slot,
        transport=transport,
    )
    if isinstance(result, AbstainResult):
        return _at_cutoff(result, as_of_slot)
    return result


def _merge_observations(
    destination: dict[object, RawChainObservation],
    observations: tuple[RawChainObservation, ...],
    *,
    as_of_slot: int,
) -> AbstainResult | None:
    if type(observations) is not tuple or any(
        type(item) is not RawChainObservation for item in observations
    ):
        return _abstain(
            AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
            "RPC acquisition returned malformed raw observations",
            as_of_slot,
        )
    for observation in observations:
        if (
            observation.commitment != "finalized"
            or observation.canonical_status != "canonical"
        ):
            return _abstain(
                AbstainReason.STALE_STATE,
                "RPC acquisition requires canonical finalized observations",
                as_of_slot,
            )
        if type(observation.slot) is not int or observation.slot < 0:
            return _abstain(
                AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
                "RPC acquisition observation slot is malformed",
                as_of_slot,
            )
        if observation.slot > as_of_slot:
            return _abstain(
                AbstainReason.STALE_STATE,
                "RPC acquisition returned evidence newer than its cutoff",
                as_of_slot,
            )
        destination.setdefault(observation_identity(observation), observation)
    return None


def _record_mint_metadata(
    destination: dict[str, PumpFinalizedMintMetadataEvidence],
    observation: RawChainObservation,
    *,
    mint_pubkey: str,
    as_of_slot: int,
) -> AbstainResult | None:
    metadata = decode_pump_create_mint_metadata_observation(
        observation,
        mint_pubkey=mint_pubkey,
    )
    if isinstance(metadata, AbstainResult):
        if metadata.reason is AbstainReason.MISSING_FEATURE:
            return None
        return _at_cutoff(metadata, as_of_slot)
    existing = destination.get(metadata.mint_pubkey)
    if existing is not None and existing != metadata:
        return _abstain(
            AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
            "finalized mint metadata is contradictory for one mint",
            as_of_slot,
        )
    destination[metadata.mint_pubkey] = metadata
    return None


def _pump_swap_event_key(event: PumpSwapTradeEventEvidence) -> tuple[int, int, int]:
    return (
        int(event.as_of_slot),
        event.event_index,
        0 if event.side.value == "buy" else 1,
    )


def _validate_request(  # noqa: PLR0911, PLR0913
    *,
    operator_wallet: object,
    endpoint: object,
    start_slot: object,
    as_of_slot: object,
    launch_mints: object,
    max_transactions_per_address: object,
    max_launch_mints: object,
    max_pages: object,
    source_id: object,
    observer_id: object,
) -> AbstainResult | None:
    cutoff = _safe_slot(as_of_slot)
    if type(as_of_slot) is not int or as_of_slot < 0:
        return _abstain(
            AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
            "RPC case acquisition cutoff is malformed",
            cutoff,
        )
    if type(start_slot) is not int or start_slot < 0 or start_slot > as_of_slot:
        return _abstain(
            AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
            "RPC case acquisition slot window is malformed",
            cutoff,
        )
    if not _canonical_address(operator_wallet):
        return _abstain(
            AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
            "operator wallet is not a canonical Solana address",
            cutoff,
        )
    if not isinstance(endpoint, str) or not endpoint.strip():
        return _abstain(
            AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
            "RPC endpoint is malformed",
            cutoff,
        )
    if type(launch_mints) is not tuple:
        return _abstain(
            AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
            "launch mints must be an immutable tuple",
            cutoff,
        )
    if (
        type(max_transactions_per_address) is not int
        or not 1 <= max_transactions_per_address <= MAX_TRANSACTIONS
    ):
        return _abstain(
            AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
            "per-address transaction bound is invalid",
            cutoff,
        )
    if (
        type(max_launch_mints) is not int
        or not 1 <= max_launch_mints <= MAX_LAUNCH_MINTS
    ):
        return _abstain(
            AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
            "launch mint bound is invalid",
            cutoff,
        )
    if type(max_pages) is not int or not 1 <= max_pages <= MAX_PAGES:
        return _abstain(
            AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
            "pagination bound is invalid",
            cutoff,
        )
    if not _non_blank(source_id) or not _non_blank(observer_id):
        return _abstain(
            AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
            "RPC observation identifiers are malformed",
            cutoff,
        )
    return None


def _canonical_mints(
    launch_mints: tuple[str, ...],
) -> tuple[str, ...] | AbstainResult:
    if any(not _canonical_address(mint) for mint in launch_mints):
        return _abstain(
            AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
            "launch mint set contains a non-canonical Solana address",
            -1,
        )
    return tuple(sorted(set(launch_mints)))


def _launch_mentions_wallet(launch: LaunchCreatedV2, wallet: str) -> bool:
    return wallet in {
        launch.creator_pubkey,
        launch.user_pubkey,
        launch.fee_payer_pubkey,
        launch.first_buyer_pubkey,
    }


def _validate_launch(
    launch: LaunchCreatedV2,
    observation: RawChainObservation,
    as_of_slot: int,
) -> AbstainResult | None:
    if (
        type(launch.as_of_slot) is not int
        or launch.as_of_slot != observation.slot
        or launch.signature != observation.signature
        or launch.transaction_index != observation.transaction_index
        or launch.as_of_slot > as_of_slot
        or not _canonical_address(launch.mint_pubkey)
    ):
        return _abstain(
            AbstainReason.UNKNOWN_PROTOCOL_STATE,
            "decoded launch proof does not match its finalized observation",
            as_of_slot,
        )
    return None


def _canonical_address(value: object) -> bool:
    if not isinstance(value, str) or not value:
        return False
    try:
        decoded = base58.b58decode(value)
    except ValueError:
        return False
    return (
        len(decoded) == SOLANA_PUBKEY_BYTES
        and base58.b58encode(decoded).decode("ascii") == value
    )


def _non_blank(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _observation_key(observation: RawChainObservation) -> tuple[object, ...]:
    return (
        observation.slot,
        observation.transaction_index
        if observation.transaction_index is not None
        else -1,
        observation.outer_instruction_index
        if observation.outer_instruction_index is not None
        else -1,
        observation.event_ordinal if observation.event_ordinal is not None else -1,
        repr(observation_identity(observation)),
    )


def _launch_key(launch: LaunchCreatedV2) -> tuple[object, ...]:
    return (
        launch.as_of_slot,
        launch.transaction_index if launch.transaction_index is not None else -1,
        launch.outer_instruction_index,
        launch.launch_id,
    )


def _safe_slot(value: object) -> int:
    return value if type(value) is int else -1


def _at_cutoff(result: AbstainResult, as_of_slot: int) -> AbstainResult:
    return AbstainResult(
        reason=result.reason,
        message=result.message,
        as_of_slot=as_of_slot,
    )


def _abstain(
    reason: AbstainReason,
    message: str,
    as_of_slot: int,
) -> AbstainResult:
    return AbstainResult(reason=reason, message=message, as_of_slot=as_of_slot)


__all__ = [
    "DEFAULT_MAX_LAUNCH_MINTS",
    "DEFAULT_MAX_TRANSACTIONS_PER_ADDRESS",
    "FinalizedRpcCaseAcquisition",
    "RpcCaseAcquisitionResult",
    "acquire_finalized_rpc_case_observations",
    "build_rpc_case_proofs",
]
