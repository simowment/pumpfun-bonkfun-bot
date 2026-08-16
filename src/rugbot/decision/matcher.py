"""Pure known-operator launch matcher contracts."""

from dataclasses import dataclass

from rugbot.decision.snapshots import LaunchMatcherSnapshot
from rugbot.domain.amounts import Slot
from rugbot.domain.decisions import AbstainReason, AbstainResult
from rugbot.graph.entity_resolution import AddressRole, AddressRoleAssignment
from rugbot.graph.operator_profile import (
    CampaignSegment,
    OperatorAddressProfile,
    OperatorProfileSnapshot,
    OperatorRegimeKind,
    RegimeClassification,
)
from rugbot.graph.point_in_time import PROBABILITY_PPM_DENOMINATOR


@dataclass(frozen=True, slots=True)
class LaunchAddressSignal:
    """Observed launch address signal from an already decoded launch artifact."""

    as_of_slot: Slot
    launch_id: str
    address: str
    role: AddressRole
    signal_probability_ppm: int
    evidence_ids: tuple[str, ...]
    source_version: str


@dataclass(frozen=True, slots=True)
class KnownLaunchMatcherConfig:
    """Thresholds and versions for known-operator launch matching."""

    as_of_slot: Slot
    matcher_version: str
    entity_graph_snapshot_version: str
    min_signal_probability_ppm: int
    min_address_probability_ppm: int
    min_profile_role_probability_ppm: int
    min_entity_probability_ppm: int
    min_regime_probability_ppm: int
    min_required_role_matches: int


@dataclass(frozen=True, slots=True)
class LaunchRoleMatch:
    """One role-level address match between a launch and an operator profile."""

    as_of_slot: Slot
    launch_id: str
    entity_id: str
    campaign_id: str
    regime_id: str
    address: str
    role: AddressRole
    address_probability_ppm: int
    profile_role_probability_ppm: int
    signal_probability_ppm: int
    match_probability_ppm: int
    evidence_ids: tuple[str, ...]
    source_version: str
    profile_model_version: str


@dataclass(frozen=True, slots=True)
class KnownLaunchMatchResult:
    """Known-operator launch match result with decision snapshot output."""

    as_of_slot: Slot
    launch_id: str
    campaign_id: str
    regime_kind: OperatorRegimeKind
    matched_role_count: int
    best_match_probability_ppm: int
    matcher_snapshot: LaunchMatcherSnapshot
    role_matches: tuple[LaunchRoleMatch, ...]
    reason_codes: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class _ActiveProfileContext:
    profile: OperatorProfileSnapshot
    campaign: CampaignSegment
    regime: RegimeClassification
    config: KnownLaunchMatcherConfig


def match_known_operator_launch(
    *,
    signals: tuple[LaunchAddressSignal, ...],
    profile: OperatorProfileSnapshot,
    config: KnownLaunchMatcherConfig,
) -> KnownLaunchMatchResult | AbstainResult:
    """Match explicit launch address signals against a known operator profile."""

    validation_error = _validate_match_inputs(
        signals=signals,
        profile=profile,
        config=config,
    )
    if validation_error is not None:
        return validation_error

    context = _active_profile_context(profile=profile, config=config)
    if isinstance(context, AbstainResult):
        return context

    role_matches = _role_matches(
        signals=signals,
        context=context,
    )
    best_match = _validated_best_match(
        role_matches=role_matches,
        context=context,
    )
    if isinstance(best_match, AbstainResult):
        return best_match
    return _match_result(
        best_match=best_match, role_matches=role_matches, context=context
    )


