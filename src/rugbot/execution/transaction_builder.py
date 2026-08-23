"""Pre-baked Pump.fun swap instruction and transaction assembly."""

# ruff: noqa: ARG004, S105

from __future__ import annotations

from dataclasses import dataclass

from solders.pubkey import Pubkey

from rugbot.execution.ports import ExecutionIntent, ExecutionMode

PUMP_PROGRAM_ID = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
PUMP_FEE_RECIPIENT = "CebN5WGQ4jvEPvsVU4EoHEpgzq1VV7AbicfhtW4xC9iM"
PUMP_DEFAULT_FEE_BPS = 100
SPL_TOKEN_PROGRAM_ID = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
SPL_ATA_PROGRAM_ID = "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL"


@dataclass(frozen=True, slots=True)
class SwapInstructionData:
    """Pre-computed swap instruction accounts and parameters."""

    mint: Pubkey
    bonding_curve: Pubkey
    associated_bonding_curve: Pubkey
    user_wallet: Pubkey
    user_token_account: Pubkey
    quote_lamports: int
    max_slippage_bps: int
    priority_fee_microlamports: int
    jito_tip_lamports: int


def derive_bonding_curve_pda(mint_str: str) -> tuple[Pubkey, Pubkey]:
    """Derive bonding curve and associated bonding curve PDAs for a Pump token mint."""
    pump_program = Pubkey.from_string(PUMP_PROGRAM_ID)
    mint_pubkey = Pubkey.from_string(mint_str)
    bonding_curve, _ = Pubkey.find_program_address(
        [b"bonding-curve", bytes(mint_pubkey)], pump_program
    )
    spl_token_program = Pubkey.from_string(SPL_TOKEN_PROGRAM_ID)
    spl_ata_program = Pubkey.from_string(SPL_ATA_PROGRAM_ID)
    associated_bonding_curve, _ = Pubkey.find_program_address(
        [bytes(bonding_curve), bytes(spl_token_program), bytes(mint_pubkey)],
        spl_ata_program,
    )
    return bonding_curve, associated_bonding_curve


class TransactionBuilder:
    """Fast assembly for Pump.fun swap instructions."""

    @staticmethod
    def build_buy_intent(
        mint: str,
        creator: str,
        quote_lamports: int = 25_000_000,
        max_slippage_bps: int = 500,
        mode: ExecutionMode = ExecutionMode.PAPER,
    ) -> ExecutionIntent:
        """Create an execution intent for a buy order."""
        return ExecutionIntent(
            mode=mode,
            launch_id=mint,
            market_id=mint,
            route_id="pumpfun",
            trade_action="buy",
            quote_size_base_units=quote_lamports,
            limit_price_quote_per_base="0",
            max_slippage_bps=max_slippage_bps,
        )

    @staticmethod
    def derive_curve_accounts(mint_str: str) -> tuple[Pubkey, Pubkey]:
        """Derive bonding curve and associated bonding curve pubkeys."""
        return derive_bonding_curve_pda(mint_str)


__all__ = [
    "PUMP_DEFAULT_FEE_BPS",
    "PUMP_FEE_RECIPIENT",
    "PUMP_PROGRAM_ID",
    "SPL_ATA_PROGRAM_ID",
    "SPL_TOKEN_PROGRAM_ID",
    "SwapInstructionData",
    "TransactionBuilder",
    "derive_bonding_curve_pda",
]
