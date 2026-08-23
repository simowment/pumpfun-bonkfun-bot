"""Build point-in-time trajectories from finalized Pump trade events."""

# This producer is deliberately pure.  RPC payload parsing belongs to the
# observation/fill boundary; this module only consumes typed, finalized proof.
# ruff: noqa: PLR0911, PLR2004

from __future__ import annotations

from dataclasses import dataclass

from rugbot.backtest.trajectory.finalized_trade_builder import PumpTradeEventProof
from rugbot.backtest.trajectory.outcome_builder import FinalizedOutcomePointInput
from rugbot.domain.adverse_event import MarketTrajectoryPoint
from rugbot.domain.amounts import QuoteBaseUnits, Slot, TokenBaseUnits
from rugbot.domain.create_state_adapter import PumpCreateMintMetadataProof
from rugbot.domain.decisions import AbstainReason, AbstainResult
from rugbot.domain.fees import FeeConfig
from rugbot.domain.migration import PUMP_AMM_PROGRAM_ID
from rugbot.domain.observations import RawChainObservation
from rugbot.domain.quote_engine import (
    PUMP_SWAP_POOL_DECODER_VERSION,
    PoolReserves,
    executable_sell_quote,
)
from rugbot.domain.quotes import QuotePath
from rugbot.domain.version_registry import PumpProtocolVersionSnapshot
from rugbot.ingest.pump.bonding_curve_account import (
    PINNED_PUMP_IDL_SHA256,
    PUMP_BONDING_CURVE_ACCOUNT_DECODER_VERSION,
    PUMP_PROGRAM_ID,
)
from rugbot.ingest.pump.create_event_decoder import SOL_PUBKEY
from rugbot.ingest.pump.swap_trade_decoder import PINNED_PUMP_SWAP_IDL_SHA256

PRICE_PPM_DENOMINATOR = 1_000_000
MAX_SUPPORTED_DECIMALS = 18


@dataclass(frozen=True, slots=True)
class TradeEventTrajectoryMetadataProof:
    """Non-market proof needed to place one TradeEvent on a trajectory."""

    as_of_slot: Slot
    event_index: int
    trajectory_start_timestamp: int
    curve_completed: bool
    migration_observed: bool
    full_exit_base_amount_base_units: TokenBaseUnits | None
    protocol_snapshot: PumpProtocolVersionSnapshot | PumpTradeEventProtocolProof | None
    mint_metadata: PumpCreateMintMetadataProof | None
    evidence_ids: tuple[str, ...]
    quote_path: QuotePath = QuotePath.PUMP_BONDING_CURVE


@dataclass(frozen=True, slots=True)
class TradeEventTrajectorySource:
    """One immutable finalized event plus its explicit point-in-time proof."""

    observation: RawChainObservation
    event: PumpTradeEventProof
    metadata: TradeEventTrajectoryMetadataProof


@dataclass(frozen=True, slots=True)
class PumpTradeEventProtocolProof:
    """Protocol and fee proof carried by one finalized Pump TradeEvent."""

    as_of_slot: Slot
    program_id: str
    idl_hash: str
    program_config_version: str
    fee_config: FeeConfig
    source_artifact: str
    evidence_ids: tuple[str, ...]


TradeEventTrajectoryPointResult = FinalizedOutcomePointInput | AbstainResult
TradeEventTrajectoryResult = tuple[FinalizedOutcomePointInput, ...] | AbstainResult