def _active_profile_context(
    *,
    profile: OperatorProfileSnapshot,
    config: KnownLaunchMatcherConfig,
) -> _ActiveProfileContext | AbstainResult:
    current_regime = _current_active_regime(profile)
    if current_regime is None:
        return _missing("operator profile has no active regime", config)
    if current_regime.regime_kind is OperatorRegimeKind.UNKNOWN_OR_NEW_REGIME:
        return _missing("operator regime is observe-only or unknown", config)
    campaign = _campaign_for_regime(profile, current_regime)
    if campaign is None:
        return _unsupported("active regime does not have an active campaign", config)
    return _ActiveProfileContext(
        profile=profile,
        campaign=campaign,
        regime=current_regime,
        config=config,
    )


def _validated_best_match(
    *,
    role_matches: tuple[LaunchRoleMatch, ...],
    context: _ActiveProfileContext,
) -> LaunchRoleMatch | AbstainResult:
    config = context.config
    if len(role_matches) < config.min_required_role_matches:
        return _missing("known launch match lacks required role support", config)
    best_match = _best_role_match(role_matches)
    if best_match.match_probability_ppm < config.min_entity_probability_ppm:
        return _missing("known launch entity probability is below threshold", config)
    if context.regime.regime_probability_ppm < config.min_regime_probability_ppm:
        return _missing("known launch regime probability is below threshold", config)
    return best_match


def _match_result(
    *,
    best_match: LaunchRoleMatch,
    role_matches: tuple[LaunchRoleMatch, ...],
    context: _ActiveProfileContext,
) -> KnownLaunchMatchResult:
    profile = context.profile
    campaign = context.campaign
    regime = context.regime
    config = context.config
    snapshot = LaunchMatcherSnapshot(
        as_of_slot=config.as_of_slot,
        entity_id=profile.entity_id,
        regime_id=regime.regime_id,
        entity_probability_ppm=best_match.match_probability_ppm,
        regime_probability_ppm=regime.regime_probability_ppm,
        entity_graph_snapshot_version=config.entity_graph_snapshot_version,
        operator_profile_version=profile.profile_version,
        regime_model_version=regime.model_version,
        matcher_version=config.matcher_version,
    )
    return KnownLaunchMatchResult(
        as_of_slot=config.as_of_slot,
        launch_id=best_match.launch_id,
        campaign_id=campaign.campaign_id,
        regime_kind=regime.regime_kind,
        matched_role_count=len(role_matches),
        best_match_probability_ppm=best_match.match_probability_ppm,
        matcher_snapshot=snapshot,
        role_matches=tuple(
            sorted(role_matches, key=lambda match: (match.role.value, match.address))
        ),
        reason_codes=("known_operator_launch_matched",),
    )


def _validate_match_inputs(
    *,
    signals: tuple[LaunchAddressSignal, ...],
    profile: OperatorProfileSnapshot,
    config: KnownLaunchMatcherConfig,
) -> AbstainResult | None:
    config_error = _validate_config(config)
    if config_error is not None:
        return config_error
    profile_error = _validate_profile(profile, config)
    if profile_error is not None:
        return profile_error
    return _validate_signals(signals, config)


def _validate_signals(
    signals: tuple[LaunchAddressSignal, ...],
    config: KnownLaunchMatcherConfig,
) -> AbstainResult | None:
    if type(signals) is not tuple or not signals:
        return _missing("launch address signals are required", config)
    signal_keys: set[tuple[str, str, AddressRole]] = set()
    launch_ids: set[str] = set()
    for signal in signals:
        signal_error = _validate_signal(signal, config)
        if signal_error is not None:
            return signal_error
        launch_ids.add(signal.launch_id)
        signal_key = (signal.launch_id, signal.address, signal.role)
        if signal_key in signal_keys:
            return _unsupported("duplicate launch address signal", config)
        signal_keys.add(signal_key)
    if len(launch_ids) != 1:
        return _unsupported("launch address signals must belong to one launch", config)
    return None


