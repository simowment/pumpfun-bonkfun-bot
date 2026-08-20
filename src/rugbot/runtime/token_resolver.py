"""On-chain resolver for tokens, creators, and pump.fun metadata."""

# ruff: noqa: S310

from __future__ import annotations

import contextlib
import json
import os
import urllib.request
from dataclasses import dataclass
from typing import Any, Final

from solders.pubkey import Pubkey

from rugbot.runtime.config import resolve_dotenv

resolve_dotenv()
PUMP_PROGRAM_ID = Pubkey.from_string("6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P")
MAX_PAGE_SIGNATURES = 1000
MIN_BASE58_ADDRESS_LENGTH: Final[int] = 32


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
    bundle_wallets: tuple[str, ...] = ()


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


def fetch_token_metadata(mint: str) -> tuple[str, str]:
    """Fetch real token (name, symbol) using DexScreener and Pump.fun APIs."""
    # 1. DexScreener API (fast, unthrottled, returns verified on-chain token name and symbol)
    try:
        url = f"https://api.dexscreener.com/latest/dex/tokens/{mint}"
        req = urllib.request.Request(
            url,
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"},
        )
        with urllib.request.urlopen(req, timeout=3) as resp:
            data = json.loads(resp.read().decode())
            pairs = data.get("pairs") or []
            if pairs:
                base = pairs[0].get("baseToken", {})
                name = str(base.get("name", "")).strip() or "Pump Token"
                symbol = str(base.get("symbol", "")).strip() or name[:6].upper()
                return (name, symbol)
    except (
        urllib.error.URLError,
        TimeoutError,
        json.JSONDecodeError,
        KeyError,
        ValueError,
        OSError,
    ):
        pass

    # 2. Fallback to pump.fun frontend API
    try:
        url = f"https://frontend-api-v2.pump.fun/coins/{mint}"
        req = urllib.request.Request(
            url,
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"},
        )
        with urllib.request.urlopen(req, timeout=3) as resp:
            data = json.loads(resp.read().decode())
            name = str(data.get("name", "")).strip() or "Pump Token"
            symbol = str(data.get("symbol", "")).strip() or "PUMP"
            return (name, symbol)
    except (
        urllib.error.URLError,
        TimeoutError,
        json.JSONDecodeError,
        KeyError,
        ValueError,
        OSError,
    ):
        return ("Pump Token", "PUMP")


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
                    raw_keys = tx_info["transaction"]["message"]["accountKeys"]
                    account_keys = [
                        k["pubkey"] if isinstance(k, dict) else k for k in raw_keys
                    ]
                    creator_wallet = account_keys[0]

                    known_system = {
                        "11111111111111111111111111111111",
                        "ComputeBudget111111111111111111111111111111",
                        "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",
                        "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb",
                        "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P",
                        "metaqbxxUerdq28cj1RbAWkYQm3ybzjb6a8bt518x1s",
                        "SysvarRent111111111111111111111111111111111",
                        "8fwS3wUbk5qeUe9RAyxiLi21hVM6EqiTCX1NA1DH6FyG",
                    }
                    bundle_wallets = tuple(
                        acc
                        for acc in account_keys[1:]
                        if acc not in known_system
                        and not acc.endswith("pump")
                        and len(acc) >= MIN_BASE58_ADDRESS_LENGTH
                    )

                    name, symbol = fetch_token_metadata(cleaned)
                    label = custom_label or f"Dev of {name} (${symbol})"
                    return ResolvedTarget(
                        input_address=cleaned,
                        target_wallet=creator_wallet,
                        is_token=True,
                        symbol=symbol,
                        name=name,
                        creation_slot=creation_slot,
                        creation_signature=creation_sig,
                        default_label=label,
                        bundle_wallets=bundle_wallets,
                    )

    label = custom_label or f"Dev {cleaned[:6]}..."
    return ResolvedTarget(
        input_address=cleaned,
        target_wallet=cleaned,
        is_token=False,
        default_label=label,
    )
