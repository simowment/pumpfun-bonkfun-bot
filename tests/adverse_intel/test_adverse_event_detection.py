"""Adverse event detection and dump-attribution tests."""

import ast
import unittest
from dataclasses import replace
from pathlib import Path
from typing import Any, cast

from rugbot.domain.amounts import QuoteBaseUnits, Slot, TokenBaseUnits
from rugbot.domain.decisions import AbstainReason, AbstainResult
from rugbot.models.adverse_event import (
    AdverseEvent,
    AdverseEventDetection,
    AdverseEventDetectionConfig,
    CandidateDumpSell,
    DumpAttribution,
    DumpAttributionConfig,
    MarketTrajectoryPoint,
    attribute_dump_sells,
    detect_adverse_event,
)

DETECTION_MODULE = Path("src/rugbot/models/adverse_event.py")
TEST_MINT = "mint-1"
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


class AdverseEventDetectionTests(unittest.TestCase):
    """Tests for pure material adverse-event detection."""

    def test_detects_largest_material_drawdown(self) -> None:
        """Detector finds the strongest point-in-time collapse."""

        result = detect_adverse_event(
            points=(
                _point(slot=10, event_index=0, elapsed_ms=0, price=100_000),
                _point(slot=10, event_index=1, elapsed_ms=100, price=150_000),
                _point(slot=11, event_index=0, elapsed_ms=500, price=90_000),
                _point(slot=12, event_index=0, elapsed_ms=900, price=170_000),
                _point(slot=13, event_index=0, elapsed_ms=1_200, price=68_000),
                _point(slot=14, event_index=0, elapsed_ms=2_000, price=80_000),
            ),
            config=_detection_config(min_drawdown=500_000),
        )

        self.assertIsInstance(result, AdverseEventDetection)
        result = cast("AdverseEventDetection", result)
        self.assertIsNotNone(result.event)
        event = cast("AdverseEvent", result.event)
        self.assertEqual(event.peak_price_ppm, 170_000)
        self.assertEqual(event.trough_price_ppm, 68_000)
        self.assertEqual(event.collapse_start_slot, Slot(13))
        self.assertEqual(event.drawdown_ppm, 600_000)
        self.assertEqual(event.recovery_ppm, 176_470)
        self.assertEqual(result.reason_codes, ("material_adverse_event_detected",))

    def test_no_material_event_returns_explicit_no_event(self) -> None:
        """A non-collapsing trajectory is not an abstention."""

        result = detect_adverse_event(
            points=(
                _point(slot=10, event_index=0, elapsed_ms=0, price=100_000),
                _point(slot=11, event_index=0, elapsed_ms=500, price=95_000),
            ),
            config=_detection_config(min_drawdown=500_000),
        )

        self.assertIsInstance(result, AdverseEventDetection)
        result = cast("AdverseEventDetection", result)
        self.assertIsNone(result.event)
        self.assertEqual(result.reason_codes, ("no_material_adverse_event",))

    def test_missing_points_abstains(self) -> None:
        """Detector cannot infer a trajectory from missing evidence."""

        result = detect_adverse_event(points=(), config=_detection_config())

        self.assert_abstains(result, AbstainReason.MISSING_FEATURE, as_of_slot=20)

    def test_future_point_abstains(self) -> None:
        """A point newer than as_of_slot would leak future evidence."""

        result = detect_adverse_event(
            points=(_point(slot=21, event_index=0, elapsed_ms=0, price=100_000),),
            config=_detection_config(),
        )

        self.assert_abstains(result, AbstainReason.STALE_STATE, as_of_slot=20)

    def test_invalid_curve_progress_abstains(self) -> None:
        """Corrupt probability-like fields abstain before detection."""

        result = detect_adverse_event(
            points=(
                _point(
                    slot=10,
                    event_index=0,
                    elapsed_ms=0,
                    price=100_000,
                    curve_progress=1_000_001,
                ),
            ),
            config=_detection_config(),
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


class DumpAttributionTests(unittest.TestCase):
    """Tests for pure probabilistic dump-sell attribution."""

    def test_attributes_probable_dump_sells_in_window(self) -> None:
        """Attribution selects candidate sells by time window and probability."""

        event = _event(collapse_elapsed_ms=1_000)
        attribution = attribute_dump_sells(
            event=event,
            candidates=(
                _sell(slot=19, tx_index=0, elapsed_ms=900, wallet="wallet-a"),
                _sell(
                    slot=20,
                    tx_index=1,
                    elapsed_ms=1_100,
                    wallet="wallet-b",
                    cooperating_probability=820_000,
                    base_amount=2_000,
                    price_impact=150_000,
                ),
                _sell(slot=18, tx_index=0, elapsed_ms=100, wallet="too-early"),
                _sell(
                    slot=20,
                    tx_index=2,
                    elapsed_ms=1_050,
                    wallet="weak-link",
                    same_controller_probability=100_000,
                ),
            ),
            config=_attribution_config(),
        )

        self.assertIsInstance(attribution, DumpAttribution)
        attribution = cast("DumpAttribution", attribution)
        self.assertEqual(attribution.probable_dump_wallets, ("wallet-a", "wallet-b"))
        self.assertEqual(len(attribution.responsible_sells), 2)
        self.assertEqual(attribution.responsible_sells[0].as_of_slot, Slot(20))
        self.assertEqual(attribution.attributed_sell_base_units, TokenBaseUnits(3_000))
        self.assertEqual(attribution.max_sell_price_impact_ppm, 150_000)
        self.assertEqual(attribution.attribution_confidence_ppm, 900_000)
        self.assertEqual(attribution.reason_codes, ("dump_sells_attributed",))

    def test_weak_candidates_return_unattributed_result(self) -> None:
        """Weak candidate evidence does not become a confident attribution."""

        attribution = attribute_dump_sells(
            event=_event(collapse_elapsed_ms=1_000),
            candidates=(
                _sell(
                    slot=20,
                    tx_index=1,
                    elapsed_ms=1_050,
                    wallet="weak-link",
                    same_controller_probability=100_000,
                    cooperating_probability=120_000,
                ),
            ),
            config=_attribution_config(min_probability=800_000),
        )

        self.assertIsInstance(attribution, DumpAttribution)
        attribution = cast("DumpAttribution", attribution)
        self.assertEqual(attribution.responsible_sells, ())
        self.assertEqual(attribution.attribution_confidence_ppm, 0)
        self.assertEqual(
            attribution.reason_codes,
            ("no_candidate_above_attribution_threshold",),
        )

    def test_missing_candidates_abstains(self) -> None:
        """Attribution requires candidate sell evidence."""

        result = attribute_dump_sells(
            event=_event(collapse_elapsed_ms=1_000),
            candidates=(),
            config=_attribution_config(),
        )

        self.assert_abstains(result, AbstainReason.MISSING_FEATURE, as_of_slot=20)

    def test_candidate_slot_mismatch_abstains(self) -> None:
        """Attribution cannot mix candidate evidence from another snapshot."""

        result = attribute_dump_sells(
            event=_event(collapse_elapsed_ms=1_000),
            candidates=(_sell(slot=19, tx_index=0, elapsed_ms=900, slot_view=19),),
            config=_attribution_config(),
        )

        self.assert_abstains(result, AbstainReason.STALE_STATE, as_of_slot=20)

    def test_future_event_peak_slot_abstains(self) -> None:
        """Attribution rejects adverse events with future nested slots."""

        result = attribute_dump_sells(
            event=replace(_event(collapse_elapsed_ms=1_000), peak_slot=Slot(21)),
            candidates=(_sell(slot=19, tx_index=0, elapsed_ms=900),),
            config=_attribution_config(),
        )

        self.assert_abstains(result, AbstainReason.STALE_STATE, as_of_slot=20)

    def test_malformed_event_price_abstains(self) -> None:
        """Attribution rejects malformed adverse event evidence."""

        result = attribute_dump_sells(
            event=replace(_event(collapse_elapsed_ms=1_000), trough_price_ppm=0),
            candidates=(_sell(slot=19, tx_index=0, elapsed_ms=900),),
            config=_attribution_config(),
        )

        self.assert_abstains(
            result,
            AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
            as_of_slot=20,
        )

    def test_missing_evidence_ids_abstains(self) -> None:
        """Attributed sells must preserve source evidence IDs."""

        result = attribute_dump_sells(
            event=_event(collapse_elapsed_ms=1_000),
            candidates=(
                _sell(
                    slot=19,
                    tx_index=0,
                    elapsed_ms=900,
                    evidence_ids=(),
                ),
            ),
            config=_attribution_config(),
        )

        self.assert_abstains(result, AbstainReason.MISSING_FEATURE, as_of_slot=20)

    def test_float_trajectory_price_abstains(self) -> None:
        """Runtime validators reject float contract values."""

        result = detect_adverse_event(
            points=(
                _point(
                    slot=10,
                    event_index=0,
                    elapsed_ms=0,
                    price=cast("Any", 100_000.5),
                ),
            ),
            config=_detection_config(),
        )

        self.assert_abstains(
            result,
            AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
            as_of_slot=20,
        )

    def test_float_candidate_probability_abstains(self) -> None:
        """Float probability values do not pass integer ppm validation."""

        result = attribute_dump_sells(
            event=_event(collapse_elapsed_ms=1_000),
            candidates=(
                _sell(
                    slot=19,
                    tx_index=0,
                    elapsed_ms=900,
                    same_controller_probability=cast("Any", 0.5),
                ),
            ),
            config=_attribution_config(),
        )

        self.assert_abstains(
            result,
            AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
            as_of_slot=20,
        )

    def test_missing_attribution_version_abstains(self) -> None:
        """Attribution artifacts must be versioned."""

        result = attribute_dump_sells(
            event=_event(collapse_elapsed_ms=1_000),
            candidates=(_sell(slot=19, tx_index=0, elapsed_ms=900),),
            config=replace(_attribution_config(), attribution_version=""),
        )

        self.assert_abstains(result, AbstainReason.DECODER_MISMATCH, as_of_slot=20)

    def test_detector_module_stays_pure_and_integer_only(self) -> None:
        """Detection must not grow adapters, signers, or floats."""

        source = DETECTION_MODULE.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(DETECTION_MODULE))
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


