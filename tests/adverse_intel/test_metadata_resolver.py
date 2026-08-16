"""Focused tests for the pure Pump metadata resolver boundary."""

import unittest
from dataclasses import replace
from typing import TYPE_CHECKING, cast

from rugbot.domain.amounts import Slot
from rugbot.domain.decisions import AbstainReason, AbstainResult
from rugbot.protocol.pump.bonding_curve_account import (
    PINNED_PUMP_IDL_SHA256,
    PUMP_PROGRAM_ID,
)
from rugbot.protocol.pump.create_decoder import SPL_2022_PROGRAM_ID
from rugbot.protocol.pump.create_event_decoder import SOL_PUBKEY
from rugbot.protocol.pump.metadata_resolver import (
    PumpFinalizedAccountMetadataEvidence,
    PumpFinalizedMintMetadataEvidence,
    PumpMetadataResolveRequest,
    resolve_pump_create_metadata,
)
from rugbot.protocol.pump.version_registry import (
    PUMP_VERSION_REGISTRY_VERSION,
    PumpFeeScheduleVersion,
    PumpProgramConfigVersion,
    PumpProtocolVersionSnapshot,
)

if TYPE_CHECKING:
    from rugbot.protocol.pump.create_state_adapter import PumpCreateMintMetadataProof

SLOT = Slot(371_337_946)
MINT = "GGYkiUJGopoH9DZUjGFtLiKpXXW5AQovqZ7eVcjEpump"


class PumpMetadataResolverTests(unittest.TestCase):
    """The resolver must only emit aligned, pinned metadata contracts."""

    def test_resolves_typed_finalized_evidence_and_registry(self) -> None:
        result = resolve_pump_create_metadata(_request())

        self.assertNotIsInstance(result, AbstainResult)
        metadata, protocol = cast(
            "tuple[PumpCreateMintMetadataProof, PumpProtocolVersionSnapshot]",
            result,
        )
        self.assertEqual(metadata.as_of_slot, SLOT)
        self.assertEqual(metadata.base_mint_pubkey, MINT)
        self.assertEqual(metadata.base_decimals, 6)
        self.assertEqual(metadata.quote_mint_pubkey, SOL_PUBKEY)
        self.assertEqual(metadata.quote_decimals, 9)
        self.assertEqual(
            metadata.source_artifact,
            "account-fixture:mint-fixture:sol-fixture",
        )
        self.assertEqual(protocol.as_of_slot, SLOT)
        self.assertEqual(protocol.program_id, PUMP_PROGRAM_ID)
        self.assertEqual(protocol.idl_hash, PINNED_PUMP_IDL_SHA256)

    def test_evidence_must_share_as_of_slot(self) -> None:
        result = resolve_pump_create_metadata(
            replace(
                _request(),
                base_mint_evidence=replace(
                    _request().base_mint_evidence,
                    as_of_slot=Slot(SLOT + 1),
                ),
            )
        )

        self.assert_abstains(result, AbstainReason.STALE_STATE)

    def test_non_finalized_evidence_abstains(self) -> None:
        result = resolve_pump_create_metadata(
            replace(
                _request(),
                quote_mint_evidence=replace(
                    _request().quote_mint_evidence,
                    commitment="confirmed",
                ),
            )
        )

        self.assert_abstains(result, AbstainReason.STALE_STATE)

    def test_program_and_idl_are_pinned(self) -> None:
        wrong_program = resolve_pump_create_metadata(
            replace(
                _request(),
                account_evidence=replace(
                    _request().account_evidence,
                    program_id="other-program",
                ),
            )
        )
        wrong_idl = resolve_pump_create_metadata(
            replace(
                _request(),
                account_evidence=replace(
                    _request().account_evidence,
                    idl_hash="0" * 64,
                ),
            )
        )

        self.assert_abstains(wrong_program, AbstainReason.UNKNOWN_PROTOCOL_STATE)
        self.assert_abstains(wrong_idl, AbstainReason.DECODER_MISMATCH)

    def test_registry_identity_is_pinned_and_artifacts_are_required(self) -> None:
        wrong_registry = resolve_pump_create_metadata(
            replace(_request(), registry_version="other-registry")
        )
        empty_registry = resolve_pump_create_metadata(
            replace(_request(), program_configs=())
        )

        self.assert_abstains(wrong_registry, AbstainReason.DECODER_MISMATCH)
        self.assert_abstains(empty_registry, AbstainReason.UNKNOWN_PROTOCOL_STATE)

    def test_no_defaults_for_missing_metadata_or_decimals(self) -> None:
        missing_source = resolve_pump_create_metadata(
            replace(
                _request(),
                base_mint_evidence=replace(
                    _request().base_mint_evidence,
                    source_artifact="",
                ),
            )
        )
        invalid_decimals = resolve_pump_create_metadata(
            replace(
                _request(),
                base_mint_evidence=replace(
                    _request().base_mint_evidence,
                    decimals=18 + 1,
                ),
            )
        )

        self.assert_abstains(missing_source, AbstainReason.MISSING_FEATURE)
        self.assert_abstains(
            invalid_decimals,
            AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
        )

    def test_quote_metadata_must_be_explicit_native_sol(self) -> None:
        result = resolve_pump_create_metadata(
            replace(
                _request(),
                quote_mint_evidence=replace(
                    _request().quote_mint_evidence,
                    mint_pubkey="other-quote",
                ),
            )
        )

        self.assert_abstains(result, AbstainReason.UNKNOWN_PROTOCOL_STATE)

    def assert_abstains(self, result: object, reason: AbstainReason) -> None:
        self.assertIsInstance(result, AbstainResult)
        if isinstance(result, AbstainResult):
            self.assertEqual(result.reason, reason)
            self.assertEqual(result.as_of_slot, SLOT)


