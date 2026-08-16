"""Finalized account-state replay timeline tests."""

import ast
import hashlib
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
from rugbot.replay.account_state_timeline import (
    FINALIZED_ACCOUNT_STATE_TIMELINE_VERSION,
    FinalizedAccountStateTimeline,
    build_finalized_account_state_timeline,
)

ACCOUNT_TIMELINE_MODULE = Path("src/rugbot/replay/account_state_timeline.py")
BOOT_ID = UUID("00000000-0000-0000-0000-000000000001")
ACCOUNT_A = bytes([1]) * 32
ACCOUNT_B = bytes([2]) * 32
ACCOUNT_C = bytes([3]) * 32
OWNER_A = bytes([11]) * 32
OWNER_B = bytes([12]) * 32
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


class FinalizedAccountStateTimelineTests(unittest.TestCase):
    """Tests for pure canonical account-state timeline construction."""

    def test_builds_finalized_account_state_timeline_in_replay_order(self) -> None:
        """Finalized canonical account updates sort by slot and write version."""

        timeline = build_finalized_account_state_timeline(
            observations=(
                _account_observation(
                    raw_id_suffix=1,
                    slot=101,
                    account_write_version=0,
                    account_pubkey=ACCOUNT_C,
                ),
                _account_observation(
                    raw_id_suffix=2,
                    slot=100,
                    account_write_version=2,
                    account_pubkey=ACCOUNT_B,
                ),
                _account_observation(
                    raw_id_suffix=3,
                    slot=100,
                    account_write_version=1,
                    account_pubkey=ACCOUNT_A,
                ),
            ),
            as_of_slot=Slot(101),
        )

        self.assertIsInstance(timeline, FinalizedAccountStateTimeline)
        timeline = cast("FinalizedAccountStateTimeline", timeline)
        self.assertEqual(timeline.as_of_slot, 101)
        self.assertEqual(
            [
                (int(event.slot), event.account_write_version, event.account_pubkey)
                for event in timeline.events
            ],
            [(100, 1, ACCOUNT_A), (100, 2, ACCOUNT_B), (101, 0, ACCOUNT_C)],
        )
        self.assertEqual(
            [event.as_of_slot for event in timeline.events],
            [101, 101, 101],
        )
        self.assertEqual(
            timeline.timeline_version,
            FINALIZED_ACCOUNT_STATE_TIMELINE_VERSION,
        )

    def test_deduplicates_repeated_finalized_account_observation(self) -> None:
        """Repeated delivery of identical finalized account evidence is idempotent."""

        first = _account_observation(
            raw_id_suffix=4,
            overrides=_ObservationOverrides(raw_account_data=b"same-account"),
        )
        duplicate = _account_observation(
            raw_id_suffix=5,
            overrides=_ObservationOverrides(raw_account_data=b"same-account"),
        )

        timeline = build_finalized_account_state_timeline(
            observations=(first, duplicate),
            as_of_slot=Slot(100),
        )

        self.assertIsInstance(timeline, FinalizedAccountStateTimeline)
        timeline = cast("FinalizedAccountStateTimeline", timeline)
        self.assertEqual(len(timeline.events), 1)
        self.assertEqual(
            timeline.events[0].source_raw_ids,
            (first.raw_id, duplicate.raw_id),
        )
        self.assertEqual(timeline.source_observation_count, 2)
        self.assertEqual(timeline.deduped_observation_count, 1)

    def test_same_slot_write_version_different_accounts_are_distinct(self) -> None:
        """Account pubkey is part of finalized account-state identity."""

        timeline = build_finalized_account_state_timeline(
            observations=(
                _account_observation(
                    raw_id_suffix=6,
                    account_pubkey=ACCOUNT_A,
                    account_write_version=7,
                ),
                _account_observation(
                    raw_id_suffix=7,
                    account_pubkey=ACCOUNT_B,
                    account_write_version=7,
                ),
            ),
            as_of_slot=Slot(100),
        )

        self.assertIsInstance(timeline, FinalizedAccountStateTimeline)
        timeline = cast("FinalizedAccountStateTimeline", timeline)
        self.assertEqual(len(timeline.events), 2)
        self.assertEqual(
            {event.account_pubkey for event in timeline.events},
            {ACCOUNT_A, ACCOUNT_B},
        )

    def test_explicit_account_owner_takes_precedence_over_program_id(self) -> None:
        """Account replay uses explicit owner evidence when present."""

        timeline = build_finalized_account_state_timeline(
            observations=(
                _account_observation(
                    raw_id_suffix=27,
                    overrides=_ObservationOverrides(
                        program_id=OWNER_B,
                        owner_program_id=OWNER_A,
                    ),
                ),
            ),
            as_of_slot=Slot(100),
        )

        self.assertIsInstance(timeline, FinalizedAccountStateTimeline)
        timeline = cast("FinalizedAccountStateTimeline", timeline)
        self.assertEqual(timeline.events[0].owner_program_id, OWNER_A)

    def test_missing_explicit_account_owner_abstains(self) -> None:
        """Replay no longer treats program_id as account owner evidence."""

        timeline = build_finalized_account_state_timeline(
            observations=(
                _account_observation(
                    raw_id_suffix=28,
                    overrides=_ObservationOverrides(
                        program_id=OWNER_A,
                        owner_program_id=None,
                    ),
                ),
            ),
            as_of_slot=Slot(100),
        )

        self.assert_abstains(timeline, AbstainReason.MISSING_FEATURE, as_of_slot=100)

    def test_empty_account_data_is_valid_evidence(self) -> None:
        """A zero-length account data payload is evidence, not a missing value."""

        timeline = build_finalized_account_state_timeline(
            observations=(
                _account_observation(
                    raw_id_suffix=8,
                    overrides=_ObservationOverrides(raw_account_data=b""),
                ),
            ),
            as_of_slot=Slot(100),
        )

        self.assertIsInstance(timeline, FinalizedAccountStateTimeline)
        timeline = cast("FinalizedAccountStateTimeline", timeline)
        self.assertEqual(timeline.events[0].raw_account_data, b"")
        self.assertEqual(
            timeline.events[0].raw_account_data_sha256,
            hashlib.sha256(b"").hexdigest(),
        )

    def test_transaction_observation_without_account_bytes_is_ignored(self) -> None:
        """Non-account observations without account bytes are irrelevant."""

        timeline = build_finalized_account_state_timeline(
            observations=(
                _account_observation(
                    raw_id_suffix=9,
                    overrides=_ObservationOverrides(
                        source_update_kind="transaction",
                        raw_account_data=None,
                    ),
                ),
            ),
            as_of_slot=Slot(100),
        )

        self.assertIsInstance(timeline, FinalizedAccountStateTimeline)
        timeline = cast("FinalizedAccountStateTimeline", timeline)
        self.assertEqual(timeline.events, ())
        self.assertEqual(timeline.source_observation_count, 1)

    def test_provisional_account_observation_abstains(self) -> None:
        """Replay timelines are derived only from finalized canonical evidence."""

        timeline = build_finalized_account_state_timeline(
            observations=(
                _account_observation(
                    raw_id_suffix=10,
                    overrides=_ObservationOverrides(
                        commitment="processed",
                        canonical_status="provisional",
                    ),
                ),
            ),
            as_of_slot=Slot(100),
        )

        self.assert_abstains(timeline, AbstainReason.STALE_STATE, as_of_slot=100)

    def test_future_account_observation_abstains(self) -> None:
        """A point-in-time replay view cannot include future slots."""

        timeline = build_finalized_account_state_timeline(
            observations=(
                _account_observation(
                    raw_id_suffix=11,
                    overrides=_ObservationOverrides(slot=105),
                ),
            ),
            as_of_slot=Slot(104),
        )

        self.assert_abstains(timeline, AbstainReason.STALE_STATE, as_of_slot=104)

    def test_negative_account_slot_abstains(self) -> None:
        """Account replay slots must be valid non-negative Solana slots."""

        timeline = build_finalized_account_state_timeline(
            observations=(
                _account_observation(
                    raw_id_suffix=24,
                    overrides=_ObservationOverrides(slot=-1),
                ),
            ),
            as_of_slot=Slot(100),
        )

        self.assert_abstains(
            timeline,
            AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
            as_of_slot=100,
        )

    def test_non_account_update_with_raw_bytes_abstains(self) -> None:
        """Raw account bytes on a non-account observation are not replay evidence."""

        timeline = build_finalized_account_state_timeline(
            observations=(
                _account_observation(
                    raw_id_suffix=12,
                    overrides=_ObservationOverrides(source_update_kind="transaction"),
                ),
            ),
            as_of_slot=Slot(100),
        )

        self.assert_abstains(
            timeline,
            AbstainReason.MISSING_FEATURE,
            as_of_slot=100,
        )

    def test_account_observation_missing_raw_bytes_abstains(self) -> None:
        """Account observations require raw account bytes."""

        timeline = build_finalized_account_state_timeline(
            observations=(
                _account_observation(
                    raw_id_suffix=13,
                    overrides=_ObservationOverrides(raw_account_data=None),
                ),
            ),
            as_of_slot=Slot(100),
        )

        self.assert_abstains(timeline, AbstainReason.MISSING_FEATURE, as_of_slot=100)

    def test_future_account_missing_raw_bytes_still_abstains_stale(self) -> None:
        """Future account rows are stale before raw-byte checks."""

        timeline = build_finalized_account_state_timeline(
            observations=(
                _account_observation(
                    raw_id_suffix=14,
                    overrides=_ObservationOverrides(slot=105, raw_account_data=None),
                ),
            ),
            as_of_slot=Slot(104),
        )

        self.assert_abstains(timeline, AbstainReason.STALE_STATE, as_of_slot=104)

    def test_processed_account_missing_raw_bytes_still_abstains_stale(self) -> None:
        """Provisional account rows are stale before raw-byte checks."""

        timeline = build_finalized_account_state_timeline(
            observations=(
                _account_observation(
                    raw_id_suffix=15,
                    overrides=_ObservationOverrides(
                        raw_account_data=None,
                        commitment="processed",
                        canonical_status="provisional",
                    ),
                ),
            ),
            as_of_slot=Slot(100),
        )

        self.assert_abstains(timeline, AbstainReason.STALE_STATE, as_of_slot=100)

    def test_missing_account_pubkey_abstains(self) -> None:
        """Account replay requires stable account identity."""

        timeline = build_finalized_account_state_timeline(
            observations=(
                _account_observation(
                    raw_id_suffix=16,
                    overrides=_ObservationOverrides(account_pubkey=None),
                ),
            ),
            as_of_slot=Slot(100),
        )

        self.assert_abstains(timeline, AbstainReason.MISSING_FEATURE, as_of_slot=100)

    def test_malformed_account_pubkey_abstains(self) -> None:
        """Account pubkeys must be valid Solana public-key bytes."""

        timeline = build_finalized_account_state_timeline(
            observations=(
                _account_observation(
                    raw_id_suffix=17,
                    overrides=_ObservationOverrides(account_pubkey=b"short"),
                ),
            ),
            as_of_slot=Slot(100),
        )

        self.assert_abstains(timeline, AbstainReason.MISSING_FEATURE, as_of_slot=100)

    def test_missing_owner_program_abstains(self) -> None:
        """Account replay requires owner program evidence."""

        timeline = build_finalized_account_state_timeline(
            observations=(
                _account_observation(
                    raw_id_suffix=18,
                    overrides=_ObservationOverrides(owner_program_id=None),
                ),
            ),
            as_of_slot=Slot(100),
        )

        self.assert_abstains(timeline, AbstainReason.MISSING_FEATURE, as_of_slot=100)

    def test_malformed_owner_program_abstains(self) -> None:
        """Owner program ids must be valid Solana public-key bytes."""

        timeline = build_finalized_account_state_timeline(
            observations=(
                _account_observation(
                    raw_id_suffix=25,
                    overrides=_ObservationOverrides(owner_program_id=b"short"),
                ),
            ),
            as_of_slot=Slot(100),
        )

        self.assert_abstains(timeline, AbstainReason.MISSING_FEATURE, as_of_slot=100)

    def test_missing_write_version_abstains(self) -> None:
        """Account replay requires stable write-version evidence."""

        timeline = build_finalized_account_state_timeline(
            observations=(
                _account_observation(
                    raw_id_suffix=19,
                    overrides=_ObservationOverrides(account_write_version=None),
                ),
            ),
            as_of_slot=Slot(100),
        )

        self.assert_abstains(timeline, AbstainReason.MISSING_FEATURE, as_of_slot=100)

    def test_negative_write_version_abstains(self) -> None:
        """Account write versions must be non-negative."""

        timeline = build_finalized_account_state_timeline(
            observations=(
                _account_observation(
                    raw_id_suffix=26,
                    overrides=_ObservationOverrides(account_write_version=-1),
                ),
            ),
            as_of_slot=Slot(100),
        )

        self.assert_abstains(timeline, AbstainReason.MISSING_FEATURE, as_of_slot=100)

    def test_conflicting_bytes_for_same_identity_abstains(self) -> None:
        """Duplicate finalized account identities must carry identical bytes."""

        timeline = build_finalized_account_state_timeline(
            observations=(
                _account_observation(
                    raw_id_suffix=20,
                    overrides=_ObservationOverrides(raw_account_data=b"account-a"),
                ),
                _account_observation(
                    raw_id_suffix=21,
                    overrides=_ObservationOverrides(raw_account_data=b"account-b"),
                ),
            ),
            as_of_slot=Slot(100),
        )

        self.assert_abstains(
            timeline,
            AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
            as_of_slot=100,
        )

    def test_conflicting_owner_for_same_identity_abstains(self) -> None:
        """Duplicate finalized account identities must carry identical owners."""

        timeline = build_finalized_account_state_timeline(
            observations=(
                _account_observation(
                    raw_id_suffix=22,
                    overrides=_ObservationOverrides(owner_program_id=OWNER_A),
                ),
                _account_observation(
                    raw_id_suffix=23,
                    overrides=_ObservationOverrides(owner_program_id=OWNER_B),
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
        """A slot boundary with no account observations is still explicit."""

        timeline = build_finalized_account_state_timeline(
            observations=(),
            as_of_slot=Slot(100),
        )

        self.assertIsInstance(timeline, FinalizedAccountStateTimeline)
        timeline = cast("FinalizedAccountStateTimeline", timeline)
        self.assertEqual(timeline.events, ())
        self.assertEqual(timeline.source_observation_count, 0)
        self.assertEqual(timeline.as_of_slot, 100)

    def test_negative_as_of_slot_abstains(self) -> None:
        """Point-in-time replay boundaries must be non-negative."""

        timeline = build_finalized_account_state_timeline(
            observations=(),
            as_of_slot=Slot(-1),
        )

        self.assert_abstains(
            timeline,
            AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
            as_of_slot=-1,
        )

    def test_missing_timeline_version_abstains(self) -> None:
        """Timeline builders require explicit version provenance."""

        timeline = build_finalized_account_state_timeline(
            observations=(),
            as_of_slot=Slot(100),
            timeline_version="",
        )

        self.assert_abstains(
            timeline,
            AbstainReason.DECODER_MISMATCH,
            as_of_slot=100,
        )

    def test_timeline_builder_stays_pure_and_integer_only(self) -> None:
        """Timeline construction must not grow adapters, signers, or floats."""

        source = ACCOUNT_TIMELINE_MODULE.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(ACCOUNT_TIMELINE_MODULE))
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
    account_pubkey: bytes | None = ACCOUNT_A
    program_id: bytes | None = None
    owner_program_id: bytes | None = OWNER_A
    account_write_version: int | None = 0
    raw_account_data: bytes | None = b"raw-account"
    commitment: Commitment = "finalized"
    canonical_status: CanonicalStatus = "canonical"
    source_update_kind: str | None = "account"


def _account_observation(
    *,
    raw_id_suffix: int,
    slot: int | None = None,
    account_pubkey: bytes | None = None,
    account_write_version: int | None = None,
    overrides: _ObservationOverrides | None = None,
) -> RawChainObservation:
    values = overrides or _ObservationOverrides()
    selected_slot = values.slot if slot is None else slot
    selected_account_pubkey = (
        values.account_pubkey if account_pubkey is None else account_pubkey
    )
    selected_account_write_version = (
        values.account_write_version
        if account_write_version is None
        else account_write_version
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
        signature=None,
        transaction_index=None,
        outer_instruction_index=None,
        inner_instruction_group_index=None,
        inner_instruction_index=None,
        stack_height=None,
        event_ordinal=None,
        commitment=values.commitment,
        canonical_status=values.canonical_status,
        received_wall_ns=10,
        received_monotonic_ns=20,
        program_id=values.program_id,
        account_pubkey=selected_account_pubkey,
        account_owner_program_id=values.owner_program_id,
        raw_transaction=None,
        raw_transaction_format=None,
        raw_account_data=values.raw_account_data,
        account_write_version=selected_account_write_version,
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
