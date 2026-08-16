"""Point-in-time address graph tests."""

import ast
import unittest
from dataclasses import replace
from pathlib import Path
from typing import Any, cast

from rugbot.domain.amounts import Slot
from rugbot.domain.decisions import AbstainReason, AbstainResult
from rugbot.graph.point_in_time import (
    AddressEvidenceEdge,
    AddressGraphSnapshot,
    AddressRelationshipKind,
    build_address_graph_snapshot,
    direct_relationships_for_address,
)

GRAPH_MODULE = Path("src/rugbot/graph/point_in_time.py")
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


class PointInTimeAddressGraphTests(unittest.TestCase):
    """Tests for temporal direct-edge graph snapshots."""

    def test_builds_active_edges_with_integer_time_decay(self) -> None:
        """Active relationships are decayed by edge age and confidence."""

        snapshot = build_address_graph_snapshot(
            edges=(
                _edge(source="a", target="b", observed_slot=10, confidence=800_000),
                _edge(source="inactive", target="b", valid_to_slot=20),
            ),
            as_of_slot=Slot(20),
            snapshot_version="graph-v1",
        )

        self.assertIsInstance(snapshot, AddressGraphSnapshot)
        snapshot = cast("AddressGraphSnapshot", snapshot)
        self.assertEqual(snapshot.source_edge_count, 2)
        self.assertEqual(snapshot.active_edge_count, 1)
        self.assertEqual(snapshot.skipped_inactive_edge_count, 1)
        relationship = snapshot.relationships[0]
        self.assertEqual(relationship.as_of_slot, Slot(20))
        self.assertEqual(relationship.age_slots, 10)
        self.assertEqual(relationship.raw_confidence_ppm, 800_000)
        self.assertEqual(relationship.decayed_confidence_ppm, 400_000)
        self.assertEqual(relationship.same_controller_probability_ppm, 360_000)

    def test_partial_half_life_decay_is_integer_and_conservative(self) -> None:
        """Partial half-life age applies deterministic integer decay."""

        snapshot = build_address_graph_snapshot(
            edges=(
                _edge(
                    source="a",
                    target="b",
                    observed_slot=15,
                    confidence=800_000,
                    half_life_slots=10,
                ),
            ),
            as_of_slot=Slot(20),
            snapshot_version="graph-v1",
        )

        self.assertIsInstance(snapshot, AddressGraphSnapshot)
        snapshot = cast("AddressGraphSnapshot", snapshot)
        self.assertEqual(snapshot.relationships[0].decayed_confidence_ppm, 600_000)

    def test_direct_lookup_does_not_apply_transitive_closure(self) -> None:
        """The graph exposes direct evidence only, not inferred clusters."""

        snapshot = build_address_graph_snapshot(
            edges=(
                _edge(source="a", target="b", observed_slot=10),
                _edge(source="b", target="c", observed_slot=10),
            ),
            as_of_slot=Slot(20),
            snapshot_version="graph-v1",
        )

        self.assertIsInstance(snapshot, AddressGraphSnapshot)
        snapshot = cast("AddressGraphSnapshot", snapshot)
        direct = direct_relationships_for_address(snapshot=snapshot, address="a")

        self.assertEqual(len(direct), 1)
        self.assertEqual(direct[0].target_address, "b")

    def test_shared_service_probability_stays_separate(self) -> None:
        """Shared-service evidence does not imply same-controller evidence."""

        snapshot = build_address_graph_snapshot(
            edges=(
                _edge(
                    source="a",
                    target="b",
                    observed_slot=20,
                    kind=AddressRelationshipKind.SHARED_SERVICE,
                    same_controller=0,
                    shared_service=950_000,
                    confidence=900_000,
                ),
            ),
            as_of_slot=Slot(20),
            snapshot_version="graph-v1",
        )

        self.assertIsInstance(snapshot, AddressGraphSnapshot)
        snapshot = cast("AddressGraphSnapshot", snapshot)
        relationship = snapshot.relationships[0]
        self.assertEqual(relationship.same_controller_probability_ppm, 0)
        self.assertEqual(relationship.shared_service_probability_ppm, 855_000)

    def test_future_edge_abstains(self) -> None:
        """Evidence newer than the graph snapshot cannot be used."""

        result = build_address_graph_snapshot(
            edges=(_edge(source="a", target="b", observed_slot=21, edge_as_of=21),),
            as_of_slot=Slot(20),
            snapshot_version="graph-v1",
        )

        self.assert_abstains(result, AbstainReason.STALE_STATE, as_of_slot=20)

    def test_future_valid_from_slot_abstains(self) -> None:
        """Future-valid graph evidence is stale, not inactive history."""

        result = build_address_graph_snapshot(
            edges=(_edge(source="a", target="b", valid_from_slot=21),),
            as_of_slot=Slot(20),
            snapshot_version="graph-v1",
        )

        self.assert_abstains(result, AbstainReason.STALE_STATE, as_of_slot=20)

    def test_negative_snapshot_slot_abstains(self) -> None:
        """Snapshot slots must be non-negative integers."""

        result = build_address_graph_snapshot(
            edges=(),
            as_of_slot=Slot(-1),
            snapshot_version="graph-v1",
        )

        self.assert_abstains(
            result,
            AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
            as_of_slot=-1,
        )

    def test_float_snapshot_slot_abstains(self) -> None:
        """Runtime validators reject float snapshot slots."""

        result = build_address_graph_snapshot(
            edges=(),
            as_of_slot=cast("Any", 20.5),
            snapshot_version="graph-v1",
        )

        self.assert_abstains(
            result,
            AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
            as_of_slot=-1,
        )

    def test_missing_snapshot_version_abstains(self) -> None:
        """Graph snapshots must be versioned."""

        result = build_address_graph_snapshot(
            edges=(),
            as_of_slot=Slot(20),
            snapshot_version="",
        )

        self.assert_abstains(result, AbstainReason.DECODER_MISMATCH, as_of_slot=20)

    def test_invalid_validity_interval_abstains(self) -> None:
        """Edge validity intervals must be coherent."""

        result = build_address_graph_snapshot(
            edges=(
                _edge(source="a", target="b", valid_from_slot=10, valid_to_slot=10),
            ),
            as_of_slot=Slot(20),
            snapshot_version="graph-v1",
        )

        self.assert_abstains(
            result,
            AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
            as_of_slot=20,
        )

    def test_self_edge_abstains(self) -> None:
        """The direct graph does not need self edges."""

        result = build_address_graph_snapshot(
            edges=(_edge(source="a", target="a"),),
            as_of_slot=Slot(20),
            snapshot_version="graph-v1",
        )

        self.assert_abstains(
            result,
            AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
            as_of_slot=20,
        )

    def test_missing_address_abstains(self) -> None:
        """Graph edges require both endpoint addresses."""

        result = build_address_graph_snapshot(
            edges=(_edge(source="", target="b"),),
            as_of_slot=Slot(20),
            snapshot_version="graph-v1",
        )

        self.assert_abstains(result, AbstainReason.MISSING_FEATURE, as_of_slot=20)

    def test_missing_target_address_abstains(self) -> None:
        """Graph edges require target endpoint addresses."""

        result = build_address_graph_snapshot(
            edges=(_edge(source="a", target=""),),
            as_of_slot=Slot(20),
            snapshot_version="graph-v1",
        )

        self.assert_abstains(result, AbstainReason.MISSING_FEATURE, as_of_slot=20)

    def test_invalid_relationship_kind_abstains(self) -> None:
        """Relationship kind must be one of the pinned enum values."""

        result = build_address_graph_snapshot(
            edges=(_edge(source="a", target="b", kind=cast("Any", "bad-kind")),),
            as_of_slot=Slot(20),
            snapshot_version="graph-v1",
        )

        self.assert_abstains(
            result,
            AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
            as_of_slot=20,
        )

    def test_out_of_range_probability_abstains(self) -> None:
        """Probability ppm fields must be within range."""

        result = build_address_graph_snapshot(
            edges=(_edge(source="a", target="b", confidence=1_000_001),),
            as_of_slot=Slot(20),
            snapshot_version="graph-v1",
        )

        self.assert_abstains(
            result,
            AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
            as_of_slot=20,
        )

    def test_float_slot_field_abstains(self) -> None:
        """Runtime validators reject float edge slot fields."""

        result = build_address_graph_snapshot(
            edges=(_edge(source="a", target="b", observed_slot=cast("Any", 10.5)),),
            as_of_slot=Slot(20),
            snapshot_version="graph-v1",
        )

        self.assert_abstains(
            result,
            AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
            as_of_slot=20,
        )

    def test_float_probability_abstains(self) -> None:
        """Runtime validators reject float probability values."""

        result = build_address_graph_snapshot(
            edges=(
                _edge(
                    source="a",
                    target="b",
                    observed_slot=10,
                    same_controller=cast("Any", 0.5),
                ),
            ),
            as_of_slot=Slot(20),
            snapshot_version="graph-v1",
        )

        self.assert_abstains(
            result,
            AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
            as_of_slot=20,
        )

    def test_missing_model_version_abstains(self) -> None:
        """Graph evidence must preserve model version provenance."""

        result = build_address_graph_snapshot(
            edges=(
                replace(
                    _edge(source="a", target="b", observed_slot=10), model_version=""
                ),
            ),
            as_of_slot=Slot(20),
            snapshot_version="graph-v1",
        )

        self.assert_abstains(result, AbstainReason.DECODER_MISMATCH, as_of_slot=20)

    def test_missing_evidence_ids_abstains(self) -> None:
        """Graph evidence must preserve source evidence IDs."""

        result = build_address_graph_snapshot(
            edges=(
                replace(
                    _edge(source="a", target="b", observed_slot=10), evidence_ids=()
                ),
            ),
            as_of_slot=Slot(20),
            snapshot_version="graph-v1",
        )

        self.assert_abstains(result, AbstainReason.MISSING_FEATURE, as_of_slot=20)

    def test_invalid_half_life_abstains(self) -> None:
        """Edges must declare a positive integer decay half-life."""

        result = build_address_graph_snapshot(
            edges=(
                replace(
                    _edge(source="a", target="b", observed_slot=10),
                    half_life_slots=0,
                ),
            ),
            as_of_slot=Slot(20),
            snapshot_version="graph-v1",
        )

        self.assert_abstains(
            result,
            AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
            as_of_slot=20,
        )

    def test_graph_module_stays_pure_and_integer_only(self) -> None:
        """Graph contracts must not grow adapters, signers, or floats."""

        source = GRAPH_MODULE.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(GRAPH_MODULE))
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


