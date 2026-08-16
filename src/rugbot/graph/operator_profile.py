"""Pure point-in-time operator profile contracts."""

from dataclasses import dataclass
from enum import Enum

from rugbot.domain.amounts import Slot
from rugbot.domain.decisions import AbstainReason, AbstainResult
from rugbot.graph.entity_resolution import (
    AddressRole,
    AddressRoleAssignment,
    AddressRoleSnapshot,
    EntityMembership,
    ProbabilisticEntity,
)
from rugbot.graph.point_in_time import PROBABILITY_PPM_DENOMINATOR


class OperatorRegimeKind(Enum):
    """Supported operator behavior regimes."""

    INSTANT_DUMP = "instant_dump"
    FAKE_PUMP_THEN_FULL_DUMP = "fake_pump_then_full_dump"
    FAKE_PUMP_THEN_STAGED_DUMP = "fake_pump_then_staged_dump"
    TARGET_RESERVE_DUMP = "target_reserve_dump"
    TARGET_BUYER_COUNT_DUMP = "target_buyer_count_dump"
    TIMEOUT_DUMP = "timeout_dump"
    CURVE_COMPLETION_STRATEGY = "curve_completion_strategy"
    UNKNOWN_OR_NEW_REGIME = "unknown_or_new_regime"


@dataclass(frozen=True, slots=True)
class CampaignEvidence:
    """Point-in-time campaign membership evidence for one entity."""

    as_of_slot: Slot
    entity_id: str
    campaign_id: str
    valid_from_slot: Slot
    valid_to_slot: Slot | None
    campaign_probability_ppm: int
    launch_count: int
    evidence_ids: tuple[str, ...]
    model_version: str


@dataclass(frozen=True, slots=True)
class RegimeEvidence:
    """Point-in-time behavior-regime evidence for one entity campaign."""

    as_of_slot: Slot
    entity_id: str
    campaign_id: str
    regime_id: str
    regime_kind: OperatorRegimeKind
    valid_from_slot: Slot
    valid_to_slot: Slot | None
    regime_probability_ppm: int
    support_launch_count: int
    evidence_ids: tuple[str, ...]
    model_version: str


@dataclass(frozen=True, slots=True)
class OperatorProfileBuildConfig:
    """Thresholds and versions for building one operator profile snapshot."""

    as_of_slot: Slot
    entity_id: str
    profile_version: str
    min_membership_probability_ppm: int
    min_role_probability_ppm: int
    min_campaign_probability_ppm: int
    min_regime_probability_ppm: int
    min_regime_support_launches: int


@dataclass(frozen=True, slots=True)
class OperatorAddressProfile:
    """Address membership and role view inside an operator profile."""

    as_of_slot: Slot
    entity_id: str
    address: str
    same_controller_probability_ppm: int
    cooperating_probability_ppm: int
    shared_service_probability_ppm: int
    incidental_interaction_probability_ppm: int
    probable_roles: tuple[AddressRoleAssignment, ...]
    evidence_ids: tuple[str, ...]
    model_version: str


@dataclass(frozen=True, slots=True)
class CampaignSegment:
    """Active campaign segment in an operator profile."""

    as_of_slot: Slot
    entity_id: str
    campaign_id: str
    campaign_probability_ppm: int
    launch_count: int
    evidence_ids: tuple[str, ...]
    model_version: str


@dataclass(frozen=True, slots=True)
class RegimeClassification:
    """Active behavior-regime classification for one campaign."""

    as_of_slot: Slot
    entity_id: str
    campaign_id: str
    regime_id: str
    regime_kind: OperatorRegimeKind
    regime_probability_ppm: int
    support_launch_count: int
    evidence_ids: tuple[str, ...]
    model_version: str


