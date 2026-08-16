"""Build finalized backtest datasets from bounded read-only RPC evidence."""

from __future__ import annotations

from typing import TYPE_CHECKING

from rugbot.backtest.dataset import (
    FinalizedBacktestDataset,
    FinalizedTrade,
    build_finalized_dataset,
)
from rugbot.backtest.finalized_trade_builder import (
    FinalizedTradeJoin,
    build_finalized_trades_from_observations,
)
from rugbot.backtest.observation_trade_join import (
    derive_finalized_trade_joins,
    discover_finalized_trade_mints,
)
from rugbot.backtest.production_case_adapter import (
    FinalizedLaunchCaseProof,
    assemble_observation_copy_trade_cases,
)
from rugbot.domain.decisions import AbstainReason, AbstainResult
from rugbot.ingest.pump_create_observation import decode_pump_create_v2_observation
from rugbot.ingest.rpc_observer import MAX_PAGES, MAX_TRANSACTIONS, observe_address
from rugbot.storage.jsonl_observation_store import observation_identity

if TYPE_CHECKING:
    from rugbot.backtest.copytrade import CopyTradeLaunchCase
    from rugbot.decision.operator_qualification import WalletEntityEvidence
    from rugbot.domain.amounts import Slot
    from rugbot.domain.launches import LaunchCreatedV2
    from rugbot.domain.observations import RawChainObservation
    from rugbot.ingest.rpc_observer import RpcHttpTransport


async def build_finalized_rpc_dataset(  # noqa: C901, PLR0911, PLR0912, PLR0913
    *,
    address: str,
    endpoint: str,
    start_slot: Slot,
    end_slot: Slot,
    max_transactions: int,
    cases: tuple[CopyTradeLaunchCase, ...] = (),
    trades: tuple[FinalizedTrade, ...] = (),
    trade_joins: tuple[FinalizedTradeJoin, ...] = (),
    source_id: str = "solana-http-rpc",
    observer_id: str = "backtest-rpc-dataset",
    transport: RpcHttpTransport | None = None,
    case_proofs: tuple[FinalizedLaunchCaseProof, ...] = (),
    entity_evidence: tuple[WalletEntityEvidence, ...] = (),
    entity_id: str = "",
    regime_id: str = "",
    min_entity_probability_ppm: int = 500_000,
    max_entry_transaction_index: int = 1,
) -> FinalizedBacktestDataset | AbstainResult:
    """Fetch one bounded finalized window and build the canonical dataset.

    Solana address history is transaction-bounded by the existing read-only
    observer. Because ``getSignaturesForAddress`` has no slot-range parameter,
    the observer proves the requested window by paginating newest-first until
    it crosses the lower bound or exhausts history.
    """

    validation = _validate_request(
        start_slot=start_slot,
        end_slot=end_slot,
        max_transactions=max_transactions,
        cases=cases,
        trades=trades,
        trade_joins=trade_joins,
        case_proofs=case_proofs,
    )
    if validation is not None:
        return validation

    observations = await observe_address(
        address,
        endpoint=endpoint,
        source_id=source_id,
        observer_id=observer_id,
        max_signatures=max_transactions,
        max_transactions=max_transactions,
        max_pages=MAX_PAGES,
        start_slot=int(start_slot),
        end_slot=int(end_slot),
        transport=transport,
    )
    if isinstance(observations, AbstainResult):
        return observations
    if len(observations) > max_transactions:
        return _abstain(
            AbstainReason.UNKNOWN_PROTOCOL_STATE,
            "RPC observation count exceeded the transaction bound",
            end_slot,
        )
    if any(
        observation.slot < start_slot or observation.slot > end_slot
        for observation in observations
    ):
        return _abstain(
            AbstainReason.STALE_STATE,
            "finalized RPC evidence is outside the requested slot window",
            end_slot,
        )

    if trade_joins and trades:
        return _abstain(
            AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
            "provide trade joins or finalized trades, not both",
            end_slot,
        )
    if trade_joins:
        derived_trades = build_finalized_trades_from_observations(
            observations=observations,
            joins=trade_joins,
            as_of_slot=end_slot,
        )
        if isinstance(derived_trades, AbstainResult):
            return derived_trades
        trades = derived_trades
    elif not trades:
        derived = derive_finalized_trade_joins(
            observations=observations,
            as_of_slot=end_slot,
        )
        if (
            isinstance(derived, AbstainResult)
            and derived.reason is AbstainReason.MISSING_FEATURE
        ):
            mints = discover_finalized_trade_mints(
                observations=observations,
                as_of_slot=end_slot,
            )
            if isinstance(mints, AbstainResult):
                return mints
            observations = await _extend_with_mint_history(
                observations=observations,
                mints=mints,
                endpoint=endpoint,
                start_slot=start_slot,
                end_slot=end_slot,
                max_transactions=max_transactions,
                source_id=source_id,
                observer_id=observer_id,
                transport=transport,
            )
            if isinstance(observations, AbstainResult):
                return observations
            derived = derive_finalized_trade_joins(
                observations=observations,
                as_of_slot=end_slot,
            )
        if isinstance(derived, AbstainResult):
            return derived
        _, derived_joins = derived
        derived_trades = build_finalized_trades_from_observations(
            observations=observations,
            joins=derived_joins,
            as_of_slot=end_slot,
        )
        if isinstance(derived_trades, AbstainResult):
            return derived_trades
        trades = derived_trades

    if case_proofs:
        launches = _decode_launches(observations=observations)
        if isinstance(launches, AbstainResult):
            return launches
        assembled_cases = assemble_observation_copy_trade_cases(
            launches=launches,
            fills=trades,
            entity_evidence=entity_evidence,
            observations=observations,
            proofs=case_proofs,
            as_of_slot=end_slot,
            entity_id=entity_id,
            regime_id=regime_id,
            min_entity_probability_ppm=min_entity_probability_ppm,
            max_entry_transaction_index=max_entry_transaction_index,
        )
        if isinstance(assembled_cases, AbstainResult):
            return assembled_cases
        cases = assembled_cases

    return build_finalized_dataset(
        observations=observations,
        cases=cases,
        trades=trades,
        as_of_slot=end_slot,
    )


