"""Operator wallet churn snapshot tests."""

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
)
from rugbot.graph.operator_profile import (
    OperatorAddressProfile,
    OperatorProfileSnapshot,
)
from rugbot.graph.wallet_churn import (
    OPERATOR_WALLET_CHURN_SNAPSHOT_VERSION,
    OperatorWalletChurnConfig,
    OperatorWalletChurnSnapshot,
    WalletChurnStatus,
    build_operator_wallet_churn_snapshot,
)

CHURN_MODULE = Path("src/rugbot/graph/wallet_churn.py")
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


class OperatorWalletChurnTests(unittest.TestCase):
    """Tests for point-in-time wallet churn snapshots."""

    def test_builds_new_retained_and_retired_wallet_churn(self) -> None:
        """Wallet switch evidence is explicit and role-aware."""

        previous = _profile(
            as_of_slot=10,
            addresses=(
                _address("retained", role=AddressRole.FUNDER, as_of_slot=10),
                _address("retired", role=AddressRole.DUMPER, as_of_slot=10),
            ),
        )
        current = _profile(
            as_of_slot=20,
            addresses=(
                _address("retained", role=AddressRole.CREATOR, as_of_slot=20),
                _address("new-relay", role=AddressRole.RELAY_ADDRESS, as_of_slot=20),
            ),
        )

        result = build_operator_wallet_churn_snapshot(
            previous_profile=previous,
            current_profile=current,
            config=_config(),
        )

        self.assertIsInstance(result, OperatorWalletChurnSnapshot)
        snapshot = cast("OperatorWalletChurnSnapshot", result)
        self.assertEqual(snapshot.as_of_slot, Slot(20))
        self.assertEqual(
            snapshot.churn_snapshot_version,
            OPERATOR_WALLET_CHURN_SNAPSHOT_VERSION,
        )
        self.assertEqual(snapshot.previous_as_of_slot, Slot(10))
        self.assertEqual(snapshot.new_address_count, 1)
        self.assertEqual(snapshot.retained_address_count, 1)
        self.assertEqual(snapshot.retired_address_count, 1)
        self.assertEqual(snapshot.new_high_risk_role_count, 1)
        self.assertEqual(snapshot.retained_role_change_count, 1)
        self.assertEqual(snapshot.address_turnover_ppm, 500_000)
        self.assertEqual(snapshot.new_addresses[0].address, "new-relay")
        self.assertEqual(snapshot.new_addresses[0].status, WalletChurnStatus.NEW)
        self.assertEqual(snapshot.retired_addresses[0].address, "retired")
        self.assertIn("new_high_risk_operator_roles_detected", snapshot.reason_codes)
        self.assertIn(
            "retained_operator_role_changes_detected",
            snapshot.reason_codes,
        )

    def test_new_launch_origin_roles_are_high_risk_churn(self) -> None:
        """Creator and creation-submitter switches count even with low turnover."""

        previous = _profile(
            as_of_slot=10,
            addresses=(
                _address("stable-a", role=AddressRole.FUNDER, as_of_slot=10),
                _address("stable-b", role=AddressRole.DUMPER, as_of_slot=10),
                _address("stable-c", role=AddressRole.RELAY_ADDRESS, as_of_slot=10),
            ),
        )
        current = _profile(
            as_of_slot=20,
            addresses=(
                _address("stable-a", role=AddressRole.FUNDER, as_of_slot=20),
                _address("stable-b", role=AddressRole.DUMPER, as_of_slot=20),
                _address("stable-c", role=AddressRole.RELAY_ADDRESS, as_of_slot=20),
                _address(
                    "new-creator",
                    role=AddressRole.CREATOR,
                    as_of_slot=20,
                ),
                _address(
                    "new-submitter",
                    role=AddressRole.CREATION_SUBMITTER,
                    as_of_slot=20,
                ),
            ),
        )

        result = build_operator_wallet_churn_snapshot(
            previous_profile=previous,
            current_profile=current,
            config=_config(),
        )

        self.assertIsInstance(result, OperatorWalletChurnSnapshot)
        snapshot = cast("OperatorWalletChurnSnapshot", result)
        self.assertEqual(snapshot.new_address_count, 2)
        self.assertEqual(snapshot.retained_address_count, 3)
        self.assertEqual(snapshot.address_turnover_ppm, 250_000)
        self.assertEqual(snapshot.new_high_risk_role_count, 2)
        self.assertEqual(
            tuple(address.high_risk_role_count for address in snapshot.new_addresses),
            (1, 1),
        )
        self.assertIn("new_high_risk_operator_roles_detected", snapshot.reason_codes)

    def test_unchanged_profiles_report_no_churn(self) -> None:
        """No wallet changes are represented explicitly."""

        previous = _profile(
            as_of_slot=10,
            addresses=(_address("stable", role=AddressRole.CREATOR, as_of_slot=10),),
        )
        current = _profile(
            as_of_slot=20,
            addresses=(_address("stable", role=AddressRole.CREATOR, as_of_slot=20),),
        )

        result = build_operator_wallet_churn_snapshot(
            previous_profile=previous,
            current_profile=current,
            config=_config(),
        )

        self.assertIsInstance(result, OperatorWalletChurnSnapshot)
        snapshot = cast("OperatorWalletChurnSnapshot", result)
        self.assertEqual(snapshot.new_address_count, 0)
        self.assertEqual(snapshot.retired_address_count, 0)
        self.assertEqual(snapshot.retained_role_change_count, 0)
        self.assertEqual(snapshot.address_turnover_ppm, 0)
        self.assertEqual(
            snapshot.reason_codes,
            (
                "operator_wallet_churn_snapshot_built",
                "no_operator_wallet_churn_detected",
            ),
        )

    def test_missing_previous_profile_abstains(self) -> None:
        """A first profile is not enough evidence to claim churn."""

        result = build_operator_wallet_churn_snapshot(
            previous_profile=None,
            current_profile=_profile(as_of_slot=20),
            config=_config(),
        )

        self.assert_abstains(result, AbstainReason.MISSING_FEATURE, as_of_slot=20)

    def test_current_profile_must_match_churn_slot(self) -> None:
        """The current profile cannot be re-stamped to another slot."""

        result = build_operator_wallet_churn_snapshot(
            previous_profile=_profile(as_of_slot=10),
            current_profile=_profile(as_of_slot=19),
            config=_config(),
        )

        self.assert_abstains(result, AbstainReason.STALE_STATE, as_of_slot=20)

    def test_previous_profile_must_be_older_than_current(self) -> None:
        """Equal or future previous profiles fail closed."""

        result = build_operator_wallet_churn_snapshot(
            previous_profile=_profile(as_of_slot=20),
            current_profile=_profile(as_of_slot=20),
            config=_config(),
        )

        self.assert_abstains(result, AbstainReason.STALE_STATE, as_of_slot=20)

    def test_unknown_profile_version_abstains(self) -> None:
        """Churn snapshots only consume accepted profile versions."""

        result = build_operator_wallet_churn_snapshot(
            previous_profile=_profile(as_of_slot=10),
            current_profile=_profile(as_of_slot=20, profile_version="profile-v2"),
            config=_config(),
        )

        self.assert_abstains(result, AbstainReason.DECODER_MISMATCH, as_of_slot=20)

    def test_old_churn_snapshot_version_abstains(self) -> None:
        """Material churn ontology changes require the current pinned version."""

        result = build_operator_wallet_churn_snapshot(
            previous_profile=_profile(as_of_slot=10),
            current_profile=_profile(as_of_slot=20),
            config=replace(_config(), churn_snapshot_version="wallet-churn-v1"),
        )

        self.assert_abstains(result, AbstainReason.DECODER_MISMATCH, as_of_slot=20)

    def test_entity_mismatch_abstains(self) -> None:
        """Profiles for different operators cannot be compared."""

        result = build_operator_wallet_churn_snapshot(
            previous_profile=_profile(as_of_slot=10, entity_id="entity-1"),
            current_profile=_profile(as_of_slot=20, entity_id="entity-2"),
            config=_config(),
        )

        self.assert_abstains(
            result,
            AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
            as_of_slot=20,
        )

    def test_weak_loaded_membership_abstains(self) -> None:
        """Loaded profiles cannot hide below-threshold memberships."""

        result = build_operator_wallet_churn_snapshot(
            previous_profile=_profile(as_of_slot=10),
            current_profile=_profile(
                as_of_slot=20,
                addresses=(
                    _address(
                        "weak",
                        role=AddressRole.CREATOR,
                        same_controller=100_000,
                        as_of_slot=20,
                    ),
                ),
            ),
            config=_config(),
        )

        self.assert_abstains(result, AbstainReason.MISSING_FEATURE, as_of_slot=20)

    def test_float_probability_abstains(self) -> None:
        """Runtime validators reject float membership probabilities."""

        result = build_operator_wallet_churn_snapshot(
            previous_profile=_profile(as_of_slot=10),
            current_profile=_profile(
                as_of_slot=20,
                addresses=(
                    _address(
                        "bad-float",
                        role=AddressRole.CREATOR,
                        same_controller=cast("Any", 0.5),
                        as_of_slot=20,
                    ),
                ),
            ),
            config=_config(),
        )

        self.assert_abstains(
            result,
            AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
            as_of_slot=20,
        )

    def test_malformed_role_assignment_abstains(self) -> None:
        """Loaded probable roles are revalidated before churn output."""

        result = build_operator_wallet_churn_snapshot(
            previous_profile=_profile(as_of_slot=10),
            current_profile=_profile(
                as_of_slot=20,
                addresses=(
                    replace(
                        _address("bad-role", role=AddressRole.CREATOR, as_of_slot=20),
                        probable_roles=cast("Any", (object(),)),
                    ),
                ),
            ),
            config=_config(),
        )

        self.assert_abstains(
            result,
            AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
            as_of_slot=20,
        )

    def test_mutable_loaded_collections_abstain(self) -> None:
        """Loaded profile and address collections must be immutable tuples."""

        cases = (
            _profile(
                as_of_slot=20,
                addresses=cast(
                    "Any",
                    [_address("list-row", role=AddressRole.CREATOR, as_of_slot=20)],
                ),
            ),
            _profile(
                as_of_slot=20,
                addresses=(
                    replace(
                        _address(
                            "list-roles",
                            role=AddressRole.CREATOR,
                            as_of_slot=20,
                        ),
                        probable_roles=cast(
                            "Any",
                            [
                                _role(
                                    address="list-roles",
                                    role=AddressRole.CREATOR,
                                    as_of_slot=20,
                                    probability=900_000,
                                )
                            ],
                        ),
                    ),
                ),
            ),
        )
        for current_profile in cases:
            with self.subTest(current_profile=current_profile):
                result = build_operator_wallet_churn_snapshot(
                    previous_profile=_profile(as_of_slot=10),
                    current_profile=current_profile,
                    config=_config(),
                )

                self.assert_abstains(
                    result,
                    AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
                    as_of_slot=20,
                )

    def test_whitespace_identity_and_provenance_abstain(self) -> None:
        """Whitespace-only loaded identity/provenance strings are missing."""

        missing_cases = (
            replace(
                _address("blank-address", role=AddressRole.CREATOR, as_of_slot=20),
                address=" ",
            ),
            replace(
                _address("blank-evidence", role=AddressRole.CREATOR, as_of_slot=20),
                evidence_ids=(" ",),
            ),
            replace(
                _address(
                    "blank-role-evidence", role=AddressRole.CREATOR, as_of_slot=20
                ),
                probable_roles=(
                    replace(
                        _role(
                            address="blank-role-evidence",
                            role=AddressRole.CREATOR,
                            as_of_slot=20,
                            probability=900_000,
                        ),
                        evidence_ids=(" ",),
                    ),
                ),
            ),
        )
        for address in missing_cases:
            with self.subTest(address=address):
                result = build_operator_wallet_churn_snapshot(
                    previous_profile=_profile(as_of_slot=10),
                    current_profile=_profile(as_of_slot=20, addresses=(address,)),
                    config=_config(),
                )

                self.assert_abstains(
                    result,
                    AbstainReason.MISSING_FEATURE,
                    as_of_slot=20,
                )

        decoder_cases = (
            replace(
                _address("blank-model", role=AddressRole.CREATOR, as_of_slot=20),
                model_version=" ",
            ),
            replace(
                _address("blank-role-model", role=AddressRole.CREATOR, as_of_slot=20),
                probable_roles=(
                    replace(
                        _role(
                            address="blank-role-model",
                            role=AddressRole.CREATOR,
                            as_of_slot=20,
                            probability=900_000,
                        ),
                        model_version=" ",
                    ),
                ),
            ),
        )
        for address in decoder_cases:
            with self.subTest(address=address):
                result = build_operator_wallet_churn_snapshot(
                    previous_profile=_profile(as_of_slot=10),
                    current_profile=_profile(as_of_slot=20, addresses=(address,)),
                    config=_config(),
                )

                self.assert_abstains(
                    result,
                    AbstainReason.DECODER_MISMATCH,
                    as_of_slot=20,
                )

    def test_duplicate_profile_addresses_abstain(self) -> None:
        """Address churn cannot collapse duplicate loaded profile rows."""

        result = build_operator_wallet_churn_snapshot(
            previous_profile=_profile(as_of_slot=10),
            current_profile=_profile(
                as_of_slot=20,
                addresses=(
                    _address("dup", role=AddressRole.CREATOR, as_of_slot=20),
                    _address("dup", role=AddressRole.FUNDER, as_of_slot=20),
                ),
            ),
            config=_config(),
        )

        self.assert_abstains(
            result,
            AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
            as_of_slot=20,
        )

    def test_wallet_churn_module_stays_pure_and_integer_only(self) -> None:
        """Churn contracts must not grow adapters, signers, floats, or division."""

        source = CHURN_MODULE.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(CHURN_MODULE))
        violations = [
            imported_name
            for imported_name in _imported_module_names(tree)
            if imported_name.startswith(FORBIDDEN_IMPORT_PREFIXES)
        ]
        float_literals = [
            node.value
            for node in ast.walk(tree)
            if isinstance(node, ast.Constant) and isinstance(node.value, float)
        ]
        true_divisions = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div)
        ]

        self.assertEqual(violations, [])
        self.assertEqual(float_literals, [])
        self.assertEqual(true_divisions, [])
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


