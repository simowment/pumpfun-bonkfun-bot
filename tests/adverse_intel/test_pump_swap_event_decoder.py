"""Regression guards for finalized Pump AMM trade event decoding."""

import base64
import json
import unittest
from pathlib import Path

import base58

from rugbot.domain.decisions import AbstainReason, AbstainResult
from rugbot.domain.trades import PumpSwapTradeEventEvidence, TradeSide
from rugbot.protocol.pump.swap_event_decoder import decode_pump_swap_trade_event

FIXTURE = Path(
    "fixtures/finalized_transactions/pump_swap_event/"
    "EnQwPsrnPALHzwfiD1acUvV63d4zq4iU5t7DGahspJdzvEQwirkKDPhAwDgnP7ndaKtFv4VmNthzo8g6rRKaEPv.json"
)


class PumpSwapEventDecoderTests(unittest.TestCase):
    """Verify the official BuyEvent byte layout and fail-closed behavior."""

    def test_decodes_finalized_buy_event_fixture(self) -> None:
        artifact = json.loads(FIXTURE.read_text(encoding="utf-8"))
        result = decode_pump_swap_trade_event(
            base64.b64decode(artifact["data_base64"]),
            as_of_slot=artifact["as_of_slot"],
            signature=base58.b58decode(artifact["signature"]),
            event_index=artifact["event_index"],
        )

        self.assertIsInstance(result, PumpSwapTradeEventEvidence)
        if isinstance(result, AbstainResult):
            self.fail(result.message)
        self.assertEqual(result.side, TradeSide.BUY)
        self.assertEqual(result.base_amount_base_units, 587_410)
        self.assertEqual(result.quote_amount_base_units, 199_421)
        self.assertEqual(result.user_quote_amount_base_units, 200_020)
        self.assertEqual(result.lp_fee_basis_points, 20)
        self.assertEqual(result.protocol_fee_basis_points, 5)
        self.assertEqual(result.creator_fee_basis_points, 5)
        self.assertEqual(result.instruction_name, "buy")

    def test_truncated_event_abstains(self) -> None:
        result = decode_pump_swap_trade_event(
            b"x" * 20,
            as_of_slot=1,
            signature=b"s" * 64,
            event_index=0,
        )

        self.assertIsInstance(result, AbstainResult)
        if isinstance(result, AbstainResult):
            self.assertIs(result.reason, AbstainReason.UNSUPPORTED_PROTOCOL_STATE)


if __name__ == "__main__":
    unittest.main()
