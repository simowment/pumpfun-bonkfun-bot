"""Entity resolution and address role classification tests."""

import ast
import unittest
from dataclasses import replace
from pathlib import Path
from typing import Any, cast

from rugbot.domain.amounts import Slot
from rugbot.domain.decisions import AbstainReason, AbstainResult
from rugbot.graph.entity_resolution import (
    AddressRole,
    AddressRoleEvidence,
    AddressRoleSnapshot,
    EntityResolutionConfig,
    EntitySeedEvidence,
    ProbabilisticEntity,
    classify_address_roles,
    resolve_probabilistic_entity,
)
from rugbot.graph.point_in_time import (
    AddressEvidenceEdge,
    AddressGraphSnapshot,
    AddressRelationshipKind,
    AddressRelationshipView,
    build_address_graph_snapshot,
)

ENTITY_MODULE = Path("src/rugbot/graph/entity_resolution.py")
FORBIDDEN_IMPORT_PREFIXES = (
    "requests",
    "aiohttp",
    "httpx",
    "sqlite",
    "psycopg",
    "rugbot.ingest",
    "rugbot.storage",
    "rugbot.execution",
    "rugbot.protocol",
    "src.core",
    "src.trading",
    "src.platforms",
    "solana",
    "solders",
    "dotenv",
)


