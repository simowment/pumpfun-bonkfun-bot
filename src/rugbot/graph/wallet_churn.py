"""Pure point-in-time operator wallet churn snapshots."""

from dataclasses import dataclass
from enum import Enum

from rugbot.domain.amounts import Slot
from rugbot.domain.decisions import AbstainReason, AbstainResult
from rugbot.graph.entity_resolution import AddressRole, AddressRoleAssignment
from rugbot.graph.operator_profile import (
    OperatorAddressProfile,
    OperatorProfileSnapshot,
)
from rugbot.graph.point_in_time import PROBABILITY_PPM_DENOMINATOR

OPERATOR_WALLET_CHURN_SNAPSHOT_VERSION = "wallet-churn-v2"
HIGH_RISK_CHURN_ROLES = (
    AddressRole.CREATOR,
    AddressRole.CREATION_SUBMITTER,
    AddressRole.FEE_PAYER,
    AddressRole.FUNDER,
    AddressRole.FIRST_BUYER,
    AddressRole.FAKE_PUMP_BUYER,
    AddressRole.INVENTORY_HOLDER,
    AddressRole.DUMPER,
    AddressRole.PROFIT_COLLECTOR,
    AddressRole.RELAY_ADDRESS,
)


class WalletChurnStatus(Enum):
    """Address status between two profile snapshots."""

    NEW = "new"
    RETAINED = "retained"
    RETIRED = "retired"


@dataclass(frozen=True, slots=True)
class OperatorWalletChurnConfig:
    """Configuration for a point-in-time wallet churn snapshot."""

    as_of_slot: Slot
    churn_snapshot_version: str
    accepted_profile_versions: tuple[str, ...]
    min_membership_probability_ppm: int
    min_role_probability_ppm: int


@dataclass(frozen=True, slots=True)
class WalletChurnAddress:
    """Address-level churn evidence."""

    as_of_slot: Slot
    entity_id: str
    address: str
    status: WalletChurnStatus
    membership_probability_ppm: int
    same_controller_probability_ppm: int
    cooperating_probability_ppm: int
    roles: tuple[AddressRole, ...]
    high_risk_role_count: int
    evidence_ids: tuple[str, ...]
    model_version: str


@dataclass(frozen=True, slots=True)
class OperatorWalletChurnSnapshot:
    """Immutable wallet churn view for one operator entity."""

    as_of_slot: Slot
    entity_id: str
    churn_snapshot_version: str
    current_profile_version: str
    previous_profile_version: str
    previous_as_of_slot: Slot
    current_active_address_count: int
    previous_active_address_count: int
    new_address_count: int
    retained_address_count: int
    retired_address_count: int
    new_high_risk_role_count: int
    retained_role_change_count: int
    address_turnover_ppm: int
    new_addresses: tuple[WalletChurnAddress, ...]
    retained_addresses: tuple[WalletChurnAddress, ...]
    retired_addresses: tuple[WalletChurnAddress, ...]
    evidence_ids: tuple[str, ...]
    reason_codes: tuple[str, ...]


