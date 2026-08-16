"""Regression guards for the strict Pump FeeConfig account decoder."""

import unittest
from dataclasses import FrozenInstanceError, replace
from uuid import uuid4

import base58

from rugbot.domain.decisions import AbstainReason, AbstainResult
from rugbot.domain.observations import RawChainObservation
from rugbot.protocol.pump.fee_config_account import (
    FEE_CONFIG_DISCRIMINATOR,
    PUMP_FEE_CONFIG_BUMP,
    PUMP_FEE_CONFIG_PDA,
    PUMP_FEE_PROGRAM_ID,
    PumpFeeConfigAccount,
    decode_pump_fee_config_account,
)

SLOT = 439_422_576
ADMIN = "Hru9DKo86LsmUFKz2ZhrqMQ7HMc1rPZBBUoVy7znu5mR"


class PumpFeeConfigAccountDecoderTests(unittest.TestCase):
    def test_decodes_exact_finalized_canonical_borsh_layout(self) -> None:
        result = decode_pump_fee_config_account(
            _observation(
                _fee_config_bytes(
                    flat_fees=(0, 95, 30),
                    tiers=((0, (0, 95, 30)), (1_000_000, (20, 80, 25))),
                )
            )
        )

        self.assertIsInstance(result, PumpFeeConfigAccount)
        if not isinstance(result, PumpFeeConfigAccount):
            self.fail(result)
        self.assertEqual(result.as_of_slot, SLOT)
        self.assertEqual(result.account_pubkey, PUMP_FEE_CONFIG_PDA)
        self.assertEqual(result.owner_program_id, PUMP_FEE_PROGRAM_ID)
        self.assertEqual(result.bump, PUMP_FEE_CONFIG_BUMP)
        self.assertEqual(result.admin_pubkey, ADMIN)
        self.assertEqual(result.flat_fees.lp_fee_bps, 0)
        self.assertEqual(result.flat_fees.protocol_fee_bps, 95)
        self.assertEqual(result.flat_fees.creator_fee_bps, 30)
        self.assertEqual(
            tuple(tier.market_cap_lamports_threshold for tier in result.fee_tiers),
            (0, 1_000_000),
        )
        self.assertEqual(result.fee_tiers[1].fees.lp_fee_bps, 20)
        self.assertEqual(result.stable_fee_tiers, ())
        with self.assertRaises(FrozenInstanceError):
            result.bump = 1  # type: ignore[misc]

    def test_requires_finalized_canonical_account_evidence(self) -> None:
        observation = _observation(_fee_config_bytes())

        for changed in (
            replace(observation, commitment="confirmed"),
            replace(observation, canonical_status="provisional"),
            replace(observation, source_update_kind="transaction"),
        ):
            with self.subTest(changed=changed):
                result = decode_pump_fee_config_account(changed)
                self.assertIsInstance(result, AbstainResult)
                if isinstance(result, AbstainResult):
                    self.assertIs(result.reason, AbstainReason.STALE_STATE)

    def test_rejects_wrong_pda_owner_and_bump(self) -> None:
        observation = _observation(_fee_config_bytes())
        wrong_bump = bytearray(observation.raw_account_data)
        wrong_bump[8] ^= 1

        for changed in (
            replace(observation, account_pubkey=b"wrong-pda".ljust(32, b"p")),
            replace(
                observation,
                account_owner_program_id=b"wrong-owner".ljust(32, b"o"),
            ),
            replace(observation, raw_account_data=bytes(wrong_bump)),
        ):
            with self.subTest(changed=changed):
                result = decode_pump_fee_config_account(changed)
                self.assertIsInstance(result, AbstainResult)
                if isinstance(result, AbstainResult):
                    self.assertIs(
                        result.reason,
                        AbstainReason.UNKNOWN_PROTOCOL_STATE,
                    )

    def test_rejects_discriminator_bounds_and_trailing_bytes(self) -> None:
        valid = _fee_config_bytes(tiers=((0, (0, 95, 30)),))
        wrong_discriminator = b"bad-idl!" + valid[8:]
        truncated_fixed = valid[:60]
        truncated_vector = valid[:-1]

        for data in (
            wrong_discriminator,
            truncated_fixed,
            truncated_vector,
            valid + b"\x01",
            valid + b"stale-allocation",
        ):
            with self.subTest(length=len(data)):
                result = decode_pump_fee_config_account(_observation(data))
                self.assertIsInstance(result, AbstainResult)
                if isinstance(result, AbstainResult):
                    self.assertIs(
                        result.reason,
                        AbstainReason.UNKNOWN_PROTOCOL_STATE,
                    )

    def test_decodes_current_4073_byte_padded_account_shape(self) -> None:
        data = bytearray(4_073)
        pinned_prefix = _fee_config_bytes(
            tiers=((0, (0, 95, 30)),),
            stable_tiers=((0, (0, 95, 30)),),
        )
        data[: len(pinned_prefix)] = pinned_prefix

        result = decode_pump_fee_config_account(_observation(bytes(data)))

        self.assertIsInstance(result, PumpFeeConfigAccount)
        if isinstance(result, PumpFeeConfigAccount):
            self.assertEqual(len(result.fee_tiers), 1)
            self.assertEqual(len(result.stable_fee_tiers), 1)

    def test_rejects_unknown_or_malformed_fee_values(self) -> None:
        for fees in ((10_001, 0, 0), (4_000, 4_000, 4_000)):
            with self.subTest(fees=fees):
                result = decode_pump_fee_config_account(
                    _observation(_fee_config_bytes(flat_fees=fees))
                )
                self.assertIsInstance(result, AbstainResult)
                if isinstance(result, AbstainResult):
                    self.assertIs(result.reason, AbstainReason.UNKNOWN_FEE_CONFIG)

        missing_bytes = replace(
            _observation(_fee_config_bytes()),
            raw_account_data=None,
        )
        result = decode_pump_fee_config_account(missing_bytes)
        self.assertIsInstance(result, AbstainResult)
        if isinstance(result, AbstainResult):
            self.assertIs(result.reason, AbstainReason.MISSING_FEATURE)


