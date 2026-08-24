"""On-chain resolver for tokens, creators, and pump.fun metadata."""

# ruff: noqa: S310, PLR2004, TRY003, TRY004, C901

from __future__ import annotations

import base64
import json
import os
import time
import urllib.request
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

import base58
from sol_trade_sdk.solana.provider_pool import (
    RpcHttpResponse,
    SyncRpcProviderPool,
    SyncRpcTransport,
)
from solders.pubkey import Pubkey

from rugbot.ingest.pump.bonding_curve_account import (
    PUMP_BONDING_CURVE_LAYOUT_ARTIFACT_VERSION,
    PumpBondingCurveAccountState,
    decode_pump_bonding_curve_creator,
)
from rugbot.ingest.pump.create_decoder import (
    CREATE_V2_ACCOUNT_NAMES,
    CREATE_V2_DISCRIMINATOR,
)
from rugbot.ingest.pump.trade_decoder import (
    BUY_ACCOUNT_NAMES,
    BUY_DISCRIMINATOR,
    BUY_V2_ACCOUNT_NAMES,
    BUY_V2_DISCRIMINATOR,
)

PUMP_PROGRAM_ID = Pubkey.from_string("6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P")
MAX_PAGE_SIGNATURES = 1000
MAX_SIGNATURE_HISTORY_PAGES = 50
CREATE_DISCRIMINATOR = bytes([24, 30, 200, 40, 5, 28, 7, 119])
CREATE_ACCOUNT_NAMES = (
    "mint",
    "mint_authority",
    "bonding_curve",
    "associated_bonding_curve",
    "global",
    "mpl_token_metadata",
    "metadata",
    "user",
    "system_program",
    "token_program",
    "associated_token_program",
    "rent",
    "event_authority",
    "program",
)
CREATE_LAYOUTS = {
    CREATE_DISCRIMINATOR: CREATE_ACCOUNT_NAMES,
    CREATE_V2_DISCRIMINATOR: CREATE_V2_ACCOUNT_NAMES,
}
BUY_LAYOUTS = {
    BUY_DISCRIMINATOR: BUY_ACCOUNT_NAMES,
    BUY_V2_DISCRIMINATOR: BUY_V2_ACCOUNT_NAMES,
}


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
    bonding_curve: str | None = None
    default_label: str = "Tracked Target"
    bundle_wallets: tuple[str, ...] = ()
    root_funder: str | None = None


