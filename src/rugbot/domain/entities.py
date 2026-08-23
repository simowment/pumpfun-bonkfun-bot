"""Domain entity models for operators, funders, launches, and execution policies."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import NewType

from rugbot.domain.amounts import Lamports, Slot

LAMPORTS_PER_SOL = 1_000_000_000

WalletAddress = NewType("WalletAddress", str)
MintAddress = NewType("MintAddress", str)
Signature = NewType("Signature", str)


class TargetExecutionMode(StrEnum):
    """Execution mode assigned to a tracked target wallet."""

    OFF = "off"
    SIMULATED = "simulated"
    LIVE = "live"


@dataclass(frozen=True, slots=True)
class TargetExecutionPolicy:
    """Persisted execution and risk policy owned by one tracked funder."""

    funder_address: str
    monitoring_enabled: bool
    execution_mode: TargetExecutionMode
    quote_size_lamports: int
    take_profit_pnl_ppm: int
    stop_loss_pnl_ppm: int
    max_slippage_bps: int
    priority_fee_microlamports: int
    jito_tip_lamports: int
    updated_at: str


class DecisionKind(StrEnum):
    """Canonical qualification and execution decision kinds."""

    PASS = "PASS"  # noqa: S105
    SKIP = "SKIP"
    EXEC = "EXEC"
    FAIL = "FAIL"


@dataclass(frozen=True, slots=True)
class DecisionEvent:
    """Canonical domain-evaluated decision event emitted when evaluating a launch candidate."""

    kind: DecisionKind
    token_symbol: str
    token_mint: str
    creator_wallet: str
    root_funder: str
    reason: str
    timestamp: str
    market_cap_usd: float | None = None
    winrate_pct: float | None = None
    block_number: int | None = None
    latency_summary: str | None = None
    order_size_sol: float | None = None


@dataclass(slots=True)
class TargetStrategy:
    """Target-specific execution, qualification strategy rules, and fee configuration."""

    monitoring_enabled: bool = True
    execution_mode: TargetExecutionMode = TargetExecutionMode.LIVE
    min_winrate_pct: float = 33.0
    max_entry_mc_usd: float = 15000.0
    required_block_zero: bool = True
    funding_match_required: bool = True
    size_sol: float = 0.010
    take_profit_pct: float = 100.0
    stop_loss_pct: float = -30.0
    max_consecutive_losses: int = 5
    max_exposure_pct: float = 5.0
    priority_fee_microlamports: int = 50_000
    jito_tip_sol: float = 0.0010
    slippage_bps: int = 500
    max_gas_sol: float = 0.0050

    @property
    def display_name(self) -> str:
        if not self.monitoring_enabled:
            return "PAUSED"
        if self.execution_mode == TargetExecutionMode.LIVE:
            return "LIVE"
        return "DRY RUN"

    @property
    def status_badge(self) -> str:
        if not self.monitoring_enabled:
            return "○ PAUSED"
        if self.execution_mode == TargetExecutionMode.LIVE:
            return "● LIVE"
        return "● DRY RUN"


@dataclass(slots=True)
class TargetRecord:
    """Tracked target dev/funder wallet entity with assigned strategy and track record."""

    address: str
    label: str = "Target Dev"
    policy: TargetExecutionPolicy | None = None
    strategy: TargetStrategy = field(default_factory=TargetStrategy)
    launches_count: int = 0
    winrate_pct: float = 0.0
    avg_ath_pct: float = 0.0
    perf_metric: str = "—"


@dataclass(frozen=True, slots=True)
class OperatorEntity:
    """Canonical representation of a tracked serial developer / funder entity."""

    address: str
    label: str
    root_funder: str
    launches_count: int = 0
    winrate_pct: float = 0.0
    avg_ath_multiplier: float = 1.0
    optimal_tp_multiplier: float = 1.0
    optimal_tp_label: str = "+0%"
    is_qualified: bool = False
    qualification_reason: str = ""
    discovered_at: str = ""
    policy: TargetExecutionPolicy | None = None
    sub_wallets: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True, slots=True)
class FunderRecord:
    """Registered root funder entity."""

    id: int | None
    address: str
    label: str
    enabled: bool
    created_at: str
    last_seen_at: str


class WalletStatus(StrEnum):
    """Lifecycle status of a tracked wallet within a funding tree."""

    FUNDER = "funder"
    FUNDED = "funded"
    CREATOR = "creator"
    EXPIRED = "expired"
    IGNORED = "ignored"


@dataclass(slots=True)
class WalletRecord:
    """In-memory and persisted tracked wallet node in the deterministic funding tree."""

    address: str
    root_funder: str
    parent_wallet: str | None
    depth: int
    status: WalletStatus
    discovered_at: str
    expires_at: str | None
    last_active_at: str


TrackedWallet = WalletRecord


@dataclass(frozen=True, slots=True)
class TransferRecord:
    """Cryptographically verified native SOL transfer edge between two addresses."""

    signature: str
    instruction_index: int
    slot: int
    timestamp: int
    from_wallet: str
    to_wallet: str
    amount_lamports: int
    amount_sol: float
    root_funder: str
    depth: int


FundingEdge = TransferRecord


@dataclass(frozen=True, slots=True)
class LaunchRecord:
    """Pump.fun token creation event linked deterministically to a funding root."""

    mint: str
    creator_wallet: str
    root_funder: str
    symbol: str
    name: str
    created_signature: str
    created_slot: int
    created_at: int
    depth: int
    funding_signature: str | None = None
    funding_amount_lamports: int | None = None
    funding_timestamp: int | None = None


Launch = LaunchRecord


@dataclass(frozen=True, slots=True)
class AlertOutboxRecord:
    """Durable cross-process alert delivery row keyed by launch mint and consumer."""

    mint: str
    consumer: str
    delivered: bool
    created_at: int


@dataclass(frozen=True, slots=True)
class FundingHop:
    """One single verified transfer hop along the funding chain."""

    from_wallet: str
    to_wallet: str
    amount_lamports: int
    amount_sol: float
    signature: str
    timestamp: int
    depth: int


@dataclass(frozen=True, slots=True)
class FundingPath:
    """Complete, cryptographic provenance tree from root funder to creator and token launch."""

    root_funder: str
    creator_wallet: str
    hops: tuple[FundingHop, ...]
    total_depth: int
    last_funding_timestamp: int | None
    launch_timestamp: int | None
    time_to_launch_seconds: int | None


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
    "TransferRecord",
    "WalletAddress",
    "WalletRecord",
    "WalletStatus",
]
