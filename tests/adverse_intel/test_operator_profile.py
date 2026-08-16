"""Operator profile, campaign, and regime contract tests."""

import ast
import unittest
from dataclasses import replace
from pathlib import Path
from typing import Any, cast

from rugbot.domain.amounts import Slot
from rugbot.domain.decisions import AbstainReason, AbstainResult
from rugbot.graph.entity_resolution import (
    AddressRole,
    AddressRoleAssignment,
    AddressRoleSnapshot,
    EntityMembership,
    ProbabilisticEntity,
)
from rugbot.graph.operator_profile import (
    CampaignEvidence,
    OperatorProfileBuildConfig,
    OperatorProfileSnapshot,
    OperatorRegimeKind,
    RegimeEvidence,
    build_operator_profile_snapshot,
)

PROFILE_MODULE = Path("src/rugbot/graph/operator_profile.py")
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


class OperatorProfileTests(unittest.TestCase):
    """Tests for pure operator profile construction."""

    def test_builds_profile_with_active_campaigns_and_regimes(self) -> None:
        """Profile keeps active evidence above thresholds and picks top regime."""

        result = build_operator_profile_snapshot(
            entity=_entity(
                memberships=(
                    _membership(address="creator-a"),
                    _membership(
                        address="weak-service",
                        same_controller=0,
                        cooperating=100_000,
                        shared_service=900_000,
                    ),
                )
            ),
            roles=_roles(
                _role(address="creator-a", role=AddressRole.CREATOR),
                _role(
                    address="creator-a",
                    role=AddressRole.FUNDER,
                    probability=100_000,
                ),
            ),
            campaigns=(
                _campaign(campaign_id="campaign-a", probability=900_000),
                _campaign(campaign_id="expired", valid_to_slot=20),
                _campaign(campaign_id="weak", probability=100_000),
            ),
            regimes=(
                _regime(
                    campaign_id="campaign-a",
                    regime_id="regime-a",
                    regime_kind=OperatorRegimeKind.FAKE_PUMP_THEN_FULL_DUMP,
                    probability=850_000,
                    support=5,
                ),
                _regime(
                    campaign_id="campaign-a",
                    regime_id="unsupported",
                    probability=900_000,
                    support=1,
                ),
                _regime(
                    campaign_id="campaign-a",
                    regime_id="weak",
                    probability=100_000,
                    support=10,
                ),
            ),
            config=_config(),
        )

        self.assertIsInstance(result, OperatorProfileSnapshot)
        profile = cast("OperatorProfileSnapshot", result)
        self.assertEqual(profile.as_of_slot, Slot(20))
        self.assertEqual(profile.profile_version, "profile-v1")
        self.assertEqual(profile.entity_resolver_version, "resolver-v1")
        self.assertEqual(profile.role_classifier_version, "roles-v1")
        self.assertEqual(
            tuple(address.address for address in profile.addresses), ("creator-a",)
        )
        self.assertEqual(
            profile.addresses[0].probable_roles[0].role, AddressRole.CREATOR
        )
        self.assertEqual(
            tuple(campaign.campaign_id for campaign in profile.campaigns),
            ("campaign-a",),
        )
        self.assertEqual(
            tuple(regime.regime_id for regime in profile.regimes), ("regime-a",)
        )
        self.assertEqual(profile.current_active_regime_id, "regime-a")
        self.assertEqual(profile.source_membership_count, 2)
        self.assertEqual(profile.active_address_count, 1)
        self.assertEqual(profile.source_campaign_count, 3)
        self.assertEqual(profile.active_campaign_count, 1)

    def test_regime_without_active_campaign_is_skipped(self) -> None:
        """Regimes only materialize under active campaigns."""

        result = build_operator_profile_snapshot(
            entity=_entity(),
            roles=_roles(),
            campaigns=(_campaign(campaign_id="expired", valid_to_slot=20),),
            regimes=(
                _regime(
                    campaign_id="expired",
                    regime_id="orphan-regime",
                    probability=900_000,
                    support=10,
                ),
            ),
            config=_config(),
        )

        self.assertIsInstance(result, OperatorProfileSnapshot)
        profile = cast("OperatorProfileSnapshot", result)
        self.assertEqual(profile.campaigns, ())
        self.assertEqual(profile.regimes, ())
        self.assertIsNone(profile.current_active_regime_id)

    def test_missing_profile_version_abstains(self) -> None:
        """Profile snapshots must be versioned."""

        result = build_operator_profile_snapshot(
            entity=_entity(),
            roles=_roles(),
            campaigns=(),
            regimes=(),
            config=replace(_config(), profile_version=""),
        )

        self.assert_abstains(result, AbstainReason.DECODER_MISMATCH, as_of_slot=20)

    def test_entity_slot_mismatch_abstains(self) -> None:
        """Profile cannot mix entity snapshots from another slot."""

        result = build_operator_profile_snapshot(
            entity=_entity(as_of_slot=19),
            roles=_roles(),
            campaigns=(),
            regimes=(),
            config=_config(),
        )

        self.assert_abstains(result, AbstainReason.STALE_STATE, as_of_slot=20)

    def test_invalid_entity_counts_abstain(self) -> None:
        """Profile builder revalidates prebuilt entity counts."""

        result = build_operator_profile_snapshot(
            entity=replace(_entity(), active_seed_count=2, source_seed_count=1),
            roles=_roles(),
            campaigns=(),
            regimes=(),
            config=_config(),
        )

        self.assert_abstains(
            result,
            AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
            as_of_slot=20,
        )

    def test_weak_memberships_below_threshold_abstain(self) -> None:
        """Profiles require at least one membership above threshold."""

        result = build_operator_profile_snapshot(
            entity=_entity(
                memberships=(
                    _membership(
                        address="weak-a",
                        same_controller=100_000,
                        cooperating=100_000,
                    ),
                )
            ),
            roles=_roles(),
            campaigns=(),
            regimes=(),
            config=_config(),
        )

        self.assert_abstains(result, AbstainReason.MISSING_FEATURE, as_of_slot=20)

    def test_float_membership_probability_abstains(self) -> None:
        """Runtime validators reject float membership probabilities."""

        result = build_operator_profile_snapshot(
            entity=_entity(
                memberships=(
                    _membership(
                        address="creator-a",
                        same_controller=cast("Any", 0.5),
                    ),
                )
            ),
            roles=_roles(),
            campaigns=(),
            regimes=(),
            config=_config(),
        )

        self.assert_abstains(
            result,
            AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
            as_of_slot=20,
        )

    def test_mutable_membership_evidence_ids_abstain(self) -> None:
        """Entity membership provenance must be immutable tuple data."""

        result = build_operator_profile_snapshot(
            entity=_entity(
                memberships=(
                    replace(
                        _membership(address="creator-a"),
                        evidence_ids=cast("Any", ["mutable-evidence"]),
                    ),
                )
            ),
            roles=_roles(),
            campaigns=(),
            regimes=(),
            config=_config(),
        )

        self.assert_abstains(result, AbstainReason.MISSING_FEATURE, as_of_slot=20)

    def test_future_campaign_abstains(self) -> None:
        """Future campaign evidence cannot enter a historical profile."""

        result = build_operator_profile_snapshot(
            entity=_entity(),
            roles=_roles(),
            campaigns=(_campaign(campaign_id="future", valid_from_slot=21),),
            regimes=(),
            config=_config(),
        )

        self.assert_abstains(result, AbstainReason.STALE_STATE, as_of_slot=20)

    def test_stale_campaign_snapshot_abstains(self) -> None:
        """Older campaign snapshots cannot be re-stamped as current."""

        result = build_operator_profile_snapshot(
            entity=_entity(),
            roles=_roles(),
            campaigns=(_campaign(campaign_id="stale", as_of_slot=19),),
            regimes=(),
            config=_config(),
        )

        self.assert_abstains(result, AbstainReason.STALE_STATE, as_of_slot=20)

    def test_float_campaign_probability_abstains(self) -> None:
        """Runtime validators reject float campaign probabilities."""

        result = build_operator_profile_snapshot(
            entity=_entity(),
            roles=_roles(),
            campaigns=(
                _campaign(
                    campaign_id="campaign-a",
                    probability=cast("Any", 0.5),
                ),
            ),
            regimes=(),
            config=_config(),
        )

        self.assert_abstains(
            result,
            AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
            as_of_slot=20,
        )

    def test_mutable_campaign_evidence_ids_abstain(self) -> None:
        """Campaign provenance must be immutable tuple data."""

        result = build_operator_profile_snapshot(
            entity=_entity(),
            roles=_roles(),
            campaigns=(
                replace(
                    _campaign(campaign_id="campaign-a"),
                    evidence_ids=cast("Any", ["mutable-evidence"]),
                ),
            ),
            regimes=(),
            config=_config(),
        )

        self.assert_abstains(result, AbstainReason.MISSING_FEATURE, as_of_slot=20)

    def test_campaign_entity_mismatch_abstains(self) -> None:
        """Campaign evidence for another entity is rejected."""

        result = build_operator_profile_snapshot(
            entity=_entity(),
            roles=_roles(),
            campaigns=(_campaign(campaign_id="campaign-a", entity_id="entity-2"),),
            regimes=(),
            config=_config(),
        )

        self.assert_abstains(
            result,
            AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
            as_of_slot=20,
        )

    def test_future_regime_abstains(self) -> None:
        """Future regime evidence cannot enter a historical profile."""

        result = build_operator_profile_snapshot(
            entity=_entity(),
            roles=_roles(),
            campaigns=(),
            regimes=(_regime(regime_id="future", valid_from_slot=21),),
            config=_config(),
        )

        self.assert_abstains(result, AbstainReason.STALE_STATE, as_of_slot=20)

    def test_stale_regime_snapshot_abstains(self) -> None:
        """Older regime snapshots cannot be re-stamped as current."""

        result = build_operator_profile_snapshot(
            entity=_entity(),
            roles=_roles(),
            campaigns=(),
            regimes=(_regime(regime_id="stale", as_of_slot=19),),
            config=_config(),
        )

        self.assert_abstains(result, AbstainReason.STALE_STATE, as_of_slot=20)

    def test_float_regime_probability_abstains(self) -> None:
        """Runtime validators reject float regime probabilities."""

        result = build_operator_profile_snapshot(
            entity=_entity(),
            roles=_roles(),
            campaigns=(),
            regimes=(
                _regime(
                    regime_id="regime-a",
                    probability=cast("Any", 0.5),
                ),
            ),
            config=_config(),
        )

        self.assert_abstains(
            result,
            AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
            as_of_slot=20,
        )

    def test_mutable_regime_evidence_ids_abstain(self) -> None:
        """Regime provenance must be immutable tuple data."""

        result = build_operator_profile_snapshot(
            entity=_entity(),
            roles=_roles(),
            campaigns=(),
            regimes=(
                replace(
                    _regime(regime_id="regime-a"),
                    evidence_ids=cast("Any", ["mutable-evidence"]),
                ),
            ),
            config=_config(),
        )

        self.assert_abstains(result, AbstainReason.MISSING_FEATURE, as_of_slot=20)

    def test_invalid_regime_kind_abstains(self) -> None:
        """Regime kind must be one of the pinned enum values."""

        result = build_operator_profile_snapshot(
            entity=_entity(),
            roles=_roles(),
            campaigns=(),
            regimes=(_regime(regime_id="bad", regime_kind=cast("Any", "bad")),),
            config=_config(),
        )

        self.assert_abstains(
            result,
            AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
            as_of_slot=20,
        )

    def test_malformed_role_assignment_abstains(self) -> None:
        """Profile builder revalidates role assignments before use."""

        result = build_operator_profile_snapshot(
            entity=_entity(),
            roles=_roles(
                replace(
                    _role(address="creator-a", role=AddressRole.CREATOR),
                    role=cast("Any", "bad-role"),
                )
            ),
            campaigns=(),
            regimes=(),
            config=_config(),
        )

        self.assert_abstains(
            result,
            AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
            as_of_slot=20,
        )

    def test_mutable_role_assignment_evidence_ids_abstain(self) -> None:
        """Role assignment provenance must be immutable tuple data."""

        result = build_operator_profile_snapshot(
            entity=_entity(),
            roles=_roles(
                replace(
                    _role(address="creator-a", role=AddressRole.CREATOR),
                    evidence_ids=cast("Any", ["mutable-evidence"]),
                )
            ),
            campaigns=(),
            regimes=(),
            config=_config(),
        )

        self.assert_abstains(result, AbstainReason.MISSING_FEATURE, as_of_slot=20)

    def test_invalid_role_counts_abstain(self) -> None:
        """Profile builder revalidates prebuilt role snapshot counts."""

        result = build_operator_profile_snapshot(
            entity=_entity(),
            roles=replace(
                _roles(_role(address="creator-a", role=AddressRole.CREATOR)),
                active_evidence_count=0,
            ),
            campaigns=(),
            regimes=(),
            config=_config(),
        )

        self.assert_abstains(
            result,
            AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
            as_of_slot=20,
        )

    def test_operator_profile_module_stays_pure_and_integer_only(self) -> None:
        """Profile contracts must not grow adapters, signers, or floats."""

        source = PROFILE_MODULE.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(PROFILE_MODULE))
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


