"""Solana wallet adapter implementing WalletPort."""

from __future__ import annotations

from typing import TYPE_CHECKING

from solders.pubkey import Pubkey

from rugbot.core.models.address import Address
from rugbot.core.models.token import TokenAmount
from rugbot.core.ports.wallet_port import WalletPort

if TYPE_CHECKING:
    from solders.keypair import Keypair

    from rugbot.integrations.solana_rpc import SolanaClient

SOLANA_CHAIN_ID = "solana:mainnet"
SOL_DECIMALS = 9


class SolanaWalletAdapter(WalletPort):
    """Adapter managing a Solana keypair and on-chain account balances."""

    def __init__(self, keypair: Keypair, rpc_client: SolanaClient) -> None:
        self._keypair = keypair
        self._rpc_client = rpc_client
        self._address = Address(raw=str(keypair.pubkey()), chain_id=SOLANA_CHAIN_ID)

    @property
    def public_address(self) -> Address:
        """Return the public base58 address of the Solana wallet."""
        return self._address

    @property
    def keypair(self) -> Keypair:
        """Return the underlying Solders Keypair for transaction signing."""
        return self._keypair

    async def get_native_balance(self) -> TokenAmount:
        """Fetch current native SOL balance."""
        res = await self._rpc_client.get_balance(self._keypair.pubkey())
        lamports = res.value if hasattr(res, "value") else int(res)
        return TokenAmount(raw_units=int(lamports), decimals=SOL_DECIMALS)

    async def get_token_balance(self, target_token: Address) -> TokenAmount:
        """Fetch token balance for this wallet."""
        mint = Pubkey.from_string(target_token.raw)
        res = await self._rpc_client.get_token_accounts_by_owner(
            mint, self._keypair.pubkey()
        )
        # Default SPL token decimals for Pump.fun is 6
        if hasattr(res, "value") and res.value:
            amount_raw = int(
                res.value[0].account.data.parsed["info"]["tokenAmount"]["amount"]
            )
            decimals = int(
                res.value[0].account.data.parsed["info"]["tokenAmount"]["decimals"]
            )
            return TokenAmount(raw_units=amount_raw, decimals=decimals)
        return TokenAmount(raw_units=0, decimals=6)
