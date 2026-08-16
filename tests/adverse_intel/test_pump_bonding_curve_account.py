"""Pump bonding-curve account decoder tests."""

import ast
import base64
import hashlib
import json
import unittest
from dataclasses import dataclass, replace
from pathlib import Path
from typing import cast

from rugbot.domain.amounts import Slot
from rugbot.domain.decisions import AbstainReason, AbstainResult
from rugbot.domain.market_state import PumpBondingCurveAccountSnapshot
from rugbot.protocol.pump.bonding_curve_account import (
    BONDING_CURVE_DISCRIMINATOR,
    CURRENT_LAYOUT_SIZE,
    PINNED_PUMP_IDL_SHA256,
    PUMP_BONDING_CURVE_ACCOUNT_DECODER_VERSION,
    PUMP_BONDING_CURVE_LAYOUT_ARTIFACT_VERSION,
    PUMP_PROGRAM_ID,
    PumpBondingCurveAccountState,
    PumpBondingCurveDecodeRequest,
    bonding_curve_snapshot_to_pool_reserves,
    decode_pump_bonding_curve_account,
)
from rugbot.protocol.pump.quote_engine import (
    ExecutableQuote,
    FeeConfig,
    PoolReserves,
    QuotePath,
    executable_buy_quote,
)
from rugbot.protocol.pump.version_registry import PumpProtocolVersionSnapshot

DECODER_MODULE = Path("src/rugbot/protocol/pump/bonding_curve_account.py")
PUMP_IDL_PATH = Path("idl/pump_fun_idl.json")
FIXTURE_PATH = Path(
    "fixtures/account_states/pump_bonding_curve/finalized_current_layout_ffzxakv.json"
)
SOURCE_ARTIFACT_VERSION = "pump-bonding-curve-account-fixture-v1"
BASE_MINT = "fixture-base-mint"
QUOTE_MINT = "So11111111111111111111111111111111111111112"
FORBIDDEN_IMPORT_PREFIXES = (
    "rugbot.ingest",
    "rugbot.storage",
    "rugbot.execution",
    "src.core",
    "src.trading",
    "src.platforms",
    "solana",
    "solders",
    "dotenv",
    "requests",
    "aiohttp",
    "httpx",
)