def _fee_config_bytes(
    *,
    flat_fees: tuple[int, int, int] = (0, 95, 30),
    tiers: tuple[tuple[int, tuple[int, int, int]], ...] = (),
    stable_tiers: tuple[tuple[int, tuple[int, int, int]], ...] = (),
) -> bytes:
    return b"".join(
        (
            FEE_CONFIG_DISCRIMINATOR,
            PUMP_FEE_CONFIG_BUMP.to_bytes(1, "little"),
            bytes(base58.b58decode(ADMIN)),
            _fees_bytes(flat_fees),
            len(tiers).to_bytes(4, "little"),
            *(
                threshold.to_bytes(16, "little") + _fees_bytes(fees)
                for threshold, fees in tiers
            ),
            len(stable_tiers).to_bytes(4, "little"),
            *(
                threshold.to_bytes(16, "little") + _fees_bytes(fees)
                for threshold, fees in stable_tiers
            ),
        )
    )


def _fees_bytes(fees: tuple[int, int, int]) -> bytes:
    return b"".join(value.to_bytes(8, "little") for value in fees)


def _observation(data: bytes) -> RawChainObservation:
    return RawChainObservation(
        raw_id=uuid4(),
        source_id="fee-config-golden",
        observer_id="fee-config-test",
        boot_id=uuid4(),
        receive_sequence=1,
        slot=SLOT,
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
        account_pubkey=bytes(base58.b58decode(PUMP_FEE_CONFIG_PDA)),
        account_owner_program_id=bytes(base58.b58decode(PUMP_FEE_PROGRAM_ID)),
        raw_transaction=None,
        raw_transaction_format=None,
        raw_account_data=data,
        account_write_version=None,
        source_update_kind="account",
        raw_source_status=None,
        raw_source_payload=b"finalized-account-fixture",
        decoder_name=None,
        decoder_version=None,
        idl_hash=None,
    )


if __name__ == "__main__":
    unittest.main()