def build_operator_wallet_churn_snapshot(
    *,
    previous_profile: OperatorProfileSnapshot | None,
    current_profile: OperatorProfileSnapshot,
    config: OperatorWalletChurnConfig,
) -> OperatorWalletChurnSnapshot | AbstainResult:
    """Build wallet churn evidence from two point-in-time profile snapshots."""

    validation_error = _validate_churn_inputs(
        previous_profile=previous_profile,
        current_profile=current_profile,
        config=config,
    )
    if validation_error is not None:
        return validation_error

    if previous_profile is None:
        return _missing("previous operator profile is required", config.as_of_slot)
    previous_by_address = _address_profiles_by_address(previous_profile.addresses)
    current_by_address = _address_profiles_by_address(current_profile.addresses)

    new_addresses = tuple(
        _churn_address(
            profile=current_by_address[address],
            status=WalletChurnStatus.NEW,
            config=config,
        )
        for address in sorted(set(current_by_address) - set(previous_by_address))
    )
    retained_addresses = tuple(
        _churn_address(
            profile=current_by_address[address],
            status=WalletChurnStatus.RETAINED,
            config=config,
        )
        for address in sorted(set(current_by_address) & set(previous_by_address))
    )
    retired_addresses = tuple(
        _churn_address(
            profile=previous_by_address[address],
            status=WalletChurnStatus.RETIRED,
            config=config,
        )
        for address in sorted(set(previous_by_address) - set(current_by_address))
    )

    retained_role_change_count = _retained_role_change_count(
        current_by_address=current_by_address,
        previous_by_address=previous_by_address,
        config=config,
    )
    new_high_risk_role_count = sum(
        address.high_risk_role_count for address in new_addresses
    )
    return OperatorWalletChurnSnapshot(
        as_of_slot=config.as_of_slot,
        entity_id=current_profile.entity_id,
        churn_snapshot_version=config.churn_snapshot_version,
        current_profile_version=current_profile.profile_version,
        previous_profile_version=previous_profile.profile_version,
        previous_as_of_slot=previous_profile.as_of_slot,
        current_active_address_count=current_profile.active_address_count,
        previous_active_address_count=previous_profile.active_address_count,
        new_address_count=len(new_addresses),
        retained_address_count=len(retained_addresses),
        retired_address_count=len(retired_addresses),
        new_high_risk_role_count=new_high_risk_role_count,
        retained_role_change_count=retained_role_change_count,
        address_turnover_ppm=_address_turnover_ppm(
            new_count=len(new_addresses),
            retired_count=len(retired_addresses),
            previous_count=previous_profile.active_address_count,
            current_count=current_profile.active_address_count,
        ),
        new_addresses=new_addresses,
        retained_addresses=retained_addresses,
        retired_addresses=retired_addresses,
        evidence_ids=_combined_evidence_ids(previous_profile, current_profile),
        reason_codes=_churn_reason_codes(
            new_count=len(new_addresses),
            retired_count=len(retired_addresses),
            new_high_risk_role_count=new_high_risk_role_count,
            retained_role_change_count=retained_role_change_count,
        ),
    )


def _validate_churn_inputs(
    *,
    previous_profile: OperatorProfileSnapshot | None,
    current_profile: OperatorProfileSnapshot,
    config: OperatorWalletChurnConfig,
) -> AbstainResult | None:
    config_error = _validate_churn_config(config)
    if config_error is not None:
        return config_error
    if previous_profile is None:
        return _missing("previous operator profile is required", config.as_of_slot)
    for profile, label in (
        (previous_profile, "previous"),
        (current_profile, "current"),
    ):
        profile_error = _validate_profile(profile, config, label=label)
        if profile_error is not None:
            return profile_error
    if previous_profile.entity_id != current_profile.entity_id:
        return _unsupported("profile entity_id mismatch", config.as_of_slot)
    if previous_profile.as_of_slot >= current_profile.as_of_slot:
        return _stale("previous profile must be older than current profile", config)
    return None


def _validate_churn_config(
    config: OperatorWalletChurnConfig,
) -> AbstainResult | None:
    if not isinstance(config, OperatorWalletChurnConfig):
        return _unsupported("wallet churn config is malformed", Slot(-1))
    if not _non_negative_int(config.as_of_slot):
        return _unsupported("wallet churn as_of_slot is invalid", Slot(-1))
    decoder_checks = (
        (
            not _non_empty_str(config.churn_snapshot_version),
            "wallet churn snapshot version is required",
        ),
        (
            not _valid_str_tuple(config.accepted_profile_versions),
            "accepted profile versions are required",
        ),
    )
    for failed, message in decoder_checks:
        if failed:
            return _decoder_mismatch(message, config)
    if config.churn_snapshot_version != OPERATOR_WALLET_CHURN_SNAPSHOT_VERSION:
        return _decoder_mismatch(
            "wallet churn snapshot version is not current",
            config,
        )
    probability_checks = (
        (
            not _valid_probability_ppm(config.min_membership_probability_ppm),
            "min_membership_probability_ppm must be in probability ppm range",
        ),
        (
            not _valid_probability_ppm(config.min_role_probability_ppm),
            "min_role_probability_ppm must be in probability ppm range",
        ),
    )
    for failed, message in probability_checks:
        if failed:
            return _unsupported(message, config)
    return None


def _validate_profile(
    profile: OperatorProfileSnapshot,
    config: OperatorWalletChurnConfig,
    *,
    label: str,
) -> AbstainResult | None:
    for validation in (
        _validate_profile_shape,
        _validate_profile_temporal,
        _validate_profile_identity_and_version,
        _validate_profile_counts,
        _validate_profile_addresses,
    ):
        validation_error = validation(profile, config, label=label)
        if validation_error is not None:
            return validation_error
    return None


