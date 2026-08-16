"""Pump fixture harvest smoke tests."""

import argparse
import base64
import hashlib
import io
import json
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import AsyncMock, patch

import base58
from solders.hash import Hash
from solders.instruction import CompiledInstruction
from solders.message import Message
from solders.pubkey import Pubkey
from solders.transaction import VersionedTransaction

from rugbot.ingest.pump_fixture_harvest import (
    PUMP_BUY_DISCRIMINATOR,
    PUMP_CREATE_V2_DISCRIMINATOR,
    PUMP_INSTRUCTION_FIXTURE_TARGETS,
    PUMP_PROGRAM_ID,
    HarvestStatus,
    PumpFixtureHarvestConfig,
    ReadOnlyRpcClient,
    RpcMethodNotAllowedError,
    RpcRateLimitedError,
    async_main,
    build_arg_parser,
    ensure_rpc_method_allowed,
    harvest_one_pump_create_v2_fixture,
    harvest_one_pump_instruction_fixture,
    rpc_endpoint_from_env,
)


class PumpFixtureHarvestTests(unittest.IsolatedAsyncioTestCase):
    """Tests for bounded read-only Pump fixture harvesting."""

    async def test_harvest_writes_one_finalized_create_v2_fixture(self) -> None:
        """A finalized create_v2 candidate writes an immutable raw fixture."""

        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            idl_path = root / "pump_fun_idl.json"
            idl_path.write_text('{"name":"pump"}', encoding="utf-8")
            output_dir = root / "fixtures"
            signature = "sig-create-v2"
            client = _FakeRpcClient(
                {
                    "getHealth": "ok",
                    "getVersion": {"solana-core": "test"},
                    "getSlot": 500,
                    "getSignaturesForAddress": [
                        {"signature": signature, "err": None},
                    ],
                    "getTransaction": [
                        _json_parsed_create_v2_response(slot=490, signature=signature),
                        _base64_transaction_response(slot=490),
                    ],
                }
            )

            result = await harvest_one_pump_create_v2_fixture(
                rpc_client=client,
                config=PumpFixtureHarvestConfig(
                    output_dir=output_dir,
                    idl_path=idl_path,
                    max_signatures=1,
                    max_transactions=1,
                ),
            )

            self.assertEqual(result.status, HarvestStatus.OK)
            self.assertEqual(result.signature, signature)
            self.assertEqual(result.as_of_slot, 490)
            self.assertIsNotNone(result.artifact_path)
            artifact_path = result.artifact_path
            if artifact_path is None:
                self.fail("artifact_path should be present for OK harvest")
            artifact = json.loads(artifact_path.read_text(encoding="utf-8"))

        self.assertEqual(artifact["signature"], signature)
        self.assertEqual(artifact["as_of_slot"], 490)
        self.assertEqual(artifact["finalized_slot_seen"], 500)
        self.assertEqual(
            artifact["pump_idl_sha256"],
            hashlib.sha256(b'{"name":"pump"}').hexdigest(),
        )
        self.assertEqual(artifact["create_v2"]["account_indices"], [0, 1])
        self.assertEqual(artifact["create_v2"]["program_id_index"], 2)
        self.assertEqual(artifact["pump_program_id"], PUMP_PROGRAM_ID)

    async def test_no_candidate_returns_skip_without_artifact(self) -> None:
        """Missing create_v2 instructions return SKIP."""

        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            idl_path = root / "pump_fun_idl.json"
            idl_path.write_text("{}", encoding="utf-8")
            output_dir = root / "fixtures"
            client = _FakeRpcClient(
                {
                    "getHealth": "ok",
                    "getVersion": {},
                    "getSlot": 500,
                    "getSignaturesForAddress": [
                        {"signature": "sig-other", "err": None},
                    ],
                    "getTransaction": [
                        _json_parsed_non_create_response(slot=490),
                    ],
                }
            )

            result = await harvest_one_pump_create_v2_fixture(
                rpc_client=client,
                config=PumpFixtureHarvestConfig(
                    output_dir=output_dir,
                    idl_path=idl_path,
                    max_signatures=1,
                    max_transactions=1,
                ),
            )

            self.assertEqual(result.status, HarvestStatus.SKIP)
            self.assertFalse(output_dir.exists())

    async def test_harvest_writes_one_finalized_buy_fixture(self) -> None:
        """A finalized buy candidate writes a raw instruction fixture."""

        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            idl_path = root / "pump_fun_idl.json"
            idl_path.write_text('{"name":"pump"}', encoding="utf-8")
            output_dir = root / "fixtures"
            signature = "sig-buy"
            client = _FakeRpcClient(
                {
                    "getHealth": "ok",
                    "getVersion": {"solana-core": "test"},
                    "getSlot": 500,
                    "getSignaturesForAddress": [
                        {"signature": signature, "err": None},
                    ],
                    "getTransaction": [
                        _json_parsed_instruction_response(
                            slot=491,
                            signature=signature,
                            data=PUMP_BUY_DISCRIMINATOR + b"\x01\x02",
                        ),
                        _base64_instruction_response(
                            slot=491,
                            data=PUMP_BUY_DISCRIMINATOR + b"\x01\x02",
                        ),
                    ],
                }
            )

            result = await harvest_one_pump_instruction_fixture(
                rpc_client=client,
                config=PumpFixtureHarvestConfig(
                    output_dir=output_dir,
                    idl_path=idl_path,
                    max_signatures=1,
                    max_transactions=1,
                ),
                target=PUMP_INSTRUCTION_FIXTURE_TARGETS["buy"],
            )

            self.assertEqual(result.status, HarvestStatus.OK)
            self.assertEqual(result.signature, signature)
            self.assertEqual(result.as_of_slot, 491)
            artifact_path = result.artifact_path
            if artifact_path is None:
                self.fail("artifact_path should be present for OK harvest")
            artifact = json.loads(artifact_path.read_text(encoding="utf-8"))

        self.assertEqual(artifact["signature"], signature)
        self.assertEqual(artifact["as_of_slot"], 491)
        self.assertEqual(artifact["instruction"]["instruction_name"], "buy")
        self.assertEqual(
            artifact["instruction"]["discriminator_hex"],
            PUMP_BUY_DISCRIMINATOR.hex(),
        )
        self.assertEqual(artifact["instruction"]["account_indices"], [0, 1])
        self.assertEqual(artifact["instruction"]["program_id_index"], 2)

    async def test_instruction_harvest_skip_for_wrong_target(self) -> None:
        """A non-matching discriminator skips without writing an artifact."""

        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            idl_path = root / "pump_fun_idl.json"
            idl_path.write_text("{}", encoding="utf-8")
            output_dir = root / "fixtures"
            client = _FakeRpcClient(
                {
                    "getHealth": "ok",
                    "getVersion": {},
                    "getSlot": 500,
                    "getSignaturesForAddress": [
                        {"signature": "sig-other", "err": None},
                    ],
                    "getTransaction": [
                        _json_parsed_non_create_response(slot=490),
                    ],
                }
            )

            result = await harvest_one_pump_instruction_fixture(
                rpc_client=client,
                config=PumpFixtureHarvestConfig(
                    output_dir=output_dir,
                    idl_path=idl_path,
                    max_signatures=1,
                    max_transactions=1,
                ),
                target=PUMP_INSTRUCTION_FIXTURE_TARGETS["migrate"],
            )

            self.assertEqual(result.status, HarvestStatus.SKIP)
            self.assertFalse(output_dir.exists())

    async def test_rate_limit_returns_skip_without_partial_artifact(self) -> None:
        """Public RPC rate limits skip cleanly."""

        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            idl_path = root / "pump_fun_idl.json"
            idl_path.write_text("{}", encoding="utf-8")
            output_dir = root / "fixtures"
            client = _RateLimitedRpcClient()

            result = await harvest_one_pump_create_v2_fixture(
                rpc_client=client,
                config=PumpFixtureHarvestConfig(
                    output_dir=output_dir,
                    idl_path=idl_path,
                ),
            )

            self.assertEqual(result.status, HarvestStatus.SKIP)
            self.assertFalse(output_dir.exists())

    async def test_private_key_environment_is_ignored(self) -> None:
        """Secret-looking environment variables are not read by the harvester."""

        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            idl_path = root / "pump_fun_idl.json"
            idl_path.write_text("{}", encoding="utf-8")
            client = _FakeRpcClient(
                {
                    "getHealth": "ok",
                    "getVersion": {},
                    "getSlot": 500,
                    "getSignaturesForAddress": [],
                }
            )

            result = await harvest_one_pump_create_v2_fixture(
                rpc_client=client,
                config=PumpFixtureHarvestConfig(
                    output_dir=root / "fixtures",
                    idl_path=idl_path,
                ),
            )

        self.assertEqual(result.status, HarvestStatus.SKIP)

    def test_rpc_method_allowlist_rejects_non_read_methods(self) -> None:
        """The RPC allowlist rejects transaction-submission methods."""

        with self.assertRaises(RpcMethodNotAllowedError):
            ensure_rpc_method_allowed("send" + "Transaction")

    def test_cli_defaults_to_create_v2_target(self) -> None:
        """The CLI remains backward-compatible by default."""

        args = build_arg_parser().parse_args(["--rpc", "https://example.invalid"])

        self.assertEqual(args.target, "create_v2")
        self.assertIsNone(args.output_dir)

    def test_cli_accepts_instruction_targets(self) -> None:
        """The CLI exposes read-only instruction fixture targets."""

        args = build_arg_parser().parse_args(
            [
                "--rpc",
                "https://example.invalid",
                "--target",
                "migrate",
                "--max-signatures",
                "1",
                "--max-transactions",
                "1",
            ]
        )

        self.assertEqual(args.target, "migrate")
        self.assertEqual(args.max_signatures, 1)
        self.assertEqual(args.max_transactions, 1)

    def test_rpc_endpoint_from_env_prefers_smoke_specific_name(self) -> None:
        """Fixture harvest keeps endpoint precedence aligned with API smoke."""

        endpoint = rpc_endpoint_from_env(
            {
                "SOLANA_NODE_RPC_ENDPOINT": " https://primary.example ",
                "SOLANA_RPC_HTTP": "https://fallback.example",
            }
        )

        self.assertEqual(endpoint, "https://primary.example")

    def test_rpc_endpoint_from_env_uses_documented_repo_name(self) -> None:
        """The repository-wide Solana HTTP endpoint is accepted as fallback."""

        endpoint = rpc_endpoint_from_env(
            {
                "SOLANA_NODE_RPC_ENDPOINT": "",
                "SOLANA_RPC_HTTP": " https://rpc.example ",
            }
        )

        self.assertEqual(endpoint, "https://rpc.example")

    def test_rpc_endpoint_from_env_treats_blank_values_as_missing(self) -> None:
        """Blank endpoint environment values do not trigger a harvest."""

        endpoint = rpc_endpoint_from_env(
            {
                "SOLANA_NODE_RPC_ENDPOINT": " ",
                "SOLANA_RPC_HTTP": "\t",
            }
        )

        self.assertIsNone(endpoint)

    def test_arg_parser_default_uses_documented_rpc_http_fallback(self) -> None:
        """Default CLI args pick up SOLANA_RPC_HTTP without requiring --rpc."""

        with patch.dict(
            "os.environ",
            {"SOLANA_RPC_HTTP": "https://rpc.example"},
            clear=True,
        ):
            args = build_arg_parser().parse_args([])

        self.assertEqual(args.rpc, "https://rpc.example")

    def test_arg_parser_default_prefers_smoke_specific_rpc_env(self) -> None:
        """Parser defaults keep the shared endpoint precedence."""

        with patch.dict(
            "os.environ",
            {
                "SOLANA_NODE_RPC_ENDPOINT": "https://primary.example",
                "SOLANA_RPC_HTTP": "https://fallback.example",
            },
            clear=True,
        ):
            args = build_arg_parser().parse_args([])

        self.assertEqual(args.rpc, "https://primary.example")

    def test_arg_parser_explicit_rpc_overrides_endpoint_env(self) -> None:
        """An explicit --rpc value overrides endpoint environment defaults."""

        with patch.dict(
            "os.environ",
            {
                "SOLANA_NODE_RPC_ENDPOINT": "https://primary.example",
                "SOLANA_RPC_HTTP": "https://fallback.example",
            },
            clear=True,
        ):
            args = build_arg_parser().parse_args(["--rpc", "https://override.example"])

        self.assertEqual(args.rpc, "https://override.example")

    async def test_blank_env_defaults_skip_without_harvest(self) -> None:
        """Parsed blank endpoint env values skip before harvest/network use."""

        stdout = io.StringIO()
        harvest_target = AsyncMock()
        with (
            patch.dict(
                "os.environ",
                {
                    "SOLANA_NODE_RPC_ENDPOINT": " ",
                    "SOLANA_RPC_HTTP": "\t",
                },
                clear=True,
            ),
            patch(
                "rugbot.ingest.pump_fixture_harvest._harvest_target",
                harvest_target,
            ),
            redirect_stdout(stdout),
        ):
            args = build_arg_parser().parse_args([])
            exit_code = await async_main(args)

        self.assertEqual(exit_code, 0)
        harvest_target.assert_not_called()
        self.assertIn(
            "pump_fixture_harvest: skip - SOLANA_NODE_RPC_ENDPOINT or "
            "SOLANA_RPC_HTTP not configured",
            stdout.getvalue(),
        )

    async def test_blank_explicit_rpc_argument_skips_without_harvest(self) -> None:
        """A blank --rpc override skips before building an RPC client."""

        stdout = io.StringIO()
        harvest_target = AsyncMock()
        with (
            patch(
                "rugbot.ingest.pump_fixture_harvest._harvest_target",
                harvest_target,
            ),
            redirect_stdout(stdout),
        ):
            exit_code = await async_main(
                argparse.Namespace(
                    rpc=" \t ",
                    target="create_v2",
                    output_dir=None,
                    idl_path=Path("idl/pump_fun_idl.json"),
                    max_signatures=1,
                    max_transactions=1,
                    request_delay_seconds=0.0,
                )
            )

        self.assertEqual(exit_code, 0)
        harvest_target.assert_not_called()
        self.assertIn(
            "pump_fixture_harvest: skip - SOLANA_NODE_RPC_ENDPOINT or "
            "SOLANA_RPC_HTTP not configured",
            stdout.getvalue(),
        )

    async def test_blank_explicit_rpc_overrides_env_and_skips(self) -> None:
        """A blank explicit --rpc does not fall back to endpoint env vars."""

        stdout = io.StringIO()
        harvest_target = AsyncMock()
        with (
            patch.dict(
                "os.environ",
                {
                    "SOLANA_NODE_RPC_ENDPOINT": "https://primary.example",
                    "SOLANA_RPC_HTTP": "https://fallback.example",
                },
                clear=True,
            ),
            patch(
                "rugbot.ingest.pump_fixture_harvest._harvest_target",
                harvest_target,
            ),
            redirect_stdout(stdout),
        ):
            args = build_arg_parser().parse_args(["--rpc", " "])
            exit_code = await async_main(args)

        self.assertEqual(exit_code, 0)
        harvest_target.assert_not_called()
        self.assertIn(
            "pump_fixture_harvest: skip - SOLANA_NODE_RPC_ENDPOINT or "
            "SOLANA_RPC_HTTP not configured",
            stdout.getvalue(),
        )