class ProbabilisticEntityResolutionTests(unittest.TestCase):
    """Tests for conservative entity resolution from direct evidence."""

    def test_resolves_seed_and_direct_relationship_without_closure(self) -> None:
        """Resolution uses seed and direct edges but does not walk A-B-C."""

        graph = _graph(
            _edge(source="seed-a", target="linked-b"),
            _edge(source="linked-b", target="not-included-c"),
        )

        result = resolve_probabilistic_entity(
            graph=graph,
            seeds=(_seed(address="seed-a"),),
            config=_resolution_config(),
        )

        self.assertIsInstance(result, ProbabilisticEntity)
        entity = cast("ProbabilisticEntity", result)
        self.assertEqual(entity.as_of_slot, Slot(20))
        self.assertEqual(entity.graph_snapshot_version, "graph-v1")
        self.assertEqual(
            tuple(membership.address for membership in entity.memberships),
            ("linked-b", "seed-a"),
        )
        linked = entity.memberships[0]
        self.assertEqual(linked.source, "direct_graph_edge")
        self.assertEqual(linked.same_controller_probability_ppm, 900_000)
        self.assertEqual(entity.direct_relationship_count, 1)

    def test_shared_service_edge_does_not_create_membership(self) -> None:
        """Shared-service probability stays separate from membership support."""

        graph = _graph(
            _edge(
                source="seed-a",
                target="shared-service-peer",
                kind=AddressRelationshipKind.SHARED_SERVICE,
                same_controller=0,
                cooperating=0,
                shared_service=950_000,
            )
        )

        result = resolve_probabilistic_entity(
            graph=graph,
            seeds=(_seed(address="seed-a"),),
            config=_resolution_config(),
        )

        self.assertIsInstance(result, ProbabilisticEntity)
        entity = cast("ProbabilisticEntity", result)
        self.assertEqual(
            tuple(membership.address for membership in entity.memberships),
            ("seed-a",),
        )

    def test_graph_slot_mismatch_abstains(self) -> None:
        """Entity resolution cannot mix graph and request slots."""

        result = resolve_probabilistic_entity(
            graph=_graph(
                _edge(
                    source="seed-a",
                    target="linked-b",
                    edge_as_of=19,
                    observed_slot=19,
                ),
                as_of_slot=19,
            ),
            seeds=(_seed(address="seed-a"),),
            config=_resolution_config(),
        )

        self.assert_abstains(result, AbstainReason.STALE_STATE, as_of_slot=20)

    def test_seed_entity_mismatch_abstains(self) -> None:
        """Seeds for another entity cannot be mixed into a request."""

        result = resolve_probabilistic_entity(
            graph=_graph(_edge(source="seed-a", target="linked-b")),
            seeds=(_seed(address="seed-a", entity_id="entity-2"),),
            config=_resolution_config(),
        )

        self.assert_abstains(
            result,
            AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
            as_of_slot=20,
        )

    def test_future_seed_abstains(self) -> None:
        """Future seed evidence would leak later knowledge."""

        result = resolve_probabilistic_entity(
            graph=_graph(_edge(source="seed-a", target="linked-b")),
            seeds=(_seed(address="seed-a", valid_from_slot=21),),
            config=_resolution_config(),
        )

        self.assert_abstains(result, AbstainReason.STALE_STATE, as_of_slot=20)

    def test_expired_seed_is_skipped_and_empty_active_set_abstains(self) -> None:
        """Expired seed evidence is historical but not active membership."""

        result = resolve_probabilistic_entity(
            graph=_graph(_edge(source="seed-a", target="linked-b")),
            seeds=(_seed(address="seed-a", valid_to_slot=20),),
            config=_resolution_config(),
        )

        self.assert_abstains(result, AbstainReason.MISSING_FEATURE, as_of_slot=20)

    def test_float_seed_probability_abstains(self) -> None:
        """Runtime validators reject float probability values."""

        result = resolve_probabilistic_entity(
            graph=_graph(_edge(source="seed-a", target="linked-b")),
            seeds=(_seed(address="seed-a", same_controller=cast("Any", 0.5)),),
            config=_resolution_config(),
        )

        self.assert_abstains(
            result,
            AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
            as_of_slot=20,
        )

    def test_weak_active_seed_below_threshold_abstains(self) -> None:
        """Weak seed evidence does not become positive membership."""

        result = resolve_probabilistic_entity(
            graph=_graph(_edge(source="seed-a", target="linked-b")),
            seeds=(
                _seed(
                    address="seed-a",
                    same_controller=100_000,
                    cooperating=100_000,
                ),
            ),
            config=_resolution_config(),
        )

        self.assert_abstains(result, AbstainReason.MISSING_FEATURE, as_of_slot=20)

    def test_float_graph_relationship_probability_abstains(self) -> None:
        """Resolver revalidates prebuilt graph relationship probabilities."""

        result = resolve_probabilistic_entity(
            graph=_prebuilt_graph(
                replace(
                    _relationship(source="seed-a", target="linked-b"),
                    same_controller_probability_ppm=cast("Any", 0.5),
                )
            ),
            seeds=(_seed(address="seed-a"),),
            config=_resolution_config(),
        )

        self.assert_abstains(
            result,
            AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
            as_of_slot=20,
        )

    def test_missing_graph_relationship_provenance_abstains(self) -> None:
        """Resolver rejects graph relationships without source evidence IDs."""

        result = resolve_probabilistic_entity(
            graph=_prebuilt_graph(
                replace(
                    _relationship(source="seed-a", target="linked-b"), evidence_ids=()
                )
            ),
            seeds=(_seed(address="seed-a"),),
            config=_resolution_config(),
        )

        self.assert_abstains(result, AbstainReason.MISSING_FEATURE, as_of_slot=20)

    def test_missing_graph_relationship_model_version_abstains(self) -> None:
        """Resolver rejects graph relationships without model provenance."""

        result = resolve_probabilistic_entity(
            graph=_prebuilt_graph(
                replace(
                    _relationship(source="seed-a", target="linked-b"),
                    model_version="",
                )
            ),
            seeds=(_seed(address="seed-a"),),
            config=_resolution_config(),
        )

        self.assert_abstains(result, AbstainReason.DECODER_MISMATCH, as_of_slot=20)

    def test_future_graph_relationship_observed_slot_abstains(self) -> None:
        """Resolver revalidates relationship observed slots."""

        result = resolve_probabilistic_entity(
            graph=_prebuilt_graph(
                replace(
                    _relationship(source="seed-a", target="linked-b"),
                    observed_slot=Slot(21),
                )
            ),
            seeds=(_seed(address="seed-a"),),
            config=_resolution_config(),
        )

        self.assert_abstains(result, AbstainReason.STALE_STATE, as_of_slot=20)

    def test_invalid_graph_relationship_count_abstains(self) -> None:
        """Resolver rejects inconsistent prebuilt graph counts."""

        result = resolve_probabilistic_entity(
            graph=_prebuilt_graph(
                _relationship(source="seed-a", target="linked-b"),
                active_edge_count=2,
            ),
            seeds=(_seed(address="seed-a"),),
            config=_resolution_config(),
        )

        self.assert_abstains(
            result,
            AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
            as_of_slot=20,
        )

    def test_invalid_graph_relationship_kind_abstains(self) -> None:
        """Resolver revalidates prebuilt graph relationship kinds."""

        result = resolve_probabilistic_entity(
            graph=_prebuilt_graph(
                replace(
                    _relationship(source="seed-a", target="linked-b"),
                    relationship_kind=cast("Any", "bad-kind"),
                )
            ),
            seeds=(_seed(address="seed-a"),),
            config=_resolution_config(),
        )

        self.assert_abstains(
            result,
            AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
            as_of_slot=20,
        )

    def assert_abstains(
        self,
        result: object,
        reason: AbstainReason,
        *,
        as_of_slot: int,
    ) -> None:
        self.assertIsInstance(result, AbstainResult)
        result = cast("AbstainResult", result)
        self.assertEqual(result.reason, reason)
        self.assertEqual(result.as_of_slot, as_of_slot)


