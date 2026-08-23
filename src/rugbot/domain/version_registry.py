"""Point-in-time Pump protocol/config/fee version resolution."""

from dataclasses import dataclass

from rugbot.domain.amounts import Slot
from rugbot.domain.decisions import AbstainReason, AbstainResult
from rugbot.domain.fees import BASIS_POINTS_DENOMINATOR, FeeConfig

PUMP_VERSION_REGISTRY_VERSION = "pump-version-registry-v1"


@dataclass(frozen=True, slots=True)
class PumpProgramConfigVersion:
    """Versioned Pump program/config state proven by historical artifacts."""

    version: str
    program_id: str
    idl_hash: str
    global_config_hash: str
    valid_from_slot: Slot
    valid_to_slot: Slot | None
    source_artifact_version: str


@dataclass(frozen=True, slots=True)
class PumpFeeScheduleVersion:
    """Versioned Pump fee schedule for one program/config version."""

    version: str
    program_config_version: str
    protocol_fee_bps: int
    creator_fee_bps: int
    valid_from_slot: Slot
    valid_to_slot: Slot | None
    source_artifact_version: str


@dataclass(frozen=True, slots=True)
class PumpVersionResolveRequest:
    """Point-in-time protocol material to resolve against the registry."""

    as_of_slot: Slot
    program_id: str
    idl_hash: str
    global_config_hash: str


@dataclass(frozen=True, slots=True)
class PumpProtocolVersionSnapshot:
    """Resolved point-in-time protocol and fee state for quoting/replay."""

    as_of_slot: Slot
    program_id: str
    idl_hash: str
    global_config_hash: str
    program_config_version: str
    fee_config: FeeConfig
    program_config_source_artifact_version: str
    fee_source_artifact_version: str
    registry_version: str


PumpVersionResolveResult = PumpProtocolVersionSnapshot | AbstainResult


def resolve_pump_protocol_versions(
    *,
    request: PumpVersionResolveRequest,
    program_configs: tuple[PumpProgramConfigVersion, ...],
    fee_schedules: tuple[PumpFeeScheduleVersion, ...],
    registry_version: str = PUMP_VERSION_REGISTRY_VERSION,
) -> PumpVersionResolveResult:
    """Resolve Pump protocol/config/fee versions at one slot.

    Args:
        request: Slot-bounded program id, IDL hash, and global config hash.
        program_configs: Artifact-backed program/config version intervals.
        fee_schedules: Artifact-backed fee schedule intervals.
        registry_version: Version of the registry implementation/data contract.

    Returns:
        A point-in-time snapshot, or an abstention when the state cannot be
        proven unambiguously. This function is pure and performs no RPC or
        database access.
    """

    basic_error = _validate_resolve_inputs(request, registry_version)
    if basic_error is not None:
        return basic_error

    config_error = _validate_program_configs(program_configs, request.as_of_slot)
    if config_error is not None:
        return config_error

    fee_error = _validate_fee_schedules(fee_schedules, request.as_of_slot)
    if fee_error is not None:
        return fee_error

    program_config = _select_active_program_config(request, program_configs)
    if isinstance(program_config, AbstainResult):
        return program_config

    fee_schedule = _select_active_fee_schedule(
        as_of_slot=request.as_of_slot,
        program_config=program_config,
        fee_schedules=fee_schedules,
    )
    if isinstance(fee_schedule, AbstainResult):
        return fee_schedule

    return _build_protocol_version_snapshot(
        request=request,
        program_config=program_config,
        fee_schedule=fee_schedule,
        registry_version=registry_version,
    )


def _select_active_program_config(
    request: PumpVersionResolveRequest,
    program_configs: tuple[PumpProgramConfigVersion, ...],
) -> PumpProgramConfigVersion | AbstainResult:
    active_program_configs = tuple(
        config
        for config in program_configs
        if _is_active(
            valid_from_slot=config.valid_from_slot,
            valid_to_slot=config.valid_to_slot,
            as_of_slot=request.as_of_slot,
        )
        and config.program_id == request.program_id
        and config.global_config_hash == request.global_config_hash
    )
    if not active_program_configs:
        return _abstain(
            reason=AbstainReason.UNKNOWN_PROTOCOL_STATE,
            message="no artifact-backed Pump program config for slot",
            as_of_slot=request.as_of_slot,
        )
    if len(active_program_configs) > 1:
        return _abstain(
            reason=AbstainReason.UNKNOWN_PROTOCOL_STATE,
            message="ambiguous active Pump program config versions",
            as_of_slot=request.as_of_slot,
        )

    program_config = active_program_configs[0]
    if program_config.idl_hash != request.idl_hash:
        return _abstain(
            reason=AbstainReason.DECODER_MISMATCH,
            message="Pump IDL hash does not match active program config",
            as_of_slot=request.as_of_slot,
        )
    return program_config