def build_trade_event_trajectory_point(
    *,
    source: TradeEventTrajectorySource,
    as_of_slot: Slot,
) -> TradeEventTrajectoryPointResult:
    """Build one market point and executable full-exit quote input.

    The reserve snapshot, fee schedule, mint decimals, and event placement are
    all required proofs.  No Pump or quote defaults are inferred.
    """

    cutoff = _safe_slot(as_of_slot)
    if type(as_of_slot) is not int or as_of_slot < 0:
        return _abstain(
            AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
            "trajectory cutoff slot must be a non-negative integer",
            cutoff,
        )
    if type(source) is not TradeEventTrajectorySource:
        return _abstain(
            AbstainReason.MISSING_FEATURE,
            "typed TradeEvent trajectory source is required",
            cutoff,
        )

    observation = source.observation
    event = source.event
    metadata = source.metadata
    validation = _validate_source(source, cutoff)
    if validation is not None:
        return validation

    snapshot = metadata.protocol_snapshot
    mint_metadata = metadata.mint_metadata
    full_exit_amount = metadata.full_exit_base_amount_base_units
    if snapshot is None or mint_metadata is None or full_exit_amount is None:
        raise AssertionError

    reserves = PoolReserves(
        virtual_base_reserves=TokenBaseUnits(event.virtual_token_reserves_base_units),
        virtual_quote_reserves=QuoteBaseUnits(event.virtual_sol_reserves_base_units),
        real_base_reserves=TokenBaseUnits(event.real_token_reserves_base_units),
        real_quote_reserves=QuoteBaseUnits(event.real_sol_reserves_base_units),
        is_complete=metadata.curve_completed,
        as_of_slot=Slot(observation.slot),
        base_decimals=mint_metadata.base_decimals,
        quote_decimals=mint_metadata.quote_decimals,
        decoder_version=(
            PUMP_BONDING_CURVE_ACCOUNT_DECODER_VERSION
            if metadata.quote_path is QuotePath.PUMP_BONDING_CURVE
            else PUMP_SWAP_POOL_DECODER_VERSION
        ),
        idl_hash=snapshot.idl_hash,
        program_config_version=snapshot.program_config_version,
    )
    full_exit_quote = executable_sell_quote(
        path=metadata.quote_path,
        reserves=reserves,
        base_input_amount=full_exit_amount,
        fee_config=snapshot.fee_config,
    )
    if isinstance(full_exit_quote, AbstainResult):
        return full_exit_quote

    elapsed_ms = (event.timestamp - metadata.trajectory_start_timestamp) * 1_000
    price_ppm = (
        event.sol_amount_base_units
        * PRICE_PPM_DENOMINATOR
        // event.token_amount_base_units
    )
    market_state = MarketTrajectoryPoint(
        as_of_slot=Slot(observation.slot),
        slot=Slot(observation.slot),
        event_index=metadata.event_index,
        elapsed_ms=elapsed_ms,
        price_quote_base_units_per_token_base_unit_ppm=price_ppm,
        real_quote_reserves_base_units=QuoteBaseUnits(
            event.real_sol_reserves_base_units
        ),
        curve_progress_ppm=None,
    )
    return FinalizedOutcomePointInput(
        observation=observation,
        market_state=market_state,
        full_exit_quote=full_exit_quote,
        curve_completed=metadata.curve_completed,
        migration_observed=metadata.migration_observed,
        evidence_ids=metadata.evidence_ids,
    )


def build_trade_event_trajectory(
    *,
    sources: tuple[TradeEventTrajectorySource, ...],
    as_of_slot: Slot,
) -> TradeEventTrajectoryResult:
    """Build an ordered immutable trajectory from finalized TradeEvents."""

    cutoff = _safe_slot(as_of_slot)
    if type(sources) is not tuple or not sources:
        return _abstain(
            AbstainReason.MISSING_FEATURE,
            "finalized TradeEvent trajectory sources are required",
            cutoff,
        )

    built: list[FinalizedOutcomePointInput] = []
    previous_key: tuple[int, int] | None = None
    previous_elapsed_ms: int | None = None
    seen_evidence_ids: set[str] = set()
    for source in sources:
        point = build_trade_event_trajectory_point(
            source=source,
            as_of_slot=as_of_slot,
        )
        if isinstance(point, AbstainResult):
            return point
        key = (int(point.market_state.slot), point.market_state.event_index)
        if previous_key is not None and key <= previous_key:
            return _abstain(
                AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
                "TradeEvent trajectory sources are not strictly ordered",
                cutoff,
            )
        if (
            previous_elapsed_ms is not None
            and point.market_state.elapsed_ms < previous_elapsed_ms
        ):
            return _abstain(
                AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
                "TradeEvent trajectory elapsed time moves backwards",
                cutoff,
            )
        if seen_evidence_ids.intersection(point.evidence_ids):
            return _abstain(
                AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
                "TradeEvent trajectory evidence IDs must be unique",
                cutoff,
            )
        seen_evidence_ids.update(point.evidence_ids)
        built.append(point)
        previous_key = key
        previous_elapsed_ms = point.market_state.elapsed_ms
    return tuple(built)