class AddressRoleClassificationTests(unittest.TestCase):
    """Tests for direct address-role classification evidence."""

    def test_classifies_active_roles_above_threshold(self) -> None:
        """Role classifier preserves only active evidence above threshold."""

        result = classify_address_roles(
            evidence=(
                _role_evidence(address="creator-a", role=AddressRole.CREATOR),
                _role_evidence(
                    address="weak",
                    role=AddressRole.FUNDER,
                    probability=100_000,
                ),
                _role_evidence(
                    address="expired",
                    role=AddressRole.DUMPER,
                    valid_to_slot=20,
                ),
            ),
            as_of_slot=Slot(20),
            classifier_version="roles-v1",
            min_role_probability_ppm=700_000,
        )

        self.assertIsInstance(result, AddressRoleSnapshot)
        snapshot = cast("AddressRoleSnapshot", result)
        self.assertEqual(snapshot.source_evidence_count, 3)
        self.assertEqual(snapshot.active_evidence_count, 2)
        self.assertEqual(snapshot.skipped_inactive_evidence_count, 1)
        self.assertEqual(len(snapshot.assignments), 1)
        self.assertEqual(snapshot.assignments[0].as_of_slot, Slot(20))
        self.assertEqual(snapshot.assignments[0].role, AddressRole.CREATOR)

    def test_no_role_above_threshold_returns_explicit_empty_snapshot(self) -> None:
        """Weak active role evidence is not a false positive."""

        result = classify_address_roles(
            evidence=(
                _role_evidence(
                    address="weak",
                    role=AddressRole.FUNDER,
                    probability=100_000,
                ),
            ),
            as_of_slot=Slot(20),
            classifier_version="roles-v1",
            min_role_probability_ppm=700_000,
        )

        self.assertIsInstance(result, AddressRoleSnapshot)
        snapshot = cast("AddressRoleSnapshot", result)
        self.assertEqual(snapshot.assignments, ())
        self.assertEqual(snapshot.reason_codes, ("no_role_evidence_above_threshold",))

    def test_future_role_evidence_abstains(self) -> None:
        """Role evidence cannot be published before its valid_from_slot."""

        result = classify_address_roles(
            evidence=(
                _role_evidence(
                    address="future",
                    role=AddressRole.FUNDER,
                    valid_from_slot=21,
                ),
            ),
            as_of_slot=Slot(20),
            classifier_version="roles-v1",
            min_role_probability_ppm=700_000,
        )

        self.assert_abstains(result, AbstainReason.STALE_STATE, as_of_slot=20)

    def test_missing_role_model_version_abstains(self) -> None:
        """Role evidence must preserve its model version."""

        result = classify_address_roles(
            evidence=(
                replace(
                    _role_evidence(address="creator-a", role=AddressRole.CREATOR),
                    model_version="",
                ),
            ),
            as_of_slot=Slot(20),
            classifier_version="roles-v1",
            min_role_probability_ppm=700_000,
        )

        self.assert_abstains(result, AbstainReason.DECODER_MISMATCH, as_of_slot=20)

    def test_invalid_role_abstains(self) -> None:
        """Role values must come from the pinned enum."""

        result = classify_address_roles(
            evidence=(
                _role_evidence(
                    address="creator-a",
                    role=cast("Any", "bad-role"),
                ),
            ),
            as_of_slot=Slot(20),
            classifier_version="roles-v1",
            min_role_probability_ppm=700_000,
        )

        self.assert_abstains(
            result,
            AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
            as_of_slot=20,
        )

    def test_float_role_probability_abstains(self) -> None:
        """Runtime validators reject float role probabilities."""

        result = classify_address_roles(
            evidence=(
                _role_evidence(
                    address="creator-a",
                    role=AddressRole.CREATOR,
                    probability=cast("Any", 0.5),
                ),
            ),
            as_of_slot=Slot(20),
            classifier_version="roles-v1",
            min_role_probability_ppm=700_000,
        )

        self.assert_abstains(
            result,
            AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
            as_of_slot=20,
        )

    def test_entity_resolution_module_stays_pure_and_integer_only(self) -> None:
        """Resolver contracts must not grow adapters, signers, or floats."""

        source = ENTITY_MODULE.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(ENTITY_MODULE))
        violations = [
            imported_name
            for imported_name in _imported_module_names(tree)
            if imported_name.startswith(FORBIDDEN_IMPORT_PREFIXES)
        ]

        self.assertEqual(violations, [])
        for token in _forbidden_source_tokens():
            with self.subTest(token=token):
                self.assertNotIn(token, source)

    def assert_abstains(
        self,
        result: object,
        reason: AbstainReason,
        *,
        as_of_slot: int,
    ) -> None:
        self.assertIsInstance(result, AbstainResult)
        result = cast("AbstainResult", result)
        self.assertEqual(result.reason, reason)
        self.assertEqual(result.as_of_slot, as_of_slot)


