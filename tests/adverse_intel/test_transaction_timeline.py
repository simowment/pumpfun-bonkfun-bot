"""Finalized transaction replay timeline tests."""

import ast
import unittest
from dataclasses import dataclass
from pathlib import Path
from typing import cast
from uuid import UUID

from rugbot.domain.amounts import Slot
from rugbot.domain.decisions import AbstainReason, AbstainResult
from rugbot.domain.observations import (
    CanonicalStatus,
    Commitment,
    RawChainObservation,
)
from rugbot.replay.transaction_timeline import (
    FINALIZED_TRANSACTION_TIMELINE_VERSION,
    FinalizedTransactionTimeline,
    build_finalized_transaction_timeline,
)

TIMELINE_MODULE = Path("src/rugbot/replay/transaction_timeline.py")
BOOT_ID = UUID("00000000-0000-0000-0000-000000000001")
RAW_TRANSACTION_FORMAT = "test-transaction-bytes"
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


class FinalizedTransactionTimelineTests(unittest.TestCase):
    """Tests for pure canonical transaction replay timeline construction."""

    def test_builds_finalized_transaction_timeline_in_replay_order(self) -> None:
        """Finalized canonical transactions sort by slot and transaction index."""

        timeline = build_finalized_transaction_timeline(
            observations=(
                _tx_observation(raw_id_suffix=1, slot=101, transaction_index=1),
                _tx_observation(raw_id_suffix=2, slot=100, transaction_index=2),
                _tx_observation(raw_id_suffix=3, slot=100, transaction_index=1),
            ),
            as_of_slot=Slot(101),
        )

        self.assertIsInstance(timeline, FinalizedTransactionTimeline)
        timeline = cast("FinalizedTransactionTimeline", timeline)
        self.assertEqual(timeline.as_of_slot, 101)
        self.assertEqual(
            [(int(event.slot), event.transaction_index) for event in timeline.events],
            [(100, 1), (100, 2), (101, 1)],
        )
        self.assertEqual(
            [event.as_of_slot for event in timeline.events],
            [101, 101, 101],
        )
        self.assertEqual(
            timeline.timeline_version,
            FINALIZED_TRANSACTION_TIMELINE_VERSION,
        )

    def test_deduplicates_repeated_finalized_transaction_observation(self) -> None:
        """Repeated delivery of identical finalized evidence is idempotent."""

        first = _tx_observation(
            raw_id_suffix=4,
            overrides=_ObservationOverrides(
                slot=102,
                signature=b"same-signature",
                raw_transaction=b"same-transaction",
            ),
        )
        duplicate = _tx_observation(
            raw_id_suffix=5,
            overrides=_ObservationOverrides(
                slot=102,
                signature=b"same-signature",
                raw_transaction=b"same-transaction",
            ),
        )

        timeline = build_finalized_transaction_timeline(
            observations=(first, duplicate),
            as_of_slot=Slot(102),
        )

        self.assertIsInstance(timeline, FinalizedTransactionTimeline)
        timeline = cast("FinalizedTransactionTimeline", timeline)
        self.assertEqual(len(timeline.events), 1)
        self.assertEqual(
            timeline.events[0].source_raw_ids,
            (first.raw_id, duplicate.raw_id),
        )
        self.assertEqual(
            timeline.events[0].raw_transaction_format, RAW_TRANSACTION_FORMAT
        )
        self.assertEqual(timeline.source_observation_count, 2)
        self.assertEqual(timeline.deduped_observation_count, 1)

    def test_provisional_transaction_observation_abstains(self) -> None:
        """Replay timelines are derived only from finalized canonical evidence."""

        timeline = build_finalized_transaction_timeline(
            observations=(
                _tx_observation(
                    raw_id_suffix=6,
                    overrides=_ObservationOverrides(
                        commitment="processed",
                        canonical_status="provisional",
                    ),
                ),
            ),
            as_of_slot=Slot(100),
        )

        self.assert_abstains(timeline, AbstainReason.STALE_STATE, as_of_slot=100)

    def test_future_transaction_observation_abstains(self) -> None:
        """A point-in-time replay view cannot include future slots."""

        timeline = build_finalized_transaction_timeline(
            observations=(
                _tx_observation(
                    raw_id_suffix=7,
                    overrides=_ObservationOverrides(slot=105),
                ),
            ),
            as_of_slot=Slot(104),
        )

        self.assert_abstains(timeline, AbstainReason.STALE_STATE, as_of_slot=104)

    def test_non_transaction_update_with_raw_bytes_abstains(self) -> None:
        """Raw bytes on a non-transaction observation are not replay evidence."""

        timeline = build_finalized_transaction_timeline(
            observations=(
                _tx_observation(
                    raw_id_suffix=13,
                    overrides=_ObservationOverrides(source_update_kind="account"),
                ),
            ),
            as_of_slot=Slot(100),
        )

        self.assert_abstains(
            timeline,
            AbstainReason.MISSING_FEATURE,
            as_of_slot=100,
        )

    def test_transaction_observation_missing_raw_bytes_abstains(self) -> None:
        """Transaction observations require raw transaction bytes."""

        timeline = build_finalized_transaction_timeline(
            observations=(
                _tx_observation(
                    raw_id_suffix=8,
                    overrides=_ObservationOverrides(raw_transaction=None),
                ),
            ),
            as_of_slot=Slot(100),
        )

        self.assert_abstains(timeline, AbstainReason.MISSING_FEATURE, as_of_slot=100)

    def test_future_transaction_missing_raw_bytes_still_abstains_stale(
        self,
    ) -> None:
        """Future transaction rows are stale before raw-byte checks."""

        timeline = build_finalized_transaction_timeline(
            observations=(
                _tx_observation(
                    raw_id_suffix=14,
                    overrides=_ObservationOverrides(
                        slot=105,
                        raw_transaction=None,
                    ),
                ),
            ),
            as_of_slot=Slot(104),
        )

        self.assert_abstains(timeline, AbstainReason.STALE_STATE, as_of_slot=104)

    def test_processed_transaction_missing_raw_bytes_still_abstains_stale(
        self,
    ) -> None:
        """Provisional transaction rows are stale before raw-byte checks."""

        timeline = build_finalized_transaction_timeline(
            observations=(
                _tx_observation(
                    raw_id_suffix=15,
                    overrides=_ObservationOverrides(
                        raw_transaction=None,
                        commitment="processed",
                        canonical_status="provisional",
                    ),
                ),
            ),
            as_of_slot=Slot(100),
        )

        self.assert_abstains(timeline, AbstainReason.STALE_STATE, as_of_slot=100)

    def test_missing_ordering_evidence_abstains(self) -> None:
        """Transaction replay requires stable transaction index and signature."""

        timeline = build_finalized_transaction_timeline(
            observations=(
                _tx_observation(
                    raw_id_suffix=16,
                    overrides=_ObservationOverrides(transaction_index=None),
                ),
            ),
            as_of_slot=Slot(100),
        )

        self.assert_abstains(timeline, AbstainReason.MISSING_FEATURE, as_of_slot=100)

    def test_missing_transaction_format_abstains(self) -> None:
        """Transaction replay requires raw byte format provenance."""

        timeline = build_finalized_transaction_timeline(
            observations=(
                _tx_observation(
                    raw_id_suffix=21,
                    overrides=_ObservationOverrides(raw_transaction_format=None),
                ),
            ),
            as_of_slot=Slot(100),
        )

        self.assert_abstains(timeline, AbstainReason.MISSING_FEATURE, as_of_slot=100)

    def test_conflicting_signature_for_replay_key_abstains(self) -> None:
        """One slot/index cannot map to two finalized transaction identities."""

        timeline = build_finalized_transaction_timeline(
            observations=(
                _tx_observation(
                    raw_id_suffix=17,
                    overrides=_ObservationOverrides(
                        signature=b"signature-a",
                        raw_transaction=b"transaction-a",
                    ),
                ),
                _tx_observation(
                    raw_id_suffix=18,
                    overrides=_ObservationOverrides(
                        signature=b"signature-b",
                        raw_transaction=b"transaction-b",
                    ),
                ),
            ),
            as_of_slot=Slot(100),
        )

        self.assert_abstains(
            timeline,
            AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
            as_of_slot=100,
        )

    def test_conflicting_bytes_for_same_identity_abstains(self) -> None:
        """Duplicate finalized identities must carry identical raw bytes."""

        timeline = build_finalized_transaction_timeline(
            observations=(
                _tx_observation(
                    raw_id_suffix=19,
                    overrides=_ObservationOverrides(
                        signature=b"same-signature",
                        raw_transaction=b"transaction-a",
                    ),
                ),
                _tx_observation(
                    raw_id_suffix=20,
                    overrides=_ObservationOverrides(
                        signature=b"same-signature",
                        raw_transaction=b"transaction-b",
                    ),
                ),
            ),
            as_of_slot=Slot(100),
        )

        self.assert_abstains(
            timeline,
            AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
            as_of_slot=100,
        )

    def test_conflicting_format_for_same_identity_abstains(self) -> None:
        """Duplicate finalized identities must carry identical byte formats."""

        timeline = build_finalized_transaction_timeline(
            observations=(
                _tx_observation(
                    raw_id_suffix=22,
                    overrides=_ObservationOverrides(
                        signature=b"same-signature",
                        raw_transaction=b"same-transaction",
                        raw_transaction_format="format-a",
                    ),
                ),
                _tx_observation(
                    raw_id_suffix=23,
                    overrides=_ObservationOverrides(
                        signature=b"same-signature",
                        raw_transaction=b"same-transaction",
                        raw_transaction_format="format-b",
                    ),
                ),
            ),
            as_of_slot=Slot(100),
        )

        self.assert_abstains(
            timeline,
            AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
            as_of_slot=100,
        )

    def test_empty_timeline_is_valid_point_in_time_snapshot(self) -> None:
        """A slot boundary with no transaction observations is still explicit."""

        timeline = build_finalized_transaction_timeline(
            observations=(),
            as_of_slot=Slot(100),
        )

        self.assertIsInstance(timeline, FinalizedTransactionTimeline)
        timeline = cast("FinalizedTransactionTimeline", timeline)
        self.assertEqual(timeline.events, ())
        self.assertEqual(timeline.source_observation_count, 0)
        self.assertEqual(timeline.as_of_slot, 100)

    def test_timeline_builder_stays_pure_and_integer_only(self) -> None:
        """Timeline construction must not grow adapters, signers, or floats."""

        source = TIMELINE_MODULE.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(TIMELINE_MODULE))
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
        if isinstance(result, AbstainResult):
            self.assertEqual(result.reason, reason)
            self.assertEqual(result.as_of_slot, as_of_slot)