class _FakeRpcClient(ReadOnlyRpcClient):
    def __init__(self, responses: dict[str, object]) -> None:
        self._responses = responses

    async def post_rpc(
        self,
        method: str,
        params: list[object] | None = None,
    ) -> object:
        del params
        response = self._responses[method]
        if isinstance(response, list) and method == "getTransaction":
            return response.pop(0)
        return response


class _RateLimitedRpcClient(ReadOnlyRpcClient):
    async def post_rpc(
        self,
        method: str,
        params: list[object] | None = None,
    ) -> object:
        del method, params
        raise RpcRateLimitedError


def _json_parsed_create_v2_response(*, slot: int, signature: str) -> dict[str, object]:
    return {
        "slot": slot,
        "meta": {"err": None},
        "transaction": {
            "signatures": [signature],
            "message": {
                "instructions": [
                    {
                        "programId": PUMP_PROGRAM_ID,
                        "accounts": [
                            "11111111111111111111111111111111",
                            "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb",
                        ],
                        "data": base58.b58encode(
                            PUMP_CREATE_V2_DISCRIMINATOR + b"\x01\x02"
                        ).decode("ascii"),
                    }
                ]
            },
        },
    }


def _json_parsed_non_create_response(*, slot: int) -> dict[str, object]:
    return {
        "slot": slot,
        "meta": {"err": None},
        "transaction": {
            "message": {
                "instructions": [
                    {
                        "programId": PUMP_PROGRAM_ID,
                        "accounts": [],
                        "data": base58.b58encode(b"not-create").decode("ascii"),
                    }
                ]
            }
        },
    }


