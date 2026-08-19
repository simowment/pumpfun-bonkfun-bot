"""Tests for route-aware Pump transaction simulation."""

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from solders.hash import Hash
from solders.instruction import Instruction
from solders.pubkey import Pubkey

from rugbot.execution.ports import ExecutionIntent, ExecutionMode
from rugbot.execution.route_simulation import SimulationPumpExecutionPort
from rugbot.execution.sender import RoutingPolicy
from rugbot.protocol.pump.create_decoder import PUMP_PROGRAM_ID
from rugbot.runtime.config import ExecutionMode as SniperExecutionMode
from rugbot.runtime.config import parse_sniper_config


class RouteSimulationTests(unittest.IsolatedAsyncioTestCase):
    """Require simulation to share the live build boundary without senders."""

    async def test_simulation_port_has_no_private_key_and_never_broadcasts(
        self,
    ) -> None:
        port = object.__new__(SimulationPumpExecutionPort)
        port.endpoint = "https://rpc.example"
        port.signer_pubkey = str(Pubkey.new_unique())
        port.fixed_priority_fee_microlamports = 200_000
        port.jito_tip_lamports = 1_000_000
        port.compute_unit_limit = 400_000
        port.loaded_accounts_data_size_limit = 128_000
        port.routing_policy = RoutingPolicy.JITO_ONLY
        port.jito_block_engine_url = "https://jito.example"
        port._payer = Pubkey.from_string(port.signer_pubkey)
        port._initialized = True

        def random_tip_account() -> Pubkey:
            return Pubkey.new_unique()

        port._jito_sender = SimpleNamespace(
            tip_accounts=[str(Pubkey.new_unique())],
            get_random_tip_account=random_tip_account,
            send_transaction=AsyncMock(),
            initialize_tip_accounts=AsyncMock(),
            close=AsyncMock(),
        )
        fake_client = _FakeSimulationClient()
        port._client = fake_client
        intent = ExecutionIntent(
            intent_id="route-simulation-test",
            as_of_slot=123,
            market_id=str(Pubkey.new_unique()),
            side="buy",
            quote_amount_base_units=10_000,
            base_amount_base_units=None,
            max_slippage_bps=500,
            reason_codes=("route_simulation",),
        )

        with (
            patch(
                "rugbot.execution.route_simulation._fetch_trade_accounts",
                new=AsyncMock(return_value=(123, {})),
            ),
            patch(
                "rugbot.execution.route_simulation._build_trade_context",
                return_value=(
                    SimpleNamespace(amount=777, quote_limit=10_000),
                    object(),
                ),
            ),
            patch(
                "rugbot.execution.route_simulation._build_transaction_instructions",
                return_value=(
                    Instruction(Pubkey.from_string(PUMP_PROGRAM_ID), b"", []),
                ),
            ),
            patch(
                "rugbot.execution.route_simulation.validate_pump_v2_instructions",
                side_effect=lambda instructions, policy: instructions,
            ),
        ):
            receipt = await port.submit(intent)

        self.assertEqual(receipt.mode, ExecutionMode.SIMULATION)
        self.assertTrue(receipt.accepted)
        self.assertFalse(receipt.would_submit_transaction)
        self.assertIsNone(receipt.signature)
        self.assertEqual(receipt.simulated_output_base_units, 777)
        self.assertEqual(fake_client.requests[0]["method"], "simulateTransaction")
        port._jito_sender.send_transaction.assert_not_awaited()
        self.assertFalse(hasattr(port, "private_key"))

    def test_configuration_accepts_simulation_without_private_key(self) -> None:
        pubkey = str(Pubkey.new_unique())
        config = parse_sniper_config(
            f"""target:
  kind: wallet
  id: {pubkey}
execution:
  mode: simulation
  quote_size_lamports: 100000
  signer_pubkey: {pubkey}
"""
        )

        self.assertEqual(config.execution.mode, SniperExecutionMode.SIMULATION)

    def test_constructor_rejects_missing_public_identity(self) -> None:
        with self.assertRaisesRegex(ValueError, "signer_pubkey"):
            SimulationPumpExecutionPort(
                endpoint="https://rpc.example",
                signer_pubkey="",
            )


class _FakeSimulationClient:
    def __init__(self) -> None:
        self.requests: list[dict[str, object]] = []

    async def get_cached_blockhash(self) -> Hash:
        return Hash.new_unique()

    async def post_rpc(self, request: dict[str, object]) -> dict[str, object]:
        self.requests.append(request)
        return {
            "jsonrpc": "2.0",
            "id": 1,
            "result": {
                "err": None,
                "unitsConsumed": 100_000,
                "loadedAccountsDataSize": 10_000,
                "logs": ["simulated"],
            },
        }


if __name__ == "__main__":
    unittest.main()