class PumpBondingCurveAccountDecoderTests(unittest.TestCase):
    """Tests for pinned Pump bonding-curve account snapshot decoding."""

    def test_decodes_finalized_fixture_exactly(self) -> None:
        """The finalized account fixture decodes to the expected IDL fields."""

        fixture = _fixture()
        snapshot = decode_pump_bonding_curve_account(_request())

        self.assertIsInstance(snapshot, PumpBondingCurveAccountSnapshot)
        snapshot = cast("PumpBondingCurveAccountSnapshot", snapshot)
        expected = fixture["expected"]
        self.assertEqual(snapshot.as_of_slot, fixture["rpc"]["context_slot"])
        self.assertEqual(snapshot.account_pubkey, fixture["account"]["pubkey"])
        self.assertEqual(snapshot.owner_program_id, PUMP_PROGRAM_ID)
        self.assertEqual(
            int(snapshot.virtual_token_reserves),
            expected["virtual_token_reserves"],
        )
        self.assertEqual(
            int(snapshot.virtual_sol_reserves),
            expected["virtual_sol_reserves"],
        )
        self.assertEqual(
            int(snapshot.real_token_reserves),
            expected["real_token_reserves"],
        )
        self.assertEqual(int(snapshot.real_sol_reserves), expected["real_sol_reserves"])
        self.assertEqual(
            int(snapshot.token_total_supply),
            expected["token_total_supply"],
        )
        self.assertEqual(snapshot.complete, expected["complete"])
        self.assertEqual(snapshot.creator.hex(), expected["creator_hex"])
        self.assertEqual(snapshot.is_mayhem_mode, expected["is_mayhem_mode"])
        self.assertEqual(snapshot.is_cashback_coin, expected["is_cashback_coin"])
        self.assertEqual(snapshot.account_data_length, fixture["account"]["space"])
        self.assertEqual(
            snapshot.trailing_zero_padding_length,
            expected["trailing_zero_padding_length"],
        )
        self.assertEqual(
            snapshot.raw_account_data_sha256, fixture["account"]["data_sha256"]
        )

    def test_maps_snapshot_to_pool_reserves_for_quote_engine(self) -> None:
        """Decoded snapshots adapt directly to integer quote-engine reserves."""

        snapshot = cast(
            "PumpBondingCurveAccountSnapshot",
            decode_pump_bonding_curve_account(_request()),
        )
        reserves = bonding_curve_snapshot_to_pool_reserves(snapshot)

        self.assertIsInstance(reserves, PoolReserves)
        reserves = cast("PoolReserves", reserves)
        self.assertEqual(reserves.as_of_slot, snapshot.as_of_slot)
        self.assertEqual(
            reserves.virtual_base_reserves, snapshot.virtual_token_reserves
        )
        self.assertEqual(reserves.virtual_quote_reserves, snapshot.virtual_sol_reserves)
        self.assertEqual(reserves.real_base_reserves, snapshot.real_token_reserves)
        self.assertEqual(reserves.real_quote_reserves, snapshot.real_sol_reserves)
        self.assertEqual(reserves.is_complete, snapshot.complete)
        self.assertEqual(reserves.base_decimals, 6)
        self.assertEqual(reserves.quote_decimals, 9)
        self.assertEqual(reserves.idl_hash, PINNED_PUMP_IDL_SHA256)
        self.assertEqual(reserves.program_config_version, "pump-global-v1")

        quote = executable_buy_quote(
            path=QuotePath.PUMP_BONDING_CURVE,
            reserves=reserves,
            quote_input_amount=type(reserves.virtual_quote_reserves)(10_000),
            fee_config=_fee_config(snapshot.as_of_slot),
        )

        self.assertIsInstance(quote, ExecutableQuote)
        quote = cast("ExecutableQuote", quote)
        self.assertEqual(quote.as_of_slot, snapshot.as_of_slot)
        self.assertEqual(quote.output_amount_base_units, 350_320_631)
        self.assertEqual(quote.fee_amount_base_units, 124)
        self.assertEqual(quote.idl_hash, PINNED_PUMP_IDL_SHA256)
        self.assertEqual(quote.program_config_version, "pump-global-v1")

    def test_old_49_byte_learning_layout_abstains(self) -> None:
        """Legacy account bytes are not silently upgraded to current layout."""

        raw_data = _old_learning_fixture_raw_data()
        result = decode_pump_bonding_curve_account(
            _request(overrides=_RequestOverrides(raw_account_data=raw_data))
        )

        self.assert_abstains(result, AbstainReason.UNSUPPORTED_PROTOCOL_STATE)

    def test_discriminator_mismatch_abstains(self) -> None:
        """Account data must start with the pinned BondingCurve discriminator."""

        raw_data = bytearray(_fixture_raw_data())
        raw_data[0] ^= 1

        result = decode_pump_bonding_curve_account(
            _request(overrides=_RequestOverrides(raw_account_data=bytes(raw_data)))
        )

        self.assert_abstains(result, AbstainReason.DECODER_MISMATCH)

    def test_invalid_bool_abstains(self) -> None:
        """Anchor bool fields must be canonical 0 or 1 bytes."""

        raw_data = bytearray(_fixture_raw_data())
        raw_data[81] = 2

        result = decode_pump_bonding_curve_account(
            _request(overrides=_RequestOverrides(raw_account_data=bytes(raw_data)))
        )

        self.assert_abstains(result, AbstainReason.UNSUPPORTED_PROTOCOL_STATE)

    def test_nonzero_trailing_bytes_abstain(self) -> None:
        """Trailing account bytes are accepted only as zero padding."""

        raw_data = bytearray(_fixture_raw_data())
        raw_data[CURRENT_LAYOUT_SIZE] = 1

        result = decode_pump_bonding_curve_account(
            _request(overrides=_RequestOverrides(raw_account_data=bytes(raw_data)))
        )

        self.assert_abstains(result, AbstainReason.UNSUPPORTED_PROTOCOL_STATE)

    def test_owner_mismatch_abstains(self) -> None:
        """Only Pump-owned bonding-curve accounts are decoded."""

        result = decode_pump_bonding_curve_account(
            _request(
                overrides=_RequestOverrides(
                    owner_program_id="11111111111111111111111111111111"
                )
            )
        )

        self.assert_abstains(result, AbstainReason.UNKNOWN_PROTOCOL_STATE)

    def test_missing_protocol_snapshot_abstains(self) -> None:
        """Protocol/config version provenance is required."""

        result = decode_pump_bonding_curve_account(
            _request(overrides=_RequestOverrides(protocol_snapshot=None))
        )

        self.assert_abstains(result, AbstainReason.UNKNOWN_PROTOCOL_STATE)

    def test_protocol_slot_mismatch_abstains(self) -> None:
        """Account state and protocol snapshot must share one as_of_slot."""

        result = decode_pump_bonding_curve_account(
            _request(
                overrides=_RequestOverrides(
                    protocol_snapshot=_protocol_snapshot(as_of_slot=Slot(1))
                )
            )
        )

        self.assert_abstains(result, AbstainReason.STALE_STATE)

    def test_idl_hash_mismatch_abstains(self) -> None:
        """IDL hash must match the pinned account decoder."""

        result = decode_pump_bonding_curve_account(
            _request(overrides=_RequestOverrides(idl_hash="bad-idl"))
        )

        self.assert_abstains(result, AbstainReason.DECODER_MISMATCH)

    def test_decoder_version_mismatch_abstains_before_snapshot_decode(self) -> None:
        """Only the pinned account decoder may label a snapshot."""

        result = decode_pump_bonding_curve_account(
            _request(overrides=_RequestOverrides(decoder_version="other-decoder"))
        )

        self.assert_abstains(result, AbstainReason.DECODER_MISMATCH)

    def test_missing_decimals_abstains(self) -> None:
        """Decimals are explicit provenance, not inferred from mint strings."""

        result = decode_pump_bonding_curve_account(
            _request(overrides=_RequestOverrides(base_decimals=None))
        )

        self.assert_abstains(result, AbstainReason.UNKNOWN_PROTOCOL_STATE)

    def test_float_decimals_abstain(self) -> None:
        """Runtime float decimals must not reach quote snapshots."""

        result = decode_pump_bonding_curve_account(
            _request(overrides=_RequestOverrides(base_decimals=6.0))
        )

        self.assert_abstains(result, AbstainReason.UNSUPPORTED_PROTOCOL_STATE)

    def test_non_string_mint_provenance_abstains(self) -> None:
        """Mint provenance must be explicit non-empty strings."""

        result = decode_pump_bonding_curve_account(
            _request(overrides=_RequestOverrides(base_mint=123))
        )

        self.assert_abstains(result, AbstainReason.UNKNOWN_PROTOCOL_STATE)

    def test_missing_layout_artifact_abstains(self) -> None:
        """Layout support must be explicit and artifact-backed."""

        result = decode_pump_bonding_curve_account(
            _request(overrides=_RequestOverrides(layout_artifact_version=""))
        )

        self.assert_abstains(result, AbstainReason.UNKNOWN_PROTOCOL_STATE)

    def test_snapshot_adapter_abstains_on_decoder_mismatch(self) -> None:
        """PoolReserves adapter revalidates snapshot decoder provenance."""

        snapshot = cast(
            "PumpBondingCurveAccountSnapshot",
            decode_pump_bonding_curve_account(_request()),
        )
        mismatched_snapshot = replace(snapshot, decoder_version="other-decoder")

        result = bonding_curve_snapshot_to_pool_reserves(mismatched_snapshot)

        self.assert_abstains(result, AbstainReason.DECODER_MISMATCH)

    def test_snapshot_adapter_revalidates_account_and_market_provenance(
        self,
    ) -> None:
        """Manually constructed snapshots cannot bypass provenance checks."""

        snapshot = cast(
            "PumpBondingCurveAccountSnapshot",
            decode_pump_bonding_curve_account(_request()),
        )
        cases = (
            (
                "owner_program_id",
                "11111111111111111111111111111111",
                AbstainReason.UNKNOWN_PROTOCOL_STATE,
            ),
            ("layout_artifact_version", "", AbstainReason.UNKNOWN_PROTOCOL_STATE),
            ("source_artifact_version", "", AbstainReason.UNKNOWN_PROTOCOL_STATE),
            ("base_mint", "", AbstainReason.UNKNOWN_PROTOCOL_STATE),
            ("base_decimals", 6.0, AbstainReason.UNSUPPORTED_PROTOCOL_STATE),
            ("complete", "", AbstainReason.UNSUPPORTED_PROTOCOL_STATE),
            (
                "virtual_token_reserves",
                1.5,
                AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
            ),
        )

        for field_name, value, reason in cases:
            with self.subTest(field_name=field_name):
                result = bonding_curve_snapshot_to_pool_reserves(
                    replace(snapshot, **{field_name: value})
                )

                self.assert_abstains(result, reason)

    def test_decoder_stays_pure_and_integer_only(self) -> None:
        """Account decoding must not grow adapters, signers, or float paths."""

        source = DECODER_MODULE.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(DECODER_MODULE))
        violations = [
            imported_name
            for imported_name in _imported_module_names(tree)
            if imported_name.startswith(FORBIDDEN_IMPORT_PREFIXES)
        ]

        self.assertEqual(violations, [])
        for token in _forbidden_source_tokens():
            with self.subTest(token=token):
                self.assertNotIn(token, source)

    def test_checked_in_pump_idl_matches_pinned_hash_and_layout(self) -> None:
        """The decoder pin matches the local Pump IDL account artifact."""

        idl_payload = json.loads(PUMP_IDL_PATH.read_text(encoding="utf-8"))
        idl_hash = hashlib.sha256(PUMP_IDL_PATH.read_bytes()).hexdigest()
        account_discriminator = _idl_account_discriminator(idl_payload)
        fields = _idl_bonding_curve_fields(idl_payload)

        self.assertEqual(idl_hash, PINNED_PUMP_IDL_SHA256)
        self.assertEqual(account_discriminator, list(BONDING_CURVE_DISCRIMINATOR))
        self.assertEqual(
            fields,
            [
                ("virtual_token_reserves", "u64"),
                ("virtual_sol_reserves", "u64"),
                ("real_token_reserves", "u64"),
                ("real_sol_reserves", "u64"),
                ("token_total_supply", "u64"),
                ("complete", "bool"),
                ("creator", "pubkey"),
                ("is_mayhem_mode", "bool"),
                ("is_cashback_coin", "bool"),
            ],
        )

    def assert_abstains(self, result: object, reason: AbstainReason) -> None:
        self.assertIsInstance(result, AbstainResult)
        if isinstance(result, AbstainResult):
            self.assertEqual(result.reason, reason)
            self.assertEqual(result.as_of_slot, _fixture()["rpc"]["context_slot"])


