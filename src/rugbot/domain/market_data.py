"""Data-based market history (on-chain first, honest unavailable flags)."""

# ruff: noqa: PLC0415, C901, PLR0911, PLR0912, PLR0915, S110, BLE001, S310, I001, B007, B905, PLW0108, RUF059, F841, PLR5501, PLR2004
from __future__ import annotations

import base64
import json
import os
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

from rugbot.utils.logger import get_logger

logger = get_logger(__name__)

PUMP_PROGRAM_ID_STR: str = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
_SOLSCAN_MAX_PAGES: int = 100
_SOLSCAN_PAGE_LIMIT: int = 10
_ALCHEMY_FALLBACK_RPC: str = (
    "https://solana-mainnet.g.alchemy.com/v2/alch_2O5Is1Oqa0hgCkxXA3w7T"
)
_TRADE_EVENT_DISCRIMINATOR = bytes([189, 219, 127, 211, 78, 230, 97, 238])
_EARLY_SIG_LIMIT = 1000
_EARLY_SIG_PAGES = 5
_EARLY_TX_FETCH_LIMIT = 120
_EARLY_TX_BATCH_SLEEP = 0.35

_DEFAULT_DB_CANDIDATES = [
    Path(".state/discover/rugbot.db"),
    Path(".state/rugbot.db"),
]


@dataclass(frozen=True, slots=True)
class TokenMarketHistory:
    """Honest market history for one mint."""

    mint: str
    entry_mc_quote_lamports: int | None
    entry_mc_usd: float | None
    entry_price_ppm: int | None
    entry_slot: int | None
    peak_mc_quote_lamports: int | None
    peak_price_ppm: int | None
    peak_slot: int | None
    floor_mc_quote_lamports: int | None
    floor_price_ppm: int | None
    trajectory: tuple[tuple[int, int, int], ...]
    migrated: bool
    total_supply_base_units: int | None
    base_decimals: int | None
    quote_decimals: int | None
    sources: dict[str, str]
    unavailable: tuple[str, ...]
    as_of_slot: int | None
    ath_unavailable: bool = False


def _resolve_db_path(db_path: str | Path | None) -> Path | None:
    if db_path is not None:
        p = Path(db_path)
        return p if p.exists() else None
    for c in _DEFAULT_DB_CANDIDATES:
        if c.exists():
            return c
    # also try explicit
    explicit = Path(".state/discover/rugbot.db")
    if explicit.exists():
        return explicit
    return None


def _recalc_price(row: dict) -> int | None:
    q = row.get("quote_amount_base_units")
    b = row.get("base_amount")
    ppm = row.get("price_ppm")
    if isinstance(q, int) and isinstance(b, int) and b and b > 0 and q > 0:
        return q * 1_000_000 // b
    if isinstance(ppm, int) and ppm and ppm > 0:
        return ppm
    return None


def _derive_bonding_curve(mint: str) -> str | None:
    try:
        from solders.pubkey import Pubkey  # type: ignore

        from rugbot.intelligence.token_resolver import PUMP_PROGRAM_ID as _PUMP_PID  # type: ignore

        mpk = Pubkey.from_string(mint)
        pda, _ = Pubkey.find_program_address([b"bonding-curve", bytes(mpk)], _PUMP_PID)
        return str(pda)
    except Exception:
        return None


def _solscan_api_key() -> str | None:
    try:
        from rugbot.runtime.config import resolve_dotenv  # type: ignore

        resolve_dotenv()
    except Exception:
        pass
    key = os.getenv("SOLSCAN_API_KEY")
    if key and key.strip():
        return key.strip()
    return None


