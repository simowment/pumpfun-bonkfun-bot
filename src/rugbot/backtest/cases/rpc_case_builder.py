"""Build canonical backtest proofs from one finalized RPC acquisition."""

# The builder is a bounded evidence join; explicit fail-closed branches are
# preferable to hiding missing proof behind a generic adapter.
# ruff: noqa: C901, PLR0911, PLR0913

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from rugbot.backtest.cases.production_case_adapter import (
    FinalizedLaunchCaseProof,
    ProductionEntryFacts,
)
from rugbot.backtest.cases.rpc_case_acquisition import FinalizedRpcCaseAcquisition
from rugbot.backtest.dataset import FinalizedTrade
from rugbot.backtest.trajectory.finalized_trade_builder import (
    PumpTradeEventProof,
    decode_pump_trade_event_proofs,
)
from rugbot.backtest.trajectory.trade_event_trajectory import (
    PumpTradeEventProtocolProof,
)
from rugbot.backtest.trajectory.trajectory_producer import (
    FinalizedPumpTradePoint,
    LaunchTrajectoryMetadata,
)
from rugbot.decision.operator_qualification import WalletEntityEvidence
from rugbot.domain.adverse_event import AdverseEventDetectionConfig
from rugbot.domain.amounts import QuoteBaseUnits, Slot, TokenBaseUnits
from rugbot.domain.create_state_adapter import PumpCreateMintMetadataProof
from rugbot.domain.decisions import AbstainReason, AbstainResult
from rugbot.domain.fees import FeeConfig
from rugbot.domain.migration import PUMP_AMM_PROGRAM_ID
from rugbot.domain.outcome_labels import OutcomeLabelConfig
from rugbot.domain.quote_engine import (
    CANONICAL_PUMP_PROGRAM_CONFIG_VERSION,
    CANONICAL_PUMPSWAP_PROGRAM_CONFIG_VERSION,
    PoolReserves,
    executable_buy_quote,
)
from rugbot.domain.quotes import QuotePath
from rugbot.domain.trades import TradeSide
from rugbot.ingest.pump.bonding_curve_account import (
    PINNED_PUMP_IDL_SHA256,
    PUMP_PROGRAM_ID,
)
from rugbot.ingest.pump.create_event_decoder import SOL_PUBKEY
from rugbot.ingest.pump.pump_create_observation import (
    decode_pump_create_market_state_observation,
)
from rugbot.ingest.pump.pump_swap_event_observation import (
    decode_pump_swap_events_observation,
)
from rugbot.ingest.pump.pump_swap_trade_observation import (
    decode_pump_swap_trade_observation,
)
from rugbot.ingest.pump.swap_trade_decoder import PINNED_PUMP_SWAP_IDL_SHA256

if TYPE_CHECKING:
    from rugbot.domain.launches import LaunchCreatedV2
    from rugbot.domain.metadata_resolver import (
        PumpFinalizedMintMetadataEvidence,
    )
    from rugbot.domain.observations import RawChainObservation


DEFAULT_FIXED_ENTRY_QUOTE_BASE_UNITS = 1_000_000
DEFAULT_LABELER_VERSION = "pump-trade-event-outcome"
DEFAULT_DETECTOR_VERSION = "pump-trade-event-collapse"


@dataclass(frozen=True, slots=True)
class RpcCaseProofBundle:
    """Immutable proofs and entity evidence produced from finalized RPC data."""

    proofs: tuple[FinalizedLaunchCaseProof, ...]
    entity_evidence: tuple[WalletEntityEvidence, ...]


RpcCaseProofResult = RpcCaseProofBundle | AbstainResult