@dataclass(frozen=True, slots=True)
class _RequestOverrides:
    raw_account_data: bytes | None = None
    owner_program_id: str = PUMP_PROGRAM_ID
    protocol_snapshot: PumpProtocolVersionSnapshot | None | object = "__default__"
    idl_hash: str = PINNED_PUMP_IDL_SHA256
    base_decimals: int | object | None = 6
    quote_decimals: int | object | None = 9
    base_mint: str | object | None = BASE_MINT
    quote_mint: str | object | None = QUOTE_MINT
    layout_artifact_version: str = PUMP_BONDING_CURVE_LAYOUT_ARTIFACT_VERSION
    decoder_version: str = PUMP_BONDING_CURVE_ACCOUNT_DECODER_VERSION


def _request(
    *,
    overrides: _RequestOverrides | None = None,
) -> PumpBondingCurveDecodeRequest:
    values = overrides or _RequestOverrides()
    fixture = _fixture()
    as_of_slot = Slot(fixture["rpc"]["context_slot"])
    selected_protocol_snapshot = (
        _protocol_snapshot(as_of_slot=as_of_slot)
        if values.protocol_snapshot == "__default__"
        else values.protocol_snapshot
    )
    return PumpBondingCurveDecodeRequest(
        account_state=PumpBondingCurveAccountState(
            as_of_slot=as_of_slot,
            account_pubkey=fixture["account"]["pubkey"],
            owner_program_id=values.owner_program_id,
            raw_account_data=values.raw_account_data or _fixture_raw_data(),
            source_artifact_version=SOURCE_ARTIFACT_VERSION,
            layout_artifact_version=values.layout_artifact_version,
        ),
        protocol_snapshot=selected_protocol_snapshot,
        idl_hash=values.idl_hash,
        base_decimals=values.base_decimals,
        quote_decimals=values.quote_decimals,
        base_mint=values.base_mint,
        quote_mint=values.quote_mint,
        decoder_version=values.decoder_version,
    )


