"""Normalized Solana SystemProgram native transfer parser."""

# ruff: noqa: C901, PLR0913, PLR2004

from __future__ import annotations

from typing import Any

import base58

from rugbot.protocol.solana.models import SolTransfer

SYSTEM_PROGRAM_ID = "11111111111111111111111111111111"
TRANSFER_INSTRUCTION_INDEX = 2


def parse_sol_transfers(tx_data: dict[str, Any]) -> tuple[SolTransfer, ...]:
    """Extract all verified native SOL transfers from outer and inner instructions of a transaction."""
    meta = tx_data.get("meta")
    if meta and meta.get("err") is not None:
        return ()

    slot = int(tx_data.get("slot", 0))
    timestamp = int(tx_data.get("blockTime", 0) or 0)
    tx = tx_data.get("transaction", {})

    signatures = tx.get("signatures", [])
    if not signatures and "signature" in tx_data:
        signatures = [tx_data["signature"]]
    if not signatures:
        return ()
    signature = signatures[0]

    message = tx.get("message", {})
    account_keys = _extract_account_keys(message)
    if not account_keys:
        return ()

    results: list[SolTransfer] = []

    # 1. Outer instructions
    outer_ixs = message.get("instructions", [])
    for idx, ix in enumerate(outer_ixs):
        transfer = _parse_instruction_transfer(
            ix, account_keys, signature, idx, slot, timestamp
        )
        if transfer is not None:
            results.append(transfer)

    # 2. Inner instructions
    inner_ix_groups = meta.get("innerInstructions", []) if meta else []
    for group in inner_ix_groups:
        parent_idx = int(group.get("index", 0))
        for inner_sub_idx, inner_ix in enumerate(group.get("instructions", [])):
            flat_idx = 1000 * (parent_idx + 1) + inner_sub_idx
            transfer = _parse_instruction_transfer(
                inner_ix, account_keys, signature, flat_idx, slot, timestamp
            )
            if transfer is not None:
                results.append(transfer)

    return tuple(results)


def _extract_account_keys(message: dict[str, Any]) -> list[str]:
    raw_keys = message.get("accountKeys", [])
    keys: list[str] = []
    for item in raw_keys:
        if isinstance(item, str):
            keys.append(item)
        elif isinstance(item, dict) and "pubkey" in item:
            keys.append(str(item["pubkey"]))
    return keys


def _parse_instruction_transfer(
    ix: dict[str, Any],
    account_keys: list[str],
    signature: str,
    index: int,
    slot: int,
    timestamp: int,
) -> SolTransfer | None:
    # A. JSON-parsed instruction format
    parsed = ix.get("parsed")
    if isinstance(parsed, dict) and parsed.get("type") == "transfer":
        info = parsed.get("info", {})
        sender = str(info.get("source", ""))
        recipient = str(info.get("destination", ""))
        lamports = int(info.get("lamports", 0))
        if sender and recipient and lamports > 0:
            return SolTransfer(
                signature=signature,
                instruction_index=index,
                slot=slot,
                timestamp=timestamp,
                sender=sender,
                recipient=recipient,
                lamports=lamports,
            )

    # B. Compiled raw instruction format
    prog_id_raw = ix.get("programId")
    prog_idx = ix.get("programIdIndex")
    program_id = ""
    if isinstance(prog_id_raw, str):
        program_id = prog_id_raw
    elif isinstance(prog_idx, int) and 0 <= prog_idx < len(account_keys):
        program_id = account_keys[prog_idx]

    if program_id != SYSTEM_PROGRAM_ID:
        return None

    raw_data = ix.get("data")
    if not isinstance(raw_data, str):
        return None

    try:
        data_bytes = base58.b58decode(raw_data)
    except Exception:  # noqa: BLE001
        return None

    if (
        len(data_bytes) == 12
        and int.from_bytes(data_bytes[0:4], "little") == TRANSFER_INSTRUCTION_INDEX
    ):
        lamports = int.from_bytes(data_bytes[4:12], "little")
        accounts = ix.get("accounts", [])
        if len(accounts) >= 2:
            src_idx, dst_idx = accounts[0], accounts[1]
            if (
                isinstance(src_idx, int)
                and isinstance(dst_idx, int)
                and 0 <= src_idx < len(account_keys)
                and 0 <= dst_idx < len(account_keys)
            ):
                sender = account_keys[src_idx]
                recipient = account_keys[dst_idx]
                if lamports > 0:
                    return SolTransfer(
                        signature=signature,
                        instruction_index=index,
                        slot=slot,
                        timestamp=timestamp,
                        sender=sender,
                        recipient=recipient,
                        lamports=lamports,
                    )

    return None
