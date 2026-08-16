"""Focused tests for the pinned PumpSwap trade decoder."""

import base64
import json
import unittest
from dataclasses import replace
from pathlib import Path

import base58

from rugbot.domain.account_roles import AccountRoleProof
from rugbot.domain.decisions import AbstainReason, AbstainResult
from rugbot.domain.trades import (
    PumpSwapTradeInstructionEvidence,
    TradeSide,
)
from rugbot.protocol.pump.swap_trade_decoder import (
    BUY_ACCOUNT_NAMES,
    BUY_DISCRIMINATOR,
    BUY_EXACT_QUOTE_IN_DISCRIMINATOR,
    PINNED_PUMP_SWAP_IDL_SHA256,
    PUMP_AMM_PROGRAM_ID,
    CompiledPumpSwapInstruction,
    decode_pump_swap_trade_instruction,
)

FIXTURE = Path(
    "fixtures/finalized_transactions/pump_swap_trade/"
    "3enYokNkLEXQwWPTkdRqJeLrkK1jTjADvb6ay2vTiNeBFUXxCqxD9179r5Peq6jHN9hyQsGQGnDxYkvnV6C6k79s.json"
)


class PumpSwapTradeDecoderTests(unittest.TestCase):
    """Tests for real and synthetic pinned PumpSwap instructions."""

    def test_decodes_real_finalized_buy_fixture(self) -> None:
        """A finalized PumpSwap buy keeps exact integer instruction arguments."""

        artifact = json.loads(FIXTURE.read_text(encoding="utf-8"))
        result = decode_pump_swap_trade_instruction(
            _fixture_instruction(artifact),
            idl_hash=PINNED_PUMP_SWAP_IDL_SHA256,
        )

        self.assertIsInstance(result, PumpSwapTradeInstructionEvidence)
        if isinstance(result, AbstainResult):
            self.fail(result.message)
        self.assertEqual(result.as_of_slot, 436909298)
        self.assertEqual(result.instruction_name, "buy")
        self.assertEqual(result.side, TradeSide.BUY)
        self.assertEqual(result.base_amount_base_units, 26757683)
        self.assertEqual(result.max_quote_cost_base_units, 7700000)
        self.assertTrue(result.track_volume)
        self.assertEqual(result.remaining_account_indices, (23, 24, 25))
        self.assertEqual(result.pool_account_index, 0)
        self.assertEqual(result.fee_config_account_index, 21)
        self.assertEqual(result.fee_program_account_index, 22)
        self.assertEqual(result.missing_evidence, ("transaction_slot_account_state",))

    def test_decodes_old_finalized_exact_quote_input_length(self) -> None:
        """The pinned finalized layout accepts the observed 24-byte form."""

        data = BUY_EXACT_QUOTE_IN_DISCRIMINATOR + _u64(2_705_805) + _u64(1)
        result = decode_pump_swap_trade_instruction(
            _synthetic_instruction(data, BUY_ACCOUNT_NAMES),
            idl_hash=PINNED_PUMP_SWAP_IDL_SHA256,
        )

        self.assertIsInstance(result, PumpSwapTradeInstructionEvidence)
        if isinstance(result, AbstainResult):
            self.fail(result.message)
        self.assertEqual(result.instruction_name, "buy_exact_quote_in")
        self.assertEqual(result.quote_amount_base_units, 2_705_805)
        self.assertEqual(result.min_base_output_base_units, 1)
        self.assertIsNone(result.track_volume)

    def test_incomplete_role_proofs_abstain(self) -> None:
        """Account positions are not trusted without the complete role proof set."""

        instruction = _synthetic_instruction(
            BUY_DISCRIMINATOR + _u64(1) + _u64(2) + b"\x00",
            BUY_ACCOUNT_NAMES,
        )
        instruction = replace(
            instruction,
            account_role_proofs=instruction.account_role_proofs[:-1],
        )
        result = decode_pump_swap_trade_instruction(
            instruction,
            idl_hash=PINNED_PUMP_SWAP_IDL_SHA256,
        )

        self.assertIsInstance(result, AbstainResult)
        self.assertEqual(
            result.reason,
            AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
        )

    def test_idl_mismatch_abstains(self) -> None:
        """Unpinned account layouts cannot produce trade evidence."""

        result = decode_pump_swap_trade_instruction(
            _synthetic_instruction(
                BUY_DISCRIMINATOR + _u64(1) + _u64(2) + b"\x00",
                BUY_ACCOUNT_NAMES,
            ),
            idl_hash="wrong",
        )

        self.assertIsInstance(result, AbstainResult)
        self.assertEqual(result.reason, AbstainReason.DECODER_MISMATCH)


def _fixture_instruction(artifact: dict[str, object]) -> CompiledPumpSwapInstruction:
    account_pubkeys = tuple(artifact["account_pubkeys"])
    account_indices = tuple(artifact["account_indices"])
    return CompiledPumpSwapInstruction(
        as_of_slot=artifact["as_of_slot"],
        program_id=artifact["program_id"],
        account_indices=account_indices,
        data=base64.b64decode(artifact["data_base64"]),
        transaction_index=None,
        outer_instruction_index=0,
        program_id_index=artifact["program_id_index"],
        account_pubkeys=account_pubkeys,
        account_role_proofs=tuple(
            AccountRoleProof(name, account_pubkeys[index])
            for index, name in enumerate(BUY_ACCOUNT_NAMES)
        ),
        signature=base58.b58decode(artifact["signature"]),
    )


def _synthetic_instruction(
    data: bytes,
    required_account_names: tuple[str, ...],
) -> CompiledPumpSwapInstruction:
    account_pubkeys = [
        f"account-{index}" for index in range(len(required_account_names))
    ]
    positions = {name: index for index, name in enumerate(required_account_names)}
    account_pubkeys[positions["system_program"]] = "11111111111111111111111111111111"
    account_pubkeys[positions["associated_token_program"]] = (
        "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL"
    )
    account_pubkeys[positions["program"]] = PUMP_AMM_PROGRAM_ID
    account_pubkeys[positions["fee_program"]] = (
        "pfeeUxB6jkeY1Hxd7CsFCAjcbHA9rWtchMGdZ6VojVZ"
    )
    return CompiledPumpSwapInstruction(
        as_of_slot=1,
        program_id=PUMP_AMM_PROGRAM_ID,
        account_indices=tuple(range(len(required_account_names))),
        data=data,
        transaction_index=0,
        outer_instruction_index=0,
        program_id_index=positions["program"],
        account_pubkeys=tuple(account_pubkeys),
        account_role_proofs=tuple(
            AccountRoleProof(name, account_pubkeys[index])
            for index, name in enumerate(required_account_names)
        ),
    )


def _u64(value: int) -> bytes:
    return value.to_bytes(8, byteorder="little", signed=False)


if __name__ == "__main__":
    unittest.main()
