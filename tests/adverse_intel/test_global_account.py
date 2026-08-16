"""Regression guards for the strict Pump Global account decoder."""

import base64
import hashlib
import unittest
from dataclasses import FrozenInstanceError, replace
from uuid import uuid4

import base58

from rugbot.domain.decisions import AbstainReason, AbstainResult
from rugbot.domain.observations import RawChainObservation
from rugbot.protocol.pump.bonding_curve_account import PUMP_PROGRAM_ID
from rugbot.protocol.pump.global_account import (
    PINNED_OFFICIAL_PUMP_IDL_COMMIT,
    PINNED_OFFICIAL_PUMP_IDL_SHA256,
    PUMP_GLOBAL_ACCOUNT_SIZE,
    PUMP_GLOBAL_DISCRIMINATOR,
    PUMP_GLOBAL_PDA,
    PumpGlobalAccount,
    decode_pump_global_account,
)

FINALIZED_SLOT = 439_425_049
FINALIZED_ACCOUNT_SHA256 = (
    "9aa57beb2b897cc175a536f170ddb25c0c8bed8ed6620c7087d8a7fb834f7d30"
)
FINALIZED_ACCOUNT_BASE64 = (
    "p+joschscn8B07uMqzQc4FKEV/LDgX0yeEQZY9zVX+1YuiTJmd2sAqpKwvjQ3Vy8l+Mo"
    "nBl8tQYqVPPZVrnOblEV+WVnqlyz5gAQ2EfjzwMAAKwj/AYAAAAAeMX7UdECAACAxqR+"
    "jQMAXwAAAAAAAAAf6nQ58860xO9Lucx77kChpiYXG2hBX+3tQLeolW+E5wHB4eQAAAAA"
    "AAUAAAAAAAAAYIzMHfzpYbQ7d5wZFQWm4tO/RdWk20YYrXbILWF1RTVjg3MADqIssmTT"
    "Sv9koEte+r+7dN3NBImXsZgVR9fREIOEdCkuZ1qUtDbssKmYiUIyioPdxiM4ApYSZ8XN"
    "YRfLjRgaDISfqTem80re0wge+VcAqssMm7PZCaS5FHUnpOutEeak/ClEpPqCUb74FUJuG"
    "/soxrZkZndgfGrZ9WamRteqj7Bg2CkbTE1HXa/3Yslr3A2s6zbAEurRLtOpSEFh4ATIf"
    "OuY+lzkf4A4Bv0seUXSlSSVmuwA3tl4FPOPeEYf6nQ58860xO9Lucx77kChpiYXG2hBX"
    "+3tQLeolW+E5wchXZlAeTaU4RYGbORZuBj9+bugx7QbeD+joSDKQZUyAaKLX9JqtHmmq"
    "cxsv2sLI+thiFo3HgEgrKkTvu89E4p46JMUH7GOnxV02BDheOGeMGBOMXWqLkoy38hgB"
    "yfRBwkBNYRTYlYJT5EoGRJ++k5Ea0MzcheT0Th2+arb89x9C19udQGCIPlCZ3ADI3tNa"
    "0U3WbSlxpC1nDXZuxh6CQy9KjOYep67E2eZq1mSWxPl3Iswgd8AXbQnwUePpG/4w0eg"
    "dOlUPz43otBGInrdy06cd0xEJYxD7fJKqKrh8AIUZlvaTDjNbbdDj1m0CLuew7TKnor"
    "R8fJGU8SZtXlsINv5sy3dnuo/ObNyEVxxhHwYRc+lNsaFB04DDkTQId4++eNcTLeA8I"
    "7i/uhL7ERqV3gl2mjUOfqKXaOwxc/1D2P0VGsBQ55lEMA9ZfrZMeidBL4Ltw1Rlx9RxB"
    "X7NEwH20GfISICI1UWqRcTTGdYjEk4IK4VXulmZVd6wbcY2kfdzyoFDuan4iBou4hkCq"
    "V/kJMIxh/vcRoBY/WnVcBwvIYNH2NnIHzs2lvMbLHq8PFtaEBFZrGNVtJIGssxcDJlbp"
    "BVHHhElkH4SVjcc6dqhdh1b1XALNrKiboZMnkMNoqxV+ktc8VLlrXJMZQeRupL4uDjE"
    "Sd0T8a3TPtFXv6vi9VxeSztRPwfePlKM9CQnF5rX7AhVwrY262N6P2z0g7RzZnrjk6H"
    "cBV+6+tnimVduZs39rEybHZX25DPuKh6vvjHtvLIaYgTAAAAAAAAALnS/wAAAADG+nrz"
    "vtutOj1l82qryXQxsbvkwtL24OR8pgIDRS9dYQ=="
)