def _graph(
    *edges: AddressEvidenceEdge,
    as_of_slot: int = 20,
) -> AddressGraphSnapshot:
    result = build_address_graph_snapshot(
        edges=edges,
        as_of_slot=Slot(as_of_slot),
        snapshot_version="graph-v1",
    )
    if not isinstance(result, AddressGraphSnapshot):
        raise TypeError(result)
    return result


def _prebuilt_graph(
    relationship: AddressRelationshipView,
    *,
    active_edge_count: int = 1,
) -> AddressGraphSnapshot:
    return AddressGraphSnapshot(
        as_of_slot=Slot(20),
        snapshot_version="graph-v1",
        relationships=(relationship,),
        source_edge_count=1,
        active_edge_count=active_edge_count,
        skipped_inactive_edge_count=0,
    )


def _relationship(**overrides: object) -> AddressRelationshipView:
    return AddressRelationshipView(
        as_of_slot=Slot(_override_int(overrides, "as_of_slot", 20)),
        source_address=_override_str(overrides, "source", "seed-a"),
        target_address=_override_str(overrides, "target", "linked-b"),
        relationship_kind=cast(
            "AddressRelationshipKind",
            overrides.get("kind", AddressRelationshipKind.DIRECT_NATIVE_TRANSFER),
        ),
        observed_slot=Slot(_override_int(overrides, "observed_slot", 20)),
        age_slots=_override_int(overrides, "age_slots", 0),
        raw_confidence_ppm=_override_int(overrides, "raw_confidence", 1_000_000),
        decayed_confidence_ppm=_override_int(
            overrides,
            "decayed_confidence",
            1_000_000,
        ),
        same_controller_probability_ppm=_override_int(
            overrides,
            "same_controller",
            900_000,
        ),
        cooperating_probability_ppm=_override_int(overrides, "cooperating", 0),
        shared_service_probability_ppm=_override_int(overrides, "shared_service", 0),
        incidental_interaction_probability_ppm=0,
        evidence_ids=("edge-evidence",),
        model_version="graph-model-v1",
    )