def _parse_solscan_trade_row(row: dict, mint: str) -> dict | None:
    """Extract one Pump trade from a Solscan enhanced transaction row."""
    try:
        slot = row.get("slot")
        if type(slot) is not int or slot < 0:
            return None
        block_time = row.get("blockTime")
        if block_time is None:
            block_time = row.get("block_time")
        # signature
        sig: str | None = None
        tx_val = row.get("transaction")
        sigs: object = None
        if isinstance(tx_val, dict):
            sigs = tx_val.get("signatures")
        if sigs is None:
            sigs = row.get("signatures")
        if isinstance(sigs, list) and sigs and isinstance(sigs[0], str):
            sig = sigs[0]
        else:
            # solscan enhanced may have signature top-level
            s2 = row.get("signature") or row.get("txHash") or row.get("tx_hash")
            if isinstance(s2, str) and s2:
                sig = s2
        if not sig:
            return None
        # tx index
        tx_index = row.get("transactionIndex")
        if tx_index is None:
            tx_index = row.get("txIndex")
        if tx_index is not None and type(tx_index) is not int:
            tx_index = None
        # wallet (signer) = first accountKeys
        wallet: str | None = None
        account_keys: list[str] = []
        msg = None
        if isinstance(tx_val, dict):
            msg = tx_val.get("message")
            if isinstance(msg, dict):
                ak = msg.get("accountKeys")
                if isinstance(ak, list):
                    account_keys = [str(x) for x in ak if isinstance(x, str)]
                    if account_keys:
                        wallet = account_keys[0]
        if wallet is None:
            ak2 = row.get("accountKeys")
            if isinstance(ak2, list) and ak2 and isinstance(ak2[0], str):
                wallet = str(ak2[0])
                account_keys = [str(x) for x in ak2 if isinstance(x, str)]
        # meta
        meta = row.get("meta")
        if not isinstance(meta, dict) and isinstance(tx_val, dict):
            meta = tx_val.get("meta")
        if not isinstance(meta, dict):
            meta = row.get("meta") if isinstance(row.get("meta"), dict) else {}
            if not isinstance(meta, dict):
                meta = {}
        # side via logs if available
        side: str | None = None
        logs = meta.get("logMessages") if isinstance(meta, dict) else None
        if isinstance(logs, list):
            joined = " ".join(str(x) for x in logs if isinstance(x, str))
            low = joined.lower()
            has_buy = "buy" in low
            has_sell = "sell" in low
            if has_buy and not has_sell:
                side = "buy"
            elif has_sell and not has_buy:
                side = "sell"
        # Try token balance delta for mint
        pre_tok = meta.get("preTokenBalances") if isinstance(meta, dict) else None
        post_tok = meta.get("postTokenBalances") if isinstance(meta, dict) else None
        # solscan may use snake_case
        if pre_tok is None and isinstance(meta, dict):
            pre_tok = meta.get("pre_token_balances")
        if post_tok is None and isinstance(meta, dict):
            post_tok = meta.get("post_token_balances")
        # fallback to top-level balances
        if pre_tok is None:
            pre_tok = row.get("preTokenBalances") or row.get("pre_token_balances")
        if post_tok is None:
            post_tok = row.get("postTokenBalances") or row.get("post_token_balances")
        base_amount: int | None = None
        # parse token amounts: structure varies
        if isinstance(pre_tok, list) and isinstance(post_tok, list):

            def _amount(entry: object) -> int | None:
                if not isinstance(entry, dict):
                    return None
                # amount may be string under amount, uiTokenAmount.amount, or tokenAmount
                for k in ("amount", "tokenAmount"):
                    v = entry.get(k)
                    if isinstance(v, str) and v.lstrip("-").isdigit():
                        try:
                            return int(v)
                        except ValueError:
                            continue
                    if isinstance(v, int):
                        return v
                ui = entry.get("uiTokenAmount")
                if isinstance(ui, dict):
                    a2 = ui.get("amount")
                    if isinstance(a2, str) and a2.lstrip("-").isdigit():
                        try:
                            return int(a2)
                        except ValueError:
                            pass
                    if isinstance(a2, int):
                        return int(a2)
                return None

            def _mint_of(e: dict) -> str | None:
                m = e.get("mint")
                return str(m) if isinstance(m, str) else None

            # build maps by (mint, owner/index)
            pre_map: dict[tuple[str, str], int] = {}
            for e in pre_tok:
                if not isinstance(e, dict):
                    continue
                m = _mint_of(e)
                if m != mint:
                    continue
                # key by owner or accountIndex
                owner = e.get("owner")
                if not isinstance(owner, str):
                    owner = str(e.get("accountIndex", ""))
                amt = _amount(e)
                if amt is None:
                    continue
                pre_map[(m or "", owner)] = amt
            post_map: dict[tuple[str, str], int] = {}
            for e in post_tok:
                if not isinstance(e, dict):
                    continue
                m = _mint_of(e)
                if m != mint:
                    continue
                owner = e.get("owner")
                if not isinstance(owner, str):
                    owner = str(e.get("accountIndex", ""))
                amt = _amount(e)
                if amt is None:
                    continue
                post_map[(m or "", owner)] = amt
            # compute delta: try per-owner then aggregate
            deltas: list[int] = []
            all_keys = set(pre_map.keys()) | set(post_map.keys())
            for k in all_keys:
                d = post_map.get(k, 0) - pre_map.get(k, 0)
                if d != 0:
                    deltas.append(d)
            if deltas:
                # For user trade, pick largest absolute delta (user account)
                # If wallet known, prefer owner's delta
                if wallet is not None:
                    for k, d in zip(
                        all_keys,
                        [post_map.get(k, 0) - pre_map.get(k, 0) for k in all_keys],
                    ):
                        if k[1] == wallet and d != 0:
                            base_amount = abs(d)
                            if d > 0 and side is None:
                                side = "buy"
                            elif d < 0 and side is None:
                                side = "sell"
                            break
                if base_amount is None:
                    # pick largest
                    best = max(deltas, key=lambda x: abs(x))
                    base_amount = abs(best)
                    if side is None:
                        side = "buy" if best > 0 else "sell"
            else:
                # no per-owner change? try aggregate
                pre_sum = sum(pre_map.values())
                post_sum = sum(post_map.values())
                d = post_sum - pre_sum
                if d != 0:
                    base_amount = abs(d)
                    if side is None:
                        side = "buy" if d > 0 else "sell"
        # fallback: direct fields in row (some Solscan shapes)
        if base_amount is None:
            for k in ("baseAmount", "base_amount", "token_amount", "amount"):
                v = row.get(k)
                if isinstance(v, int) and v > 0:
                    base_amount = v
                    break
                if isinstance(v, str) and v.isdigit():
                    base_amount = int(v)
                    break
        if base_amount is None or base_amount <= 0:
            return None
        # quote amount via SOL balances delta for wallet
        quote_amount: int | None = None
        pre_bal = meta.get("preBalances") if isinstance(meta, dict) else None
        post_bal = meta.get("postBalances") if isinstance(meta, dict) else None
        if pre_bal is None:
            pre_bal = row.get("preBalances") or row.get("pre_balances")
        if post_bal is None:
            post_bal = row.get("postBalances") or row.get("post_balances")
        if isinstance(pre_bal, list) and isinstance(post_bal, list) and account_keys:
            try:
                idx = account_keys.index(wallet) if wallet in account_keys else 0
                if 0 <= idx < len(pre_bal) and 0 <= idx < len(post_bal):
                    pb = (
                        int(pre_bal[idx])
                        if isinstance(pre_bal[idx], int)
                        else int(str(pre_bal[idx]))
                    )
                    ab = (
                        int(post_bal[idx])
                        if isinstance(post_bal[idx], int)
                        else int(str(post_bal[idx]))
                    )
                    diff = pb - ab  # buy: positive pay
                    if diff != 0:
                        quote_amount = abs(diff)
                        # infer side from SOL diff if still unknown
                        if side is None:
                            # SOL down => buy
                            side = "buy" if (pb - ab) > 0 else "sell"
            except Exception:
                pass
        if quote_amount is None:
            # try explicit quote fields
            for k in (
                "quoteAmount",
                "quote_amount_base_units",
                "quote_amount",
                "solAmount",
                "lamports",
            ):
                v = row.get(k)
                if isinstance(v, int) and v > 0:
                    quote_amount = v
                    break
                if isinstance(v, str) and v.isdigit():
                    quote_amount = int(v)
                    break
            if isinstance(meta, dict):
                for k in ("quoteAmount", "quote_amount"):
                    v2 = meta.get(k)
                    if isinstance(v2, int) and v2 > 0:
                        quote_amount = v2
                        break
        if quote_amount is None or quote_amount <= 0:
            return None
        if side not in ("buy", "sell"):
            # infer from token delta sign already or default buy
            side = side or "buy"
        price_ppm = quote_amount * 1_000_000 // base_amount if base_amount else None
        if price_ppm is None or price_ppm <= 0:
            return None
        return {
            "slot": slot,
            "tx_index": tx_index,
            "signature": sig,
            "wallet": wallet,
            "side": side,
            "quote_amount": int(quote_amount),
            "base_amount": int(base_amount),
            "price_ppm": int(price_ppm),
            "block_time": int(block_time) if isinstance(block_time, int) else None,
        }
    except Exception:
        return None


