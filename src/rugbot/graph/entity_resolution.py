"""Pure point-in-time entity resolution and address-role contracts."""

from dataclasses import dataclass
from enum import Enum

from rugbot.domain.amounts import Slot
from rugbot.domain.decisions import AbstainReason, AbstainResult
from rugbot.graph.point_in_time import (
    PROBABILITY_PPM_DENOMINATOR,
    AddressGraphSnapshot,
    AddressRelationshipKind,
    AddressRelationshipView,
    direct_relationships_for_address,
)


class AddressRole(Enum):
    """Probabilistic launch/campaign role assigned to an address."""

    CREATOR = "creator"
    CREATION_SUBMITTER = "creation_submitter"
    FEE_PAYER = "fee_payer"
    FUNDER = "funder"
    FIRST_BUYER = "first_buyer"
    FAKE_PUMP_BUYER = "fake_pump_buyer"
    INVENTORY_HOLDER = "inventory_holder"
    DUMPER = "dumper"
    PROFIT_COLLECTOR = "profit_collector"
    CREATOR_FEE_COLLECTOR = "creator_fee_collector"
    RELAY_ADDRESS = "relay_address"


@dataclass(frozen=True, slots=True)
class EntitySeedEvidence:
    """Direct point-in-time seed evidence for one entity address."""

    as_of_slot: Slot
    entity_id: str
    address: str
    valid_from_slot: Slot
    valid_to_slot: Slot | None
    same_controller_probability_ppm: int
    cooperating_probability_ppm: int
    evidence_ids: tuple[str, ...]
    model_version: str


@dataclass(frozen=True, slots=True)
class EntityResolutionConfig:
    """Configuration for a conservative point-in-time entity resolution."""

    as_of_slot: Slot
    entity_id: str
    resolver_version: str
    min_membership_probability_ppm: int


@dataclass(frozen=True, slots=True)
class EntityMembership:
    """Probabilistic direct membership result for one address."""

    as_of_slot: Slot
    entity_id: str
    address: str
    same_controller_probability_ppm: int
    cooperating_probability_ppm: int
    shared_service_probability_ppm: int
    incidental_interaction_probability_ppm: int
    evidence_ids: tuple[str, ...]
    model_version: str
    source: str


@dataclass(frozen=True, slots=True)
class ProbabilisticEntity:
    """Resolved entity snapshot using only direct point-in-time evidence."""

    as_of_slot: Slot
    entity_id: str
    resolver_version: str
    graph_snapshot_version: str
    memberships: tuple[EntityMembership, ...]
    source_seed_count: int
    active_seed_count: int
    direct_relationship_count: int
    reason_codes: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class AddressRoleEvidence:
    """Direct evidence that an address played a role at one point in time."""

    as_of_slot: Slot
    address: str
    role: AddressRole
    valid_from_slot: Slot
    valid_to_slot: Slot | None
    role_probability_ppm: int
    evidence_ids: tuple[str, ...]
    model_version: str


@dataclass(frozen=True, slots=True)
class AddressRoleAssignment:
    """Point-in-time role assignment above configured support threshold."""

    as_of_slot: Slot
    address: str
    role: AddressRole
    role_probability_ppm: int
    evidence_ids: tuple[str, ...]
    model_version: str


@dataclass(frozen=True, slots=True)
class AddressRoleSnapshot:
    """Immutable point-in-time role assignment snapshot."""

    as_of_slot: Slot
    classifier_version: str
    assignments: tuple[AddressRoleAssignment, ...]
    source_evidence_count: int
    active_evidence_count: int
    skipped_inactive_evidence_count: int
    reason_codes: tuple[str, ...]


