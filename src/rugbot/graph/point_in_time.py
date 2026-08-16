"""Pure point-in-time address graph snapshots."""

from dataclasses import dataclass
from enum import Enum

from rugbot.domain.amounts import Slot
from rugbot.domain.decisions import AbstainReason, AbstainResult

PROBABILITY_PPM_DENOMINATOR = 1_000_000


class AddressRelationshipKind(Enum):
    """Evidence kind for a direct address relationship."""

    DIRECT_NATIVE_TRANSFER = "direct_native_transfer"
    DIRECT_ASSET_TRANSFER = "direct_asset_transfer"
    SHARED_PRE_LAUNCH_FUNDER = "shared_pre_launch_funder"
    SHARED_FEE_PAYER = "shared_fee_payer"
    REPEATED_CO_BUYING = "repeated_co_buying"
    REPEATED_CO_SELLING = "repeated_co_selling"
    SAME_METADATA_CONTENT_HASH = "same_metadata_content_hash"
    SAME_CREATION_CADENCE = "same_creation_cadence"
    SAME_FIRST_BUY_PATTERN = "same_first_buy_pattern"
    SAME_SELL_DESTINATION = "same_sell_destination"
    SAME_TRANSACTION_FINGERPRINT = "same_transaction_fingerprint"
    SHARED_SERVICE = "shared_service"


@dataclass(frozen=True, slots=True)
class AddressEvidenceEdge:
    """One direct, temporal, probabilistic address relationship evidence item."""

    as_of_slot: Slot
    source_address: str
    target_address: str
    relationship_kind: AddressRelationshipKind
    observed_slot: Slot
    valid_from_slot: Slot
    valid_to_slot: Slot | None
    confidence_ppm: int
    same_controller_probability_ppm: int
    cooperating_probability_ppm: int
    shared_service_probability_ppm: int
    incidental_interaction_probability_ppm: int
    half_life_slots: int
    evidence_ids: tuple[str, ...]
    model_version: str


@dataclass(frozen=True, slots=True)
class AddressRelationshipView:
    """Point-in-time direct edge view after confidence time decay."""

    as_of_slot: Slot
    source_address: str
    target_address: str
    relationship_kind: AddressRelationshipKind
    observed_slot: Slot
    age_slots: int
    raw_confidence_ppm: int
    decayed_confidence_ppm: int
    same_controller_probability_ppm: int
    cooperating_probability_ppm: int
    shared_service_probability_ppm: int
    incidental_interaction_probability_ppm: int
    evidence_ids: tuple[str, ...]
    model_version: str


@dataclass(frozen=True, slots=True)
class AddressGraphSnapshot:
    """Immutable direct-edge graph snapshot at one slot boundary."""

    as_of_slot: Slot
    snapshot_version: str
    relationships: tuple[AddressRelationshipView, ...]
    source_edge_count: int
    active_edge_count: int
    skipped_inactive_edge_count: int


def build_address_graph_snapshot(
    *,
    edges: tuple[AddressEvidenceEdge, ...],
    as_of_slot: Slot,
    snapshot_version: str,
) -> AddressGraphSnapshot | AbstainResult:
    """Build a direct, point-in-time address graph snapshot.

    The builder filters edges by temporal validity and applies deterministic
    integer confidence decay. It intentionally does not perform transitive
    closure or entity resolution.
    """

    request_error = _validate_snapshot_request(as_of_slot, snapshot_version)
    if request_error is not None:
        return request_error

    relationships: list[AddressRelationshipView] = []
    skipped_inactive_edge_count = 0
    for edge in edges:
        edge_error = _validate_edge(edge, as_of_slot)
        if edge_error is not None:
            return edge_error
        if not _edge_active_at(edge, as_of_slot):
            skipped_inactive_edge_count += 1
            continue
        relationships.append(_relationship_view(edge=edge, as_of_slot=as_of_slot))

    return AddressGraphSnapshot(
        as_of_slot=as_of_slot,
        snapshot_version=snapshot_version,
        relationships=tuple(
            sorted(
                relationships,
                key=lambda relationship: (
                    relationship.source_address,
                    relationship.target_address,
                    relationship.relationship_kind.value,
                    int(relationship.observed_slot),
                    relationship.evidence_ids,
                ),
            )
        ),
        source_edge_count=len(edges),
        active_edge_count=len(relationships),
        skipped_inactive_edge_count=skipped_inactive_edge_count,
    )