def _entity(
    *,
    as_of_slot: int = 20,
    memberships: tuple[EntityMembership, ...] | None = None,
) -> ProbabilisticEntity:
    selected_memberships = memberships or (_membership(address="creator-a"),)
    return ProbabilisticEntity(
        as_of_slot=Slot(as_of_slot),
        entity_id="entity-1",
        resolver_version="resolver-v1",
        graph_snapshot_version="graph-v1",
        memberships=selected_memberships,
        source_seed_count=1,
        active_seed_count=1,
        direct_relationship_count=0,
        reason_codes=("test-entity",),
    )


def _membership(**overrides: object) -> EntityMembership:
    return EntityMembership(
        as_of_slot=Slot(_override_int(overrides, "as_of_slot", 20)),
        entity_id=_override_str(overrides, "entity_id", "entity-1"),
        address=_override_str(overrides, "address", "creator-a"),
        same_controller_probability_ppm=_override_int(
            overrides,
            "same_controller",
            900_000,
        ),
        cooperating_probability_ppm=_override_int(overrides, "cooperating", 0),
        shared_service_probability_ppm=_override_int(overrides, "shared_service", 0),
        incidental_interaction_probability_ppm=0,
        evidence_ids=("membership-evidence",),
        model_version="membership-model-v1",
        source="seed",
    )