def _resolve_rpc_url(explicit: str | None) -> str | None:
    if explicit and explicit.strip():
        return explicit.strip()
    try:
        from rugbot.runtime.config import resolve_dotenv  # type: ignore

        resolve_dotenv()
    except Exception:
        pass
    # priority: explicit env vars then fallbacks then Alchemy
    for key in ("SOLANA_RPC_HTTP", "SOLANA_NODE_RPC_ENDPOINT"):
        v = os.getenv(key)
        if v and v.strip():
            return v.strip()
    fallbacks = os.getenv("SOLANA_RPC_HTTP_FALLBACKS")
    if fallbacks:
        for part in fallbacks.split(","):
            p = part.strip()
            if p:
                return p
    return _ALCHEMY_FALLBACK_RPC


def _fetch_solscan_trades(mint: str, db_path: str | Path) -> list[dict] | None:
    """Fetch Pump trades via Solscan paginated enhanced_transactions and persist to DB.

    Returns list of parsed trades sorted by slot, or None if unavailable.
    """
    api_key = _solscan_api_key()
    if not api_key:
        logger.debug("solscan unavailable: no api key")
        return None
    bonding_curve = _derive_bonding_curve(mint)
    if not bonding_curve:
        logger.debug("solscan unavailable: bonding_curve derive failed for %s", mint)
        return None
    try:
        from rugbot.integrations.solscan import SolscanClient  # type: ignore
    except Exception as exc:
        logger.debug("solscan import failed: %s", exc)
        return None
    try:
        client = SolscanClient(api_key)
    except Exception as exc:
        logger.debug("solscan client init failed: %s", exc)
        return None
    collected: list[dict] = []
    cursor: str | None = None
    truncated = False
    for _ in range(_SOLSCAN_MAX_PAGES):
        try:
            page = client.enhanced_transactions(
                bonding_curve,
                program=PUMP_PROGRAM_ID_STR,
                cursor=cursor,
                limit=_SOLSCAN_PAGE_LIMIT,
            )
        except Exception as exc:
            # 429 or transient — backoff and stop gracefully
            logger.debug("solscan page fetch failed for %s: %s", mint, exc)
            if "429" in str(exc):
                time.sleep(1.0)
            break
        for tx_row in page.transactions:
            if not isinstance(tx_row, dict):
                continue
            parsed = _parse_solscan_trade_row(tx_row, mint)
            if parsed is not None:
                collected.append(parsed)
        cursor = page.cursor
        if cursor is None:
            break
        if len(collected) >= _SOLSCAN_MAX_PAGES * _SOLSCAN_PAGE_LIMIT:
            truncated = True
            break
        # backoff to respect free-tier rate limits
        time.sleep(0.25)
    if truncated:
        logger.debug("solscan truncated at %s pages for %s", _SOLSCAN_MAX_PAGES, mint)
    if not collected:
        return None
    collected.sort(
        key=lambda x: (int(x["slot"]), int(x["tx_index"] or 0), str(x["signature"]))
    )
    # persist to discover_trades for reuse
    try:
        from rugbot.discover.store import ensure_discover_schema, upsert_trade  # type: ignore
        from rugbot.storage.database import DatabaseManager  # type: ignore

        # ensure db_path exists (create if needed)
        db_file_path = Path(db_path)
        # if db_path is candidate dir path, use it directly
        if db_file_path.suffix != ".db":
            db_file_path = Path(".state/discover/rugbot.db")
        db_file_path.parent.mkdir(parents=True, exist_ok=True)
        dbm = DatabaseManager(str(db_file_path))
        ensure_discover_schema(dbm)
        for idx, tr in enumerate(collected):
            try:
                upsert_trade(
                    dbm,
                    mint=mint,
                    signature=str(tr["signature"]),
                    event_index=0,
                    slot=int(tr["slot"]),
                    tx_index=int(tr["tx_index"])
                    if tr["tx_index"] is not None
                    else None,
                    wallet=str(tr["wallet"]) if tr.get("wallet") else None,
                    side=str(tr["side"]),
                    quote_amount_base_units=int(tr["quote_amount"]),
                    quote_mint="So11111111111111111111111111111111111111112",
                    base_amount=int(tr["base_amount"]),
                    price_ppm=int(tr["price_ppm"]),
                    signers_json=json.dumps([tr["wallet"]] if tr.get("wallet") else []),
                    raw_json=json.dumps({"solscan": True, "slot": tr["slot"]}),
                )
            except Exception as exc:
                logger.debug("solscan upsert failed for %s: %s", mint, exc)
        dbm.close()
    except Exception as exc:
        logger.debug("solscan persist failed for %s: %s", mint, exc)
    return collected