@dataclass(frozen=True, slots=True)
class OperatorProfileSnapshot:
    """Immutable operator profile snapshot for one point in time."""

    as_of_slot: Slot
    entity_id: str
    profile_version: str
    entity_resolver_version: str
    role_classifier_version: str
    addresses: tuple[OperatorAddressProfile, ...]
    campaigns: tuple[CampaignSegment, ...]
    regimes: tuple[RegimeClassification, ...]
    current_active_regime_id: str | None
    source_membership_count: int
    active_address_count: int
    source_campaign_count: int
    active_campaign_count: int
    source_regime_count: int
    active_regime_count: int
    reason_codes: tuple[str, ...]


def build_operator_profile_snapshot(
    *,
    entity: ProbabilisticEntity,
    roles: AddressRoleSnapshot,
    campaigns: tuple[CampaignEvidence, ...],
    regimes: tuple[RegimeEvidence, ...],
    config: OperatorProfileBuildConfig,
) -> OperatorProfileSnapshot | AbstainResult:
    """Build a pure point-in-time operator profile snapshot."""

    validation_error = _validate_profile_inputs(
        entity=entity,
        roles=roles,
        campaigns=campaigns,
        regimes=regimes,
        config=config,
    )
    if validation_error is not None:
        return validation_error

    addresses = _active_addresses(entity=entity, roles=roles, config=config)
    if not addresses:
        return _missing("operator profile requires active address memberships", config)

    active_campaigns = _active_campaigns(campaigns=campaigns, config=config)
    active_regimes = _active_regimes(
        regimes=regimes,
        config=config,
        active_campaign_ids=tuple(
            campaign.campaign_id for campaign in active_campaigns
        ),
    )
    current_regime = _current_active_regime(active_regimes)
    return OperatorProfileSnapshot(
        as_of_slot=config.as_of_slot,
        entity_id=config.entity_id,
        profile_version=config.profile_version,
        entity_resolver_version=entity.resolver_version,
        role_classifier_version=roles.classifier_version,
        addresses=addresses,
        campaigns=active_campaigns,
        regimes=active_regimes,
        current_active_regime_id=(
            current_regime.regime_id if current_regime is not None else None
        ),
        source_membership_count=len(entity.memberships),
        active_address_count=len(addresses),
        source_campaign_count=len(campaigns),
        active_campaign_count=len(active_campaigns),
        source_regime_count=len(regimes),
        active_regime_count=len(active_regimes),
        reason_codes=("operator_profile_built",),
    )


def _validate_profile_inputs(
    *,
    entity: ProbabilisticEntity,
    roles: AddressRoleSnapshot,
    campaigns: tuple[CampaignEvidence, ...],
    regimes: tuple[RegimeEvidence, ...],
    config: OperatorProfileBuildConfig,
) -> AbstainResult | None:
    config_error = _validate_config(config)
    if config_error is not None:
        return config_error
    entity_error = _validate_entity(entity, config)
    if entity_error is not None:
        return entity_error
    role_error = _validate_roles(roles, config)
    if role_error is not None:
        return role_error
    for campaign in campaigns:
        campaign_error = _validate_campaign(campaign, config)
        if campaign_error is not None:
            return campaign_error
    for regime in regimes:
        regime_error = _validate_regime(regime, config)
        if regime_error is not None:
            return regime_error
    return None


def _validate_config(config: OperatorProfileBuildConfig) -> AbstainResult | None:
    if not _non_negative_int(config.as_of_slot):
        return _unsupported("as_of_slot must be a non-negative integer", config)
    if not isinstance(config.entity_id, str) or not config.entity_id:
        return _missing("entity_id is required", config)
    if not isinstance(config.profile_version, str) or not config.profile_version:
        return _decoder_mismatch("profile_version is required", config)
    threshold_error = _validate_thresholds(config)
    if threshold_error is not None:
        return threshold_error
    if not _non_negative_int(config.min_regime_support_launches):
        return _unsupported("min_regime_support_launches must be non-negative", config)
    return None