def _select_active_fee_schedule(
    *,
    as_of_slot: Slot,
    program_config: PumpProgramConfigVersion,
    fee_schedules: tuple[PumpFeeScheduleVersion, ...],
) -> PumpFeeScheduleVersion | AbstainResult:
    active_fee_schedules = tuple(
        fee
        for fee in fee_schedules
        if _is_active(
            valid_from_slot=fee.valid_from_slot,
            valid_to_slot=fee.valid_to_slot,
            as_of_slot=as_of_slot,
        )
        and fee.program_config_version == program_config.version
    )
    if not active_fee_schedules:
        return _abstain(
            reason=AbstainReason.UNKNOWN_FEE_CONFIG,
            message="no artifact-backed Pump fee schedule for slot",
            as_of_slot=as_of_slot,
        )
    if len(active_fee_schedules) > 1:
        return _abstain(
            reason=AbstainReason.UNKNOWN_FEE_CONFIG,
            message="ambiguous active Pump fee schedules",
            as_of_slot=as_of_slot,
        )

    return active_fee_schedules[0]


def _build_protocol_version_snapshot(
    *,
    request: PumpVersionResolveRequest,
    program_config: PumpProgramConfigVersion,
    fee_schedule: PumpFeeScheduleVersion,
    registry_version: str,
) -> PumpProtocolVersionSnapshot:
    return PumpProtocolVersionSnapshot(
        as_of_slot=request.as_of_slot,
        program_id=program_config.program_id,
        idl_hash=program_config.idl_hash,
        global_config_hash=program_config.global_config_hash,
        program_config_version=program_config.version,
        fee_config=FeeConfig(
            version=fee_schedule.version,
            protocol_fee_bps=fee_schedule.protocol_fee_bps,
            creator_fee_bps=fee_schedule.creator_fee_bps,
            is_known=True,
            program_config_version=fee_schedule.program_config_version,
            valid_from_slot=fee_schedule.valid_from_slot,
            valid_to_slot=fee_schedule.valid_to_slot,
            source_artifact_version=fee_schedule.source_artifact_version,
        ),
        program_config_source_artifact_version=(program_config.source_artifact_version),
        fee_source_artifact_version=fee_schedule.source_artifact_version,
        registry_version=registry_version,
    )


def _validate_resolve_inputs(
    request: PumpVersionResolveRequest,
    registry_version: str,
) -> AbstainResult | None:
    as_of_slot = request.as_of_slot
    if int(as_of_slot) < 0:
        return _abstain(
            reason=AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
            message="as_of_slot must be non-negative",
            as_of_slot=as_of_slot,
        )
    if not request.program_id:
        return _abstain(
            reason=AbstainReason.UNKNOWN_PROTOCOL_STATE,
            message="program_id is required for protocol version resolution",
            as_of_slot=as_of_slot,
        )
    if not request.idl_hash:
        return _abstain(
            reason=AbstainReason.DECODER_MISMATCH,
            message="idl_hash is required for protocol version resolution",
            as_of_slot=as_of_slot,
        )
    if not request.global_config_hash:
        return _abstain(
            reason=AbstainReason.UNKNOWN_PROTOCOL_STATE,
            message="global_config_hash is required for config version resolution",
            as_of_slot=as_of_slot,
        )
    if not registry_version:
        return _abstain(
            reason=AbstainReason.DECODER_MISMATCH,
            message="registry_version is required",
            as_of_slot=as_of_slot,
        )
    return None