def _validate_profile_shape(
    profile: OperatorProfileSnapshot,
    config: OperatorWalletChurnConfig,
    *,
    label: str,
) -> AbstainResult | None:
    if not isinstance(profile, OperatorProfileSnapshot):
        return _unsupported(f"{label} operator profile is malformed", config)
    if not _non_negative_int(profile.as_of_slot):
        return _unsupported(f"{label} profile as_of_slot is invalid", config)
    return None


def _validate_profile_temporal(
    profile: OperatorProfileSnapshot,
    config: OperatorWalletChurnConfig,
    *,
    label: str,
) -> AbstainResult | None:
    if profile.as_of_slot > config.as_of_slot:
        return _stale(f"{label} profile is newer than churn snapshot", config)
    if label == "current" and profile.as_of_slot != config.as_of_slot:
        return _stale("current profile must match churn as_of_slot", config)
    return None


def _validate_profile_identity_and_version(
    profile: OperatorProfileSnapshot,
    config: OperatorWalletChurnConfig,
    *,
    label: str,
) -> AbstainResult | None:
    if not _non_empty_str(profile.entity_id):
        return _missing(f"{label} profile entity_id is required", config)
    if profile.profile_version not in config.accepted_profile_versions:
        return _decoder_mismatch(f"{label} profile version is not accepted", config)
    return None


def _validate_profile_addresses(
    profile: OperatorProfileSnapshot,
    config: OperatorWalletChurnConfig,
    *,
    label: str,
) -> AbstainResult | None:
    for address in profile.addresses:
        address_error = _validate_address_profile(address, profile, config)
        if address_error is not None:
            return address_error
    if len({address.address for address in profile.addresses}) != len(
        profile.addresses
    ):
        return _unsupported(f"{label} profile has duplicate addresses", config)
    return None


def _validate_profile_counts(
    profile: OperatorProfileSnapshot,
    config: OperatorWalletChurnConfig,
    *,
    label: str,
) -> AbstainResult | None:
    if type(profile.addresses) is not tuple:
        return _unsupported(f"{label} profile addresses must be a tuple", config)
    count_fields = (
        profile.source_membership_count,
        profile.active_address_count,
        profile.source_campaign_count,
        profile.active_campaign_count,
        profile.source_regime_count,
        profile.active_regime_count,
    )
    if any(not _non_negative_int(count) for count in count_fields):
        return _unsupported(f"{label} profile counts must be non-negative", config)
    if profile.active_address_count != len(profile.addresses):
        return _unsupported(
            f"{label} profile active_address_count must match addresses",
            config,
        )
    if profile.active_address_count > profile.source_membership_count:
        return _unsupported(
            f"{label} profile active addresses exceed source memberships",
            config,
        )
    return None


def _validate_address_profile(
    address: OperatorAddressProfile,
    profile: OperatorProfileSnapshot,
    config: OperatorWalletChurnConfig,
) -> AbstainResult | None:
    for validation in (
        _validate_address_shape,
        _validate_address_identity,
        _validate_address_probabilities,
        _validate_address_membership,
        _validate_address_provenance,
        _validate_address_roles,
    ):
        validation_error = validation(address, profile, config)
        if validation_error is not None:
            return validation_error
    return None


def _validate_address_shape(
    address: OperatorAddressProfile,
    profile: OperatorProfileSnapshot,
    config: OperatorWalletChurnConfig,
) -> AbstainResult | None:
    if not isinstance(address, OperatorAddressProfile):
        return _unsupported("operator address profile is malformed", config)
    if not isinstance(profile, OperatorProfileSnapshot):
        return _unsupported("operator profile is malformed", config)
    return None


def _validate_address_identity(
    address: OperatorAddressProfile,
    profile: OperatorProfileSnapshot,
    config: OperatorWalletChurnConfig,
) -> AbstainResult | None:
    if address.as_of_slot != profile.as_of_slot:
        return _stale("address profile uses a different as_of_slot", config)
    if address.entity_id != profile.entity_id:
        return _unsupported("address profile entity_id mismatch", config)
    if not _non_empty_str(address.address):
        return _missing("address profile address is required", config)
    return None