@dataclass(frozen=True, slots=True)
class _ObservationOverrides:
    slot: int = 100
    transaction_index: int | None = 0
    signature: bytes | None = b"signature"
    raw_transaction: bytes | None = b"raw-transaction"
    raw_transaction_format: str | None = RAW_TRANSACTION_FORMAT
    commitment: Commitment = "finalized"
    canonical_status: CanonicalStatus = "canonical"
    source_update_kind: str | None = "transaction"


def _tx_observation(
    *,
    raw_id_suffix: int,
    slot: int | None = None,
    transaction_index: int | None = None,
    overrides: _ObservationOverrides | None = None,
) -> RawChainObservation:
    values = overrides or _ObservationOverrides()
    selected_slot = values.slot if slot is None else slot
    selected_transaction_index = (
        values.transaction_index if transaction_index is None else transaction_index
    )
    return RawChainObservation(
        raw_id=UUID(f"00000000-0000-0000-0000-{raw_id_suffix:012d}"),
        source_id="geyser-main",
        observer_id="observer-1",
        boot_id=BOOT_ID,
        receive_sequence=raw_id_suffix,
        slot=selected_slot,
        parent_slot=selected_slot - 1 if selected_slot > 0 else None,
        blockhash=None,
        signature=values.signature,
        transaction_index=selected_transaction_index,
        outer_instruction_index=None,
        inner_instruction_group_index=None,
        inner_instruction_index=None,
        stack_height=None,
        event_ordinal=None,
        commitment=values.commitment,
        canonical_status=values.canonical_status,
        received_wall_ns=10,
        received_monotonic_ns=20,
        program_id=b"pump-program",
        account_pubkey=None,
        account_owner_program_id=None,
        raw_transaction=values.raw_transaction,
        raw_transaction_format=values.raw_transaction_format,
        raw_account_data=None,
        account_write_version=None,
        source_update_kind=values.source_update_kind,
        raw_source_status=None,
        raw_source_payload=b"raw-source-payload",
        decoder_name=None,
        decoder_version=None,
        idl_hash=None,
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
