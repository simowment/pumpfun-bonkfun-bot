"""Build leakage-safe outcome trajectories from finalized typed evidence."""

# The validators deliberately keep each fail-closed branch close to the
# contract it protects.
# ruff: noqa: PLR0911, PLR2004, C901

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from rugbot.domain.adverse_event import MarketTrajectoryPoint
from rugbot.domain.amounts import QuoteBaseUnits, Slot
from rugbot.domain.decisions import AbstainReason, AbstainResult
from rugbot.domain.observations import RawChainObservation
from rugbot.domain.outcome_labels import OutcomeObservationPoint
from rugbot.domain.quotes import ExecutableQuote
from rugbot.storage.jsonl_observation_store import observation_identity


@dataclass(frozen=True, slots=True)
class FinalizedOutcomePointInput:
    """Evidence needed to construct one outcome trajectory point.

    ``full_exit_quote`` must already be produced by the canonical quote
    engine.  This builder only validates and copies it; it never loads market
    state, computes reserves, or performs I/O.
    """

    observation: RawChainObservation
    market_state: MarketTrajectoryPoint
    full_exit_quote: ExecutableQuote
    curve_completed: bool
    migration_observed: bool
    evidence_ids: tuple[str, ...]


OutcomeTrajectoryResult = tuple[OutcomeObservationPoint, ...] | AbstainResult


def build_outcome_observation_point(
    *,
    point: FinalizedOutcomePointInput,
    as_of_slot: Slot,
) -> OutcomeObservationPoint | AbstainResult:
    """Construct one point without consulting any point after its slot."""

    cutoff = _safe_slot(as_of_slot)
    if type(as_of_slot) is not int or as_of_slot < 0:
        return _abstain(
            AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
            "outcome cutoff slot must be a non-negative integer",
            cutoff,
        )
    if type(point) is not FinalizedOutcomePointInput:
        return _abstain(
            AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
            "outcome point input is malformed",
            cutoff,
        )

    validation = _validate_point_input(point, cutoff)
    if validation is not None:
        return validation

    observation_id = _canonical_observation_id(point.observation)
    evidence_error = _validate_supplied_evidence_ids(
        point.evidence_ids,
        cutoff,
        forbidden=observation_id,
    )
    if evidence_error is not None:
        return evidence_error

    return OutcomeObservationPoint(
        as_of_slot=Slot(cutoff),
        slot=point.market_state.slot,
        event_index=point.market_state.event_index,
        elapsed_ms=point.market_state.elapsed_ms,
        price_quote_base_units_per_token_base_unit_ppm=(
            point.market_state.price_quote_base_units_per_token_base_unit_ppm
        ),
        full_exit_output_quote_base_units=QuoteBaseUnits(
            point.full_exit_quote.output_amount_base_units
        ),
        full_exit_execution_cost_quote_base_units=QuoteBaseUnits(
            point.full_exit_quote.fee_amount_base_units
        ),
        curve_progress_ppm=point.market_state.curve_progress_ppm,
        curve_completed=point.curve_completed,
        migration_observed=point.migration_observed,
        evidence_ids=(observation_id, *point.evidence_ids),
    )


