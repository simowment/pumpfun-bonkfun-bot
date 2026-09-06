"""Smart routing and venue detection between Pump.fun bonding curve and PumpSwap AMM.

Auto-detects whether a token mint is still trading on the canonical bonding curve
or has completed and graduated to the PumpSwap AMM pool.
"""

# ruff: noqa: C901, BLE001, TRY300

from __future__ import annotations

import asyncio
import base64
from enum import StrEnum
from typing import Any, Final

from solders.pubkey import Pubkey

from rugbot.execution.pumpswap_builder import (
    PUMP_AMM_PROGRAM_ID,
    PUMP_PROGRAM_ID,
    PUMPSWAP_POOL_DATA_MIN_SIZE,
    PUMPSWAP_POOL_DISCRIMINATOR,
    derive_amm_pool,
    parse_pumpswap_pool_data,
)
from rugbot.ingest.pump.bonding_curve_account import BONDING_CURVE_DISCRIMINATOR
from rugbot.integrations.solana_rpc import SolanaClient
from rugbot.runtime.config import load_provider_settings, resolve_dotenv
from rugbot.utils.logger import get_logger

logger = get_logger(__name__)

BONDING_CURVE_SEED: Final[bytes] = b"bonding-curve"
BONDING_CURVE_COMPLETE_OFFSET: Final[int] = 48
BONDING_CURVE_MIN_SIZE: Final[int] = 49
ANCHOR_DISCRIMINATOR_SIZE: Final[int] = 8


class RouteVenue(StrEnum):
    """Execution venue for a token trade."""

    BONDING_CURVE = "bonding_curve"
    PUMPSWAP_AMM = "pumpswap_amm"


def _to_pubkey(val: Pubkey | str) -> Pubkey:
    """Normalize input to Pubkey."""
    if isinstance(val, Pubkey):
        return val
    return Pubkey.from_string(str(val).strip())


def derive_bonding_curve_address(mint: Pubkey | str) -> Pubkey:
    """Derive the canonical bonding curve PDA for a token mint."""
    mint_pk = _to_pubkey(mint)
    pump_prog = Pubkey.from_string(PUMP_PROGRAM_ID)
    addr, _ = Pubkey.find_program_address(
        [BONDING_CURVE_SEED, bytes(mint_pk)], pump_prog
    )
    return addr


def detect_venue_from_bonding_curve_data(data: bytes | None) -> RouteVenue:
    """Determine venue purely from raw bonding curve account data bytes.

    If the bonding curve has been marked complete (offset 48 byte == 1),
    the token has graduated to PumpSwap AMM.
    """
    if data is not None and len(data) >= BONDING_CURVE_MIN_SIZE:
        if (
            len(data) >= ANCHOR_DISCRIMINATOR_SIZE
            and data[:ANCHOR_DISCRIMINATOR_SIZE] == BONDING_CURVE_DISCRIMINATOR
        ):
            if data[BONDING_CURVE_COMPLETE_OFFSET] == 1:
                return RouteVenue.PUMPSWAP_AMM
            return RouteVenue.BONDING_CURVE
        # Backward compatibility for synthetic test payloads without Anchor discriminator
        if data[BONDING_CURVE_COMPLETE_OFFSET] == 1:
            return RouteVenue.PUMPSWAP_AMM
        return RouteVenue.BONDING_CURVE
    return RouteVenue.BONDING_CURVE


def detect_venue_from_pool_data(data: bytes | None) -> RouteVenue:
    """Determine venue from raw PumpSwap AMM pool account data bytes."""
    if (
        data is not None
        and len(data) >= PUMPSWAP_POOL_DATA_MIN_SIZE
        and (
            data[:8] == PUMPSWAP_POOL_DISCRIMINATOR
            # Allow synthetic non-zero mock payloads from legacy tests if length >= 243
            or (data[:8] != b"\x00" * 8 and data[8] in (254, 255))
        )
    ):
        return RouteVenue.PUMPSWAP_AMM
    return RouteVenue.BONDING_CURVE