def _validate_source(  # noqa: C901
    source: TradeEventTrajectorySource,
    cutoff: int,
) -> AbstainResult | None:
    observation = source.observation
    event = source.event
    metadata = source.metadata
    if type(observation) is not RawChainObservation:
        return _abstain(
            AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
            "raw finalized observation is malformed",
            cutoff,
        )
    if (
        observation.commitment != "finalized"
        or observation.canonical_status != "canonical"
        or observation.source_update_kind != "transaction"
        or type(observation.slot) is not int
        or observation.slot < 0
        or observation.slot > cutoff
        or observation.signature is None
        or observation.transaction_index is None
    ):
        return _abstain(
            AbstainReason.STALE_STATE,
            "TradeEvent evidence must be finalized and within the cutoff",
            cutoff,
        )
    if type(event) is not PumpTradeEventProof:
        return _abstain(
            AbstainReason.MISSING_FEATURE,
            "decoded Pump TradeEvent proof is required",
            cutoff,
        )
    if type(metadata) is not TradeEventTrajectoryMetadataProof:
        return _abstain(
            AbstainReason.MISSING_FEATURE,
            "trajectory metadata proof is required",
            cutoff,
        )
    if metadata.as_of_slot != observation.slot:
        return _abstain(
            AbstainReason.STALE_STATE,
            "trajectory metadata and TradeEvent use different slots",
            cutoff,
        )
    if type(metadata.event_index) is not int or metadata.event_index < 0:
        return _abstain(
            AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
            "trajectory event index is malformed",
            cutoff,
        )
    if (
        observation.event_ordinal is not None
        and observation.event_ordinal != metadata.event_index
    ):
        return _abstain(
            AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
            "trajectory event index conflicts with the observation",
            cutoff,
        )
    if (
        type(metadata.trajectory_start_timestamp) is not int
        or metadata.trajectory_start_timestamp < 0
        or type(event.timestamp) is not int
        or event.timestamp < metadata.trajectory_start_timestamp
    ):
        return _abstain(
            AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
            "TradeEvent timestamp proof is malformed",
            cutoff,
        )
    if (
        type(metadata.curve_completed) is not bool
        or type(metadata.migration_observed) is not bool
        or not isinstance(metadata.quote_path, QuotePath)
    ):
        return _abstain(
            AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
            "trajectory protocol state flags must be explicit booleans",
            cutoff,
        )
    if (
        type(metadata.full_exit_base_amount_base_units) is not int
        or metadata.full_exit_base_amount_base_units <= 0
    ):
        return _abstain(
            AbstainReason.MISSING_FEATURE,
            "positive full-exit position size proof is required",
            cutoff,
        )
    evidence_error = _validate_evidence_ids(metadata.evidence_ids, cutoff)
    if evidence_error is not None:
        return evidence_error
    reserve_error = _validate_event_reserves(event, cutoff)
    if reserve_error is not None:
        return reserve_error
    return _validate_fee_and_metadata(source, cutoff)


def _validate_event_reserves(
    event: PumpTradeEventProof,
    cutoff: int,
) -> AbstainResult | None:
    values = (
        event.sol_amount_base_units,
        event.token_amount_base_units,
        event.virtual_sol_reserves_base_units,
        event.virtual_token_reserves_base_units,
        event.real_sol_reserves_base_units,
        event.real_token_reserves_base_units,
    )
    if any(type(value) is not int or value <= 0 for value in values):
        return _abstain(
            AbstainReason.UNKNOWN_PROTOCOL_STATE,
            "TradeEvent reserve or executed amount state is missing",
            cutoff,
        )
    return None


