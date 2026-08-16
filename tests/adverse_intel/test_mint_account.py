"""Regression guards for the pinned SPL-2022 mint decoder."""

import unittest
from dataclasses import replace
from uuid import uuid4

import base58

from rugbot.domain.decisions import AbstainReason, AbstainResult
from rugbot.domain.observations import RawChainObservation
from rugbot.protocol.pump.create_decoder import SPL_2022_PROGRAM_ID
from rugbot.protocol.pump.mint_account import (
    MINT_BASE_LAYOUT_SIZE,
    decode_spl_token_2022_mint_metadata,
)

MINT = base58.b58encode(b"mint-account".ljust(32, b"m")).decode()


class MintAccountDecoderTests(unittest.TestCase):
    def test_decodes_initialized_base_layout(self) -> None:
        result = decode_spl_token_2022_mint_metadata(
            _observation(_mint_bytes(decimals=6)),
            mint_pubkey=MINT,
        )

        self.assertEqual(result.decimals, 6)
        self.assertEqual(result.mint_pubkey, MINT)
        self.assertEqual(result.owner_program_id, SPL_2022_PROGRAM_ID)

    def test_decodes_pinned_metadata_extensions(self) -> None:
        result = decode_spl_token_2022_mint_metadata(
            _observation(_mint_bytes(decimals=6) + _metadata_extensions()),
            mint_pubkey=MINT,
        )

        self.assertEqual(result.decimals, 6)

    def test_rejects_unknown_extensions(self) -> None:
        result = decode_spl_token_2022_mint_metadata(
            _observation(_mint_bytes(decimals=6) + _tlv(99, b"unknown")),
            mint_pubkey=MINT,
        )

        self.assertIsInstance(result, AbstainResult)
        if isinstance(result, AbstainResult):
            self.assertIs(result.reason, AbstainReason.UNSUPPORTED_PROTOCOL_STATE)

    def test_rejects_wrong_owner(self) -> None:
        observation = _observation(_mint_bytes(decimals=6))
        result = decode_spl_token_2022_mint_metadata(
            replace(
                observation,
                account_owner_program_id=b"wrong-owner".ljust(32, b"w"),
            ),
            mint_pubkey=MINT,
        )

        self.assertIsInstance(result, AbstainResult)


def _mint_bytes(*, decimals: int) -> bytes:
    data = bytearray(MINT_BASE_LAYOUT_SIZE)
    data[44] = decimals
    data[45] = 1
    return bytes(data)


def _metadata_extensions() -> bytes:
    return b"\x00" * 83 + b"\x01" + _tlv(18, b"\x00" * 64) + _tlv(19, b"metadata")


def _tlv(extension_type: int, value: bytes) -> bytes:
    return (
        extension_type.to_bytes(2, "little") + len(value).to_bytes(2, "little") + value
    )


def _observation(data: bytes) -> RawChainObservation:
    return RawChainObservation(
        raw_id=uuid4(),
        source_id="mint-test",
        observer_id="mint-test",
        boot_id=uuid4(),
        receive_sequence=1,
        slot=700,
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
        account_pubkey=base58.b58decode(MINT),
        account_owner_program_id=base58.b58decode(SPL_2022_PROGRAM_ID),
        raw_transaction=None,
        raw_transaction_format=None,
        raw_account_data=data,
        account_write_version=None,
        source_update_kind="account",
        raw_source_status=None,
        raw_source_payload=b"account",
        decoder_name=None,
        decoder_version=None,
        idl_hash=None,
    )


if __name__ == "__main__":
    unittest.main()
