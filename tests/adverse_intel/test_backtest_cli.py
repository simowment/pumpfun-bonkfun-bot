"""Tests for the runnable offline backtest command."""

import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from rugbot.backtest.cli import main, run_backtest_file
from rugbot.backtest.qualified_run import QualifiedRunResult
from rugbot.decision.operator_qualification import (
    OperatorQualification,
    QualificationStatus,
)
from rugbot.domain.amounts import QuoteBaseUnits, Slot
from rugbot.domain.decisions import AbstainReason, AbstainResult

FIXTURE = Path("fixtures/backtest/demo.json")


class BacktestCliTests(unittest.TestCase):
    """Verify loading, evaluation, output, and fail-closed behavior."""

    def test_demo_fixture_returns_a_leakage_safe_report(self) -> None:
        result = run_backtest_file(FIXTURE)

        self.assertNotIsInstance(result, AbstainResult)
        self.assertEqual(result.source_launch_count, 3)
        self.assertEqual(result.reason_codes, ("leakage_safe_backtest_report_built",))

    def test_cli_prints_machine_readable_abstention(self) -> None:
        output = io.StringIO()

        with redirect_stdout(output):
            exit_code = main(["--input", str(FIXTURE)])

        self.assertEqual(exit_code, 0)
        payload = json.loads(output.getvalue())
        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["report"]["source_launch_count"], 3)

    def test_cli_does_not_treat_unqualified_run_result_as_success(self) -> None:
        qualification = OperatorQualification(
            status=QualificationStatus.ABSTAIN,
            as_of_slot=Slot(100),
            entity_id="operator-a",
            sample_count=3,
            win_count=0,
            win_rate_ppm=0,
            expectancy_quote_base_units=QuoteBaseUnits(0),
            average_peak_pnl_quote_base_units=QuoteBaseUnits(0),
            adverse_launch_count=0,
            adverse_rate_ppm=0,
            repeated_adverse_behavior=False,
            matched_wallet_count=3,
            reason_codes=("expectancy_below_threshold",),
            evidence_ids=("qualification:100",),
            message="operator did not meet qualification thresholds",
        )
        output = io.StringIO()

        with (
            patch(
                "rugbot.backtest.cli.run_backtest_file",
                return_value=QualifiedRunResult(
                    qualification=qualification, backtest=None
                ),
            ),
            redirect_stdout(output),
        ):
            exit_code = main(["--input", str(FIXTURE)])

        self.assertEqual(exit_code, 1)
        payload = json.loads(output.getvalue())
        self.assertEqual(payload["status"], "abstain")
        self.assertEqual(payload["reason"], "operator_not_qualified")
        self.assertEqual(payload["qualification"]["status"], "ABSTAIN")

    def test_malformed_document_abstains(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "malformed.json"
            path.write_text(
                json.dumps({"config": {}, "launches": [], "unexpected": True}),
                encoding="utf-8",
            )
            result = run_backtest_file(path)

        self.assertIsInstance(result, AbstainResult)

    def test_cli_returns_nonzero_for_malformed_document(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "malformed.json"
            path.write_text('{"config": null, "launches": []}', encoding="utf-8")
            output = io.StringIO()

            with redirect_stdout(output):
                exit_code = main(["--input", str(path)])

        self.assertEqual(exit_code, 1)
        self.assertEqual(json.loads(output.getvalue())["status"], "abstain")

    def test_rpc_cli_accepts_documented_transaction_bound(self) -> None:
        output = io.StringIO()
        abstention = AbstainResult(
            reason=AbstainReason.UNKNOWN_PROTOCOL_STATE,
            message="test transport abstention",
            as_of_slot=0,
        )

        with (
            patch(
                "rugbot.backtest.cli.run_rpc_dataset",
                return_value=abstention,
            ) as run_rpc,
            redirect_stdout(output),
        ):
            exit_code = main(
                [
                    "--operator-wallet",
                    "wallet",
                    "--start-slot",
                    "0",
                    "--end-slot",
                    "0",
                    "--max-transactions",
                    "1000",
                    "--endpoint",
                    "https://rpc.example",
                ]
            )

        self.assertEqual(exit_code, 1)
        self.assertEqual(run_rpc.call_args.kwargs["max_transactions"], 1000)


if __name__ == "__main__":
    unittest.main()