def build_rpc_case_proofs(
    *,
    acquisition: FinalizedRpcCaseAcquisition,
    trades: tuple[FinalizedTrade, ...],
    as_of_slot: Slot,
    fixed_entry_quote_base_units: int = DEFAULT_FIXED_ENTRY_QUOTE_BASE_UNITS,
    horizon_ms: int = 0,
    labeler_version: str = DEFAULT_LABELER_VERSION,
    detector_version: str = DEFAULT_DETECTOR_VERSION,
) -> RpcCaseProofResult:
    """Build launch cases using only finalized, point-in-time evidence.

    A zero ``horizon_ms`` uses the observed duration for each launch. This is
    useful for historical RPC windows, while a positive value requires that
    exact horizon to be present before producing a completed label.
    """

    cutoff = _safe_slot(as_of_slot)
    validation = _validate_request(
        acquisition=acquisition,
        trades=trades,
        as_of_slot=as_of_slot,
        fixed_entry_quote_base_units=fixed_entry_quote_base_units,
        horizon_ms=horizon_ms,
        labeler_version=labeler_version,
        detector_version=detector_version,
    )
    if validation is not None:
        return validation

    observations = tuple(sorted(acquisition.observations, key=_observation_key))
    launches = tuple(sorted(acquisition.launches, key=_launch_key))
    metadata_by_mint = {item.mint_pubkey: item for item in acquisition.mint_metadata}
    proofs: list[FinalizedLaunchCaseProof] = []
    evidence: list[WalletEntityEvidence] = []

    for index, launch in enumerate(launches):
        launch_cutoff = _launch_cutoff(launches, index, cutoff)
        create_observation = _launch_observation(launch, observations)
        if create_observation is None:
            return _abstain(
                AbstainReason.MISSING_FEATURE,
                "finalized create observation is missing for a decoded launch",
                cutoff,
            )
        market_state = decode_pump_create_market_state_observation(create_observation)
        if isinstance(market_state, AbstainResult):
            return market_state
        if market_state is None:
            return _abstain(
                AbstainReason.MISSING_FEATURE,
                "finalized Pump create market state is missing",
                cutoff,
            )
        metadata = metadata_by_mint.get(launch.mint_pubkey)
        if metadata is None:
            return _abstain(
                AbstainReason.MISSING_FEATURE,
                "finalized mint metadata is missing for a decoded launch",
                cutoff,
            )
        mint_proof = _mint_proof(metadata)
        event_records = _trade_events_for_launch(
            observations=observations,
            launch=launch,
            cutoff=launch_cutoff,
        )
        if isinstance(event_records, AbstainResult):
            return event_records
        if not event_records:
            return _abstain(
                AbstainReason.MISSING_FEATURE,
                "no finalized Pump TradeEvents were observed for a launch",
                cutoff,
            )
        first_buy = _first_operator_buy(
            event_records=event_records,
            operator_wallet=acquisition.operator_wallet,
        )
        if first_buy is None:
            return _abstain(
                AbstainReason.MISSING_FEATURE,
                "operator launch has no finalized operator buy event",
                cutoff,
            )
        fee = _fee_config(first_buy[1], first_buy[0], quote_path=first_buy[3])
        entry_position = _entry_position(
            event=first_buy[1],
            observation=first_buy[0],
            mint_proof=mint_proof,
            fee=fee,
            quote_amount=fixed_entry_quote_base_units,
        )
        if isinstance(entry_position, AbstainResult):
            return entry_position
        event_points = _build_points(
            event_records=event_records,
            launch=launch,
            mint_proof=mint_proof,
        )
        if isinstance(event_points, AbstainResult):
            return event_points
        first_timestamp = event_points[0].event.timestamp
        last_timestamp = event_points[-1].event.timestamp
        observed_horizon = max(0, (last_timestamp - first_timestamp) * 1_000)
        selected_horizon = horizon_ms or observed_horizon
        if selected_horizon < 0:
            return _abstain(
                AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
                "launch horizon must be non-negative",
                cutoff,
            )
        launch_evidence_ids = (
            f"rpc-create:{_observation_key_text(create_observation)}",
            f"rpc-mint:{metadata.source_artifact}",
        )
        launch_metadata = LaunchTrajectoryMetadata(
            launch_id=launch.launch_id,
            token_mint=launch.mint_pubkey,
            launch_slot=Slot(launch.as_of_slot),
            launch_timestamp=first_timestamp,
            full_exit_base_amount_base_units=TokenBaseUnits(entry_position),
            evidence_ids=launch_evidence_ids,
        )
        outcome_config = OutcomeLabelConfig(
            as_of_slot=Slot(launch_cutoff),
            launch_id=launch.launch_id,
            token_mint=launch.mint_pubkey,
            labeler_version=labeler_version,
            horizon_ms=(selected_horizon,),
            entry_total_cost_quote_base_units=QuoteBaseUnits(
                fixed_entry_quote_base_units
            ),
        )
        adverse_config = AdverseEventDetectionConfig(
            as_of_slot=Slot(launch_cutoff),
            token_mint=launch.mint_pubkey,
            detector_version=detector_version,
            min_peak_price_ppm=1,
            min_drawdown_ppm=300_000,
            recovery_window_ms=60_000,
        )
        entry_facts = ProductionEntryFacts(
            as_of_slot=Slot(launch_cutoff),
            launch_id=launch.launch_id,
            entry_market_cap_quote_base_units=QuoteBaseUnits(
                _market_cap(market_state.reserves)
            ),
            wallet_buy_elapsed_ms=(first_buy[1].timestamp - first_timestamp) * 1_000,
            evidence_ids=(f"rpc-entry:{_observation_key_text(first_buy[0])}",),
        )
        proofs.append(
            FinalizedLaunchCaseProof(
                launch=launch_metadata,
                points=event_points,
                outcome_config=outcome_config,
                adverse_config=adverse_config,
                entry_facts=entry_facts,
            )
        )
        evidence.append(
            WalletEntityEvidence(
                as_of_slot=Slot(launch_cutoff),
                observed_slot=Slot(launch.as_of_slot),
                entity_id=acquisition.operator_wallet,
                launch_id=launch.launch_id,
                wallet=acquisition.operator_wallet,
                entity_probability_ppm=1_000_000,
                evidence_ids=launch_evidence_ids,
            )
        )

    return RpcCaseProofBundle(
        proofs=tuple(proofs),
        entity_evidence=tuple(evidence),
    )