def resolve_probabilistic_entity(
    *,
    graph: AddressGraphSnapshot,
    seeds: tuple[EntitySeedEvidence, ...],
    config: EntityResolutionConfig,
) -> ProbabilisticEntity | AbstainResult:
    """Resolve one entity from explicit seeds and direct graph relationships."""

    validation_error = _validate_resolution_inputs(
        graph=graph,
        seeds=seeds,
        config=config,
    )
    if validation_error is not None:
        return validation_error

    active_seeds = tuple(
        seed for seed in seeds if _seed_active_at(seed, config.as_of_slot)
    )
    if not active_seeds:
        return _missing("active entity seed evidence is required", config.as_of_slot)
    eligible_seeds = tuple(
        seed
        for seed in active_seeds
        if _max_seed_probability(seed) >= config.min_membership_probability_ppm
    )
    if not eligible_seeds:
        return _missing(
            "active entity seed evidence above threshold is required",
            config.as_of_slot,
        )

    memberships: dict[str, EntityMembership] = {}
    direct_relationship_count = 0
    for seed in eligible_seeds:
        _merge_membership(
            memberships,
            _membership_from_seed(seed=seed, config=config),
        )
        for relationship in direct_relationships_for_address(
            snapshot=graph,
            address=seed.address,
        ):
            direct_relationship_count += 1
            membership = _membership_from_relationship(
                relationship=relationship,
                seed_address=seed.address,
                config=config,
            )
            if membership is not None:
                _merge_membership(memberships, membership)

    return ProbabilisticEntity(
        as_of_slot=config.as_of_slot,
        entity_id=config.entity_id,
        resolver_version=config.resolver_version,
        graph_snapshot_version=graph.snapshot_version,
        memberships=tuple(
            sorted(
                memberships.values(),
                key=lambda membership: (
                    membership.address,
                    membership.source,
                    membership.evidence_ids,
                ),
            )
        ),
        source_seed_count=len(seeds),
        active_seed_count=len(active_seeds),
        direct_relationship_count=direct_relationship_count,
        reason_codes=("probabilistic_entity_resolved",),
    )


def classify_address_roles(
    *,
    evidence: tuple[AddressRoleEvidence, ...],
    as_of_slot: Slot,
    classifier_version: str,
    min_role_probability_ppm: int,
) -> AddressRoleSnapshot | AbstainResult:
    """Build role assignments from direct role evidence only."""

    request_error = _validate_role_request(
        as_of_slot=as_of_slot,
        classifier_version=classifier_version,
        min_role_probability_ppm=min_role_probability_ppm,
    )
    if request_error is not None:
        return request_error
    if not evidence:
        return _missing("address role evidence is required", as_of_slot)

    active_evidence: list[AddressRoleEvidence] = []
    skipped_inactive_count = 0
    for item in evidence:
        item_error = _validate_role_evidence(item, as_of_slot)
        if item_error is not None:
            return item_error
        if not _role_evidence_active_at(item, as_of_slot):
            skipped_inactive_count += 1
            continue
        active_evidence.append(item)

    assignments = tuple(
        _assignment_from_role_evidence(item, as_of_slot=as_of_slot)
        for item in sorted(
            active_evidence,
            key=lambda role: (role.address, role.role.value, role.evidence_ids),
        )
        if item.role_probability_ppm >= min_role_probability_ppm
    )

    return AddressRoleSnapshot(
        as_of_slot=as_of_slot,
        classifier_version=classifier_version,
        assignments=assignments,
        source_evidence_count=len(evidence),
        active_evidence_count=len(active_evidence),
        skipped_inactive_evidence_count=skipped_inactive_count,
        reason_codes=(
            ("address_roles_classified",)
            if assignments
            else ("no_role_evidence_above_threshold",)
        ),
    )


def _validate_resolution_inputs(
    *,
    graph: AddressGraphSnapshot,
    seeds: tuple[EntitySeedEvidence, ...],
    config: EntityResolutionConfig,
) -> AbstainResult | None:
    config_error = _validate_resolution_config(config)
    if config_error is not None:
        return config_error
    graph_error = _validate_resolution_graph(graph, config)
    if graph_error is not None:
        return graph_error
    if not seeds:
        return _missing("entity seed evidence is required", config.as_of_slot)
    for seed in seeds:
        seed_error = _validate_seed(seed, config)
        if seed_error is not None:
            return seed_error
    return None


