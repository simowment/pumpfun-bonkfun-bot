"""Build point-in-time wallet/entity evidence from finalized history.

This module is deliberately an adapter, not an entity resolver.  Chain
artifacts prove what a wallet did and when a launch or fill occurred; they do
not prove that a wallet belongs to an operator entity.  That relationship must
arrive as an explicit, already-resolved ``EntityMembership`` snapshot.
"""

from __future__ import annotations

from rugbot.backtest.dataset import FinalizedTrade
from rugbot.decision.operator_qualification import WalletEntityEvidence
from rugbot.domain.amounts import Slot
from rugbot.domain.decisions import AbstainReason, AbstainResult
from rugbot.domain.launches import LaunchCreatedV2
from rugbot.domain.observations import RawChainObservation
from rugbot.domain.trades import TradeSide
from rugbot.graph.entity_resolution import EntityMembership
from rugbot.graph.point_in_time import PROBABILITY_PPM_DENOMINATOR
from rugbot.storage.jsonl_observation_store import observation_identity

WalletEntityEvidenceResult = tuple[WalletEntityEvidence, ...] | AbstainResult
_ObservationKey = tuple[int, int, bytes]
_LaunchKey = tuple[int, int]


def build_wallet_entity_evidence(  # noqa: C901, PLR0911, PLR0912, PLR0913
    *,
    observations: tuple[RawChainObservation, ...],
    launches: tuple[LaunchCreatedV2, ...],
    trades: tuple[FinalizedTrade, ...],
    entity_memberships: tuple[EntityMembership, ...] = (),
    entity_id: str,
    as_of_slot: Slot,
    max_entry_transaction_index: int = 1,
) -> WalletEntityEvidenceResult:
    """Adapt finalized artifacts into case-builder entity evidence.

    ``EntityMembership`` is required because none of the three chain
    artifact types contains an entity identity or a control probability.  A
    membership is used only at its exact snapshot slot; it is never carried
    forward to a later launch.  For each launch, exactly one wallet must have
    both an early finalized buy and a unique highest same-controller
    probability in the matching membership snapshot.

    The function performs no I/O, mutates no input, and never assigns a
    probability.  If the explicit entity proof is absent or temporally
    incomplete, it returns ``ABSTAIN`` rather than fabricating evidence.
    """

    cutoff = as_of_slot if type(as_of_slot) is int else -1
    request_error = _validate_request(
        observations=observations,
        launches=launches,
        trades=trades,
        entity_memberships=entity_memberships,
        entity_id=entity_id,
        as_of_slot=as_of_slot,
        max_entry_transaction_index=max_entry_transaction_index,
    )
    if request_error is not None:
        return request_error

    observation_keys = _validated_observation_keys(observations, cutoff)
    if isinstance(observation_keys, AbstainResult):
        return observation_keys

    launches_by_id: dict[str, LaunchCreatedV2] = {}
    launch_keys: dict[str, _LaunchKey] = {}
    for launch in launches:
        error = _validate_launch(
            launch=launch,
            cutoff=cutoff,
            observation_keys=observation_keys,
        )
        if error is not None:
            return error
        if launch.launch_id in launches_by_id:
            return _abstain(
                AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
                "finalized launches contain duplicate launch IDs",
                cutoff,
            )
        launches_by_id[launch.launch_id] = launch
        launch_keys[launch.launch_id] = (launch.as_of_slot, launch.transaction_index)

    trades_by_launch: dict[str, list[FinalizedTrade]] = {
        launch_id: [] for launch_id in launches_by_id
    }
    for trade in trades:
        error = _validate_trade(
            trade=trade,
            cutoff=cutoff,
            launches_by_id=launches_by_id,
            launch_keys=launch_keys,
            observation_keys=observation_keys,
        )
        if error is not None:
            return error
        trades_by_launch[trade.launch_id].append(trade)

    membership_error = _validate_memberships(
        entity_memberships=entity_memberships,
        entity_id=entity_id,
        cutoff=cutoff,
    )
    if membership_error is not None:
        return membership_error

    memberships_by_snapshot: dict[tuple[int, str], list[EntityMembership]] = {}
    for membership in entity_memberships:
        memberships_by_snapshot.setdefault(
            (membership.as_of_slot, membership.address), []
        ).append(membership)

    result: list[WalletEntityEvidence] = []
    for launch in launches:
        early_buys = tuple(
            trade
            for trade in trades_by_launch[launch.launch_id]
            if trade.side is TradeSide.BUY
            and trade.transaction_index <= max_entry_transaction_index
        )
        if not early_buys:
            return _abstain(
                AbstainReason.MISSING_FEATURE,
                (
                    "each launch needs a finalized early buy to identify the "
                    "wallet used by the case builder"
                ),
                cutoff,
            )

        candidate_wallets = tuple(sorted({trade.wallet for trade in early_buys}))
        candidates: list[tuple[str, int, tuple[str, ...]]] = []
        for wallet in candidate_wallets:
            snapshot_memberships = tuple(
                memberships_by_snapshot.get((launch.as_of_slot, wallet), ())
            )
            if not snapshot_memberships:
                continue
            probability = max(
                membership.same_controller_probability_ppm
                for membership in snapshot_memberships
            )
            if probability <= 0:
                continue
            evidence_ids = tuple(
                dict.fromkeys(
                    evidence_id
                    for membership in snapshot_memberships
                    if membership.same_controller_probability_ppm == probability
                    for evidence_id in membership.evidence_ids
                )
            )
            if not evidence_ids:
                return _abstain(
                    AbstainReason.MISSING_FEATURE,
                    "entity membership provenance is required for every matched wallet",
                    cutoff,
                )
            candidates.append((wallet, probability, evidence_ids))

        if not candidates:
            return _abstain(
                AbstainReason.MISSING_FEATURE,
                (
                    "an exact point-in-time EntityMembership snapshot is required "
                    "for the early-buy wallet of every launch; no carry-forward "
                    "entity inference is allowed"
                ),
                cutoff,
            )

        highest_probability = max(item[1] for item in candidates)
        best = tuple(item for item in candidates if item[1] == highest_probability)
        if len(best) != 1:
            return _abstain(
                AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
                "multiple early-buy wallets tie for entity membership probability",
                cutoff,
            )
        wallet, probability, evidence_ids = best[0]
        result.append(
            WalletEntityEvidence(
                as_of_slot=Slot(launch.as_of_slot),
                observed_slot=Slot(launch.as_of_slot),
                entity_id=entity_id,
                launch_id=launch.launch_id,
                wallet=wallet,
                entity_probability_ppm=probability,
                evidence_ids=evidence_ids,
            )
        )

    return tuple(result)