def _rpc_json_call(
    rpc_url: str, method: str, params: list[object], timeout: int = 12
) -> dict | None:
    import json as _json
    import urllib.request

    payload = _json.dumps(
        {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    ).encode()
    candidates = [rpc_url]
    fallbacks_raw = os.getenv("SOLANA_RPC_HTTP_FALLBACKS") or ""
    for part in fallbacks_raw.split(","):
        p = part.strip()
        if p and p not in candidates:
            candidates.append(p)
    if _ALCHEMY_FALLBACK_RPC not in candidates:
        candidates.append(_ALCHEMY_FALLBACK_RPC)
    for cand in candidates:
        try:
            req = urllib.request.Request(
                cand, data=payload, headers={"Content-Type": "application/json"}
            )
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return _json.loads(resp.read().decode())
        except Exception as exc:
            logger.debug("rpc %s failed %s: %s", method, cand[:40], exc)
            continue
    return None


def _decode_trade_event_price(payload: bytes) -> tuple[int, int, bool] | None:
    """Decode sol_amount, token_amount, is_buy from a TradeEvent payload. Returns None if invalid."""
    import struct

    if not payload.startswith(_TRADE_EVENT_DISCRIMINATOR):
        return None
    # layout: 8 discriminator, 32 mint, 8 sol_amount, 8 token_amount, 1 is_buy
    if len(payload) < 8 + 32 + 8 + 8 + 1:
        return None
    try:
        sol_amount = struct.unpack_from("<Q", payload, 8 + 32)[0]
        token_amount = struct.unpack_from("<Q", payload, 8 + 32 + 8)[0]
        is_buy = bool(payload[8 + 32 + 8 + 8])
    except Exception:
        return None
    if sol_amount <= 0 or token_amount <= 0:
        return None
    return sol_amount, token_amount, is_buy


def _fetch_early_onchain_trades(
    mint: str,
    bonding_curve: str,
    existing_signatures: set[str],
    rpc_url: str,
) -> list[dict] | None:
    """Paginate getSignaturesForAddress to oldest, then decode earliest Pump trades.

    Bounded: ~5 pages * 1000 sigs, then decode up to 120 earliest unknown sigs.
    Returns list of parsed trade dicts or None if unavailable/throttled.
    """
    # paginate signatures oldest
    sig_entries: list[dict] = []
    before_sig: str | None = None
    for _ in range(_EARLY_SIG_PAGES):
        params: list[object]
        if before_sig is None:
            params = [
                bonding_curve,
                {"limit": _EARLY_SIG_LIMIT, "commitment": "finalized"},
            ]
        else:
            params = [
                bonding_curve,
                {
                    "limit": _EARLY_SIG_LIMIT,
                    "commitment": "finalized",
                    "before": before_sig,
                },
            ]
        resp = _rpc_json_call(rpc_url, "getSignaturesForAddress", params, timeout=15)
        if resp is None:
            if not sig_entries:
                return None
            break
        result = resp.get("result") if isinstance(resp, dict) else None
        if not isinstance(result, list):
            if not sig_entries:
                return None
            break
        if not result:
            break
        for item in result:
            if isinstance(item, dict) and isinstance(item.get("signature"), str):
                sig_entries.append(item)
        if len(result) < _EARLY_SIG_LIMIT:
            break
        last = result[-1]
        if isinstance(last, dict) and isinstance(last.get("signature"), str):
            before_sig = str(last["signature"])
        else:
            break
        time.sleep(0.4)
    if not sig_entries:
        return None
    # oldest first
    sig_entries.reverse()
    # pick earliest not already known
    to_fetch: list[dict] = []
    for e in sig_entries:
        sig = str(e.get("signature"))
        if sig not in existing_signatures:
            to_fetch.append(e)
        if len(to_fetch) >= _EARLY_TX_FETCH_LIMIT:
            break
    if not to_fetch:
        return []
    early_trades: list[dict] = []
    throttle_hits = 0
    for ent in to_fetch:
        sig = str(ent.get("signature"))
        slot_hint = ent.get("slot") if isinstance(ent.get("slot"), int) else None
        # getTransaction
        tx_resp = _rpc_json_call(
            rpc_url,
            "getTransaction",
            [
                sig,
                {
                    "commitment": "finalized",
                    "encoding": "jsonParsed",
                    "maxSupportedTransactionVersion": 0,
                },
            ],
            timeout=12,
        )
        if tx_resp is None:
            throttle_hits += 1
            if throttle_hits >= 3:
                break
            time.sleep(_EARLY_TX_BATCH_SLEEP)
            continue
        result = tx_resp.get("result") if isinstance(tx_resp, dict) else None
        if not isinstance(result, dict):
            time.sleep(0.2)
            continue
        slot = result.get("slot") if isinstance(result.get("slot"), int) else slot_hint
        if not isinstance(slot, int) or slot < 0:
            time.sleep(0.2)
            continue
        meta = result.get("meta")
        tx_val = result.get("transaction")
        if not isinstance(meta, dict):
            time.sleep(0.2)
            continue
        if meta.get("err") is not None:
            time.sleep(0.2)
            continue
        logs = meta.get("logMessages")
        if not isinstance(logs, list):
            time.sleep(0.2)
            continue
        tx_index = ent.get("blockTime")  # not correct; use None
        # decode each TradeEvent in logs
        for msg in logs:
            if not isinstance(msg, str) or not msg.startswith("Program data: "):
                continue
            enc = msg.removeprefix("Program data: ")
            try:
                payload = base64.b64decode(enc, validate=True)
            except Exception:
                continue
            decoded = _decode_trade_event_price(payload)
            if decoded is None:
                continue
            sol_amount, token_amount, is_buy = decoded
            # need to verify mint matches: peek mint pubkey at offset 8
            try:
                import base58 as _b58

                mint_bytes = payload[8 : 8 + 32]
                evt_mint = _b58.b58encode(mint_bytes).decode("ascii")
            except Exception:
                continue
            if evt_mint != mint:
                continue
            price_ppm = sol_amount * 1_000_000 // token_amount if token_amount else 0
            if price_ppm <= 0:
                continue
            early_trades.append(
                {
                    "slot": int(slot),
                    "tx_index": None,
                    "signature": sig,
                    "wallet": None,
                    "side": "buy" if is_buy else "sell",
                    "quote_amount": int(sol_amount),
                    "base_amount": int(token_amount),
                    "price_ppm": int(price_ppm),
                }
            )
        time.sleep(_EARLY_TX_BATCH_SLEEP)
    return early_trades


def _fetch_supply_via_rpc(
    mint: str, rpc_url: str | None
) -> tuple[int | None, int | None, int | None, bool | None]:
    """Return (supply, base_decimals, quote_decimals, complete). Best-effort."""
    resolved = _resolve_rpc_url(rpc_url)
    if not resolved:
        return None, None, None, None
    candidates = [resolved]
    # also try fallbacks if primary fails
    fallbacks_raw = os.getenv("SOLANA_RPC_HTTP_FALLBACKS") or ""
    for part in fallbacks_raw.split(","):
        p = part.strip()
        if p and p not in candidates:
            candidates.append(p)
    if _ALCHEMY_FALLBACK_RPC not in candidates:
        candidates.append(_ALCHEMY_FALLBACK_RPC)
    try:
        from solders.pubkey import Pubkey  # type: ignore

        from rugbot.ingest.pump.bonding_curve_account import (
            PUMP_BONDING_CURVE_LAYOUT_ARTIFACT_VERSION,
        )

        from rugbot.execution.transaction_builder import (
            PUMP_PROGRAM_ID as _PUMP_PID_STR,
        )  # type: ignore

        _PUMP_PID = Pubkey.from_string(_PUMP_PID_STR)
        mint_pk = Pubkey.from_string(mint)
        bonding_curve_pda, _ = Pubkey.find_program_address(
            [b"bonding-curve", bytes(mint_pk)], _PUMP_PID
        )
        # raw rpc call with fallback candidates
        import json as _json
        import urllib.request

        payload = _json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "getAccountInfo",
                "params": [
                    str(bonding_curve_pda),
                    {"commitment": "finalized", "encoding": "base64"},
                ],
            }
        ).encode()
        data: dict | None = None
        last_exc: Exception | None = None
        for cand_url in candidates:
            try:
                req = urllib.request.Request(
                    cand_url, data=payload, headers={"Content-Type": "application/json"}
                )
                with urllib.request.urlopen(req, timeout=8) as resp:
                    data = _json.loads(resp.read().decode())
                break
            except Exception as exc:
                last_exc = exc
                logger.debug("supply rpc candidate failed %s: %s", cand_url[:40], exc)
                continue
        if data is None:
            logger.debug(
                "supply via rpc all candidates failed for %s: %s", mint, last_exc
            )
            return None, None, None, None
        result = data.get("result", {}) if isinstance(data, dict) else {}
        val = result.get("value") if isinstance(result, dict) else None
        if not isinstance(val, dict) or not val.get("data"):
            return None, None, None, None
        enc = val["data"]
        if not isinstance(enc, list) or len(enc) < 1:
            return None, None, None, None
        raw = base64.b64decode(enc[0])
        ctx_slot = (
            result.get("context", {}).get("slot", 0)
            if isinstance(result.get("context"), dict)
            else 0
        )
        # Prefer direct raw decode (avoid version_registry drift); layout: supply at 40, complete at 48
        if len(raw) >= 48:
            import struct

            try:
                supply = struct.unpack_from("<Q", raw, 40)[0]
                complete = bool(raw[48]) if len(raw) > 48 else None
                if supply and supply > 0:
                    return supply, 6, 9, complete
            except Exception:
                pass
        # fallback try typed decoder
        try:
            from rugbot.ingest.pump.bonding_curve_account import (
                PumpBondingCurveAccountState,
                PumpBondingCurveDecodeRequest,
                decode_pump_bonding_curve_account as _dec,
            )
            from rugbot.domain.decisions import AbstainResult

            state = PumpBondingCurveAccountState(
                as_of_slot=int(ctx_slot) if isinstance(ctx_slot, int) else 0,
                account_pubkey=str(bonding_curve_pda),
                owner_program_id=str(val.get("owner", "")),
                raw_account_data=raw,
                source_artifact_version="solana-rpc-finalized-getAccountInfo",
                layout_artifact_version=PUMP_BONDING_CURVE_LAYOUT_ARTIFACT_VERSION,
            )
            # construct minimal proto compatible with current registry shape — try legacy fallback
            proto = None
            snap = _dec(
                PumpBondingCurveDecodeRequest(
                    account_state=state,
                    protocol_snapshot=proto,  # type: ignore
                    idl_hash="b90bc471327f671449271d5d1d42354d1fae6f5a06502f5834459a3108138e49",
                    base_decimals=6,
                    quote_decimals=9,
                    base_mint=mint,
                    quote_mint="So11111111111111111111111111111111111111112",
                )
            )
            if not isinstance(snap, AbstainResult):
                return int(snap.token_total_supply), 6, 9, bool(snap.complete)
        except Exception:
            pass
        return None, None, None, None
    except Exception as exc:
        logger.debug("supply via rpc failed for %s: %s", mint, exc)
        return None, None, None, None