def _trade_events_for_launch(
    *,
    observations: tuple[RawChainObservation, ...],
    launch: LaunchCreatedV2,
    cutoff: int,
) -> (
    tuple[tuple[RawChainObservation, PumpTradeEventProof, int, QuotePath], ...]
    | AbstainResult
):
    records: list[tuple[RawChainObservation, PumpTradeEventProof, int, QuotePath]] = []
    for observation in observations:
        if observation.slot < launch.as_of_slot or observation.slot > cutoff:
            continue
        decoded = decode_pump_trade_event_proofs(observation)
        if isinstance(decoded, AbstainResult):
            if decoded.reason is AbstainReason.MISSING_FEATURE:
                continue
            return decoded
        for local_index, event in decoded:
            if event.mint == launch.mint_pubkey:
                records.append(
                    (observation, event, local_index, QuotePath.PUMP_BONDING_CURVE)
                )
        swap_events = decode_pump_swap_events_observation(observation)
        if isinstance(swap_events, AbstainResult):
            if swap_events.reason is not AbstainReason.MISSING_FEATURE:
                return swap_events
            continue
        for event in swap_events:
            if _swap_event_mint(observation, event) != launch.mint_pubkey:
                continue
            records.append(
                (
                    observation,
                    _swap_event_as_trade_event(event, launch.mint_pubkey),
                    event.event_index,
                    QuotePath.CANONICAL_PUMPSWAP,
                )
            )
    records.sort(key=lambda item: (_observation_key(item[0]), item[2], item[3].value))
    return tuple(records)


def _swap_event_mint(observation: RawChainObservation, event: object) -> str | None:
    decoded = decode_pump_swap_trade_observation(observation)
    if isinstance(decoded, AbstainResult):
        return None
    matches: list[str] = []
    for instruction in decoded:
        accounts = instruction.account_pubkeys
        if accounts is None:
            continue
        # PumpSwap's exact-quote buy layout names SOL as base and the token as
        # quote; sells use the conventional base-token account.
        mint_index = (
            instruction.quote_mint_account_index
            if event.side is TradeSide.BUY
            else instruction.base_mint_account_index
        )
        user_index = instruction.user_account_index
        if (
            instruction.side is event.side
            and 0 <= mint_index < len(accounts)
            and 0 <= user_index < len(accounts)
            and accounts[user_index] == event.user
        ):
            matches.append(accounts[mint_index])
    return matches[0] if len(matches) == 1 else None


def _build_points(
    *,
    event_records: tuple[
        tuple[RawChainObservation, PumpTradeEventProof, int, QuotePath], ...
    ],
    launch: LaunchCreatedV2,
    mint_proof: PumpCreateMintMetadataProof,
) -> tuple[FinalizedPumpTradePoint, ...] | AbstainResult:
    points: list[FinalizedPumpTradePoint] = []
    for event_index, (observation, event, local_index, quote_path) in enumerate(
        event_records
    ):
        root = _event_root(observation, local_index)
        proof = PumpTradeEventProtocolProof(
            as_of_slot=Slot(observation.slot),
            program_id=(
                PUMP_PROGRAM_ID
                if quote_path is QuotePath.PUMP_BONDING_CURVE
                else PUMP_AMM_PROGRAM_ID
            ),
            idl_hash=(
                PINNED_PUMP_IDL_SHA256
                if quote_path is QuotePath.PUMP_BONDING_CURVE
                else PINNED_PUMP_SWAP_IDL_SHA256
            ),
            program_config_version=(
                CANONICAL_PUMP_PROGRAM_CONFIG_VERSION
                if quote_path is QuotePath.PUMP_BONDING_CURVE
                else CANONICAL_PUMPSWAP_PROGRAM_CONFIG_VERSION
            ),
            fee_config=_fee_config(event, observation, quote_path=quote_path),
            source_artifact=f"finalized-trade-event:{root}",
            evidence_ids=(f"protocol:{root}",),
        )
        points.append(
            FinalizedPumpTradePoint(
                observation=observation,
                event=event,
                event_index=event_index,
                protocol_snapshot=proof,
                mint_metadata=mint_proof,
                curve_completed=False,
                migration_observed=False,
                evidence_ids=(f"trajectory:{root}", f"protocol-ref:{root}"),
                quote_path=quote_path,
            )
        )
    if not points:
        return _abstain(
            AbstainReason.MISSING_FEATURE,
            "finalized launch trajectory points are required",
            launch.as_of_slot,
        )
    return tuple(points)


