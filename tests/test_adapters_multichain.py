"""Unit and integration tests for multi-chain adapters and factory."""

from __future__ import annotations

import pytest
from solders.keypair import Keypair

from rugbot.adapters.evm_robinhood.client import EvmRpcClient
from rugbot.adapters.evm_robinhood.contracts.erc20 import (
    decode_uint256,
    encode_allowance,
    encode_approve,
    encode_balance_of,
    encode_decimals,
    encode_uint256_arg,
)
from rugbot.adapters.evm_robinhood.contracts.router import (
    encode_get_amounts_out,
    encode_swap_exact_eth_for_tokens,
    encode_swap_exact_tokens_for_eth,
)
from rugbot.adapters.evm_robinhood.execution import RobinhoodExecutionAdapter
from rugbot.adapters.evm_robinhood.wallet import RobinhoodWalletAdapter
from rugbot.adapters.simulation.paper_execution import PaperExecutionAdapter
from rugbot.adapters.solana_pumpfun.wallet import SolanaWalletAdapter
from rugbot.config.factory import create_system_container
from rugbot.core.models.address import Address
from rugbot.core.models.order import (
    ExecutionMode,
    OrderIntent,
    OrderSide,
)
from rugbot.core.models.quote import ExecutionQuote
from rugbot.core.models.token import TokenAmount
from rugbot.core.ports.execution_port import ExecutionPort
from rugbot.core.ports.market_data_port import MarketDataPort
from rugbot.core.ports.wallet_port import WalletPort
from rugbot.integrations.solana_rpc import SolanaClient


def test_erc20_abi_encoding() -> None:
    wallet = "0x1234567890123456789012345678901234567890"
    spender = "0xabcdefabcdefabcdefabcdefabcdefabcdefabcd"

    # balanceOf
    data = encode_balance_of(wallet)
    assert data.startswith("0x70a08231")
    assert wallet[2:].lower() in data

    # allowance
    allow_data = encode_allowance(wallet, spender)
    assert allow_data.startswith("0xdd62ed3e")

    # approve
    app_data = encode_approve(spender, value=1000)
    assert app_data.startswith("0x095ea7b3")

    # decimals
    dec_data = encode_decimals()
    assert dec_data == "0x313ce567"

    # decode_uint256
    encoded_val = encode_uint256_arg(42000).hex()
    assert decode_uint256(encoded_val) == 42000


def test_router_abi_encoding() -> None:
    weth = "0x82aF49447D8a07e3bd95BD0d56f35241523fBab1"
    target_coin_addr = "0x1234567890123456789012345678901234567890"
    recipient = "0x9999999999999999999999999999999999999999"

    # getAmountsOut
    amounts_data = encode_get_amounts_out(1000, [weth, target_coin_addr])
    assert amounts_data.startswith("0xd06ca61f")

    # swapExactETHForTokens
    swap_buy_data = encode_swap_exact_eth_for_tokens(
        500, [weth, target_coin_addr], recipient, deadline=1700000000
    )
    assert swap_buy_data.startswith("0x7ff36ab5")
    assert recipient[2:].lower() in swap_buy_data

    # swapExactTokensForETH
    swap_sell_data = encode_swap_exact_tokens_for_eth(
        500, 100, [target_coin_addr, weth], recipient, deadline=1700000000
    )
    assert swap_sell_data.startswith("0x18c6080d")


@pytest.mark.anyio
async def test_paper_execution_adapter() -> None:
    adapter = PaperExecutionAdapter(default_price=0.00005)
    mint_address = Address("6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P")

    # Buy quote
    quote_buy = await adapter.get_quote(
        mint_address, OrderSide.BUY, TokenAmount.from_ui(1.0, decimals=9)
    )
    assert quote_buy.side == OrderSide.BUY
    assert quote_buy.expected_amount_out.ui_value == 20000.0

    # Sell quote
    quote_sell = await adapter.get_quote(
        mint_address, OrderSide.SELL, TokenAmount.from_ui(1000.0, decimals=6)
    )
    assert quote_sell.side == OrderSide.SELL
    assert quote_sell.expected_amount_out.ui_value == 0.05

    # Buy execution
    intent = OrderIntent(
        intent_id="paper_1",
        target_token=mint_address,
        side=OrderSide.BUY,
        amount_in=TokenAmount.from_ui(0.5, decimals=9),
        mode=ExecutionMode.PAPER,
    )
    receipt = await adapter.execute(intent)
    assert receipt.ok
    assert receipt.tx_hash is not None
    assert receipt.filled_amount_out.ui_value == 10000.0


def test_robinhood_wallet_adapter() -> None:
    # Deterministic test private key
    test_key = "0x" + "a" * 64
    rpc = EvmRpcClient("http://127.0.0.1:8545")
    wallet = RobinhoodWalletAdapter(private_key_hex=test_key, rpc_client=rpc)

    assert wallet.public_address.is_evm()
    assert str(wallet.public_address).startswith("0x")
    assert wallet.public_address.chain_id == "evm:robinhood_orbit"


@pytest.mark.anyio
async def test_robinhood_execution_adapter_dry_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rpc = EvmRpcClient("http://127.0.0.1:8545")
    adapter = RobinhoodExecutionAdapter(rpc_client=rpc)
    mint_address = Address(
        "0x1234567890123456789012345678901234567890", chain_id="evm:robinhood_orbit"
    )

    async def mock_get_quote(*_args: object, **_kwargs: object) -> ExecutionQuote:
        amt_out = TokenAmount.from_ui(100.0, decimals=18)
        return ExecutionQuote(
            target_token=mint_address,
            side=OrderSide.BUY,
            amount_in=TokenAmount.from_ui(0.1, decimals=18),
            expected_amount_out=amt_out,
            minimum_amount_out=amt_out,
            price_impact_pct=0.1,
            route_venue="robinhood_orbit_router",
        )

    monkeypatch.setattr(adapter, "get_quote", mock_get_quote)

    intent = OrderIntent(
        intent_id="evm_dry_1",
        target_token=mint_address,
        side=OrderSide.BUY,
        amount_in=TokenAmount.from_ui(0.1, decimals=18),
        mode=ExecutionMode.DRY_RUN,
    )
    receipt = await adapter.execute(intent)
    assert receipt.ok
    assert receipt.tx_hash is None
    assert receipt.side == OrderSide.BUY


def test_solana_wallet_adapter() -> None:
    kp = Keypair()
    rpc = SolanaClient("http://127.0.0.1:8899")
    wallet = SolanaWalletAdapter(keypair=kp, rpc_client=rpc)
    assert wallet.public_address.is_solana()
    assert str(wallet.public_address) == str(kp.pubkey())


def test_create_system_container() -> None:
    # Solana
    exec_port, md_port, wal_port = create_system_container("solana")
    assert isinstance(exec_port, ExecutionPort)
    assert isinstance(md_port, MarketDataPort)
    assert isinstance(wal_port, WalletPort)

    # Robinhood Chain EVM
    exec_evm, md_evm, wal_evm = create_system_container("robinhood")
    assert isinstance(exec_evm, ExecutionPort)
    assert isinstance(md_evm, MarketDataPort)
    assert isinstance(wal_evm, WalletPort)

    # Paper Simulation
    exec_sim, _md_sim, _wal_sim = create_system_container("paper")
    assert isinstance(exec_sim, ExecutionPort)

    # Invalid chain
    with pytest.raises(ValueError, match="Unsupported blockchain target"):
        create_system_container("invalid_chain")