def _validate_config(config: object) -> AbstainResult | None:
    if type(config) is not KnownLaunchMatcherConfig:
        return _unsupported_at_slot(
            "known launch matcher config is malformed",
            _object_as_of_slot(config),
        )
    if not _non_negative_int(config.as_of_slot):
        return _unsupported("as_of_slot must be non-negative", config)
    version_error = _require_versions(
        config,
        {
            "matcher_version": config.matcher_version,
            "entity_graph_snapshot_version": config.entity_graph_snapshot_version,
        },
    )
    if version_error is not None:
        return version_error
    if not _positive_int(config.min_required_role_matches):
        return _unsupported("min_required_role_matches must be positive", config)
    return _validate_probability_fields(
        config,
        {
            "min_signal_probability_ppm": config.min_signal_probability_ppm,
            "min_address_probability_ppm": config.min_address_probability_ppm,
            "min_profile_role_probability_ppm": (
                config.min_profile_role_probability_ppm
            ),
            "min_entity_probability_ppm": config.min_entity_probability_ppm,
            "min_regime_probability_ppm": config.min_regime_probability_ppm,
        },
    )


def _validate_profile(
    profile: object,
    config: KnownLaunchMatcherConfig,
) -> AbstainResult | None:
    if type(profile) is not OperatorProfileSnapshot:
        return _unsupported("operator profile is malformed", config)
    for validation in (
        _validate_profile_metadata,
        _validate_profile_counts,
        _validate_profile_addresses,
        _validate_profile_campaigns,
        _validate_profile_regimes,
    ):
        validation_error = validation(profile, config)
        if validation_error is not None:
            return validation_error
    return None


def _validate_profile_metadata(
    profile: OperatorProfileSnapshot,
    config: KnownLaunchMatcherConfig,
) -> AbstainResult | None:
    slot_error = _validate_same_as_of_slot(
        observed_slot=profile.as_of_slot,
        label="operator profile",
        config=config,
    )
    if slot_error is not None:
        return slot_error
    missing_id = _require_features(config, {"entity_id": profile.entity_id})
    if missing_id is not None:
        return missing_id
    version_error = _require_versions(
        config,
        {
            "profile_version": profile.profile_version,
            "entity_resolver_version": profile.entity_resolver_version,
            "role_classifier_version": profile.role_classifier_version,
        },
    )
    if version_error is not None:
        return version_error
    if not _valid_evidence_ids(profile.reason_codes):
        return _missing("operator profile reason_codes are required", config)
    return None


def _validate_profile_counts(
    profile: OperatorProfileSnapshot,
    config: KnownLaunchMatcherConfig,
) -> AbstainResult | None:
    count_error = _validate_profile_count_values(profile, config)
    if count_error is not None:
        return count_error
    active_error = _validate_profile_active_counts(profile, config)
    if active_error is not None:
        return active_error
    return _validate_profile_source_counts(profile, config)


def _validate_profile_count_values(
    profile: OperatorProfileSnapshot,
    config: KnownLaunchMatcherConfig,
) -> AbstainResult | None:
    collection_error = _validate_profile_collection_shapes(profile, config)
    if collection_error is not None:
        return collection_error
    counts = (
        profile.source_membership_count,
        profile.active_address_count,
        profile.source_campaign_count,
        profile.active_campaign_count,
        profile.source_regime_count,
        profile.active_regime_count,
    )
    if any(not _non_negative_int(count) for count in counts):
        return _unsupported("operator profile counts must be non-negative", config)
    return None


def _validate_profile_collection_shapes(
    profile: OperatorProfileSnapshot,
    config: KnownLaunchMatcherConfig,
) -> AbstainResult | None:
    collections = (
        profile.addresses,
        profile.campaigns,
        profile.regimes,
    )
    if any(type(collection) is not tuple for collection in collections):
        return _unsupported(
            "operator profile artifact collections must be immutable",
            config,
        )
    return None


def _validate_profile_active_counts(
    profile: OperatorProfileSnapshot,
    config: KnownLaunchMatcherConfig,
) -> AbstainResult | None:
    if profile.active_address_count != len(profile.addresses):
        return _unsupported("active_address_count mismatch", config)
    if profile.active_campaign_count != len(profile.campaigns):
        return _unsupported("active_campaign_count mismatch", config)
    if profile.active_regime_count != len(profile.regimes):
        return _unsupported("active_regime_count mismatch", config)
    return None


