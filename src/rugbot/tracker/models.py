"""Re-exports for tracker data models and invariants backed by domain entities."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

from rugbot.domain.entities import (
    LAMPORTS_PER_SOL,
    AlertOutboxRecord,
    DecisionEvent,
    DecisionKind,
    FunderRecord,
    FundingEdge,
    FundingHop,
    FundingPath,
    Lamports,
    Launch,
    LaunchRecord,
    MintAddress,
    OperatorEntity,
    Signature,
    Slot,
    TargetExecutionMode,
    TargetExecutionPolicy,
    TargetRecord,
    TargetStrategy,
    TrackedWallet,
    TransferRecord,
    WalletAddress,
    WalletRecord,
    WalletStatus,
)


@dataclass(frozen=True, slots=True)
class TrackerConfig:
    """Deterministic tracker rules and bounds."""

    max_depth: int = 3
    descendant_ttl_seconds: int = 86400  # 24 hours
    min_transfer_lamports: int = 10_000_000  # 0.01 SOL
    blocked_intermediaries: frozenset[str] = field(default_factory=frozenset)


@dataclass(frozen=True, slots=True)
class TargetScanRecord:
    """One persisted scan event associated with a resolved entity."""

    query: str
    tracking_address: str | None
    token_symbol: str | None
    token_name: str | None
    scan_ok: bool
    launch_count: int
    linked_launch_count: int
    repeat_bundler_mint_count: int
    message: str
    first_scanned_at: str
    last_scanned_at: str
    scan_count: int = 1
    id: int | None = None


class EntityBackfillStatus(StrEnum):
    """Durable lifecycle for one bounded entity-history backfill."""

    PENDING = "pending"
    RUNNING = "running"
    RATE_LIMITED = "rate_limited"
    COMPLETE = "complete"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class EntityBackfillRecord:
    """Checkpoint and cached report for one entity-history request."""

    query: str
    wallet: str
    requested_transactions: int
    cached_transactions: int
    before_signature: str | None
    status: EntityBackfillStatus
    message: str
    report_json: str | None
    created_at: str
    updated_at: str


@dataclass(frozen=True, slots=True)
class BundleParticipationRecord:
    """One wallet's finalized creation-slot buy on one confirmed entity launch."""

    bundler_wallet: str
    mint: str
    creator: str
    creation_slot: int
    buy_signature: str
    transaction_index: int | None
    max_sol_cost_lamports: int


__all__ = [
    "LAMPORTS_PER_SOL",
    "AlertOutboxRecord",
    "BundleParticipationRecord",
    "DecisionEvent",
    "DecisionKind",
    "EntityBackfillRecord",
    "EntityBackfillStatus",
    "FunderRecord",
    "FundingEdge",
    "FundingHop",
    "FundingPath",
    "Lamports",
    "Launch",
    "LaunchRecord",
    "MintAddress",
    "OperatorEntity",
    "Signature",
    "Slot",
    "TargetExecutionMode",
    "TargetExecutionPolicy",
    "TargetRecord",
    "TargetScanRecord",
    "TargetStrategy",
    "TrackedWallet",
    "TrackerConfig",
    "TransferRecord",
    "WalletAddress",
    "WalletRecord",
    "WalletStatus",
]
