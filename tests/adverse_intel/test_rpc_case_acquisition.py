"""Focused guards for bounded finalized RPC case acquisition."""

import asyncio
import unittest
from dataclasses import replace
from unittest.mock import AsyncMock, patch
from uuid import UUID

import base58

from rugbot.backtest.rpc_case_acquisition import (
    FinalizedRpcCaseAcquisition,
    acquire_finalized_rpc_case_observations,
)
from rugbot.domain.decisions import AbstainReason, AbstainResult
from rugbot.ingest.pump_create_observation import decode_pump_create_v2_observation
from tests.adverse_intel.test_pump_create_observation import _artifact, _observation


class RpcCaseAcquisitionTests(unittest.TestCase):
    """Verify the acquisition boundary without exercising strategy logic."""

    def test_deduplicates_and_discovers_only_explicitly_attributed_launch_mints(
        self,
    ) -> None:
        observation = _observation(_artifact())
        launch = decode_pump_create_v2_observation(observation)
        self.assertNotIsInstance(launch, AbstainResult)
        self.assertIsNotNone(launch)
        operator_wallet = launch.creator_pubkey  # type: ignore[union-attr]
        duplicate = replace(
            observation,
            raw_id=UUID("00000000-0000-0000-0000-000000000099"),
            observer_id="second-observer",
        )
        observed = AsyncMock(side_effect=[(observation,), (duplicate,)])

        with patch(
            "rugbot.backtest.rpc_case_acquisition.observe_address",
            observed,
        ):
            result = asyncio.run(
                acquire_finalized_rpc_case_observations(
                    operator_wallet=operator_wallet,
                    endpoint="https://rpc.example",
                    as_of_slot=observation.slot,
                    max_transactions_per_address=1,
                    max_launch_mints=1,
                )
            )

        self.assertIsInstance(result, FinalizedRpcCaseAcquisition)
        self.assertEqual(result.as_of_slot, observation.slot)
        self.assertEqual(result.launch_mints, (launch.mint_pubkey,))  # type: ignore[union-attr]
        self.assertEqual(len(result.launches), 1)
        self.assertEqual(len(result.observations), 1)
        self.assertEqual(observed.await_count, 2)

    def test_rebases_rpc_abstention_to_requested_cutoff(self) -> None:
        operator_wallet = base58.b58encode(b"operator".ljust(32, b"o")).decode()
        failure = AbstainResult(
            reason=AbstainReason.MISSING_FEATURE,
            message="transport unavailable",
            as_of_slot=999,
        )

        with patch(
            "rugbot.backtest.rpc_case_acquisition.observe_address",
            new=AsyncMock(return_value=failure),
        ):
            result = asyncio.run(
                acquire_finalized_rpc_case_observations(
                    operator_wallet=operator_wallet,
                    endpoint="https://rpc.example",
                    as_of_slot=500,
                )
            )

        self.assertIsInstance(result, AbstainResult)
        self.assertEqual(result.reason, AbstainReason.MISSING_FEATURE)
        self.assertEqual(result.as_of_slot, 500)

    def test_explicit_mint_launch_is_joined_only_after_wallet_attribution(self) -> None:
        observation = _observation(_artifact())
        launch = decode_pump_create_v2_observation(observation)
        self.assertNotIsInstance(launch, AbstainResult)
        self.assertIsNotNone(launch)
        operator_wallet = launch.creator_pubkey  # type: ignore[union-attr]

        with patch(
            "rugbot.backtest.rpc_case_acquisition.observe_address",
            new=AsyncMock(side_effect=[(), (observation,)]),
        ):
            result = asyncio.run(
                acquire_finalized_rpc_case_observations(
                    operator_wallet=operator_wallet,
                    endpoint="https://rpc.example",
                    as_of_slot=observation.slot,
                    launch_mints=(launch.mint_pubkey,),  # type: ignore[union-attr]
                    max_transactions_per_address=1,
                    max_launch_mints=1,
                )
            )

        self.assertIsInstance(result, FinalizedRpcCaseAcquisition)
        self.assertEqual(len(result.launches), 1)


if __name__ == "__main__":
    unittest.main()
