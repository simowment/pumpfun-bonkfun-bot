"""End-to-end test for the durable finalized wallet watch command."""

import asyncio
import base64
import json
import tempfile
import unittest
from collections.abc import Mapping
from pathlib import Path
from typing import Any, cast
from unittest.mock import patch

import base58
from solders.transaction import VersionedTransaction

from rugbot.decision.operator_qualification import (
    OperatorQualification,
    QualificationStatus,
    WalletEntityEvidence,
)
from rugbot.domain.decisions import AbstainReason, AbstainResult
from rugbot.domain.launches import LaunchCreatedV2
from rugbot.execution.ports import ExecutionIntent, ExecutionMode, ExecutionReceipt
from rugbot.ingest.pump_create_fixture_decode import (
    decode_pump_create_v2_fixture_artifact,
)
from rugbot.ingest.rpc_observer import RpcHttpResponse
from rugbot.runtime.cli import (
    WatchCycleResult,
    _execution_port,
    build_arg_parser,
    main,
    run_wallet_intelligence_cycle,
    run_watch_cycle,
)
from rugbot.runtime.config import (
    ExecutionMode as ConfigExecutionMode,
)
from rugbot.runtime.config import (
    parse_sniper_config,
)
from rugbot.runtime.wallet_intelligence import WalletIntelligenceReport

FIXTURE = Path(
    "fixtures/finalized_transactions/pump_create_v2/"
    "4HbY43S9UigSctrfxY5nszgf3ozN1f4kPQYaqaFLZaCDhwa55rauuRmhP85u67U7dBvGFwB5C6stmkH2b1TNxgQh.json"
)