def _validate_request(  # noqa: PLR0913
    *,
    observations: object,
    launches: object,
    trades: object,
    entity_memberships: object,
    entity_id: object,
    as_of_slot: object,
    max_entry_transaction_index: object,
) -> AbstainResult | None:
    cutoff = as_of_slot if type(as_of_slot) is int else -1
    if type(as_of_slot) is not int or as_of_slot < 0:
        return _abstain(
            AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
            "operator history cutoff must be a non-negative integer",
            cutoff,
        )
    if not isinstance(entity_id, str) or not entity_id:
        return _abstain(
            AbstainReason.MISSING_FEATURE,
            "operator entity_id is required",
            cutoff,
        )
    if type(max_entry_transaction_index) is not int or max_entry_transaction_index < 0:
        return _abstain(
            AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
            "maximum entry transaction index is malformed",
            cutoff,
        )
    if not all(
        type(value) is tuple and value for value in (observations, launches, trades)
    ):
        return _abstain(
            AbstainReason.MISSING_FEATURE,
            "finalized observations, launches, and trades are required",
            cutoff,
        )
    if type(entity_memberships) is not tuple or not entity_memberships:
        return _abstain(
            AbstainReason.MISSING_FEATURE,
            (
                "WalletEntityEvidence cannot be derived from chain artifacts alone; "
                "an EntityMembership snapshot is required (or its upstream "
                "EntitySeedEvidence plus point-in-time graph resolution)"
            ),
            cutoff,
        )
    return None