def direct_relationships_for_address(
    *,
    snapshot: AddressGraphSnapshot,
    address: str,
) -> tuple[AddressRelationshipView, ...]:
    """Return direct relationships touching one address without closure."""

    return tuple(
        relationship
        for relationship in snapshot.relationships
        if address in {relationship.source_address, relationship.target_address}
    )


def _validate_snapshot_request(
    as_of_slot: Slot,
    snapshot_version: str,
) -> AbstainResult | None:
    if not _non_negative_int(as_of_slot):
        return _unsupported("as_of_slot must be a non-negative integer", as_of_slot)
    if not isinstance(snapshot_version, str) or not snapshot_version:
        return _decoder_mismatch("snapshot_version is required", as_of_slot)
    return None


def _validate_edge(
    edge: AddressEvidenceEdge,
    snapshot_as_of_slot: Slot,
) -> AbstainResult | None:
    for validation in (
        _validate_edge_slots,
        _validate_edge_addresses,
        _validate_edge_probabilities,
        _validate_edge_decay,
        _validate_edge_provenance,
    ):
        validation_error = validation(edge, snapshot_as_of_slot)
        if validation_error is not None:
            return validation_error
    return None


def _validate_edge_slots(
    edge: AddressEvidenceEdge,
    snapshot_as_of_slot: Slot,
) -> AbstainResult | None:
    slot_fields = (
        edge.as_of_slot,
        edge.observed_slot,
        edge.valid_from_slot,
        snapshot_as_of_slot,
    )
    if any(not _non_negative_int(slot) for slot in slot_fields):
        return _unsupported(
            "edge slot fields must be non-negative integers",
            snapshot_as_of_slot,
        )
    if edge.valid_to_slot is not None and not _non_negative_int(edge.valid_to_slot):
        return _unsupported("edge valid_to_slot is invalid", snapshot_as_of_slot)
    if edge.valid_to_slot is not None and edge.valid_to_slot <= edge.valid_from_slot:
        return _unsupported(
            "edge valid_to_slot must be after valid_from_slot",
            snapshot_as_of_slot,
        )
    if (
        edge.observed_slot > edge.as_of_slot
        or edge.as_of_slot > snapshot_as_of_slot
        or edge.valid_from_slot > snapshot_as_of_slot
    ):
        return _stale(
            "edge evidence is newer than the graph snapshot", snapshot_as_of_slot
        )
    return None


def _validate_edge_addresses(
    edge: AddressEvidenceEdge,
    snapshot_as_of_slot: Slot,
) -> AbstainResult | None:
    if not isinstance(edge.source_address, str) or not edge.source_address:
        return _missing("edge source_address is required", snapshot_as_of_slot)
    if not isinstance(edge.target_address, str) or not edge.target_address:
        return _missing("edge target_address is required", snapshot_as_of_slot)
    if edge.source_address == edge.target_address:
        return _unsupported("self edges are unsupported", snapshot_as_of_slot)
    if not isinstance(edge.relationship_kind, AddressRelationshipKind):
        return _unsupported("edge relationship_kind is invalid", snapshot_as_of_slot)
    return None


def _validate_edge_probabilities(
    edge: AddressEvidenceEdge,
    snapshot_as_of_slot: Slot,
) -> AbstainResult | None:
    probability_fields = {
        "confidence_ppm": edge.confidence_ppm,
        "same_controller_probability_ppm": edge.same_controller_probability_ppm,
        "cooperating_probability_ppm": edge.cooperating_probability_ppm,
        "shared_service_probability_ppm": edge.shared_service_probability_ppm,
        "incidental_interaction_probability_ppm": (
            edge.incidental_interaction_probability_ppm
        ),
    }
    for field_name, value in probability_fields.items():
        if not _valid_probability_ppm(value):
            return _unsupported(
                f"{field_name} must be in probability ppm range",
                snapshot_as_of_slot,
            )
    return None