def _detection_config(
    *,
    min_drawdown: int = 400_000,
) -> AdverseEventDetectionConfig:
    return AdverseEventDetectionConfig(
        as_of_slot=Slot(20),
        token_mint=TEST_MINT,
        detector_version="detector-v1",
        min_peak_price_ppm=1,
        min_drawdown_ppm=min_drawdown,
        recovery_window_ms=1_000,
    )


def _attribution_config(
    *,
    min_probability: int = 800_000,
) -> DumpAttributionConfig:
    return DumpAttributionConfig(
        as_of_slot=Slot(20),
        attribution_version="attribution-v1",
        pre_collapse_window_ms=250,
        post_collapse_window_ms=250,
        min_cluster_probability_ppm=min_probability,
    )


def _point(
    *,
    slot: int,
    event_index: int,
    elapsed_ms: int,
    price: int,
    curve_progress: int | None = 100_000,
) -> MarketTrajectoryPoint:
    return MarketTrajectoryPoint(
        as_of_slot=Slot(20),
        slot=Slot(slot),
        event_index=event_index,
        elapsed_ms=elapsed_ms,
        price_quote_base_units_per_token_base_unit_ppm=price,
        real_quote_reserves_base_units=QuoteBaseUnits(1_000_000),
        curve_progress_ppm=curve_progress,
    )