def _edge(**overrides: object) -> AddressEvidenceEdge:
    valid_to_slot = overrides.get("valid_to_slot")
    return AddressEvidenceEdge(
        as_of_slot=Slot(_override_int(overrides, "edge_as_of", 20)),
        source_address=_override_str(overrides, "source", "a"),
        target_address=_override_str(overrides, "target", "b"),
        relationship_kind=cast(
            "AddressRelationshipKind",
            overrides.get("kind", AddressRelationshipKind.DIRECT_NATIVE_TRANSFER),
        ),
        observed_slot=Slot(_override_int(overrides, "observed_slot", 10)),
        valid_from_slot=Slot(_override_int(overrides, "valid_from_slot", 0)),
        valid_to_slot=Slot(valid_to_slot) if valid_to_slot is not None else None,
        confidence_ppm=_override_int(overrides, "confidence", 1_000_000),
        same_controller_probability_ppm=_override_int(
            overrides,
            "same_controller",
            900_000,
        ),
        cooperating_probability_ppm=100_000,
        shared_service_probability_ppm=_override_int(overrides, "shared_service", 0),
        incidental_interaction_probability_ppm=50_000,
        half_life_slots=_override_int(overrides, "half_life_slots", 10),
        evidence_ids=("evidence-1",),
        model_version="graph-model-v1",
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
