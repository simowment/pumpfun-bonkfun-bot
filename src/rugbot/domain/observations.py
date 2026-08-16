"""Raw chain observation contracts."""

from dataclasses import dataclass
from typing import Literal
from uuid import UUID

Commitment = Literal["processed", "confirmed", "finalized"]
CanonicalStatus = Literal["provisional", "canonical", "dead_fork", "replaced"]


@dataclass(frozen=True, slots=True)
class RawChainObservation:
    """Immutable source observation before canonical domain-event derivation.

    Args:
        raw_id: Unique identifier assigned by ingestion.
        source_id: Logical stream/source identifier.
        observer_id: Process or host observer identity.
        boot_id: Unique process boot identifier.
        receive_sequence: Monotonic per-source receive sequence.
        slot: Observed Solana slot.
        parent_slot: Optional parent slot when supplied by the stream.
        blockhash: Optional raw blockhash bytes.
        signature: Optional raw transaction signature bytes.
        transaction_index: Canonical transaction position when known.
        outer_instruction_index: Outer instruction index when known.
        inner_instruction_group_index: Inner instruction group when known.
        inner_instruction_index: Inner instruction index when known.
        stack_height: Instruction stack height when known.
        event_ordinal: Source-specific event ordinal when known.
        commitment: Stream commitment for this observation.
        canonical_status: Canonicalization state.
        received_wall_ns: Local wall-clock receive time in nanoseconds.
        received_monotonic_ns: Local monotonic receive time in nanoseconds.
        program_id: Program ID bytes when the observation is program-specific.
        account_pubkey: Account pubkey bytes for account-state observations.
        account_owner_program_id: Owner program bytes for account observations.
        raw_transaction: Raw transaction bytes when available.
        raw_transaction_format: Encoding/structure of raw transaction bytes.
        raw_account_data: Raw account data bytes when available.
        account_write_version: Geyser account write version when available.
        source_update_kind: Source-specific update kind.
        raw_source_status: Source-specific status enum value when available.
        raw_source_payload: Serialized source payload for future re-decode.
        decoder_name: Decoder that produced decoded fields, if any.
        decoder_version: Decoder version, if any.
        idl_hash: IDL hash used by decoder, if any.
    """

    raw_id: UUID
    source_id: str
    observer_id: str
    boot_id: UUID
    receive_sequence: int
    slot: int
    parent_slot: int | None
    blockhash: bytes | None
    signature: bytes | None
    transaction_index: int | None
    outer_instruction_index: int | None
    inner_instruction_group_index: int | None
    inner_instruction_index: int | None
    stack_height: int | None
    event_ordinal: int | None
    commitment: Commitment
    canonical_status: CanonicalStatus
    received_wall_ns: int
    received_monotonic_ns: int
    program_id: bytes | None
    account_pubkey: bytes | None
    account_owner_program_id: bytes | None
    raw_transaction: bytes | None
    raw_transaction_format: str | None
    raw_account_data: bytes | None
    account_write_version: int | None
    source_update_kind: str | None
    raw_source_status: int | None
    raw_source_payload: bytes | None
    decoder_name: str | None
    decoder_version: str | None
    idl_hash: str | None