def _validate_thresholds(config: OperatorProfileBuildConfig) -> AbstainResult | None:
    thresholds = {
        "min_membership_probability_ppm": config.min_membership_probability_ppm,
        "min_role_probability_ppm": config.min_role_probability_ppm,
        "min_campaign_probability_ppm": config.min_campaign_probability_ppm,
        "min_regime_probability_ppm": config.min_regime_probability_ppm,
    }
    for field_name, value in thresholds.items():
        if not _valid_probability_ppm(value):
            return _unsupported(
                f"{field_name} must be in probability ppm range",
                config,
            )
    return None


def _validate_entity(
    entity: ProbabilisticEntity,
    config: OperatorProfileBuildConfig,
) -> AbstainResult | None:
    for validation in (
        _validate_entity_identity,
        _validate_entity_versions,
        _validate_entity_counts,
    ):
        validation_error = validation(entity, config)
        if validation_error is not None:
            return validation_error
    if not entity.memberships:
        return _missing("entity memberships are required", config)
    for membership in entity.memberships:
        membership_error = _validate_membership(membership, config)
        if membership_error is not None:
            return membership_error
    return None


def _validate_entity_counts(
    entity: ProbabilisticEntity,
    config: OperatorProfileBuildConfig,
) -> AbstainResult | None:
    counts = (
        entity.source_seed_count,
        entity.active_seed_count,
        entity.direct_relationship_count,
    )
    if any(not _non_negative_int(count) for count in counts):
        return _unsupported("entity counts must be non-negative", config)
    if entity.active_seed_count > entity.source_seed_count:
        return _unsupported("entity active_seed_count exceeds source count", config)
    return None


def _validate_entity_identity(
    entity: ProbabilisticEntity,
    config: OperatorProfileBuildConfig,
) -> AbstainResult | None:
    if entity.as_of_slot != config.as_of_slot:
        return _stale("entity snapshot uses a different as_of_slot", config)
    if entity.entity_id != config.entity_id:
        return _unsupported("entity_id mismatch", config)
    return None


def _validate_entity_versions(
    entity: ProbabilisticEntity,
    config: OperatorProfileBuildConfig,
) -> AbstainResult | None:
    if not isinstance(entity.resolver_version, str) or not entity.resolver_version:
        return _decoder_mismatch("entity resolver_version is required", config)
    if (
        not isinstance(entity.graph_snapshot_version, str)
        or not entity.graph_snapshot_version
    ):
        return _decoder_mismatch("entity graph_snapshot_version is required", config)
    return None


def _validate_membership(
    membership: EntityMembership,
    config: OperatorProfileBuildConfig,
) -> AbstainResult | None:
    for validation in (
        _validate_membership_identity,
        _validate_membership_probabilities,
        _validate_membership_provenance,
    ):
        validation_error = validation(membership, config)
        if validation_error is not None:
            return validation_error
    return None


def _validate_membership_identity(
    membership: EntityMembership,
    config: OperatorProfileBuildConfig,
) -> AbstainResult | None:
    if membership.as_of_slot != config.as_of_slot:
        return _stale("membership uses a different as_of_slot", config)
    if membership.entity_id != config.entity_id:
        return _unsupported("membership entity_id mismatch", config)
    if not isinstance(membership.address, str) or not membership.address:
        return _missing("membership address is required", config)
    return None


def _validate_membership_probabilities(
    membership: EntityMembership,
    config: OperatorProfileBuildConfig,
) -> AbstainResult | None:
    probabilities = {
        "same_controller_probability_ppm": membership.same_controller_probability_ppm,
        "cooperating_probability_ppm": membership.cooperating_probability_ppm,
        "shared_service_probability_ppm": membership.shared_service_probability_ppm,
        "incidental_interaction_probability_ppm": (
            membership.incidental_interaction_probability_ppm
        ),
    }
    for field_name, value in probabilities.items():
        if not _valid_probability_ppm(value):
            return _unsupported(
                f"membership {field_name} must be in probability ppm range",
                config,
            )
    return None


