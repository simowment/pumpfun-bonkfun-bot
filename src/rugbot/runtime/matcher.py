"""Pure, fail-closed target matching for proven launch artifacts."""

# These validators keep every protocol assumption explicit and fail closed.
# ruff: noqa: C901, PLR0911, PLR0913, PLR2004

import base58

from rugbot.decision.launch_signals import (
    ACCEPTED_PUMP_CREATE_V2_DECODER_VERSION,
    ACCEPTED_PUMP_CREATE_V2_IDL_SHA256,
    ACCEPTED_PUMP_PROGRAM_ID,
)
from rugbot.decision.operator_qualification import (
    OperatorQualification,
    QualificationStatus,
    WalletEntityEvidence,
)
from rugbot.domain.decisions import AbstainReason, AbstainResult
from rugbot.domain.launches import LaunchCreatedV2
from rugbot.graph.entity_resolution import AddressRole
from rugbot.graph.rugger_protection import (
    FreshWalletStatus,
    RuggerProtectionSnapshot,
    WalletFreshnessEvidence,
    WalletTransferRange,
)
from rugbot.graph.wallet_churn import (
    OPERATOR_WALLET_CHURN_SNAPSHOT_VERSION,
    OperatorWalletChurnSnapshot,
    WalletChurnAddress,
    WalletChurnStatus,
)
from rugbot.runtime.config import (
    CoreSniperConfig,
    ExecutionMode,
    SniperExecution,
    SniperTarget,
    TargetKind,
)

WATCH_OPTIONAL_EVIDENCE = frozenset({"first_buyer", "transaction_slot_account_state"})


def match_launch_target(
    *,
    config: CoreSniperConfig,
    launch: object,
    qualification: OperatorQualification | None = None,
    entity_evidence: tuple[WalletEntityEvidence, ...] | None = None,
    operator_churn: OperatorWalletChurnSnapshot | AbstainResult | None = None,
    rugger_protection: RuggerProtectionSnapshot | AbstainResult | None = None,
) -> bool | AbstainResult:
    """Match a configured target against one proven Pump.fun launch.

    The function performs no I/O. ``False`` means the launch is valid but is
    not the configured target; an ``AbstainResult`` means the evidence cannot
    safely support a sniper decision.
    """

    config_error = _validate_config(config)
    if config_error is not None:
        return config_error
    return _match_validated_target(
        target=config.target,
        launch=launch,
        qualification=qualification,
        entity_evidence=entity_evidence,
        operator_churn=operator_churn,
        rugger_protection=rugger_protection,
    )


def _match_validated_target(
    *,
    target: SniperTarget,
    launch: object,
    qualification: OperatorQualification | None,
    entity_evidence: tuple[WalletEntityEvidence, ...] | None,
    operator_churn: OperatorWalletChurnSnapshot | AbstainResult | None,
    rugger_protection: RuggerProtectionSnapshot | AbstainResult | None,
) -> bool | AbstainResult:
    launch_error = _validate_proven_launch(launch)
    if launch_error is not None:
        return launch_error
    if target.kind is TargetKind.WALLET:
        if launch.creator_pubkey != target.id and operator_churn is None:
            return False
        qualified = _match_qualified_wallet(
            target=target,
            launch=launch,
            qualification=qualification,
            entity_evidence=entity_evidence,
        )
        if isinstance(qualified, AbstainResult) or qualified is False:
            return qualified
        churn_error = _validate_operator_churn(
            operator_churn=operator_churn,
            launch=launch,
            qualification=qualification,
        )
        if churn_error is not None:
            return churn_error
        protection_error = _validate_rugger_protection(
            rugger_protection=rugger_protection,
            launch=launch,
            target=target,
        )
        if protection_error is not None:
            return protection_error
        if launch.creator_pubkey == target.id:
            return True
        if operator_churn is None:
            return False
        if not _churn_contains_creator(operator_churn, launch.creator_pubkey):
            return False
        return _match_rotated_creator_protection(
            rugger_protection=rugger_protection,
            launch=launch,
            target=target,
        )
    return launch.mint_pubkey == target.id


