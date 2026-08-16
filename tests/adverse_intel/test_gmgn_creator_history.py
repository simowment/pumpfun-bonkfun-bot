"""Focused tests for the read-only GMGN creator-history subprocess."""

import asyncio
import os
import unittest
from unittest.mock import patch

from rugbot.runtime.gmgn_creator_history import (
    _read_only_subprocess_environment,
    fetch_gmgn_creator_history,
)


class GmgnCreatorHistoryEnvironmentTests(unittest.IsolatedAsyncioTestCase):
    """Ensure the external history CLI cannot inherit signer material."""

    async def test_subprocess_environment_excludes_signing_secrets(self) -> None:
        captured: dict[str, str] = {}

        async def create_process(*args: object, **kwargs: object) -> object:
            del args
            captured.update(kwargs["env"])
            return _SuccessfulProcess()

        environ = {
            "PATH": os.environ.get("PATH", ""),
            "GMGN_API_KEY": "configured-api-key",
            "SOLANA_RPC_HTTP": "https://rpc.example",
            "HELIUS_API_KEY": "helius-api-key",
            "PRIVATE_KEY": "private-key",
            "SOLANA_PRIVATE_KEY": "solana-private-key",
            "WALLET_PRIVATE_KEY": "wallet-private-key",
            "SECRET_KEY": "secret-key",
            "MNEMONIC": "seed words",
            "SEED_PHRASE": "seed phrase",
            "KEYPAIR_PATH": "wallet.json",
            "RUGBOT_ENABLE_LIVE": "1",
        }
        with (
            patch.dict(os.environ, environ, clear=True),
            patch(
                "rugbot.runtime.gmgn_creator_history.asyncio.create_subprocess_exec",
                new=create_process,
            ),
        ):
            result = await fetch_gmgn_creator_history(_wallet())

        self.assertEqual(result.message, "GMGN creator history returned invalid JSON")
        self.assertEqual(captured["GMGN_API_KEY"], "configured-api-key")
        self.assertEqual(captured["SOLANA_RPC_HTTP"], "https://rpc.example")
        self.assertEqual(captured["HELIUS_API_KEY"], "helius-api-key")
        for secret_name in (
            "PRIVATE_KEY",
            "SOLANA_PRIVATE_KEY",
            "WALLET_PRIVATE_KEY",
            "SECRET_KEY",
            "MNEMONIC",
            "SEED_PHRASE",
            "KEYPAIR_PATH",
            "RUGBOT_ENABLE_LIVE",
        ):
            with self.subTest(secret_name=secret_name):
                self.assertNotIn(secret_name, captured)

    def test_environment_builder_overrides_inherited_api_key(self) -> None:
        with patch.dict(
            os.environ,
            {
                "GMGN_API_KEY": "inherited-key",
                "SOLANA_RPC_HTTP": "https://rpc.example",
                "PRIVATE_KEY": "must-not-pass",
            },
            clear=True,
        ):
            environment = _read_only_subprocess_environment("selected-key")

        self.assertEqual(environment["GMGN_API_KEY"], "selected-key")
        self.assertEqual(environment["SOLANA_RPC_HTTP"], "https://rpc.example")
        self.assertNotIn("PRIVATE_KEY", environment)


class _SuccessfulProcess:
    returncode = 0

    async def communicate(self) -> tuple[bytes, bytes]:
        await asyncio.sleep(0)
        return b"not-json", b""


def _wallet() -> str:
    return "11111111111111111111111111111111"
