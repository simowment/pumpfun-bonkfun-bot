"""Domain types and unified models for adverse-intelligence workflows."""

from __future__ import annotations

from rugbot.domain.account_roles import AccountRoleProof
from rugbot.domain.adverse_event import (
    AdverseEvent,
    AdverseEventDetection,
    AdverseEventDetectionConfig,
    CandidateDumpSell,
    DumpAttribution,
    DumpAttributionConfig,
    MarketTrajectoryPoint,
    ResponsibleSell,
    attribute_dump_sells,
    detect_adverse_event,
)
from rugbot.domain.amounts import (
    PPM_SCALE,
    PROBABILITY_PPM_DENOMINATOR,
    ComputeUnitLimit,
    Lamports,
    MicroLamportsPerComputeUnit,
    QuoteBaseUnits,
    Slot,
    TokenBaseUnits,
)
from rugbot.domain.decisions import AbstainReason, AbstainResult
from rugbot.domain.entities import (
    AlertOutboxRecord,
    FunderRecord,
    LaunchRecord,
    MintAddress,
    OperatorEntity,
    Signature,
    TargetExecutionMode,
    TargetExecutionPolicy,
    TargetRecord,
    TransferRecord,
    WalletAddress,
    WalletRecord,
)
from rugbot.domain.fees import BASIS_POINTS_DENOMINATOR, FeeConfig
from rugbot.domain.intents import (
    BuyIntent,
    ChainCommitment,
    EconomicLifecycleState,
    ExitIntent,
    compute_buy_intent_id,
    compute_exit_intent_id,
)
from rugbot.domain.migrations import PumpMigrationInstructionEvidence
from rugbot.domain.observations import (
    CanonicalStatus,
    Commitment,
    RawChainObservation,
)
from rugbot.domain.outcome_labels import (
    HorizonOutcomeLabel,
    LaunchOutcomeLabels,
    OutcomeLabelConfig,
    OutcomeObservationPoint,
    build_launch_outcome_labels,
)
from rugbot.domain.positions import (
    CalibratedExitEvidence,
    PaperPositionState,
    PositionMarketEvidence,
)
from rugbot.domain.pump_market_state import (
    PumpBondingCurveAccountSnapshot,
    PumpCreateMarketState,
    PumpCreateMarketStateResult,
    PumpCreateReserveSnapshot,
    reconstruct_pump_create_market_state,
)
from rugbot.domain.trades import (
    PumpSwapTradeEventEvidence,
    PumpSwapTradeInstructionEvidence,
    PumpTradeInstructionEvidence,
    TradeSide,
)
from rugbot.domain.wallets import (
    TrackedWallet,
    WalletStatus,
)

__all__ = [
    "BASIS_POINTS_DENOMINATOR",
    "PPM_SCALE",
    "PROBABILITY_PPM_DENOMINATOR",
    "AbstainReason",
    "AbstainResult",
    "AccountRoleProof",
    "AdverseEvent",
    "AdverseEventDetection",
    "AdverseEventDetectionConfig",
    "AlertOutboxRecord",
    "BuyIntent",
    "CalibratedExitEvidence",
    "CandidateDumpSell",
    "CanonicalStatus",
    "ChainCommitment",
    "Commitment",
    "ComputeUnitLimit",
    "DumpAttribution",
    "DumpAttributionConfig",
    "EconomicLifecycleState",
    "ExitIntent",
    "FeeConfig",
    "FunderRecord",
    "HorizonOutcomeLabel",
    "Lamports",
    "LaunchOutcomeLabels",
    "LaunchRecord",
    "MarketTrajectoryPoint",
    "MicroLamportsPerComputeUnit",
    "MintAddress",
    "OperatorEntity",
    "OutcomeLabelConfig",
    "OutcomeObservationPoint",
    "PaperPositionState",
    "PositionMarketEvidence",
    "PumpBondingCurveAccountSnapshot",
    "PumpCreateMarketState",
    "PumpCreateMarketStateResult",
    "PumpCreateReserveSnapshot",
    "PumpMigrationInstructionEvidence",
    "PumpSwapTradeEventEvidence",
    "PumpSwapTradeInstructionEvidence",
    "PumpTradeInstructionEvidence",
    "QuoteBaseUnits",
    "RawChainObservation",
    "ResponsibleSell",
    "Signature",
    "Slot",
    "TargetExecutionMode",
    "TargetExecutionPolicy",
    "TargetRecord",
    "TokenBaseUnits",
    "TrackedWallet",
    "TradeSide",
    "TransferRecord",
    "WalletAddress",
    "WalletRecord",
    "WalletStatus",
    "attribute_dump_sells",
    "build_launch_outcome_labels",
    "compute_buy_intent_id",
    "compute_exit_intent_id",
    "detect_adverse_event",
    "reconstruct_pump_create_market_state",
]
