"""End-to-end test for the durable finalized wallet watch command."""

import asyncio
import json
import tempfile
import unittest
from collections.abc import Mapping
from pathlib import Path
from typing import Any, cast
from unittest.mock import patch

import base58
from solders.keypair import Keypair

from rugbot.domain.decisions import AbstainReason, AbstainResult
from rugbot.ingest.rpc_observer import RpcHttpResponse
from rugbot.runtime.cli import (
    WatchCycleResult,
    _execution_port,
    build_arg_parser,
    main,
    run_watch_cycle,
)
from rugbot.runtime.config import (
    ExecutionMode as ConfigExecutionMode,
)
from rugbot.runtime.config import (
    parse_sniper_config,
)


class WatchCliTests(unittest.TestCase):
    """Exercise RPC observation, decoding, matching, and restart state together."""

    def test_watch_cycle_passes_position_evidence_resolver_to_handler(self) -> None:
        config = parse_sniper_config(
            """target:
  kind: wallet
  id: "11111111111111111111111111111111"
execution:
  mode: observe
  quote_size_lamports: 1
"""
        )
        resolver = object()
        with tempfile.TemporaryDirectory() as temporary_directory:
            with patch("rugbot.runtime.cli.WatchSnipeHandler") as handler_type:
                handler = handler_type.return_value
                handler.candidates = []
                handler.receipts = []
                result = asyncio.run(
                    run_watch_cycle(
                        config,
                        endpoint="https://rpc.example",
                        state_dir=Path(temporary_directory),
                        transport=_FakeTransport(
                            {
                                "getSlot": _rpc_response(1),
                                "getSignaturesForAddress": _rpc_response([]),
                            }
                        ),
                        position_evidence_resolver=cast("Any", resolver),
                    )
                )

        self.assertIsInstance(result, WatchCycleResult)
        self.assertIs(
            handler_type.call_args.kwargs["position_evidence_resolver"],
            resolver,
        )

    def test_paper_watch_abstains_without_exact_execution_context(self) -> None:
        config = parse_sniper_config(
            """target:
  kind: wallet
  id: "11111111111111111111111111111111"
execution:
  mode: paper
  quote_size_lamports: 1
"""
        )

        with tempfile.TemporaryDirectory() as temporary_directory:
            result = asyncio.run(
                run_watch_cycle(
                    config,
                    endpoint="https://rpc.example",
                    state_dir=Path(temporary_directory),
                )
            )

        self.assertIsInstance(result, AbstainResult)
        if isinstance(result, AbstainResult):
            self.assertEqual(result.reason, AbstainReason.MISSING_FEATURE)
            self.assertEqual(
                result.message,
                "exact finalized paper execution context is required",
            )

    def test_intelligence_mode_is_available_without_a_watch_config(self) -> None:
        args = build_arg_parser().parse_args(
            ["--intelligence", "--wallet", "target-wallet", "--pretty"]
        )

        self.assertTrue(args.intelligence)
        self.assertEqual(args.wallet, "target-wallet")
        self.assertTrue(args.pretty)

    def test_cli_accepts_live_mode_without_a_second_enable_flag(self) -> None:
        args = build_arg_parser().parse_args(["--mode", "live"])

        self.assertEqual(args.mode, "live")
        self.assertFalse(hasattr(args, "enable_live"))

    def test_live_configuration_reaches_signer_validation(self) -> None:
        config = """target:
  kind: wallet
  id: "11111111111111111111111111111111"
execution:
  mode: live
  quote_size_lamports: 1
"""
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "watch.yaml"
            path.write_text(config, encoding="utf-8")
            with patch.dict(
                "os.environ",
                {"SOLANA_RPC_HTTP": "https://rpc.example"},
                clear=True,
            ):
                exit_code = main(["--config", str(path), "--once"])

        self.assertEqual(exit_code, 1)

    def test_live_execution_requires_key_before_port_creation(self) -> None:
        with patch.dict("os.environ", {}, clear=True):
            with self.assertRaisesRegex(ValueError, "SOLANA_PRIVATE_KEY"):
                _execution_port(ConfigExecutionMode.LIVE, "https://rpc.example")

    def test_live_execution_requires_key_when_signer_is_configured(self) -> None:
        with patch.dict("os.environ", {}, clear=True):
            with self.assertRaisesRegex(ValueError, "SOLANA_PRIVATE_KEY"):
                _execution_port(
                    ConfigExecutionMode.LIVE,
                    "https://rpc.example",
                    expected_signer_pubkey="signer-pubkey",
                )

    def test_live_execution_port_constructs_with_matching_signer(self) -> None:
        keypair = Keypair()
        encoded = base58.b58encode(bytes(keypair)).decode("ascii")
        with patch.dict("os.environ", {"SOLANA_PRIVATE_KEY": encoded}, clear=True):
            port = _execution_port(
                ConfigExecutionMode.LIVE,
                "https://rpc.example",
                expected_signer_pubkey=str(keypair.pubkey()),
            )

        self.assertEqual(port.signer_pubkey, str(keypair.pubkey()))
        asyncio.run(port.close())


class _FakeTransport:
    def __init__(self, responses: Mapping[str, bytes]) -> None:
        self.responses = dict(responses)
        self.calls: list[dict[str, object]] = []

    async def __call__(self, _endpoint: str, body: bytes) -> RpcHttpResponse:
        request = cast("dict[str, object]", json.loads(body))
        self.calls.append(request)
        return RpcHttpResponse(
            status=200,
            body=self.responses[cast("str", request["method"])],
        )


def _rpc_response(result: object) -> bytes:
    return json.dumps(
        {"jsonrpc": "2.0", "id": 1, "result": result},
        separators=(",", ":"),
    ).encode("utf-8")


if __name__ == "__main__":
    unittest.main()
