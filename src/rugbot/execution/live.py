"""Live Pump bonding-curve execution behind the explicit live mode gate."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from solders.pubkey import Pubkey

from core.client import SolanaClient
from core.priority_fee.manager import PriorityFeeManager
from core.wallet import Wallet
from interfaces.core import (
    AddressProvider,
    CurveManager,
    InstructionBuilder,
    Platform,
    TokenInfo,
)
from platforms import get_platform_implementations
from rugbot.domain.amounts import Lamports
from rugbot.execution.ports import (
    ExecutionIntent,
    ExecutionMode,
    ExecutionReceipt,
    non_submitting_receipt,
    validate_execution_intent,
)

if TYPE_CHECKING:
    from solders.instruction import Instruction


@dataclass(slots=True)
class LivePumpExecutionPort:
    """Submit one Pump bonding-curve intent using the existing SDK path.

    The port deliberately resolves the curve and mint program immediately
    before every transaction.  It never trusts launch-time account flags for
    a sell, and it uses the same integer quote inputs as paper/backtest code.
    """

    endpoint: str
    private_key: str
    max_retries: int = 2
    fixed_priority_fee_microlamports: int = 200_000
    _client: SolanaClient = field(init=False, repr=False)
    _wallet: Wallet = field(init=False, repr=False)
    _priority_fees: PriorityFeeManager = field(init=False, repr=False)

    def __post_init__(self) -> None:
        if not self.endpoint or not self.private_key:
            raise ValueError(  # noqa: TRY003
                "live execution requires RPC endpoint and private key"
            )
        if type(self.max_retries) is not int or self.max_retries < 1:
            raise ValueError("max_retries must be positive")  # noqa: TRY003
        if (
            type(self.fixed_priority_fee_microlamports) is not int
            or self.fixed_priority_fee_microlamports < 0
        ):
            raise ValueError("fixed priority fee must be non-negative")  # noqa: TRY003
        self._client = SolanaClient(self.endpoint)
        self._wallet = Wallet(self.private_key)
        self._priority_fees = PriorityFeeManager(
            client=self._client,
            enable_dynamic_fee=False,
            enable_fixed_fee=self.fixed_priority_fee_microlamports > 0,
            fixed_fee=self.fixed_priority_fee_microlamports,
            extra_fee=0,
            hard_cap=self.fixed_priority_fee_microlamports,
        )

    async def submit(self, intent: ExecutionIntent) -> ExecutionReceipt:
        """Build, sign, submit, and confirm one live Pump intent."""

        intent_error = validate_execution_intent(intent)
        if intent_error is not None:
            return non_submitting_receipt(
                mode=ExecutionMode.LIVE,
                intent=intent if isinstance(intent, ExecutionIntent) else None,
                message=intent_error,
                estimated_fee_lamports=Lamports(0),
            )

        try:
            mint = Pubkey.from_string(intent.market_id)
            implementations = get_platform_implementations(
                Platform.PUMP_FUN, self._client
            )
            provider = implementations.address_provider
            builder = implementations.instruction_builder
            curve_manager = implementations.curve_manager
            curve = provider.derive_pool_address(mint)
            state = await curve_manager.get_pool_state(curve, commitment="processed")
            token_program = await self._mint_owner(mint)
            creator = Pubkey.from_string(str(state["creator"]))
            token = TokenInfo(
                name="",
                symbol="",
                uri="",
                mint=mint,
                platform=Platform.PUMP_FUN,
                bonding_curve=curve,
                associated_bonding_curve=provider.derive_associated_bonding_curve(
                    mint, curve, token_program
                ),
                user=self._wallet.pubkey,
                creator=creator,
                creator_vault=provider.derive_creator_vault(creator),
                token_program_id=token_program,
                is_mayhem_mode=bool(state.get("is_mayhem_mode", False)),
                is_cashback_coin=bool(state.get("is_cashback_coin", False)),
            )
            if intent.side == "buy":
                receipt = await self._submit_buy(
                    intent=intent,
                    token=token,
                    builder=builder,
                    provider=provider,
                    curve_manager=curve_manager,
                )
            else:
                receipt = await self._submit_sell(
                    intent=intent,
                    token=token,
                    builder=builder,
                    provider=provider,
                    curve_manager=curve_manager,
                )
            return receipt  # noqa: TRY300
        except Exception as error:  # noqa: BLE001
            return ExecutionReceipt(
                mode=ExecutionMode.LIVE,
                intent_id=intent.intent_id,
                as_of_slot=intent.as_of_slot,
                accepted=False,
                would_submit_transaction=False,
                signature=None,
                simulated_output_base_units=None,
                estimated_fee_lamports=Lamports(0),
                message=f"live execution failed: {type(error).__name__}",
            )

    async def _submit_buy(
        self,
        *,
        intent: ExecutionIntent,
        token: TokenInfo,
        builder: InstructionBuilder,
        provider: AddressProvider,
        curve_manager: CurveManager,
    ) -> ExecutionReceipt:
        amount_in = int(intent.quote_amount_base_units)
        output = await curve_manager.calculate_buy_amount_out(
            token.bonding_curve, amount_in
        )
        minimum_output = output * (10_000 - intent.max_slippage_bps) // 10_000
        instructions = await builder.build_buy_instruction(
            token,
            self._wallet.pubkey,
            amount_in,
            minimum_output,
            provider,
        )
        signature = await self._send(
            instructions,
            builder.get_required_accounts_for_buy(token, self._wallet.pubkey, provider),
            builder.get_buy_compute_unit_limit(),
        )
        if not await self._client.confirm_transaction(signature):
            return _failed_receipt(intent, signature, "buy confirmation failed")
        actual_output, _ = await self._client.get_buy_transaction_details(
            signature, token.mint, token.bonding_curve
        )
        return ExecutionReceipt(
            mode=ExecutionMode.LIVE,
            intent_id=intent.intent_id,
            as_of_slot=intent.as_of_slot,
            accepted=True,
            would_submit_transaction=True,
            signature=signature,
            simulated_output_base_units=actual_output or output,
            estimated_fee_lamports=Lamports(0),
            message="live buy confirmed",
        )

    async def _submit_sell(
        self,
        *,
        intent: ExecutionIntent,
        token: TokenInfo,
        builder: InstructionBuilder,
        provider: AddressProvider,
        curve_manager: CurveManager,
    ) -> ExecutionReceipt:
        amount_in = int(intent.base_amount_base_units)
        output = await curve_manager.calculate_sell_amount_out(
            token.bonding_curve, amount_in
        )
        minimum_output = output * (10_000 - intent.max_slippage_bps) // 10_000
        instructions = await builder.build_sell_instruction(
            token,
            self._wallet.pubkey,
            amount_in,
            minimum_output,
            provider,
        )
        signature = await self._send(
            instructions,
            builder.get_required_accounts_for_sell(
                token, self._wallet.pubkey, provider
            ),
            builder.get_sell_compute_unit_limit(),
        )
        if not await self._client.confirm_transaction(signature):
            return _failed_receipt(intent, signature, "sell confirmation failed")
        return ExecutionReceipt(
            mode=ExecutionMode.LIVE,
            intent_id=intent.intent_id,
            as_of_slot=intent.as_of_slot,
            accepted=True,
            would_submit_transaction=True,
            signature=signature,
            simulated_output_base_units=None,
            estimated_fee_lamports=Lamports(0),
            message="live sell confirmed",
        )

    async def _send(
        self,
        instructions: list[Instruction],
        accounts: list[Pubkey],
        compute_units: int,
    ) -> str:
        priority_fee = await self._priority_fees.calculate_priority_fee(accounts)
        return await self._client.build_and_send_transaction(
            instructions,
            self._wallet.keypair,
            skip_preflight=False,
            max_retries=self.max_retries,
            priority_fee=priority_fee,
            compute_unit_limit=compute_units,
        )

    async def _mint_owner(self, mint: Pubkey) -> Pubkey:
        account = await self._client.get_account_info(mint, commitment="processed")
        owner = getattr(account, "owner", None)
        if not isinstance(owner, Pubkey):
            raise TypeError("mint owner is missing")  # noqa: TRY003
        return owner


def _failed_receipt(
    intent: ExecutionIntent, signature: str, message: str
) -> ExecutionReceipt:
    """Return a failed live receipt without claiming a successful fill."""

    return ExecutionReceipt(
        mode=ExecutionMode.LIVE,
        intent_id=intent.intent_id,
        as_of_slot=intent.as_of_slot,
        accepted=False,
        would_submit_transaction=True,
        signature=signature,
        simulated_output_base_units=None,
        estimated_fee_lamports=Lamports(0),
        message=message,
    )