def _validate_address_membership(
    address: OperatorAddressProfile,
    profile: OperatorProfileSnapshot,
    config: OperatorWalletChurnConfig,
) -> AbstainResult | None:
    _ = profile
    if not _valid_probability_ppm(_membership_probability_ppm(address)):
        return _unsupported("address membership probability is invalid", config)
    if _membership_probability_ppm(address) < config.min_membership_probability_ppm:
        return _missing("address membership is below churn threshold", config)
    return None


def _validate_address_provenance(
    address: OperatorAddressProfile,
    profile: OperatorProfileSnapshot,
    config: OperatorWalletChurnConfig,
) -> AbstainResult | None:
    _ = profile
    if not _valid_evidence_ids(address.evidence_ids):
        return _missing("address profile evidence_ids are required", config)
    if not _non_empty_str(address.model_version):
        return _decoder_mismatch("address profile model_version is required", config)
    return None


def _validate_address_roles(
    address: OperatorAddressProfile,
    profile: OperatorProfileSnapshot,
    config: OperatorWalletChurnConfig,
) -> AbstainResult | None:
    _ = profile
    if type(address.probable_roles) is not tuple:
        return _unsupported("address probable_roles must be a tuple", config)
    for role in address.probable_roles:
        role_error = _validate_role_assignment(role, address, config)
        if role_error is not None:
            return role_error
    return None


def _validate_address_probabilities(
    address: OperatorAddressProfile,
    profile: OperatorProfileSnapshot,
    config: OperatorWalletChurnConfig,
) -> AbstainResult | None:
    _ = profile
    probability_fields = {
        "same_controller_probability_ppm": address.same_controller_probability_ppm,
        "cooperating_probability_ppm": address.cooperating_probability_ppm,
        "shared_service_probability_ppm": address.shared_service_probability_ppm,
        "incidental_interaction_probability_ppm": (
            address.incidental_interaction_probability_ppm
        ),
    }
    for field_name, value in probability_fields.items():
        if not _valid_probability_ppm(value):
            return _unsupported(f"address {field_name} is invalid", config)
    return None


def _validate_role_assignment(
    role: AddressRoleAssignment,
    address: OperatorAddressProfile,
    config: OperatorWalletChurnConfig,
) -> AbstainResult | None:
    if not isinstance(role, AddressRoleAssignment):
        return _unsupported("address role assignment is malformed", config)
    stale_checks = ((role.as_of_slot != address.as_of_slot, "different as_of_slot"),)
    for failed, label in stale_checks:
        if failed:
            return _stale(f"address role assignment uses a {label}", config)
    unsupported_checks = (
        (role.address != address.address, "address mismatch"),
        (not isinstance(role.role, AddressRole), "role is invalid"),
        (
            not _valid_probability_ppm(role.role_probability_ppm),
            "probability is invalid",
        ),
    )
    for failed, label in unsupported_checks:
        if failed:
            return _unsupported(f"address role assignment {label}", config)
    missing_checks = (
        (
            not _valid_evidence_ids(role.evidence_ids),
            "address role evidence_ids are required",
        ),
    )
    for failed, message in missing_checks:
        if failed:
            return _missing(message, config)
    if not _non_empty_str(role.model_version):
        return _decoder_mismatch("address role model_version is required", config)
    return None


def _address_profiles_by_address(
    addresses: tuple[OperatorAddressProfile, ...],
) -> dict[str, OperatorAddressProfile]:
    return {address.address: address for address in addresses}


def _churn_address(
    *,
    profile: OperatorAddressProfile,
    status: WalletChurnStatus,
    config: OperatorWalletChurnConfig,
) -> WalletChurnAddress:
    roles = _roles_above_threshold(profile, config)
    return WalletChurnAddress(
        as_of_slot=config.as_of_slot,
        entity_id=profile.entity_id,
        address=profile.address,
        status=status,
        membership_probability_ppm=_membership_probability_ppm(profile),
        same_controller_probability_ppm=profile.same_controller_probability_ppm,
        cooperating_probability_ppm=profile.cooperating_probability_ppm,
        roles=roles,
        high_risk_role_count=sum(1 for role in roles if role in HIGH_RISK_CHURN_ROLES),
        evidence_ids=_address_evidence_ids(profile),
        model_version=profile.model_version,
    )


def _roles_above_threshold(
    profile: OperatorAddressProfile,
    config: OperatorWalletChurnConfig,
) -> tuple[AddressRole, ...]:
    return tuple(
        sorted(
            (
                role.role
                for role in profile.probable_roles
                if role.role_probability_ppm >= config.min_role_probability_ppm
            ),
            key=lambda role: role.value,
        )
    )


