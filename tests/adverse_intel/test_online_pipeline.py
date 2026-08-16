"""Focused checks for the shared finalized replay/RPC orchestration boundary."""

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch
from uuid import UUID

from rugbot.backtest.cli import main, run_replayed_dataset_file
from rugbot.backtest.dataset import FinalizedBacktestDataset
from rugbot.backtest.online_pipeline import (
    FinalizedBacktestMetadata,
    run_finalized_backtest_pipeline,
    run_production_backtest_pipeline,
)
from rugbot.domain.decisions import AbstainReason, AbstainResult
from rugbot.storage.jsonl_observation_store import JsonlObservationStore
from tests.adverse_intel.test_pump_create_observation import _artifact, _observation


class OnlinePipelineTests(unittest.TestCase):
    """Verify the shared pure boundary without treating tests as strategy proof."""

    def test_replay_jsonl_uses_the_finalized_dataset_path(self) -> None:
        observation = _observation(_artifact())
        with patch("builtins.print") as output:
            with tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "observations.jsonl"
                JsonlObservationStore(path).append(observation)
                result = run_replayed_dataset_file(
                    path,
                    as_of_slot=observation.slot,
                )
                exit_code = main(
                    [
                        "--replay",
                        str(path),
                        "--as-of-slot",
                        str(observation.slot),
                    ]
                )

        self.assertIsInstance(result, FinalizedBacktestDataset)
        self.assertEqual(exit_code, 1)
        output.assert_called_once()

    def test_finalized_transaction_replay_produces_a_dataset(self) -> None:
        artifact = _artifact()
        observation = _observation(artifact)

        result = run_finalized_backtest_pipeline(
            observations=(observation,),
            metadata=FinalizedBacktestMetadata(
                as_of_slot=observation.slot,
                trades=(),
                cases=(),
            ),
        )

        self.assertIsInstance(result, FinalizedBacktestDataset)
        if isinstance(result, FinalizedBacktestDataset):
            self.assertEqual(len(result.launches), 1)
            self.assertEqual(result.observations, (observation,))

    def test_observation_after_cutoff_abstains_before_decoding(self) -> None:
        artifact = _artifact()
        observation = _observation(artifact)
        future = replace(observation, slot=observation.slot + 1)

        result = run_finalized_backtest_pipeline(
            observations=(future,),
            metadata=FinalizedBacktestMetadata(
                as_of_slot=observation.slot,
                trades=(),
                cases=(),
            ),
        )

        self.assertIsInstance(result, AbstainResult)
        if isinstance(result, AbstainResult):
            self.assertIs(result.reason, AbstainReason.STALE_STATE)

    def test_duplicate_canonical_identity_abstains_even_with_new_raw_uuid(self) -> None:
        artifact = _artifact()
        observation = _observation(artifact)
        duplicate = replace(observation, raw_id=UUID(int=999))

        result = run_finalized_backtest_pipeline(
            observations=(observation, duplicate),
            metadata=FinalizedBacktestMetadata(
                as_of_slot=observation.slot,
                trades=(),
                cases=(),
            ),
        )

        self.assertIsInstance(result, AbstainResult)
        if isinstance(result, AbstainResult):
            self.assertIs(result.reason, AbstainReason.UNSUPPORTED_PROTOCOL_STATE)

    def test_orchestrator_has_no_acquisition_or_signing_dependency(self) -> None:
        source = Path("src/rugbot/backtest/online_pipeline.py").read_text(
            encoding="utf-8"
        )

        self.assertNotIn("observe_address", source)
        self.assertNotIn("AsyncClient", source)
        self.assertNotIn("sign_transaction", source)

    def test_incomplete_production_inputs_abstain_before_dataset_build(self) -> None:
        observation = _observation(_artifact())

        result = run_production_backtest_pipeline(
            observations=(observation,),
            launches=(),
            trades=(),
            entity_evidence=(),
            productions=(),
            entry_facts=(),
            as_of_slot=observation.slot,
            entity_id="entity",
            regime_id="regime",
        )

        self.assertIsInstance(result, AbstainResult)
        if isinstance(result, AbstainResult):
            self.assertIs(result.reason, AbstainReason.MISSING_FEATURE)


if __name__ == "__main__":
    unittest.main()
