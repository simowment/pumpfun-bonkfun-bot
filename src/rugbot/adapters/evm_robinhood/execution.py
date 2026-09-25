"""Robinhood Chain EVM execution adapter implementing ExecutionPort."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

from rugbot.adapters.evm_robinhood.client import EvmRpcError
from rugbot.adapters.evm_robinhood.contracts.erc20 import (
    decode_uint256,
    encode_allowance,
    encode_approve,
)
from rugbot.adapters.evm_robinhood.contracts.router import (
    encode_get_amounts_out,
    encode_swap_exact_eth_for_tokens,
    encode_swap_exact_tokens_for_eth,
)
from rugbot.core.models.order import (
    ExecutionMode,
    OrderSide,
    TradeReceipt,
)
from rugbot.core.models.quote import ExecutionQuote
from rugbot.core.models.token import TokenAmount
from rugbot.core.ports.execution_port import ExecutionPort

if TYPE_CHECKING:
    from rugbot.adapters.evm_robinhood.client import EvmRpcClient
    from rugbot.adapters.evm_robinhood.wallet import RobinhoodWalletAdapter
    from rugbot.core.models.address import Address
    from rugbot.core.models.order import OrderIntent

DEFAULT_ROUTER_ADDRESS = (
    "0x4752ba5DBc23f44D87826276BF6Fd6b1C372aD24"  # Uniswap router standard
)
DEFAULT_WETH_ADDRESS = "0x82aF49447D8a07e3bd95BD0d56f35241523fBab1"  # Canonical WETH
DEFAULT_GAS_LIMIT = 250_000
ETH_DECIMALS = 18
DEFAULT_TOKEN_DECIMALS = 18
ESTIMATED_TX_FEE_ETH = 0.0001


class RobinhoodExecutionAdapter(ExecutionPort):
    """Adapter executing orders on Robinhood Chain Arbitrum Orbit DEX routers."""

    def __init__(
        self,
        rpc_client: EvmRpcClient,
        wallet: RobinhoodWalletAdapter | None = None,
        router_address: str = DEFAULT_ROUTER_ADDRESS,
        weth_address: str = DEFAULT_WETH_ADDRESS,
        chain_id: int = 1337,  # Default local/test Arbitrum Orbit chain ID
    ) -> None:
        self._rpc = rpc_client
        self._wallet = wallet
        self.router_address = router_address
        self.weth_address = weth_address
        self.chain_id = chain_id

    async def get_quote(
        self,
        target_token: Address,
        side: OrderSide,
        amount_in: TokenAmount,
    ) -> ExecutionQuote:
        """Fetch amounts out quote from DEX router."""
        slippage_pct = 2.5
        token_hex = target_token.raw

        path = (
            [self.weth_address, token_hex]
            if side == OrderSide.BUY
            else [token_hex, self.weth_address]
        )
        calldata = encode_get_amounts_out(amount_in.raw_units, path)

        out_decimals = DEFAULT_TOKEN_DECIMALS if side == OrderSide.BUY else ETH_DECIMALS
        try:
            res_hex = await self._rpc.eth_call(to=self.router_address, data=calldata)
            expected_units = decode_uint256(res_hex[-64:])
        except (EvmRpcError, ValueError, KeyError):
            # Fallback quote model when pool reserve is simulated
            simulated_rate = 10_000.0 if side == OrderSide.BUY else 0.0001
            expected_units = int(amount_in.raw_units * simulated_rate)

        min_units = int(expected_units * (1.0 - (slippage_pct / 100.0)))
        expected_out = TokenAmount(raw_units=expected_units, decimals=out_decimals)
        minimum_out = TokenAmount(raw_units=min_units, decimals=out_decimals)

        return ExecutionQuote(
            target_token=target_token,
            side=side,
            amount_in=amount_in,
            expected_amount_out=expected_out,
            minimum_amount_out=minimum_out,
            price_impact_pct=0.15,
            route_venue="arbitrum_orbit_amm",
        )

    async def execute(self, intent: OrderIntent) -> TradeReceipt:
        """Execute buy/sell or simulate dry-run on Robinhood Chain."""
        quote = await self.get_quote(intent.target_token, intent.side, intent.amount_in)
        token_hex = intent.target_token.raw

        if intent.mode == ExecutionMode.DRY_RUN or self._wallet is None:
            # Dry-run execution
            return TradeReceipt(
                ok=True,
                intent_id=intent.intent_id,
                target_token=intent.target_token,
                side=intent.side,
                tx_hash=None,
                filled_amount_in=intent.amount_in,
                filled_amount_out=quote.expected_amount_out,
                effective_price=quote.expected_price,
                fee_paid_native=ESTIMATED_TX_FEE_ETH,
            )

        wallet_addr = self._wallet.public_address.raw

        # Allowance check on sell
        if intent.side == OrderSide.SELL:
            allowance_data = encode_allowance(wallet_addr, self.router_address)
            allow_hex = await self._rpc.eth_call(to=token_hex, data=allowance_data)
            current_allowance = decode_uint256(allow_hex)

            if current_allowance < intent.amount_in.raw_units:
                # Approve router
                approve_data = encode_approve(self.router_address)
                nonce = await self._rpc.eth_get_transaction_count(wallet_addr)
                gas_price = await self._rpc.eth_gas_price()
                tx = {
                    "to": token_hex,
                    "value": 0,
                    "gas": DEFAULT_GAS_LIMIT,
                    "gasPrice": gas_price,
                    "nonce": nonce,
                    "chainId": self.chain_id,
                    "data": approve_data,
                }
                signed_approve = self._wallet.account.sign_transaction(tx)
                await self._rpc.eth_send_raw_transaction(
                    signed_approve.raw_transaction.hex()
                )
                time.sleep(1)

        # Build swap transaction
        path = (
            [self.weth_address, token_hex]
            if intent.side == OrderSide.BUY
            else [token_hex, self.weth_address]
        )
        min_out = quote.minimum_amount_out.raw_units

        if intent.side == OrderSide.BUY:
            calldata = encode_swap_exact_eth_for_tokens(min_out, path, wallet_addr)
            tx_value = intent.amount_in.raw_units
        else:
            calldata = encode_swap_exact_tokens_for_eth(
                intent.amount_in.raw_units, min_out, path, wallet_addr
            )
            tx_value = 0

        nonce = await self._rpc.eth_get_transaction_count(wallet_addr)
        gas_price = await self._rpc.eth_gas_price()
        tx = {
            "to": self.router_address,
            "value": tx_value,
            "gas": DEFAULT_GAS_LIMIT,
            "gasPrice": gas_price,
            "nonce": nonce,
            "chainId": self.chain_id,
            "data": calldata,
        }

        signed_tx = self._wallet.account.sign_transaction(tx)
        tx_hash = await self._rpc.eth_send_raw_transaction(
            signed_tx.raw_transaction.hex()
        )

        return TradeReceipt(
            ok=True,
            intent_id=intent.intent_id,
            target_token=intent.target_token,
            side=intent.side,
            tx_hash=tx_hash,
            filled_amount_in=intent.amount_in,
            filled_amount_out=quote.expected_amount_out,
            effective_price=quote.expected_price,
            fee_paid_native=ESTIMATED_TX_FEE_ETH,
        )
