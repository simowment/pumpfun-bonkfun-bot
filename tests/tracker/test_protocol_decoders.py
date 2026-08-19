"""Unit tests for the normalized Solana and Pump.fun protocol decoders."""

from __future__ import annotations

import base58

from rugbot.protocol.pump.instructions import (
    CREATE_INSTRUCTION_DISCRIMINATOR,
    PUMP_PROGRAM_ID,
    parse_pumpfun_launch,
)
from rugbot.protocol.solana.transfers import (
    SYSTEM_PROGRAM_ID,
    parse_sol_transfers,
)


def test_parse_json_sol_transfer() -> None:
    """Test extracting SolTransfer from JSON-parsed outer and inner instructions."""
    tx_data = {
        "slot": 439874630,
        "blockTime": 1700000000,
        "transaction": {
            "signatures": ["sig_test_1"],
            "message": {
                "accountKeys": ["Sender111", "Receiver222", SYSTEM_PROGRAM_ID],
                "instructions": [
                    {
                        "program": "system",
                        "programId": SYSTEM_PROGRAM_ID,
                        "parsed": {
                            "type": "transfer",
                            "info": {
                                "source": "Sender111",
                                "destination": "Receiver222",
                                "lamports": 3_200_000_000,
                            },
                        },
                    }
                ],
            },
        },
        "meta": {
            "err": None,
            "innerInstructions": [
                {
                    "index": 0,
                    "instructions": [
                        {
                            "program": "system",
                            "programId": SYSTEM_PROGRAM_ID,
                            "parsed": {
                                "type": "transfer",
                                "info": {
                                    "source": "Receiver222",
                                    "destination": "Hop333",
                                    "lamports": 3_180_000_000,
                                },
                            },
                        }
                    ],
                }
            ],
        },
    }

    transfers = parse_sol_transfers(tx_data)
    assert len(transfers) == 2
    assert transfers[0].sender == "Sender111"
    assert transfers[0].recipient == "Receiver222"
    assert transfers[0].lamports == 3_200_000_000
    assert transfers[1].sender == "Receiver222"
    assert transfers[1].recipient == "Hop333"
    assert transfers[1].lamports == 3_180_000_000


def test_parse_pumpfun_create_instruction() -> None:
    """Test extracting TokenLaunch from Pump.fun create instruction."""
    name_bytes = b"Doge Coin"
    symbol_bytes = b"DOGE"
    data = (
        CREATE_INSTRUCTION_DISCRIMINATOR
        + len(name_bytes).to_bytes(4, "little")
        + name_bytes
        + len(symbol_bytes).to_bytes(4, "little")
        + symbol_bytes
        + b"https://uri.example/token.json"
    )
    b58_data = base58.b58encode(data).decode("ascii")

    tx_data = {
        "slot": 439874700,
        "blockTime": 1700000047,
        "transaction": {
            "signatures": ["sig_create_123"],
            "message": {
                "accountKeys": [
                    "TokenMintAddr111111111111111111111111111",  # 0: mint
                    "BondingCurve111111111111111111111111111",
                    "AssocBonding111111111111111111111111111",
                    "Global111111111111111111111111111111111",
                    "MplTokenMeta111111111111111111111111111",
                    "Metadata1111111111111111111111111111111",
                    "CreatorWalletAddr11111111111111111111111",  # 6
                    "CreatorWalletAddr11111111111111111111111",  # 7: user/creator
                    PUMP_PROGRAM_ID,
                ],
                "instructions": [
                    {
                        "programIdIndex": 8,
                        "accounts": [0, 1, 2, 3, 4, 5, 6, 7],
                        "data": b58_data,
                    }
                ],
            },
        },
        "meta": {"err": None},
    }

    launch = parse_pumpfun_launch(tx_data)
    assert launch is not None
    assert launch.mint == "TokenMintAddr111111111111111111111111111"
    assert launch.creator == "CreatorWalletAddr11111111111111111111111"
    assert launch.name == "Doge Coin"
    assert launch.symbol == "DOGE"
    assert launch.signature == "sig_create_123"