def _json_parsed_instruction_response(
    *,
    slot: int,
    signature: str,
    data: bytes,
) -> dict[str, object]:
    return {
        "slot": slot,
        "meta": {"err": None},
        "transaction": {
            "signatures": [signature],
            "message": {
                "instructions": [
                    {
                        "programId": PUMP_PROGRAM_ID,
                        "accounts": [
                            "11111111111111111111111111111111",
                            "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb",
                        ],
                        "data": base58.b58encode(data).decode("ascii"),
                    }
                ]
            },
        },
    }


def _base64_transaction_response(*, slot: int) -> dict[str, object]:
    program = Pubkey.from_string(PUMP_PROGRAM_ID)
    account_a = Pubkey.from_string("11111111111111111111111111111111")
    account_b = Pubkey.from_string("TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb")
    instruction = CompiledInstruction(
        2,
        PUMP_CREATE_V2_DISCRIMINATOR + b"\x01\x02",
        bytes([0, 1]),
    )
    message = Message.new_with_compiled_instructions(
        0,
        0,
        0,
        [account_a, account_b, program],
        Hash.default(),
        [instruction],
    )
    transaction = VersionedTransaction.populate(message, [])
    return {
        "slot": slot,
        "meta": {"err": None},
        "transaction": [
            base64.b64encode(bytes(transaction)).decode("ascii"),
            "base64",
        ],
    }


def _base64_instruction_response(*, slot: int, data: bytes) -> dict[str, object]:
    return _base64_response_for_instruction(slot=slot, data=data)


def _base64_response_for_instruction(*, slot: int, data: bytes) -> dict[str, object]:
    program = Pubkey.from_string(PUMP_PROGRAM_ID)
    account_a = Pubkey.from_string("11111111111111111111111111111111")
    account_b = Pubkey.from_string("TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb")
    instruction = CompiledInstruction(
        2,
        data,
        bytes([0, 1]),
    )
    message = Message.new_with_compiled_instructions(
        0,
        0,
        0,
        [account_a, account_b, program],
        Hash.default(),
        [instruction],
    )
    transaction = VersionedTransaction.populate(message, [])
    return {
        "slot": slot,
        "meta": {"err": None},
        "transaction": [
            base64.b64encode(bytes(transaction)).decode("ascii"),
            "base64",
        ],
    }


if __name__ == "__main__":
    unittest.main()