def _validate_operator_churn(  # noqa: PLR0912
    *,
    operator_churn: OperatorWalletChurnSnapshot | AbstainResult | None,
    launch: LaunchCreatedV2,
    qualification: OperatorQualification | None,
) -> AbstainResult | None:
    if operator_churn is None:
        return None
    if isinstance(operator_churn, AbstainResult):
        return operator_churn
    if not isinstance(operator_churn, OperatorWalletChurnSnapshot):
        return _abstain(
            launch,
            AbstainReason.UNKNOWN_PROTOCOL_STATE,
            "operator churn snapshot is malformed",
        )
    if (
        type(operator_churn.as_of_slot) is not int
        or operator_churn.as_of_slot > launch.as_of_slot
    ):
        return _abstain(
            launch,
            AbstainReason.STALE_STATE,
            "operator churn snapshot is after the launch",
        )
    if operator_churn.churn_snapshot_version != OPERATOR_WALLET_CHURN_SNAPSHOT_VERSION:
        return _abstain(
            launch,
            AbstainReason.DECODER_MISMATCH,
            "operator churn snapshot version is not pinned",
        )
    if qualification is None or operator_churn.entity_id != qualification.entity_id:
        return _abstain(
            launch,
            AbstainReason.UNKNOWN_PROTOCOL_STATE,
            "operator churn entity does not match qualification",
        )
    if (
        type(operator_churn.previous_as_of_slot) is not int
        or operator_churn.previous_as_of_slot >= operator_churn.as_of_slot
    ):
        return _abstain(
            launch,
            AbstainReason.STALE_STATE,
            "operator churn profile order is invalid",
        )
    collections = (
        (operator_churn.new_addresses, WalletChurnStatus.NEW),
        (operator_churn.retained_addresses, WalletChurnStatus.RETAINED),
        (operator_churn.retired_addresses, WalletChurnStatus.RETIRED),
    )
    seen_addresses: set[str] = set()
    for addresses, expected_status in collections:
        if type(addresses) is not tuple:
            return _abstain(
                launch,
                AbstainReason.UNKNOWN_PROTOCOL_STATE,
                "operator churn address records are malformed",
            )
        for address in addresses:
            if not _valid_churn_address(
                address=address,
                expected_status=expected_status,
                snapshot=operator_churn,
            ):
                return _abstain(
                    launch,
                    AbstainReason.MISSING_FEATURE,
                    "operator churn address provenance is incomplete",
                )
            if address.address in seen_addresses:
                return _abstain(
                    launch,
                    AbstainReason.UNKNOWN_PROTOCOL_STATE,
                    "operator churn address appears more than once",
                )
            seen_addresses.add(address.address)
    if (
        any(
            type(value) is not int or value < 0
            for value in (
                operator_churn.current_active_address_count,
                operator_churn.previous_active_address_count,
                operator_churn.new_address_count,
                operator_churn.retained_address_count,
                operator_churn.retired_address_count,
                operator_churn.new_high_risk_role_count,
                operator_churn.retained_role_change_count,
            )
        )
        or type(operator_churn.address_turnover_ppm) is not int
        or not 0 <= operator_churn.address_turnover_ppm <= 1_000_000
        or not _valid_evidence_ids(operator_churn.evidence_ids)
        or not isinstance(operator_churn.reason_codes, tuple)
        or not operator_churn.reason_codes
        or any(
            not isinstance(reason_code, str) or not reason_code
            for reason_code in operator_churn.reason_codes
        )
    ):
        return _abstain(
            launch,
            AbstainReason.MISSING_FEATURE,
            "operator churn provenance is incomplete",
        )
    if (
        operator_churn.new_address_count != len(operator_churn.new_addresses)
        or operator_churn.retained_address_count
        != len(operator_churn.retained_addresses)
        or operator_churn.retired_address_count != len(operator_churn.retired_addresses)
        or operator_churn.current_active_address_count
        != operator_churn.new_address_count + operator_churn.retained_address_count
        or operator_churn.previous_active_address_count
        != operator_churn.retired_address_count + operator_churn.retained_address_count
        or operator_churn.new_high_risk_role_count
        != sum(address.high_risk_role_count for address in operator_churn.new_addresses)
        or operator_churn.retained_role_change_count
        > operator_churn.retained_address_count
    ):
        return _abstain(
            launch,
            AbstainReason.UNKNOWN_PROTOCOL_STATE,
            "operator churn counts do not match address records",
        )
    return None