def build_outcome_trajectory(
    *,
    points: tuple[FinalizedOutcomePointInput, ...],
    as_of_slot: Slot,
) -> OutcomeTrajectoryResult:
    """Build an ordered immutable trajectory from finalized point inputs.

    Inputs are intentionally consumed in the supplied order.  Reordering
    evidence would conceal a bad upstream join, so slots must be strictly
    increasing and elapsed time must never move backwards.
    """

    cutoff = _safe_slot(as_of_slot)
    if type(as_of_slot) is not int or as_of_slot < 0:
        return _abstain(
            AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
            "outcome cutoff slot must be a non-negative integer",
            cutoff,
        )
    if type(points) is not tuple or not points:
        return _abstain(
            AbstainReason.MISSING_FEATURE,
            "finalized outcome points are required",
            cutoff,
        )

    built: list[OutcomeObservationPoint] = []
    previous_slot: int | None = None
    previous_elapsed_ms: int | None = None
    seen_evidence_ids: set[str] = set()
    for point_input in points:
        point = build_outcome_observation_point(
            point=point_input,
            as_of_slot=Slot(cutoff),
        )
        if isinstance(point, AbstainResult):
            return point
        if previous_slot is not None and point.slot <= previous_slot:
            return _abstain(
                AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
                "outcome trajectory slots must be strictly increasing",
                cutoff,
            )
        if previous_elapsed_ms is not None and point.elapsed_ms < previous_elapsed_ms:
            return _abstain(
                AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
                "outcome trajectory elapsed time moves backwards",
                cutoff,
            )
        if seen_evidence_ids.intersection(point.evidence_ids):
            return _abstain(
                AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
                "outcome trajectory evidence IDs must be unique",
                cutoff,
            )
        seen_evidence_ids.update(point.evidence_ids)
        built.append(point)
        previous_slot = int(point.slot)
        previous_elapsed_ms = point.elapsed_ms
    return tuple(built)


def _validate_point_input(
    point: FinalizedOutcomePointInput,
    cutoff: int,
) -> AbstainResult | None:
    observation = point.observation
    market_state = point.market_state
    quote = point.full_exit_quote

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
    ):
        return _abstain(
            AbstainReason.STALE_STATE,
            "outcome evidence must be finalized canonical transaction data",
            cutoff,
        )
    if (
        type(observation.slot) is not int
        or observation.slot < 0
        or observation.slot > cutoff
    ):
        return _abstain(
            AbstainReason.STALE_STATE,
            "raw outcome observation is outside the cutoff",
            cutoff,
        )
    if (
        type(observation.signature) is not bytes
        or not observation.signature
        or type(observation.transaction_index) is not int
        or observation.transaction_index < 0
        or (
            observation.event_ordinal is not None
            and (
                type(observation.event_ordinal) is not int
                or observation.event_ordinal < 0
            )
        )
    ):
        return _abstain(
            AbstainReason.MISSING_FEATURE,
            "raw outcome transaction identity is incomplete",
            cutoff,
        )
    if type(market_state) is not MarketTrajectoryPoint:
        return _abstain(
            AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
            "market trajectory state is malformed",
            cutoff,
        )
    market_error = _validate_market_state(
        observation=observation,
        market_state=market_state,
        cutoff=cutoff,
    )
    if market_error is not None:
        return market_error
    if type(quote) is not ExecutableQuote:
        return _abstain(
            AbstainReason.MISSING_FEATURE,
            "an executable full-exit quote is required",
            cutoff,
        )
    quote_error = _validate_quote(
        quote=quote,
        market_state=market_state,
        cutoff=cutoff,
    )
    if quote_error is not None:
        return quote_error
    if type(point.curve_completed) is not bool:
        return _abstain(
            AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
            "curve_completed must be boolean",
            cutoff,
        )
    if type(point.migration_observed) is not bool:
        return _abstain(
            AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
            "migration_observed must be boolean",
            cutoff,
        )
    return None