def _entry_position(
    *,
    event: PumpTradeEventProof,
    observation: RawChainObservation,
    mint_proof: PumpCreateMintMetadataProof,
    fee: FeeConfig,
    quote_amount: int,
) -> int | AbstainResult:
    reserves = PoolReserves(
        virtual_base_reserves=TokenBaseUnits(
            event.virtual_token_reserves_base_units + event.token_amount_base_units
        ),
        virtual_quote_reserves=QuoteBaseUnits(
            event.virtual_sol_reserves_base_units - event.sol_amount_base_units
        ),
        real_base_reserves=TokenBaseUnits(
            event.real_token_reserves_base_units + event.token_amount_base_units
        ),
        real_quote_reserves=QuoteBaseUnits(
            max(0, event.real_sol_reserves_base_units - event.sol_amount_base_units)
        ),
        is_complete=False,
        as_of_slot=Slot(observation.slot),
        base_decimals=mint_proof.base_decimals,
        quote_decimals=mint_proof.quote_decimals,
        decoder_version="pump-bonding-curve-account-v1",
        idl_hash=PINNED_PUMP_IDL_SHA256,
        program_config_version=CANONICAL_PUMP_PROGRAM_CONFIG_VERSION,
    )
    quote = executable_buy_quote(
        path=QuotePath.PUMP_BONDING_CURVE,
        reserves=reserves,
        quote_input_amount=QuoteBaseUnits(quote_amount),
        fee_config=fee,
    )
    if isinstance(quote, AbstainResult):
        return quote
    return quote.output_amount_base_units


def _mint_proof(
    metadata: PumpFinalizedMintMetadataEvidence,
) -> PumpCreateMintMetadataProof:
    return PumpCreateMintMetadataProof(
        as_of_slot=metadata.as_of_slot,
        base_mint_pubkey=metadata.mint_pubkey,
        quote_mint_pubkey=SOL_PUBKEY,
        base_decimals=metadata.decimals,
        quote_decimals=9,
        source_artifact=metadata.source_artifact,
    )


def _fee_config(
    event: PumpTradeEventProof,
    observation: RawChainObservation,
    *,
    quote_path: QuotePath,
) -> FeeConfig:
    return FeeConfig(
        version=(
            "pump-trade-event-fees"
            if quote_path is QuotePath.PUMP_BONDING_CURVE
            else "pump-swap-event-fees"
        ),
        protocol_fee_bps=event.protocol_fee_basis_points,
        creator_fee_bps=event.creator_fee_basis_points,
        is_known=True,
        program_config_version=(
            CANONICAL_PUMP_PROGRAM_CONFIG_VERSION
            if quote_path is QuotePath.PUMP_BONDING_CURVE
            else CANONICAL_PUMPSWAP_PROGRAM_CONFIG_VERSION
        ),
        valid_from_slot=Slot(observation.slot),
        source_artifact_version=f"finalized-trade-event:{_observation_key_text(observation)}",
    )


def _first_operator_buy(
    *,
    event_records: tuple[
        tuple[RawChainObservation, PumpTradeEventProof, int, QuotePath], ...
    ],
    operator_wallet: str,
) -> tuple[RawChainObservation, PumpTradeEventProof, int, QuotePath] | None:
    return next(
        (
            record
            for record in event_records
            if record[1].user == operator_wallet and record[1].is_buy
        ),
        None,
    )


