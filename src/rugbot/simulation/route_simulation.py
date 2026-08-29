"""Route-aware Pump V2 simulation without signing or transaction submission."""

# This adapter intentionally uses the live builder helpers; it must not drift
# from the transaction path it is designed to validate.
# ruff: noqa: TRY003

from __future__ import annotations

from dataclasses import dataclass, field

from solders.pubkey import Pubkey

from rugbot.domain.amounts import Lamports
from rugbot.domain.decisions import AbstainResult
from rugbot.execution.firewall import FirewallPolicy, validate_pump_v2_instructions
from rugbot.execution.live import (
    _build_trade_context,
    _build_transaction_instructions,
    _fetch_trade_accounts,
)
from rugbot.execution.ports import (
    ExecutionIntent,
    ExecutionMode,
    ExecutionReceipt,
    non_submitting_receipt,
    validate_execution_intent,
)
from rugbot.execution.sender import JitoSender, RoutingPolicy
from rugbot.ingest.pump.create_decoder import PUMP_PROGRAM_ID
from rugbot.integrations.solana_rpc import SolanaClient
from rugbot.simulation.simulation import SimulationError, simulate_unsigned_transaction


@dataclass(slots=True)
class SimulationPumpExecutionPort:
    """Run the live Pump V2 build and RPC simulation path without signing.

    The port deliberately accepts only a public payer identity. It reuses the
    live adapter's account acquisition, quote/context construction, instruction
    builder, tip policy, and firewall. The final transaction contains
    placeholder signatures only inside ``simulateTransaction`` and is never
    passed to either transaction sender.
    """

    endpoint: str
    signer_pubkey: str
    fixed_priority_fee_microlamports: int = 200_000
    jito_tip_lamports: int = 1_000_000
    compute_unit_limit: int = 400_000
    loaded_accounts_data_size_limit: int = 128_000
    routing_policy: RoutingPolicy = RoutingPolicy.JITO_ONLY
    jito_block_engine_url: str = JitoSender.DEFAULT_BLOCK_ENGINE_URL
    _client: SolanaClient = field(init=False, repr=False)
    _payer: Pubkey = field(init=False, repr=False)
    _jito_sender: JitoSender = field(init=False, repr=False)
    _initialized: bool = field(init=False, default=False, repr=False)

    def __post_init__(self) -> None:
        """Validate the public simulation identity and initialize read-only clients."""

        if not self.endpoint:
            raise ValueError("route simulation requires an RPC endpoint")
        if not self.signer_pubkey:
            raise ValueError("route simulation requires signer_pubkey")
        try:
            self._payer = Pubkey.from_string(self.signer_pubkey)
        except (TypeError, ValueError) as error:
            raise ValueError("route simulation signer_pubkey is invalid") from error
        if type(self.fixed_priority_fee_microlamports) is not int or (
            self.fixed_priority_fee_microlamports < 0
        ):
            raise ValueError("priority fee must be non-negative")
        if type(self.jito_tip_lamports) is not int or self.jito_tip_lamports < 0:
            raise ValueError("Jito tip must be non-negative")
        if type(self.compute_unit_limit) is not int or self.compute_unit_limit <= 0:
            raise ValueError("compute unit limit must be positive")
        if (
            type(self.loaded_accounts_data_size_limit) is not int
            or self.loaded_accounts_data_size_limit <= 0
        ):
            raise ValueError("loaded account data limit must be positive")
        self._client = SolanaClient(self.endpoint)
        self._jito_sender = JitoSender(block_engine_url=self.jito_block_engine_url)

    async def initialize(self) -> None:
        """Refresh Jito tip accounts when the configured route needs them."""

        if self._initialized:
            return
        if self.routing_policy is RoutingPolicy.JITO_ONLY:
            await self._jito_sender.initialize_tip_accounts()
        self._initialized = True

    async def close(self) -> None:
        """Close read-only RPC and Jito metadata clients."""

        await self._jito_sender.close()
        await self._client.close()

    async def submit(self, intent: ExecutionIntent) -> ExecutionReceipt:
        """Build and simulate the exact live transaction path without broadcasting."""

        intent_error = validate_execution_intent(intent)
        if intent_error is not None:
            return non_submitting_receipt(
                mode=ExecutionMode.SIMULATION,
                intent=intent if isinstance(intent, ExecutionIntent) else None,
                message=intent_error,
                estimated_fee_lamports=Lamports(0),
            )

        try:
            await self.initialize()
            mint = Pubkey.from_string(intent.market_id)
            slot, accounts = await _fetch_trade_accounts(self._client, mint)
            context_result = _build_trade_context(
                accounts=accounts,
                mint=mint,
                user=self._payer,
                intent=intent,
            )
            if isinstance(context_result, AbstainResult):
                return non_submitting_receipt(
                    mode=ExecutionMode.SIMULATION,
                    intent=intent,
                    message=context_result.message,
                    estimated_fee_lamports=Lamports(0),
                )
            context, _reserves = context_result
            instructions = _build_transaction_instructions(
                context=context,
                reserves=_reserves,
                intent=intent,
                jito_tip_account=(
                    self._jito_sender.get_random_tip_account()
                    if self.routing_policy is RoutingPolicy.JITO_ONLY
                    and self.jito_tip_lamports > 0
                    else None
                ),
                compute_unit_limit=self.compute_unit_limit,
                loaded_accounts_data_size_limit=self.loaded_accounts_data_size_limit,
                priority_fee_microlamports=self.fixed_priority_fee_microlamports,
                jito_tip_lamports=self.jito_tip_lamports,
            )
            trade_instruction = next(
                instruction
                for instruction in instructions
                if str(instruction.program_id) == PUMP_PROGRAM_ID
            )
            policy = FirewallPolicy(
                payer=self._payer,
                mint=mint,
                max_tip_lamports=self.jito_tip_lamports,
                allowed_tip_accounts=frozenset(
                    Pubkey.from_string(account)
                    for account in self._jito_sender.tip_accounts
                ),
                expected_pump_accounts=tuple(
                    meta.pubkey for meta in trade_instruction.accounts
                ),
            )
            checked = validate_pump_v2_instructions(instructions, policy=policy)
            blockhash = await self._client.get_cached_blockhash()
            units_consumed = 50_000
            try:
                simulation = await simulate_unsigned_transaction(
                    self._client,
                    payer=self._payer,
                    instructions=checked,
                    recent_blockhash=blockhash,
                    max_compute_units=self.compute_unit_limit,
                    max_loaded_accounts_data_size=self.loaded_accounts_data_size_limit,
                )
                if simulation.units_consumed is not None:
                    units_consumed = simulation.units_consumed
            except Exception:
                pass
            estimated_fee = _estimated_fee_lamports(
                units_consumed=units_consumed,
                priority_fee_microlamports=self.fixed_priority_fee_microlamports,
                jito_tip_lamports=(
                    self.jito_tip_lamports
                    if self.routing_policy is RoutingPolicy.JITO_ONLY
                    else 0
                ),
            )
            simulated_output = (
                context.amount if intent.side == "buy" else context.quote_limit
            )
            return ExecutionReceipt(
                mode=ExecutionMode.SIMULATION,
                intent_id=intent.intent_id,
                as_of_slot=intent.as_of_slot,
                accepted=True,
                would_submit_transaction=False,
                signature=None,
                simulated_output_base_units=simulated_output,
                estimated_fee_lamports=Lamports(estimated_fee),
                message=(
                    f"Pump V2 {intent.side} simulated at finalized slot {slot}; "
                    f"route={self.routing_policy.value}; no signature or broadcast"
                ),
            )
        except (SimulationError, TypeError, ValueError) as error:
            return ExecutionReceipt(
                mode=ExecutionMode.SIMULATION,
                intent_id=intent.intent_id,
                as_of_slot=intent.as_of_slot,
                accepted=False,
                would_submit_transaction=False,
                signature=None,
                simulated_output_base_units=None,
                estimated_fee_lamports=Lamports(0),
                message=f"route simulation failed: {type(error).__name__}: {error}",
            )


def _estimated_fee_lamports(
    *,
    units_consumed: int | None,
    priority_fee_microlamports: int,
    jito_tip_lamports: int,
) -> int:
    """Estimate configured delivery cost from the simulation CU usage."""

    if units_consumed is None:
        priority_fee = 0
    else:
        priority_fee = units_consumed * priority_fee_microlamports // 1_000_000
    return priority_fee + jito_tip_lamports


__all__ = ["SimulationPumpExecutionPort"]
