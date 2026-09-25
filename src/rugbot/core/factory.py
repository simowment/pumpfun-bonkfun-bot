"""Multi-chain dependency injection and adapter factory."""

# ruff: noqa: PLR0913

from __future__ import annotations

import os
from typing import TYPE_CHECKING

import base58
from solders.keypair import Keypair

from rugbot.adapters.evm_robinhood.client import EvmRpcClient
from rugbot.adapters.evm_robinhood.execution import RobinhoodExecutionAdapter
from rugbot.adapters.evm_robinhood.market_data import RobinhoodMarketDataAdapter
from rugbot.adapters.evm_robinhood.wallet import RobinhoodWalletAdapter
from rugbot.adapters.simulation.paper_execution import PaperExecutionAdapter
from rugbot.adapters.solana_pumpfun.execution import SolanaPumpExecutionAdapter
from rugbot.adapters.solana_pumpfun.market_data import SolanaPumpMarketDataAdapter
from rugbot.adapters.solana_pumpfun.wallet import SolanaWalletAdapter
from rugbot.integrations.pumpfun_api import PumpFunApiClient
from rugbot.integrations.solana_rpc import SolanaClient

if TYPE_CHECKING:
    from rugbot.core.ports.execution_port import ExecutionPort
    from rugbot.core.ports.market_data_port import MarketDataPort
    from rugbot.core.ports.wallet_port import WalletPort

DEFAULT_EVM_ROUTER = "0x4752ba5DBc23f44D87826276BF6Fd6b1C372aD24"
DEFAULT_EVM_WETH = "0x82aF49447D8a07e3bd95BD0d56f35241523fBab1"


def create_system_container(
    chain: str = "solana",
    *,
    solana_rpc_url: str | None = None,
    solana_private_key: str | None = None,
    evm_rpc_url: str | None = None,
    evm_private_key: str | None = None,
    evm_router_address: str = DEFAULT_EVM_ROUTER,
    evm_weth_address: str = DEFAULT_EVM_WETH,
) -> tuple[ExecutionPort, MarketDataPort, WalletPort]:
    """Dependency injection factory constructing ports for the active blockchain."""
    chain_lower = chain.strip().lower()

    if chain_lower in ("solana", "pumpfun", "pump"):
        rpc_url = solana_rpc_url or os.getenv(
            "SOLANA_RPC_URL", "https://api.mainnet-beta.solana.com"
        )
        sol_client = SolanaClient(rpc_url)
        api_client = PumpFunApiClient()

        # Keypair initialization
        kp_str = solana_private_key or os.getenv("SOLANA_PRIVATE_KEY")
        if kp_str:
            keypair = Keypair.from_bytes(base58.b58decode(kp_str.strip())[:64])
        else:
            keypair = Keypair()

        wallet = SolanaWalletAdapter(keypair=keypair, rpc_client=sol_client)
        market_data = SolanaPumpMarketDataAdapter(
            api_client=api_client, rpc_client=sol_client
        )
        execution = SolanaPumpExecutionAdapter(
            rpc_url=rpc_url,
            private_key=kp_str,
        )
        return execution, market_data, wallet

    if chain_lower in ("robinhood", "robinhood_orbit", "arbitrum_orbit", "evm"):
        rpc_url = evm_rpc_url or os.getenv("ROBINHOOD_RPC_URL", "http://127.0.0.1:8545")
        evm_client = EvmRpcClient(rpc_url)

        priv_key = evm_private_key or os.getenv("EVM_PRIVATE_KEY") or ("0x" + "1" * 64)
        wallet = RobinhoodWalletAdapter(private_key_hex=priv_key, rpc_client=evm_client)
        market_data = RobinhoodMarketDataAdapter(rpc_client=evm_client)
        execution = RobinhoodExecutionAdapter(
            rpc_client=evm_client,
            wallet=wallet,
            router_address=evm_router_address,
            weth_address=evm_weth_address,
        )
        return execution, market_data, wallet

    if chain_lower in ("paper", "simulation"):
        market_data = SolanaPumpMarketDataAdapter()
        execution = PaperExecutionAdapter(market_data=market_data)
        wallet = SolanaWalletAdapter(
            keypair=Keypair(), rpc_client=SolanaClient("http://127.0.0.1:8899")
        )
        return execution, market_data, wallet

    msg = f"Unsupported blockchain target: {chain}"
    raise ValueError(msg)