def _roles(*assignments: AddressRoleAssignment) -> AddressRoleSnapshot:
    return AddressRoleSnapshot(
        as_of_slot=Slot(20),
        classifier_version="roles-v1",
        assignments=assignments,
        source_evidence_count=len(assignments),
        active_evidence_count=len(assignments),
        skipped_inactive_evidence_count=0,
        reason_codes=("test-roles",),
    )


def _role(**overrides: object) -> AddressRoleAssignment:
    return AddressRoleAssignment(
        as_of_slot=Slot(_override_int(overrides, "as_of_slot", 20)),
        address=_override_str(overrides, "address", "creator-a"),
        role=cast("AddressRole", overrides.get("role", AddressRole.CREATOR)),
        role_probability_ppm=_override_int(overrides, "probability", 900_000),
        evidence_ids=("role-evidence",),
        model_version="role-model-v1",
    )


def _campaign(**overrides: object) -> CampaignEvidence:
    valid_to_slot = overrides.get("valid_to_slot")
    return CampaignEvidence(
        as_of_slot=Slot(_override_int(overrides, "as_of_slot", 20)),
        entity_id=_override_str(overrides, "entity_id", "entity-1"),
        campaign_id=_override_str(overrides, "campaign_id", "campaign-a"),
        valid_from_slot=Slot(_override_int(overrides, "valid_from_slot", 0)),
        valid_to_slot=Slot(valid_to_slot) if valid_to_slot is not None else None,
        campaign_probability_ppm=_override_int(overrides, "probability", 900_000),
        launch_count=_override_int(overrides, "launch_count", 5),
        evidence_ids=("campaign-evidence",),
        model_version="campaign-model-v1",
    )


