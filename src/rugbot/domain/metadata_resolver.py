"""Pure point-in-time Pump mint metadata resolution boundary."""

from dataclasses import dataclass
from typing import TypeAlias

from rugbot.domain.amounts import Slot
from rugbot.domain.create_state_adapter import PumpCreateMintMetadataProof
from rugbot.domain.decisions import AbstainReason, AbstainResult
from rugbot.domain.observations import Commitment
from rugbot.domain.quote_engine import MAX_SUPPORTED_DECIMALS
from rugbot.domain.version_registry import (
    PUMP_VERSION_REGISTRY_VERSION,
    PumpFeeScheduleVersion,
    PumpProgramConfigVersion,
    PumpProtocolVersionSnapshot,
    PumpVersionResolveRequest,
    resolve_pump_protocol_versions,
)
from rugbot.ingest.pump.bonding_curve_account import (
    PINNED_PUMP_IDL_SHA256,
    PUMP_PROGRAM_ID,
)
from rugbot.ingest.pump.create_decoder import SPL_2022_PROGRAM_ID
from rugbot.ingest.pump.create_event_decoder import SOL_PUBKEY

NATIVE_SOL_DECIMALS = 9


@dataclass(frozen=True, slots=True)
class PumpFinalizedAccountMetadataEvidence:
    """Finalized protocol/account metadata needed to select Pump registry state."""

    as_of_slot: Slot
    account_pubkey: str
    owner_program_id: str
    program_id: str
    idl_hash: str
    global_config_hash: str
    source_artifact: str
    commitment: Commitment


@dataclass(frozen=True, slots=True)
class PumpFinalizedMintMetadataEvidence:
    """Finalized mint-account metadata with no inferred decimals or ownership."""

    as_of_slot: Slot
    mint_pubkey: str
    owner_program_id: str
    decimals: int
    source_artifact: str
    commitment: Commitment


@dataclass(frozen=True, slots=True)
class PumpMetadataResolveRequest:
    """All typed evidence and registry artifacts for one create-point resolve."""

    as_of_slot: Slot
    account_evidence: PumpFinalizedAccountMetadataEvidence
    base_mint_evidence: PumpFinalizedMintMetadataEvidence
    quote_mint_evidence: PumpFinalizedMintMetadataEvidence
    program_configs: tuple[PumpProgramConfigVersion, ...]
    fee_schedules: tuple[PumpFeeScheduleVersion, ...]
    registry_version: str


PumpMetadataResolveResult: TypeAlias = (
    tuple[PumpCreateMintMetadataProof, PumpProtocolVersionSnapshot] | AbstainResult
)


def resolve_pump_create_metadata(
    request: PumpMetadataResolveRequest,
) -> PumpMetadataResolveResult:
    """Resolve finalized Pump mint metadata and protocol state at one slot.

    This boundary is deliberately pure. It accepts evidence collected by a
    caller and artifact-backed registry entries; it never fetches missing
    metadata, applies protocol defaults, or calls RPC/database services.
    """

    request_error = _validate_request_shape(request)
    if request_error is not None:
        return request_error

    as_of_slot = request.as_of_slot
    evidence_error = _validate_evidence(request)
    if evidence_error is not None:
        return evidence_error

    registry_error = _validate_registry_shape(request)
    if registry_error is not None:
        return registry_error

    protocol_result = resolve_pump_protocol_versions(
        request=PumpVersionResolveRequest(
            as_of_slot=as_of_slot,
            program_id=request.account_evidence.program_id,
            idl_hash=request.account_evidence.idl_hash,
            global_config_hash=request.account_evidence.global_config_hash,
        ),
        program_configs=request.program_configs,
        fee_schedules=request.fee_schedules,
        registry_version=request.registry_version,
    )
    if isinstance(protocol_result, AbstainResult):
        return protocol_result

    protocol_error = _validate_resolved_protocol(protocol_result, as_of_slot)
    if protocol_error is not None:
        return protocol_error

    return (
        PumpCreateMintMetadataProof(
            as_of_slot=as_of_slot,
            base_mint_pubkey=request.base_mint_evidence.mint_pubkey,
            quote_mint_pubkey=request.quote_mint_evidence.mint_pubkey,
            base_decimals=request.base_mint_evidence.decimals,
            quote_decimals=request.quote_mint_evidence.decimals,
            source_artifact=_metadata_source_artifact(request),
        ),
        protocol_result,
    )