def _validate_market_state(
    *,
    observation: RawChainObservation,
    market_state: MarketTrajectoryPoint,
    cutoff: int,
) -> AbstainResult | None:
    values = (
        market_state.as_of_slot,
        market_state.slot,
        market_state.event_index,
        market_state.elapsed_ms,
        market_state.price_quote_base_units_per_token_base_unit_ppm,
    )
    if any(type(value) is not int or value < 0 for value in values):
        return _abstain(
            AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
            "market trajectory numeric fields are malformed",
            cutoff,
        )
    if market_state.as_of_slot > cutoff or market_state.slot > market_state.as_of_slot:
        return _abstain(
            AbstainReason.STALE_STATE,
            "market trajectory state contains future evidence",
            cutoff,
        )
    if market_state.slot != observation.slot:
        return _abstain(
            AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
            "market state and raw observation use different slots",
            cutoff,
        )
    if (
        observation.event_ordinal is not None
        and market_state.event_index != observation.event_ordinal
    ):
        return _abstain(
            AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
            "market state and raw observation use different event indexes",
            cutoff,
        )
    if (
        market_state.elapsed_ms < 0
        or market_state.price_quote_base_units_per_token_base_unit_ppm <= 0
    ):
        return _abstain(
            AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
            "market trajectory price and elapsed time are invalid",
            cutoff,
        )
    if (
        market_state.curve_progress_ppm is not None
        and not 0 <= market_state.curve_progress_ppm <= 1_000_000
    ):
        return _abstain(
            AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
            "market trajectory curve progress is invalid",
            cutoff,
        )
    if market_state.real_quote_reserves_base_units is not None and (
        type(market_state.real_quote_reserves_base_units) is not int
        or market_state.real_quote_reserves_base_units < 0
    ):
        return _abstain(
            AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
            "market trajectory reserves are malformed",
            cutoff,
        )
    return None


def _validate_quote(
    *,
    quote: ExecutableQuote,
    market_state: MarketTrajectoryPoint,
    cutoff: int,
) -> AbstainResult | None:
    if quote.as_of_slot != market_state.slot or quote.as_of_slot > cutoff:
        return _abstain(
            AbstainReason.STALE_STATE,
            "full-exit quote is outside the market point boundary",
            cutoff,
        )
    numeric_values = (
        quote.input_amount_base_units,
        quote.output_amount_base_units,
        quote.fee_amount_base_units,
    )
    if any(type(value) is not int or value < 0 for value in numeric_values):
        return _abstain(
            AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
            "full-exit quote amounts are malformed",
            cutoff,
        )
    if quote.input_amount_base_units <= 0 or quote.output_amount_base_units <= 0:
        return _abstain(
            AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
            "full-exit quote must have positive input and output",
            cutoff,
        )
    if quote.fee_amount_base_units > quote.output_amount_base_units:
        return _abstain(
            AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
            "full-exit quote fee exceeds output",
            cutoff,
        )
    provenance = (
        quote.fee_config_version,
        quote.decoder_version,
        quote.idl_hash,
        quote.program_config_version,
    )
    if any(type(value) is not str or not value for value in provenance):
        return _abstain(
            AbstainReason.MISSING_FEATURE,
            "full-exit quote provenance is incomplete",
            cutoff,
        )
    return None


def _validate_supplied_evidence_ids(
    evidence_ids: tuple[str, ...],
    cutoff: int,
    *,
    forbidden: str,
) -> AbstainResult | None:
    if type(evidence_ids) is not tuple or not all(
        type(evidence_id) is str and bool(evidence_id) for evidence_id in evidence_ids
    ):
        return _abstain(
            AbstainReason.MISSING_FEATURE,
            "supplied outcome evidence IDs must be an immutable string tuple",
            cutoff,
        )
    if len(set(evidence_ids)) != len(evidence_ids) or forbidden in evidence_ids:
        return _abstain(
            AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
            "outcome evidence IDs must be unique canonical identifiers",
            cutoff,
        )
    return None


def _canonical_observation_id(observation: RawChainObservation) -> str:
    """Hash canonical observation identity, deliberately excluding raw UUID."""

    identity = repr(observation_identity(observation)).encode("utf-8")
    return f"observation:{hashlib.sha256(identity).hexdigest()}"


def _safe_slot(value: object) -> int:
    return value if type(value) is int else -1


def _abstain(reason: AbstainReason, message: str, as_of_slot: int) -> AbstainResult:
    return AbstainResult(reason=reason, message=message, as_of_slot=as_of_slot)


__all__ = [
    "FinalizedOutcomePointInput",
    "OutcomeTrajectoryResult",
    "build_outcome_observation_point",
    "build_outcome_trajectory",
]
