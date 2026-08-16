"""Online Pump curve evidence adapters used by the shared watch state machine."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from solders.pubkey import Pubkey

from core.client import SolanaClient
from interfaces.core import Platform
from platforms import get_platform_implementations
from rugbot.decision.playbook_rules import EntryRuleInput
from rugbot.domain.decisions import AbstainReason, AbstainResult
from rugbot.execution.position_runtime import PositionMarketEvidence

if TYPE_CHECKING:
    from rugbot.domain.launches import LaunchCreatedV2
    from rugbot.domain.observations import RawChainObservation
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
            state = await self._state(launch.mint_pubkey)
            market_cap = _market_cap(state)
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
        return await self._curve_manager.get_pool_state(curve, commitment="processed")


def _market_cap(state: dict[str, object]) -> int:
    virtual_tokens = int(state["virtual_token_reserves"])
    virtual_sol = int(state["virtual_sol_reserves"])
    total_supply = int(state["token_total_supply"])
    if virtual_tokens <= 0 or virtual_sol <= 0 or total_supply <= 0:
        raise ValueError("invalid curve reserves")  # noqa: TRY003
    return virtual_sol * total_supply // virtual_tokens


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