def _validate_request_shape(
    request: object,
) -> AbstainResult | None:
    if type(request) is not PumpMetadataResolveRequest:
        return _abstain(
            AbstainReason.MISSING_FEATURE,
            "Pump metadata resolve request is required",
            -1,
        )
    if type(request.as_of_slot) is not int or request.as_of_slot < 0:
        return _abstain(
            AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
            "metadata resolve as_of_slot must be a non-negative integer",
            _safe_slot(request.as_of_slot),
        )
    return None


def _validate_evidence(
    request: PumpMetadataResolveRequest,
) -> AbstainResult | None:
    account = request.account_evidence
    base = request.base_mint_evidence
    quote = request.quote_mint_evidence
    if (
        type(account) is not PumpFinalizedAccountMetadataEvidence
        or type(base) is not PumpFinalizedMintMetadataEvidence
        or type(quote) is not PumpFinalizedMintMetadataEvidence
    ):
        return _abstain(
            AbstainReason.MISSING_FEATURE,
            "typed finalized account and mint evidence is required",
            request.as_of_slot,
        )

    common_error = next(
        (
            error
            for evidence in (account, base, quote)
            for error in (_validate_common_evidence(evidence, request.as_of_slot),)
            if error is not None
        ),
        None,
    )
    if common_error is not None:
        return common_error

    account_error = _validate_account_evidence(account, request.as_of_slot)
    if account_error is not None:
        return account_error
    base_error = _validate_mint_evidence(
        base,
        request.as_of_slot,
        expected_owner=SPL_2022_PROGRAM_ID,
        label="base",
    )
    if base_error is not None:
        return base_error
    return _validate_mint_evidence(
        quote,
        request.as_of_slot,
        expected_owner=SOL_PUBKEY,
        label="quote",
    )


def _validate_common_evidence(
    evidence: (
        PumpFinalizedAccountMetadataEvidence | PumpFinalizedMintMetadataEvidence
    ),
    as_of_slot: Slot,
) -> AbstainResult | None:
    if evidence.as_of_slot != as_of_slot:
        return _abstain(
            AbstainReason.STALE_STATE,
            "all metadata evidence must use the request as_of_slot",
            as_of_slot,
        )
    if evidence.commitment != "finalized":
        return _abstain(
            AbstainReason.STALE_STATE,
            "metadata evidence must be finalized",
            as_of_slot,
        )
    if not evidence.source_artifact:
        return _abstain(
            AbstainReason.MISSING_FEATURE,
            "metadata evidence source_artifact is required",
            as_of_slot,
        )
    return None


def _validate_account_evidence(
    evidence: PumpFinalizedAccountMetadataEvidence,
    as_of_slot: Slot,
) -> AbstainResult | None:
    if any(
        type(value) is not str or not value
        for value in (
            evidence.account_pubkey,
            evidence.owner_program_id,
            evidence.program_id,
            evidence.idl_hash,
            evidence.global_config_hash,
        )
    ):
        return _abstain(
            AbstainReason.MISSING_FEATURE,
            "account metadata identity and registry provenance are required",
            as_of_slot,
        )
    if evidence.owner_program_id != PUMP_PROGRAM_ID:
        return _abstain(
            AbstainReason.UNKNOWN_PROTOCOL_STATE,
            "account metadata owner is not the pinned Pump program",
            as_of_slot,
        )
    if evidence.program_id != PUMP_PROGRAM_ID:
        return _abstain(
            AbstainReason.UNKNOWN_PROTOCOL_STATE,
            "account metadata is not for the pinned Pump program",
            as_of_slot,
        )
    if evidence.idl_hash != PINNED_PUMP_IDL_SHA256:
        return _abstain(
            AbstainReason.DECODER_MISMATCH,
            "account metadata IDL hash does not match the pinned Pump IDL",
            as_of_slot,
        )
    return None