def _validate_resolution_config(
    config: EntityResolutionConfig,
) -> AbstainResult | None:
    if not _non_negative_int(config.as_of_slot):
        return _unsupported(
            "as_of_slot must be a non-negative integer", config.as_of_slot
        )
    if not isinstance(config.entity_id, str) or not config.entity_id:
        return _missing("entity_id is required", config.as_of_slot)
    if not isinstance(config.resolver_version, str) or not config.resolver_version:
        return _decoder_mismatch("resolver_version is required", config.as_of_slot)
    if not _valid_probability_ppm(config.min_membership_probability_ppm):
        return _unsupported(
            "min_membership_probability_ppm must be in probability ppm range",
            config.as_of_slot,
        )
    return None


def _validate_resolution_graph(
    graph: AddressGraphSnapshot,
    config: EntityResolutionConfig,
) -> AbstainResult | None:
    if graph.as_of_slot != config.as_of_slot:
        return _stale("graph snapshot uses a different as_of_slot", config.as_of_slot)
    if not isinstance(graph.snapshot_version, str) or not graph.snapshot_version:
        return _decoder_mismatch(
            "graph snapshot_version is required", config.as_of_slot
        )
    count_error = _validate_graph_counts(graph, config)
    if count_error is not None:
        return count_error
    for relationship in graph.relationships:
        relationship_error = _validate_graph_relationship(relationship, config)
        if relationship_error is not None:
            return relationship_error
    return None


def _validate_graph_counts(
    graph: AddressGraphSnapshot,
    config: EntityResolutionConfig,
) -> AbstainResult | None:
    count_fields = (
        graph.source_edge_count,
        graph.active_edge_count,
        graph.skipped_inactive_edge_count,
    )
    if any(not _non_negative_int(count) for count in count_fields):
        return _unsupported("graph edge counts must be non-negative", config.as_of_slot)
    if graph.active_edge_count != len(graph.relationships):
        return _unsupported(
            "graph active_edge_count must match relationship count",
            config.as_of_slot,
        )
    return None


def _validate_graph_relationship(
    relationship: AddressRelationshipView,
    config: EntityResolutionConfig,
) -> AbstainResult | None:
    for validation in (
        _validate_graph_relationship_slots,
        _validate_graph_relationship_addresses,
        _validate_graph_relationship_probabilities,
        _validate_graph_relationship_provenance,
    ):
        validation_error = validation(relationship, config)
        if validation_error is not None:
            return validation_error
    return None


def _validate_graph_relationship_slots(
    relationship: AddressRelationshipView,
    config: EntityResolutionConfig,
) -> AbstainResult | None:
    slot_fields = (
        relationship.as_of_slot,
        relationship.observed_slot,
        config.as_of_slot,
    )
    if any(not _non_negative_int(slot) for slot in slot_fields):
        return _unsupported(
            "graph relationship slot fields must be non-negative",
            config.as_of_slot,
        )
    if relationship.as_of_slot != config.as_of_slot:
        return _stale(
            "graph relationship uses a different as_of_slot",
            config.as_of_slot,
        )
    if relationship.observed_slot > config.as_of_slot:
        return _stale("graph relationship observed_slot is future", config.as_of_slot)
    if not _non_negative_int(relationship.age_slots):
        return _unsupported(
            "graph relationship age_slots must be non-negative",
            config.as_of_slot,
        )
    return None


def _validate_graph_relationship_addresses(
    relationship: AddressRelationshipView,
    config: EntityResolutionConfig,
) -> AbstainResult | None:
    if (
        not isinstance(relationship.source_address, str)
        or not relationship.source_address
    ):
        return _missing(
            "graph relationship source_address is required", config.as_of_slot
        )
    if (
        not isinstance(relationship.target_address, str)
        or not relationship.target_address
    ):
        return _missing(
            "graph relationship target_address is required", config.as_of_slot
        )
    if not isinstance(relationship.relationship_kind, AddressRelationshipKind):
        return _unsupported(
            "graph relationship kind is invalid",
            config.as_of_slot,
        )
    return None


