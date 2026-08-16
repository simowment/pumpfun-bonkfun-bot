"""Regression guards for decoding Pump trade instructions from raw RPC JSON."""

import json
import struct
import unittest
from uuid import UUID

import base58

from rugbot.domain.observations import RawChainObservation
from rugbot.ingest.pump_trade_observation import decode_pump_trade_observation
from rugbot.protocol.pump.trade_decoder import (
    BUY_DISCRIMINATOR,
    PUMP_PROGRAM_ID,
)

SIGNATURE = b"s" * 64


class PumpTradeObservationTests(unittest.TestCase):
    """Ensure raw finalized observations feed the pinned instruction decoder."""

    def test_decodes_outer_buy_with_loaded_addresses(self) -> None:
        result = decode_pump_trade_observation(_observation())

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].instruction_name, "buy")
        self.assertEqual(result[0].base_amount_base_units, 123)
        self.assertEqual(result[0].max_quote_cost_base_units, 999)


def _observation() -> RawChainObservation:
    keys = [f"account-{index}" for index in range(16)]
    keys[7] = "11111111111111111111111111111111"
    keys[8] = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
    keys[11] = PUMP_PROGRAM_ID
    keys[15] = "pfeeUxB6jkeY1Hxd7CsFCAjcbHA9rWtchMGdZ6VojVZ"
    keys.append(PUMP_PROGRAM_ID)
    data = BUY_DISCRIMINATOR + struct.pack("<QQ", 123, 999) + b"\x01"
    signature_text = base58.b58encode(SIGNATURE).decode("ascii")
    payload = {
        "jsonrpc": "2.0",
        "result": {
            "slot": 7,
            "transaction": {
                "signatures": [signature_text],
                "message": {
                    "accountKeys": keys,
                    "instructions": [
                        {
                            "programIdIndex": 16,
                            "accounts": list(range(16)),
                            "data": base58.b58encode(data).decode("ascii"),
                        }
                    ],
                },
            },
            "meta": {
                "err": None,
                "loadedAddresses": {"writable": [], "readonly": []},
                "logMessages": [],
            },
        },
    }
    return RawChainObservation(
        raw_id=UUID("00000000-0000-0000-0000-000000000001"),
        source_id="test",
        observer_id="test",
        boot_id=UUID("00000000-0000-0000-0000-000000000002"),
        receive_sequence=1,
        slot=7,
        parent_slot=None,
        blockhash=None,
        signature=SIGNATURE,
        transaction_index=0,
        outer_instruction_index=0,
        inner_instruction_group_index=None,
        inner_instruction_index=None,
        stack_height=None,
        event_ordinal=0,
        commitment="finalized",
        canonical_status="canonical",
        received_wall_ns=1,
        received_monotonic_ns=1,
        program_id=None,
        account_pubkey=None,
        account_owner_program_id=None,
        raw_transaction=None,
        raw_transaction_format=None,
        raw_account_data=None,
        account_write_version=None,
        source_update_kind="transaction",
        raw_source_status=None,
        raw_source_payload=json.dumps(payload).encode("utf-8"),
        decoder_name=None,
        decoder_version=None,
        idl_hash=None,
    )


if __name__ == "__main__":
    unittest.main()