def _validate_mint_evidence(
    evidence: PumpFinalizedMintMetadataEvidence,
    as_of_slot: Slot,
    *,
    expected_owner: str,
    label: str,
) -> AbstainResult | None:
    if type(evidence.mint_pubkey) is not str or not evidence.mint_pubkey:
        return _abstain(
            AbstainReason.MISSING_FEATURE,
            f"{label} mint pubkey is required",
            as_of_slot,
        )
    if evidence.owner_program_id != expected_owner:
        return _abstain(
            AbstainReason.UNKNOWN_PROTOCOL_STATE,
            f"{label} mint owner is not the pinned owner",
            as_of_slot,
        )
    if type(evidence.decimals) is not int or not (
        0 <= evidence.decimals <= MAX_SUPPORTED_DECIMALS
    ):
        return _abstain(
            AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
            f"{label} mint decimals are unsupported",
            as_of_slot,
        )
    if label == "quote" and (
        evidence.mint_pubkey != SOL_PUBKEY or evidence.decimals != NATIVE_SOL_DECIMALS
    ):
        return _abstain(
            AbstainReason.UNKNOWN_PROTOCOL_STATE,
            "Pump quote metadata is not the pinned native SOL metadata",
            as_of_slot,
        )
    return None


def _validate_registry_shape(
    request: PumpMetadataResolveRequest,
) -> AbstainResult | None:
    if request.registry_version != PUMP_VERSION_REGISTRY_VERSION:
        return _abstain(
            AbstainReason.DECODER_MISMATCH,
            "Pump registry artifact version is not the pinned registry",
            request.as_of_slot,
        )
    if type(request.program_configs) is not tuple or not all(
        type(value) is PumpProgramConfigVersion for value in request.program_configs
    ):
        return _abstain(
            AbstainReason.UNKNOWN_PROTOCOL_STATE,
            "typed Pump program registry artifacts are required",
            request.as_of_slot,
        )
    if type(request.fee_schedules) is not tuple or not all(
        type(value) is PumpFeeScheduleVersion for value in request.fee_schedules
    ):
        return _abstain(
            AbstainReason.UNKNOWN_FEE_CONFIG,
            "typed Pump fee registry artifacts are required",
            request.as_of_slot,
        )
    return None


def _validate_resolved_protocol(
    snapshot: PumpProtocolVersionSnapshot,
    as_of_slot: Slot,
) -> AbstainResult | None:
    if snapshot.as_of_slot != as_of_slot:
        return _abstain(
            AbstainReason.STALE_STATE,
            "resolved Pump protocol snapshot uses a different as_of_slot",
            as_of_slot,
        )
    if snapshot.program_id != PUMP_PROGRAM_ID:
        return _abstain(
            AbstainReason.UNKNOWN_PROTOCOL_STATE,
            "resolved protocol snapshot is not for the pinned Pump program",
            as_of_slot,
        )
    if snapshot.idl_hash != PINNED_PUMP_IDL_SHA256:
        return _abstain(
            AbstainReason.DECODER_MISMATCH,
            "resolved protocol snapshot IDL hash is not pinned",
            as_of_slot,
        )
    return None


def _metadata_source_artifact(request: PumpMetadataResolveRequest) -> str:
    return ":".join(
        (
            request.account_evidence.source_artifact,
            request.base_mint_evidence.source_artifact,
            request.quote_mint_evidence.source_artifact,
        )
    )


def _safe_slot(value: object) -> int:
    return value if type(value) is int else -1


def _abstain(reason: AbstainReason, message: str, as_of_slot: int) -> AbstainResult:
    return AbstainResult(reason=reason, message=message, as_of_slot=as_of_slot)


__all__ = [
    "PumpFinalizedAccountMetadataEvidence",
    "PumpFinalizedMintMetadataEvidence",
    "PumpMetadataResolveRequest",
    "PumpMetadataResolveResult",
    "resolve_pump_create_metadata",
]