def _validate_edge_decay(
    edge: AddressEvidenceEdge,
    snapshot_as_of_slot: Slot,
) -> AbstainResult | None:
    if not _positive_int(edge.half_life_slots):
        return _unsupported(
            "edge half_life_slots must be positive", snapshot_as_of_slot
        )
    return None


def _validate_edge_provenance(
    edge: AddressEvidenceEdge,
    snapshot_as_of_slot: Slot,
) -> AbstainResult | None:
    if not edge.evidence_ids or any(
        not isinstance(evidence_id, str) or not evidence_id
        for evidence_id in edge.evidence_ids
    ):
        return _missing("edge evidence_ids are required", snapshot_as_of_slot)
    if not isinstance(edge.model_version, str) or not edge.model_version:
        return _decoder_mismatch("edge model_version is required", snapshot_as_of_slot)
    return None


def _edge_active_at(edge: AddressEvidenceEdge, as_of_slot: Slot) -> bool:
    if edge.valid_from_slot > as_of_slot:
        return False
    return edge.valid_to_slot is None or as_of_slot < edge.valid_to_slot


def _relationship_view(
    *,
    edge: AddressEvidenceEdge,
    as_of_slot: Slot,
) -> AddressRelationshipView:
    age_slots = as_of_slot - edge.observed_slot
    decayed_confidence_ppm = _decayed_confidence_ppm(
        confidence_ppm=edge.confidence_ppm,
        age_slots=age_slots,
        half_life_slots=edge.half_life_slots,
    )
    return AddressRelationshipView(
        as_of_slot=as_of_slot,
        source_address=edge.source_address,
        target_address=edge.target_address,
        relationship_kind=edge.relationship_kind,
        observed_slot=edge.observed_slot,
        age_slots=age_slots,
        raw_confidence_ppm=edge.confidence_ppm,
        decayed_confidence_ppm=decayed_confidence_ppm,
        same_controller_probability_ppm=_weighted_probability_ppm(
            edge.same_controller_probability_ppm,
            decayed_confidence_ppm,
        ),
        cooperating_probability_ppm=_weighted_probability_ppm(
            edge.cooperating_probability_ppm,
            decayed_confidence_ppm,
        ),
        shared_service_probability_ppm=_weighted_probability_ppm(
            edge.shared_service_probability_ppm,
            decayed_confidence_ppm,
        ),
        incidental_interaction_probability_ppm=_weighted_probability_ppm(
            edge.incidental_interaction_probability_ppm,
            decayed_confidence_ppm,
        ),
        evidence_ids=edge.evidence_ids,
        model_version=edge.model_version,
    )


def _decayed_confidence_ppm(
    *,
    confidence_ppm: int,
    age_slots: int,
    half_life_slots: int,
) -> int:
    decayed = confidence_ppm
    full_half_lives = age_slots // half_life_slots
    remainder_slots = age_slots % half_life_slots
    for _ in range(full_half_lives):
        decayed //= 2
    if remainder_slots == 0:
        return decayed
    half_value = decayed // 2
    return decayed - ((decayed - half_value) * remainder_slots // half_life_slots)


def _weighted_probability_ppm(
    probability_ppm: int,
    decayed_confidence_ppm: int,
) -> int:
    return probability_ppm * decayed_confidence_ppm // PROBABILITY_PPM_DENOMINATOR


def _valid_probability_ppm(value: object) -> bool:
    return _non_negative_int(value) and value <= PROBABILITY_PPM_DENOMINATOR


def _non_negative_int(value: object) -> bool:
    return type(value) is int and value >= 0


def _positive_int(value: object) -> bool:
    return type(value) is int and value > 0


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