def _validated_observation_keys(
    observations: tuple[RawChainObservation, ...], cutoff: int
) -> dict[_ObservationKey, RawChainObservation] | AbstainResult:
    keys: dict[_ObservationKey, RawChainObservation] = {}
    identities: set[object] = set()
    for observation in observations:
        if type(observation) is not RawChainObservation:
            return _abstain(
                AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
                "finalized observation is malformed",
                cutoff,
            )
        if (
            observation.commitment != "finalized"
            or observation.canonical_status != "canonical"
            or observation.source_update_kind != "transaction"
            or not _valid_observation_identity_fields(observation)
            or type(observation.slot) is not int
            or observation.slot < 0
            or observation.slot > cutoff
            or type(observation.transaction_index) is not int
            or observation.transaction_index < 0
            or type(observation.signature) is not bytes
            or not observation.signature
        ):
            return _abstain(
                AbstainReason.STALE_STATE,
                "wallet/entity adaptation requires finalized canonical transaction observations",
                cutoff,
            )
        identity = observation_identity(observation)
        if identity in identities:
            return _abstain(
                AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
                "finalized observations contain duplicate canonical evidence",
                cutoff,
            )
        identities.add(identity)
        key = (observation.slot, observation.transaction_index, observation.signature)
        existing = keys.get(key)
        if existing is not None and observation_identity(existing) != identity:
            return _abstain(
                AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
                "multiple finalized observations ambiguously represent one transaction",
                cutoff,
            )
        keys[key] = observation
    return keys


def _validate_launch(
    *,
    launch: LaunchCreatedV2,
    cutoff: int,
    observation_keys: dict[_ObservationKey, RawChainObservation],
) -> AbstainResult | None:
    if type(launch) is not LaunchCreatedV2:
        return _abstain(
            AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
            "launches must use finalized LaunchCreatedV2 artifacts",
            cutoff,
        )
    if (
        type(launch.as_of_slot) is not int
        or launch.as_of_slot < 0
        or launch.as_of_slot > cutoff
        or not isinstance(launch.launch_id, str)
        or not launch.launch_id
        or not isinstance(launch.mint_pubkey, str)
        or not launch.mint_pubkey
        or not isinstance(launch.creator_pubkey, str)
        or not launch.creator_pubkey
        or type(launch.transaction_index) is not int
        or launch.transaction_index < 0
        or type(launch.signature) is not bytes
        or not launch.signature
        or launch.missing_evidence
    ):
        return _abstain(
            AbstainReason.MISSING_FEATURE,
            "launch lacks complete finalized identity or decoder evidence",
            cutoff,
        )
    if (
        launch.as_of_slot,
        launch.transaction_index,
        launch.signature,
    ) not in observation_keys:
        return _abstain(
            AbstainReason.MISSING_FEATURE,
            "launch has no matching finalized raw observation",
            cutoff,
        )
    return None