def _validate_membership_provenance(
    membership: EntityMembership,
    config: OperatorProfileBuildConfig,
) -> AbstainResult | None:
    if not _valid_evidence_ids(membership.evidence_ids):
        return _missing("membership evidence_ids are required", config)
    if not isinstance(membership.model_version, str) or not membership.model_version:
        return _decoder_mismatch("membership model_version is required", config)
    return None


def _validate_roles(
    roles: AddressRoleSnapshot,
    config: OperatorProfileBuildConfig,
) -> AbstainResult | None:
    if roles.as_of_slot != config.as_of_slot:
        return _stale("role snapshot uses a different as_of_slot", config)
    if not isinstance(roles.classifier_version, str) or not roles.classifier_version:
        return _decoder_mismatch("role classifier_version is required", config)
    count_error = _validate_role_counts(roles, config)
    if count_error is not None:
        return count_error
    for assignment in roles.assignments:
        assignment_error = _validate_role_assignment(assignment, config)
        if assignment_error is not None:
            return assignment_error
    return None


def _role_counts(roles: AddressRoleSnapshot) -> tuple[int, int, int]:
    return (
        roles.source_evidence_count,
        roles.active_evidence_count,
        roles.skipped_inactive_evidence_count,
    )


def _validate_role_counts(
    roles: AddressRoleSnapshot,
    config: OperatorProfileBuildConfig,
) -> AbstainResult | None:
    if any(not _non_negative_int(count) for count in _role_counts(roles)):
        return _unsupported("role snapshot counts must be non-negative", config)
    if roles.active_evidence_count > roles.source_evidence_count:
        return _unsupported("role active_evidence_count exceeds source count", config)
    if len(roles.assignments) > roles.active_evidence_count:
        return _unsupported("role assignments exceed active evidence count", config)
    return None


def _validate_role_assignment(
    assignment: AddressRoleAssignment,
    config: OperatorProfileBuildConfig,
) -> AbstainResult | None:
    for validation in (
        _validate_role_assignment_identity,
        _validate_role_assignment_probability,
        _validate_role_assignment_provenance,
    ):
        validation_error = validation(assignment, config)
        if validation_error is not None:
            return validation_error
    return None


def _validate_role_assignment_identity(
    assignment: AddressRoleAssignment,
    config: OperatorProfileBuildConfig,
) -> AbstainResult | None:
    if assignment.as_of_slot != config.as_of_slot:
        return _stale("role assignment uses a different as_of_slot", config)
    if not isinstance(assignment.address, str) or not assignment.address:
        return _missing("role assignment address is required", config)
    if not isinstance(assignment.role, AddressRole):
        return _unsupported("role assignment role is invalid", config)
    return None


def _validate_role_assignment_probability(
    assignment: AddressRoleAssignment,
    config: OperatorProfileBuildConfig,
) -> AbstainResult | None:
    if not _valid_probability_ppm(assignment.role_probability_ppm):
        return _unsupported(
            "role assignment probability must be in probability ppm range",
            config,
        )
    return None


def _validate_role_assignment_provenance(
    assignment: AddressRoleAssignment,
    config: OperatorProfileBuildConfig,
) -> AbstainResult | None:
    if not _valid_evidence_ids(assignment.evidence_ids):
        return _missing("role assignment evidence_ids are required", config)
    if not isinstance(assignment.model_version, str) or not assignment.model_version:
        return _decoder_mismatch("role assignment model_version is required", config)
    return None


def _validate_campaign(
    campaign: CampaignEvidence,
    config: OperatorProfileBuildConfig,
) -> AbstainResult | None:
    for validation in (
        _validate_campaign_slots,
        _validate_campaign_identity,
        _validate_campaign_metrics,
        _validate_campaign_provenance,
    ):
        validation_error = validation(campaign, config)
        if validation_error is not None:
            return validation_error
    return None


