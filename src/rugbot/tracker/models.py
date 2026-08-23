"""Re-exports for tracker data models and invariants backed by domain entities."""

from __future__ import annotations

from dataclasses import dataclass, field

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


__all__ = [
    "LAMPORTS_PER_SOL",
    "AlertOutboxRecord",
    "DecisionEvent",
    "DecisionKind",
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
    "TargetStrategy",
    "TrackedWallet",
    "TrackerConfig",
    "TransferRecord",
    "WalletAddress",
    "WalletRecord",
    "WalletStatus",
]