class WatchCliTests(unittest.TestCase):
    """Exercise RPC observation, decoding, matching, and restart state together."""

    def test_finalized_create_is_observed_once_across_restart(self) -> None:
        artifact = json.loads(FIXTURE.read_text(encoding="utf-8"))
        launch = decode_pump_create_v2_fixture_artifact(artifact)
        self.assertIsInstance(launch, LaunchCreatedV2)
        launch = cast("LaunchCreatedV2", launch)
        config = parse_sniper_config(
            f"""target:
  kind: wallet
  id: "{launch.creator_pubkey}"
execution:
  mode: observe
  quote_size_lamports: 1000000
"""
        )
        signature = cast("str", artifact["signature"])
        slot = cast("int", artifact["as_of_slot"])

        with tempfile.TemporaryDirectory() as temporary_directory:
            state_dir = Path(temporary_directory)
            first = asyncio.run(
                run_watch_cycle(
                    config,
                    endpoint="https://rpc.example",
                    state_dir=state_dir,
                    max_transactions=1,
                    transport=_FakeTransport(
                        {
                            "getSlot": _rpc_response(slot),
                            "getSignaturesForAddress": _signature_response(
                                signature, slot
                            ),
                            "getTransaction": _transaction_response(artifact),
                            "getBlock": _rpc_response(
                                {
                                    "transactions": [
                                        {"transaction": {"signatures": [signature]}}
                                    ]
                                }
                            ),
                        }
                    ),
                    qualification=_qualification(launch.creator_pubkey),
                    entity_evidence=_entity_evidence(launch.creator_pubkey),
                )
            )

            self.assertIsInstance(first, WatchCycleResult)
            first = cast("WatchCycleResult", first)
            self.assertIsNone(first.report.abstention)
            self.assertEqual(first.report.handled_count, 1)
            self.assertEqual(len(first.candidates), 1)
            self.assertFalse(first.receipts[0].would_submit_transaction)
            self.assertTrue((state_dir / "observations.jsonl").is_file())
            self.assertTrue((state_dir / "state.sqlite3").is_file())

            second_transport = _FakeTransport(
                {
                    "getSlot": _rpc_response(slot),
                    "getSignaturesForAddress": _signature_response(signature, slot),
                }
            )
            second = asyncio.run(
                run_watch_cycle(
                    config,
                    endpoint="https://rpc.example",
                    state_dir=state_dir,
                    max_transactions=1,
                    transport=second_transport,
                )
            )

            self.assertIsInstance(second, WatchCycleResult)
            second = cast("WatchCycleResult", second)
            self.assertEqual(second.report.observed_count, 0)
            self.assertEqual(second.candidates, ())
            self.assertEqual(second_transport.calls[1]["params"][1]["until"], signature)

    def test_shared_watch_path_accepts_an_injected_non_signing_port(self) -> None:
        artifact = json.loads(FIXTURE.read_text(encoding="utf-8"))
        launch = decode_pump_create_v2_fixture_artifact(artifact)
        self.assertIsInstance(launch, LaunchCreatedV2)
        launch = cast("LaunchCreatedV2", launch)
        config = parse_sniper_config(
            f"""target:
  kind: wallet
  id: "{launch.creator_pubkey}"
execution:
  mode: observe
  quote_size_lamports: 1000000
"""
        )
        signature = cast("str", artifact["signature"])
        slot = cast("int", artifact["as_of_slot"])

        with tempfile.TemporaryDirectory() as temporary_directory:
            result = asyncio.run(
                run_watch_cycle(
                    config,
                    endpoint="https://rpc.example",
                    state_dir=Path(temporary_directory),
                    max_transactions=1,
                    transport=_FakeTransport(
                        {
                            "getSlot": _rpc_response(slot),
                            "getSignaturesForAddress": _signature_response(
                                signature, slot
                            ),
                            "getTransaction": _transaction_response(artifact),
                            "getBlock": _rpc_response(
                                {
                                    "transactions": [
                                        {"transaction": {"signatures": [signature]}}
                                    ]
                                }
                            ),
                        }
                    ),
                    execution_port=_InjectedObservePort(),
                    qualification=_qualification(launch.creator_pubkey),
                    entity_evidence=_entity_evidence(launch.creator_pubkey),
                )
            )

        self.assertIsInstance(result, WatchCycleResult)
        result = cast("WatchCycleResult", result)
        self.assertEqual(result.receipts[0].message, "injected observe port")

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

    def test_cli_accepts_live_mode_override_without_enabling_it(self) -> None:
        args = build_arg_parser().parse_args(["--mode", "live"])

        self.assertEqual(args.mode, "live")
        self.assertFalse(args.enable_live)

    def test_live_configuration_requires_explicit_enable_before_port_creation(
        self,
    ) -> None:
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
            with patch.dict("os.environ", {"SOLANA_RPC_HTTP": "https://rpc.example"}):
                with patch("rugbot.runtime.cli._execution_port") as port:
                    exit_code = main(["--config", str(path), "--once"])

        self.assertEqual(exit_code, 1)
        port.assert_not_called()

    def test_live_execution_dispatch_is_fail_closed(self) -> None:
        with self.assertRaisesRegex(ValueError, "requires the explicit --enable-live"):
            _execution_port(ConfigExecutionMode.LIVE, "https://rpc.example")

    def test_live_execution_requires_signing_key(self) -> None:
        with patch.dict("os.environ", {}, clear=True):
            with self.assertRaisesRegex(ValueError, "SOLANA_PRIVATE_KEY"):
                _execution_port(
                    ConfigExecutionMode.LIVE,
                    "https://rpc.example",
                    allow_live=True,
                )

    def test_live_execution_builds_port_only_with_explicit_gate(self) -> None:
        with (
            patch.dict("os.environ", {"SOLANA_PRIVATE_KEY": "test-key"}),
            patch("rugbot.runtime.cli.LivePumpExecutionPort") as live_port,
        ):
            live_port.return_value.signer_pubkey = "signer-pubkey"
            result = _execution_port(
                ConfigExecutionMode.LIVE,
                "https://rpc.example",
                allow_live=True,
                expected_signer_pubkey="signer-pubkey",
            )

        self.assertIs(result, live_port.return_value)
        live_port.assert_called_once_with("https://rpc.example", "test-key")

    def test_intelligence_cycle_reports_finalized_launch_position(self) -> None:
        artifact = json.loads(FIXTURE.read_text(encoding="utf-8"))
        launch = decode_pump_create_v2_fixture_artifact(artifact)
        self.assertIsInstance(launch, LaunchCreatedV2)
        launch = cast("LaunchCreatedV2", launch)
        result = asyncio.run(
            run_wallet_intelligence_cycle(
                launch.creator_pubkey,
                endpoint="https://rpc.example",
                max_transactions=1,
                transport=_FakeTransport(
                    {
                        "getSlot": _rpc_response(artifact["as_of_slot"]),
                        "getSignaturesForAddress": _signature_response(
                            artifact["signature"], artifact["as_of_slot"]
                        ),
                        "getTransaction": _transaction_response(artifact),
                        "getBlock": _rpc_response(
                            {
                                "transactions": [
                                    {
                                        "transaction": {
                                            "signatures": [artifact["signature"]]
                                        }
                                    }
                                ]
                            }
                        ),
                    }
                ),
            )
        )

        self.assertIsInstance(result, WalletIntelligenceReport)
        result = cast("WalletIntelligenceReport", result)
        self.assertEqual(result.launch_count, 1)
        self.assertEqual(result.early_launch_count, 1)
        self.assertEqual(result.launches[0].transaction_index, 0)


