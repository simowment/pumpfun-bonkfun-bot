"""Resolve exact Pump create evidence into a non-submitting paper port."""

from __future__ import annotations

from dataclasses import dataclass

from rugbot.domain.decisions import AbstainReason, AbstainResult
from rugbot.domain.quotes import QuotePath
from rugbot.execution.paper import PaperExecutionPort
from rugbot.execution.paper_simulator import PaperRoundTripSimulator, PaperStress
from rugbot.execution.ports import ExecutionPort
from rugbot.market_state.pump_create import PumpCreateMarketState
from rugbot.protocol.pump.bonding_curve_account import (
    PINNED_PUMP_IDL_SHA256,
)
from rugbot.protocol.pump.create_decoder import PUMP_CREATE_V2_DECODER_VERSION
from rugbot.protocol.pump.create_state_adapter import (
    PumpCreateMintMetadataProof,
    pump_create_snapshot_to_pool_reserves,
)
from rugbot.protocol.pump.quote_engine import PoolReserves
from rugbot.protocol.pump.version_registry import PumpProtocolVersionSnapshot


@dataclass(frozen=True, slots=True)
class PaperContextInput:
    """Immutable, point-in-time evidence for one Pump paper context."""

    market_state: PumpCreateMarketState | None
    protocol_snapshot: PumpProtocolVersionSnapshot | None
    mint_metadata: PumpCreateMintMetadataProof | None
    stress: PaperStress | None
    current_reserves: PoolReserves | None = None


PaperContextResult = ExecutionPort | AbstainResult


def resolve_paper_context(  # noqa: C901, PLR0911, PLR0912
    *, inputs: PaperContextInput
) -> PaperContextResult:
    """Build a paper execution port from exact Pump create-state evidence.

    The resolver performs no I/O and never supplies defaults for protocol,
    mint, or stress evidence. The returned port is backed by the deterministic
    round-trip simulator and cannot submit or sign a transaction.
    """

    if type(inputs) is not PaperContextInput:
        return _abstain(
            AbstainReason.UNKNOWN_PROTOCOL_STATE,
            "paper context input is malformed",
            -1,
        )

    market_state = inputs.market_state
    current_reserves = inputs.current_reserves
    if current_reserves is not None and market_state is not None:
        return _abstain(
            AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
            "paper context must use create state or current reserves, not both",
            _state_slot(market_state),
        )
    if current_reserves is not None:
        as_of_slot = _reserves_slot(current_reserves)
        if type(current_reserves) is not PoolReserves:
            return _abstain(
                AbstainReason.MISSING_FEATURE,
                "current Pump reserves are required",
                as_of_slot,
            )
    else:
        as_of_slot = _state_slot(market_state)
        if type(market_state) is not PumpCreateMarketState:
            return _abstain(
                AbstainReason.MISSING_FEATURE,
                "Pump create market state is required",
                as_of_slot,
            )
    if not isinstance(inputs.stress, PaperStress):
        return _abstain(
            AbstainReason.MISSING_FEATURE,
            "paper stress is required",
            as_of_slot,
        )
    stress_error = _validate_stress_slot(inputs.stress, as_of_slot)
    if stress_error is not None:
        return stress_error

    if current_reserves is None:
        if market_state is None:
            raise AssertionError
        reserves = pump_create_snapshot_to_pool_reserves(
            market_state.reserves,
            protocol_snapshot=inputs.protocol_snapshot,
            mint_metadata=inputs.mint_metadata,
            create_decoder_version=PUMP_CREATE_V2_DECODER_VERSION,
            create_idl_hash=PINNED_PUMP_IDL_SHA256,
        )
        if isinstance(reserves, AbstainResult):
            return reserves
    else:
        reserves = _validate_current_reserves(
            reserves=current_reserves,
            protocol_snapshot=inputs.protocol_snapshot,
            mint_metadata=inputs.mint_metadata,
            as_of_slot=as_of_slot,
        )
        if isinstance(reserves, AbstainResult):
            return reserves
    if inputs.protocol_snapshot is None:
        raise AssertionError

    simulator = PaperRoundTripSimulator(
        as_of_slot=as_of_slot,
        path=QuotePath.PUMP_BONDING_CURVE,
        reserves=reserves,
        fee_config=inputs.protocol_snapshot.fee_config,
        stress=inputs.stress,
    )
    return PaperExecutionPort(simulator)


def _validate_stress_slot(stress: PaperStress, as_of_slot: int) -> AbstainResult | None:
    latency = stress.latency_snapshot
    if latency is None:
        return _abstain(
            AbstainReason.MISSING_FEATURE,
            "paper latency snapshot is required",
            as_of_slot,
        )
    if latency.as_of_slot != as_of_slot:
        return _abstain(
            AbstainReason.STALE_STATE,
            "paper stress uses a different slot",
            as_of_slot,
        )
    return None


def _state_slot(market_state: object) -> int:
    if isinstance(market_state, PumpCreateMarketState):
        value = market_state.as_of_slot
        return value if type(value) is int else -1
    return -1


def _reserves_slot(reserves: object) -> int:
    if isinstance(reserves, PoolReserves):
        value = reserves.as_of_slot
        return value if type(value) is int else -1
    return -1


def _validate_current_reserves(  # noqa: PLR0911
    *,
    reserves: PoolReserves,
    protocol_snapshot: PumpProtocolVersionSnapshot | None,
    mint_metadata: PumpCreateMintMetadataProof | None,
    as_of_slot: int,
) -> PoolReserves | AbstainResult:
    if protocol_snapshot is None or type(protocol_snapshot) is not PumpProtocolVersionSnapshot:
        return _abstain(
            AbstainReason.MISSING_FEATURE,
            "protocol snapshot is required for current Pump reserves",
            as_of_slot,
        )
    if mint_metadata is None or type(mint_metadata) is not PumpCreateMintMetadataProof:
        return _abstain(
            AbstainReason.MISSING_FEATURE,
            "mint metadata is required for current Pump reserves",
            as_of_slot,
        )
    if reserves.as_of_slot != as_of_slot:
        return _abstain(
            AbstainReason.STALE_STATE,
            "current Pump reserves use a different slot",
            as_of_slot,
        )
    if protocol_snapshot.as_of_slot != as_of_slot:
        return _abstain(
            AbstainReason.STALE_STATE,
            "protocol snapshot uses a different slot",
            as_of_slot,
        )
    if mint_metadata.as_of_slot != as_of_slot:
        return _abstain(
            AbstainReason.STALE_STATE,
            "mint metadata uses a different slot",
            as_of_slot,
        )
    if (
        reserves.idl_hash != PINNED_PUMP_IDL_SHA256
        or reserves.program_config_version != protocol_snapshot.program_config_version
    ):
        return _abstain(
            AbstainReason.DECODER_MISMATCH,
            "current Pump reserves provenance is not pinned",
            as_of_slot,
        )
    return reserves


def _abstain(reason: AbstainReason, message: str, as_of_slot: int) -> AbstainResult:
    return AbstainResult(reason=reason, message=message, as_of_slot=as_of_slot)
