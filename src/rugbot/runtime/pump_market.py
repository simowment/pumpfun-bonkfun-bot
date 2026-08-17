"""Online Pump curve evidence adapters used by the shared watch state machine."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from solders.pubkey import Pubkey

from core.client import SolanaClient
from interfaces.core import Platform
from platforms import get_platform_implementations
from rugbot.decision.playbook_rules import EntryRuleInput
from rugbot.decision.sizing import EntryLatencySnapshot
from rugbot.domain.decisions import AbstainReason, AbstainResult
from rugbot.execution.paper_simulator import PaperStress
from rugbot.execution.ports import MAX_SLIPPAGE_BPS
from rugbot.execution.position_runtime import PositionMarketEvidence
from rugbot.ingest.pump_create_observation import (
    decode_pump_create_market_state_observation,
)
from rugbot.protocol.pump.bonding_curve_account import (
    PINNED_PUMP_IDL_SHA256,
    PUMP_BONDING_CURVE_ACCOUNT_DECODER_VERSION,
    PUMP_BONDING_CURVE_LAYOUT_ARTIFACT_VERSION,
    PUMP_PROGRAM_ID,
    PumpBondingCurveAccountState,
    PumpBondingCurveDecodeRequest,
    bonding_curve_snapshot_to_pool_reserves,
    decode_pump_bonding_curve_account,
)
from rugbot.protocol.pump.create_event_decoder import SOL_PUBKEY
from rugbot.protocol.pump.create_state_adapter import PumpCreateMintMetadataProof
from rugbot.protocol.pump.fee_config_account import (
    decode_pump_fee_config_account,
)
from rugbot.protocol.pump.global_account import (
    PUMP_GLOBAL_PDA,
    decode_pump_global_account,
)
from rugbot.protocol.pump.metadata_resolver import (
    PumpFinalizedAccountMetadataEvidence,
    PumpFinalizedMintMetadataEvidence,
    PumpMetadataResolveRequest,
    resolve_pump_create_metadata,
)
from rugbot.protocol.pump.mint_account import decode_spl_token_2022_mint_metadata
from rugbot.protocol.pump.quote_engine import (
    CANONICAL_PUMP_PROGRAM_CONFIG_VERSION,
    PoolReserves,
)
from rugbot.protocol.pump.version_registry import (
    PUMP_VERSION_REGISTRY_VERSION,
    PumpFeeScheduleVersion,
    PumpProgramConfigVersion,
    PumpProtocolVersionSnapshot,
)
from rugbot.runtime.paper_context import PaperContextInput, resolve_paper_context
from rugbot.runtime.pump_paper_rpc import (
    PumpPaperAccountObservations,
    resolve_pump_paper_accounts,
)

if TYPE_CHECKING:
    from rugbot.domain.launches import LaunchCreatedV2
    from rugbot.domain.observations import RawChainObservation
    from rugbot.execution.paper import PaperExecutionPort
    from rugbot.execution.position_runtime import (
        PaperPositionState,
    )


@dataclass(slots=True)
class PumpOnlineMarket:
    """Read current Pump bonding-curve state without loading signing keys."""

    endpoint: str
    _client: SolanaClient = field(init=False, repr=False)
    _provider: object = field(init=False, repr=False)
    _curve_manager: object = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self._client = SolanaClient(self.endpoint)
        implementations = get_platform_implementations(Platform.PUMP_FUN, self._client)
        self._provider = implementations.address_provider
        self._curve_manager = implementations.curve_manager

    async def close(self) -> None:
        """Close the read-only RPC client owned by this market adapter."""

        await self._client.close()

    async def entry_evidence(
        self, launch: LaunchCreatedV2, observation: RawChainObservation
    ) -> EntryRuleInput | AbstainResult:
        """Read the launch-time market snapshot used by configured filters."""

        if observation.slot != launch.as_of_slot:
            return _abstain(
                "launch and observation slots do not match", observation.slot
            )
        try:
            state = decode_pump_create_market_state_observation(observation)
            if isinstance(state, AbstainResult) or state is None:
                return state or _abstain(
                    "finalized create market state unavailable", observation.slot
                )
            market_cap = _market_cap_from_reserves(state.reserves)
        except Exception as error:  # noqa: BLE001
            return _abstain(
                f"entry market state unavailable: {type(error).__name__}",
                observation.slot,
            )
        event_time_ms = observation.received_wall_ns // 1_000_000
        return EntryRuleInput(
            as_of_slot=observation.slot,
            token_mint=launch.mint_pubkey,
            now_ms=event_time_ms,
            event_time_ms=event_time_ms,
            is_copytrade=True,
            token_created_time_ms=event_time_ms,
            market_cap_quote_base_units=market_cap,
            current_market_cap_quote_base_units=market_cap,
        )

    async def execution_port(
        self,
        launch: LaunchCreatedV2,
        observation: RawChainObservation,
        candidate: object,
    ) -> PaperExecutionPort | AbstainResult:
        """Resolve one exact-slot paper port from a finalized account batch.

        The account batch is requested at the launch observation slot. A
        newer account context is rejected by the RPC boundary, so this path
        cannot leak later curve state into a launch decision.
        """

        if observation.slot != launch.as_of_slot:
            return _abstain(
                "paper launch and observation slots do not match",
                observation.slot,
            )
        intent = getattr(candidate, "intent", None)
        max_slippage_bps = getattr(intent, "max_slippage_bps", None)
        if (
            type(max_slippage_bps) is not int
            or not 0 <= max_slippage_bps <= MAX_SLIPPAGE_BPS
        ):
            return _abstain("paper candidate slippage is malformed", observation.slot)
        accounts = await resolve_pump_paper_accounts(
            launch=launch,
            create_observation=observation,
            endpoint=self.endpoint,
            account_as_of_slot=observation.slot,
        )
        if isinstance(accounts, AbstainResult):
            return accounts
        return _paper_port_from_accounts(
            accounts=accounts,
            launch=launch,
            max_slippage_bps=max_slippage_bps,
        )

    async def position_evidence(
        self,
        observation: RawChainObservation,
        position: PaperPositionState,
        *,
        entry_quote_lamports: int,
    ) -> PositionMarketEvidence | AbstainResult | None:
        """Produce current curve PnL and exit capacity for one open position."""

        return await self.position_evidence_at_slot(
            position,
            as_of_slot=observation.slot,
            entry_quote_lamports=entry_quote_lamports,
        )

    async def position_evidence_at_slot(
        self,
        position: PaperPositionState,
        *,
        as_of_slot: int,
        entry_quote_lamports: int,
    ) -> PositionMarketEvidence | AbstainResult | None:
        """Produce position evidence from an explicit newer finalized slot."""

        if as_of_slot <= position.as_of_slot:
            return _abstain("position observation slot did not advance", as_of_slot)
        if type(entry_quote_lamports) is not int or entry_quote_lamports <= 0:
            return _abstain("entry quote size is malformed", as_of_slot)
        try:
            state = await self._state(position.market_id)
            current_quote = _sell_quote(
                int(position.current_position_base_units), state
            )
            market_cap = _market_cap(state)
            current_pnl = (
                (current_quote - entry_quote_lamports)
                * 1_000_000
                // entry_quote_lamports
            )
        except Exception as error:  # noqa: BLE001
            return _abstain(
                f"position market state unavailable: {type(error).__name__}",
                as_of_slot,
            )
        return PositionMarketEvidence(
            as_of_slot=as_of_slot,
            market_id=position.market_id,
            current_pnl_ppm=current_pnl,
            idle_ms=0,
            executable_exit_capacity_base_units=position.current_position_base_units,
            current_market_cap_quote_base_units=market_cap,
        )

    async def finalized_slot(self) -> int | AbstainResult:
        """Read the latest finalized slot used for position polling."""

        response = await self._client.post_rpc(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "getSlot",
                "params": [{"commitment": "finalized"}],
            }
        )
        result = response.get("result") if isinstance(response, dict) else None
        if type(result) is not int or result < 0:
            return _abstain("finalized slot response is malformed", -1)
        return result

    async def _state(self, mint: str) -> dict[str, object]:
        mint_key = Pubkey.from_string(mint)
        curve = self._provider.derive_pool_address(mint_key)
        return await self._curve_manager.get_pool_state(curve, commitment="finalized")


def _market_cap(state: dict[str, object]) -> int:
    virtual_tokens = int(state["virtual_token_reserves"])
    virtual_sol = int(state["virtual_sol_reserves"])
    total_supply = int(state["token_total_supply"])
    if virtual_tokens <= 0 or virtual_sol <= 0 or total_supply <= 0:
        raise ValueError("invalid curve reserves")  # noqa: TRY003
    return virtual_sol * total_supply // virtual_tokens


def _market_cap_from_reserves(reserves: object) -> int:
    virtual_tokens = getattr(reserves, "virtual_token_reserves", None)
    virtual_quote = getattr(reserves, "virtual_quote_reserves", None)
    total_supply = getattr(reserves, "token_total_supply", None)
    if any(
        type(value) is not int or value <= 0
        for value in (virtual_tokens, virtual_quote, total_supply)
    ):
        raise ValueError("invalid create reserves")  # noqa: TRY003
    return virtual_quote * total_supply // virtual_tokens


def _paper_port_from_accounts(  # noqa: PLR0911
    *,
    accounts: PumpPaperAccountObservations,
    launch: LaunchCreatedV2,
    max_slippage_bps: int,
) -> PaperExecutionPort | AbstainResult:
    slot = accounts.as_of_slot
    global_state = decode_pump_global_account(accounts.global_account)
    if isinstance(global_state, AbstainResult):
        return global_state
    fee_state = decode_pump_fee_config_account(accounts.fee_config_account)
    if isinstance(fee_state, AbstainResult):
        return fee_state
    mint_evidence = decode_spl_token_2022_mint_metadata(
        accounts.mint_account,
        mint_pubkey=launch.mint_pubkey,
    )
    if isinstance(mint_evidence, AbstainResult):
        return mint_evidence

    global_hash = global_state.raw_account_data_sha256
    fee_hash = hashlib.sha256(
        accounts.fee_config_account.raw_account_data or b""
    ).hexdigest()
    program_source = f"rpc:pump-global:{global_hash}"
    fee_source = f"rpc:pump-fee-config:{fee_hash}"
    metadata = resolve_pump_create_metadata(
        PumpMetadataResolveRequest(
            as_of_slot=slot,
            account_evidence=PumpFinalizedAccountMetadataEvidence(
                as_of_slot=slot,
                account_pubkey=PUMP_GLOBAL_PDA,
                owner_program_id=PUMP_PROGRAM_ID,
                program_id=PUMP_PROGRAM_ID,
                idl_hash=PINNED_PUMP_IDL_SHA256,
                global_config_hash=global_hash,
                source_artifact=program_source,
                commitment="finalized",
            ),
            base_mint_evidence=mint_evidence,
            quote_mint_evidence=PumpFinalizedMintMetadataEvidence(
                as_of_slot=slot,
                mint_pubkey=SOL_PUBKEY,
                owner_program_id=SOL_PUBKEY,
                decimals=9,
                source_artifact="runtime:native-sol-metadata",
                commitment="finalized",
            ),
            program_configs=(
                PumpProgramConfigVersion(
                    version=CANONICAL_PUMP_PROGRAM_CONFIG_VERSION,
                    program_id=PUMP_PROGRAM_ID,
                    idl_hash=PINNED_PUMP_IDL_SHA256,
                    global_config_hash=global_hash,
                    valid_from_slot=slot,
                    valid_to_slot=None,
                    source_artifact_version=program_source,
                ),
            ),
            fee_schedules=(
                PumpFeeScheduleVersion(
                    version=fee_source,
                    program_config_version=CANONICAL_PUMP_PROGRAM_CONFIG_VERSION,
                    protocol_fee_bps=fee_state.flat_fees.protocol_fee_bps,
                    creator_fee_bps=fee_state.flat_fees.creator_fee_bps,
                    valid_from_slot=slot,
                    valid_to_slot=None,
                    source_artifact_version=fee_source,
                ),
            ),
            registry_version=PUMP_VERSION_REGISTRY_VERSION,
        )
    )
    if isinstance(metadata, AbstainResult):
        return metadata
    mint_metadata, protocol_snapshot = metadata

    curve_raw = accounts.bonding_curve_account.raw_account_data
    if curve_raw is None:
        return _abstain("bonding curve account bytes are missing", slot)
    curve_state = decode_pump_bonding_curve_account(
        PumpBondingCurveDecodeRequest(
            account_state=PumpBondingCurveAccountState(
                as_of_slot=slot,
                account_pubkey=launch.bonding_curve_pubkey,
                owner_program_id=PUMP_PROGRAM_ID,
                raw_account_data=curve_raw,
                source_artifact_version=f"rpc:pump-curve:{hashlib.sha256(curve_raw).hexdigest()}",
                layout_artifact_version=PUMP_BONDING_CURVE_LAYOUT_ARTIFACT_VERSION,
            ),
            protocol_snapshot=protocol_snapshot,
            idl_hash=PINNED_PUMP_IDL_SHA256,
            base_decimals=mint_metadata.base_decimals,
            quote_decimals=mint_metadata.quote_decimals,
            base_mint=launch.mint_pubkey,
            quote_mint=SOL_PUBKEY,
            decoder_version=PUMP_BONDING_CURVE_ACCOUNT_DECODER_VERSION,
        )
    )
    if isinstance(curve_state, AbstainResult):
        return curve_state
    reserves = bonding_curve_snapshot_to_pool_reserves(curve_state)
    if isinstance(reserves, AbstainResult):
        return reserves

    latency = EntryLatencySnapshot(
        as_of_slot=slot,
        latency_snapshot_version="runtime:finalized-account-batch",
        p99_entry_latency_ms=0,
        p99_exit_latency_ms=0,
        safety_margin_ms=0,
        evidence_ids=(
            str(accounts.global_account.raw_id),
            str(curve_state.raw_account_data_sha256),
        ),
    )
    return _resolve_runtime_paper_port(
        reserves=reserves,
        protocol_snapshot=protocol_snapshot,
        mint_metadata=mint_metadata,
        stress=PaperStress(
            latency_snapshot=latency,
            max_entry_latency_ms=0,
            max_exit_latency_ms=0,
            entry_slippage_bps=max_slippage_bps,
            exit_slippage_bps=max_slippage_bps,
        ),
    )


def _resolve_runtime_paper_port(
    *,
    reserves: object,
    protocol_snapshot: object,
    mint_metadata: object,
    stress: PaperStress,
) -> PaperExecutionPort | AbstainResult:
    if not isinstance(reserves, PoolReserves):
        return _abstain("runtime Pump reserves are malformed", -1)
    if not isinstance(protocol_snapshot, PumpProtocolVersionSnapshot):
        return _abstain(
            "runtime Pump protocol snapshot is malformed", reserves.as_of_slot
        )
    if not isinstance(mint_metadata, PumpCreateMintMetadataProof):
        return _abstain("runtime Pump mint metadata is malformed", reserves.as_of_slot)
    return resolve_paper_context(
        inputs=PaperContextInput(
            market_state=None,
            protocol_snapshot=protocol_snapshot,
            mint_metadata=mint_metadata,
            stress=stress,
            current_reserves=reserves,
        )
    )


def _sell_quote(amount: int, state: dict[str, object]) -> int:
    virtual_tokens = int(state["virtual_token_reserves"])
    virtual_sol = int(state["virtual_sol_reserves"])
    if amount <= 0 or virtual_tokens <= 0 or virtual_sol <= 0:
        raise ValueError("invalid sell quote inputs")  # noqa: TRY003
    return amount * virtual_sol // (virtual_tokens + amount)


def _abstain(message: str, as_of_slot: int) -> AbstainResult:
    return AbstainResult(
        reason=AbstainReason.MISSING_FEATURE,
        message=message,
        as_of_slot=as_of_slot,
    )