def _qualification(wallet: str) -> OperatorQualification:
    return OperatorQualification(
        status=QualificationStatus.QUALIFIED,
        as_of_slot=0,
        entity_id="operator-a",
        sample_count=3,
        win_count=2,
        win_rate_ppm=666_666,
        expectancy_quote_base_units=1,
        average_peak_pnl_quote_base_units=1,
        adverse_launch_count=2,
        adverse_rate_ppm=666_666,
        repeated_adverse_behavior=True,
        matched_wallet_count=1,
        reason_codes=("operator_qualified",),
        evidence_ids=(f"entity:{wallet}",),
    )


def _entity_evidence(wallet: str) -> tuple[WalletEntityEvidence, ...]:
    return (
        WalletEntityEvidence(
            as_of_slot=0,
            observed_slot=0,
            entity_id="operator-a",
            launch_id="historical-launch",
            wallet=wallet,
            entity_probability_ppm=900_000,
            evidence_ids=(f"entity:{wallet}",),
        ),
    )


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


class _InjectedObservePort:
    async def submit(self, intent: ExecutionIntent) -> ExecutionReceipt:
        return ExecutionReceipt(
            mode=ExecutionMode.OBSERVE,
            intent_id=intent.intent_id,
            as_of_slot=intent.as_of_slot,
            accepted=True,
            would_submit_transaction=False,
            signature=None,
            simulated_output_base_units=None,
            estimated_fee_lamports=0,
            message="injected observe port",
        )


def _rpc_response(result: object) -> bytes:
    return json.dumps(
        {"jsonrpc": "2.0", "id": 1, "result": result},
        separators=(",", ":"),
    ).encode("utf-8")


def _signature_response(signature: str, slot: int) -> bytes:
    return _rpc_response(
        [
            {
                "signature": signature,
                "slot": slot,
                "confirmationStatus": "finalized",
            }
        ]
    )


def _transaction_response(artifact: dict[str, object]) -> bytes:
    base64_response = artifact["base64_transaction_response"]
    transaction = VersionedTransaction.from_bytes(
        base64.b64decode(base64_response["transaction"][0])
    )
    return _rpc_response(
        {
            "slot": artifact["as_of_slot"],
            "meta": {
                "err": None,
                "loadedAddresses": base64_response["meta"]["loadedAddresses"],
            },
            "transaction": {
                "signatures": [artifact["signature"]],
                "message": {
                    "accountKeys": [
                        str(pubkey) for pubkey in transaction.message.account_keys
                    ],
                    "instructions": [
                        {
                            "programIdIndex": instruction.program_id_index,
                            "accounts": list(instruction.accounts),
                            "data": base58.b58encode(instruction.data).decode("ascii"),
                        }
                        for instruction in transaction.message.instructions
                    ],
                },
            },
        }
    )


if __name__ == "__main__":
    unittest.main()