def _valid_churn_address(
    *,
    address: object,
    expected_status: WalletChurnStatus,
    snapshot: OperatorWalletChurnSnapshot,
) -> bool:
    return (
        isinstance(address, WalletChurnAddress)
        and address.as_of_slot == snapshot.as_of_slot
        and address.entity_id == snapshot.entity_id
        and isinstance(address.address, str)
        and bool(address.address)
        and address.status is expected_status
        and type(address.membership_probability_ppm) is int
        and 0 <= address.membership_probability_ppm <= 1_000_000
        and type(address.same_controller_probability_ppm) is int
        and 0 <= address.same_controller_probability_ppm <= 1_000_000
        and type(address.cooperating_probability_ppm) is int
        and 0 <= address.cooperating_probability_ppm <= 1_000_000
        and type(address.roles) is tuple
        and all(isinstance(role, AddressRole) for role in address.roles)
        and type(address.high_risk_role_count) is int
        and address.high_risk_role_count >= 0
        and _valid_evidence_ids(address.evidence_ids)
        and isinstance(address.model_version, str)
        and bool(address.model_version)
    )


def _churn_contains_creator(
    operator_churn: OperatorWalletChurnSnapshot,
    creator_pubkey: str,
) -> bool:
    addresses = (*operator_churn.new_addresses, *operator_churn.retained_addresses)
    return any(
        address.address == creator_pubkey and AddressRole.CREATOR in address.roles
        for address in addresses
    )


def _validate_rugger_protection(
    *,
    rugger_protection: RuggerProtectionSnapshot | AbstainResult | None,
    launch: LaunchCreatedV2,
    target: SniperTarget,
) -> AbstainResult | None:
    if rugger_protection is None:
        return None
    if isinstance(rugger_protection, AbstainResult):
        return rugger_protection
    if not isinstance(rugger_protection, RuggerProtectionSnapshot):
        return _abstain(
            launch,
            AbstainReason.UNKNOWN_PROTOCOL_STATE,
            "rugger protection snapshot is malformed",
        )
    if (
        type(rugger_protection.as_of_slot) is not int
        or rugger_protection.as_of_slot > launch.as_of_slot
        or not isinstance(rugger_protection.target_wallet, str)
        or not rugger_protection.target_wallet
        or rugger_protection.target_wallet != target.id
        or type(rugger_protection.transfer_ranges) is not tuple
        or type(rugger_protection.freshness) is not tuple
    ):
        return _abstain(
            launch,
            AbstainReason.STALE_STATE,
            "rugger protection snapshot is not aligned to the launch",
        )
    for item in rugger_protection.transfer_ranges:
        if not _valid_transfer_range(item, rugger_protection):
            return _abstain(
                launch,
                AbstainReason.UNKNOWN_PROTOCOL_STATE,
                "rugger protection transfer range is malformed",
            )
    for item in rugger_protection.freshness:
        if not _valid_freshness(item, rugger_protection):
            return _abstain(
                launch,
                AbstainReason.UNKNOWN_PROTOCOL_STATE,
                "rugger protection freshness evidence is malformed",
            )
    return None


def _valid_transfer_range(
    item: object,
    snapshot: RuggerProtectionSnapshot,
) -> bool:
    return (
        isinstance(item, WalletTransferRange)
        and item.as_of_slot == snapshot.as_of_slot
        and isinstance(item.source_wallet, str)
        and bool(item.source_wallet)
        and isinstance(item.destination_wallet, str)
        and bool(item.destination_wallet)
        and item.source_wallet != item.destination_wallet
        and item.first_slot <= item.last_slot <= snapshot.as_of_slot
        and type(item.transfer_count) is int
        and item.transfer_count > 0
        and type(item.amount_base_units) is int
        and item.amount_base_units > 0
        and _valid_evidence_ids(item.evidence_ids)
    )


def _valid_freshness(
    item: object,
    snapshot: RuggerProtectionSnapshot,
) -> bool:
    return (
        isinstance(item, WalletFreshnessEvidence)
        and item.as_of_slot == snapshot.as_of_slot
        and isinstance(item.wallet, str)
        and bool(item.wallet)
        and isinstance(item.status, FreshWalletStatus)
        and _valid_evidence_ids(item.evidence_ids)
        and (
            (item.status is FreshWalletStatus.UNKNOWN and item.age_slots is None)
            or (
                item.status is not FreshWalletStatus.UNKNOWN
                and item.first_observed_slot is not None
                and type(item.first_observed_slot) is int
                and item.first_observed_slot <= snapshot.as_of_slot
                and type(item.age_slots) is int
                and item.age_slots == snapshot.as_of_slot - item.first_observed_slot
                and item.age_slots >= 0
            )
        )
    )