def _swap_event_as_trade_event(event: object, mint: str) -> PumpTradeEventProof:
    return PumpTradeEventProof(
        mint=mint,
        user=event.user,
        sol_amount_base_units=int(event.user_quote_amount_base_units),
        token_amount_base_units=int(event.base_amount_base_units),
        is_buy=event.side is TradeSide.BUY,
        instruction_name=event.instruction_name,
        timestamp=int(event.timestamp),
        virtual_sol_reserves_base_units=int(event.pool_quote_reserves_base_units),
        virtual_token_reserves_base_units=int(event.pool_base_reserves_base_units),
        real_sol_reserves_base_units=int(event.pool_quote_reserves_base_units),
        real_token_reserves_base_units=int(event.pool_base_reserves_base_units),
        protocol_fee_base_units=int(event.protocol_fee_base_units),
        creator_fee_base_units=int(event.creator_fee_base_units),
        protocol_fee_basis_points=int(event.protocol_fee_basis_points),
        creator_fee_basis_points=int(event.creator_fee_basis_points),
        cashback_base_units=0,
        encoded_event=event.encoded_event,
        quote_mint=SOL_PUBKEY,
        quote_amount_base_units=int(event.user_quote_amount_base_units),
        virtual_quote_reserves_base_units=int(event.virtual_quote_reserves_base_units),
        real_quote_reserves_base_units=int(event.pool_quote_reserves_base_units),
    )


def _launch_observation(
    launch: LaunchCreatedV2,
    observations: tuple[RawChainObservation, ...],
) -> RawChainObservation | None:
    return next(
        (
            observation
            for observation in observations
            if observation.signature == launch.signature
            and observation.slot == launch.as_of_slot
        ),
        None,
    )


def _launch_cutoff(
    launches: tuple[LaunchCreatedV2, ...], index: int, cutoff: int
) -> int:
    if index + 1 < len(launches):
        return min(cutoff, int(launches[index + 1].as_of_slot) - 1)
    return cutoff


def _market_cap(reserves: object) -> int:
    virtual_tokens = int(reserves.virtual_token_reserves)
    virtual_quote = int(reserves.virtual_quote_reserves)
    total_supply = int(reserves.token_total_supply)
    if min(virtual_tokens, virtual_quote, total_supply) <= 0:
        raise ValueError("create reserves do not prove a positive market cap")  # noqa: TRY003
    return virtual_quote * total_supply // virtual_tokens


def _event_root(observation: RawChainObservation, local_index: int) -> str:
    return f"{_observation_key_text(observation)}:{local_index}"


def _observation_key_text(observation: RawChainObservation) -> str:
    return f"{observation.slot}:{observation.transaction_index}:{(observation.signature or b'').hex()}"


def _observation_key(observation: RawChainObservation) -> tuple[int, int, bytes]:
    return (
        int(observation.slot),
        observation.transaction_index
        if observation.transaction_index is not None
        else -1,
        observation.signature or b"",
    )


def _launch_key(launch: LaunchCreatedV2) -> tuple[int, str]:
    return int(launch.as_of_slot), launch.launch_id


def _validate_request(
    *,
    acquisition: object,
    trades: object,
    as_of_slot: object,
    fixed_entry_quote_base_units: object,
    horizon_ms: object,
    labeler_version: object,
    detector_version: object,
) -> AbstainResult | None:
    cutoff = _safe_slot(as_of_slot)
    if type(acquisition) is not FinalizedRpcCaseAcquisition:
        return _abstain(
            AbstainReason.MISSING_FEATURE, "RPC acquisition is required", cutoff
        )
    if (
        type(as_of_slot) is not int
        or as_of_slot < 0
        or acquisition.as_of_slot != as_of_slot
    ):
        return _abstain(
            AbstainReason.STALE_STATE, "RPC acquisition cutoff is stale", cutoff
        )
    if type(trades) is not tuple or any(
        type(item) is not FinalizedTrade for item in trades
    ):
        return _abstain(
            AbstainReason.MISSING_FEATURE, "finalized fills are required", cutoff
        )
    if (
        type(fixed_entry_quote_base_units) is not int
        or fixed_entry_quote_base_units <= 0
    ):
        return _abstain(
            AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
            "fixed entry quote must be positive",
            cutoff,
        )
    if type(horizon_ms) is not int or horizon_ms < 0:
        return _abstain(
            AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
            "horizon must be non-negative",
            cutoff,
        )
    if any(
        type(value) is not str or not value
        for value in (labeler_version, detector_version)
    ):
        return _abstain(
            AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
            "label and detector identifiers are required",
            cutoff,
        )
    return None


def _safe_slot(value: object) -> int:
    return value if type(value) is int else -1


def _abstain(reason: AbstainReason, message: str, as_of_slot: int) -> AbstainResult:
    return AbstainResult(reason=reason, message=message, as_of_slot=as_of_slot)


__all__ = ["RpcCaseProofBundle", "build_rpc_case_proofs"]
