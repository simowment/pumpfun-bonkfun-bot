"""Validated Pump.fun V2 live execution behind a signer/configuration gate.

The adapter is intentionally not used by observe or paper modes.  It fetches
all protocol state at finalized commitment, builds the documented V2 account
layout, validates the complete instruction set before signing, simulates each
unsigned transaction, signs once, broadcasts identical bytes, and waits for
finalized landing evidence.
"""

# The live adapter is an orchestration boundary; pure validation and RPC
# details remain split into their dedicated modules.
# ruff: noqa: C901, PLR0911, PLR0912, PLR0913, PLR0915, PLR2004, TRY003, TRY004, TC002

from __future__ import annotations

import base64
import hashlib
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING
from uuid import uuid4

import base58
from rugbot.execution.simulation import SimulationError, simulate_unsigned_transaction
from solders.compute_budget import set_compute_unit_limit, set_compute_unit_price
from solders.instruction import Instruction
from solders.keypair import Keypair
from solders.message import Message
from solders.pubkey import Pubkey
from solders.transaction import Transaction

from rugbot.domain.amounts import Lamports
from rugbot.domain.decisions import AbstainReason, AbstainResult
from rugbot.domain.observations import RawChainObservation
from rugbot.domain.quote_engine import (
    CANONICAL_PUMP_PROGRAM_CONFIG_VERSION,
    PoolReserves,
    executable_buy_quote,
    executable_sell_quote,
)
from rugbot.domain.quotes import QuotePath
from rugbot.domain.version_registry import (
    PUMP_VERSION_REGISTRY_VERSION,
    PumpFeeScheduleVersion,
    PumpProgramConfigVersion,
    PumpVersionResolveRequest,
    resolve_pump_protocol_versions,
)
from rugbot.execution.firewall import FirewallPolicy, validate_pump_v2_instructions
from rugbot.execution.landing import (
    FinalizedLanding,
    observe_finalized_signature,
    wait_for_finalized_signatures,
)
from rugbot.execution.landing_reconciliation import (
    LandingReconciliationError,
    reconcile_finalized_landing,
)
from rugbot.execution.ports import (
    ExecutionIntent,
    ExecutionMode,
    ExecutionReceipt,
    non_submitting_receipt,
    validate_execution_intent,
)
from rugbot.execution.sender import (
    JitoSender,
    RoutingPolicy,
    RpcSender,
    TransactionRouter,
    create_jito_tip_instruction,
)
from rugbot.execution.v2_builder import (
    BONDING_CURVE_SEED,
    PumpV2BuildContext,
    build_buy_v2_instructions,
    build_sell_v2_instructions,
    derive_pump_pda,
)
from rugbot.ingest.pump.bonding_curve_account import (
    PINNED_PUMP_IDL_SHA256,
    PUMP_BONDING_CURVE_LAYOUT_ARTIFACT_VERSION,
    PUMP_PROGRAM_ID,
    PumpBondingCurveAccountState,
    PumpBondingCurveDecodeRequest,
    bonding_curve_snapshot_to_pool_reserves,
    decode_pump_bonding_curve_account,
)
from rugbot.ingest.pump.create_decoder import (
    SPL_2022_PROGRAM_ID,
    WSOL_MINT_ID,
)
from rugbot.ingest.pump.fee_config_account import (
    PUMP_FEE_CONFIG_PDA,
    decode_pump_fee_config_account,
)
from rugbot.ingest.pump.global_account import (
    PUMP_GLOBAL_PDA,
    decode_pump_global_account,
)
from rugbot.ingest.pump.mint_account import (
    decode_spl_token_2022_mint_metadata,
)
from rugbot.integrations.solana_rpc import (
    SolanaClient,
    set_loaded_accounts_data_size_limit,
)
from rugbot.storage.transaction_state import (
    SqliteTransactionStateStore,
    TransactionIntentRecord,
    TransactionState,
)

if TYPE_CHECKING:
    from pathlib import Path

    from rugbot.execution.telemetry import ExecutionMetrics