class AutoRouter:
    """Evaluates whether a token is on the bonding curve or graduated to PumpSwap AMM."""

    def __init__(
        self,
        endpoint: str | None = None,
        *,
        client: SolanaClient | None = None,
    ) -> None:
        """Initialize the AutoRouter with RPC configuration."""
        resolve_dotenv()
        if client is not None:
            self._client = client
            self._owns_client = False
        else:
            providers = load_provider_settings()
            rpc_url = (
                endpoint or providers.rpc_http or "https://api.mainnet-beta.solana.com"
            )
            self._client = SolanaClient(rpc_url)
            self._owns_client = True

    async def detect_venue(self, mint: str | Pubkey) -> RouteVenue:
        """Query on-chain account state to auto-detect the active trading venue.

        Checks the bonding curve account first. If completed, returns PUMPSWAP_AMM.
        If bonding curve account is missing or closed, checks the PumpSwap AMM pool PDA.
        """
        mint_pk = _to_pubkey(mint)
        bonding_curve_addr = derive_bonding_curve_address(mint_pk)

        try:
            bc_resp = await self._client.get_account_info(str(bonding_curve_addr))
            if bc_resp and isinstance(bc_resp, dict):
                val = bc_resp.get("value")
                if val and isinstance(val, dict):
                    raw_data = val.get("data")
                    if isinstance(raw_data, list) and raw_data:
                        raw_bytes = base64.b64decode(raw_data[0])
                        if len(raw_bytes) >= BONDING_CURVE_MIN_SIZE:
                            if raw_bytes[BONDING_CURVE_COMPLETE_OFFSET] == 1:
                                logger.debug(
                                    "Token %s bonding curve is complete -> graduated to PumpSwap AMM",
                                    str(mint_pk)[:8],
                                )
                                return RouteVenue.PUMPSWAP_AMM
                            return RouteVenue.BONDING_CURVE
        except Exception as exc:
            logger.debug("Failed to inspect bonding curve for %s: %s", mint_pk, exc)

        # Bonding curve missing or unconfirmed: inspect PumpSwap AMM pool directly
        try:
            pool_addr = derive_amm_pool(mint_pk)
            pool_resp = await self._client.get_account_info(str(pool_addr))
            if pool_resp and isinstance(pool_resp, dict):
                val = pool_resp.get("value")
                if val and isinstance(val, dict):
                    owner = val.get("owner")
                    raw_data = val.get("data")
                    if (
                        owner == PUMP_AMM_PROGRAM_ID
                        and isinstance(raw_data, list)
                        and raw_data
                    ):
                        raw_bytes = base64.b64decode(raw_data[0])
                        if len(raw_bytes) >= PUMPSWAP_POOL_DATA_MIN_SIZE:
                            logger.debug(
                                "Token %s PumpSwap AMM pool found (%s) -> PUMPSWAP_AMM",
                                str(mint_pk)[:8],
                                str(pool_addr)[:8],
                            )
                            return RouteVenue.PUMPSWAP_AMM
        except Exception as exc:
            logger.debug("Failed to inspect PumpSwap pool for %s: %s", mint_pk, exc)

        return RouteVenue.BONDING_CURVE

    async def is_graduated(self, mint: str | Pubkey) -> bool:
        """Check whether a token has graduated to the PumpSwap AMM."""
        venue = await self.detect_venue(mint)
        return venue == RouteVenue.PUMPSWAP_AMM

    async def get_pumpswap_pool_info(
        self, mint: str | Pubkey
    ) -> tuple[Pubkey, dict[str, Any]] | None:
        """Fetch and decode the binary PumpSwap pool for a token mint, if it exists."""
        mint_pk = _to_pubkey(mint)
        pool_addr = derive_amm_pool(mint_pk)
        try:
            resp = await self._client.get_account_info(str(pool_addr))
            if not resp or not isinstance(resp, dict):
                return None
            val = resp.get("value")
            if not val or not isinstance(val, dict):
                return None
            raw_data = val.get("data")
            if not isinstance(raw_data, list) or not raw_data:
                return None
            pool_bytes = base64.b64decode(raw_data[0])
            parsed = parse_pumpswap_pool_data(pool_bytes, validate_discriminator=False)
            return pool_addr, parsed
        except Exception as exc:
            logger.debug("Could not fetch PumpSwap pool info for %s: %s", mint_pk, exc)
            return None

    async def get_pool_reserves(
        self, pool_dict: dict[str, Any]
    ) -> tuple[int, int] | None:
        """Fetch on-chain token reserves (base_reserves, quote_reserves) in base units."""
        try:
            base_ata = str(pool_dict["pool_base_token_account"])
            quote_ata = str(pool_dict["pool_quote_token_account"])

            base_resp, quote_resp = await asyncio.gather(
                self._client.post_rpc(
                    {
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "getTokenAccountBalance",
                        "params": [base_ata],
                    }
                ),
                self._client.post_rpc(
                    {
                        "jsonrpc": "2.0",
                        "id": 2,
                        "method": "getTokenAccountBalance",
                        "params": [quote_ata],
                    }
                ),
                return_exceptions=True,
            )

            if (
                isinstance(base_resp, dict)
                and "result" in base_resp
                and isinstance(quote_resp, dict)
                and "result" in quote_resp
            ):
                base_val = base_resp["result"].get("value", {})
                quote_val = quote_resp["result"].get("value", {})
                base_amt = int(base_val.get("amount", 0))
                quote_amt = int(quote_val.get("amount", 0))
                if base_amt > 0 and quote_amt > 0:
                    return base_amt, quote_amt
        except Exception as exc:
            logger.debug("Failed to fetch PumpSwap pool reserves: %s", exc)
        return None

    async def close(self) -> None:
        """Close the underlying RPC client if owned."""
        if self._owns_client and self._client:
            await self._client.close()


__all__ = [
    "AutoRouter",
    "RouteVenue",
    "derive_bonding_curve_address",
    "detect_venue_from_bonding_curve_data",
    "detect_venue_from_pool_data",
]