def _validate_campaign_slots(
    campaign: CampaignEvidence,
    config: OperatorProfileBuildConfig,
) -> AbstainResult | None:
    if any(
        not _non_negative_int(slot)
        for slot in (campaign.as_of_slot, campaign.valid_from_slot, config.as_of_slot)
    ):
        return _unsupported("campaign slot fields must be non-negative", config)
    if campaign.valid_to_slot is not None and not _non_negative_int(
        campaign.valid_to_slot
    ):
        return _unsupported("campaign valid_to_slot is invalid", config)
    if (
        campaign.valid_to_slot is not None
        and campaign.valid_to_slot <= campaign.valid_from_slot
    ):
        return _unsupported(
            "campaign valid_to_slot must follow valid_from_slot", config
        )
    if campaign.as_of_slot != config.as_of_slot:
        return _stale("campaign evidence uses a different as_of_slot", config)
    if campaign.valid_from_slot > config.as_of_slot:
        return _stale("campaign evidence is newer than as_of_slot", config)
    return None


def _validate_campaign_identity(
    campaign: CampaignEvidence,
    config: OperatorProfileBuildConfig,
) -> AbstainResult | None:
    if campaign.entity_id != config.entity_id:
        return _unsupported("campaign entity_id mismatch", config)
    if not isinstance(campaign.campaign_id, str) or not campaign.campaign_id:
        return _missing("campaign_id is required", config)
    return None


def _validate_campaign_metrics(
    campaign: CampaignEvidence,
    config: OperatorProfileBuildConfig,
) -> AbstainResult | None:
    if not _valid_probability_ppm(campaign.campaign_probability_ppm):
        return _unsupported(
            "campaign_probability_ppm must be in probability ppm range",
            config,
        )
    if not _non_negative_int(campaign.launch_count):
        return _unsupported("campaign launch_count must be non-negative", config)
    return None


def _validate_campaign_provenance(
    campaign: CampaignEvidence,
    config: OperatorProfileBuildConfig,
) -> AbstainResult | None:
    if not _valid_evidence_ids(campaign.evidence_ids):
        return _missing("campaign evidence_ids are required", config)
    if not isinstance(campaign.model_version, str) or not campaign.model_version:
        return _decoder_mismatch("campaign model_version is required", config)
    return None


def _validate_regime(
    regime: RegimeEvidence,
    config: OperatorProfileBuildConfig,
) -> AbstainResult | None:
    for validation in (
        _validate_regime_slots,
        _validate_regime_identity,
        _validate_regime_metrics,
        _validate_regime_provenance,
    ):
        validation_error = validation(regime, config)
        if validation_error is not None:
            return validation_error
    return None


def _validate_regime_slots(
    regime: RegimeEvidence,
    config: OperatorProfileBuildConfig,
) -> AbstainResult | None:
    if any(
        not _non_negative_int(slot)
        for slot in (regime.as_of_slot, regime.valid_from_slot, config.as_of_slot)
    ):
        return _unsupported("regime slot fields must be non-negative", config)
    if regime.valid_to_slot is not None and not _non_negative_int(regime.valid_to_slot):
        return _unsupported("regime valid_to_slot is invalid", config)
    if (
        regime.valid_to_slot is not None
        and regime.valid_to_slot <= regime.valid_from_slot
    ):
        return _unsupported("regime valid_to_slot must follow valid_from_slot", config)
    if regime.as_of_slot != config.as_of_slot:
        return _stale("regime evidence uses a different as_of_slot", config)
    if regime.valid_from_slot > config.as_of_slot:
        return _stale("regime evidence is newer than as_of_slot", config)
    return None


