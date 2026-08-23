"""On-chain resolver for tokens, creators, and pump.fun metadata."""

# ruff: noqa: S310, BLE001, PLR2004, S110

from __future__ import annotations

import contextlib
import json
import os
import time
import urllib.request
from dataclasses import dataclass
from typing import Any, Final

from solders.pubkey import Pubkey

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
    root_funder: str | None = None


def _rpc_call(rpc_url: str, method: str, params: list[object]) -> object:
    """Perform a raw JSON-RPC HTTP call with endpoint rotation and retries."""
    endpoints = [
        rpc_url,
        "https://api.mainnet-beta.solana.com",
        "https://rpc.ankr.com/solana",
    ]
    payload = json.dumps(
        {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    ).encode()

    last_error: Exception | None = None
    for endpoint in endpoints:
        for _ in range(2):
            try:
                req = urllib.request.Request(
                    endpoint,
                    data=payload,
                    headers={
                        "Content-Type": "application/json",
                        "User-Agent": "Mozilla/5.0",
                    },
                )
                with urllib.request.urlopen(req, timeout=10) as resp:
                    data: dict[str, Any] = json.loads(resp.read().decode())
                    return data.get("result")
            except Exception as exc:
                last_error = exc
                time.sleep(0.5)

    if last_error:
        raise last_error
    return None


def fetch_token_metadata(mint: str) -> tuple[str, str, float, float]:
    """Fetch real token (name, symbol, market_cap_usd, ath_multiplier) using DexScreener and Pump.fun APIs."""
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
                market_cap = float(
                    pairs[0].get("marketCap") or pairs[0].get("fdv") or 0.0
                )
                ath_multiplier = (
                    max(1.0, round(market_cap / 5000.0, 2))
                    if market_cap >= 5000.0
                    else 1.0
                )
                return (name, symbol, market_cap, ath_multiplier)
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
            market_cap = float(data.get("usd_market_cap") or 0.0)
            ath_multiplier = (
                max(1.0, round(market_cap / 5000.0, 2)) if market_cap >= 5000.0 else 1.0
            )
            return (name, symbol, market_cap, ath_multiplier)
    except (
        urllib.error.URLError,
        TimeoutError,
        json.JSONDecodeError,
        KeyError,
        ValueError,
        OSError,
    ):
        return ("Pump Token", "PUMP", 0.0, 1.0)


@dataclass(frozen=True, slots=True)
class DiscoveredTokenLaunch:
    """Historical token launch discovered for an operator cluster."""

    mint: str
    symbol: str
    name: str
    created_at: int
    creator_wallet: str


def scan_helius_cluster_history(
    wallet: str, api_key: str
) -> tuple[str | None, list[DiscoveredTokenLaunch]]:
    """Scan Helius API for incoming SOL transfers and token creation history."""
    url = f"https://api.helius.xyz/v0/addresses/{wallet}/transactions?api-key={api_key}"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    root_funder: str | None = None
    launches: list[DiscoveredTokenLaunch] = []
    try:
        with urllib.request.urlopen(req, timeout=12) as resp:
            txs: list[dict[str, Any]] = json.loads(resp.read().decode())
        for tx in txs:
            if not isinstance(tx, dict):
                continue
            if root_funder is None:
                for nt in tx.get("nativeTransfers", []):
                    from_u = nt.get("fromUserAccount")
                    to_u = nt.get("toUserAccount")
                    amt = nt.get("amount", 0) / 1e9
                    if to_u == wallet and amt > 0.05 and from_u != wallet:
                        root_funder = from_u
                        break
            events = tx.get("events", {})
            pump_ev = events.get("pump", {}) if isinstance(events, dict) else {}
            mint = pump_ev.get("mint")
            if mint:
                launches.append(
                    DiscoveredTokenLaunch(
                        mint=mint,
                        symbol=pump_ev.get("symbol", ""),
                        name=pump_ev.get("name", ""),
                        created_at=int(tx.get("timestamp", time.time())),
                        creator_wallet=wallet,
                    )
                )
    except Exception:
        pass
    return root_funder, launches


def scan_helius_root_funder(wallet: str, api_key: str) -> str | None:
    """Return the latest substantial incoming funder from Helius evidence."""
    root_funder, _ = scan_helius_cluster_history(wallet, api_key)
    return root_funder


def resolve_token_or_wallet(
    input_str: str, custom_label: str | None = None, rpc_url: str | None = None
) -> ResolvedTarget:
    """Resolve an input address to its creator developer wallet or return the wallet directly."""
    cleaned = input_str.strip()
    mint_pubkey = Pubkey.from_string(cleaned)

    endpoint = rpc_url or os.environ.get(
        "SOLANA_RPC_HTTP", "https://api.mainnet-beta.solana.com"
    )
    helius_api_key = os.environ.get("HELIUS_API_KEY")

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

                    name, symbol, _, _ = fetch_token_metadata(cleaned)
                    label = custom_label or f"Dev of {name} (${symbol})"

                    # Helius enrichment is used only for funding evidence. Token
                    # interactions are not launch attribution.
                    root_funder = None
                    if helius_api_key:
                        root_funder = scan_helius_root_funder(
                            creator_wallet, helius_api_key
                        )

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
                        root_funder=root_funder,
                    )

    # Input is a wallet directly
    root_funder = None
    if helius_api_key:
        root_funder = scan_helius_root_funder(cleaned, helius_api_key)

    label = custom_label or f"Dev {cleaned[:6]}..."
    return ResolvedTarget(
        input_address=cleaned,
        target_wallet=cleaned,
        is_token=False,
        default_label=label,
        root_funder=root_funder,
    )