class PumpGlobalAccountDecoderTests(unittest.TestCase):
    def test_decodes_finalized_mainnet_global_account_exactly(self) -> None:
        raw = _finalized_account_data()
        result = decode_pump_global_account(_observation(raw))

        self.assertIsInstance(result, PumpGlobalAccount)
        if not isinstance(result, PumpGlobalAccount):
            self.fail(result)
        self.assertEqual(result.as_of_slot, FINALIZED_SLOT)
        self.assertEqual(result.raw_account_data_sha256, FINALIZED_ACCOUNT_SHA256)
        self.assertTrue(result.initialized)
        self.assertEqual(
            result.authority_pubkey,
            "FFWtrEQ4B4PKQoVuHYzZq8FabGkVatYzDpEVHsK5rrhF",
        )
        self.assertEqual(result.initial_virtual_token_reserves, 1_073_000_000_000_000)
        self.assertEqual(result.initial_virtual_sol_reserves, 30_000_000_000)
        self.assertEqual(result.initial_real_token_reserves, 793_100_000_000_000)
        self.assertEqual(result.token_total_supply, 1_000_000_000_000_000)
        self.assertEqual(result.fee_basis_points, 95)
        self.assertEqual(result.creator_fee_basis_points, 5)
        self.assertTrue(result.create_v2_enabled)
        self.assertTrue(result.mayhem_mode_enabled)
        self.assertTrue(result.is_cashback_enabled)
        self.assertEqual(len(result.fee_recipients), 7)
        self.assertEqual(len(result.reserved_fee_recipients), 7)
        self.assertEqual(len(result.buyback_fee_recipients), 8)
        self.assertEqual(result.buyback_basis_points, 5_000)
        self.assertEqual(result.initial_virtual_quote_reserves, 4_292_000_000)
        self.assertEqual(
            result.whitelisted_quote_mints,
            ("EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",),
        )
        with self.assertRaises(FrozenInstanceError):
            result.initialized = False  # type: ignore[misc]

    def test_requires_finalized_canonical_account_evidence(self) -> None:
        observation = _observation(_finalized_account_data())

        for changed in (
            replace(observation, commitment="confirmed"),
            replace(observation, canonical_status="provisional"),
            replace(observation, source_update_kind="transaction"),
        ):
            with self.subTest(changed=changed):
                result = decode_pump_global_account(changed)
                self._assert_abstains(result, AbstainReason.STALE_STATE)

    def test_rejects_wrong_pda_owner_discriminator_and_account_size(self) -> None:
        raw = _finalized_account_data()
        bad_discriminator = bytes([raw[0] ^ 1]) + raw[1:]
        cases = (
            (
                replace(
                    _observation(raw),
                    account_pubkey=b"wrong-global".ljust(32, b"g"),
                ),
                AbstainReason.UNKNOWN_PROTOCOL_STATE,
            ),
            (
                replace(
                    _observation(raw),
                    account_owner_program_id=b"wrong-owner".ljust(32, b"o"),
                ),
                AbstainReason.UNKNOWN_PROTOCOL_STATE,
            ),
            (_observation(bad_discriminator), AbstainReason.DECODER_MISMATCH),
            (_observation(raw[:-1]), AbstainReason.UNSUPPORTED_PROTOCOL_STATE),
            (_observation(raw + b"\x00"), AbstainReason.UNSUPPORTED_PROTOCOL_STATE),
        )

        for observation, reason in cases:
            with self.subTest(reason=reason):
                self._assert_abstains(
                    decode_pump_global_account(observation),
                    reason,
                )

    def test_rejects_noncanonical_bools_and_invalid_fee_basis_points(self) -> None:
        raw = bytearray(_finalized_account_data())
        raw[8] = 2
        self._assert_abstains(
            decode_pump_global_account(_observation(bytes(raw))),
            AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
        )

        raw = bytearray(_finalized_account_data())
        fee_basis_points_offset = 8 + 1 + 32 + 32 + (4 * 8)
        raw[fee_basis_points_offset : fee_basis_points_offset + 8] = (10_001).to_bytes(
            8, "little"
        )
        self._assert_abstains(
            decode_pump_global_account(_observation(bytes(raw))),
            AbstainReason.UNKNOWN_FEE_CONFIG,
        )

    def test_pinned_official_layout_and_live_evidence_are_exact(self) -> None:
        raw = _finalized_account_data()

        self.assertEqual(
            PINNED_OFFICIAL_PUMP_IDL_COMMIT,
            "9c82f61cb711b044a17f770ab8ce9f9bdf78f333",
        )
        self.assertEqual(
            PINNED_OFFICIAL_PUMP_IDL_SHA256,
            "b90bc471327f671449271d5d1d42354d1fae6f5a06502f5834459a3108138e49",
        )
        self.assertEqual(PUMP_GLOBAL_ACCOUNT_SIZE, 1_045)
        self.assertEqual(raw[:8], PUMP_GLOBAL_DISCRIMINATOR)
        self.assertEqual(len(raw), PUMP_GLOBAL_ACCOUNT_SIZE)
        self.assertEqual(hashlib.sha256(raw).hexdigest(), FINALIZED_ACCOUNT_SHA256)

    def _assert_abstains(
        self,
        result: object,
        reason: AbstainReason,
    ) -> None:
        self.assertIsInstance(result, AbstainResult)
        if isinstance(result, AbstainResult):
            self.assertIs(result.reason, reason)
            self.assertEqual(result.as_of_slot, FINALIZED_SLOT)