def _rpc_call(
    rpc_url: str,
    method: str,
    params: list[object],
    transport: SyncRpcTransport | None = None,
) -> object:
    """Perform one raw JSON-RPC call against the configured evidence authority."""
    payload = json.dumps(
        {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    ).encode()
    rpc_transport = transport or SyncRpcProviderPool((rpc_url,))
    response = None
    try:
        for attempt in range(4):
            response = rpc_transport(rpc_url, payload)
            if 200 <= response.status < 300:
                break
            if response.status == 429 and attempt < 3:
                time.sleep(1.5 * (attempt + 1))
                continue
            break
    except Exception:  # noqa: BLE001
        # Direct HTTP fallback if pool is on cooldown
        fallback_urls = [
            "https://api.mainnet-beta.solana.com",
            "https://solana-rpc.publicnode.com",
        ]
        response = None
        for fb_url in fallback_urls:
            try:
                req = urllib.request.Request(
                    fb_url,
                    data=payload,
                    headers={
                        "Content-Type": "application/json",
                        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
                    },
                )
                with urllib.request.urlopen(req, timeout=8) as res:
                    response = RpcHttpResponse(status=res.status, body=res.read())
                    break
            except Exception:  # noqa: BLE001, S112
                continue

    if response is None or not 200 <= response.status < 300:
        status_code = response.status if response is not None else "unknown"
        raise RuntimeError(f"RPC {method} returned HTTP {status_code}")
    try:
        data = json.loads(response.body)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise RuntimeError(f"RPC {method} returned invalid JSON") from error
    if not isinstance(data, Mapping) or "error" in data:
        raise RuntimeError(f"RPC {method} returned an error")
    if "result" not in data:
        raise RuntimeError(f"RPC {method} returned an incomplete response")
    return data["result"]


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


def _complete_signature_history(
    endpoint: str,
    address: str,
    transport: SyncRpcTransport,
) -> tuple[dict[str, Any], ...]:
    """Read a complete bounded finalized signature history or fail closed."""

    signatures: list[dict[str, Any]] = []
    before: str | None = None
    for _ in range(MAX_SIGNATURE_HISTORY_PAGES):
        options: dict[str, object] = {
            "commitment": "finalized",
            "limit": MAX_PAGE_SIGNATURES,
        }
        if before is not None:
            options["before"] = before
        page = _rpc_call(
            endpoint,
            "getSignaturesForAddress",
            [address, options],
            transport,
        )
        if not isinstance(page, list):
            raise RuntimeError("signature history response is incomplete")
        typed_page = tuple(item for item in page if isinstance(item, dict))
        if len(typed_page) != len(page):
            raise RuntimeError("signature history contains malformed entries")
        signatures.extend(typed_page)
        if len(page) < MAX_PAGE_SIGNATURES:
            return tuple(signatures)
        candidate = typed_page[-1].get("signature") if typed_page else None
        if not isinstance(candidate, str) or not candidate or candidate == before:
            raise RuntimeError("signature history pagination did not advance")
        before = candidate
    raise RuntimeError("signature history exceeded the bounded page limit")


def _account_keys(transaction: Mapping[str, object]) -> tuple[str, ...]:
    """Return static and loaded transaction account keys in runtime order."""

    transaction_value = transaction.get("transaction")
    meta = transaction.get("meta")
    if not isinstance(transaction_value, Mapping) or not isinstance(meta, Mapping):
        return ()
    message = transaction_value.get("message")
    loaded = meta.get("loadedAddresses")
    if not isinstance(message, Mapping):
        return ()
    raw_keys = message.get("accountKeys")
    if not isinstance(raw_keys, list):
        return ()
    keys = [
        item.get("pubkey") if isinstance(item, Mapping) else item for item in raw_keys
    ]
    if isinstance(loaded, Mapping):
        for group in ("writable", "readonly"):
            values = loaded.get(group)
            if isinstance(values, list):
                keys.extend(values)
    return tuple(key for key in keys if isinstance(key, str) and key)


def _compiled_instructions(
    transaction: Mapping[str, object],
) -> Iterable[tuple[bytes, tuple[str, ...]]]:
    """Yield Pump instructions from outer and inner compiled instruction groups."""

    keys = _account_keys(transaction)
    transaction_value = transaction.get("transaction")
    meta = transaction.get("meta")
    if not keys or not isinstance(transaction_value, Mapping):
        return
    message = transaction_value.get("message")
    if not isinstance(message, Mapping):
        return
    groups: list[object] = [message.get("instructions")]
    if isinstance(meta, Mapping):
        inner_groups = meta.get("innerInstructions")
        if isinstance(inner_groups, list):
            groups.extend(
                item.get("instructions")
                for item in inner_groups
                if isinstance(item, Mapping)
            )
    for group in groups:
        if not isinstance(group, list):
            continue
        for instruction in group:
            if not isinstance(instruction, Mapping):
                continue
            program_index = instruction.get("programIdIndex")
            account_indices = instruction.get("accounts")
            encoded_data = instruction.get("data")
            if (
                type(program_index) is not int
                or not 0 <= program_index < len(keys)
                or keys[program_index] != str(PUMP_PROGRAM_ID)
                or not isinstance(account_indices, list)
                or not isinstance(encoded_data, str)
            ):
                continue
            if not all(
                type(index) is int and 0 <= index < len(keys)
                for index in account_indices
            ):
                continue
            try:
                data = base58.b58decode(encoded_data)
            except ValueError:
                continue
            yield data, tuple(keys[index] for index in account_indices)


def _creation_identity(
    transaction: Mapping[str, object], mint: str
) -> tuple[str, str] | None:
    """Return the pinned create signature and creator for the requested mint."""

    transaction_value = transaction.get("transaction")
    if not isinstance(transaction_value, Mapping):
        return None
    signatures = transaction_value.get("signatures")
    if (
        not isinstance(signatures, list)
        or not signatures
        or not isinstance(signatures[0], str)
    ):
        return None
    matches: list[str] = []
    for data, accounts in _compiled_instructions(transaction):
        names = CREATE_LAYOUTS.get(data[:8])
        if names is None or len(accounts) < len(names):
            continue
        mapped = dict(zip(names, accounts[: len(names)], strict=True))
        if mapped["mint"] == mint:
            matches.append(mapped["user"])
    if len(matches) != 1:
        return None
    return signatures[0], matches[0]


def _same_slot_buyers(transaction: Mapping[str, object], mint: str) -> set[str]:
    """Return wallets with pinned Pump buys for the mint in one transaction."""

    buyers: set[str] = set()
    for data, accounts in _compiled_instructions(transaction):
        names = BUY_LAYOUTS.get(data[:8])
        if names is None or len(accounts) < len(names):
            continue
        mapped = dict(zip(names, accounts[: len(names)], strict=True))
        trade_mint = mapped.get("mint") or mapped.get("base_mint")
        if trade_mint == mint:
            buyers.add(mapped["user"])
    return buyers


def resolve_token_or_wallet(
    input_str: str,
    custom_label: str | None = None,
    rpc_url: str | None = None,
    fallback_endpoints: tuple[str, ...] = (),
) -> ResolvedTarget:
    """Resolve an input address to its creator developer wallet or return the wallet directly."""
    cleaned = input_str.strip()
    mint_pubkey = Pubkey.from_string(cleaned)

    endpoint = rpc_url or os.environ.get("SOLANA_RPC_HTTP")
    if not endpoint:
        raise ValueError("SOLANA_RPC_HTTP is required to resolve a token or wallet")
    rpc_transport = SyncRpcProviderPool((endpoint, *fallback_endpoints))

    bonding_curve_pda, _ = Pubkey.find_program_address(
        [b"bonding-curve", bytes(mint_pubkey)], PUMP_PROGRAM_ID
    )
    acc_info: Any = _rpc_call(
        endpoint,
        "getAccountInfo",
        [
            str(bonding_curve_pda),
            {"commitment": "finalized", "encoding": "base64"},
        ],
        rpc_transport,
    )
    if not isinstance(acc_info, Mapping) or "value" not in acc_info:
        raise RuntimeError("bonding curve account response is incomplete")

    if acc_info["value"] is not None:
        creator_from_account = _creator_from_bonding_curve_account(
            acc_info, str(bonding_curve_pda)
        )
        signatures = _complete_signature_history(
            endpoint,
            str(bonding_curve_pda),
            rpc_transport,
        )
        if not signatures:
            raise RuntimeError("bonding curve has no finalized signature history")
        slots = tuple(
            item["slot"] for item in signatures if type(item.get("slot")) is int
        )
        if len(slots) != len(signatures):
            raise RuntimeError("signature history contains malformed slots")
        oldest_slot = min(slots)
        same_slot = tuple(
            item
            for item in signatures
            if item.get("slot") == oldest_slot
            and isinstance(item.get("signature"), str)
        )
        candidates: list[tuple[str, str, Mapping[str, object]]] = []
        buyers: set[str] = set()
        for item in same_slot:
            tx_info = _rpc_call(
                endpoint,
                "getTransaction",
                [
                    item["signature"],
                    {
                        "commitment": "finalized",
                        "encoding": "json",
                        "maxSupportedTransactionVersion": 0,
                    },
                ],
                rpc_transport,
            )
            if not isinstance(tx_info, Mapping):
                continue
            identity = _creation_identity(tx_info, cleaned)
            if identity is not None:
                candidates.append((*identity, tx_info))
            buyers.update(_same_slot_buyers(tx_info, cleaned))

        if len(candidates) != 1:
            raise RuntimeError(
                "finalized creation transaction was not uniquely identified"
            )
        creation_sig, creator_wallet, _creation_tx = candidates[0]
        if creator_from_account is not None and creator_wallet != creator_from_account:
            raise RuntimeError(
                "finalized creation conflicts with bonding curve creator"
            )
        bundle_wallets = tuple(sorted(buyers - {creator_wallet}))

        name, symbol, _, _ = fetch_token_metadata(cleaned)
        label = custom_label or f"Dev of {name} (${symbol})"

        return ResolvedTarget(
            input_address=cleaned,
            target_wallet=creator_wallet,
            is_token=True,
            symbol=symbol,
            name=name,
            creation_slot=oldest_slot,
            creation_signature=creation_sig,
            bonding_curve=str(bonding_curve_pda),
            default_label=label,
            bundle_wallets=bundle_wallets,
        )

    # Input is a wallet directly
    label = custom_label or f"Dev {cleaned[:6]}..."
    return ResolvedTarget(
        input_address=cleaned,
        target_wallet=cleaned,
        is_token=False,
        default_label=label,
    )


def _creator_from_bonding_curve_account(
    account_info: Mapping[str, object], account_pubkey: str
) -> str | None:
    """Read the finalized current-layout creator without scanning all trades."""

    context = account_info.get("context")
    value = account_info.get("value")
    if not isinstance(context, Mapping) or not isinstance(value, Mapping):
        return None
    slot = context.get("slot")
    owner = value.get("owner")
    encoded_data = value.get("data")
    if (
        type(slot) is not int
        or not isinstance(owner, str)
        or not isinstance(encoded_data, list)
        or len(encoded_data) != 2
        or not isinstance(encoded_data[0], str)
        or encoded_data[1] != "base64"
    ):
        return None
    try:
        raw_data = base64.b64decode(encoded_data[0], validate=True)
    except ValueError as error:
        raise RuntimeError("bonding curve account returned invalid base64") from error
    decoded = decode_pump_bonding_curve_creator(
        PumpBondingCurveAccountState(
            as_of_slot=slot,
            account_pubkey=account_pubkey,
            owner_program_id=owner,
            raw_account_data=raw_data,
            source_artifact_version="solana-rpc-finalized-getAccountInfo",
            layout_artifact_version=PUMP_BONDING_CURVE_LAYOUT_ARTIFACT_VERSION,
        )
    )
    if not isinstance(decoded, bytes):
        raise RuntimeError(decoded.message)
    return str(Pubkey.from_bytes(decoded))
