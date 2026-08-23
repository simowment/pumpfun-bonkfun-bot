"""Pure launch-artifact adapters for known-operator matcher signals."""

from dataclasses import dataclass

from rugbot.domain.account_roles import AddressRole
from rugbot.domain.amounts import PROBABILITY_PPM_DENOMINATOR, Slot
from rugbot.domain.decisions import AbstainReason, AbstainResult
from rugbot.domain.launches import LaunchActorRole, LaunchCreatedV2


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


PUMP_CREATE_V2_LAUNCH_SIGNAL_SOURCE_VERSION = "pump-create-v2-launch-signals-v1"
ACCEPTED_PUMP_CREATE_V2_DECODER_VERSION = "pump-create-v2-instruction-v1"
ACCEPTED_PUMP_CREATE_V2_IDL_SHA256 = (
    "b90bc471327f671449271d5d1d42354d1fae6f5a06502f5834459a3108138e49"
)
ACCEPTED_PUMP_PROGRAM_ID = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
CREATE_V2_INSTRUCTION_NAME = "create_v2"
ACCOUNT_ROLE_PROOF_SIZE = 2
ACTOR_ROLE_PROOF_SIZE = 5


@dataclass(frozen=True, slots=True)
class _LaunchSignalContext:
    as_of_slot: Slot
    source_version: str
    signal_probability_ppm: int


@dataclass(frozen=True, slots=True)
class _ActorProof:
    account_index: int
    pubkey: str
    evidence_ids: tuple[str, ...]
    source_version: str


@dataclass(frozen=True, slots=True)
class _RawActorProof:
    role: object
    account_index: object
    pubkey: object
    evidence_ids: object
    source_version: object


@dataclass(frozen=True, slots=True)
class _ActorSignalSpec:
    actor_role: LaunchActorRole
    address_role: AddressRole
    pubkey: str | None
    account_index: int | None


@dataclass(frozen=True, slots=True)
class _PreparedLaunchSignals:
    context: _LaunchSignalContext
    actor_proofs: dict[str, _ActorProof]


def pump_create_v2_launch_address_signals(
    *,
    launch: LaunchCreatedV2,
    as_of_slot: Slot,
    source_version: str = PUMP_CREATE_V2_LAUNCH_SIGNAL_SOURCE_VERSION,
) -> tuple[LaunchAddressSignal, ...] | AbstainResult:
    """Project explicit Pump create_v2 launch actors into matcher signals."""

    prepared = _prepare_launch_signal_inputs(
        launch=launch,
        as_of_slot=as_of_slot,
        source_version=source_version,
    )
    if isinstance(prepared, AbstainResult):
        return prepared

    base_signals = _base_launch_signals(launch=launch, context=prepared.context)
    actor_signals = _actor_signals(
        launch=launch,
        context=prepared.context,
        actor_proofs=prepared.actor_proofs,
    )
    if isinstance(actor_signals, AbstainResult):
        return actor_signals
    return (*base_signals, *actor_signals)


def _prepare_launch_signal_inputs(
    *,
    launch: object,
    as_of_slot: object,
    source_version: object,
) -> _PreparedLaunchSignals | AbstainResult:
    context = _validated_launch_signal_context(
        launch=launch,
        as_of_slot=as_of_slot,
        source_version=source_version,
    )
    if isinstance(context, AbstainResult):
        return context
    account_proofs = _account_role_proofs(launch)
    if isinstance(account_proofs, AbstainResult):
        return account_proofs
    user_error = _validate_user_account_proof(launch, account_proofs)
    if user_error is not None:
        return user_error
    actor_proofs = _actor_proofs_by_role(launch)
    if isinstance(actor_proofs, AbstainResult):
        return actor_proofs
    return _PreparedLaunchSignals(
        context=context,
        actor_proofs=actor_proofs,
    )


def _validated_launch_signal_context(
    *,
    launch: object,
    as_of_slot: object,
    source_version: object,
) -> _LaunchSignalContext | AbstainResult:
    request_error = _validate_request(
        as_of_slot=as_of_slot,
        source_version=source_version,
    )
    if request_error is not None:
        return request_error
    if type(launch) is not LaunchCreatedV2:
        return _unsupported("create_v2 launch artifact is malformed", as_of_slot)
    launch_error = _validate_launch(launch, as_of_slot)
    if launch_error is not None:
        return launch_error
    return _LaunchSignalContext(
        as_of_slot=as_of_slot,
        source_version=source_version,
        signal_probability_ppm=PROBABILITY_PPM_DENOMINATOR,
    )