def _validate_trade(
    *,
    trade: FinalizedTrade,
    cutoff: int,
    launches_by_id: dict[str, LaunchCreatedV2],
    launch_keys: dict[str, _LaunchKey],
    observation_keys: dict[_ObservationKey, RawChainObservation],
) -> AbstainResult | None:
    if type(trade) is not FinalizedTrade:
        return _abstain(
            AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
            "trades must use finalized FinalizedTrade artifacts",
            cutoff,
        )
    launch = launches_by_id.get(trade.launch_id)
    if launch is None:
        return _abstain(
            AbstainReason.MISSING_FEATURE,
            "finalized trade references an unknown launch",
            cutoff,
        )
    if (
        type(trade.as_of_slot) is not int
        or trade.as_of_slot < 0
        or trade.as_of_slot > cutoff
        or type(trade.slot) is not int
        or trade.slot < 0
        or trade.slot > trade.as_of_slot
        or trade.token_mint != launch.mint_pubkey
        or not isinstance(trade.wallet, str)
        or not trade.wallet
        or trade.side not in (TradeSide.BUY, TradeSide.SELL)
        or type(trade.transaction_index) is not int
        or trade.transaction_index < 0
        or type(trade.signature) is not bytes
        or not trade.signature
        or type(trade.base_amount_base_units) is not int
        or trade.base_amount_base_units <= 0
        or type(trade.quote_amount_base_units) is not int
        or trade.quote_amount_base_units <= 0
        or type(trade.execution_cost_quote_base_units) is not int
        or trade.execution_cost_quote_base_units < 0
        or not _valid_ids(trade.evidence_ids)
    ):
        return _abstain(
            AbstainReason.MISSING_FEATURE,
            "finalized trade identity, amounts, or provenance are incomplete",
            cutoff,
        )
    if (trade.slot, trade.transaction_index, trade.signature) not in observation_keys:
        return _abstain(
            AbstainReason.MISSING_FEATURE,
            "finalized trade has no matching raw observation",
            cutoff,
        )
    if (trade.slot, trade.transaction_index) <= launch_keys[trade.launch_id]:
        return _abstain(
            AbstainReason.STALE_STATE,
            "finalized trade is not provably after its launch",
            cutoff,
        )
    return None


def _validate_memberships(
    *,
    entity_memberships: tuple[EntityMembership, ...],
    entity_id: str,
    cutoff: int,
) -> AbstainResult | None:
    for membership in entity_memberships:
        if type(membership) is not EntityMembership:
            return _abstain(
                AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
                "entity memberships must use the existing EntityMembership type",
                cutoff,
            )
        if (
            membership.entity_id != entity_id
            or type(membership.as_of_slot) is not int
            or membership.as_of_slot < 0
            or membership.as_of_slot > cutoff
            or not isinstance(membership.address, str)
            or not membership.address
            or type(membership.same_controller_probability_ppm) is not int
            or not 0
            <= membership.same_controller_probability_ppm
            <= PROBABILITY_PPM_DENOMINATOR
            or not _valid_ids(membership.evidence_ids)
            or not isinstance(membership.model_version, str)
            or not membership.model_version
            or not isinstance(membership.source, str)
            or not membership.source
        ):
            return _abstain(
                AbstainReason.MISSING_FEATURE,
                "entity membership lacks a valid point-in-time control probability or provenance",
                cutoff,
            )
    return None


def _valid_ids(evidence_ids: object) -> bool:
    return (
        type(evidence_ids) is tuple
        and bool(evidence_ids)
        and all(isinstance(value, str) and value for value in evidence_ids)
        and len(set(evidence_ids)) == len(evidence_ids)
    )


def _valid_observation_identity_fields(observation: RawChainObservation) -> bool:
    """Ensure canonical identity can be computed without trusting raw shape."""

    return (
        isinstance(observation.source_id, str)
        and isinstance(observation.commitment, str)
        and isinstance(observation.canonical_status, str)
        and (
            observation.event_ordinal is None or type(observation.event_ordinal) is int
        )
        and (
            observation.account_write_version is None
            or type(observation.account_write_version) is int
        )
        and (
            observation.account_pubkey is None
            or type(observation.account_pubkey) is bytes
        )
        and (
            observation.account_owner_program_id is None
            or type(observation.account_owner_program_id) is bytes
        )
        and (
            observation.raw_transaction_format is None
            or isinstance(observation.raw_transaction_format, str)
        )
        and (
            observation.raw_source_status is None
            or type(observation.raw_source_status) is int
        )
        and (
            observation.raw_source_payload is None
            or type(observation.raw_source_payload) is bytes
        )
        and (
            observation.raw_transaction is None
            or type(observation.raw_transaction) is bytes
        )
        and (
            observation.raw_account_data is None
            or type(observation.raw_account_data) is bytes
        )
    )


def _abstain(reason: AbstainReason, message: str, as_of_slot: int) -> AbstainResult:
    return AbstainResult(reason=reason, message=message, as_of_slot=as_of_slot)


__all__ = ["WalletEntityEvidenceResult", "build_wallet_entity_evidence"]