def _validate_regime_identity(
    regime: RegimeEvidence,
    config: OperatorProfileBuildConfig,
) -> AbstainResult | None:
    if regime.entity_id != config.entity_id:
        return _unsupported("regime entity_id mismatch", config)
    if not isinstance(regime.campaign_id, str) or not regime.campaign_id:
        return _missing("regime campaign_id is required", config)
    if not isinstance(regime.regime_id, str) or not regime.regime_id:
        return _missing("regime_id is required", config)
    if not isinstance(regime.regime_kind, OperatorRegimeKind):
        return _unsupported("regime_kind is invalid", config)
    return None


def _validate_regime_metrics(
    regime: RegimeEvidence,
    config: OperatorProfileBuildConfig,
) -> AbstainResult | None:
    if not _valid_probability_ppm(regime.regime_probability_ppm):
        return _unsupported(
            "regime_probability_ppm must be in probability ppm range",
            config,
        )
    if not _non_negative_int(regime.support_launch_count):
        return _unsupported("regime support_launch_count must be non-negative", config)
    return None


def _validate_regime_provenance(
    regime: RegimeEvidence,
    config: OperatorProfileBuildConfig,
) -> AbstainResult | None:
    if not _valid_evidence_ids(regime.evidence_ids):
        return _missing("regime evidence_ids are required", config)
    if not isinstance(regime.model_version, str) or not regime.model_version:
        return _decoder_mismatch("regime model_version is required", config)
    return None


def _active_addresses(
    *,
    entity: ProbabilisticEntity,
    roles: AddressRoleSnapshot,
    config: OperatorProfileBuildConfig,
) -> tuple[OperatorAddressProfile, ...]:
    role_lookup = _roles_by_address(roles, config)
    addresses = tuple(
        _address_profile(
            membership=membership, roles=role_lookup.get(membership.address, ())
        )
        for membership in entity.memberships
        if _membership_above_threshold(membership, config)
    )
    return tuple(sorted(addresses, key=lambda address: address.address))


def _roles_by_address(
    roles: AddressRoleSnapshot,
    config: OperatorProfileBuildConfig,
) -> dict[str, tuple[AddressRoleAssignment, ...]]:
    grouped: dict[str, list[AddressRoleAssignment]] = {}
    for assignment in roles.assignments:
        if assignment.role_probability_ppm < config.min_role_probability_ppm:
            continue
        grouped.setdefault(assignment.address, []).append(assignment)
    return {
        address: tuple(
            sorted(values, key=lambda role: (role.role.value, role.evidence_ids))
        )
        for address, values in grouped.items()
    }


def _address_profile(
    *,
    membership: EntityMembership,
    roles: tuple[AddressRoleAssignment, ...],
) -> OperatorAddressProfile:
    return OperatorAddressProfile(
        as_of_slot=membership.as_of_slot,
        entity_id=membership.entity_id,
        address=membership.address,
        same_controller_probability_ppm=membership.same_controller_probability_ppm,
        cooperating_probability_ppm=membership.cooperating_probability_ppm,
        shared_service_probability_ppm=membership.shared_service_probability_ppm,
        incidental_interaction_probability_ppm=(
            membership.incidental_interaction_probability_ppm
        ),
        probable_roles=roles,
        evidence_ids=membership.evidence_ids,
        model_version=membership.model_version,
    )


def _membership_above_threshold(
    membership: EntityMembership,
    config: OperatorProfileBuildConfig,
) -> bool:
    return (
        max(
            membership.same_controller_probability_ppm,
            membership.cooperating_probability_ppm,
        )
        >= config.min_membership_probability_ppm
    )