def _edge(**overrides: object) -> AddressEvidenceEdge:
    return AddressEvidenceEdge(
        as_of_slot=Slot(_override_int(overrides, "edge_as_of", 20)),
        source_address=_override_str(overrides, "source", "seed-a"),
        target_address=_override_str(overrides, "target", "linked-b"),
        relationship_kind=cast(
            "AddressRelationshipKind",
            overrides.get("kind", AddressRelationshipKind.DIRECT_NATIVE_TRANSFER),
        ),
        observed_slot=Slot(_override_int(overrides, "observed_slot", 20)),
        valid_from_slot=Slot(_override_int(overrides, "valid_from_slot", 0)),
        valid_to_slot=None,
        confidence_ppm=_override_int(overrides, "confidence", 1_000_000),
        same_controller_probability_ppm=_override_int(
            overrides,
            "same_controller",
            900_000,
        ),
        cooperating_probability_ppm=_override_int(overrides, "cooperating", 0),
        shared_service_probability_ppm=_override_int(overrides, "shared_service", 0),
        incidental_interaction_probability_ppm=0,
        half_life_slots=10,
        evidence_ids=("edge-evidence",),
        model_version="graph-model-v1",
    )


def _seed(**overrides: object) -> EntitySeedEvidence:
    valid_to_slot = overrides.get("valid_to_slot")
    return EntitySeedEvidence(
        as_of_slot=Slot(_override_int(overrides, "seed_as_of", 20)),
        entity_id=_override_str(overrides, "entity_id", "entity-1"),
        address=_override_str(overrides, "address", "seed-a"),
        valid_from_slot=Slot(_override_int(overrides, "valid_from_slot", 0)),
        valid_to_slot=Slot(valid_to_slot) if valid_to_slot is not None else None,
        same_controller_probability_ppm=_override_int(
            overrides,
            "same_controller",
            950_000,
        ),
        cooperating_probability_ppm=_override_int(overrides, "cooperating", 0),
        evidence_ids=("seed-evidence",),
        model_version="seed-model-v1",
    )


def _resolution_config() -> EntityResolutionConfig:
    return EntityResolutionConfig(
        as_of_slot=Slot(20),
        entity_id="entity-1",
        resolver_version="resolver-v1",
        min_membership_probability_ppm=700_000,
    )


def _role_evidence(**overrides: object) -> AddressRoleEvidence:
    valid_to_slot = overrides.get("valid_to_slot")
    return AddressRoleEvidence(
        as_of_slot=Slot(_override_int(overrides, "evidence_as_of", 20)),
        address=_override_str(overrides, "address", "creator-a"),
        role=cast("AddressRole", overrides.get("role", AddressRole.CREATOR)),
        valid_from_slot=Slot(_override_int(overrides, "valid_from_slot", 0)),
        valid_to_slot=Slot(valid_to_slot) if valid_to_slot is not None else None,
        role_probability_ppm=_override_int(overrides, "probability", 900_000),
        evidence_ids=("role-evidence",),
        model_version="role-model-v1",
    )


def _override_int(overrides: dict[str, object], key: str, default: int) -> int:
    return cast("int", overrides.get(key, default))


def _override_str(overrides: dict[str, object], key: str, default: str) -> str:
    return cast("str", overrides.get(key, default))


def _imported_module_names(tree: ast.AST) -> list[str]:
    names: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            names.append(node.module)
    return names


def _forbidden_source_tokens() -> tuple[str, ...]:
    return (
        "Key" + "pair",
        "Wal" + "let",
        "PRIVATE" + "_KEY",
        "send" + "_transaction",
        "send" + "_raw_transaction",
        "float(",
    )


if __name__ == "__main__":
    unittest.main()
