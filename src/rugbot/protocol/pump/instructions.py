"""Pump.fun instruction and event decoders."""

# ruff: noqa: C901, PLR2004

from __future__ import annotations

from typing import Any

import base58

from rugbot.protocol.pump.models import TokenLaunch

PUMP_PROGRAM_ID = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
CREATE_INSTRUCTION_DISCRIMINATOR = bytes([24, 30, 200, 40, 5, 28, 7, 119])


def parse_pumpfun_launch(tx_data: dict[str, Any]) -> TokenLaunch | None:
    """Extract a verified Pump.fun token launch event from a transaction."""
    meta = tx_data.get("meta")
    if meta and meta.get("err") is not None:
        return None

    slot = int(tx_data.get("slot", 0))
    timestamp = int(tx_data.get("blockTime", 0) or 0)
    tx = tx_data.get("transaction", {})

    signatures = tx.get("signatures", [])
    if not signatures and "signature" in tx_data:
        signatures = [tx_data["signature"]]
    if not signatures:
        return None
    signature = signatures[0]

    message = tx.get("message", {})
    raw_keys = message.get("accountKeys", [])
    account_keys: list[str] = []
    for item in raw_keys:
        if isinstance(item, str):
            account_keys.append(item)
        elif isinstance(item, dict) and "pubkey" in item:
            account_keys.append(str(item["pubkey"]))

    if not account_keys:
        return None

    outer_ixs = message.get("instructions", [])
    for ix in outer_ixs:
        launch = _parse_pump_create_ix(ix, account_keys, signature, slot, timestamp)
        if launch is not None:
            return launch

    inner_ix_groups = meta.get("innerInstructions", []) if meta else []
    for group in inner_ix_groups:
        for inner_ix in group.get("instructions", []):
            launch = _parse_pump_create_ix(
                inner_ix, account_keys, signature, slot, timestamp
            )
            if launch is not None:
                return launch

    return None


def _parse_pump_create_ix(
    ix: dict[str, Any],
    account_keys: list[str],
    signature: str,
    slot: int,
    timestamp: int,
) -> TokenLaunch | None:
    prog_id_raw = ix.get("programId")
    prog_idx = ix.get("programIdIndex")
    program_id = ""
    if isinstance(prog_id_raw, str):
        program_id = prog_id_raw
    elif isinstance(prog_idx, int) and 0 <= prog_idx < len(account_keys):
        program_id = account_keys[prog_idx]

    if program_id != PUMP_PROGRAM_ID:
        return None

    raw_data = ix.get("data")
    if not isinstance(raw_data, str):
        return None

    try:
        data_bytes = base58.b58decode(raw_data)
    except Exception:  # noqa: BLE001
        return None

    if len(data_bytes) < 8 or data_bytes[:8] != CREATE_INSTRUCTION_DISCRIMINATOR:
        return None

    offset = 8
    try:
        name_len = int.from_bytes(data_bytes[offset : offset + 4], "little")
        offset += 4
        name = data_bytes[offset : offset + name_len].decode("utf-8", errors="replace")
        offset += name_len

        symbol_len = int.from_bytes(data_bytes[offset : offset + 4], "little")
        offset += 4
        symbol = data_bytes[offset : offset + symbol_len].decode(
            "utf-8", errors="replace"
        )
        offset += symbol_len
    except Exception:  # noqa: BLE001
        name = "Unknown"
        symbol = "UNKNOWN"

    accounts = ix.get("accounts", [])
    if len(accounts) >= 8:
        mint_idx = accounts[0]
        creator_idx = accounts[7]
        if (
            isinstance(mint_idx, int)
            and isinstance(creator_idx, int)
            and 0 <= mint_idx < len(account_keys)
            and 0 <= creator_idx < len(account_keys)
        ):
            mint = account_keys[mint_idx]
            creator = account_keys[creator_idx]
            return TokenLaunch(
                signature=signature,
                slot=slot,
                timestamp=timestamp,
                creator=creator,
                mint=mint,
                symbol=symbol,
                name=name,
            )

    return None