def _match_rotated_creator_protection(
    *,
    rugger_protection: RuggerProtectionSnapshot | AbstainResult | None,
    launch: LaunchCreatedV2,
    target: SniperTarget,
) -> bool | AbstainResult:
    if rugger_protection is None:
        return _abstain(
            launch,
            AbstainReason.MISSING_FEATURE,
            "fresh creator wallet protection evidence is required",
        )
    if isinstance(rugger_protection, AbstainResult):
        return rugger_protection
    freshness = tuple(
        item
        for item in rugger_protection.freshness
        if item.wallet == launch.creator_pubkey
    )
    if len(freshness) != 1:
        return _abstain(
            launch,
            AbstainReason.MISSING_FEATURE,
            "creator freshness evidence is required",
        )
    creator_freshness = freshness[0]
    if creator_freshness.status is FreshWalletStatus.UNKNOWN:
        return _abstain(
            launch,
            AbstainReason.UNKNOWN_PROTOCOL_STATE,
            "creator wallet freshness is unknown",
        )
    if creator_freshness.status is FreshWalletStatus.NOT_FRESH:
        return False
    direct_ranges = tuple(
        item
        for item in rugger_protection.transfer_ranges
        if item.source_wallet == target.id
        and item.destination_wallet == launch.creator_pubkey
        and item.last_slot <= launch.as_of_slot
    )
    if not direct_ranges:
        return _abstain(
            launch,
            AbstainReason.MISSING_FEATURE,
            "direct pre-launch creator funding range is required",
        )
    return True


def _match_qualified_wallet(
    *,
    target: SniperTarget,
    launch: LaunchCreatedV2,
    qualification: OperatorQualification | None,
    entity_evidence: tuple[WalletEntityEvidence, ...] | None,
) -> bool | AbstainResult:
    if qualification is None or entity_evidence is None:
        return _abstain(
            launch,
            AbstainReason.MISSING_FEATURE,
            "wallet operator qualification and entity evidence are required",
        )
    qualification_error = _validate_qualification(
        qualification=qualification,
        launch=launch,
    )
    if qualification_error is not None:
        return qualification_error
    evidence_error, matching = _matching_entity_evidence(
        target=target,
        launch=launch,
        qualification=qualification,
        entity_evidence=entity_evidence,
    )
    if evidence_error is not None:
        return evidence_error
    if not matching:
        return _abstain(
            launch,
            AbstainReason.MISSING_FEATURE,
            "configured wallet is not covered by entity evidence",
        )
    return True


def _validate_qualification(
    *,
    qualification: object,
    launch: LaunchCreatedV2,
) -> AbstainResult | None:
    if type(qualification) is not OperatorQualification:
        return _abstain(
            launch,
            AbstainReason.UNKNOWN_PROTOCOL_STATE,
            "wallet operator qualification is malformed",
        )
    if qualification.status is not QualificationStatus.QUALIFIED:
        return _abstain(
            launch,
            qualification.abstain_reason or AbstainReason.MISSING_FEATURE,
            "wallet operator qualification is not qualified",
        )
    if type(qualification.as_of_slot) is not int or qualification.as_of_slot < 0:
        return _abstain(
            launch,
            AbstainReason.UNKNOWN_PROTOCOL_STATE,
            "wallet operator qualification slot is malformed",
        )
    if qualification.as_of_slot > launch.as_of_slot:
        return _abstain(
            launch,
            AbstainReason.STALE_STATE,
            "wallet operator qualification is after the launch",
        )
    if (
        not isinstance(qualification.entity_id, str)
        or not qualification.entity_id
        or type(qualification.sample_count) is not int
        or qualification.sample_count <= 0
        or type(qualification.matched_wallet_count) is not int
        or qualification.matched_wallet_count <= 0
        or type(qualification.repeated_adverse_behavior) is not bool
        or not _valid_evidence_ids(qualification.evidence_ids)
        or "operator_qualified" not in qualification.reason_codes
    ):
        return _abstain(
            launch,
            AbstainReason.MISSING_FEATURE,
            "wallet operator qualification provenance is incomplete",
        )
    return None