def _regime(**overrides: object) -> RegimeEvidence:
    valid_to_slot = overrides.get("valid_to_slot")
    return RegimeEvidence(
        as_of_slot=Slot(_override_int(overrides, "as_of_slot", 20)),
        entity_id=_override_str(overrides, "entity_id", "entity-1"),
        campaign_id=_override_str(overrides, "campaign_id", "campaign-a"),
        regime_id=_override_str(overrides, "regime_id", "regime-a"),
        regime_kind=cast(
            "OperatorRegimeKind",
            overrides.get("regime_kind", OperatorRegimeKind.FAKE_PUMP_THEN_FULL_DUMP),
        ),
        valid_from_slot=Slot(_override_int(overrides, "valid_from_slot", 0)),
        valid_to_slot=Slot(valid_to_slot) if valid_to_slot is not None else None,
        regime_probability_ppm=_override_int(overrides, "probability", 850_000),
        support_launch_count=_override_int(overrides, "support", 5),
        evidence_ids=("regime-evidence",),
        model_version="regime-model-v1",
    )


def _config() -> OperatorProfileBuildConfig:
    return OperatorProfileBuildConfig(
        as_of_slot=Slot(20),
        entity_id="entity-1",
        profile_version="profile-v1",
        min_membership_probability_ppm=700_000,
        min_role_probability_ppm=700_000,
        min_campaign_probability_ppm=700_000,
        min_regime_probability_ppm=700_000,
        min_regime_support_launches=3,
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
