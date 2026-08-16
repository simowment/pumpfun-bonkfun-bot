"""Slot reconciliation tests."""

import unittest
from uuid import UUID

from rugbot.domain.observations import (
    CanonicalStatus,
    Commitment,
    RawChainObservation,
)
from rugbot.replay.slot_reconciliation import SlotReconciler

BOOT_ID = UUID("00000000-0000-0000-0000-000000000001")


class SlotReconcilerTests(unittest.TestCase):
    """Tests for processed to canonical slot state transitions."""

    def test_processed_slot_promotes_to_finalized_canonical(self) -> None:
        """A finalized update promotes a provisional processed slot."""

        reconciler = SlotReconciler()
        processed = _slot_observation(
            raw_id=UUID("00000000-0000-0000-0000-000000000101"),
            receive_sequence=1,
            commitment="processed",
            canonical_status="provisional",
        )
        finalized = _slot_observation(
            raw_id=UUID("00000000-0000-0000-0000-000000000102"),
            receive_sequence=2,
            commitment="finalized",
            canonical_status="canonical",
        )

        self.assertEqual(reconciler.apply(processed).canonical_status, "provisional")
        state = reconciler.apply(finalized)

        self.assertEqual(state.commitment, "finalized")
        self.assertEqual(state.canonical_status, "canonical")
        self.assertTrue(state.is_terminal)
        self.assertEqual(state.last_raw_id, finalized.raw_id)

    def test_terminal_canonical_slot_does_not_downgrade(self) -> None:
        """A later provisional duplicate cannot downgrade canonical state."""

        reconciler = SlotReconciler()
        finalized = _slot_observation(
            raw_id=UUID("00000000-0000-0000-0000-000000000103"),
            receive_sequence=3,
            commitment="finalized",
            canonical_status="canonical",
        )
        processed = _slot_observation(
            raw_id=UUID("00000000-0000-0000-0000-000000000104"),
            receive_sequence=4,
            commitment="processed",
            canonical_status="provisional",
        )

        reconciler.apply(finalized)
        state = reconciler.apply(processed)

        self.assertEqual(state.commitment, "finalized")
        self.assertEqual(state.canonical_status, "canonical")
        self.assertEqual(state.last_raw_id, finalized.raw_id)

    def test_processed_slot_can_be_marked_dead_fork(self) -> None:
        """A dead fork status terminally removes provisional slot data."""

        reconciler = SlotReconciler()
        processed = _slot_observation(
            raw_id=UUID("00000000-0000-0000-0000-000000000105"),
            receive_sequence=5,
            commitment="processed",
            canonical_status="provisional",
        )
        dead = _slot_observation(
            raw_id=UUID("00000000-0000-0000-0000-000000000106"),
            receive_sequence=6,
            commitment="processed",
            canonical_status="dead_fork",
        )

        reconciler.apply(processed)
        state = reconciler.apply(dead)

        self.assertEqual(state.canonical_status, "dead_fork")
        self.assertTrue(state.is_terminal)
        self.assertEqual(state.last_raw_id, dead.raw_id)

    def test_confirmed_slot_does_not_downgrade_to_processed(self) -> None:
        """Commitment promotion is monotonic for provisional slot states."""

        reconciler = SlotReconciler()
        confirmed = _slot_observation(
            raw_id=UUID("00000000-0000-0000-0000-000000000107"),
            receive_sequence=7,
            commitment="confirmed",
            canonical_status="provisional",
        )
        processed = _slot_observation(
            raw_id=UUID("00000000-0000-0000-0000-000000000108"),
            receive_sequence=8,
            commitment="processed",
            canonical_status="provisional",
        )

        reconciler.apply(confirmed)
        state = reconciler.apply(processed)

        self.assertEqual(state.commitment, "confirmed")
        self.assertEqual(state.last_raw_id, confirmed.raw_id)


def _slot_observation(
    *,
    raw_id: UUID,
    receive_sequence: int,
    commitment: Commitment,
    canonical_status: CanonicalStatus,
) -> RawChainObservation:
    return RawChainObservation(
        raw_id=raw_id,
        source_id="geyser-main",
        observer_id="observer-1",
        boot_id=BOOT_ID,
        receive_sequence=receive_sequence,
        slot=100,
        parent_slot=99,
        blockhash=None,
        signature=None,
        transaction_index=None,
        outer_instruction_index=None,
        inner_instruction_group_index=None,
        inner_instruction_index=None,
        stack_height=None,
        event_ordinal=None,
        commitment=commitment,
        canonical_status=canonical_status,
        received_wall_ns=10,
        received_monotonic_ns=20,
        program_id=None,
        account_pubkey=None,
        account_owner_program_id=None,
        raw_transaction=None,
        raw_transaction_format=None,
        raw_account_data=None,
        account_write_version=None,
        source_update_kind="slot",
        raw_source_status=None,
        raw_source_payload=None,
        decoder_name=None,
        decoder_version=None,
        idl_hash=None,
    )


if __name__ == "__main__":
    unittest.main()