def _event(*, collapse_elapsed_ms: int) -> AdverseEvent:
    return AdverseEvent(
        as_of_slot=Slot(20),
        token_mint=TEST_MINT,
        collapse_start_slot=Slot(20),
        collapse_start_elapsed_ms=collapse_elapsed_ms,
        peak_slot=Slot(19),
        peak_elapsed_ms=500,
        peak_price_ppm=150_000,
        trough_slot=Slot(20),
        trough_elapsed_ms=collapse_elapsed_ms,
        trough_price_ppm=60_000,
        drawdown_ppm=600_000,
        recovery_ppm=0,
        detector_version="detector-v1",
        source_point_count=4,
    )


def _sell(**overrides: object) -> CandidateDumpSell:
    tx_index = _override_int(overrides, "tx_index", 0)
    return CandidateDumpSell(
        as_of_slot=Slot(_override_int(overrides, "slot_view", 20)),
        slot=Slot(_override_int(overrides, "slot", 19)),
        transaction_index=tx_index,
        signature=bytes([tx_index + 1]) * 64,
        elapsed_ms=_override_int(overrides, "elapsed_ms", 900),
        seller_wallet=_override_str(overrides, "wallet", "wallet-a"),
        base_amount_base_units=TokenBaseUnits(
            _override_int(overrides, "base_amount", 1_000)
        ),
        quote_amount_base_units=QuoteBaseUnits(500),
        price_impact_ppm=_override_int(overrides, "price_impact", 100_000),
        same_controller_probability_ppm=_override_int(
            overrides,
            "same_controller_probability",
            900_000,
        ),
        cooperating_wallet_probability_ppm=_override_int(
            overrides,
            "cooperating_probability",
            200_000,
        ),
        evidence_ids=cast(
            "tuple[str, ...]",
            overrides.get("evidence_ids", ("evidence-1",)),
        ),
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