def _retained_role_change_count(
    *,
    current_by_address: dict[str, OperatorAddressProfile],
    previous_by_address: dict[str, OperatorAddressProfile],
    config: OperatorWalletChurnConfig,
) -> int:
    return sum(
        1
        for address in set(current_by_address) & set(previous_by_address)
        if _roles_above_threshold(current_by_address[address], config)
        != _roles_above_threshold(previous_by_address[address], config)
    )


def _membership_probability_ppm(profile: OperatorAddressProfile) -> int:
    return max(
        profile.same_controller_probability_ppm,
        profile.cooperating_probability_ppm,
    )


def _address_turnover_ppm(
    *,
    new_count: int,
    retired_count: int,
    previous_count: int,
    current_count: int,
) -> int:
    denominator = previous_count + current_count
    if denominator == 0:
        return 0
    return (new_count + retired_count) * PROBABILITY_PPM_DENOMINATOR // denominator


def _churn_reason_codes(
    *,
    new_count: int,
    retired_count: int,
    new_high_risk_role_count: int,
    retained_role_change_count: int,
) -> tuple[str, ...]:
    reasons = ["operator_wallet_churn_snapshot_built"]
    if new_count:
        reasons.append("new_operator_addresses_detected")
    if retired_count:
        reasons.append("retired_operator_addresses_detected")
    if new_high_risk_role_count:
        reasons.append("new_high_risk_operator_roles_detected")
    if retained_role_change_count:
        reasons.append("retained_operator_role_changes_detected")
    if len(reasons) == 1:
        reasons.append("no_operator_wallet_churn_detected")
    return tuple(reasons)


def _combined_evidence_ids(
    previous_profile: OperatorProfileSnapshot,
    current_profile: OperatorProfileSnapshot,
) -> tuple[str, ...]:
    evidence_ids: list[str] = []
    for profile in (previous_profile, current_profile):
        for address in profile.addresses:
            evidence_ids.extend(_address_evidence_ids(address))
    return tuple(dict.fromkeys(evidence_ids))


def _address_evidence_ids(profile: OperatorAddressProfile) -> tuple[str, ...]:
    evidence_ids: list[str] = []
    evidence_ids.extend(profile.evidence_ids)
    for role in profile.probable_roles:
        evidence_ids.extend(role.evidence_ids)
    return tuple(dict.fromkeys(evidence_ids))


def _valid_probability_ppm(value: object) -> bool:
    return _non_negative_int(value) and value <= PROBABILITY_PPM_DENOMINATOR


def _valid_str_tuple(value: object) -> bool:
    return (
        type(value) is tuple
        and bool(value)
        and all(_non_empty_str(item) for item in value)
    )


def _valid_evidence_ids(value: object) -> bool:
    return _valid_str_tuple(value)


def _non_empty_str(value: object) -> bool:
    return type(value) is str and bool(value.strip())


def _non_negative_int(value: object) -> bool:
    return type(value) is int and value >= 0


def _missing(
    message: str,
    as_of_slot: Slot | OperatorWalletChurnConfig,
) -> AbstainResult:
    return AbstainResult(
        reason=AbstainReason.MISSING_FEATURE,
        message=message,
        as_of_slot=_abstain_slot(as_of_slot),
    )


def _decoder_mismatch(
    message: str,
    config: OperatorWalletChurnConfig,
) -> AbstainResult:
    return AbstainResult(
        reason=AbstainReason.DECODER_MISMATCH,
        message=message,
        as_of_slot=_abstain_slot(config),
    )


def _stale(
    message: str,
    config: OperatorWalletChurnConfig,
) -> AbstainResult:
    return AbstainResult(
        reason=AbstainReason.STALE_STATE,
        message=message,
        as_of_slot=_abstain_slot(config),
    )


def _unsupported(
    message: str,
    as_of_slot: Slot | OperatorWalletChurnConfig,
) -> AbstainResult:
    return AbstainResult(
        reason=AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
        message=message,
        as_of_slot=_abstain_slot(as_of_slot),
    )


def _abstain_slot(value: object) -> int:
    if isinstance(value, OperatorWalletChurnConfig):
        return _abstain_slot(value.as_of_slot)
    if type(value) is int:
        return value
    return -1
