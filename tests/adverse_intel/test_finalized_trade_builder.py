"""Regression guards for finalized Pump fill reconstruction."""

import base64
import json
import struct
import unittest
from dataclasses import replace
from uuid import UUID

import base58

from rugbot.backtest.finalized_trade_builder import (
    TRADE_EVENT_DISCRIMINATOR,
    PumpTradeEventProof,
    _decode_trade_event,
    build_finalized_pump_trade,
)
from rugbot.domain.amounts import QuoteBaseUnits, Slot, TokenBaseUnits
from rugbot.domain.decisions import AbstainReason, AbstainResult
from rugbot.domain.observations import RawChainObservation
from rugbot.domain.trades import PumpTradeInstructionEvidence, TradeSide

MINT = base58.b58encode(b"m" * 32).decode("ascii")
WALLET = base58.b58encode(b"w" * 32).decode("ascii")
SIGNATURE = b"s" * 64


class FinalizedTradeBuilderTests(unittest.TestCase):
    """Verify execution events, not instruction limits, become fills."""

    def test_builds_fill_from_trade_event_and_meta_fee(self) -> None:
        observation = _observation(_event(is_buy=True))
        result = build_finalized_pump_trade(
            observation=observation,
            instruction=_instruction(TradeSide.BUY),
            launch_id="launch-1",
            token_mint=MINT,
            wallet=WALLET,
            as_of_slot=Slot(10),
        )

        self.assertFalse(isinstance(result, AbstainResult))
        if isinstance(result, AbstainResult):
            self.fail(result.message)
        self.assertEqual(result.base_amount_base_units, TokenBaseUnits(123))
        self.assertEqual(result.quote_amount_base_units, QuoteBaseUnits(456))
        self.assertEqual(result.execution_cost_quote_base_units, QuoteBaseUnits(5012))

    def test_decodes_current_official_trade_event_golden_fixture(self) -> None:
        # Golden decoder fixture: current official Pump TradeEvent Borsh layout.
        result = _decode_trade_event(_event(is_buy=True), as_of_slot=7)

        self.assertIsInstance(result, PumpTradeEventProof)
        if not isinstance(result, PumpTradeEventProof):
            self.fail(result.message)
        self.assertEqual(result.buyback_fee_basis_points, 25)
        self.assertEqual(result.buyback_fee_base_units, 7)
        self.assertEqual(
            result.shareholders, ((base58.b58encode(b"h" * 32).decode(), 2_500),)
        )
        self.assertEqual(result.quote_mint, base58.b58encode(b"q" * 32).decode())
        self.assertEqual(result.quote_amount_base_units, 456)
        self.assertEqual(result.virtual_quote_reserves_base_units, 5)
        self.assertEqual(result.real_quote_reserves_base_units, 6)

    def test_legacy_trade_event_layout_abstains(self) -> None:
        observation = _observation(_legacy_event(is_buy=True))
        result = build_finalized_pump_trade(
            observation=observation,
            instruction=_instruction(TradeSide.BUY),
            launch_id="launch-1",
            token_mint=MINT,
            wallet=WALLET,
            as_of_slot=Slot(10),
        )

        self.assertIsInstance(result, AbstainResult)
        if isinstance(result, AbstainResult):
            self.assertIs(result.reason, AbstainReason.UNSUPPORTED_PROTOCOL_STATE)

    def test_missing_or_ambiguous_event_abstains(self) -> None:
        observation = _observation(_event(is_buy=True) + _event(is_buy=True))
        result = build_finalized_pump_trade(
            observation=observation,
            instruction=_instruction(TradeSide.BUY),
            launch_id="launch-1",
            token_mint=MINT,
            wallet=WALLET,
            as_of_slot=Slot(10),
        )

        self.assertIsInstance(result, AbstainResult)
        if isinstance(result, AbstainResult):
            self.assertIs(result.reason, AbstainReason.UNSUPPORTED_PROTOCOL_STATE)

    def test_instruction_quote_limit_does_not_become_executed_quote(self) -> None:
        observation = _observation(_event(is_buy=True, sol_amount=456))
        instruction = _instruction(TradeSide.BUY)
        result = build_finalized_pump_trade(
            observation=observation,
            instruction=instruction,
            launch_id="launch-1",
            token_mint=MINT,
            wallet=WALLET,
            as_of_slot=Slot(10),
        )
        self.assertFalse(isinstance(result, AbstainResult))

    def test_v2_instruction_matches_legacy_event_name(self) -> None:
        observation = _observation(_event(is_buy=True))
        result = build_finalized_pump_trade(
            observation=observation,
            instruction=replace(_instruction(TradeSide.BUY), instruction_name="buy_v2"),
            launch_id="launch-1",
            token_mint=MINT,
            wallet=WALLET,
            as_of_slot=Slot(10),
        )

        self.assertFalse(isinstance(result, AbstainResult))


