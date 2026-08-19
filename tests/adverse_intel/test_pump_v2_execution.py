"""Pure Pump V2 construction and firewall integration tests."""

import base64
import unittest

from solders.hash import Hash
from solders.instruction import AccountMeta, Instruction
from solders.keypair import Keypair
from solders.pubkey import Pubkey

from rugbot.execution.firewall import (
    FirewallPolicy,
    TransactionFirewallError,
    validate_pump_v2_instructions,
)
from rugbot.execution.live import _keypair_from_base58
from rugbot.protocol.pump.create_decoder import SPL_2022_PROGRAM_ID
from rugbot.protocol.pump.v2_builder import (
    PumpV2BuildContext,
    build_buy_v2_instructions,
    build_sell_v2_instructions,
)


class PumpV2ExecutionTests(unittest.TestCase):
    """Verify the executable instruction layout before any signing or RPC call."""

    def test_buy_and_sell_use_pinned_v2_layouts(self) -> None:
        user = Keypair().pubkey()
        context = PumpV2BuildContext(
            mint=Pubkey.new_unique(),
            creator=Pubkey.new_unique(),
            user=user,
            base_token_program=Pubkey.from_string(SPL_2022_PROGRAM_ID),
            fee_recipient=Pubkey.new_unique(),
            buyback_fee_recipient=Pubkey.new_unique(),
            amount=123,
            quote_limit=456,
        )

        buy = build_buy_v2_instructions(context)
        sell = build_sell_v2_instructions(context)

        self.assertEqual(len(buy.trade_instruction.accounts), 27)
        self.assertEqual(len(sell.trade_instruction.accounts), 26)
        self.assertEqual(len(buy.trade_instruction.data), 24)
        self.assertEqual(len(sell.trade_instruction.data), 24)
        self.assertTrue(buy.trade_instruction.accounts[13].is_signer)
        self.assertTrue(sell.trade_instruction.accounts[13].is_signer)

    def test_firewall_accepts_builder_output_and_rejects_account_mutation(self) -> None:
        user = Keypair().pubkey()
        context = PumpV2BuildContext(
            mint=Pubkey.new_unique(),
            creator=Pubkey.new_unique(),
            user=user,
            base_token_program=Pubkey.from_string(SPL_2022_PROGRAM_ID),
            fee_recipient=Pubkey.new_unique(),
            buyback_fee_recipient=Pubkey.new_unique(),
            amount=123,
            quote_limit=456,
        )
        built = build_buy_v2_instructions(context)
        policy = FirewallPolicy(
            payer=user,
            mint=context.mint,
            max_tip_lamports=1_000_000,
            allowed_tip_accounts=frozenset(),
            expected_pump_accounts=tuple(
                meta.pubkey for meta in built.trade_instruction.accounts
            ),
        )

        checked = validate_pump_v2_instructions(
            built.instructions,
            policy=policy,
        )
        self.assertEqual(checked, built.instructions)

        mutated_accounts = list(built.trade_instruction.accounts)
        original = mutated_accounts[10]
        mutated_accounts[10] = AccountMeta(
            pubkey=Pubkey.new_unique(),
            is_signer=original.is_signer,
            is_writable=original.is_writable,
        )

        mutated_trade = Instruction(
            built.trade_instruction.program_id,
            built.trade_instruction.data,
            mutated_accounts,
        )
        with self.assertRaises(TransactionFirewallError):
            validate_pump_v2_instructions(
                (*built.instructions[:2], mutated_trade),
                policy=policy,
            )

    def test_explicit_base64_key_prefix_is_decoded_without_prefix_bytes(self) -> None:
        keypair = Keypair()
        encoded = "base64:" + base64.b64encode(bytes(keypair)).decode("ascii")

        decoded = _keypair_from_base58(encoded)

        self.assertEqual(decoded.pubkey(), keypair.pubkey())

    def test_hash_type_is_available_for_simulation_contracts(self) -> None:
        self.assertEqual(len(bytes(Hash.new_unique())), 32)


if __name__ == "__main__":
    unittest.main()
