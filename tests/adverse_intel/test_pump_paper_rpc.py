"""Tests for finalized Pump paper account-index provenance."""

import unittest
from types import SimpleNamespace

from solders.pubkey import Pubkey

from rugbot.protocol.pump.create_decoder import CREATE_V2_ACCOUNT_NAMES
from rugbot.protocol.pump.fee_config_account import PUMP_FEE_CONFIG_PDA
from rugbot.runtime.pump_paper_rpc import _required_addresses


class PumpPaperRpcTests(unittest.TestCase):
    """Require account-role positions to resolve through message indices."""

    def test_global_role_uses_role_position_not_message_index(self) -> None:
        account_pubkeys = tuple(str(Pubkey.new_unique()) for _ in range(24))
        account_indices = tuple(range(7, 23))
        global_position = CREATE_V2_ACCOUNT_NAMES.index("global")
        mint_position = CREATE_V2_ACCOUNT_NAMES.index("mint")
        global_message_index = account_indices[global_position]
        mint_message_index = account_indices[mint_position]
        launch = SimpleNamespace(
            as_of_slot=123,
            required_account_names=CREATE_V2_ACCOUNT_NAMES,
            account_indices=account_indices,
            account_pubkeys=account_pubkeys,
            global_account_index=global_message_index,
            mint_account_index=mint_message_index,
            mint_pubkey=account_pubkeys[mint_message_index],
            bonding_curve_pubkey=account_pubkeys[
                account_indices[CREATE_V2_ACCOUNT_NAMES.index("bonding_curve")]
            ],
        )

        result = _required_addresses(launch)

        self.assertEqual(
            result,
            (
                ("global", account_pubkeys[global_message_index]),
                ("fee_config", PUMP_FEE_CONFIG_PDA),
                ("mint", account_pubkeys[mint_message_index]),
                ("bonding_curve", launch.bonding_curve_pubkey),
            ),
        )


if __name__ == "__main__":
    unittest.main()