def _validate_profile_source_counts(
    profile: OperatorProfileSnapshot,
    config: KnownLaunchMatcherConfig,
) -> AbstainResult | None:
    if profile.active_address_count > profile.source_membership_count:
        return _unsupported(
            "active_address_count exceeds source membership count", config
        )
    if profile.active_campaign_count > profile.source_campaign_count:
        return _unsupported(
            "active_campaign_count exceeds source campaign count", config
        )
    if profile.active_regime_count > profile.source_regime_count:
        return _unsupported("active_regime_count exceeds source regime count", config)
    return None


def _validate_profile_addresses(
    profile: OperatorProfileSnapshot,
    config: KnownLaunchMatcherConfig,
) -> AbstainResult | None:
    if type(profile.addresses) is not tuple or not profile.addresses:
        return _missing("operator profile addresses are required", config)
    seen: set[str] = set()
    for address in profile.addresses:
        address_error = _validate_profile_address(address, profile, config)
        if address_error is not None:
            return address_error
        if address.address in seen:
            return _unsupported("operator profile has duplicate address", config)
        seen.add(address.address)
    return None


def _validate_profile_address(
    address: object,
    profile: OperatorProfileSnapshot,
    config: KnownLaunchMatcherConfig,
) -> AbstainResult | None:
    if type(address) is not OperatorAddressProfile:
        return _unsupported("operator address is malformed", config)
    identity_error = _validate_profile_address_identity(address, profile, config)
    if identity_error is not None:
        return identity_error
    probability_error = _validate_profile_address_probabilities(address, config)
    if probability_error is not None:
        return probability_error
    if not _valid_evidence_ids(address.evidence_ids):
        return _missing("operator address evidence_ids are required", config)
    if not isinstance(address.model_version, str) or not address.model_version:
        return _decoder_mismatch("operator address model_version is required", config)
    return _validate_profile_address_roles(address, config)


def _validate_profile_address_identity(
    address: OperatorAddressProfile,
    profile: OperatorProfileSnapshot,
    config: KnownLaunchMatcherConfig,
) -> AbstainResult | None:
    slot_error = _validate_same_as_of_slot(
        observed_slot=address.as_of_slot,
        label="operator address",
        config=config,
    )
    if slot_error is not None:
        return slot_error
    if address.entity_id != profile.entity_id:
        return _unsupported("operator address entity_id mismatch", config)
    if not isinstance(address.address, str) or not address.address:
        return _missing("operator address is required", config)
    return None


def _validate_profile_address_probabilities(
    address: OperatorAddressProfile,
    config: KnownLaunchMatcherConfig,
) -> AbstainResult | None:
    return _validate_probability_fields(
        config,
        {
            "same_controller_probability_ppm": (
                address.same_controller_probability_ppm
            ),
            "cooperating_probability_ppm": address.cooperating_probability_ppm,
            "shared_service_probability_ppm": address.shared_service_probability_ppm,
            "incidental_interaction_probability_ppm": (
                address.incidental_interaction_probability_ppm
            ),
        },
    )


def _validate_profile_address_roles(
    address: OperatorAddressProfile,
    config: KnownLaunchMatcherConfig,
) -> AbstainResult | None:
    if type(address.probable_roles) is not tuple:
        return _unsupported("operator address roles must be immutable", config)
    seen: set[AddressRole] = set()
    for assignment in address.probable_roles:
        assignment_error = _validate_role_assignment(assignment, address, config)
        if assignment_error is not None:
            return assignment_error
        if assignment.role in seen:
            return _unsupported("operator address has duplicate role", config)
        seen.add(assignment.role)
    return None