@dataclass(slots=True)
class LivePumpExecutionPort:
    """Execute a Pump V2 intent only after strict local and RPC gates."""

    endpoint: str
    private_key: str
    max_retries: int = 2
    fixed_priority_fee_microlamports: int = 200_000
    jito_tip_lamports: int = 1_000_000
    compute_unit_limit: int = 400_000
    loaded_accounts_data_size_limit: int = 128_000
    routing_policy: RoutingPolicy = RoutingPolicy.JITO_ONLY
    jito_block_engine_url: str = JitoSender.DEFAULT_BLOCK_ENGINE_URL
    transaction_state_path: Path | None = None
    _client: SolanaClient = field(init=False, repr=False)
    _keypair: Keypair = field(init=False, repr=False)
    _router: TransactionRouter = field(init=False, repr=False)
    _jito_sender: JitoSender = field(init=False, repr=False)
    _rpc_sender: RpcSender = field(init=False, repr=False)
    _transaction_store: SqliteTransactionStateStore | None = field(
        init=False,
        default=None,
        repr=False,
    )
    _initialized: bool = field(init=False, default=False, repr=False)

    def __post_init__(self) -> None:
        if not self.endpoint:
            raise ValueError("live execution requires an RPC endpoint")
        if not self.private_key:
            raise ValueError("live execution requires a signing key")
        if type(self.max_retries) is not int or self.max_retries < 1:
            raise ValueError("max_retries must be positive")
        if type(self.jito_tip_lamports) is not int or self.jito_tip_lamports < 0:
            raise ValueError("Jito tip must be non-negative")
        if type(self.compute_unit_limit) is not int or self.compute_unit_limit <= 0:
            raise ValueError("compute unit limit must be positive")
        if (
            type(self.loaded_accounts_data_size_limit) is not int
            or self.loaded_accounts_data_size_limit <= 0
        ):
            raise ValueError("loaded account data limit must be positive")
        try:
            self._keypair = _keypair_from_base58(self.private_key)
        except (ValueError, TypeError) as error:
            raise ValueError("signing key is not a valid Solana keypair") from error
        self._client = SolanaClient(self.endpoint)
        self._rpc_sender = RpcSender(self.endpoint, client=self._client)
        self._jito_sender = JitoSender(block_engine_url=self.jito_block_engine_url)
        self._router = TransactionRouter(
            rpc_sender=self._rpc_sender,
            jito_sender=self._jito_sender,
        )
        if self.transaction_state_path is not None:
            self._transaction_store = SqliteTransactionStateStore(
                self.transaction_state_path
            )

    @property
    def signer_pubkey(self) -> str:
        """Return the configured signer public key without exposing secret bytes."""

        return str(self._keypair.pubkey())

    async def initialize(self) -> None:
        """Refresh Jito tip accounts before constructing a tipped variant."""

        if self._initialized:
            return
        if self.routing_policy is RoutingPolicy.JITO_ONLY:
            await self._jito_sender.initialize_tip_accounts()
        self._initialized = True

    async def close(self) -> None:
        """Close all owned RPC and sender resources."""

        await self._jito_sender.close()
        await self._client.close()
        if self._transaction_store is not None:
            self._transaction_store.close()

    async def recover_pending(self) -> tuple[TransactionIntentRecord, ...]:
        """Resolve every durable non-terminal transaction before new trading."""

        if self._transaction_store is None:
            raise ValueError("durable transaction state is required for recovery")
        await self.initialize()
        recovered: list[TransactionIntentRecord] = []
        for record in self._transaction_store.list_recovery_pending():
            if record.state is TransactionState.INTENT:
                recovered.append(
                    self._transaction_store.mark_cancelled(
                        record.intent_id,
                        error_message="unsigned intent cancelled during restart recovery",
                    )
                )
                continue
            if record.state is TransactionState.SIGNED:
                recovered.append(
                    self._transaction_store.mark_cancelled(
                        record.intent_id,
                        error_message=(
                            "signed bytes were never handed to a network sender"
                        ),
                    )
                )
                continue
            if record.state is TransactionState.CONFIRMED:
                recovered.append(await self._reconcile_recovered(record))
                continue
            recovered.append(await self._recover_submitted(record))
        return tuple(recovered)

    async def _recover_submitted(
        self,
        record: TransactionIntentRecord,
    ) -> TransactionIntentRecord:
        signature, raw_transaction, last_valid_block_height = _signed_recovery_facts(
            record
        )
        landing = await observe_finalized_signature(self._client, signature)
        if not landing.finalized and landing.transaction_found:
            landing = (await wait_for_finalized_signatures(self._client, (signature,)))[
                0
            ]
        if landing.finalized:
            return await self._finish_recovered_landing(record, landing)
        current_block_height = await _get_block_height(self._client)
        if current_block_height > last_valid_block_height:
            return self._transaction_store.mark_expired(
                record.intent_id,
                error_message=(
                    "submitted signature was absent after its blockhash expired"
                ),
            )
        submission = await self._router.route(
            raw_transaction,
            policy=self.routing_policy,
        )
        if submission.acknowledged and submission.signature != signature:
            raise ValueError(
                "recovery sender acknowledged a signature that does not match "
                "durable signed bytes"
            )
        if not submission.acknowledged:
            return record
        landing = (await wait_for_finalized_signatures(self._client, (signature,)))[0]
        if not landing.finalized:
            return record
        return await self._finish_recovered_landing(record, landing)

    async def _finish_recovered_landing(
        self,
        record: TransactionIntentRecord,
        landing: FinalizedLanding,
    ) -> TransactionIntentRecord:
        if landing.err is not None:
            return self._transaction_store.mark_failed(
                record.intent_id,
                error_code="transaction_execution_failed",
                error_message=str(landing.err),
            )
        if type(landing.slot) is not int or landing.slot < 0:
            raise ValueError("recovery landing slot is malformed")
        confirmed = self._transaction_store.mark_confirmed(
            record.intent_id,
            landed_slot=landing.slot,
            confirmed_at_ts=time.time_ns() // 1_000_000,
        )
        return await self._reconcile_recovered(confirmed)

    async def _reconcile_recovered(
        self,
        record: TransactionIntentRecord,
    ) -> TransactionIntentRecord:
        signature, _raw_transaction, _last_valid_height = _signed_recovery_facts(record)
        reconciliation = await reconcile_finalized_landing(
            self._client,
            signature=signature,
            wallet_pubkey=record.wallet_pubkey,
            mint=record.market_id,
            side=record.side,
            jito_tip_accounts=self._jito_sender.tip_accounts,
            expected_jito_tip_lamports=(
                self.jito_tip_lamports
                if self.routing_policy is RoutingPolicy.JITO_ONLY
                else 0
            ),
        )
        return self._transaction_store.mark_reconciled(
            record.intent_id,
            reconciled_at_ts=time.time_ns() // 1_000_000,
            token_delta_base_units=reconciliation.token_delta_base_units,
            sol_delta_lamports=reconciliation.sol_delta_lamports,
            network_fee_lamports=reconciliation.network_fee_lamports,
            jito_tip_lamports=reconciliation.jito_tip_lamports,
            ata_rent_lamports=reconciliation.ata_rent_lamports,
            protocol_fee_lamports=reconciliation.protocol_fee_lamports,
        )

    async def submit(
        self,
        intent: ExecutionIntent,
        telemetry: ExecutionMetrics | None = None,
    ) -> ExecutionReceipt:
        """Build, firewall, simulate, sign, broadcast, and reconcile one intent."""

        started_ns = time.time_ns()
        if telemetry is not None and telemetry.t_received_ns == 0:
            telemetry.t_received_ns = started_ns
        intent_error = validate_execution_intent(intent)
        if intent_error is not None:
            return non_submitting_receipt(
                mode=ExecutionMode.LIVE,
                intent=intent if isinstance(intent, ExecutionIntent) else None,
                message=intent_error,
                estimated_fee_lamports=Lamports(0),
            )
        if self._transaction_store is None:
            return non_submitting_receipt(
                mode=ExecutionMode.LIVE,
                intent=intent,
                message="durable transaction state is required for live execution",
                estimated_fee_lamports=Lamports(0),
            )

        durable_intent = self._transaction_store.create_intent(
            intent,
            wallet_pubkey=self.signer_pubkey,
        )
        if durable_intent.state is not TransactionState.INTENT:
            return non_submitting_receipt(
                mode=ExecutionMode.LIVE,
                intent=intent,
                message=(
                    f"intent already exists in {durable_intent.state}; "
                    "restart recovery must resolve it"
                ),
                estimated_fee_lamports=Lamports(0),
            )

        try:
            await self.initialize()
            mint = Pubkey.from_string(intent.market_id)
            slot, accounts = await _fetch_trade_accounts(self._client, mint)
            context_result = _build_trade_context(
                accounts=accounts,
                mint=mint,
                user=self._keypair.pubkey(),
                intent=intent,
            )
            if isinstance(context_result, AbstainResult):
                return _abstain_receipt(intent, context_result.message)
            context, reserves = context_result
            instructions = _build_transaction_instructions(
                context=context,
                reserves=reserves,
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
                payer=self._keypair.pubkey(),
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
            if telemetry is not None:
                telemetry.t_built_ns = time.time_ns()
            (
                blockhash,
                last_valid_block_height,
            ) = await self._client.get_cached_blockhash_context()
            await simulate_unsigned_transaction(
                self._client,
                payer=self._keypair.pubkey(),
                instructions=checked,
                recent_blockhash=blockhash,
                max_compute_units=self.compute_unit_limit,
                max_loaded_accounts_data_size=self.loaded_accounts_data_size_limit,
            )
            message = Message(list(checked), self._keypair.pubkey())
            transaction = Transaction([self._keypair], message, blockhash)
            raw_transaction = bytes(transaction)
            signature = str(transaction.signatures[0])
            self._transaction_store.store_signed(
                intent.intent_id,
                raw_tx_bytes=raw_transaction,
                signature=signature,
                blockhash=str(blockhash),
                last_valid_block_height=last_valid_block_height,
            )
            if telemetry is not None:
                telemetry.t_signed_ns = time.time_ns()
                telemetry.event_slot = slot
                telemetry.creation_slot = slot
            self._transaction_store.mark_submitted(
                intent.intent_id,
                submitted_at_ts=time.time_ns() // 1_000_000,
            )
            submission = await self._router.route(
                raw_transaction,
                policy=self.routing_policy,
                telemetry=telemetry,
            )
            if not submission.acknowledged or not submission.signature:
                return _failed_receipt(intent, "no sender acknowledged the transaction")
            if submission.signature != signature:
                return _failed_receipt(
                    intent,
                    "sender acknowledged a signature that does not match signed bytes",
                )
            signatures = (signature,)
            landings = await wait_for_finalized_signatures(self._client, signatures)
            winner = next(
                (
                    landing
                    for landing in landings
                    if landing.finalized and landing.err is None
                ),
                None,
            )
            if winner is None:
                finalized_error = next(
                    (
                        landing
                        for landing in landings
                        if landing.finalized and landing.err is not None
                    ),
                    None,
                )
                if finalized_error is not None:
                    self._transaction_store.mark_failed(
                        intent.intent_id,
                        error_code="transaction_execution_failed",
                        error_message=str(finalized_error.err),
                    )
                return _failed_receipt(intent, "no variant finalized successfully")
            self._transaction_store.mark_confirmed(
                intent.intent_id,
                landed_slot=int(winner.slot),
                confirmed_at_ts=time.time_ns() // 1_000_000,
            )
            try:
                reconciliation = await reconcile_finalized_landing(
                    self._client,
                    signature=winner.signature,
                    wallet_pubkey=self.signer_pubkey,
                    mint=intent.market_id,
                    side=intent.side,
                    jito_tip_accounts=self._jito_sender.tip_accounts,
                    expected_jito_tip_lamports=(
                        self.jito_tip_lamports
                        if self.routing_policy is RoutingPolicy.JITO_ONLY
                        else 0
                    ),
                )
            except LandingReconciliationError as error:
                if telemetry is not None:
                    telemetry.error = str(error)
                return ExecutionReceipt(
                    mode=ExecutionMode.LIVE,
                    intent_id=intent.intent_id,
                    as_of_slot=intent.as_of_slot,
                    accepted=True,
                    would_submit_transaction=True,
                    signature=winner.signature,
                    simulated_output_base_units=None,
                    estimated_fee_lamports=None,
                    message=(
                        f"Pump V2 {intent.side} finalized at slot {winner.slot}; "
                        "exact reconciliation is pending"
                    ),
                )
            self._transaction_store.mark_reconciled(
                intent.intent_id,
                reconciled_at_ts=time.time_ns() // 1_000_000,
                token_delta_base_units=reconciliation.token_delta_base_units,
                sol_delta_lamports=reconciliation.sol_delta_lamports,
                network_fee_lamports=reconciliation.network_fee_lamports,
                jito_tip_lamports=reconciliation.jito_tip_lamports,
                ata_rent_lamports=reconciliation.ata_rent_lamports,
                protocol_fee_lamports=reconciliation.protocol_fee_lamports,
            )
            if telemetry is not None:
                telemetry.landed_slot = winner.slot
                telemetry.first_observed_ns = time.time_ns()
                telemetry.success = True
            return ExecutionReceipt(
                mode=ExecutionMode.LIVE,
                intent_id=intent.intent_id,
                as_of_slot=intent.as_of_slot,
                accepted=True,
                would_submit_transaction=True,
                signature=winner.signature,
                simulated_output_base_units=(
                    reconciliation.token_delta_base_units
                    if intent.side == "buy"
                    else reconciliation.sol_delta_lamports
                ),
                estimated_fee_lamports=Lamports(
                    reconciliation.network_fee_lamports
                    + reconciliation.jito_tip_lamports
                    + reconciliation.ata_rent_lamports
                    + reconciliation.protocol_fee_lamports
                ),
                message=(
                    f"Pump V2 {intent.side} finalized and reconciled "
                    f"at slot {winner.slot}"
                ),
            )
        except (SimulationError, ValueError, TypeError) as error:
            durable_after_error = self._transaction_store.get(intent.intent_id)
            if durable_after_error is not None and durable_after_error.state in (
                TransactionState.INTENT,
                TransactionState.SIGNED,
            ):
                self._transaction_store.mark_failed(
                    intent.intent_id,
                    error_code=type(error).__name__,
                    error_message=str(error) or type(error).__name__,
                )
            if telemetry is not None:
                telemetry.error = str(error)
            return _failed_receipt(
                intent, f"live execution abstained: {type(error).__name__}"
            )


def _build_transaction_instructions(
    *,
    context: PumpV2BuildContext,
    reserves: PoolReserves,
    intent: ExecutionIntent,
    jito_tip_account: Pubkey | None,
    compute_unit_limit: int,
    loaded_accounts_data_size_limit: int,
    priority_fee_microlamports: int,
    jito_tip_lamports: int,
) -> tuple[Instruction, ...]:
    if intent.side == "buy":
        built = build_buy_v2_instructions(context)
    else:
        built = build_sell_v2_instructions(context)
    compute = (
        set_loaded_accounts_data_size_limit(loaded_accounts_data_size_limit),
        set_compute_unit_limit(compute_unit_limit),
        set_compute_unit_price(priority_fee_microlamports),
    )
    del reserves
    delivery = ()
    if jito_tip_account is not None:
        delivery = (
            create_jito_tip_instruction(
                context.user,
                jito_tip_lamports,
                jito_tip_account,
            ),
        )
    return compute + delivery + built.instructions


def _build_trade_context(
    *,
    accounts: dict[str, RawChainObservation],
    mint: Pubkey,
    user: Pubkey,
    intent: ExecutionIntent,
) -> tuple[PumpV2BuildContext, PoolReserves] | AbstainResult:
    slot = accounts["global"].slot
    global_state = decode_pump_global_account(accounts["global"])
    fee_state = decode_pump_fee_config_account(accounts["fee_config"])
    mint_state = decode_spl_token_2022_mint_metadata(
        accounts["mint"], mint_pubkey=str(mint)
    )
    if isinstance(global_state, AbstainResult):
        return global_state
    if not global_state.initialized or not global_state.create_v2_enabled:
        return _abstain_result(
            "Pump Global is not initialized for buy_v2/sell_v2",
            slot,
        )
    if isinstance(fee_state, AbstainResult):
        return fee_state
    if isinstance(mint_state, AbstainResult):
        return mint_state
    program_source = f"live:global:{global_state.raw_account_data_sha256}"
    fee_source = f"live:fee:{hashlib.sha256(accounts['fee_config'].raw_account_data or b'').hexdigest()}"
    protocol = resolve_pump_protocol_versions(
        request=PumpVersionResolveRequest(
            as_of_slot=slot,
            program_id=PUMP_PROGRAM_ID,
            idl_hash=PINNED_PUMP_IDL_SHA256,
            global_config_hash=global_state.raw_account_data_sha256,
        ),
        program_configs=(
            PumpProgramConfigVersion(
                version=CANONICAL_PUMP_PROGRAM_CONFIG_VERSION,
                program_id=PUMP_PROGRAM_ID,
                idl_hash=PINNED_PUMP_IDL_SHA256,
                global_config_hash=global_state.raw_account_data_sha256,
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
    if isinstance(protocol, AbstainResult):
        return protocol
    curve = decode_pump_bonding_curve_account(
        PumpBondingCurveDecodeRequest(
            account_state=PumpBondingCurveAccountState(
                as_of_slot=slot,
                account_pubkey=str(derive_pump_pda(BONDING_CURVE_SEED, mint)),
                owner_program_id=PUMP_PROGRAM_ID,
                raw_account_data=accounts["bonding_curve"].raw_account_data or b"",
                source_artifact_version="live:bonding-curve",
                layout_artifact_version=PUMP_BONDING_CURVE_LAYOUT_ARTIFACT_VERSION,
            ),
            protocol_snapshot=protocol,
            idl_hash=PINNED_PUMP_IDL_SHA256,
            base_decimals=mint_state.decimals,
            quote_decimals=9,
            base_mint=str(mint),
            quote_mint=WSOL_MINT_ID,
        )
    )
    if isinstance(curve, AbstainResult):
        return curve
    reserves = bonding_curve_snapshot_to_pool_reserves(curve)
    if isinstance(reserves, AbstainResult):
        return reserves
    if intent.side == "buy":
        quote = executable_buy_quote(
            path=QuotePath.PUMP_BONDING_CURVE,
            reserves=reserves,
            quote_input_amount=int(intent.quote_amount_base_units),
            fee_config=protocol.fee_config,
        )
        if isinstance(quote, AbstainResult):
            return quote
        amount = quote.output_amount_base_units
        quote_limit = int(intent.quote_amount_base_units)
    else:
        quote = executable_sell_quote(
            path=QuotePath.PUMP_BONDING_CURVE,
            reserves=reserves,
            base_input_amount=int(intent.base_amount_base_units),
            fee_config=protocol.fee_config,
        )
        if isinstance(quote, AbstainResult):
            return quote
        amount = int(intent.base_amount_base_units)
        quote_limit = (
            quote.output_amount_base_units
            * (10_000 - intent.max_slippage_bps)
            // 10_000
        )
    creator = Pubkey(curve.creator)
    fee_recipient = Pubkey.from_string(
        global_state.reserved_fee_recipient
        if curve.is_mayhem_mode
        else global_state.fee_recipient_pubkey
    )
    if not global_state.buyback_fee_recipients:
        return _abstain_result(
            "Pump Global buyback fee recipients are missing",
            slot,
        )
    return (
        PumpV2BuildContext(
            mint=mint,
            creator=creator,
            user=user,
            base_token_program=Pubkey.from_string(SPL_2022_PROGRAM_ID),
            fee_recipient=fee_recipient,
            buyback_fee_recipient=Pubkey.from_string(
                global_state.buyback_fee_recipients[0]
            ),
            amount=amount,
            quote_limit=max(1, quote_limit),
        ),
        reserves,
    )


async def _fetch_trade_accounts(
    client: SolanaClient,
    mint: Pubkey,
) -> tuple[int, dict[str, RawChainObservation]]:
    addresses = (
        ("global", Pubkey.from_string(PUMP_GLOBAL_PDA)),
        ("fee_config", Pubkey.from_string(PUMP_FEE_CONFIG_PDA)),
        ("mint", mint),
        ("bonding_curve", derive_pump_pda(BONDING_CURVE_SEED, mint)),
    )
    response = await client.post_rpc(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "getMultipleAccounts",
            "params": [
                [str(address) for _, address in addresses],
                {"encoding": "base64", "commitment": "finalized"},
            ],
        }
    )
    if not isinstance(response, dict) or response.get("error") is not None:
        raise ValueError("finalized Pump account RPC request failed")
    result = response.get("result")
    if not isinstance(result, dict) or not isinstance(result.get("value"), list):
        raise ValueError("finalized Pump account RPC response is malformed")
    slot = result.get("context", {}).get("slot")
    values = result["value"]
    if type(slot) is not int or slot < 0 or len(values) != len(addresses):
        raise ValueError("finalized Pump account context is malformed")
    observations: dict[str, RawChainObservation] = {}
    for (role, address), value in zip(addresses, values, strict=True):
        if not isinstance(value, dict):
            raise ValueError(f"finalized {role} account is missing")
        data = value.get("data")
        if not isinstance(data, list) or len(data) != 2 or data[1] != "base64":
            raise ValueError(f"finalized {role} account encoding is unsupported")
        raw_data = base64.b64decode(data[0], validate=True)
        owner = base58.b58decode(value["owner"])
        observations[role] = RawChainObservation(
            raw_id=uuid4(),
            source_id="solana-http-rpc-account-info",
            observer_id="rugbot-live-v2",
            boot_id=uuid4(),
            receive_sequence=0,
            slot=slot,
            parent_slot=None,
            blockhash=None,
            signature=None,
            transaction_index=None,
            outer_instruction_index=None,
            inner_instruction_group_index=None,
            inner_instruction_index=None,
            stack_height=None,
            event_ordinal=None,
            commitment="finalized",
            canonical_status="canonical",
            received_wall_ns=time.time_ns(),
            received_monotonic_ns=time.perf_counter_ns(),
            program_id=None,
            account_pubkey=bytes(address),
            account_owner_program_id=owner,
            raw_transaction=None,
            raw_transaction_format=None,
            raw_account_data=raw_data,
            account_write_version=None,
            source_update_kind="account",
            raw_source_status=None,
            raw_source_payload=None,
            decoder_name=None,
            decoder_version=None,
            idl_hash=None,
        )
    return slot, observations


def _keypair_from_base58(value: str) -> Keypair:
    """Decode a base58 key or an explicitly prefixed base64 key."""

    if value.startswith("base64:"):
        decoded = base64.b64decode(value.removeprefix("base64:"), validate=True)
    else:
        decoded = base58.b58decode(value)
    if len(decoded) != 64:
        raise ValueError("Solana keypair bytes must be exactly 64 bytes")
    return Keypair.from_bytes(decoded)


def _signed_recovery_facts(
    record: TransactionIntentRecord,
) -> tuple[str, bytes, int]:
    if (
        type(record.signature) is not str
        or not record.signature
        or type(record.raw_tx_bytes) is not bytes
        or not record.raw_tx_bytes
        or type(record.last_valid_block_height) is not int
        or record.last_valid_block_height < 0
    ):
        raise ValueError("durable signed transaction recovery facts are incomplete")
    signed_signature = str(Transaction.from_bytes(record.raw_tx_bytes).signatures[0])
    if signed_signature != record.signature:
        raise ValueError("durable transaction signature does not match signed bytes")
    return record.signature, record.raw_tx_bytes, record.last_valid_block_height


async def _get_block_height(client: SolanaClient) -> int:
    response = await client.post_rpc(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "getBlockHeight",
            "params": [{"commitment": "processed"}],
        }
    )
    if not isinstance(response, dict) or response.get("error") is not None:
        raise ValueError("block height RPC response is malformed")
    block_height = response.get("result")
    if type(block_height) is not int or block_height < 0:
        raise ValueError("block height RPC result is malformed")
    return block_height


def _abstain_receipt(intent: ExecutionIntent, message: str) -> ExecutionReceipt:
    return non_submitting_receipt(
        mode=ExecutionMode.LIVE,
        intent=intent,
        message=message,
        estimated_fee_lamports=Lamports(0),
    )


def _abstain_result(message: str, as_of_slot: int) -> AbstainResult:
    """Return a fail-closed context abstention."""

    return AbstainResult(
        reason=AbstainReason.UNKNOWN_PROTOCOL_STATE,
        message=message,
        as_of_slot=as_of_slot,
    )


def _failed_receipt(intent: ExecutionIntent, message: str) -> ExecutionReceipt:
    return ExecutionReceipt(
        mode=ExecutionMode.LIVE,
        intent_id=intent.intent_id,
        as_of_slot=intent.as_of_slot,
        accepted=False,
        would_submit_transaction=True,
        signature=None,
        simulated_output_base_units=None,
        estimated_fee_lamports=Lamports(0),
        message=message,
    )


__all__ = ["LivePumpExecutionPort"]
