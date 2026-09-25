"""EVM wallet adapter implementing WalletPort."""

from __future__ import annotations

from typing import TYPE_CHECKING

from eth_account import Account

from rugbot.adapters.evm_robinhood.client import EvmRpcError
from rugbot.adapters.evm_robinhood.contracts.erc20 import (
    decode_uint256,
    encode_balance_of,
    encode_decimals,
)
from rugbot.core.models.address import Address
from rugbot.core.models.token import TokenAmount
from rugbot.core.ports.wallet_port import WalletPort

if TYPE_CHECKING:
    from rugbot.adapters.evm_robinhood.client import EvmRpcClient

ROBINHOOD_CHAIN_ID = "evm:robinhood_orbit"
ETH_DECIMALS = 18
DEFAULT_ERC20_DECIMALS = 18


class RobinhoodWalletAdapter(WalletPort):
    """EVM Wallet managing private keys, nonce tracking, and account balances."""

    def __init__(self, private_key_hex: str, rpc_client: EvmRpcClient) -> None:
        clean_key = private_key_hex.strip()
        if not clean_key.startswith("0x"):
            clean_key = "0x" + clean_key
        self._account = Account.from_key(clean_key)
        self._rpc = rpc_client
        self._address = Address(raw=self._account.address, chain_id=ROBINHOOD_CHAIN_ID)

    @property
    def public_address(self) -> Address:
        """Return the checksummed hex address."""
        return self._address

    @property
    def account(self) -> Account:
        """Return the underlying eth_account instance for signing."""
        return self._account

    async def get_native_balance(self) -> TokenAmount:
        """Fetch current native ETH balance in wei."""
        wei_balance = await self._rpc.eth_get_balance(self._address.raw)
        return TokenAmount(raw_units=wei_balance, decimals=ETH_DECIMALS)

    async def get_token_balance(self, target_token: Address) -> TokenAmount:
        """Fetch ERC-20 token balance for this wallet."""
        calldata = encode_balance_of(self._address.raw)
        res_hex = await self._rpc.eth_call(to=target_token.raw, data=calldata)
        raw_units = decode_uint256(res_hex)

        # Query decimals
        dec_calldata = encode_decimals()
        try:
            dec_hex = await self._rpc.eth_call(to=target_token.raw, data=dec_calldata)
            decimals = decode_uint256(dec_hex) or DEFAULT_ERC20_DECIMALS
        except (EvmRpcError, ValueError, TypeError):
            decimals = DEFAULT_ERC20_DECIMALS

        return TokenAmount(raw_units=raw_units, decimals=decimals)