def _protocol_snapshot(*, as_of_slot: Slot) -> PumpProtocolVersionSnapshot:
    return PumpProtocolVersionSnapshot(
        as_of_slot=as_of_slot,
        program_id=PUMP_PROGRAM_ID,
        idl_hash=PINNED_PUMP_IDL_SHA256,
        global_config_hash="fixture-global-config-hash",
        program_config_version="pump-global-v1",
        fee_config=_fee_config(as_of_slot),
        program_config_source_artifact_version="program-config-artifact-v1",
        fee_source_artifact_version="fee-artifact-v1",
        registry_version="pump-version-registry-v1",
    )


def _fee_config(as_of_slot: Slot) -> FeeConfig:
    return FeeConfig(
        version="pump-fees-v1",
        protocol_fee_bps=100,
        creator_fee_bps=25,
        is_known=True,
        program_config_version="pump-global-v1",
        valid_from_slot=Slot(max(0, int(as_of_slot) - 1)),
        valid_to_slot=None,
        source_artifact_version="fee-artifact-v1",
    )


def _fixture_raw_data() -> bytes:
    fixture = _fixture()
    return base64.b64decode(fixture["account"]["data_base64"], validate=True)


def _old_learning_fixture_raw_data() -> bytes:
    payload = json.loads(
        Path(
            "fixtures/account_states/pump_bonding_curve/legacy_49_byte_layout.json"
        ).read_text(encoding="utf-8")
    )
    return base64.b64decode(payload["result"]["value"]["data"][0], validate=True)


def _fixture() -> dict[str, object]:
    return cast(
        "dict[str, object]",
        json.loads(FIXTURE_PATH.read_text(encoding="utf-8")),
    )


def _idl_account_discriminator(idl_payload: dict[str, object]) -> list[int]:
    accounts = cast("list[dict[str, object]]", idl_payload["accounts"])
    for account in accounts:
        if account["name"] == "BondingCurve":
            return cast("list[int]", account["discriminator"])
    raise AssertionError


def _idl_bonding_curve_fields(
    idl_payload: dict[str, object],
) -> list[tuple[str, str]]:
    types = cast("list[dict[str, object]]", idl_payload["types"])
    for item in types:
        if item["name"] != "BondingCurve":
            continue
        fields = cast("list[dict[str, object]]", item["type"]["fields"])
        return [
            (
                cast("str", field["name"]),
                cast("str", field["type"]),
            )
            for field in fields
        ]
    raise AssertionError


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