def _request() -> PumpMetadataResolveRequest:
    return PumpMetadataResolveRequest(
        as_of_slot=SLOT,
        account_evidence=PumpFinalizedAccountMetadataEvidence(
            as_of_slot=SLOT,
            account_pubkey="global-account-fixture",
            owner_program_id=PUMP_PROGRAM_ID,
            program_id=PUMP_PROGRAM_ID,
            idl_hash=PINNED_PUMP_IDL_SHA256,
            global_config_hash="global-config-fixture",
            source_artifact="account-fixture",
            commitment="finalized",
        ),
        base_mint_evidence=PumpFinalizedMintMetadataEvidence(
            as_of_slot=SLOT,
            mint_pubkey=MINT,
            owner_program_id=SPL_2022_PROGRAM_ID,
            decimals=6,
            source_artifact="mint-fixture",
            commitment="finalized",
        ),
        quote_mint_evidence=PumpFinalizedMintMetadataEvidence(
            as_of_slot=SLOT,
            mint_pubkey=SOL_PUBKEY,
            owner_program_id=SOL_PUBKEY,
            decimals=9,
            source_artifact="sol-fixture",
            commitment="finalized",
        ),
        program_configs=(
            PumpProgramConfigVersion(
                version="pump-global-v1",
                program_id=PUMP_PROGRAM_ID,
                idl_hash=PINNED_PUMP_IDL_SHA256,
                global_config_hash="global-config-fixture",
                valid_from_slot=Slot(SLOT - 1),
                valid_to_slot=None,
                source_artifact_version="program-fixture",
            ),
        ),
        fee_schedules=(
            PumpFeeScheduleVersion(
                version="pump-fees-v1",
                program_config_version="pump-global-v1",
                protocol_fee_bps=100,
                creator_fee_bps=25,
                valid_from_slot=Slot(SLOT - 1),
                valid_to_slot=None,
                source_artifact_version="fee-fixture",
            ),
        ),
        registry_version=PUMP_VERSION_REGISTRY_VERSION,
    )


if __name__ == "__main__":
    unittest.main()