def _active_campaigns(
    *,
    campaigns: tuple[CampaignEvidence, ...],
    config: OperatorProfileBuildConfig,
) -> tuple[CampaignSegment, ...]:
    active = tuple(
        CampaignSegment(
            as_of_slot=config.as_of_slot,
            entity_id=campaign.entity_id,
            campaign_id=campaign.campaign_id,
            campaign_probability_ppm=campaign.campaign_probability_ppm,
            launch_count=campaign.launch_count,
            evidence_ids=campaign.evidence_ids,
            model_version=campaign.model_version,
        )
        for campaign in campaigns
        if _campaign_active_at(campaign, config.as_of_slot)
        and campaign.campaign_probability_ppm >= config.min_campaign_probability_ppm
    )
    return tuple(sorted(active, key=lambda campaign: campaign.campaign_id))


def _active_regimes(
    *,
    regimes: tuple[RegimeEvidence, ...],
    config: OperatorProfileBuildConfig,
    active_campaign_ids: tuple[str, ...],
) -> tuple[RegimeClassification, ...]:
    active_campaign_id_set = set(active_campaign_ids)
    active = tuple(
        RegimeClassification(
            as_of_slot=config.as_of_slot,
            entity_id=regime.entity_id,
            campaign_id=regime.campaign_id,
            regime_id=regime.regime_id,
            regime_kind=regime.regime_kind,
            regime_probability_ppm=regime.regime_probability_ppm,
            support_launch_count=regime.support_launch_count,
            evidence_ids=regime.evidence_ids,
            model_version=regime.model_version,
        )
        for regime in regimes
        if _regime_active_at(regime, config.as_of_slot)
        and regime.campaign_id in active_campaign_id_set
        and regime.regime_probability_ppm >= config.min_regime_probability_ppm
        and regime.support_launch_count >= config.min_regime_support_launches
    )
    return tuple(
        sorted(
            active,
            key=lambda regime: (regime.campaign_id, regime.regime_id),
        )
    )


def _current_active_regime(
    regimes: tuple[RegimeClassification, ...],
) -> RegimeClassification | None:
    if not regimes:
        return None
    return max(
        regimes,
        key=lambda regime: (regime.regime_probability_ppm, regime.support_launch_count),
    )


def _campaign_active_at(campaign: CampaignEvidence, as_of_slot: Slot) -> bool:
    return campaign.valid_to_slot is None or as_of_slot < campaign.valid_to_slot


def _regime_active_at(regime: RegimeEvidence, as_of_slot: Slot) -> bool:
    return regime.valid_to_slot is None or as_of_slot < regime.valid_to_slot


def _valid_probability_ppm(value: object) -> bool:
    return _non_negative_int(value) and value <= PROBABILITY_PPM_DENOMINATOR


def _valid_evidence_ids(evidence_ids: object) -> bool:
    return (
        type(evidence_ids) is tuple
        and bool(evidence_ids)
        and all(
            isinstance(evidence_id, str) and evidence_id for evidence_id in evidence_ids
        )
    )


def _non_negative_int(value: object) -> bool:
    return type(value) is int and value >= 0


def _missing(message: str, config: OperatorProfileBuildConfig) -> AbstainResult:
    return AbstainResult(
        reason=AbstainReason.MISSING_FEATURE,
        message=message,
        as_of_slot=_abstain_slot(config.as_of_slot),
    )


def _decoder_mismatch(
    message: str,
    config: OperatorProfileBuildConfig,
) -> AbstainResult:
    return AbstainResult(
        reason=AbstainReason.DECODER_MISMATCH,
        message=message,
        as_of_slot=_abstain_slot(config.as_of_slot),
    )


def _stale(message: str, config: OperatorProfileBuildConfig) -> AbstainResult:
    return AbstainResult(
        reason=AbstainReason.STALE_STATE,
        message=message,
        as_of_slot=_abstain_slot(config.as_of_slot),
    )


def _unsupported(message: str, config: OperatorProfileBuildConfig) -> AbstainResult:
    return AbstainResult(
        reason=AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
        message=message,
        as_of_slot=_abstain_slot(config.as_of_slot),
    )


def _abstain_slot(as_of_slot: object) -> int:
    if type(as_of_slot) is int:
        return as_of_slot
    return -1
