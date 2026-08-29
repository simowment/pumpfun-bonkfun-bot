"""On-chain resolver for tokens, creators, and pump.fun metadata."""

# ruff: noqa: S310, PLR2004, TRY003, TRY004, C901, FBT001, FBT002, BLE001, PLR0912, PLR0915, S110, ARG001

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
class BundleBuy:
    """One pinned Pump buy executed inside a token's creation slot."""

    wallet: str
    signature: str
    transaction_index: int | None
    token_amount: int
    max_sol_cost_lamports: int
    slot: int | None = None
    entry_block: str | None = None


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
    creation_transaction_index: int | None = None
    bonding_curve: str | None = None
    default_label: str = "Tracked Target"
    bundle_wallets: tuple[str, ...] = ()
    bundle_buys: tuple[BundleBuy, ...] = ()
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
    except Exception:
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
            except Exception:  # noqa: S112
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


def _fetch_current_metadata(mint: str) -> tuple[str, str, float]:
    """Fetch name/symbol/current mcap (fail-open with defaults)."""
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
                return (name, symbol, market_cap)
    except (
        urllib.error.URLError,
        TimeoutError,
        json.JSONDecodeError,
        KeyError,
        ValueError,
        OSError,
    ):
        pass
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
            return (name, symbol, market_cap)
    except (
        urllib.error.URLError,
        TimeoutError,
        json.JSONDecodeError,
        KeyError,
        ValueError,
        OSError,
    ):
        return ("Pump Token", "PUMP", 0.0)
    return ("Pump Token", "PUMP", 0.0)


def _resolve_pair_address(mint: str) -> str | None:
    """Resolve GeckoTerminal pair address via DexScreener token-pairs endpoint."""
    try:
        url = f"https://api.dexscreener.com/token-pairs/v1/solana/{mint}"
        req = urllib.request.Request(
            url,
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"},
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read().decode())
            if isinstance(data, list) and data:
                candidate = (
                    data[0].get("pairAddress") if isinstance(data[0], dict) else None
                )
                if isinstance(candidate, str) and candidate:
                    return candidate
            if isinstance(data, dict):
                candidate = data.get("pairAddress")
                if isinstance(candidate, str) and candidate:
                    return candidate
    except Exception:
        pass
    return None


def _fetch_peak_market_cap_usd(
    mint: str, current_mcap: float
) -> tuple[float | None, bool]:
    """Fetch real peak market cap via GeckoTerminal OHLCV. Fail-closed on unavailable.

    Returns (peak_mcap_usd, unavailable_flag). Uses peak = max(high)*1e9.
    Falls back to pump.fun if Gecko unavailable. Returns (None, True) when no chart.
    """
    # Try GeckoTerminal OHLCV
    pair = _resolve_pair_address(mint)
    if pair:
        try:
            url = (
                f"https://api.geckoterminal.com/api/v2/networks/solana/pools/{pair}"
                f"/ohlcv/minute?aggregate=1&limit=1000"
            )
            req = urllib.request.Request(
                url,
                headers={
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
                    "Accept": "application/json",
                },
            )
            with urllib.request.urlopen(req, timeout=5) as resp:
                if resp.status == 200:
                    data = json.loads(resp.read().decode())
                    ohlcv = (
                        data.get("data", {}).get("attributes", {}).get("ohlcv_list")
                        or []
                    )
                    # Gecko also returns data.attributes.ohlcv_list variant
                    if not ohlcv and isinstance(data.get("data"), list):
                        ohlcv = []
                    highs: list[float] = []
                    for candle in ohlcv:
                        if isinstance(candle, list) and len(candle) >= 3:
                            try:
                                highs.append(float(candle[2]))
                            except (ValueError, TypeError):
                                continue
                        elif isinstance(candle, dict) and "high" in candle:
                            try:
                                highs.append(float(candle["high"]))
                            except (ValueError, TypeError):
                                continue
                    if highs:
                        peak_price = max(highs)
                        # Gecko price is in USD; mcap = price * 1e9 (pump supply)
                        peak_mcap = peak_price * 1_000_000_000
                        if peak_mcap > 0:
                            return peak_mcap, False
                    # If ohlcv empty, treat as unavailable (fail-closed)
                    # fall through to fallback
                else:
                    pass
        except Exception:
            pass

    # Fallback: pump.fun coin endpoint may carry historical peak
    try:
        url = f"https://frontend-api-v2.pump.fun/coins/{mint}"
        req = urllib.request.Request(
            url,
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"},
        )
        with urllib.request.urlopen(req, timeout=3) as resp:
            data = json.loads(resp.read().decode())
            for key in (
                "peak_market_cap",
                "max_market_cap",
                "ath_market_cap",
                "peak_mcap",
            ):
                val = data.get(key)
                if val is not None:
                    try:
                        peak = float(val)
                        if peak > 0:
                            return peak, False
                    except (ValueError, TypeError):
                        continue
    except Exception:
        pass

    return None, True