def _validate_graph_relationship_probabilities(
    relationship: AddressRelationshipView,
    config: EntityResolutionConfig,
) -> AbstainResult | None:
    probability_fields = {
        "raw_confidence_ppm": relationship.raw_confidence_ppm,
        "decayed_confidence_ppm": relationship.decayed_confidence_ppm,
        "same_controller_probability_ppm": (
            relationship.same_controller_probability_ppm
        ),
        "cooperating_probability_ppm": relationship.cooperating_probability_ppm,
        "shared_service_probability_ppm": relationship.shared_service_probability_ppm,
        "incidental_interaction_probability_ppm": (
            relationship.incidental_interaction_probability_ppm
        ),
    }
    for field_name, value in probability_fields.items():
        if not _valid_probability_ppm(value):
            return _unsupported(
                f"graph relationship {field_name} must be in probability ppm range",
                config.as_of_slot,
            )
    return None


def _validate_graph_relationship_provenance(
    relationship: AddressRelationshipView,
    config: EntityResolutionConfig,
) -> AbstainResult | None:
    if not relationship.evidence_ids or any(
        not isinstance(evidence_id, str) or not evidence_id
        for evidence_id in relationship.evidence_ids
    ):
        return _missing(
            "graph relationship evidence_ids are required", config.as_of_slot
        )
    if (
        not isinstance(relationship.model_version, str)
        or not relationship.model_version
    ):
        return _decoder_mismatch(
            "graph relationship model_version is required",
            config.as_of_slot,
        )
    return None


def _validate_seed(
    seed: EntitySeedEvidence,
    config: EntityResolutionConfig,
) -> AbstainResult | None:
    for validation in (
        _validate_seed_slots,
        _validate_seed_identity,
        _validate_seed_probabilities,
        _validate_seed_provenance,
    ):
        validation_error = validation(seed, config)
        if validation_error is not None:
            return validation_error
    return None


def _validate_seed_slots(
    seed: EntitySeedEvidence,
    config: EntityResolutionConfig,
) -> AbstainResult | None:
    slot_fields = (seed.as_of_slot, seed.valid_from_slot, config.as_of_slot)
    if any(not _non_negative_int(slot) for slot in slot_fields):
        return _unsupported(
            "seed slot fields must be non-negative integers",
            config.as_of_slot,
        )
    if seed.valid_to_slot is not None and not _non_negative_int(seed.valid_to_slot):
        return _unsupported("seed valid_to_slot is invalid", config.as_of_slot)
    if seed.valid_to_slot is not None and seed.valid_to_slot <= seed.valid_from_slot:
        return _unsupported(
            "seed valid_to_slot must be after valid_from_slot",
            config.as_of_slot,
        )
    if seed.as_of_slot > config.as_of_slot or seed.valid_from_slot > config.as_of_slot:
        return _stale("seed evidence is newer than as_of_slot", config.as_of_slot)
    return None


def _validate_seed_identity(
    seed: EntitySeedEvidence,
    config: EntityResolutionConfig,
) -> AbstainResult | None:
    if seed.entity_id != config.entity_id:
        return _unsupported("seed entity_id does not match request", config.as_of_slot)
    if not isinstance(seed.address, str) or not seed.address:
        return _missing("seed address is required", config.as_of_slot)
    return None


def _validate_seed_probabilities(
    seed: EntitySeedEvidence,
    config: EntityResolutionConfig,
) -> AbstainResult | None:
    probabilities = {
        "seed same_controller_probability_ppm": seed.same_controller_probability_ppm,
        "seed cooperating_probability_ppm": seed.cooperating_probability_ppm,
    }
    for field_name, value in probabilities.items():
        if not _valid_probability_ppm(value):
            return _unsupported(
                f"{field_name} must be in probability ppm range",
                config.as_of_slot,
            )
    return None


def _validate_seed_provenance(
    seed: EntitySeedEvidence,
    config: EntityResolutionConfig,
) -> AbstainResult | None:
    if not seed.evidence_ids or any(
        not isinstance(evidence_id, str) or not evidence_id
        for evidence_id in seed.evidence_ids
    ):
        return _missing("seed evidence_ids are required", config.as_of_slot)
    if not isinstance(seed.model_version, str) or not seed.model_version:
        return _decoder_mismatch("seed model_version is required", config.as_of_slot)
    return None


def _max_seed_probability(seed: EntitySeedEvidence) -> int:
    return max(
        seed.same_controller_probability_ppm,
        seed.cooperating_probability_ppm,
    )


