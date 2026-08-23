"""Derive finalized launch/trade joins from one immutable observation window."""

from __future__ import annotations

from typing import TYPE_CHECKING

from rugbot.backtest.trajectory.finalized_trade_builder import FinalizedTradeJoin
from rugbot.domain.decisions import AbstainReason, AbstainResult
from rugbot.domain.launches import LaunchCreatedV2
from rugbot.domain.observations import RawChainObservation
from rugbot.ingest.pump.pump_create_observation import decode_pump_create_v2_observation
from rugbot.ingest.pump.pump_swap_event_observation import (
    decode_pump_swap_events_observation,
)
from rugbot.ingest.pump.pump_swap_trade_observation import (
    decode_pump_swap_trade_observation,
)
from rugbot.ingest.pump.pump_trade_observation import decode_pump_trade_observation

if TYPE_CHECKING:
    from rugbot.domain.amounts import Slot


JoinDerivationResult = (
    tuple[tuple[LaunchCreatedV2, ...], tuple[FinalizedTradeJoin, ...]] | AbstainResult
)
TradeMintDiscoveryResult = tuple[str, ...] | AbstainResult


def derive_finalized_trade_joins(  # noqa: C901, PLR0911, PLR0912, PLR0915
    *,
    observations: tuple[RawChainObservation, ...],
    as_of_slot: Slot,
    eligible_mints: frozenset[str] | None = None,
) -> JoinDerivationResult:
    """Decode launches and derive unambiguous Pump trade joins.

    The launch is selected from the latest finalized create for the same mint
    that precedes the trade in transaction order.  A missing or ambiguous
    launch is an abstention; no mint-only guess is emitted.
    """

    cutoff = as_of_slot if type(as_of_slot) is int else -1
    if type(as_of_slot) is not int or as_of_slot < 0:
        return _abstain(
            AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
            "trade join cutoff must be a non-negative integer",
            cutoff,
        )
    if type(observations) is not tuple or any(
        type(item) is not RawChainObservation for item in observations
    ):
        return _abstain(
            AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
            "trade join observations are malformed",
            cutoff,
        )

    if any(
        observation.slot < 0 or observation.slot > cutoff
        for observation in observations
    ):
        return _abstain(
            AbstainReason.STALE_STATE,
            "trade join observations exceed the cutoff",
            cutoff,
        )
    ordered = tuple(sorted(observations, key=_observation_key))
    launches: list[LaunchCreatedV2] = []
    for observation in ordered:
        decoded_launch = decode_pump_create_v2_observation(observation)
        if isinstance(decoded_launch, AbstainResult):
            return decoded_launch
        if decoded_launch is not None:
            launches.append(decoded_launch)

    launches_by_mint: dict[str, list[LaunchCreatedV2]] = {}
    for launch in launches:
        launches_by_mint.setdefault(launch.mint_pubkey, []).append(launch)

    joins: list[FinalizedTradeJoin] = []
    seen: set[tuple[bytes, int]] = set()
    for observation in ordered:
        decoded_trades = decode_pump_trade_observation(observation)
        if isinstance(decoded_trades, AbstainResult):
            return decoded_trades
        for instruction in decoded_trades:
            if observation.signature is None:
                return _abstain(
                    AbstainReason.MISSING_FEATURE,
                    "trade observation signature is missing",
                    cutoff,
                )
            account_pubkeys = instruction.account_pubkeys
            if account_pubkeys is None:
                return _abstain(
                    AbstainReason.MISSING_FEATURE,
                    "trade account table is missing",
                    cutoff,
                )
            mint = _account_at(
                account_pubkeys,
                instruction.mint_account_index,
            )
            if mint is None:
                return _abstain(
                    AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
                    "trade mint or wallet account proof is incomplete",
                    cutoff,
                )
            if eligible_mints is not None and mint not in eligible_mints:
                continue
            wallet = _account_at(
                account_pubkeys,
                instruction.user_account_index,
            )
            if wallet is None:
                return _abstain(
                    AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
                    "trade mint or wallet account proof is incomplete",
                    cutoff,
                )
            trade_key = (observation.signature, instruction.outer_instruction_index)
            if trade_key in seen:
                return _abstain(
                    AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
                    "finalized trade instruction is duplicated",
                    cutoff,
                )
            launch = _launch_before_trade(
                launches_by_mint.get(mint, ()),
                observation=observation,
                trade_outer_instruction_index=instruction.outer_instruction_index,
            )
            if isinstance(launch, AbstainResult):
                return launch
            joins.append(
                FinalizedTradeJoin(
                    signature=observation.signature,
                    outer_instruction_index=instruction.outer_instruction_index,
                    launch_id=launch.launch_id,
                    token_mint=mint,
                    wallet=wallet,
                )
            )
            seen.add(trade_key)
        decoded_swap_trades = decode_pump_swap_trade_observation(observation)
        if isinstance(decoded_swap_trades, AbstainResult):
            return decoded_swap_trades
        if decoded_swap_trades:
            decoded_swap_events = decode_pump_swap_events_observation(observation)
            if isinstance(decoded_swap_events, AbstainResult):
                return decoded_swap_events
            for instruction in decoded_swap_trades:
                if observation.signature is None:
                    return _abstain(
                        AbstainReason.MISSING_FEATURE,
                        "Pump AMM trade observation signature is missing",
                        cutoff,
                    )
                account_pubkeys = instruction.account_pubkeys
                if account_pubkeys is None:
                    return _abstain(
                        AbstainReason.MISSING_FEATURE,
                        "Pump AMM trade account table is missing",
                        cutoff,
                    )
                mint = _account_at(
                    account_pubkeys,
                    instruction.base_mint_account_index,
                )
                if mint is None:
                    return _abstain(
                        AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
                        "Pump AMM trade mint or wallet proof is incomplete",
                        cutoff,
                    )
                if eligible_mints is not None and mint not in eligible_mints:
                    continue
                wallet = _account_at(
                    account_pubkeys,
                    instruction.user_account_index,
                )
                if wallet is None:
                    return _abstain(
                        AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
                        "Pump AMM trade mint or wallet proof is incomplete",
                        cutoff,
                    )
                matching_events = tuple(
                    event
                    for event in decoded_swap_events
                    if event.user == wallet
                    and event.side is instruction.side
                    and event.instruction_name == instruction.instruction_name
                )
                if len(matching_events) != 1:
                    return _abstain(
                        AbstainReason.MISSING_FEATURE,
                        "Pump AMM trade lacks one unambiguous finalized event",
                        cutoff,
                    )
                trade_key = (observation.signature, instruction.outer_instruction_index)
                if trade_key in seen:
                    return _abstain(
                        AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
                        "finalized trade instruction is duplicated",
                        cutoff,
                    )
                launch = _launch_before_trade(
                    launches_by_mint.get(mint, ()),
                    observation=observation,
                    trade_outer_instruction_index=instruction.outer_instruction_index,
                )
                if isinstance(launch, AbstainResult):
                    return launch
                joins.append(
                    FinalizedTradeJoin(
                        signature=observation.signature,
                        outer_instruction_index=instruction.outer_instruction_index,
                        launch_id=launch.launch_id,
                        token_mint=mint,
                        wallet=wallet,
                    )
                )
                seen.add(trade_key)
    return tuple(launches), tuple(joins)


