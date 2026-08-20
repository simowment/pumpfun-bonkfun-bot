"""On-chain resolver for tokens, creators, and pump.fun metadata."""

# ruff: noqa: S310

from __future__ import annotations

import contextlib
import json
import os
import urllib.request
from dataclasses import dataclass
from typing import Any

from solders.pubkey import Pubkey

from rugbot.runtime.config import resolve_dotenv

resolve_dotenv()
PUMP_PROGRAM_ID = Pubkey.from_string("6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P")
MAX_PAGE_SIGNATURES = 1000


@dataclass(frozen=True, slots=True)
class ResolvedTarget:
    """Resolved entity identity from user input (token or wallet)."""

    input_address: str
    target_wallet: str
    is_token: bool
    symbol: str | None = None
    name: str | None = None
    creation_slot: int | None = None
    creation_signature: str | None = None
    default_label: str = "Tracked Target"


def _rpc_call(rpc_url: str, method: str, params: list[object]) -> object:
    """Perform a raw JSON-RPC HTTP call."""
    payload = json.dumps(
        {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    ).encode()
    req = urllib.request.Request(
        rpc_url, data=payload, headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        data: dict[str, Any] = json.loads(resp.read().decode())
        return data.get("result")


def resolve_token_or_wallet(
    input_str: str, custom_label: str | None = None, rpc_url: str | None = None
) -> ResolvedTarget:
    """Resolve an input address to its creator developer wallet or return the wallet directly."""
    cleaned = input_str.strip()
    mint_pubkey = Pubkey.from_string(cleaned)

    endpoint = rpc_url or os.environ.get(
        "SOLANA_RPC_HTTP", "https://api.mainnet-beta.solana.com"
    )

    with contextlib.suppress(Exception):
        bonding_curve_pda, _ = Pubkey.find_program_address(
            [b"bonding-curve", bytes(mint_pubkey)], PUMP_PROGRAM_ID
        )
        acc_info: Any = _rpc_call(
            endpoint,
            "getAccountInfo",
            [str(bonding_curve_pda), {"encoding": "jsonParsed"}],
        )

        if acc_info and acc_info.get("value"):
            last_sig = None
            creation_sig = None
            creation_slot = None

            for _ in range(5):
                params: list[object] = [
                    str(bonding_curve_pda),
                    {"limit": MAX_PAGE_SIGNATURES},
                ]
                if last_sig:
                    params[1] = {"limit": MAX_PAGE_SIGNATURES, "before": last_sig}
                sigs: Any = _rpc_call(endpoint, "getSignaturesForAddress", params)
                if not sigs:
                    break
                creation_sig = sigs[-1]["signature"]
                creation_slot = sigs[-1]["slot"]
                if len(sigs) < MAX_PAGE_SIGNATURES:
                    break
                last_sig = sigs[-1]["signature"]

            if creation_sig:
                tx_info: Any = _rpc_call(
                    endpoint,
                    "getTransaction",
                    [
                        creation_sig,
                        {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0},
                    ],
                )
                if tx_info and tx_info.get("transaction"):
                    account_keys = tx_info["transaction"]["message"]["accountKeys"]
                    creator_wallet = (
                        account_keys[0]["pubkey"]
                        if isinstance(account_keys[0], dict)
                        else account_keys[0]
                    )

                    label = custom_label or f"Dev of {cleaned[:6]}...pump"
                    return ResolvedTarget(
                        input_address=cleaned,
                        target_wallet=creator_wallet,
                        is_token=True,
                        symbol="PUMP",
                        name="Pump Token",
                        creation_slot=creation_slot,
                        creation_signature=creation_sig,
                        default_label=label,
                    )

    label = custom_label or f"Dev {cleaned[:6]}..."
    return ResolvedTarget(
        input_address=cleaned,
        target_wallet=cleaned,
        is_token=False,
        default_label=label,
    )