def _validate_role_assignment(
    assignment: object,
    address: OperatorAddressProfile,
    config: KnownLaunchMatcherConfig,
) -> AbstainResult | None:
    if type(assignment) is not AddressRoleAssignment:
        return _unsupported("operator role assignment is malformed", config)
    identity_error = _validate_role_assignment_identity(assignment, address, config)
    if identity_error is not None:
        return identity_error
    probability_error = _validate_role_assignment_probability(assignment, config)
    if probability_error is not None:
        return probability_error
    return _validate_role_assignment_provenance(assignment, config)


def _validate_role_assignment_identity(
    assignment: AddressRoleAssignment,
    address: OperatorAddressProfile,
    config: KnownLaunchMatcherConfig,
) -> AbstainResult | None:
    slot_error = _validate_same_as_of_slot(
        observed_slot=assignment.as_of_slot,
        label="operator role assignment",
        config=config,
    )
    if slot_error is not None:
        return slot_error
    if assignment.address != address.address:
        return _unsupported("operator role assignment address mismatch", config)
    if not isinstance(assignment.role, AddressRole):
        return _unsupported("operator role assignment role is invalid", config)
    return None


def _validate_role_assignment_probability(
    assignment: AddressRoleAssignment,
    config: KnownLaunchMatcherConfig,
) -> AbstainResult | None:
    if not _valid_probability_ppm(assignment.role_probability_ppm):
        return _unsupported("operator role probability is invalid", config)
    return None


def _validate_role_assignment_provenance(
    assignment: AddressRoleAssignment,
    config: KnownLaunchMatcherConfig,
) -> AbstainResult | None:
    if not _valid_evidence_ids(assignment.evidence_ids):
        return _missing("operator role evidence_ids are required", config)
    if not isinstance(assignment.model_version, str) or not assignment.model_version:
        return _decoder_mismatch("operator role model_version is required", config)
    return None


def _validate_profile_campaigns(
    profile: OperatorProfileSnapshot,
    config: KnownLaunchMatcherConfig,
) -> AbstainResult | None:
    if type(profile.campaigns) is not tuple or not profile.campaigns:
        return _missing("operator profile campaigns are required", config)
    seen: set[str] = set()
    for campaign in profile.campaigns:
        campaign_error = _validate_campaign(campaign, profile, config)
        if campaign_error is not None:
            return campaign_error
        if campaign.campaign_id in seen:
            return _unsupported("operator profile has duplicate campaign", config)
        seen.add(campaign.campaign_id)
    return None


def _validate_campaign(
    campaign: object,
    profile: OperatorProfileSnapshot,
    config: KnownLaunchMatcherConfig,
) -> AbstainResult | None:
    if type(campaign) is not CampaignSegment:
        return _unsupported("campaign segment is malformed", config)
    identity_error = _validate_campaign_identity(campaign, profile, config)
    if identity_error is not None:
        return identity_error
    metric_error = _validate_campaign_metrics(campaign, config)
    if metric_error is not None:
        return metric_error
    return _validate_campaign_provenance(campaign, config)


def _validate_campaign_identity(
    campaign: CampaignSegment,
    profile: OperatorProfileSnapshot,
    config: KnownLaunchMatcherConfig,
) -> AbstainResult | None:
    slot_error = _validate_same_as_of_slot(
        observed_slot=campaign.as_of_slot,
        label="campaign segment",
        config=config,
    )
    if slot_error is not None:
        return slot_error
    if campaign.entity_id != profile.entity_id:
        return _unsupported("campaign entity_id mismatch", config)
    if not isinstance(campaign.campaign_id, str) or not campaign.campaign_id:
        return _missing("campaign_id is required", config)
    return None


def _validate_campaign_metrics(
    campaign: CampaignSegment,
    config: KnownLaunchMatcherConfig,
) -> AbstainResult | None:
    if not _valid_probability_ppm(campaign.campaign_probability_ppm):
        return _unsupported("campaign probability is invalid", config)
    if not _non_negative_int(campaign.launch_count):
        return _unsupported("campaign launch_count must be non-negative", config)
    return None