def build_token_market_history(
    mint: str,
    *,
    db_path: str | Path = ".state/discover/rugbot.db",
    rpc_url: str | None = None,
    sol_price_usd: float | None = None,
) -> TokenMarketHistory:
    """Build data-based history. Never returns 0 defaults; None+unavailable instead."""
    sources: dict[str, str] = {}
    unavailable: list[str] = []
    db_file = _resolve_db_path(db_path)
    # try load discover_trades
    rows: list[dict] = []
    if db_file is not None and db_file.exists():
        try:
            conn = sqlite3.connect(str(db_file))
            conn.row_factory = sqlite3.Row
            cur = conn.cursor()
            cur.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='discover_trades'"
            )
            if cur.fetchone():
                cur.execute(
                    "SELECT mint, signature, event_index, slot, tx_index, wallet, side, quote_amount_base_units, quote_mint, base_amount, price_ppm FROM discover_trades WHERE mint=? ORDER BY slot ASC, tx_index ASC, event_index ASC",
                    (mint,),
                )
                rows = [dict(r) for r in cur.fetchall()]
                if rows:
                    sources["trajectory"] = "discover_trades on-chain"
                else:
                    unavailable.append("no discover_trades rows")
            else:
                unavailable.append("discover_trades table missing")
            conn.close()
        except Exception as exc:
            unavailable.append(f"discover_trades query failed: {exc}")
            logger.debug("db query failed: %s", exc)
    else:
        unavailable.append("discover db not found")

    # on-chain path
    if rows:
        # supply
        supply, base_dec, quote_dec, complete = _fetch_supply_via_rpc(mint, rpc_url)
        if supply is None:
            unavailable.append("total_supply: bonding_curve fetch failed")
            base_dec = base_dec or 6
            quote_dec = quote_dec or 9
        migrated = bool(complete) if complete is not None else False
        traj: list[tuple[int, int, int]] = []
        prices: list[int] = []
        price_by_slot: list[tuple[int, int]] = []
        for r in rows:
            ppm = _recalc_price(r)
            if ppm is None or ppm <= 0:
                continue
            slot = int(r["slot"]) if r["slot"] is not None else 0
            mc = 0
            if supply is not None:
                mc = ppm * supply // 1_000_000
            prices.append(ppm)
            price_by_slot.append((slot, ppm))
            traj.append((slot, ppm, mc))

        if not traj:
            unavailable.append("no valid price in discover_trades")
            return TokenMarketHistory(
                mint=mint,
                entry_mc_quote_lamports=None,
                entry_mc_usd=None,
                entry_price_ppm=None,
                entry_slot=None,
                peak_mc_quote_lamports=None,
                peak_price_ppm=None,
                peak_slot=None,
                floor_mc_quote_lamports=None,
                floor_price_ppm=None,
                trajectory=(),
                migrated=migrated,
                total_supply_base_units=supply,
                base_decimals=base_dec,
                quote_decimals=quote_dec,
                sources=dict(sources),
                unavailable=tuple(unavailable),
                as_of_slot=traj[-1][0] if traj else None,
                ath_unavailable=True,
            )
        # try early on-chain complement for discover_trades path (bounded, honest unavailable on throttle)
        try:
            _bc2 = _derive_bonding_curve(mint)
            _rpc2 = _resolve_rpc_url(rpc_url)
            if _bc2 and _rpc2:
                _existing_sigs2 = {
                    str(r.get("signature")) for r in rows if r.get("signature")
                }
                _early2 = _fetch_early_onchain_trades(
                    mint, _bc2, _existing_sigs2, _rpc2
                )
                if _early2:
                    _existing_set2 = {str(r.get("signature")) for r in rows}
                    _added2 = 0
                    for tr in _early2:
                        sig = str(tr.get("signature"))
                        if sig in _existing_set2:
                            continue
                        rows.append(
                            {
                                "mint": mint,
                                "signature": sig,
                                "event_index": 0,
                                "slot": tr["slot"],
                                "tx_index": tr.get("tx_index"),
                                "wallet": tr.get("wallet"),
                                "side": tr["side"],
                                "quote_amount_base_units": tr["quote_amount"],
                                "quote_mint": "So11111111111111111111111111111111111111112",
                                "base_amount": tr["base_amount"],
                                "price_ppm": tr["price_ppm"],
                            }
                        )
                        _existing_set2.add(sig)
                        _added2 += 1
                    if _added2:
                        rows.sort(
                            key=lambda x: (
                                int(x["slot"]),
                                int(x["tx_index"] or 0),
                                str(x["signature"]),
                            )
                        )
                        # rebuild traj with early trades
                        traj = []
                        for r in rows:
                            ppm = _recalc_price(r)
                            if ppm is None or ppm <= 0:
                                continue
                            slot = int(r["slot"]) if r["slot"] is not None else 0
                            mc = ppm * supply // 1_000_000 if supply is not None else 0
                            traj.append((slot, ppm, mc))
                        sources["trajectory"] = "discover_trades+early-onchain on-chain"
                elif _early2 is None:
                    unavailable.append(
                        "early on-chain unavailable (rpc throttle/empty)"
                    )
        except Exception as exc:
            logger.debug("early on-chain complement failed for %s: %s", mint, exc)
        entry_slot, entry_ppm, entry_mc = traj[0]
        # peak
        peak_idx = max(range(len(traj)), key=lambda i: traj[i][1])
        peak_slot, peak_ppm, peak_mc = traj[peak_idx]
        floor_slot, floor_ppm, floor_mc = traj[-1]
        # migrated flag + note about post-migration
        if migrated:
            # we did not merge PumpSwap trades in V1
            if "post-migration trades not collected" not in unavailable:
                unavailable.append("post-migration trades not collected")
            sources["migrated"] = "bonding_curve complete==True"
        # if supply missing, mc fields unavailable
        if supply is None:
            entry_mc_val = None
            peak_mc_val = None
            floor_mc_val = None
            if "market_cap unavailable: total_supply missing" not in unavailable:
                unavailable.append("market_cap unavailable: total_supply missing")
        else:
            entry_mc_val = entry_mc
            peak_mc_val = peak_mc
            floor_mc_val = floor_mc
            sources["market_cap"] = "price_ppm * total_supply // 1e6"

        # sources for price
        sources["price_ppm"] = "quote_amount_base_units*1e6//base_amount (executed)"

        entry_usd = (
            (entry_mc_val / 1e9 * sol_price_usd)
            if (entry_mc_val is not None and sol_price_usd)
            else None
        )

        return TokenMarketHistory(
            mint=mint,
            entry_mc_quote_lamports=entry_mc_val,
            entry_mc_usd=entry_usd,
            entry_price_ppm=entry_ppm,
            entry_slot=entry_slot,
            peak_mc_quote_lamports=peak_mc_val,
            peak_price_ppm=peak_ppm,
            peak_slot=peak_slot,
            floor_mc_quote_lamports=floor_mc_val,
            floor_price_ppm=floor_ppm,
            trajectory=tuple(traj),
            migrated=migrated,
            total_supply_base_units=supply,
            base_decimals=base_dec,
            quote_decimals=quote_dec,
            sources=dict(sources),
            unavailable=tuple(unavailable),
            as_of_slot=floor_slot,
            ath_unavailable=False,
        )

    # Try Solscan fast path before off-chain fallback
    if not rows:
        solscan_trades = _fetch_solscan_trades(mint, db_path)
        if solscan_trades:
            sources["trajectory"] = "solscan enhanced_transactions on-chain"
            # reload from DB after persist
            rows = []
            db_file2 = _resolve_db_path(db_path)
            # fallback to direct path if not yet resolved
            if db_file2 is None:
                cand = Path(db_path)
                if cand.exists():
                    db_file2 = cand
            if db_file2 is not None and db_file2.exists():
                try:
                    conn2 = sqlite3.connect(str(db_file2))
                    conn2.row_factory = sqlite3.Row
                    cur2 = conn2.cursor()
                    cur2.execute(
                        "SELECT mint, signature, event_index, slot, tx_index, wallet, side, quote_amount_base_units, quote_mint, base_amount, price_ppm FROM discover_trades WHERE mint=? ORDER BY slot ASC, tx_index ASC, event_index ASC",
                        (mint,),
                    )
                    rows = [dict(r) for r in cur2.fetchall()]
                    conn2.close()
                    if rows:
                        sources["trajectory"] = "solscan enhanced_transactions on-chain"
                        # remove unavailable marker for empty rows
                        unavailable = [
                            u
                            for u in unavailable
                            if "no discover_trades" not in u
                            and "discover db not found" not in u
                        ]
                except Exception as exc:
                    logger.debug("solscan reload failed: %s", exc)
            # if DB reload failed but we have parsed trades, build rows from memory
            if not rows:
                for tr in solscan_trades:
                    rows.append(
                        {
                            "mint": mint,
                            "signature": tr["signature"],
                            "event_index": 0,
                            "slot": tr["slot"],
                            "tx_index": tr.get("tx_index"),
                            "wallet": tr.get("wallet"),
                            "side": tr["side"],
                            "quote_amount_base_units": tr["quote_amount"],
                            "quote_mint": "So11111111111111111111111111111111111111112",
                            "base_amount": tr["base_amount"],
                            "price_ppm": tr["price_ppm"],
                        }
                    )
                unavailable = [
                    u
                    for u in unavailable
                    if "no discover_trades" not in u
                    and "discover db not found" not in u
                ]
                sources["trajectory"] = "solscan enhanced_transactions on-chain"
            # complement early on-chain peak if Solscan truncated (always bounded attempt)
            if rows:
                try:
                    _bc_for_early = _derive_bonding_curve(mint)
                    _rpc_for_early = _resolve_rpc_url(rpc_url)
                    if _bc_for_early and _rpc_for_early:
                        _existing_sigs = {
                            str(r.get("signature")) for r in rows if r.get("signature")
                        }
                        _early = _fetch_early_onchain_trades(
                            mint, _bc_for_early, _existing_sigs, _rpc_for_early
                        )
                        if _early:
                            # merge dedup by signature
                            _existing_set = {str(r.get("signature")) for r in rows}
                            _added = 0
                            for tr in _early:
                                sig = str(tr.get("signature"))
                                if sig in _existing_set:
                                    continue
                                rows.append(
                                    {
                                        "mint": mint,
                                        "signature": sig,
                                        "event_index": 0,
                                        "slot": tr["slot"],
                                        "tx_index": tr.get("tx_index"),
                                        "wallet": tr.get("wallet"),
                                        "side": tr["side"],
                                        "quote_amount_base_units": tr["quote_amount"],
                                        "quote_mint": "So11111111111111111111111111111111111111112",
                                        "base_amount": tr["base_amount"],
                                        "price_ppm": tr["price_ppm"],
                                    }
                                )
                                _existing_set.add(sig)
                                _added += 1
                            if _added:
                                rows.sort(
                                    key=lambda x: (
                                        int(x["slot"]),
                                        int(x["tx_index"] or 0),
                                        str(x["signature"]),
                                    )
                                )
                                sources["trajectory"] = "solscan+early-onchain on-chain"
                                # persist early trades too
                                try:
                                    from rugbot.discover.store import (
                                        ensure_discover_schema as _eds2,
                                    )
                                    from rugbot.discover.store import (
                                        upsert_trade as _ut2,
                                    )
                                    from rugbot.storage.database import (
                                        DatabaseManager as _DBM2,
                                    )

                                    _dbp2 = (
                                        Path(db_path)
                                        if Path(db_path).suffix == ".db"
                                        else Path(".state/discover/rugbot.db")
                                    )
                                    _dbp2.parent.mkdir(parents=True, exist_ok=True)
                                    _dbm2 = _DBM2(str(_dbp2))
                                    _eds2(_dbm2)
                                    for tr in _early:
                                        if (
                                            str(tr.get("signature"))
                                            not in _existing_sigs
                                        ):
                                            try:
                                                _ut2(
                                                    _dbm2,
                                                    mint=mint,
                                                    signature=str(tr["signature"]),
                                                    event_index=0,
                                                    slot=int(tr["slot"]),
                                                    tx_index=None,
                                                    wallet=None,
                                                    side=str(tr["side"]),
                                                    quote_amount_base_units=int(
                                                        tr["quote_amount"]
                                                    ),
                                                    quote_mint="So11111111111111111111111111111111111111112",
                                                    base_amount=int(tr["base_amount"]),
                                                    price_ppm=int(tr["price_ppm"]),
                                                    signers_json=json.dumps([]),
                                                    raw_json=json.dumps(
                                                        {
                                                            "early_onchain": True,
                                                            "slot": tr["slot"],
                                                        }
                                                    ),
                                                )
                                            except Exception:
                                                pass
                                    _dbm2.close()
                                except Exception:
                                    pass
                        elif _early is not None and len(_early) == 0:
                            pass
                        else:
                            # throttle/unavailable -> mark honest
                            if _early is None:
                                unavailable.append(
                                    "early on-chain unavailable (rpc throttle/empty)"
                                )
                except Exception as exc:
                    logger.debug("early on-chain fetch failed for %s: %s", mint, exc)
                supply, base_dec, quote_dec, complete = _fetch_supply_via_rpc(
                    mint, rpc_url
                )
                if supply is None:
                    unavailable.append("total_supply: bonding_curve fetch failed")
                    base_dec = base_dec or 6
                    quote_dec = quote_dec or 9
                migrated = bool(complete) if complete is not None else False
                traj2: list[tuple[int, int, int]] = []
                for r in rows:
                    ppm = _recalc_price(r)
                    if ppm is None or ppm <= 0:
                        continue
                    slot = int(r["slot"]) if r["slot"] is not None else 0
                    mc = ppm * supply // 1_000_000 if supply is not None else 0
                    traj2.append((slot, ppm, mc))
                if traj2:
                    entry_slot, entry_ppm, entry_mc = traj2[0]
                    peak_idx = max(range(len(traj2)), key=lambda i: traj2[i][1])
                    peak_slot, peak_ppm, peak_mc = traj2[peak_idx]
                    floor_slot, floor_ppm, floor_mc = traj2[-1]
                    if (
                        migrated
                        and "post-migration trades not collected" not in unavailable
                    ):
                        unavailable.append("post-migration trades not collected")
                        sources["migrated"] = "bonding_curve complete==True"
                    if supply is None:
                        entry_mc_val = None
                        peak_mc_val = None
                        floor_mc_val = None
                        if (
                            "market_cap unavailable: total_supply missing"
                            not in unavailable
                        ):
                            unavailable.append(
                                "market_cap unavailable: total_supply missing"
                            )
                    else:
                        entry_mc_val = entry_mc
                        peak_mc_val = peak_mc
                        floor_mc_val = floor_mc
                        sources["market_cap"] = "price_ppm * total_supply // 1e6"
                    sources["price_ppm"] = (
                        "quote_amount_base_units*1e6//base_amount (executed)"
                    )
                    entry_usd = (
                        (entry_mc_val / 1e9 * sol_price_usd)
                        if (entry_mc_val is not None and sol_price_usd)
                        else None
                    )
                    return TokenMarketHistory(
                        mint=mint,
                        entry_mc_quote_lamports=entry_mc_val,
                        entry_mc_usd=entry_usd,
                        entry_price_ppm=entry_ppm,
                        entry_slot=entry_slot,
                        peak_mc_quote_lamports=peak_mc_val,
                        peak_price_ppm=peak_ppm,
                        peak_slot=peak_slot,
                        floor_mc_quote_lamports=floor_mc_val,
                        floor_price_ppm=floor_ppm,
                        trajectory=tuple(traj2),
                        migrated=migrated,
                        total_supply_base_units=supply,
                        base_decimals=base_dec,
                        quote_decimals=quote_dec,
                        sources=dict(sources),
                        unavailable=tuple(unavailable),
                        as_of_slot=floor_slot,
                        ath_unavailable=False,
                    )
        else:
            # solscan unavailable; keep unavailable list honest
            if api_key_missing := (_solscan_api_key() is None):
                unavailable.append("solscan unavailable: no api key")
            else:
                # only add if not already
                if "solscan unavailable" not in ",".join(unavailable):
                    unavailable.append("solscan: no trades")

    # fallback cascade off-chain
    try:
        from rugbot.intelligence.token_resolver import (
            _fetch_current_metadata,
            _fetch_peak_market_cap_usd,
            _resolve_pair_address,
        )

        name, symbol, current_mcap = _fetch_current_metadata(mint)
        if current_mcap and current_mcap > 0:
            sources["current_mcap"] = "dexscreener/pump"
        else:
            unavailable.append("current_mcap unavailable (dexscreener/pump empty)")
        pair = _resolve_pair_address(mint)
        if pair:
            sources["pairAddress"] = pair
        peak, peak_unavailable = _fetch_peak_market_cap_usd(mint, current_mcap)
        ath_unavailable = bool(peak_unavailable or peak is None)
        if ath_unavailable:
            unavailable.append("peak_market_cap unavailable (gecko/pump empty)")
        else:
            sources["peak_mcap"] = "geckoterminal ohlcv minute high * 1e9"
        # No trajectory off-chain honest -> empty
        unavailable.append("trajectory unavailable (no on-chain trades)")
        return TokenMarketHistory(
            mint=mint,
            entry_mc_quote_lamports=None,
            entry_mc_usd=None,
            entry_price_ppm=None,
            entry_slot=None,
            peak_mc_quote_lamports=int(peak) if peak else None,
            peak_price_ppm=None,
            peak_slot=None,
            floor_mc_quote_lamports=int(current_mcap) if current_mcap else None,
            floor_price_ppm=None,
            trajectory=(),
            migrated=False,
            total_supply_base_units=None,
            base_decimals=None,
            quote_decimals=None,
            sources=dict(sources),
            unavailable=tuple(unavailable),
            as_of_slot=None,
            ath_unavailable=ath_unavailable,
        )
    except Exception as exc:
        unavailable.append(f"fallback failed: {exc}")
        return TokenMarketHistory(
            mint=mint,
            entry_mc_quote_lamports=None,
            entry_mc_usd=None,
            entry_price_ppm=None,
            entry_slot=None,
            peak_mc_quote_lamports=None,
            peak_price_ppm=None,
            peak_slot=None,
            floor_mc_quote_lamports=None,
            floor_price_ppm=None,
            trajectory=(),
            migrated=False,
            total_supply_base_units=None,
            base_decimals=None,
            quote_decimals=None,
            sources=dict(sources),
            unavailable=tuple(unavailable),
            as_of_slot=None,
            ath_unavailable=True,
        )
