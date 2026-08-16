"""Integer quote engine tests."""

import ast
import json
import unittest
from pathlib import Path
from typing import Any, cast

from rugbot.domain.amounts import QuoteBaseUnits, Slot, TokenBaseUnits
from rugbot.domain.decisions import AbstainReason, AbstainResult
from rugbot.protocol.pump.bonding_curve_account import (
    PINNED_PUMP_IDL_SHA256,
    PUMP_BONDING_CURVE_ACCOUNT_DECODER_VERSION,
)
from rugbot.protocol.pump.quote_engine import (
    PUMP_SWAP_POOL_DECODER_VERSION,
    ExecutableQuote,
    FeeConfig,
    PoolReserves,
    QuotePath,
    executable_buy_quote,
    executable_sell_quote,
)
from rugbot.protocol.pump.swap_trade_decoder import (
    PINNED_PUMP_SWAP_IDL_SHA256,
)
from rugbot.protocol.pump.version_registry import (
    PumpFeeScheduleVersion,
    PumpProgramConfigVersion,
    PumpProtocolVersionSnapshot,
    PumpVersionResolveRequest,
    resolve_pump_protocol_versions,
)

QUOTE_ENGINE_MODULE = Path("src/rugbot/protocol/pump/quote_engine.py")
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


class IntegerQuoteEngineTests(unittest.TestCase):
    """Tests for integer-only executable quote contracts."""

    def test_buy_quote_uses_integer_fee_and_constant_product_math(self) -> None:
        """Buy quotes use integer base units and deterministic rounding."""

        reserves = _valid_reserves(as_of_slot=10)
        fee_config = _known_fee_config()

        quote = executable_buy_quote(
            path=QuotePath.PUMP_BONDING_CURVE,
            reserves=reserves,
            quote_input_amount=QuoteBaseUnits(10_000),
            fee_config=fee_config,
        )

        self.assertIsInstance(quote, ExecutableQuote)
        quote = cast("ExecutableQuote", quote)
        self.assertEqual(quote.fee_amount_base_units, 124)
        self.assertEqual(quote.output_amount_base_units, 19_367)
        self.assertEqual(quote.as_of_slot, 10)
        self.assertEqual(
            quote.decoder_version,
            PUMP_BONDING_CURVE_ACCOUNT_DECODER_VERSION,
        )
        self.assertEqual(quote.base_decimals, 6)
        self.assertEqual(quote.quote_decimals, 9)
        self.assertEqual(quote.idl_hash, PINNED_PUMP_IDL_SHA256)
        self.assertEqual(quote.program_config_version, "pump-global-v1")

    def test_sell_quote_uses_integer_fee_and_constant_product_math(self) -> None:
        """Sell quotes use integer base units and deterministic rounding."""

        reserves = _valid_reserves(as_of_slot=11)
        fee_config = _known_fee_config()

        quote = executable_sell_quote(
            path=QuotePath.PUMP_BONDING_CURVE,
            reserves=reserves,
            base_input_amount=TokenBaseUnits(20_000),
            fee_config=fee_config,
        )

        self.assertIsInstance(quote, ExecutableQuote)
        quote = cast("ExecutableQuote", quote)
        self.assertEqual(quote.fee_amount_base_units, 124)
        self.assertEqual(quote.output_amount_base_units, 9_679)
        self.assertEqual(quote.fee_config_version, "test-fees")

    def test_unknown_fee_config_abstains(self) -> None:
        """Unknown fee configuration returns ABSTAIN."""

        reserves = _valid_reserves(as_of_slot=12)

        quote = executable_buy_quote(
            path=QuotePath.PUMP_BONDING_CURVE,
            reserves=reserves,
            quote_input_amount=QuoteBaseUnits(10_000),
            fee_config=None,
        )

        self.assertIsInstance(quote, AbstainResult)
        quote = cast("AbstainResult", quote)
        self.assertEqual(quote.reason, AbstainReason.UNKNOWN_FEE_CONFIG)
        self.assertEqual(quote.as_of_slot, 12)

    def test_unverified_fee_config_abstains(self) -> None:
        """Unverified fee configurations are not executable."""

        quote = executable_buy_quote(
            path=QuotePath.PUMP_BONDING_CURVE,
            reserves=_valid_reserves(as_of_slot=13),
            quote_input_amount=QuoteBaseUnits(10_000),
            fee_config=FeeConfig(
                version="unknown",
                protocol_fee_bps=100,
                creator_fee_bps=25,
                is_known=False,
            ),
        )

        self.assertIsInstance(quote, AbstainResult)
        quote = cast("AbstainResult", quote)
        self.assertEqual(quote.reason, AbstainReason.UNKNOWN_FEE_CONFIG)
        self.assertEqual(quote.as_of_slot, 13)

    def test_invalid_fee_bps_abstains(self) -> None:
        """Invalid fee ranges abstain instead of producing negative output."""

        quote = executable_sell_quote(
            path=QuotePath.PUMP_BONDING_CURVE,
            reserves=_valid_reserves(as_of_slot=14),
            base_input_amount=TokenBaseUnits(20_000),
            fee_config=FeeConfig(
                version="bad-fees",
                protocol_fee_bps=10_001,
                creator_fee_bps=0,
                is_known=True,
                program_config_version="pump-global-v1",
                valid_from_slot=Slot(0),
                source_artifact_version="fee-artifact-v1",
            ),
        )

        self.assertIsInstance(quote, AbstainResult)
        quote = cast("AbstainResult", quote)
        self.assertEqual(quote.reason, AbstainReason.UNSUPPORTED_PROTOCOL_STATE)
        self.assertEqual(quote.as_of_slot, 14)

    def test_non_integer_reserves_and_inputs_abstain(self) -> None:
        """Financial values are never truncated into executable quotes."""

        reserves = _valid_reserves(as_of_slot=14)
        malformed_reserves = PoolReserves(
            virtual_base_reserves=cast("Any", 1_000_000.5),
            virtual_quote_reserves=reserves.virtual_quote_reserves,
            real_base_reserves=reserves.real_base_reserves,
            real_quote_reserves=reserves.real_quote_reserves,
            is_complete=reserves.is_complete,
            as_of_slot=reserves.as_of_slot,
            base_decimals=reserves.base_decimals,
            quote_decimals=reserves.quote_decimals,
            decoder_version=reserves.decoder_version,
            idl_hash=reserves.idl_hash,
            program_config_version=reserves.program_config_version,
        )

        malformed_reserve_quote = executable_buy_quote(
            path=QuotePath.PUMP_BONDING_CURVE,
            reserves=malformed_reserves,
            quote_input_amount=QuoteBaseUnits(10_000),
            fee_config=_known_fee_config(),
        )
        self.assertIsInstance(malformed_reserve_quote, AbstainResult)

        malformed_input_quote = executable_buy_quote(
            path=QuotePath.PUMP_BONDING_CURVE,
            reserves=reserves,
            quote_input_amount=cast("Any", 10_000.5),
            fee_config=_known_fee_config(),
        )
        self.assertIsInstance(malformed_input_quote, AbstainResult)

    def test_malformed_completion_state_abstains(self) -> None:
        """Non-boolean completion state is unknown, not an open curve."""

        reserves = _valid_reserves(as_of_slot=14)
        malformed_reserves = PoolReserves(
            virtual_base_reserves=reserves.virtual_base_reserves,
            virtual_quote_reserves=reserves.virtual_quote_reserves,
            real_base_reserves=reserves.real_base_reserves,
            real_quote_reserves=reserves.real_quote_reserves,
            is_complete=cast("Any", 0),
            as_of_slot=reserves.as_of_slot,
            base_decimals=reserves.base_decimals,
            quote_decimals=reserves.quote_decimals,
            decoder_version=reserves.decoder_version,
            idl_hash=reserves.idl_hash,
            program_config_version=reserves.program_config_version,
        )

        quote = executable_buy_quote(
            path=QuotePath.PUMP_BONDING_CURVE,
            reserves=malformed_reserves,
            quote_input_amount=QuoteBaseUnits(10_000),
            fee_config=_known_fee_config(),
        )

        self.assertIsInstance(quote, AbstainResult)

    def test_float_fee_bps_abstains(self) -> None:
        """Runtime float fee bps must not produce executable quote amounts."""

        quote = executable_sell_quote(
            path=QuotePath.PUMP_BONDING_CURVE,
            reserves=_valid_reserves(as_of_slot=15),
            base_input_amount=TokenBaseUnits(20_000),
            fee_config=FeeConfig(
                version="bad-fees",
                protocol_fee_bps=100.5,
                creator_fee_bps=25,
                is_known=True,
                program_config_version="pump-global-v1",
                valid_from_slot=Slot(0),
                source_artifact_version="fee-artifact-v1",
            ),
        )

        self.assertIsInstance(quote, AbstainResult)
        quote = cast("AbstainResult", quote)
        self.assertEqual(quote.reason, AbstainReason.UNSUPPORTED_PROTOCOL_STATE)
        self.assertEqual(quote.as_of_slot, 15)

    def test_fee_config_without_program_version_abstains(self) -> None:
        """Known fee configs still need a scoped program config version."""

        quote = executable_sell_quote(
            path=QuotePath.PUMP_BONDING_CURVE,
            reserves=_valid_reserves(as_of_slot=16),
            base_input_amount=TokenBaseUnits(20_000),
            fee_config=FeeConfig(
                version="unbacked-fees",
                protocol_fee_bps=100,
                creator_fee_bps=25,
                is_known=True,
                valid_from_slot=Slot(0),
                source_artifact_version="fee-artifact-v1",
            ),
        )

        self.assertIsInstance(quote, AbstainResult)
        quote = cast("AbstainResult", quote)
        self.assertEqual(quote.reason, AbstainReason.UNKNOWN_FEE_CONFIG)
        self.assertEqual(quote.as_of_slot, 16)

    def test_fee_config_without_valid_from_slot_abstains(self) -> None:
        """Known fee configs still need an inclusive validity start slot."""

        quote = executable_sell_quote(
            path=QuotePath.PUMP_BONDING_CURVE,
            reserves=_valid_reserves(as_of_slot=17),
            base_input_amount=TokenBaseUnits(20_000),
            fee_config=FeeConfig(
                version="unbounded-fees",
                protocol_fee_bps=100,
                creator_fee_bps=25,
                is_known=True,
                program_config_version="pump-global-v1",
                source_artifact_version="fee-artifact-v1",
            ),
        )

        self.assertIsInstance(quote, AbstainResult)
        quote = cast("AbstainResult", quote)
        self.assertEqual(quote.reason, AbstainReason.UNKNOWN_FEE_CONFIG)
        self.assertEqual(quote.as_of_slot, 17)

    def test_fee_config_without_source_artifact_abstains(self) -> None:
        """Known fee configs still need artifact provenance."""

        quote = executable_sell_quote(
            path=QuotePath.PUMP_BONDING_CURVE,
            reserves=_valid_reserves(as_of_slot=18),
            base_input_amount=TokenBaseUnits(20_000),
            fee_config=FeeConfig(
                version="unbacked-fees",
                protocol_fee_bps=100,
                creator_fee_bps=25,
                is_known=True,
                program_config_version="pump-global-v1",
                valid_from_slot=Slot(0),
            ),
        )

        self.assertIsInstance(quote, AbstainResult)
        quote = cast("AbstainResult", quote)
        self.assertEqual(quote.reason, AbstainReason.UNKNOWN_FEE_CONFIG)
        self.assertEqual(quote.as_of_slot, 18)

    def test_fee_config_program_version_mismatch_abstains(self) -> None:
        """A fee schedule for a different program config is not executable."""

        quote = executable_sell_quote(
            path=QuotePath.PUMP_BONDING_CURVE,
            reserves=_valid_reserves(as_of_slot=19),
            base_input_amount=TokenBaseUnits(20_000),
            fee_config=FeeConfig(
                version="wrong-fees",
                protocol_fee_bps=100,
                creator_fee_bps=25,
                is_known=True,
                program_config_version="different-program-config",
                valid_from_slot=Slot(0),
                source_artifact_version="fee-artifact-v1",
            ),
        )

        self.assertIsInstance(quote, AbstainResult)
        quote = cast("AbstainResult", quote)
        self.assertEqual(quote.reason, AbstainReason.UNKNOWN_FEE_CONFIG)
        self.assertEqual(quote.as_of_slot, 19)

    def test_fee_config_validity_window_abstains(self) -> None:
        """Point-in-time fee schedules must include the quote slot."""

        quote = executable_sell_quote(
            path=QuotePath.PUMP_BONDING_CURVE,
            reserves=_valid_reserves(as_of_slot=20),
            base_input_amount=TokenBaseUnits(20_000),
            fee_config=FeeConfig(
                version="stale-fees",
                protocol_fee_bps=100,
                creator_fee_bps=25,
                is_known=True,
                program_config_version="pump-global-v1",
                valid_from_slot=Slot(1),
                valid_to_slot=Slot(20),
                source_artifact_version="fee-artifact-v1",
            ),
        )

        self.assertIsInstance(quote, AbstainResult)
        quote = cast("AbstainResult", quote)
        self.assertEqual(quote.reason, AbstainReason.UNKNOWN_FEE_CONFIG)
        self.assertEqual(quote.as_of_slot, 20)

    def test_missing_decoder_provenance_abstains(self) -> None:
        """Missing decoder provenance returns ABSTAIN."""

        reserves = PoolReserves(
            virtual_base_reserves=TokenBaseUnits(1_000_000),
            virtual_quote_reserves=QuoteBaseUnits(500_000),
            real_base_reserves=TokenBaseUnits(900_000),
            real_quote_reserves=QuoteBaseUnits(400_000),
            is_complete=False,
            as_of_slot=Slot(21),
            base_decimals=6,
            quote_decimals=9,
            decoder_version="",
            idl_hash="pump-idl-sha256",
            program_config_version="pump-global-v1",
        )

        quote = executable_buy_quote(
            path=QuotePath.PUMP_BONDING_CURVE,
            reserves=reserves,
            quote_input_amount=QuoteBaseUnits(10_000),
            fee_config=_known_fee_config(),
        )

        self.assertIsInstance(quote, AbstainResult)
        quote = cast("AbstainResult", quote)
        self.assertEqual(quote.reason, AbstainReason.DECODER_MISMATCH)
        self.assertEqual(quote.as_of_slot, 21)

    def test_fabricated_decoder_provenance_abstains(self) -> None:
        """A non-pinned decoder identifier cannot produce an executable quote."""

        reserves = _valid_reserves(as_of_slot=23)
        fabricated = PoolReserves(
            virtual_base_reserves=reserves.virtual_base_reserves,
            virtual_quote_reserves=reserves.virtual_quote_reserves,
            real_base_reserves=reserves.real_base_reserves,
            real_quote_reserves=reserves.real_quote_reserves,
            is_complete=reserves.is_complete,
            as_of_slot=reserves.as_of_slot,
            base_decimals=reserves.base_decimals,
            quote_decimals=reserves.quote_decimals,
            decoder_version="decoder-v1",
            idl_hash=reserves.idl_hash,
            program_config_version=reserves.program_config_version,
        )

        quote = executable_buy_quote(
            path=QuotePath.PUMP_BONDING_CURVE,
            reserves=fabricated,
            quote_input_amount=QuoteBaseUnits(10_000),
            fee_config=_known_fee_config(),
        )

        self.assertIsInstance(quote, AbstainResult)
        self.assertEqual(
            cast("AbstainResult", quote).reason, AbstainReason.DECODER_MISMATCH
        )

    def test_fabricated_idl_provenance_abstains(self) -> None:
        """A non-pinned IDL hash cannot produce an executable quote."""

        reserves = _valid_reserves(as_of_slot=24)
        fabricated = PoolReserves(
            virtual_base_reserves=reserves.virtual_base_reserves,
            virtual_quote_reserves=reserves.virtual_quote_reserves,
            real_base_reserves=reserves.real_base_reserves,
            real_quote_reserves=reserves.real_quote_reserves,
            is_complete=reserves.is_complete,
            as_of_slot=reserves.as_of_slot,
            base_decimals=reserves.base_decimals,
            quote_decimals=reserves.quote_decimals,
            decoder_version=reserves.decoder_version,
            idl_hash="pump-idl-sha256",
            program_config_version=reserves.program_config_version,
        )

        quote = executable_buy_quote(
            path=QuotePath.PUMP_BONDING_CURVE,
            reserves=fabricated,
            quote_input_amount=QuoteBaseUnits(10_000),
            fee_config=_known_fee_config(),
        )

        self.assertIsInstance(quote, AbstainResult)
        self.assertEqual(
            cast("AbstainResult", quote).reason, AbstainReason.DECODER_MISMATCH
        )

    def test_fabricated_program_config_provenance_abstains(self) -> None:
        """An unknown program config identifier cannot produce an executable quote."""

        reserves = _valid_reserves(as_of_slot=25)
        fabricated = PoolReserves(
            virtual_base_reserves=reserves.virtual_base_reserves,
            virtual_quote_reserves=reserves.virtual_quote_reserves,
            real_base_reserves=reserves.real_base_reserves,
            real_quote_reserves=reserves.real_quote_reserves,
            is_complete=reserves.is_complete,
            as_of_slot=reserves.as_of_slot,
            base_decimals=reserves.base_decimals,
            quote_decimals=reserves.quote_decimals,
            decoder_version=reserves.decoder_version,
            idl_hash=reserves.idl_hash,
            program_config_version="pump-global-v2",
        )

        quote = executable_buy_quote(
            path=QuotePath.PUMP_BONDING_CURVE,
            reserves=fabricated,
            quote_input_amount=QuoteBaseUnits(10_000),
            fee_config=_known_fee_config(),
        )

        self.assertIsInstance(quote, AbstainResult)
        self.assertEqual(
            cast("AbstainResult", quote).reason,
            AbstainReason.UNKNOWN_PROTOCOL_STATE,
        )

    def test_canonical_fixture_provenance_produces_executable_quote(self) -> None:
        """Pinned bonding-curve fixture provenance remains executable."""

        quote = executable_buy_quote(
            path=QuotePath.PUMP_BONDING_CURVE,
            reserves=_valid_reserves(as_of_slot=26),
            quote_input_amount=QuoteBaseUnits(10_000),
            fee_config=_known_fee_config(),
        )

        self.assertIsInstance(quote, ExecutableQuote)
        quote = cast("ExecutableQuote", quote)
        self.assertEqual(
            quote.decoder_version, PUMP_BONDING_CURVE_ACCOUNT_DECODER_VERSION
        )
        self.assertEqual(quote.idl_hash, PINNED_PUMP_IDL_SHA256)
        self.assertEqual(quote.program_config_version, "pump-global-v1")

    def test_pumpswap_golden_fixture_produces_executable_buy_quote(self) -> None:
        """A finalized PumpSwap event anchors effective-reserve quote math."""

        fixture = json.loads(
            Path("fixtures/expected_quotes/pump_swap_buy_exact_quote.json").read_text(
                encoding="utf-8"
            )
        )
        reserves = PoolReserves(
            virtual_base_reserves=fixture["virtual_base_reserves"],
            virtual_quote_reserves=(
                fixture["raw_quote_reserves"] + fixture["virtual_quote_reserves"]
            ),
            real_base_reserves=fixture["virtual_base_reserves"],
            real_quote_reserves=fixture["raw_quote_reserves"],
            is_complete=None,
            as_of_slot=fixture["as_of_slot"],
            base_decimals=fixture["base_decimals"],
            quote_decimals=fixture["quote_decimals"],
            decoder_version=fixture["decoder_version"],
            idl_hash=fixture["idl_hash"],
            program_config_version=fixture["program_config_version"],
        )
        fee_config = FeeConfig(
            version=fixture["fee_config_version"],
            protocol_fee_bps=fixture["protocol_fee_bps"],
            creator_fee_bps=fixture["creator_fee_bps"],
            lp_fee_bps=fixture["lp_fee_bps"],
            is_known=True,
            program_config_version=fixture["program_config_version"],
            valid_from_slot=fixture["as_of_slot"],
            source_artifact_version=fixture["fixture_id"],
        )

        quote = executable_buy_quote(
            path=QuotePath.CANONICAL_PUMPSWAP,
            reserves=reserves,
            quote_input_amount=fixture["quote_input_amount"],
            fee_config=fee_config,
        )

        self.assertIsInstance(quote, ExecutableQuote)
        quote = cast("ExecutableQuote", quote)
        self.assertEqual(quote.output_amount_base_units, 26757683)
        self.assertEqual(quote.fee_amount_base_units, 20939)
        self.assertEqual(quote.path, QuotePath.CANONICAL_PUMPSWAP)
        self.assertEqual(quote.decoder_version, PUMP_SWAP_POOL_DECODER_VERSION)
        self.assertEqual(quote.idl_hash, PINNED_PUMP_SWAP_IDL_SHA256)
        self.assertEqual(quote.program_config_version, "pump-amm-v1")

    def test_pumpswap_golden_reserves_quote_sell_with_all_fees(self) -> None:
        """The same finalized reserves support an integer sell quote."""

        fixture = json.loads(
            Path("fixtures/expected_quotes/pump_swap_buy_exact_quote.json").read_text(
                encoding="utf-8"
            )
        )
        reserves = PoolReserves(
            virtual_base_reserves=fixture["virtual_base_reserves"],
            virtual_quote_reserves=(
                fixture["raw_quote_reserves"] + fixture["virtual_quote_reserves"]
            ),
            real_base_reserves=fixture["virtual_base_reserves"],
            real_quote_reserves=fixture["raw_quote_reserves"],
            is_complete=None,
            as_of_slot=fixture["as_of_slot"],
            base_decimals=fixture["base_decimals"],
            quote_decimals=fixture["quote_decimals"],
            decoder_version=fixture["decoder_version"],
            idl_hash=fixture["idl_hash"],
            program_config_version=fixture["program_config_version"],
        )
        fee_config = FeeConfig(
            version=fixture["fee_config_version"],
            protocol_fee_bps=fixture["protocol_fee_bps"],
            creator_fee_bps=fixture["creator_fee_bps"],
            lp_fee_bps=fixture["lp_fee_bps"],
            is_known=True,
            program_config_version=fixture["program_config_version"],
            valid_from_slot=fixture["as_of_slot"],
            source_artifact_version=fixture["fixture_id"],
        )

        quote = executable_sell_quote(
            path=QuotePath.CANONICAL_PUMPSWAP,
            reserves=reserves,
            base_input_amount=TokenBaseUnits(26757683),
            fee_config=fee_config,
        )

        self.assertIsInstance(quote, ExecutableQuote)
        quote = cast("ExecutableQuote", quote)
        self.assertEqual(quote.output_amount_base_units, 6958075)
        self.assertEqual(quote.fee_amount_base_units, 20939)

    def test_registry_snapshot_fee_config_feeds_quote_provenance(self) -> None:
        """Resolved protocol versions feed executable quote provenance."""

        snapshot = resolve_pump_protocol_versions(
            request=PumpVersionResolveRequest(
                as_of_slot=Slot(23),
                program_id="6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P",
                idl_hash=PINNED_PUMP_IDL_SHA256,
                global_config_hash="pump-global-config-sha256",
            ),
            program_configs=(
                PumpProgramConfigVersion(
                    version="pump-global-v1",
                    program_id="6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P",
                    idl_hash=PINNED_PUMP_IDL_SHA256,
                    global_config_hash="pump-global-config-sha256",
                    valid_from_slot=Slot(1),
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
                    valid_from_slot=Slot(1),
                    valid_to_slot=None,
                    source_artifact_version="fee-artifact-v1",
                ),
            ),
        )
        self.assertIsInstance(snapshot, PumpProtocolVersionSnapshot)
        snapshot = cast("PumpProtocolVersionSnapshot", snapshot)

        quote = executable_buy_quote(
            path=QuotePath.PUMP_BONDING_CURVE,
            reserves=_valid_reserves(as_of_slot=23),
            quote_input_amount=QuoteBaseUnits(10_000),
            fee_config=snapshot.fee_config,
        )

        self.assertIsInstance(quote, ExecutableQuote)
        quote = cast("ExecutableQuote", quote)
        self.assertEqual(quote.as_of_slot, 23)
        self.assertEqual(quote.fee_config_version, "pump-fees-v1")
        self.assertEqual(
            quote.decoder_version,
            PUMP_BONDING_CURVE_ACCOUNT_DECODER_VERSION,
        )
        self.assertEqual(quote.idl_hash, snapshot.idl_hash)
        self.assertEqual(
            quote.program_config_version,
            snapshot.program_config_version,
        )

    def test_quote_engine_stays_pure_and_integer_only(self) -> None:
        """Quote logic must not grow adapter, signer, or float dependencies."""

        source = QUOTE_ENGINE_MODULE.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(QUOTE_ENGINE_MODULE))
        violations = [
            imported_name
            for imported_name in _imported_module_names(tree)
            if imported_name.startswith(FORBIDDEN_IMPORT_PREFIXES)
        ]

        self.assertEqual(violations, [])
        for token in _forbidden_source_tokens():
            with self.subTest(token=token):
                self.assertNotIn(token, source)


def _valid_reserves(*, as_of_slot: int) -> PoolReserves:
    return PoolReserves(
        virtual_base_reserves=TokenBaseUnits(1_000_000),
        virtual_quote_reserves=QuoteBaseUnits(500_000),
        real_base_reserves=TokenBaseUnits(900_000),
        real_quote_reserves=QuoteBaseUnits(400_000),
        is_complete=False,
        as_of_slot=Slot(as_of_slot),
        base_decimals=6,
        quote_decimals=9,
        decoder_version=PUMP_BONDING_CURVE_ACCOUNT_DECODER_VERSION,
        idl_hash=PINNED_PUMP_IDL_SHA256,
        program_config_version="pump-global-v1",
    )


def _known_fee_config() -> FeeConfig:
    return FeeConfig(
        version="test-fees",
        protocol_fee_bps=100,
        creator_fee_bps=25,
        is_known=True,
        program_config_version="pump-global-v1",
        valid_from_slot=Slot(0),
        valid_to_slot=None,
        source_artifact_version="fee-artifact-v1",
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
        "Wal" + "let",
        "PRIVATE" + "_KEY",
        "send" + "_transaction",
        "send" + "_raw_transaction",
        "float(",
    )


if __name__ == "__main__":
    unittest.main()