def _validate_campaign_provenance(
    campaign: CampaignSegment,
    config: KnownLaunchMatcherConfig,
) -> AbstainResult | None:
    if not _valid_evidence_ids(campaign.evidence_ids):
        return _missing("campaign evidence_ids are required", config)
    if not isinstance(campaign.model_version, str) or not campaign.model_version:
        return _decoder_mismatch("campaign model_version is required", config)
    return None


def _validate_profile_regimes(
    profile: OperatorProfileSnapshot,
    config: KnownLaunchMatcherConfig,
) -> AbstainResult | None:
    if type(profile.regimes) is not tuple or not profile.regimes:
        return _missing("operator profile regimes are required", config)
    seen: set[str] = set()
    for regime in profile.regimes:
        regime_error = _validate_regime(regime, profile, config)
        if regime_error is not None:
            return regime_error
        if regime.regime_id in seen:
            return _unsupported("operator profile has duplicate regime", config)
        seen.add(regime.regime_id)
    return None


def _validate_regime(
    regime: object,
    profile: OperatorProfileSnapshot,
    config: KnownLaunchMatcherConfig,
) -> AbstainResult | None:
    if type(regime) is not RegimeClassification:
        return _unsupported("regime classification is malformed", config)
    identity_error = _validate_regime_identity(regime, profile, config)
    if identity_error is not None:
        return identity_error
    metric_error = _validate_regime_metrics(regime, config)
    if metric_error is not None:
        return metric_error
    return _validate_regime_provenance(regime, config)


def _validate_regime_identity(
    regime: RegimeClassification,
    profile: OperatorProfileSnapshot,
    config: KnownLaunchMatcherConfig,
) -> AbstainResult | None:
    slot_error = _validate_same_as_of_slot(
        observed_slot=regime.as_of_slot,
        label="regime classification",
        config=config,
    )
    if slot_error is not None:
        return slot_error
    if regime.entity_id != profile.entity_id:
        return _unsupported("regime entity_id mismatch", config)
    if not isinstance(regime.campaign_id, str) or not regime.campaign_id:
        return _missing("regime campaign_id is required", config)
    if not isinstance(regime.regime_id, str) or not regime.regime_id:
        return _missing("regime_id is required", config)
    if not isinstance(regime.regime_kind, OperatorRegimeKind):
        return _unsupported("regime_kind is invalid", config)
    return None


def _validate_regime_metrics(
    regime: RegimeClassification,
    config: KnownLaunchMatcherConfig,
) -> AbstainResult | None:
    probability_error = _validate_probability_fields(
        config,
        {"regime_probability_ppm": regime.regime_probability_ppm},
    )
    if probability_error is not None:
        return probability_error
    if not _non_negative_int(regime.support_launch_count):
        return _unsupported("regime support count must be non-negative", config)
    return None


def _validate_regime_provenance(
    regime: RegimeClassification,
    config: KnownLaunchMatcherConfig,
) -> AbstainResult | None:
    if not _valid_evidence_ids(regime.evidence_ids):
        return _missing("regime evidence_ids are required", config)
    if not isinstance(regime.model_version, str) or not regime.model_version:
        return _decoder_mismatch("regime model_version is required", config)
    return None


def _validate_signal(
    signal: object,
    config: KnownLaunchMatcherConfig,
) -> AbstainResult | None:
    if type(signal) is not LaunchAddressSignal:
        return _unsupported("launch address signal is malformed", config)
    identity_error = _validate_signal_identity(signal, config)
    if identity_error is not None:
        return identity_error
    metric_error = _validate_signal_metrics(signal, config)
    if metric_error is not None:
        return metric_error
    return _validate_signal_provenance(signal, config)