def _matching_entity_evidence(
    *,
    target: SniperTarget,
    launch: LaunchCreatedV2,
    qualification: OperatorQualification,
    entity_evidence: tuple[WalletEntityEvidence, ...],
) -> tuple[AbstainResult | None, tuple[WalletEntityEvidence, ...]]:
    if type(entity_evidence) is not tuple or not entity_evidence:
        return (
            _abstain(
                launch,
                AbstainReason.MISSING_FEATURE,
                "wallet entity evidence is required",
            ),
            (),
        )
    qualification_evidence_ids = set(qualification.evidence_ids)
    matching: list[WalletEntityEvidence] = []
    for item in entity_evidence:
        if type(item) is not WalletEntityEvidence:
            return (
                _abstain(
                    launch,
                    AbstainReason.UNKNOWN_PROTOCOL_STATE,
                    "wallet entity evidence is malformed",
                ),
                (),
            )
        if item.entity_id != qualification.entity_id:
            return (
                _abstain(
                    launch,
                    AbstainReason.UNKNOWN_PROTOCOL_STATE,
                    "wallet entity evidence belongs to another entity",
                ),
                (),
            )
        if not _valid_wallet_entity_evidence(item):
            return (
                _abstain(
                    launch,
                    AbstainReason.MISSING_FEATURE,
                    "wallet entity evidence provenance is incomplete",
                ),
                (),
            )
        if (
            item.as_of_slot > launch.as_of_slot
            or item.observed_slot > launch.as_of_slot
        ):
            return (
                _abstain(
                    launch,
                    AbstainReason.STALE_STATE,
                    "wallet entity evidence is after the launch",
                ),
                (),
            )
        if item.as_of_slot > qualification.as_of_slot:
            return (
                _abstain(
                    launch,
                    AbstainReason.STALE_STATE,
                    "wallet entity evidence is after the qualification",
                ),
                (),
            )
        if not set(item.evidence_ids) <= qualification_evidence_ids:
            return (
                _abstain(
                    launch,
                    AbstainReason.MISSING_FEATURE,
                    "wallet entity evidence is not covered by qualification",
                ),
                (),
            )
        if item.wallet == target.id and item.launch_id != launch.launch_id:
            matching.append(item)
    return None, tuple(matching)


def _valid_wallet_entity_evidence(item: WalletEntityEvidence) -> bool:
    return (
        type(item.as_of_slot) is int
        and type(item.observed_slot) is int
        and item.as_of_slot >= 0
        and item.observed_slot >= 0
        and item.observed_slot <= item.as_of_slot
        and isinstance(item.launch_id, str)
        and bool(item.launch_id)
        and isinstance(item.wallet, str)
        and bool(item.wallet)
        and type(item.entity_probability_ppm) is int
        and 0 < item.entity_probability_ppm <= 1_000_000
        and _valid_evidence_ids(item.evidence_ids)
    )


def _valid_evidence_ids(evidence_ids: object) -> bool:
    return (
        type(evidence_ids) is tuple
        and bool(evidence_ids)
        and all(isinstance(value, str) and bool(value) for value in evidence_ids)
        and len(set(evidence_ids)) == len(evidence_ids)
    )


def _validate_config(config: object) -> AbstainResult | None:
    if type(config) is not CoreSniperConfig:
        return AbstainResult(
            reason=AbstainReason.UNKNOWN_PROTOCOL_STATE,
            message="sniper config is malformed",
            as_of_slot=_as_of_slot(None),
        )
    component_error = _validate_target(config.target)
    if component_error is not None:
        return component_error
    if type(config.execution) is not SniperExecution:
        return AbstainResult(
            reason=AbstainReason.UNKNOWN_PROTOCOL_STATE,
            message="sniper execution is malformed",
            as_of_slot=0,
        )
    if config.execution.mode not in (ExecutionMode.OBSERVE, ExecutionMode.PAPER):
        return AbstainResult(
            reason=AbstainReason.UNKNOWN_PROTOCOL_STATE,
            message="sniper execution mode is unsupported",
            as_of_slot=0,
        )
    if (
        type(config.execution.quote_size_lamports) is not int
        or config.execution.quote_size_lamports <= 0
    ):
        return AbstainResult(
            reason=AbstainReason.UNKNOWN_PROTOCOL_STATE,
            message="sniper quote size is malformed",
            as_of_slot=0,
        )
    return None