def _validate_request(
    *,
    as_of_slot: object,
    source_version: object,
) -> AbstainResult | None:
    if not _non_negative_int(as_of_slot):
        return _unsupported("launch signal as_of_slot is invalid", as_of_slot)
    if not _non_empty_str(source_version):
        return _decoder_mismatch("launch signal source_version is required", as_of_slot)
    return None


def _validate_launch(
    launch: LaunchCreatedV2,
    as_of_slot: Slot,
) -> AbstainResult | None:
    validators = (
        _validate_launch_slot,
        _validate_launch_instruction,
        _validate_launch_required_fields,
        _validate_launch_versions,
    )
    for validator in validators:
        validation_error = validator(launch, as_of_slot)
        if validation_error is not None:
            return validation_error
    return None


def _validate_launch_slot(
    launch: LaunchCreatedV2,
    as_of_slot: Slot,
) -> AbstainResult | None:
    if not _non_negative_int(launch.as_of_slot):
        return _unsupported("create_v2 launch as_of_slot is invalid", -1)
    if launch.as_of_slot != as_of_slot:
        return _stale("create_v2 launch uses a different as_of_slot", as_of_slot)
    return None


def _validate_launch_instruction(
    launch: LaunchCreatedV2,
    as_of_slot: Slot,
) -> AbstainResult | None:
    if launch.program_id != ACCEPTED_PUMP_PROGRAM_ID:
        return _unsupported("create_v2 launch program_id is unsupported", as_of_slot)
    if launch.instruction_name != CREATE_V2_INSTRUCTION_NAME:
        return _unsupported("launch instruction is not create_v2", as_of_slot)
    if launch.creation_instruction_type != CREATE_V2_INSTRUCTION_NAME:
        return _unsupported(
            "launch creation instruction type is not create_v2",
            as_of_slot,
        )
    return None


def _validate_launch_required_fields(
    launch: LaunchCreatedV2,
    as_of_slot: Slot,
) -> AbstainResult | None:
    required_fields = {
        "launch_id": launch.launch_id,
        "creator_pubkey": launch.creator_pubkey,
        "user_pubkey": launch.user_pubkey,
    }
    for field_name, value in required_fields.items():
        if not _non_empty_str(value):
            return _missing(f"create_v2 launch {field_name} is required", as_of_slot)
    return None


def _validate_launch_versions(
    launch: LaunchCreatedV2,
    as_of_slot: Slot,
) -> AbstainResult | None:
    if launch.decoder_version != ACCEPTED_PUMP_CREATE_V2_DECODER_VERSION:
        return _decoder_mismatch(
            "create_v2 launch decoder_version is unknown",
            as_of_slot,
        )
    if launch.idl_hash != ACCEPTED_PUMP_CREATE_V2_IDL_SHA256:
        return _decoder_mismatch("create_v2 launch idl_hash is unknown", as_of_slot)
    return None


def _account_role_proofs(
    launch: LaunchCreatedV2,
) -> dict[str, str] | AbstainResult:
    if type(launch.account_role_proofs) is not tuple:
        return _unsupported(
            "create_v2 account role proofs must be immutable",
            launch.as_of_slot,
        )

    proofs: dict[str, str] = {}
    for proof in launch.account_role_proofs:
        parsed = _parse_account_role_proof(proof, launch.as_of_slot)
        if isinstance(parsed, AbstainResult):
            return parsed
        name, pubkey = parsed
        if name in proofs:
            return _unsupported(
                "duplicate create_v2 account role proof", launch.as_of_slot
            )
        proofs[name] = pubkey
    return proofs


def _parse_account_role_proof(
    proof: object,
    as_of_slot: Slot,
) -> tuple[str, str] | AbstainResult:
    if type(proof) is not tuple or len(proof) != ACCOUNT_ROLE_PROOF_SIZE:
        return _unsupported("create_v2 account role proof is malformed", as_of_slot)
    proof_name, proof_pubkey = proof
    if not _non_empty_str(proof_name) or not _non_empty_str(proof_pubkey):
        return _missing("create_v2 account role proof is incomplete", as_of_slot)
    return proof_name, proof_pubkey