def _validate_program_configs(
    program_configs: tuple[PumpProgramConfigVersion, ...],
    as_of_slot: Slot,
) -> AbstainResult | None:
    if not program_configs:
        return _abstain(
            reason=AbstainReason.UNKNOWN_PROTOCOL_STATE,
            message="program config registry is empty",
            as_of_slot=as_of_slot,
        )
    for config in program_configs:
        missing_field = _missing_text_field(
            version=config.version,
            program_id=config.program_id,
            idl_hash=config.idl_hash,
            global_config_hash=config.global_config_hash,
            source_artifact_version=config.source_artifact_version,
        )
        if missing_field is not None:
            return _abstain(
                reason=AbstainReason.UNKNOWN_PROTOCOL_STATE,
                message=f"program config field is required: {missing_field}",
                as_of_slot=as_of_slot,
            )
        interval_error = _validate_slot_interval(
            valid_from_slot=config.valid_from_slot,
            valid_to_slot=config.valid_to_slot,
            as_of_slot=as_of_slot,
            interval_name="program config",
            reason=AbstainReason.UNKNOWN_PROTOCOL_STATE,
        )
        if interval_error is not None:
            return interval_error
    return None


def _validate_fee_schedules(
    fee_schedules: tuple[PumpFeeScheduleVersion, ...],
    as_of_slot: Slot,
) -> AbstainResult | None:
    if not fee_schedules:
        return _abstain(
            reason=AbstainReason.UNKNOWN_FEE_CONFIG,
            message="fee schedule registry is empty",
            as_of_slot=as_of_slot,
        )
    for fee in fee_schedules:
        fee_error = _validate_fee_schedule(fee, as_of_slot)
        if fee_error is not None:
            return fee_error
    return None


def _validate_fee_schedule(
    fee: PumpFeeScheduleVersion,
    as_of_slot: Slot,
) -> AbstainResult | None:
    missing_field = _missing_text_field(
        version=fee.version,
        program_config_version=fee.program_config_version,
        source_artifact_version=fee.source_artifact_version,
    )
    if missing_field is not None:
        return _abstain(
            reason=AbstainReason.UNKNOWN_FEE_CONFIG,
            message=f"fee schedule field is required: {missing_field}",
            as_of_slot=as_of_slot,
        )
    interval_error = _validate_slot_interval(
        valid_from_slot=fee.valid_from_slot,
        valid_to_slot=fee.valid_to_slot,
        as_of_slot=as_of_slot,
        interval_name="fee schedule",
        reason=AbstainReason.UNKNOWN_FEE_CONFIG,
    )
    if interval_error is not None:
        return interval_error
    return _validate_fee_schedule_bps(fee, as_of_slot)


def _validate_fee_schedule_bps(
    fee: PumpFeeScheduleVersion,
    as_of_slot: Slot,
) -> AbstainResult | None:
    if not _valid_fee_bps_type(fee.protocol_fee_bps) or not _valid_fee_bps_type(
        fee.creator_fee_bps
    ):
        return _abstain(
            reason=AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
            message="fee basis points must be integers",
            as_of_slot=as_of_slot,
        )
    if fee.protocol_fee_bps < 0 or fee.creator_fee_bps < 0:
        return _abstain(
            reason=AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
            message="fee basis points must be non-negative",
            as_of_slot=as_of_slot,
        )
    if fee.protocol_fee_bps + fee.creator_fee_bps > BASIS_POINTS_DENOMINATOR:
        return _abstain(
            reason=AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
            message="total fee basis points exceed denominator",
            as_of_slot=as_of_slot,
        )
    return None


def _valid_fee_bps_type(value: object) -> bool:
    return type(value) is int


def _validate_slot_interval(
    *,
    valid_from_slot: Slot,
    valid_to_slot: Slot | None,
    as_of_slot: Slot,
    interval_name: str,
    reason: AbstainReason,
) -> AbstainResult | None:
    if int(valid_from_slot) < 0:
        return _abstain(
            reason=reason,
            message=f"{interval_name} valid_from_slot must be non-negative",
            as_of_slot=as_of_slot,
        )
    if valid_to_slot is not None and int(valid_to_slot) <= int(valid_from_slot):
        return _abstain(
            reason=reason,
            message=f"{interval_name} valid_to_slot must be after valid_from_slot",
            as_of_slot=as_of_slot,
        )
    return None


def _missing_text_field(**fields: str) -> str | None:
    for name, value in fields.items():
        if not value:
            return name
    return None


def _is_active(
    *,
    valid_from_slot: Slot,
    valid_to_slot: Slot | None,
    as_of_slot: Slot,
) -> bool:
    if int(as_of_slot) < int(valid_from_slot):
        return False
    return valid_to_slot is None or int(as_of_slot) < int(valid_to_slot)


def _abstain(
    *,
    reason: AbstainReason,
    message: str,
    as_of_slot: Slot,
) -> AbstainResult:
    return AbstainResult(reason=reason, message=message, as_of_slot=int(as_of_slot))
