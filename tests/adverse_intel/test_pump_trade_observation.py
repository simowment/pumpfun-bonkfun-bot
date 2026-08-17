"""Regression guards for decoding Pump trade instructions from raw RPC JSON."""

import json
import struct
import unittest
from dataclasses import replace
from uuid import UUID

import base58

from rugbot.domain.decisions import AbstainResult
from rugbot.domain.observations import RawChainObservation
from rugbot.ingest.pump_trade_observation import (
    BUY_V2_ACCOUNT_NAMES,
    BUY_V2_DISCRIMINATOR,
    SELL_V2_ACCOUNT_NAMES,
    SELL_V2_DISCRIMINATOR,
    _compiled_instruction,
    decode_pump_trade_observation,
)
from rugbot.protocol.pump.trade_decoder import (
    ASSOCIATED_TOKEN_PROGRAM_ID,
    BUY_DISCRIMINATOR,
    PUMP_FEE_PROGRAM_ID,
    PUMP_PROGRAM_ID,
    SYSTEM_PROGRAM_ID,
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

    def test_compiles_canonical_buy_v2_role_proofs_in_idl_order(self) -> None:
        compiled = _compiled_instruction(
            _raw_instruction(
                discriminator=BUY_V2_DISCRIMINATOR,
                account_count=len(BUY_V2_ACCOUNT_NAMES),
            ),
            observation=_observation(),
            account_pubkeys=_v2_account_pubkeys(len(BUY_V2_ACCOUNT_NAMES)),
            outer_index=0,
        )

        self.assertIsNotNone(compiled)
        if isinstance(compiled, AbstainResult) or compiled is None:
            self.fail("buy_v2 instruction did not compile")
        self.assertEqual(
            tuple(proof.name for proof in compiled.account_role_proofs),
            BUY_V2_ACCOUNT_NAMES,
        )
        self.assertEqual(
            tuple(proof.pubkey for proof in compiled.account_role_proofs),
            tuple(
                compiled.account_pubkeys[index] for index in compiled.account_indices
            ),
        )

    def test_compiles_canonical_sell_v2_role_proofs_in_idl_order(self) -> None:
        compiled = _compiled_instruction(
            _raw_instruction(
                discriminator=SELL_V2_DISCRIMINATOR,
                account_count=len(SELL_V2_ACCOUNT_NAMES),
            ),
            observation=_observation(),
            account_pubkeys=_v2_account_pubkeys(len(SELL_V2_ACCOUNT_NAMES)),
            outer_index=0,
        )

        self.assertIsNotNone(compiled)
        if isinstance(compiled, AbstainResult) or compiled is None:
            self.fail("sell_v2 instruction did not compile")
        self.assertEqual(
            tuple(proof.name for proof in compiled.account_role_proofs),
            SELL_V2_ACCOUNT_NAMES,
        )

    def test_v2_compilation_rejects_missing_or_extra_accounts(self) -> None:
        expected = len(BUY_V2_ACCOUNT_NAMES)
        for account_count in (expected - 1, expected + 1):
            with self.subTest(account_count=account_count):
                result = _compiled_instruction(
                    _raw_instruction(
                        discriminator=BUY_V2_DISCRIMINATOR,
                        account_count=account_count,
                    ),
                    observation=_observation(),
                    account_pubkeys=_v2_account_pubkeys(account_count),
                    outer_index=0,
                )
                self.assertIsInstance(result, AbstainResult)

    def test_decodes_finalized_buy_v2_envelope(self) -> None:
        result = decode_pump_trade_observation(
            _v2_observation(
                discriminator=BUY_V2_DISCRIMINATOR,
                account_names=BUY_V2_ACCOUNT_NAMES,
            )
        )

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].instruction_name, "buy_v2")
        self.assertEqual(result[0].required_account_names, BUY_V2_ACCOUNT_NAMES)
        self.assertEqual(result[0].base_amount_base_units, 123)
        self.assertEqual(result[0].max_quote_cost_base_units, 456)


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


def _raw_instruction(*, discriminator: bytes, account_count: int) -> dict[str, object]:
    return {
        "programIdIndex": account_count,
        "accounts": list(range(account_count)),
        "data": base58.b58encode(discriminator + b"\x01" * 16).decode("ascii"),
    }


def _v2_account_pubkeys(account_count: int) -> tuple[str, ...]:
    keys = [f"v2-account-{index}" for index in range(account_count + 1)]
    keys[-1] = PUMP_PROGRAM_ID
    keys[account_count - 1] = PUMP_PROGRAM_ID
    names = (
        BUY_V2_ACCOUNT_NAMES
        if account_count == len(BUY_V2_ACCOUNT_NAMES)
        else (SELL_V2_ACCOUNT_NAMES)
    )
    fixed = {
        "system_program": SYSTEM_PROGRAM_ID,
        "associated_token_program": ASSOCIATED_TOKEN_PROGRAM_ID,
        "fee_program": PUMP_FEE_PROGRAM_ID,
    }
    for index, name in enumerate(names):
        if name in fixed:
            keys[index] = fixed[name]
    return tuple(keys)


def _v2_observation(
    *, discriminator: bytes, account_names: tuple[str, ...]
) -> RawChainObservation:
    observation = _observation()
    payload = json.loads(observation.raw_source_payload)
    account_count = len(account_names)
    payload["result"]["transaction"]["message"] = {
        "accountKeys": list(_v2_account_pubkeys(account_count)),
        "instructions": [
            {
                **_raw_instruction(
                    discriminator=discriminator, account_count=account_count
                ),
                "data": base58.b58encode(
                    discriminator + struct.pack("<QQ", 123, 456)
                ).decode("ascii"),
            }
        ],
    }
    return replace(
        observation,
        raw_source_payload=json.dumps(payload).encode("utf-8"),
    )


if __name__ == "__main__":
    unittest.main()