def _validate_fee_and_metadata(  # noqa: C901
    source: TradeEventTrajectorySource,
    cutoff: int,
) -> AbstainResult | None:
    metadata = source.metadata
    event = source.event
    snapshot = metadata.protocol_snapshot
    mint_metadata = metadata.mint_metadata
    if not isinstance(
        snapshot, (PumpProtocolVersionSnapshot, PumpTradeEventProtocolProof)
    ):
        return _abstain(
            AbstainReason.UNKNOWN_FEE_CONFIG,
            "point-in-time Pump fee and protocol proof is required",
            cutoff,
        )
    if (
        snapshot.as_of_slot != source.observation.slot
        or snapshot.program_id
        != (
            PUMP_PROGRAM_ID
            if metadata.quote_path is QuotePath.PUMP_BONDING_CURVE
            else PUMP_AMM_PROGRAM_ID
        )
        or snapshot.idl_hash
        != (
            PINNED_PUMP_IDL_SHA256
            if metadata.quote_path is QuotePath.PUMP_BONDING_CURVE
            else PINNED_PUMP_SWAP_IDL_SHA256
        )
        or not snapshot.program_config_version
    ):
        return _abstain(
            AbstainReason.UNKNOWN_PROTOCOL_STATE,
            "Pump protocol proof is missing or mismatched",
            cutoff,
        )
    if (
        isinstance(snapshot, PumpProtocolVersionSnapshot)
        and not snapshot.registry_version
    ):
        return _abstain(
            AbstainReason.UNKNOWN_PROTOCOL_STATE,
            "Pump protocol registry proof is missing",
            cutoff,
        )
    if isinstance(snapshot, PumpTradeEventProtocolProof):
        if not snapshot.source_artifact:
            return _abstain(
                AbstainReason.UNKNOWN_PROTOCOL_STATE,
                "Pump TradeEvent protocol source proof is missing",
                cutoff,
            )
        evidence_error = _validate_evidence_ids(snapshot.evidence_ids, cutoff)
        if evidence_error is not None:
            return evidence_error
    fee = snapshot.fee_config
    if (
        type(fee) is not FeeConfig
        or type(fee.version) is not str
        or not fee.version
        or fee.is_known is not True
        or fee.program_config_version != snapshot.program_config_version
        or type(fee.protocol_fee_bps) is not int
        or type(fee.creator_fee_bps) is not int
        or fee.protocol_fee_bps < 0
        or fee.creator_fee_bps < 0
        or fee.protocol_fee_bps + fee.creator_fee_bps > 10_000
    ):
        return _abstain(
            AbstainReason.UNKNOWN_FEE_CONFIG,
            "historical Pump fee configuration is unknown or malformed",
            cutoff,
        )
    if (
        event.protocol_fee_basis_points != fee.protocol_fee_bps
        or event.creator_fee_basis_points != fee.creator_fee_bps
    ):
        return _abstain(
            AbstainReason.UNKNOWN_FEE_CONFIG,
            "TradeEvent fee rates conflict with the historical Pump fee snapshot",
            cutoff,
        )
    if (
        fee.valid_from_slot is None
        or type(fee.valid_from_slot) is not int
        or fee.valid_from_slot < 0
        or (
            fee.valid_to_slot is not None
            and (
                type(fee.valid_to_slot) is not int
                or fee.valid_to_slot <= fee.valid_from_slot
            )
        )
        or type(fee.source_artifact_version) is not str
        or not fee.source_artifact_version
    ):
        return _abstain(
            AbstainReason.UNKNOWN_FEE_CONFIG,
            "historical Pump fee configuration is unknown or malformed",
            cutoff,
        )
    if type(mint_metadata) is not PumpCreateMintMetadataProof:
        return _abstain(
            AbstainReason.MISSING_FEATURE,
            "finalized mint metadata proof is required",
            cutoff,
        )
    if (
        mint_metadata.as_of_slot > source.observation.slot
        or mint_metadata.base_mint_pubkey != event.mint
        or mint_metadata.quote_mint_pubkey != SOL_PUBKEY
        or not mint_metadata.source_artifact
        or not _valid_decimals(mint_metadata.base_decimals)
        or mint_metadata.quote_decimals != 9
    ):
        return _abstain(
            AbstainReason.UNKNOWN_PROTOCOL_STATE,
            "finalized mint metadata does not prove the Pump quote context",
            cutoff,
        )
    return None


def _validate_evidence_ids(
    evidence_ids: tuple[str, ...],
    cutoff: int,
) -> AbstainResult | None:
    if (
        type(evidence_ids) is not tuple
        or not evidence_ids
        or any(type(value) is not str or not value for value in evidence_ids)
        or len(set(evidence_ids)) != len(evidence_ids)
    ):
        return _abstain(
            AbstainReason.MISSING_FEATURE,
            "immutable trajectory evidence IDs are required",
            cutoff,
        )
    return None


def _valid_decimals(value: object) -> bool:
    return type(value) is int and 0 <= value <= MAX_SUPPORTED_DECIMALS


def _safe_slot(value: object) -> int:
    return value if type(value) is int else -1


def _abstain(reason: AbstainReason, message: str, as_of_slot: int) -> AbstainResult:
    return AbstainResult(reason=reason, message=message, as_of_slot=as_of_slot)


__all__ = [
    "PumpTradeEventProtocolProof",
    "TradeEventTrajectoryMetadataProof",
    "TradeEventTrajectoryPointResult",
    "TradeEventTrajectoryResult",
    "TradeEventTrajectorySource",
    "build_trade_event_trajectory",
    "build_trade_event_trajectory_point",
]
