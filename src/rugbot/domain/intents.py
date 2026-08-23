"""Durable economic execution intents and on-chain commitment lifecycle models."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    from rugbot.domain.amounts import Lamports, Slot

INTENT_SCHEMA_VERSION: Final[str] = "v1"


class EconomicLifecycleState(StrEnum):
    """Business lifecycle states of an economic order/position."""

    INTENT_CREATED = "intent_created"
    SIGNED = "signed"
    SUBMITTED = "submitted"
    CONFIRMED = "confirmed"
    POSITION_OPEN = "position_open"
    EXIT_INTENT = "exit_intent"
    EXIT_SUBMITTED = "exit_submitted"
    POSITION_CLOSED = "position_closed"
    RECONCILED = "reconciled"
    FAILED = "failed"
    ABSTAINED = "abstained"


class ChainCommitment(StrEnum):
    """Solana on-chain consensus commitment tiers observed from RPC/Geyser."""

    PROCESSED = "processed"
    CONFIRMED = "confirmed"
    FINALIZED = "finalized"


def compute_buy_intent_id(
    *,
    target_id: str,
    mint: str,
    launch_signature: str,
    instruction_index: int = 0,
    amount_lamports: int,
) -> str:
    """Generate a deterministic, idempotent intent identifier for one launch entry.

    Ensures exactly-once economic execution: identical launch events for the
    same target and quote size produce the identical intent_id, preventing
    duplicate purchases across retries or RPC timeouts.
    """
    raw_key = (
        f"{target_id}:{mint}:{launch_signature}:{instruction_index}:{amount_lamports}"
    )
    digest = hashlib.sha256(raw_key.encode("utf-8")).hexdigest()[:16]
    return f"buy-{digest}"


def compute_exit_intent_id(
    *,
    position_id: str,
    fraction_ppm: int,
    trigger_slot: int,
) -> str:
    """Generate a deterministic identifier for an exit intent."""
    raw_key = f"{position_id}:{fraction_ppm}:{trigger_slot}"
    digest = hashlib.sha256(raw_key.encode("utf-8")).hexdigest()[:16]
    return f"exit-{digest}"


@dataclass(frozen=True, slots=True)
class BuyIntent:
    """Durable economic entry intent representing a qualified operator launch purchase."""

    id: str
    target_id: str
    mint: str
    launch_signature: str
    instruction_index: int
    amount_lamports: Lamports
    max_slippage_bps: int
    priority_fee_microlamports: int
    jito_tip_lamports: Lamports
    created_at_slot: Slot
    created_at_timestamp: int
    state: EconomicLifecycleState = EconomicLifecycleState.INTENT_CREATED
    tx_signature: str | None = None
    chain_commitment: ChainCommitment | None = None


@dataclass(frozen=True, slots=True)
class ExitIntent:
    """Durable economic exit intent representing a risk reduction or take-profit order."""

    id: str
    position_id: str
    market_id: str
    fraction_ppm: int
    reason: str
    created_at_slot: Slot
    created_at_timestamp: int
    state: EconomicLifecycleState = EconomicLifecycleState.EXIT_INTENT
    tx_signature: str | None = None
    chain_commitment: ChainCommitment | None = None


__all__ = [
    "INTENT_SCHEMA_VERSION",
    "BuyIntent",
    "ChainCommitment",
    "EconomicLifecycleState",
    "ExitIntent",
    "compute_buy_intent_id",
    "compute_exit_intent_id",
]