def _validate_target(target: object) -> AbstainResult | None:
    if type(target) is not SniperTarget:
        return AbstainResult(
            reason=AbstainReason.UNKNOWN_PROTOCOL_STATE,
            message="sniper target is malformed",
            as_of_slot=0,
        )
    if target.kind not in {TargetKind.WALLET, TargetKind.TOKEN} or not _valid_pubkey(
        target.id
    ):
        return AbstainResult(
            reason=AbstainReason.UNKNOWN_PROTOCOL_STATE,
            message="sniper target is malformed",
            as_of_slot=0,
        )
    return None


def _validate_proven_launch(launch: object) -> AbstainResult | None:
    if type(launch) is not LaunchCreatedV2:
        return _abstain(
            launch,
            AbstainReason.UNKNOWN_PROTOCOL_STATE,
            "launch evidence is malformed",
        )
    as_of_slot = _as_of_slot(launch)
    if not isinstance(as_of_slot, int) or as_of_slot < 0:
        return _abstain(
            launch, AbstainReason.UNKNOWN_PROTOCOL_STATE, "launch slot is malformed"
        )
    if launch.program_id != ACCEPTED_PUMP_PROGRAM_ID:
        return _abstain(
            launch,
            AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
            "launch program is unsupported",
        )
    if (
        launch.instruction_name != "create_v2"
        or launch.creation_instruction_type != "create_v2"
    ):
        return _abstain(
            launch,
            AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
            "launch instruction is unsupported",
        )
    if (
        launch.decoder_version != ACCEPTED_PUMP_CREATE_V2_DECODER_VERSION
        or launch.idl_hash != ACCEPTED_PUMP_CREATE_V2_IDL_SHA256
    ):
        return _abstain(
            launch,
            AbstainReason.DECODER_MISMATCH,
            "launch decoder provenance is not pinned",
        )
    if (
        type(launch.missing_evidence) is not tuple
        or not set(launch.missing_evidence) <= WATCH_OPTIONAL_EVIDENCE
    ):
        return _abstain(
            launch,
            AbstainReason.UNKNOWN_PROTOCOL_STATE,
            "launch evidence is incomplete",
        )
    if not _valid_pubkey(launch.creator_pubkey) or not _valid_pubkey(
        launch.mint_pubkey
    ):
        return _abstain(
            launch,
            AbstainReason.UNKNOWN_PROTOCOL_STATE,
            "launch target fields are malformed",
        )
    if launch.launch_id != launch.mint_pubkey:
        return _abstain(
            launch,
            AbstainReason.UNKNOWN_PROTOCOL_STATE,
            "launch identity does not match mint",
        )
    if type(launch.account_role_proofs) is not tuple:
        return _abstain(
            launch,
            AbstainReason.UNKNOWN_PROTOCOL_STATE,
            "launch account proofs are malformed",
        )
    try:
        proofs = dict(launch.account_role_proofs)
    except (TypeError, ValueError):
        return _abstain(
            launch,
            AbstainReason.UNKNOWN_PROTOCOL_STATE,
            "launch account proofs are malformed",
        )
    if (
        proofs.get("mint") != launch.mint_pubkey
        or proofs.get("user") != launch.user_pubkey
    ):
        return _abstain(
            launch,
            AbstainReason.UNKNOWN_PROTOCOL_STATE,
            "launch account proofs are incomplete",
        )
    return None


def _valid_pubkey(value: object) -> bool:
    if not isinstance(value, str) or not value:
        return False
    try:
        decoded = base58.b58decode(value)
    except ValueError:
        return False
    return len(decoded) == 32 and base58.b58encode(decoded).decode("ascii") == value


def _as_of_slot(value: object) -> int:
    slot = getattr(value, "as_of_slot", 0)
    return slot if type(slot) is int and slot >= 0 else 0


def _abstain(launch: object, reason: AbstainReason, message: str) -> AbstainResult:
    return AbstainResult(reason=reason, message=message, as_of_slot=_as_of_slot(launch))