def _profile(
    *,
    as_of_slot: int,
    entity_id: str = "entity-1",
    profile_version: str = "profile-v1",
    addresses: tuple[OperatorAddressProfile, ...] | None = None,
) -> OperatorProfileSnapshot:
    selected_addresses = addresses or (
        _address("creator-a", role=AddressRole.CREATOR, as_of_slot=as_of_slot),
    )
    return OperatorProfileSnapshot(
        as_of_slot=Slot(as_of_slot),
        entity_id=entity_id,
        profile_version=profile_version,
        entity_resolver_version="resolver-v1",
        role_classifier_version="roles-v1",
        addresses=selected_addresses,
        campaigns=(),
        regimes=(),
        current_active_regime_id=None,
        source_membership_count=len(selected_addresses),
        active_address_count=len(selected_addresses),
        source_campaign_count=0,
        active_campaign_count=0,
        source_regime_count=0,
        active_regime_count=0,
        reason_codes=("operator_profile_built",),
    )


def _address(  # noqa: PLR0913
    address: str,
    *,
    role: AddressRole,
    as_of_slot: int,
    same_controller: int = 900_000,
    cooperating: int = 0,
    role_probability: int = 900_000,
) -> OperatorAddressProfile:
    return OperatorAddressProfile(
        as_of_slot=Slot(as_of_slot),
        entity_id="entity-1",
        address=address,
        same_controller_probability_ppm=same_controller,
        cooperating_probability_ppm=cooperating,
        shared_service_probability_ppm=0,
        incidental_interaction_probability_ppm=0,
        probable_roles=(
            _role(
                address=address,
                role=role,
                as_of_slot=as_of_slot,
                probability=role_probability,
            ),
        ),
        evidence_ids=(f"membership:{address}:{as_of_slot}",),
        model_version="membership-model-v1",
    )


def _role(
    *,
    address: str,
    role: AddressRole,
    as_of_slot: int,
    probability: int,
) -> AddressRoleAssignment:
    return AddressRoleAssignment(
        as_of_slot=Slot(as_of_slot),
        address=address,
        role=role,
        role_probability_ppm=probability,
        evidence_ids=(f"role:{address}:{role.value}:{as_of_slot}",),
        model_version="role-model-v1",
    )


def _config() -> OperatorWalletChurnConfig:
    return OperatorWalletChurnConfig(
        as_of_slot=Slot(20),
        churn_snapshot_version=OPERATOR_WALLET_CHURN_SNAPSHOT_VERSION,
        accepted_profile_versions=("profile-v1",),
        min_membership_probability_ppm=700_000,
        min_role_probability_ppm=700_000,
    )


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
        "PRIVATE" + "_KEY",
        "send" + "_transaction",
        "send" + "_raw_transaction",
        "float(",
    )


if __name__ == "__main__":
    unittest.main()