def discover_finalized_trade_mints(  # noqa: C901, PLR0911
    *,
    observations: tuple[RawChainObservation, ...],
    as_of_slot: Slot,
) -> TradeMintDiscoveryResult:
    """Extract mints from proven Pump trade instructions for bounded lookup."""

    cutoff = as_of_slot if type(as_of_slot) is int else -1
    if type(as_of_slot) is not int or as_of_slot < 0:
        return _abstain(
            AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
            "trade mint cutoff must be a non-negative integer",
            cutoff,
        )
    if type(observations) is not tuple or any(
        type(item) is not RawChainObservation for item in observations
    ):
        return _abstain(
            AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
            "trade mint observations are malformed",
            cutoff,
        )
    discovered: set[str] = set()
    for observation in observations:
        if observation.slot < 0 or observation.slot > cutoff:
            return _abstain(
                AbstainReason.STALE_STATE,
                "trade observation exceeds the cutoff",
                cutoff,
            )
        decoded_trades = decode_pump_trade_observation(observation)
        if isinstance(decoded_trades, AbstainResult):
            return decoded_trades
        for instruction in decoded_trades:
            if instruction.account_pubkeys is None:
                return _abstain(
                    AbstainReason.MISSING_FEATURE,
                    "trade account table is missing",
                    cutoff,
                )
            mint = _account_at(
                instruction.account_pubkeys,
                instruction.mint_account_index,
            )
            if mint is None:
                return _abstain(
                    AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
                    "trade mint account proof is incomplete",
                    cutoff,
                )
            discovered.add(mint)
        decoded_swap_trades = decode_pump_swap_trade_observation(observation)
        if isinstance(decoded_swap_trades, AbstainResult):
            return decoded_swap_trades
        for instruction in decoded_swap_trades:
            if instruction.account_pubkeys is None:
                return _abstain(
                    AbstainReason.MISSING_FEATURE,
                    "Pump AMM trade account table is missing",
                    cutoff,
                )
            mint = _account_at(
                instruction.account_pubkeys,
                instruction.base_mint_account_index,
            )
            if mint is None:
                return _abstain(
                    AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
                    "Pump AMM trade mint proof is incomplete",
                    cutoff,
                )
            discovered.add(mint)
    return tuple(sorted(discovered))