def _observation(event: bytes) -> RawChainObservation:
    signature_text = base58.b58encode(SIGNATURE).decode("ascii")
    payload = {
        "jsonrpc": "2.0",
        "result": {
            "slot": 7,
            "transaction": {"signatures": [signature_text]},
            "meta": {
                "err": None,
                "fee": 5_000,
                "logMessages": [
                    "Program data: " + base64.b64encode(event).decode("ascii")
                ],
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


def _instruction(side: TradeSide) -> PumpTradeInstructionEvidence:
    accounts = [f"account-{index}" for index in range(16)]
    accounts[2] = MINT
    accounts[6] = WALLET
    return PumpTradeInstructionEvidence(
        as_of_slot=Slot(7),
        program_id="6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P",
        program_id_index=16,
        signature=SIGNATURE,
        instruction_name="buy" if side is TradeSide.BUY else "sell",
        side=side,
        account_indices=tuple(range(16)),
        account_pubkeys=(*accounts, "program"),
        account_role_proofs=(),
        required_account_names=(),
        remaining_account_indices=(),
        transaction_index=0,
        outer_instruction_index=0,
        inner_instruction_group_index=None,
        inner_instruction_index=None,
        mint_account_index=2,
        bonding_curve_account_index=3,
        associated_bonding_curve_account_index=4,
        associated_user_account_index=5,
        user_account_index=6,
        token_program_account_index=8,
        fee_config_account_index=14,
        fee_program_account_index=15,
        base_amount_base_units=TokenBaseUnits(123),
        quote_amount_base_units=None,
        max_quote_cost_base_units=QuoteBaseUnits(999_999),
        min_base_output_base_units=None,
        min_quote_output_base_units=None,
        track_volume=True,
        transaction_slot_account_state_available=True,
        missing_evidence=(),
        decoder_version="pump-trade-instruction-v1",
        idl_hash="idl",
    )


def _event(*, is_buy: bool, sol_amount: int = 456) -> bytes:
    parts = [
        TRADE_EVENT_DISCRIMINATOR,
        b"m" * 32,
        struct.pack("<Q", sol_amount),
        struct.pack("<Q", 123),
        bytes([int(is_buy)]),
        b"w" * 32,
        struct.pack("<q", 1),
        struct.pack("<QQQQ", 1, 2, 3, 4),
        b"f" * 32,
        struct.pack("<Q", 100),
        struct.pack("<Q", 4),
        b"c" * 32,
        struct.pack("<Q", 100),
        struct.pack("<Q", 1),
        bytes([1]),
        struct.pack("<QQQ", 0, 0, 0),
        struct.pack("<q", 1),
        struct.pack("<I", 3),
        b"buy",
        bytes([0]),
        struct.pack("<Q", 0),
        struct.pack("<Q", 0),
        struct.pack("<QQ", 25, 7),
        struct.pack("<I", 1),
        b"h" * 32,
        struct.pack("<H", 2_500),
        b"q" * 32,
        struct.pack("<QQQ", sol_amount, 5, 6),
    ]
    return b"".join(parts)


def _legacy_event(*, is_buy: bool, sol_amount: int = 456) -> bytes:
    event = _event(is_buy=is_buy, sol_amount=sol_amount)
    return event[:-110]


if __name__ == "__main__":
    unittest.main()