def _validate_user_account_proof(
    launch: LaunchCreatedV2,
    account_proofs: dict[str, str],
) -> AbstainResult | None:
    user_proof = account_proofs.get("user")
    if user_proof is None:
        return _missing(
            "create_v2 user account role proof is required", launch.as_of_slot
        )
    if user_proof != launch.user_pubkey:
        return _unsupported(
            "create_v2 user account proof does not match launch user",
            launch.as_of_slot,
        )
    return None


def _actor_proofs_by_role(
    launch: LaunchCreatedV2,
) -> dict[str, _ActorProof] | AbstainResult:
    if type(launch.actor_role_proofs) is not tuple:
        return _unsupported(
            "create_v2 actor role proofs must be immutable",
            launch.as_of_slot,
        )

    proofs: dict[str, _ActorProof] = {}
    for proof in launch.actor_role_proofs:
        parsed = _parse_actor_role_proof(proof, launch.as_of_slot)
        if isinstance(parsed, AbstainResult):
            return parsed
        if parsed[0] in proofs:
            return _unsupported(
                "duplicate create_v2 actor role proof", launch.as_of_slot
            )
        proofs[parsed[0]] = parsed[1]
    return proofs


def _parse_actor_role_proof(
    proof: object,
    as_of_slot: Slot,
) -> tuple[str, _ActorProof] | AbstainResult:
    if type(proof) is not tuple or len(proof) != ACTOR_ROLE_PROOF_SIZE:
        return _unsupported("create_v2 actor role proof is malformed", as_of_slot)
    raw_proof = _RawActorProof(*proof)
    proof_error = _validate_actor_role_proof(
        raw_proof,
        as_of_slot=as_of_slot,
    )
    if proof_error is not None:
        return proof_error
    return (
        raw_proof.role,
        _ActorProof(
            account_index=raw_proof.account_index,
            pubkey=raw_proof.pubkey,
            evidence_ids=raw_proof.evidence_ids,
            source_version=raw_proof.source_version,
        ),
    )


def _validate_actor_role_proof(
    raw_proof: _RawActorProof,
    *,
    as_of_slot: Slot,
) -> AbstainResult | None:
    if not _non_empty_str(raw_proof.role) or not _non_empty_str(raw_proof.pubkey):
        return _missing("create_v2 actor role proof identity is required", as_of_slot)
    if raw_proof.role not in _accepted_actor_role_names():
        return _unsupported("create_v2 actor role proof is unknown", as_of_slot)
    if not _non_negative_int(raw_proof.account_index):
        return _unsupported(
            "create_v2 actor role proof account_index is invalid",
            as_of_slot,
        )
    if not _valid_evidence_ids(raw_proof.evidence_ids):
        return _missing(
            "create_v2 actor role proof evidence_ids are required",
            as_of_slot,
        )
    if not _non_empty_str(raw_proof.source_version):
        return _decoder_mismatch(
            "create_v2 actor role proof source_version is required",
            as_of_slot,
        )
    return None


def _base_launch_signals(
    *,
    launch: LaunchCreatedV2,
    context: _LaunchSignalContext,
) -> tuple[LaunchAddressSignal, ...]:
    return (
        _signal(
            context,
            launch,
            launch.creator_pubkey,
            AddressRole.CREATOR,
            _field_evidence_ids(launch, "args.creator", launch.creator_pubkey),
        ),
        _signal(
            context,
            launch,
            launch.user_pubkey,
            AddressRole.CREATION_SUBMITTER,
            _field_evidence_ids(launch, "accounts.user", launch.user_pubkey),
        ),
    )


def _actor_signals(
    *,
    launch: LaunchCreatedV2,
    context: _LaunchSignalContext,
    actor_proofs: dict[str, _ActorProof],
) -> tuple[LaunchAddressSignal, ...] | AbstainResult:
    signals: list[LaunchAddressSignal] = []
    for spec in _actor_signal_specs(launch):
        signal = _actor_signal(
            launch=launch,
            context=context,
            actor_proofs=actor_proofs,
            spec=spec,
        )
        if isinstance(signal, AbstainResult):
            return signal
        if signal is not None:
            signals.append(signal)
    return tuple(signals)