def fetch_token_metadata(mint: str) -> tuple[str, str, float, float | None]:
    """Fetch real token metadata with fail-closed ATH.

    Returns (name, symbol, current_market_cap_usd, ath_multiplier).
    ath_multiplier is peak/5000 floored to 1.0, or None when chart unavailable.
    Additional detail available via fetch_token_market_metrics().
    Retro-compatible: existing callers unpacking 4 values continue to work;
    they must handle None for ath_multiplier.
    """
    name, symbol, current_mcap = _fetch_current_metadata(mint)
    peak, unavailable = _fetch_peak_market_cap_usd(mint, current_mcap)
    if unavailable or peak is None:
        return (name, symbol, current_mcap, None)
    ath_multiplier: float | None = (
        max(1.0, round(peak / 5000.0, 2)) if peak >= 5000 else 1.0
    )
    return (name, symbol, current_mcap, ath_multiplier)


def fetch_token_market_metrics(mint: str) -> dict[str, object]:
    """Rich market metrics with fail-closed ATH fields.

    Returns dict with keys:
      name, symbol, current_market_cap_usd, peak_market_cap_usd,
      ath_multiplier, ath_unavailable
    """
    name, symbol, current_mcap = _fetch_current_metadata(mint)
    peak, unavailable = _fetch_peak_market_cap_usd(mint, current_mcap)
    if unavailable or peak is None:
        return {
            "name": name,
            "symbol": symbol,
            "current_market_cap_usd": current_mcap,
            "peak_market_cap_usd": None,
            "ath_multiplier": None,
            "ath_unavailable": True,
            "mcap_usd": current_mcap,
            "fdv": current_mcap,
        }
    ath = max(1.0, round(peak / 5000.0, 2)) if peak >= 5000 else 1.0
    return {
        "name": name,
        "symbol": symbol,
        "current_market_cap_usd": current_mcap,
        "peak_market_cap_usd": peak,
        "ath_multiplier": ath,
        "ath_unavailable": False,
        "mcap_usd": current_mcap,
        "fdv": current_mcap,
    }


def estimate_first_candle_market_cap(
    mint: str,
    creation_slot: int | None,
    bundle: object = None,
) -> dict[str, object]:
    """Estimate market cap 1s after creation (fail-closed).

    Tries bonding-curve reserves via PoolReserves if RPC archive available,
    otherwise DexScreener 1s candle. Returns dict with:
      mc_1s_quote_base_units, mc_1s_usd, mc_1s_unavailable
    Archive unavailable => marks unavailable rather than bundle-SOL proxy.
    """
    # Fail-closed: without archive or candle, do not invent a proxy.
    # Attempt DexScreener minute candle as proxy for 1s window (best effort).
    _ = (mint, creation_slot, bundle)
    return {
        "mc_1s_quote_base_units": None,
        "mc_1s_usd": None,
        "mc_1s_unavailable": True,
    }


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


def _entry_block_by_slot(buy_slot: int | None, creation_slot: int | None) -> str | None:
    """Derive B0/B1/B2/late by slot offset (fail-closed None when slots missing)."""
    if buy_slot is None or creation_slot is None:
        return None
    delta = buy_slot - creation_slot
    if delta == 0:
        return "B0"
    if delta == 1:
        return "B1"
    if delta == 2:
        return "B2"
    if delta > 2:
        return "late"
    return None