async def _extend_with_mint_history(  # noqa: PLR0913
    *,
    observations: tuple[RawChainObservation, ...],
    mints: tuple[str, ...],
    endpoint: str,
    start_slot: Slot,
    end_slot: Slot,
    max_transactions: int,
    source_id: str,
    observer_id: str,
    transport: RpcHttpTransport | None,
) -> tuple[RawChainObservation, ...] | AbstainResult:
    """Fetch bounded mint histories and merge them by canonical identity."""

    merged = {observation_identity(item): item for item in observations}
    for mint in mints:
        extra = await observe_address(
            mint,
            endpoint=endpoint,
            source_id=source_id,
            observer_id=f"{observer_id}:mint-lookup",
            max_signatures=max_transactions,
            max_transactions=max_transactions,
            max_pages=MAX_PAGES,
            start_slot=int(start_slot),
            end_slot=int(end_slot),
            transport=transport,
        )
        if isinstance(extra, AbstainResult):
            return extra
        if any(item.slot < start_slot or item.slot > end_slot for item in extra):
            return _abstain(
                AbstainReason.STALE_STATE,
                "mint lookup evidence is outside the requested slot window",
                end_slot,
            )
        merged.update({observation_identity(item): item for item in extra})
        if len(merged) > max_transactions:
            return _abstain(
                AbstainReason.UNKNOWN_PROTOCOL_STATE,
                "merged RPC observations exceeded the transaction bound",
                end_slot,
            )
    return tuple(
        sorted(
            merged.values(),
            key=lambda item: (
                item.slot,
                item.transaction_index if item.transaction_index is not None else -1,
            ),
        )
    )


def _validate_request(  # noqa: PLR0913
    *,
    start_slot: object,
    end_slot: object,
    max_transactions: object,
    cases: object,
    trades: object,
    trade_joins: object,
    case_proofs: object,
) -> AbstainResult | None:
    if (
        type(start_slot) is not int
        or type(end_slot) is not int
        or start_slot < 0
        or end_slot < start_slot
    ):
        return _abstain(
            AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
            "RPC dataset slot window is malformed",
            end_slot if type(end_slot) is int else -1,
        )
    if (
        type(max_transactions) is not int
        or max_transactions <= 0
        or max_transactions > MAX_TRANSACTIONS
    ):
        return _abstain(
            AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
            "RPC dataset transaction bound is invalid",
            end_slot,
        )
    if (
        type(cases) is not tuple
        or type(trades) is not tuple
        or type(trade_joins) is not tuple
        or type(case_proofs) is not tuple
    ):
        return _abstain(
            AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
            "RPC dataset cases, trades, and proof bundles must be tuples",
            end_slot,
        )
    if cases and case_proofs:
        return _abstain(
            AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
            "provide finalized launch proofs or copy-trade cases, not both",
            end_slot,
        )
    return None


def _decode_launches(
    *,
    observations: tuple[RawChainObservation, ...],
) -> tuple[LaunchCreatedV2, ...] | AbstainResult:
    """Decode launch proofs without deriving any account layout."""

    launches: list[LaunchCreatedV2] = []
    for observation in observations:
        decoded = decode_pump_create_v2_observation(observation)
        if isinstance(decoded, AbstainResult):
            return decoded
        if decoded is not None:
            launches.append(decoded)
    return tuple(launches)


def _abstain(
    reason: AbstainReason,
    message: str,
    as_of_slot: int,
) -> AbstainResult:
    return AbstainResult(
        reason=reason,
        message=message,
        as_of_slot=as_of_slot,
    )


__all__ = ["build_finalized_rpc_dataset"]
