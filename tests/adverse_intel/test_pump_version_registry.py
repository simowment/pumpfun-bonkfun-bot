"""Pump protocol/config/fee version registry tests."""

import ast
import unittest
from pathlib import Path
from typing import cast

from rugbot.domain.amounts import Slot
from rugbot.domain.decisions import AbstainReason, AbstainResult
from rugbot.protocol.pump.version_registry import (
    PUMP_VERSION_REGISTRY_VERSION,
    PumpFeeScheduleVersion,
    PumpProgramConfigVersion,
    PumpProtocolVersionSnapshot,
    PumpVersionResolveRequest,
    resolve_pump_protocol_versions,
)

VERSION_REGISTRY_MODULE = Path("src/rugbot/protocol/pump/version_registry.py")
PROGRAM_ID = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
IDL_HASH = "pump-idl-sha256"
GLOBAL_CONFIG_HASH = "pump-global-config-sha256"
FORBIDDEN_IMPORT_PREFIXES = (
    "requests",
    "aiohttp",
    "httpx",
    "sqlite",
    "psycopg",
    "src.trading",
    "src.platforms",
    "solana",
    "solders",
    "dotenv",
)


class PumpVersionRegistryTests(unittest.TestCase):
    """Tests for point-in-time Pump version resolution."""

    def test_resolves_point_in_time_protocol_and_fee_snapshot(self) -> None:
        """A single active config and fee schedule produce a known snapshot."""

        result = resolve_pump_protocol_versions(
            request=_request(as_of_slot=150),
            program_configs=_program_configs(),
            fee_schedules=_fee_schedules(),
        )

        self.assertIsInstance(result, PumpProtocolVersionSnapshot)
        snapshot = cast("PumpProtocolVersionSnapshot", result)
        self.assertEqual(snapshot.as_of_slot, 150)
        self.assertEqual(snapshot.program_id, PROGRAM_ID)
        self.assertEqual(snapshot.idl_hash, IDL_HASH)
        self.assertEqual(snapshot.global_config_hash, GLOBAL_CONFIG_HASH)
        self.assertEqual(snapshot.program_config_version, "pump-global-v1")
        self.assertEqual(snapshot.registry_version, PUMP_VERSION_REGISTRY_VERSION)
        self.assertEqual(snapshot.fee_config.version, "pump-fees-v1")
        self.assertEqual(snapshot.fee_config.protocol_fee_bps, 100)
        self.assertEqual(snapshot.fee_config.creator_fee_bps, 25)
        self.assertEqual(
            snapshot.fee_config.program_config_version,
            "pump-global-v1",
        )
        self.assertEqual(snapshot.fee_config.valid_from_slot, 100)
        self.assertEqual(snapshot.fee_config.valid_to_slot, 200)
        self.assertEqual(
            snapshot.fee_config.source_artifact_version,
            "fee-artifact-v1",
        )
        self.assertEqual(
            snapshot.program_config_source_artifact_version,
            "program-config-artifact-v1",
        )
        self.assertEqual(
            snapshot.fee_source_artifact_version,
            "fee-artifact-v1",
        )

    def test_valid_to_slot_is_exclusive(self) -> None:
        """Slot intervals are point-in-time and valid_to is exclusive."""

        result = resolve_pump_protocol_versions(
            request=_request(as_of_slot=200),
            program_configs=_program_configs(),
            fee_schedules=_fee_schedules(),
        )

        self.assertIsInstance(result, AbstainResult)
        result = cast("AbstainResult", result)
        self.assertEqual(result.reason, AbstainReason.UNKNOWN_PROTOCOL_STATE)
        self.assertEqual(result.as_of_slot, 200)

    def test_valid_from_slot_is_inclusive(self) -> None:
        """Slot intervals include valid_from_slot."""

        result = resolve_pump_protocol_versions(
            request=_request(as_of_slot=100),
            program_configs=_program_configs(),
            fee_schedules=_fee_schedules(),
        )

        self.assertIsInstance(result, PumpProtocolVersionSnapshot)
        snapshot = cast("PumpProtocolVersionSnapshot", result)
        self.assertEqual(snapshot.as_of_slot, 100)

    def test_open_ended_valid_to_slot_resolves(self) -> None:
        """Open-ended intervals remain active after valid_from_slot."""

        result = resolve_pump_protocol_versions(
            request=_request(as_of_slot=300),
            program_configs=(
                PumpProgramConfigVersion(
                    version="pump-global-v1",
                    program_id=PROGRAM_ID,
                    idl_hash=IDL_HASH,
                    global_config_hash=GLOBAL_CONFIG_HASH,
                    valid_from_slot=Slot(100),
                    valid_to_slot=None,
                    source_artifact_version="program-config-artifact-v1",
                ),
            ),
            fee_schedules=(
                PumpFeeScheduleVersion(
                    version="pump-fees-v1",
                    program_config_version="pump-global-v1",
                    protocol_fee_bps=100,
                    creator_fee_bps=25,
                    valid_from_slot=Slot(100),
                    valid_to_slot=None,
                    source_artifact_version="fee-artifact-v1",
                ),
            ),
        )

        self.assertIsInstance(result, PumpProtocolVersionSnapshot)
        snapshot = cast("PumpProtocolVersionSnapshot", result)
        self.assertIsNone(snapshot.fee_config.valid_to_slot)

    def test_negative_as_of_slot_abstains(self) -> None:
        """Negative slot requests are unsupported."""

        result = resolve_pump_protocol_versions(
            request=_request(as_of_slot=-1),
            program_configs=_program_configs(),
            fee_schedules=_fee_schedules(),
        )

        self.assertIsInstance(result, AbstainResult)
        result = cast("AbstainResult", result)
        self.assertEqual(result.reason, AbstainReason.UNSUPPORTED_PROTOCOL_STATE)
        self.assertEqual(result.as_of_slot, -1)

    def test_unknown_program_state_abstains(self) -> None:
        """Unknown program/config material fails closed."""

        result = resolve_pump_protocol_versions(
            request=_request(as_of_slot=150, program_id="unknown-program"),
            program_configs=_program_configs(),
            fee_schedules=_fee_schedules(),
        )

        self.assertIsInstance(result, AbstainResult)
        result = cast("AbstainResult", result)
        self.assertEqual(result.reason, AbstainReason.UNKNOWN_PROTOCOL_STATE)

    def test_missing_global_config_hash_abstains(self) -> None:
        """A config hash is required before publishing a version snapshot."""

        result = resolve_pump_protocol_versions(
            request=_request(as_of_slot=150, global_config_hash=""),
            program_configs=_program_configs(),
            fee_schedules=_fee_schedules(),
        )

        self.assertIsInstance(result, AbstainResult)
        result = cast("AbstainResult", result)
        self.assertEqual(result.reason, AbstainReason.UNKNOWN_PROTOCOL_STATE)

    def test_idl_mismatch_abstains_as_decoder_mismatch(self) -> None:
        """Active config with a different IDL hash is a decoder mismatch."""

        result = resolve_pump_protocol_versions(
            request=_request(as_of_slot=150, idl_hash="different-idl"),
            program_configs=_program_configs(),
            fee_schedules=_fee_schedules(),
        )

        self.assertIsInstance(result, AbstainResult)
        result = cast("AbstainResult", result)
        self.assertEqual(result.reason, AbstainReason.DECODER_MISMATCH)

    def test_ambiguous_program_config_abstains(self) -> None:
        """Overlapping active configs are not resolved by guessing."""

        result = resolve_pump_protocol_versions(
            request=_request(as_of_slot=150),
            program_configs=(
                *_program_configs(),
                PumpProgramConfigVersion(
                    version="pump-global-v1-shadow",
                    program_id=PROGRAM_ID,
                    idl_hash=IDL_HASH,
                    global_config_hash=GLOBAL_CONFIG_HASH,
                    valid_from_slot=Slot(120),
                    valid_to_slot=Slot(180),
                    source_artifact_version="program-config-artifact-v1",
                ),
            ),
            fee_schedules=_fee_schedules(),
        )

        self.assertIsInstance(result, AbstainResult)
        result = cast("AbstainResult", result)
        self.assertEqual(result.reason, AbstainReason.UNKNOWN_PROTOCOL_STATE)

    def test_missing_fee_schedule_abstains(self) -> None:
        """Program config without an active fee schedule is not executable."""

        result = resolve_pump_protocol_versions(
            request=_request(as_of_slot=150),
            program_configs=_program_configs(),
            fee_schedules=(
                PumpFeeScheduleVersion(
                    version="future-fees",
                    program_config_version="pump-global-v1",
                    protocol_fee_bps=100,
                    creator_fee_bps=25,
                    valid_from_slot=Slot(201),
                    valid_to_slot=None,
                    source_artifact_version="fee-artifact-v2",
                ),
            ),
        )

        self.assertIsInstance(result, AbstainResult)
        result = cast("AbstainResult", result)
        self.assertEqual(result.reason, AbstainReason.UNKNOWN_FEE_CONFIG)

    def test_missing_program_config_source_artifact_abstains(self) -> None:
        """Program/config versions require source artifact provenance."""

        result = resolve_pump_protocol_versions(
            request=_request(as_of_slot=150),
            program_configs=(
                PumpProgramConfigVersion(
                    version="pump-global-v1",
                    program_id=PROGRAM_ID,
                    idl_hash=IDL_HASH,
                    global_config_hash=GLOBAL_CONFIG_HASH,
                    valid_from_slot=Slot(100),
                    valid_to_slot=Slot(200),
                    source_artifact_version="",
                ),
            ),
            fee_schedules=_fee_schedules(),
        )

        self.assertIsInstance(result, AbstainResult)
        result = cast("AbstainResult", result)
        self.assertEqual(result.reason, AbstainReason.UNKNOWN_PROTOCOL_STATE)

    def test_missing_fee_source_artifact_abstains(self) -> None:
        """Fee schedules require source artifact provenance."""

        result = resolve_pump_protocol_versions(
            request=_request(as_of_slot=150),
            program_configs=_program_configs(),
            fee_schedules=(
                PumpFeeScheduleVersion(
                    version="pump-fees-v1",
                    program_config_version="pump-global-v1",
                    protocol_fee_bps=100,
                    creator_fee_bps=25,
                    valid_from_slot=Slot(100),
                    valid_to_slot=Slot(200),
                    source_artifact_version="",
                ),
            ),
        )

        self.assertIsInstance(result, AbstainResult)
        result = cast("AbstainResult", result)
        self.assertEqual(result.reason, AbstainReason.UNKNOWN_FEE_CONFIG)

    def test_invalid_program_interval_abstains(self) -> None:
        """Invalid program/config version intervals fail closed."""

        result = resolve_pump_protocol_versions(
            request=_request(as_of_slot=150),
            program_configs=(
                PumpProgramConfigVersion(
                    version="pump-global-v1",
                    program_id=PROGRAM_ID,
                    idl_hash=IDL_HASH,
                    global_config_hash=GLOBAL_CONFIG_HASH,
                    valid_from_slot=Slot(200),
                    valid_to_slot=Slot(100),
                    source_artifact_version="program-config-artifact-v1",
                ),
            ),
            fee_schedules=_fee_schedules(),
        )

        self.assertIsInstance(result, AbstainResult)
        result = cast("AbstainResult", result)
        self.assertEqual(result.reason, AbstainReason.UNKNOWN_PROTOCOL_STATE)

    def test_invalid_fee_interval_abstains(self) -> None:
        """Invalid fee schedule intervals fail closed."""

        result = resolve_pump_protocol_versions(
            request=_request(as_of_slot=150),
            program_configs=_program_configs(),
            fee_schedules=(
                PumpFeeScheduleVersion(
                    version="pump-fees-v1",
                    program_config_version="pump-global-v1",
                    protocol_fee_bps=100,
                    creator_fee_bps=25,
                    valid_from_slot=Slot(200),
                    valid_to_slot=Slot(100),
                    source_artifact_version="fee-artifact-v1",
                ),
            ),
        )

        self.assertIsInstance(result, AbstainResult)
        result = cast("AbstainResult", result)
        self.assertEqual(result.reason, AbstainReason.UNKNOWN_FEE_CONFIG)

    def test_ambiguous_fee_schedule_abstains(self) -> None:
        """Overlapping active fee schedules are not resolved by guessing."""

        result = resolve_pump_protocol_versions(
            request=_request(as_of_slot=150),
            program_configs=_program_configs(),
            fee_schedules=(
                *_fee_schedules(),
                PumpFeeScheduleVersion(
                    version="pump-fees-v1-shadow",
                    program_config_version="pump-global-v1",
                    protocol_fee_bps=100,
                    creator_fee_bps=25,
                    valid_from_slot=Slot(120),
                    valid_to_slot=Slot(180),
                    source_artifact_version="fee-artifact-v1",
                ),
            ),
        )

        self.assertIsInstance(result, AbstainResult)
        result = cast("AbstainResult", result)
        self.assertEqual(result.reason, AbstainReason.UNKNOWN_FEE_CONFIG)

    def test_invalid_fee_basis_points_abstain(self) -> None:
        """Fee schedules use integer bps and invalid ranges fail closed."""

        result = resolve_pump_protocol_versions(
            request=_request(as_of_slot=150),
            program_configs=_program_configs(),
            fee_schedules=(
                PumpFeeScheduleVersion(
                    version="bad-fees",
                    program_config_version="pump-global-v1",
                    protocol_fee_bps=10_001,
                    creator_fee_bps=0,
                    valid_from_slot=Slot(100),
                    valid_to_slot=Slot(200),
                    source_artifact_version="fee-artifact-v1",
                ),
            ),
        )

        self.assertIsInstance(result, AbstainResult)
        result = cast("AbstainResult", result)
        self.assertEqual(result.reason, AbstainReason.UNSUPPORTED_PROTOCOL_STATE)

    def test_float_fee_basis_points_abstain(self) -> None:
        """Runtime float fee bps must not resolve to executable configs."""

        result = resolve_pump_protocol_versions(
            request=_request(as_of_slot=150),
            program_configs=_program_configs(),
            fee_schedules=(
                PumpFeeScheduleVersion(
                    version="bad-fees",
                    program_config_version="pump-global-v1",
                    protocol_fee_bps=100.5,
                    creator_fee_bps=25,
                    valid_from_slot=Slot(100),
                    valid_to_slot=Slot(200),
                    source_artifact_version="fee-artifact-v1",
                ),
            ),
        )

        self.assertIsInstance(result, AbstainResult)
        result = cast("AbstainResult", result)
        self.assertEqual(result.reason, AbstainReason.UNSUPPORTED_PROTOCOL_STATE)

    def test_version_registry_stays_pure_and_integer_only(self) -> None:
        """The registry must not grow RPC, database, signer, or float paths."""

        source = VERSION_REGISTRY_MODULE.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(VERSION_REGISTRY_MODULE))
        violations = [
            imported_name
            for imported_name in _imported_module_names(tree)
            if imported_name.startswith(FORBIDDEN_IMPORT_PREFIXES)
        ]

        self.assertEqual(violations, [])
        for token in _forbidden_source_tokens():
            with self.subTest(token=token):
                self.assertNotIn(token, source)


