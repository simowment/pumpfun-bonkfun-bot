"""Focused tests for canonical finalized launch/trade discovery."""

import asyncio
import json
import unittest
from dataclasses import replace
from typing import TYPE_CHECKING, cast
from unittest.mock import AsyncMock, patch
from uuid import UUID

from rugbot.backtest.observation_trade_join import derive_finalized_trade_joins
from rugbot.backtest.rpc_dataset import _extend_with_mint_history
from rugbot.domain.decisions import AbstainReason, AbstainResult
from tests.adverse_intel.test_pump_create_observation import _artifact, _observation

if TYPE_CHECKING:
    from rugbot.backtest.finalized_trade_builder import FinalizedTradeJoin
    from rugbot.domain.launches import LaunchCreatedV2


class ObservationTradeJoinTests(unittest.TestCase):
    """Ensure only decoder-proven launch/trade joins reach the backtest path."""

    def test_derives_typed_join_from_pinned_launch_and_trade(self) -> None:
        observation = _observation(_artifact())

        result = derive_finalized_trade_joins(
            observations=(observation,),
            as_of_slot=observation.slot,
        )

        self.assertFalse(isinstance(result, AbstainResult))
        launches, joins = cast(
            "tuple[tuple[LaunchCreatedV2, ...], tuple[FinalizedTradeJoin, ...]]",
            result,
        )
        self.assertEqual(len(launches), 1)
        self.assertEqual(len(joins), 1)
        self.assertEqual(joins[0].launch_id, launches[0].launch_id)
        self.assertEqual(joins[0].token_mint, launches[0].mint_pubkey)

    def test_trade_without_launch_abstains_with_missing_feature(self) -> None:
        observation = _observation(_artifact())
        payload = json.loads(observation.raw_source_payload)
        instructions = payload["result"]["transaction"]["message"]["instructions"]
        payload["result"]["transaction"]["message"]["instructions"] = (
            instructions[:2] + instructions[3:]
        )

        result = derive_finalized_trade_joins(
            observations=(
                replace(
                    observation,
                    raw_source_payload=json.dumps(payload).encode("utf-8"),
                ),
            ),
            as_of_slot=observation.slot,
        )

        self.assertIsInstance(result, AbstainResult)
        self.assertEqual(result.reason, AbstainReason.MISSING_FEATURE)
        self.assertEqual(
            result.message,
            "trade has no finalized preceding launch for its mint",
        )

    def test_mint_history_merge_rejects_rows_over_transaction_bound(self) -> None:
        observation = _observation(_artifact())
        extra = replace(
            observation,
            raw_id=UUID("00000000-0000-0000-0000-000000000009"),
            signature=b"x" * 64,
        )

        async def run() -> object:
            with patch(
                "rugbot.backtest.rpc_dataset.observe_address",
                new=AsyncMock(return_value=(extra,)),
            ):
                return await _extend_with_mint_history(
                    observations=(observation,),
                    mints=("mint",),
                    endpoint="https://rpc.example",
                    start_slot=observation.slot,
                    end_slot=observation.slot,
                    max_transactions=1,
                    source_id="test",
                    observer_id="observer",
                    transport=None,
                )

        result = asyncio.run(run())
        self.assertIsInstance(result, AbstainResult)
        self.assertEqual(result.reason, AbstainReason.UNKNOWN_PROTOCOL_STATE)
        self.assertEqual(
            result.message,
            "merged RPC observations exceeded the transaction bound",
        )


if __name__ == "__main__":
    unittest.main()