def _seed_active_at(seed: EntitySeedEvidence, as_of_slot: Slot) -> bool:
    return seed.valid_to_slot is None or as_of_slot < seed.valid_to_slot


def _membership_from_seed(
    *,
    seed: EntitySeedEvidence,
    config: EntityResolutionConfig,
) -> EntityMembership:
    return EntityMembership(
        as_of_slot=config.as_of_slot,
        entity_id=config.entity_id,
        address=seed.address,
        same_controller_probability_ppm=seed.same_controller_probability_ppm,
        cooperating_probability_ppm=seed.cooperating_probability_ppm,
        shared_service_probability_ppm=0,
        incidental_interaction_probability_ppm=0,
        evidence_ids=seed.evidence_ids,
        model_version=seed.model_version,
        source="seed",
    )


def _membership_from_relationship(
    *,
    relationship: AddressRelationshipView,
    seed_address: str,
    config: EntityResolutionConfig,
) -> EntityMembership | None:
    address = (
        relationship.target_address
        if relationship.source_address == seed_address
        else relationship.source_address
    )
    if (
        _max_membership_probability(relationship)
        < config.min_membership_probability_ppm
    ):
        return None
    return EntityMembership(
        as_of_slot=config.as_of_slot,
        entity_id=config.entity_id,
        address=address,
        same_controller_probability_ppm=relationship.same_controller_probability_ppm,
        cooperating_probability_ppm=relationship.cooperating_probability_ppm,
        shared_service_probability_ppm=relationship.shared_service_probability_ppm,
        incidental_interaction_probability_ppm=(
            relationship.incidental_interaction_probability_ppm
        ),
        evidence_ids=relationship.evidence_ids,
        model_version=relationship.model_version,
        source="direct_graph_edge",
    )


def _max_membership_probability(relationship: AddressRelationshipView) -> int:
    return max(
        relationship.same_controller_probability_ppm,
        relationship.cooperating_probability_ppm,
    )


def _merge_membership(
    memberships: dict[str, EntityMembership],
    incoming: EntityMembership,
) -> None:
    existing = memberships.get(incoming.address)
    if existing is None:
        memberships[incoming.address] = incoming
        return
    memberships[incoming.address] = EntityMembership(
        as_of_slot=existing.as_of_slot,
        entity_id=existing.entity_id,
        address=existing.address,
        same_controller_probability_ppm=max(
            existing.same_controller_probability_ppm,
            incoming.same_controller_probability_ppm,
        ),
        cooperating_probability_ppm=max(
            existing.cooperating_probability_ppm,
            incoming.cooperating_probability_ppm,
        ),
        shared_service_probability_ppm=max(
            existing.shared_service_probability_ppm,
            incoming.shared_service_probability_ppm,
        ),
        incidental_interaction_probability_ppm=max(
            existing.incidental_interaction_probability_ppm,
            incoming.incidental_interaction_probability_ppm,
        ),
        evidence_ids=tuple(
            dict.fromkeys((*existing.evidence_ids, *incoming.evidence_ids))
        ),
        model_version=existing.model_version,
        source=existing.source,
    )


def _validate_role_request(
    *,
    as_of_slot: Slot,
    classifier_version: str,
    min_role_probability_ppm: int,
) -> AbstainResult | None:
    if not _non_negative_int(as_of_slot):
        return _unsupported("as_of_slot must be a non-negative integer", as_of_slot)
    if not isinstance(classifier_version, str) or not classifier_version:
        return _decoder_mismatch("classifier_version is required", as_of_slot)
    if not _valid_probability_ppm(min_role_probability_ppm):
        return _unsupported(
            "min_role_probability_ppm must be in probability ppm range",
            as_of_slot,
        )
    return None


def _validate_role_evidence(
    evidence: AddressRoleEvidence,
    as_of_slot: Slot,
) -> AbstainResult | None:
    for validation in (
        _validate_role_slots,
        _validate_role_identity,
        _validate_role_probability,
        _validate_role_provenance,
    ):
        validation_error = validation(evidence, as_of_slot)
        if validation_error is not None:
            return validation_error
    return None