def _finalized_account_data() -> bytes:
    return base64.b64decode(FINALIZED_ACCOUNT_BASE64, validate=True)


def _observation(data: bytes) -> RawChainObservation:
    return RawChainObservation(
        raw_id=uuid4(),
        source_id="solana-http-rpc-account-info",
        observer_id="global-account-regression",
        boot_id=uuid4(),
        receive_sequence=1,
        slot=FINALIZED_SLOT,
        parent_slot=None,
        blockhash=None,
        signature=None,
        transaction_index=None,
        outer_instruction_index=None,
        inner_instruction_group_index=None,
        inner_instruction_index=None,
        stack_height=None,
        event_ordinal=None,
        commitment="finalized",
        canonical_status="canonical",
        received_wall_ns=1,
        received_monotonic_ns=1,
        program_id=None,
        account_pubkey=bytes(base58.b58decode(PUMP_GLOBAL_PDA)),
        account_owner_program_id=bytes(base58.b58decode(PUMP_PROGRAM_ID)),
        raw_transaction=None,
        raw_transaction_format=None,
        raw_account_data=data,
        account_write_version=None,
        source_update_kind="account",
        raw_source_status=None,
        raw_source_payload=b"finalized-mainnet-global-account",
        decoder_name=None,
        decoder_version=None,
        idl_hash=None,
    )


if __name__ == "__main__":
    unittest.main()