def _validate_signal_identity(
    signal: LaunchAddressSignal,
    config: KnownLaunchMatcherConfig,
) -> AbstainResult | None:
    slot_error = _validate_same_as_of_slot(
        observed_slot=signal.as_of_slot,
        label="launch address signal",
        config=config,
    )
    if slot_error is not None:
        return slot_error
    if not isinstance(signal.launch_id, str) or not signal.launch_id:
        return _missing("launch_id is required", config)
    if not isinstance(signal.address, str) or not signal.address:
        return _missing("signal address is required", config)
    if not isinstance(signal.role, AddressRole):
        return _unsupported("signal role is invalid", config)
    return None


def _validate_signal_metrics(
    signal: LaunchAddressSignal,
    config: KnownLaunchMatcherConfig,
) -> AbstainResult | None:
    if not _valid_probability_ppm(signal.signal_probability_ppm):
        return _unsupported("signal probability is invalid", config)
    return None


def _validate_signal_provenance(
    signal: LaunchAddressSignal,
    config: KnownLaunchMatcherConfig,
) -> AbstainResult | None:
    if not _valid_evidence_ids(signal.evidence_ids):
        return _missing("signal evidence_ids are required", config)
    if not isinstance(signal.source_version, str) or not signal.source_version:
        return _decoder_mismatch("signal source_version is required", config)
    return None


def _current_active_regime(
    profile: OperatorProfileSnapshot,
) -> RegimeClassification | None:
    if profile.current_active_regime_id is None:
        return None
    for regime in profile.regimes:
        if regime.regime_id == profile.current_active_regime_id:
            return regime
    return None


def _campaign_for_regime(
    profile: OperatorProfileSnapshot,
    regime: RegimeClassification,
) -> CampaignSegment | None:
    for campaign in profile.campaigns:
        if campaign.campaign_id == regime.campaign_id:
            return campaign
    return None


def _role_matches(
    *,
    signals: tuple[LaunchAddressSignal, ...],
    context: _ActiveProfileContext,
) -> tuple[LaunchRoleMatch, ...]:
    profile = context.profile
    config = context.config
    addresses = {address.address: address for address in profile.addresses}
    matches = tuple(
        match
        for signal in signals
        if signal.signal_probability_ppm >= config.min_signal_probability_ppm
        for match in (
            _role_match(
                signal=signal,
                address=addresses.get(signal.address),
                context=context,
            ),
        )
        if match is not None
    )
    return matches


def _role_match(
    *,
    signal: LaunchAddressSignal,
    address: OperatorAddressProfile | None,
    context: _ActiveProfileContext,
) -> LaunchRoleMatch | None:
    config = context.config
    if address is None:
        return None
    address_probability = _address_probability(address)
    if address_probability < config.min_address_probability_ppm:
        return None
    role_assignment = _role_assignment_for_signal(signal, address, config)
    if role_assignment is None:
        return None
    match_probability = min(
        address_probability,
        role_assignment.role_probability_ppm,
        signal.signal_probability_ppm,
        context.campaign.campaign_probability_ppm,
        context.regime.regime_probability_ppm,
    )
    return LaunchRoleMatch(
        as_of_slot=config.as_of_slot,
        launch_id=signal.launch_id,
        entity_id=context.profile.entity_id,
        campaign_id=context.campaign.campaign_id,
        regime_id=context.regime.regime_id,
        address=signal.address,
        role=signal.role,
        address_probability_ppm=address_probability,
        profile_role_probability_ppm=role_assignment.role_probability_ppm,
        signal_probability_ppm=signal.signal_probability_ppm,
        match_probability_ppm=match_probability,
        evidence_ids=tuple(
            dict.fromkeys(
                (
                    *signal.evidence_ids,
                    *address.evidence_ids,
                    *role_assignment.evidence_ids,
                    *context.campaign.evidence_ids,
                    *context.regime.evidence_ids,
                )
            )
        ),
        source_version=signal.source_version,
        profile_model_version=address.model_version,
    )


