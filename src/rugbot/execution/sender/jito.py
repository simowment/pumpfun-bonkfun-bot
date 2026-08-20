"""Jito Block Engine transaction sender and tip account management."""

# ruff: noqa: TC002

from __future__ import annotations

import base64
import random
import time
from typing import ClassVar

import aiohttp
from solders.instruction import Instruction
from solders.pubkey import Pubkey
from solders.system_program import TransferParams, transfer

from rugbot.execution.sender.base import SubmissionResult
from rugbot.utils.logger import get_logger

logger = get_logger(__name__)

MIN_BASE58_PUBKEY_LEN = 32

# Official Jito tip recipient accounts (verified 8 canonical accounts)
JITO_FALLBACK_TIP_ACCOUNTS: tuple[str, ...] = (
    "96gYZGLnJYVFmbjzopPSU6QiEV5fGqZNyN9nmNhvrZU5",
    "HFqU5x63VTqvQss8hp11i4wVV8bD44PvwucfZ2bU7gRe",
    "Cw8CFyM9FkoMi7K7Crf6HNQqf4uEMzpKw6QNghXLvLkY",
    "ADaUMid9yfUytqMBgopwjb2DTLSokTSzL1zt6iGPaS49",
    "DfXygSm4jCyNCybVYYK6DwvWqjKee8pbDmJGcLWNDXjh",
    "ADuUkR4vqLUMWXxW9gh6D6L8pMSawimctcNZ5pGwDcEt",
    "DttWaMuVvTiduZRnguLF7jNxTgiMBZ1hyAumKUiL2KRL",
    "3AVi9Tg9Uo68tJfuvoKvqKNWKkC5wPdSSdeBnizKZ6jT",
)


def create_jito_tip_instruction(
    payer: Pubkey,
    tip_lamports: int,
    tip_account: Pubkey | None = None,
) -> Instruction:
    """Create a SystemProgram transfer instruction for Jito MEV tip."""
    if tip_account is None:
        random_addr = random.choice(JITO_FALLBACK_TIP_ACCOUNTS)  # noqa: S311
        tip_account = Pubkey.from_string(random_addr)

    return transfer(
        TransferParams(
            from_pubkey=payer,
            to_pubkey=tip_account,
            lamports=tip_lamports,
        )
    )


class JitoSender:
    """Sends raw signed transactions directly to Jito Block Engine."""

    DEFAULT_BLOCK_ENGINE_URL: ClassVar[str] = (
        "https://amsterdam.mainnet.block-engine.jito.wtf/api/v1/transactions"
    )

    def __init__(
        self,
        block_engine_url: str = DEFAULT_BLOCK_ENGINE_URL,
        session: aiohttp.ClientSession | None = None,
    ) -> None:
        self.block_engine_url = block_engine_url
        self._session = session
        self._owns_session = session is None
        self._tip_accounts: list[str] = list(JITO_FALLBACK_TIP_ACCOUNTS)

    @property
    def name(self) -> str:
        return "jito"

    @property
    def tip_accounts(self) -> list[str]:
        return self._tip_accounts

    def get_random_tip_account(self) -> Pubkey:
        """Select a random cached Jito tip recipient account."""
        selected = random.choice(self._tip_accounts)  # noqa: S311
        return Pubkey.from_string(selected)

    async def initialize_tip_accounts(self) -> None:
        """Fetch and validate dynamic tip accounts from Jito at startup."""
        session = await self._get_session()
        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "getTipAccounts",
            "params": [],
        }
        try:
            async with session.post(
                self.block_engine_url,
                json=payload,
                timeout=aiohttp.ClientTimeout(total=3.0),
            ) as response:
                if response.status == 200:  # noqa: PLR2004
                    data = await response.json()
                    accounts = data.get("result", [])
                    if isinstance(accounts, list) and len(accounts) >= 1:
                        valid_accounts = []
                        for acc in accounts:
                            acc_str = str(acc)
                            if len(acc_str) >= MIN_BASE58_PUBKEY_LEN:
                                valid_accounts.append(acc_str)
                        if len(valid_accounts) >= 1:
                            self._tip_accounts = valid_accounts
                            logger.info(
                                f"Jito tip accounts refreshed: {len(valid_accounts)} accounts cached"
                            )
                            return
        except Exception as error:  # noqa: BLE001
            logger.warning(
                f"Failed to fetch Jito dynamic tip accounts ({error}); using verified fallback list"
            )

    async def send_transaction(self, raw_tx_bytes: bytes) -> SubmissionResult:
        """Send base64-encoded raw transaction to Jito Block Engine."""
        start_t = time.perf_counter()
        session = await self._get_session()
        b64_tx = base64.b64encode(raw_tx_bytes).decode("ascii")

        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "sendTransaction",
            "params": [
                b64_tx,
                {"encoding": "base64"},
            ],
        }

        try:
            async with session.post(
                self.block_engine_url,
                json=payload,
                timeout=aiohttp.ClientTimeout(total=4.0),
            ) as response:
                ack_ms = (time.perf_counter() - start_t) * 1000.0
                data = await response.json()
                if "result" in data and isinstance(data["result"], str):
                    return SubmissionResult(
                        sender_name=self.name,
                        signature=data["result"],
                        ack_ms=ack_ms,
                        acknowledged=True,
                    )
                err_msg = str(data.get("error", "Unknown Jito submission error"))
                return SubmissionResult(
                    sender_name=self.name,
                    signature="",
                    ack_ms=ack_ms,
                    acknowledged=False,
                    error_message=err_msg,
                )
        except Exception as error:  # noqa: BLE001
            ack_ms = (time.perf_counter() - start_t) * 1000.0
            return SubmissionResult(
                sender_name=self.name,
                signature="",
                ack_ms=ack_ms,
                acknowledged=False,
                error_message=f"{type(error).__name__}: {error}",
            )

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    async def close(self) -> None:
        """Close underlying HTTP session if owned."""
        if self._owns_session and self._session and not self._session.closed:
            await self._session.close()