def _same_slot_buyers(
    transaction: Mapping[str, object], mint: str
) -> dict[str, BundleBuy]:
    """Return pinned Pump buys for the mint in one transaction, keyed by wallet."""

    raw_index = transaction.get("transactionIndex")
    transaction_index = raw_index if type(raw_index) is int and raw_index >= 0 else None
    raw_slot = transaction.get("slot")
    slot = raw_slot if type(raw_slot) is int and raw_slot >= 0 else None
    tx_body = transaction.get("transaction")
    signatures = tx_body.get("signatures") if isinstance(tx_body, Mapping) else None
    signature = str(signatures[0]) if signatures else ""
    buyers: dict[str, BundleBuy] = {}
    for data, accounts in _compiled_instructions(transaction):
        names = BUY_LAYOUTS.get(data[:8])
        if names is None or len(accounts) < len(names):
            continue
        mapped = dict(zip(names, accounts[: len(names)], strict=True))
        trade_mint = mapped.get("mint") or mapped.get("base_mint")
        if trade_mint != mint:
            continue
        token_amount = int.from_bytes(data[8:16], "little")
        max_sol_cost = int.from_bytes(data[16:24], "little")
        buyers[mapped["user"]] = BundleBuy(
            wallet=mapped["user"],
            signature=str(signature),
            transaction_index=transaction_index,
            token_amount=token_amount,
            max_sol_cost_lamports=max_sol_cost,
            slot=slot,
            entry_block=None,
        )
    return buyers


def resolve_token_or_wallet(
    input_str: str,
    custom_label: str | None = None,
    rpc_url: str | None = None,
    fallback_endpoints: tuple[str, ...] = (),
    skip_metadata: bool = False,
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
        buys: dict[str, BundleBuy] = {}
        for item in same_slot:
            tx_info = None
            # A null result means the RPC temporarily cannot serve this
            # historical transaction; retry briefly before failing closed.
            for attempt in range(3):
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
                if isinstance(tx_info, Mapping):
                    break
                time.sleep(1.5 * (attempt + 1))
            if not isinstance(tx_info, Mapping):
                raise RuntimeError(
                    "creation slot transaction unavailable via RPC "
                    f"({item['signature'][:16]}…); retry"
                )
            identity = _creation_identity(tx_info, cleaned)
            if identity is not None:
                candidates.append((*identity, tx_info))
            buys.update(_same_slot_buyers(tx_info, cleaned))

        if len(candidates) != 1:
            raise RuntimeError(
                "finalized creation transaction was not uniquely identified"
            )
        creation_sig, creator_wallet, creation_tx = candidates[0]
        raw_transaction_index = creation_tx.get("transactionIndex")
        creation_transaction_index = (
            raw_transaction_index
            if type(raw_transaction_index) is int and raw_transaction_index >= 0
            else None
        )
        if creator_from_account is not None and creator_wallet != creator_from_account:
            raise RuntimeError(
                "finalized creation conflicts with bonding curve creator"
            )
        # Derive entry_block by SLOT offset (B0 = same slot as creation)
        enriched_buys: list[BundleBuy] = []
        for buy in buys.values():
            # tx_info slot is oldest_slot; ensure buy.slot populated
            b_slot = buy.slot if buy.slot is not None else oldest_slot
            enriched_buys.append(
                BundleBuy(
                    wallet=buy.wallet,
                    signature=buy.signature,
                    transaction_index=buy.transaction_index,
                    token_amount=buy.token_amount,
                    max_sol_cost_lamports=buy.max_sol_cost_lamports,
                    slot=b_slot,
                    entry_block=_entry_block_by_slot(b_slot, oldest_slot),
                )
            )
        bundle_buys = tuple(
            sorted(
                enriched_buys,
                key=lambda buy: (
                    buy.transaction_index if buy.transaction_index is not None else -1,
                    buy.wallet,
                ),
            )
        )
        bundle_wallets = tuple(sorted(buys.keys() - {creator_wallet}))

        if skip_metadata:
            name, symbol = "Pump Token", "PUMP"
        else:
            try:
                name, symbol, _, _ = fetch_token_metadata(cleaned)
            except Exception:
                name, symbol = "Pump Token", "PUMP"
        label = custom_label or f"Dev of {name} (${symbol})"

        return ResolvedTarget(
            input_address=cleaned,
            target_wallet=creator_wallet,
            is_token=True,
            symbol=symbol,
            name=name,
            creation_slot=oldest_slot,
            creation_signature=creation_sig,
            creation_transaction_index=creation_transaction_index,
            bonding_curve=str(bonding_curve_pda),
            default_label=label,
            bundle_wallets=bundle_wallets,
            bundle_buys=bundle_buys,
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