def _role_assignment_for_signal(
    signal: LaunchAddressSignal,
    address: OperatorAddressProfile,
    config: KnownLaunchMatcherConfig,
) -> AddressRoleAssignment | None:
    for assignment in address.probable_roles:
        if (
            assignment.role is signal.role
            and assignment.role_probability_ppm
            >= config.min_profile_role_probability_ppm
        ):
            return assignment
    return None


def _address_probability(address: OperatorAddressProfile) -> int:
    return max(
        address.same_controller_probability_ppm,
        address.cooperating_probability_ppm,
    )


def _best_role_match(matches: tuple[LaunchRoleMatch, ...]) -> LaunchRoleMatch:
    return max(
        matches,
        key=lambda match: (
            match.match_probability_ppm,
            match.profile_role_probability_ppm,
            match.signal_probability_ppm,
            match.role.value,
            match.address,
        ),
    )


def _require_versions(
    context: KnownLaunchMatcherConfig,
    fields: dict[str, str],
) -> AbstainResult | None:
    for field_name, value in fields.items():
        if not isinstance(value, str) or not value:
            return _decoder_mismatch(f"{field_name} is required", context)
    return None


def _require_features(
    context: KnownLaunchMatcherConfig,
    fields: dict[str, str],
) -> AbstainResult | None:
    for field_name, value in fields.items():
        if not isinstance(value, str) or not value:
            return _missing(f"{field_name} is required", context)
    return None


def _validate_probability_fields(
    context: KnownLaunchMatcherConfig,
    fields: dict[str, int],
) -> AbstainResult | None:
    for field_name, value in fields.items():
        if not _valid_probability_ppm(value):
            return _unsupported(
                f"{field_name} must be in probability ppm range",
                context,
            )
    return None


def _validate_same_as_of_slot(
    *,
    observed_slot: object,
    label: str,
    config: KnownLaunchMatcherConfig,
) -> AbstainResult | None:
    if not _non_negative_int(observed_slot):
        return _unsupported(f"{label} as_of_slot is invalid", config)
    if observed_slot != config.as_of_slot:
        return _stale(f"{label} uses a different as_of_slot", config)
    return None


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


def _positive_int(value: object) -> bool:
    return type(value) is int and value > 0


def _non_negative_int(value: object) -> bool:
    return type(value) is int and value >= 0


def _missing(message: str, context: KnownLaunchMatcherConfig) -> AbstainResult:
    return _abstain(
        reason=AbstainReason.MISSING_FEATURE,
        message=message,
        as_of_slot=context.as_of_slot,
    )


def _decoder_mismatch(
    message: str,
    context: KnownLaunchMatcherConfig,
) -> AbstainResult:
    return _abstain(
        reason=AbstainReason.DECODER_MISMATCH,
        message=message,
        as_of_slot=context.as_of_slot,
    )


def _stale(message: str, context: KnownLaunchMatcherConfig) -> AbstainResult:
    return _abstain(
        reason=AbstainReason.STALE_STATE,
        message=message,
        as_of_slot=context.as_of_slot,
    )


def _unsupported(message: str, context: KnownLaunchMatcherConfig) -> AbstainResult:
    return _abstain(
        reason=AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
        message=message,
        as_of_slot=context.as_of_slot,
    )


def _unsupported_at_slot(message: str, as_of_slot: object) -> AbstainResult:
    return _abstain(
        reason=AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
        message=message,
        as_of_slot=as_of_slot,
    )


def _abstain(
    *,
    reason: AbstainReason,
    message: str,
    as_of_slot: object,
) -> AbstainResult:
    return AbstainResult(
        reason=reason,
        message=message,
        as_of_slot=_abstain_slot(as_of_slot),
    )


def _abstain_slot(as_of_slot: object) -> int:
    if type(as_of_slot) is int:
        return as_of_slot
    return -1


def _object_as_of_slot(value: object) -> object:
    return getattr(value, "as_of_slot", -1)