def _actor_signal_specs(
    launch: LaunchCreatedV2,
) -> tuple[_ActorSignalSpec, ...]:
    return (
        _ActorSignalSpec(
            actor_role=LaunchActorRole.FEE_PAYER,
            address_role=AddressRole.FEE_PAYER,
            pubkey=launch.fee_payer_pubkey,
            account_index=launch.fee_payer_account_index,
        ),
        _ActorSignalSpec(
            actor_role=LaunchActorRole.FIRST_BUYER,
            address_role=AddressRole.FIRST_BUYER,
            pubkey=launch.first_buyer_pubkey,
            account_index=launch.first_buyer_account_index,
        ),
    )


def _actor_signal(
    *,
    launch: LaunchCreatedV2,
    context: _LaunchSignalContext,
    actor_proofs: dict[str, _ActorProof],
    spec: _ActorSignalSpec,
) -> LaunchAddressSignal | AbstainResult | None:
    proof = actor_proofs.get(spec.actor_role.value)
    if spec.pubkey is None and spec.account_index is None:
        if proof is not None:
            return _unsupported(
                f"create_v2 {spec.actor_role.value} proof has no launch actor",
                launch.as_of_slot,
            )
        return None
    if not _non_empty_str(spec.pubkey) or not _non_negative_int(spec.account_index):
        return _unsupported(
            f"create_v2 {spec.actor_role.value} actor is malformed",
            launch.as_of_slot,
        )
    if proof is None:
        return _missing(
            f"create_v2 {spec.actor_role.value} actor proof is required",
            launch.as_of_slot,
        )
    proof_error = _actor_proof_mismatch_error(proof, spec, launch.as_of_slot)
    if proof_error is not None:
        return proof_error
    return _signal(
        context,
        launch,
        spec.pubkey,
        spec.address_role,
        proof.evidence_ids,
    )


def _actor_proof_mismatch_error(
    proof: _ActorProof,
    spec: _ActorSignalSpec,
    as_of_slot: Slot,
) -> AbstainResult | None:
    if proof.account_index != spec.account_index or proof.pubkey != spec.pubkey:
        return _unsupported(
            f"create_v2 {spec.actor_role.value} actor proof mismatch",
            as_of_slot,
        )
    return None


def _signal(
    context: _LaunchSignalContext,
    launch: LaunchCreatedV2,
    address: str,
    role: AddressRole,
    evidence_ids: tuple[str, ...],
) -> LaunchAddressSignal:
    return LaunchAddressSignal(
        as_of_slot=context.as_of_slot,
        launch_id=launch.launch_id,
        address=address,
        role=role,
        signal_probability_ppm=context.signal_probability_ppm,
        evidence_ids=evidence_ids,
        source_version=context.source_version,
    )


def _field_evidence_ids(
    launch: LaunchCreatedV2,
    component: str,
    address: str,
) -> tuple[str, ...]:
    return (
        (
            f"launch:{launch.launch_id}:slot:{launch.as_of_slot}:"
            f"create_v2:{component}:{address}"
        ),
    )


def _accepted_actor_role_names() -> frozenset[str]:
    return frozenset(
        (
            LaunchActorRole.FEE_PAYER.value,
            LaunchActorRole.FIRST_BUYER.value,
        )
    )


def _valid_evidence_ids(evidence_ids: object) -> bool:
    return (
        type(evidence_ids) is tuple
        and bool(evidence_ids)
        and all(
            isinstance(evidence_id, str) and evidence_id for evidence_id in evidence_ids
        )
    )


def _non_empty_str(value: object) -> bool:
    return type(value) is str and bool(value.strip())


def _non_negative_int(value: object) -> bool:
    return type(value) is int and value >= 0


def _missing(message: str, as_of_slot: object) -> AbstainResult:
    return _abstain(
        reason=AbstainReason.MISSING_FEATURE,
        message=message,
        as_of_slot=as_of_slot,
    )


def _stale(message: str, as_of_slot: object) -> AbstainResult:
    return _abstain(
        reason=AbstainReason.STALE_STATE,
        message=message,
        as_of_slot=as_of_slot,
    )


def _decoder_mismatch(message: str, as_of_slot: object) -> AbstainResult:
    return _abstain(
        reason=AbstainReason.DECODER_MISMATCH,
        message=message,
        as_of_slot=as_of_slot,
    )


def _unsupported(message: str, as_of_slot: object) -> AbstainResult:
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