def _validate_role_slots(
    evidence: AddressRoleEvidence,
    as_of_slot: Slot,
) -> AbstainResult | None:
    slot_fields = (evidence.as_of_slot, evidence.valid_from_slot, as_of_slot)
    if any(not _non_negative_int(slot) for slot in slot_fields):
        return _unsupported(
            "role evidence slot fields must be non-negative integers",
            as_of_slot,
        )
    if evidence.valid_to_slot is not None and not _non_negative_int(
        evidence.valid_to_slot
    ):
        return _unsupported("role evidence valid_to_slot is invalid", as_of_slot)
    if (
        evidence.valid_to_slot is not None
        and evidence.valid_to_slot <= evidence.valid_from_slot
    ):
        return _unsupported(
            "role evidence valid_to_slot must be after valid_from_slot",
            as_of_slot,
        )
    if evidence.as_of_slot > as_of_slot or evidence.valid_from_slot > as_of_slot:
        return _stale("role evidence is newer than as_of_slot", as_of_slot)
    return None


def _validate_role_identity(
    evidence: AddressRoleEvidence,
    as_of_slot: Slot,
) -> AbstainResult | None:
    if not isinstance(evidence.address, str) or not evidence.address:
        return _missing("role evidence address is required", as_of_slot)
    if not isinstance(evidence.role, AddressRole):
        return _unsupported("role evidence role is invalid", as_of_slot)
    return None


def _validate_role_probability(
    evidence: AddressRoleEvidence,
    as_of_slot: Slot,
) -> AbstainResult | None:
    if not _valid_probability_ppm(evidence.role_probability_ppm):
        return _unsupported(
            "role_probability_ppm must be in probability ppm range",
            as_of_slot,
        )
    return None


def _validate_role_provenance(
    evidence: AddressRoleEvidence,
    as_of_slot: Slot,
) -> AbstainResult | None:
    if not evidence.evidence_ids or any(
        not isinstance(evidence_id, str) or not evidence_id
        for evidence_id in evidence.evidence_ids
    ):
        return _missing("role evidence_ids are required", as_of_slot)
    if not isinstance(evidence.model_version, str) or not evidence.model_version:
        return _decoder_mismatch("role model_version is required", as_of_slot)
    return None


def _role_evidence_active_at(evidence: AddressRoleEvidence, as_of_slot: Slot) -> bool:
    return evidence.valid_to_slot is None or as_of_slot < evidence.valid_to_slot


def _assignment_from_role_evidence(
    evidence: AddressRoleEvidence,
    *,
    as_of_slot: Slot,
) -> AddressRoleAssignment:
    return AddressRoleAssignment(
        as_of_slot=as_of_slot,
        address=evidence.address,
        role=evidence.role,
        role_probability_ppm=evidence.role_probability_ppm,
        evidence_ids=evidence.evidence_ids,
        model_version=evidence.model_version,
    )


def _valid_probability_ppm(value: object) -> bool:
    return _non_negative_int(value) and value <= PROBABILITY_PPM_DENOMINATOR


def _non_negative_int(value: object) -> bool:
    return type(value) is int and value >= 0


def _missing(message: str, as_of_slot: Slot) -> AbstainResult:
    return AbstainResult(
        reason=AbstainReason.MISSING_FEATURE,
        message=message,
        as_of_slot=_abstain_slot(as_of_slot),
    )


def _decoder_mismatch(message: str, as_of_slot: Slot) -> AbstainResult:
    return AbstainResult(
        reason=AbstainReason.DECODER_MISMATCH,
        message=message,
        as_of_slot=_abstain_slot(as_of_slot),
    )


def _stale(message: str, as_of_slot: Slot) -> AbstainResult:
    return AbstainResult(
        reason=AbstainReason.STALE_STATE,
        message=message,
        as_of_slot=_abstain_slot(as_of_slot),
    )


def _unsupported(message: str, as_of_slot: Slot) -> AbstainResult:
    return AbstainResult(
        reason=AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
        message=message,
        as_of_slot=_abstain_slot(as_of_slot),
    )


def _abstain_slot(as_of_slot: object) -> int:
    if type(as_of_slot) is int:
        return as_of_slot
    return -1