def _forbidden_source_tokens() -> tuple[str, ...]:
    return (
        "Key" + "pair",
        "Wal" + "let",
        "PRIVATE" + "_KEY",
        "send" + "_transaction",
        "send" + "_raw_transaction",
        "float(",
    )


def _imported_module_names(tree: ast.AST) -> list[str]:
    names: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            names.append(node.module)
    return names


def _request(
    *,
    as_of_slot: int,
    program_id: str = PROGRAM_ID,
    idl_hash: str = IDL_HASH,
    global_config_hash: str = GLOBAL_CONFIG_HASH,
) -> PumpVersionResolveRequest:
    return PumpVersionResolveRequest(
        as_of_slot=Slot(as_of_slot),
        program_id=program_id,
        idl_hash=idl_hash,
        global_config_hash=global_config_hash,
    )


def _program_configs() -> tuple[PumpProgramConfigVersion, ...]:
    return (
        PumpProgramConfigVersion(
            version="pump-global-v1",
            program_id=PROGRAM_ID,
            idl_hash=IDL_HASH,
            global_config_hash=GLOBAL_CONFIG_HASH,
            valid_from_slot=Slot(100),
            valid_to_slot=Slot(200),
            source_artifact_version="program-config-artifact-v1",
        ),
    )


def _fee_schedules() -> tuple[PumpFeeScheduleVersion, ...]:
    return (
        PumpFeeScheduleVersion(
            version="pump-fees-v1",
            program_config_version="pump-global-v1",
            protocol_fee_bps=100,
            creator_fee_bps=25,
            valid_from_slot=Slot(100),
            valid_to_slot=Slot(200),
            source_artifact_version="fee-artifact-v1",
        ),
    )


if __name__ == "__main__":
    unittest.main()