def _launch_before_trade(
    launches: list[LaunchCreatedV2] | tuple[LaunchCreatedV2, ...],
    *,
    observation: RawChainObservation,
    trade_outer_instruction_index: int,
) -> LaunchCreatedV2 | AbstainResult:
    candidates = tuple(
        launch
        for launch in launches
        if _launch_key(launch)
        < _observation_key(
            observation,
            outer_instruction_index=trade_outer_instruction_index,
        )
    )
    if not candidates:
        return _abstain(
            AbstainReason.MISSING_FEATURE,
            "trade has no finalized preceding launch for its mint",
            observation.slot,
        )
    latest_key = max(_launch_key(launch) for launch in candidates)
    latest = tuple(launch for launch in candidates if _launch_key(launch) == latest_key)
    if len(latest) != 1:
        return _abstain(
            AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
            "trade launch join is ambiguous",
            observation.slot,
        )
    return latest[0]


def _account_at(accounts: tuple[str, ...], index: int) -> str | None:
    if type(index) is not int or index < 0 or index >= len(accounts):
        return None
    value = accounts[index]
    return value if isinstance(value, str) and value else None


def _observation_key(
    observation: RawChainObservation,
    *,
    outer_instruction_index: int | None = None,
) -> tuple[int, int, int]:
    return (
        observation.slot,
        observation.transaction_index
        if observation.transaction_index is not None
        else -1,
        outer_instruction_index
        if outer_instruction_index is not None
        else observation.outer_instruction_index
        if observation.outer_instruction_index is not None
        else observation.event_ordinal
        if observation.event_ordinal is not None
        else -1,
    )


def _launch_key(launch: LaunchCreatedV2) -> tuple[int, int, int]:
    return (
        launch.as_of_slot,
        launch.transaction_index if launch.transaction_index is not None else -1,
        launch.outer_instruction_index,
    )


def _abstain(reason: AbstainReason, message: str, as_of_slot: int) -> AbstainResult:
    return AbstainResult(reason=reason, message=message, as_of_slot=as_of_slot)


__all__ = [
    "JoinDerivationResult",
    "TradeMintDiscoveryResult",
    "derive_finalized_trade_joins",
    "discover_finalized_trade_mints",
]
