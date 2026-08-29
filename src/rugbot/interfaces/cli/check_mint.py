"""One-liner mint checker: creation, B0/B1, bundle, rugged, copytrade pick."""

# ruff: noqa: PLR2004, C901, PLR0912, PLR0915, S110, RUF001, TRY003

from __future__ import annotations

import argparse
import asyncio
import concurrent.futures
import json
import sys
from collections import Counter
from typing import Any

from sol_trade_sdk.solana.provider_pool import SyncRpcProviderPool

from rugbot.integrations.pumpfun_creator_index import fetch_pumpfun_created_tokens
from rugbot.intelligence.token_resolver import (
    fetch_token_metadata,
    resolve_token_or_wallet,
)
from rugbot.runtime.config import (
    load_provider_settings,
    resolve_dotenv,
)

_FUNDING_TX_CACHE: dict[str, object | None] = {}
_FUNDING_SEEN_SLOTS: set[int] = set()
_FUNDING_CONCURRENCY = 8


def _entry_block_label(buy_tx: int | None, creator_tx: int | None) -> str:
    if buy_tx is None or creator_tx is None:
        return "—"
    if buy_tx == creator_tx:
        return "B0"
    if buy_tx == creator_tx + 1:
        return "B1"
    return f"TX {buy_tx}"


def _format_sol(lamports: int | None) -> str:
    if lamports is None:
        return "—"
    return f"{lamports / 1e9:.2f} SOL"


def _short(addr: str) -> str:
    return f"{addr[:4]}…{addr[-4:]}" if len(addr) >= 10 else addr


def _short_sig(sig: str) -> str:
    return f"{sig[:6]}…{sig[-4:]}" if len(sig) >= 12 else sig


_LAMPORTS_PER_SOL = 1_000_000_000
_MOTHER_THRESHOLD_LAMPORTS = 200 * _LAMPORTS_PER_SOL
_MAX_SIGS = 100


def _trace_rpc_call(
    rpc_url: str,
    method: str,
    params: list[object],
    fallback_endpoints: tuple[str, ...],
    transport: Any | None = None,  # noqa: ANN401
) -> object:
    pool = transport or SyncRpcProviderPool((rpc_url, *fallback_endpoints))
    payload = json.dumps(
        {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    ).encode()
    resp = pool(rpc_url, payload)
    if resp.status == 429:
        raise RuntimeError("RPC 429 rate-limited")
    if 200 <= resp.status < 300:
        data: Any = json.loads(resp.body)
        if isinstance(data, dict) and "error" in data:
            # propagate as RuntimeError to fail-closed
            raise RuntimeError(f"RPC {method} error: {data['error']}")
        return data.get("result") if isinstance(data, dict) else None
    # non-200: try direct fallback once for 429 recovery
    if resp.status >= 500:
        raise RuntimeError(f"RPC {method} HTTP {resp.status}")
    raise RuntimeError(f"RPC {method} HTTP {resp.status}")


def _find_incoming_transfers(tx: dict[str, Any], target: str) -> list[dict[str, Any]]:
    meta = tx.get("meta")
    transaction = tx.get("transaction")
    if not isinstance(meta, dict) or not isinstance(transaction, dict):
        return []
    if meta.get("err") is not None:
        return []
    message = transaction.get("message")
    if not isinstance(message, dict):
        return []
    slot = tx.get("slot")
    sigs = transaction.get("signatures")
    sig = sigs[0] if isinstance(sigs, list) and sigs else ""
    if not isinstance(slot, int) or not isinstance(sig, str):
        return []
    block_time = tx.get("blockTime") or meta.get("blockTime")
    ts = block_time if isinstance(block_time, int) else 0
    # collect parsed instructions
    instrs: list[tuple[int, dict[str, Any]]] = []
    outer = message.get("instructions")
    if isinstance(outer, list):
        for idx, ins in enumerate(outer):
            if isinstance(ins, dict):
                instrs.append((idx, ins))
    inner = meta.get("innerInstructions")
    if isinstance(inner, list):
        for grp in inner:
            if not isinstance(grp, dict):
                continue
            gidx = grp.get("index", 0)
            gins = grp.get("instructions")
            if isinstance(gins, list):
                for j, ins in enumerate(gins):
                    if isinstance(ins, dict):
                        instrs.append((1_000_000 + int(gidx) * 10_000 + j, ins))
    rows: list[dict[str, Any]] = []
    for idx, ins in instrs:
        parsed = ins.get("parsed")
        if not isinstance(parsed, dict):
            continue
        ptype = parsed.get("type")
        if ptype not in ("transfer", "transferChecked"):
            continue
        info = parsed.get("info")
        if not isinstance(info, dict):
            continue
        if info.get("destination") != target:
            continue
        src = info.get("source")
        amt = info.get("lamports") if ptype == "transfer" else info.get("amount")
        if not isinstance(src, str) or not isinstance(amt, int):
            continue
        rows.append(
            {
                "from": src,
                "to": target,
                "lamports": amt,
                "slot": slot,
                "sig": sig,
                "ix": idx,
                "ts": ts,
            }
        )
    # balance-delta fallback when no parsed instruction (e.g., pre/post directly)
    if not rows:
        # Use pre/postBalances delta for native transfers
        pre = meta.get("preBalances")
        post = meta.get("postBalances")
        # fallback: try to extract account keys from transaction.message.accountKeys
        acct_keys: list[str] = []
        if isinstance(message.get("accountKeys"), list):
            for k in message["accountKeys"]:
                if isinstance(k, dict) and isinstance(k.get("pubkey"), str):
                    acct_keys.append(k["pubkey"])
                elif isinstance(k, str):
                    acct_keys.append(k)
        # handle loadedAddresses
        loaded = (
            meta.get("loadedAddresses")
            if isinstance(meta.get("loadedAddresses"), dict)
            else None
        )
        if isinstance(loaded, dict):
            for grp in ("writable", "readonly"):
                vals = loaded.get(grp)
                if isinstance(vals, list):
                    for v in vals:
                        if isinstance(v, str):
                            acct_keys.append(v)
        if isinstance(pre, list) and isinstance(post, list) and target in acct_keys:
            try:
                idx = acct_keys.index(target)
                if 0 <= idx < len(pre) and 0 <= idx < len(post):
                    delta = int(post[idx]) - int(pre[idx])
                    if delta > 0:
                        # find sender as account with largest negative delta (approx)
                        best_src = None
                        best_neg = 0
                        for i, (a, b) in enumerate(zip(pre, post, strict=False)):
                            d = int(b) - int(a)
                            if d < best_neg:
                                best_neg = d
                                best_src = acct_keys[i] if i < len(acct_keys) else None
                        rows.append(
                            {
                                "from": best_src or "unknown",
                                "to": target,
                                "lamports": delta,
                                "slot": slot,
                                "sig": sig,
                                "ix": 0,
                                "ts": ts,
                            }
                        )
            except Exception:  # noqa: BLE001
                pass
    return rows


async def _collect_funding_rows_async(
    wallet: str,
    rpc_url: str,
    fallback_endpoints: tuple[str, ...],
    semaphore: asyncio.Semaphore,
) -> list[dict[str, Any]]:
    sigs_page = await asyncio.to_thread(
        _trace_rpc_call,
        rpc_url,
        "getSignaturesForAddress",
        [wallet, {"limit": _MAX_SIGS, "commitment": "finalized"}],
        fallback_endpoints,
    )
    if not isinstance(sigs_page, list):
        return []
    sigs: list[str] = []
    for item in sigs_page:
        if not isinstance(item, dict):
            continue
        sig = item.get("signature")
        if isinstance(sig, str):
            sigs.append(sig)

    async def _fetch_one(sig: str) -> dict[str, Any] | None:
        if sig in _FUNDING_TX_CACHE:
            return _FUNDING_TX_CACHE[sig]  # type: ignore[return-value]
        async with semaphore:
            try:
                tx = await asyncio.to_thread(
                    _trace_rpc_call,
                    rpc_url,
                    "getTransaction",
                    [
                        sig,
                        {
                            "encoding": "jsonParsed",
                            "maxSupportedTransactionVersion": 0,
                            "commitment": "finalized",
                        },
                    ],
                    fallback_endpoints,
                )
            except RuntimeError as exc:
                if "429" in str(exc):
                    # non-blocking backoff before propagating
                    await asyncio.sleep(0.5)
                    raise
                return None
            _FUNDING_TX_CACHE[sig] = tx
            return tx  # type: ignore[return-value]

    results = await asyncio.gather(
        *(_fetch_one(s) for s in sigs), return_exceptions=True
    )
    rows: list[dict[str, Any]] = []
    for res in results:
        if isinstance(res, BaseException):
            if "429" in str(res):
                raise RuntimeError("RPC 429 rate-limited")
            continue
        if not isinstance(res, dict):
            continue
        rows.extend(_find_incoming_transfers(res, wallet))
    seen: set[tuple[str, int]] = set()
    uniq: list[dict[str, Any]] = []
    for r in rows:
        key = (r["sig"], int(r["ix"]))
        if key in seen:
            continue
        seen.add(key)
        uniq.append(r)
    uniq.sort(key=lambda x: int(x["slot"]))
    return uniq


def _collect_funding_rows(
    wallet: str,
    rpc_url: str,
    fallback_endpoints: tuple[str, ...],
) -> list[dict[str, Any]]:
    """Sync wrapper for backward compatibility; runs async gather with semaphore 8."""

    async def _run() -> list[dict[str, Any]]:
        sem = asyncio.Semaphore(_FUNDING_CONCURRENCY)
        return await _collect_funding_rows_async(
            wallet, rpc_url, fallback_endpoints, sem
        )

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(_run())
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
        return ex.submit(asyncio.run, _run()).result()


def _resolve_entity_wallets(
    funding_rows: list[dict[str, Any]],
    target_wallet: str,
    bundle_wallets: list[str],
) -> list[str]:
    """Deduped entity set {creator, mother, sous-meres, burners} from funding trace."""
    seen: set[str] = set()
    ordered: list[str] = []
    for w in [target_wallet, *bundle_wallets]:
        if w and w != "unknown" and w not in seen:
            seen.add(w)
            ordered.append(w)
    for r in funding_rows:
        for key in ("from", "to"):
            addr = r.get(key)
            if isinstance(addr, str) and addr != "unknown" and addr not in seen:
                seen.add(addr)
                ordered.append(addr)
    # cluster_graph expansion when tracker DB present (best-effort, no RPC)
    try:
        from rugbot.storage.tracker import (  # type: ignore  # noqa: PLC0415
            SQLiteTrackerRepository,  # type: ignore
        )
        from rugbot.tracker.cluster_graph_model import (  # type: ignore  # noqa: PLC0415
            build_cluster_graph_model,
        )

        repo = SQLiteTrackerRepository()  # type: ignore[call-arg]
        model = build_cluster_graph_model(repo, target_wallet)
        for nid in list(model.nodes.keys()):
            if nid not in seen and nid != "unknown":
                seen.add(nid)
                ordered.append(nid)
    except Exception:  # noqa: BLE001
        pass
    return ordered


async def _build_funding_chain_async(
    wallets: list[str],
    rpc_url: str,
    fallback_endpoints: tuple[str, ...],
) -> tuple[list[dict[str, Any]], str | None]:
    """Async gather over all wallets (N1+N2) with single semaphore 8 and shared cache."""
    sem = asyncio.Semaphore(_FUNDING_CONCURRENCY)
    # first level: gather all wallets concurrently
    first_results = await asyncio.gather(
        *(
            _collect_funding_rows_async(w, rpc_url, fallback_endpoints, sem)
            for w in wallets
        ),
        return_exceptions=True,
    )
    all_rows: list[dict[str, Any]] = []
    funder_map: dict[str, dict[str, Any]] = {}
    for res in first_results:
        if isinstance(res, BaseException):
            if "429" in str(res):
                raise RuntimeError("RPC 429 rate-limited")
            continue
        rows = res  # type: ignore[assignment]
        for r in rows[:3]:
            all_rows.append(r)
            prev = funder_map.get(r["to"])
            if prev is None or int(r["slot"]) < int(prev["slot"]):
                funder_map[r["to"]] = r
    # second level: gather funders concurrently (single global gather)
    funders = list({r["from"] for r in all_rows if r["from"] != "unknown"})[:8]
    if funders:
        second_results = await asyncio.gather(
            *(
                _collect_funding_rows_async(f, rpc_url, fallback_endpoints, sem)
                for f in funders
            ),
            return_exceptions=True,
        )
        extra: list[dict[str, Any]] = []
        for res in second_results:
            if isinstance(res, BaseException):
                if "429" in str(res):
                    raise RuntimeError("RPC 429 rate-limited")
                continue
            rows = res  # type: ignore[assignment]
            for r in rows[:2]:
                extra.append(r)
                break
        all_rows.extend(extra)
    return _finalize_funding_chain(all_rows, funder_map)


def _finalize_funding_chain(
    all_rows: list[dict[str, Any]],
    funder_map: dict[str, dict[str, Any]],
) -> tuple[list[dict[str, Any]], str | None]:
    if not all_rows:
        return [], None
    all_rows.sort(key=lambda x: (int(x["slot"]), x["lamports"]))
    small = [r["lamports"] for r in all_rows if r["lamports"] < 10 * _LAMPORTS_PER_SOL]
    common = Counter(small).most_common(1)
    summary = None
    if common:
        amt, cnt = common[0]
        if cnt >= 2 or (amt in [r["lamports"] for r in funder_map.values()]):
            summary = (
                f"{amt / _LAMPORTS_PER_SOL:.3f} SOL recurrent ×{cnt}"
                if cnt >= 2
                else f"{amt / _LAMPORTS_PER_SOL:.3f} SOL"
            )
    return all_rows, summary


def _build_funding_chain(
    wallets: list[str],
    rpc_url: str,
    fallback_endpoints: tuple[str, ...],
) -> tuple[list[dict[str, Any]], str | None]:
    """Sync wrapper: single global gather over N1+N2 wallets with semaphore 8."""

    async def _run() -> tuple[list[dict[str, Any]], str | None]:
        return await _build_funding_chain_async(wallets, rpc_url, fallback_endpoints)

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(_run())
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
        return ex.submit(asyncio.run, _run()).result()


_SCORE_TP_CANDIDATES: tuple[int, ...] = (25, 50, 75, 100, 150, 200, 300)
_SCORE_SAMPLE_COUNT = 10
_SCORE_LOSS_PCT = 40.0
_SCORE_WIN_THRESHOLD_MULT = 2.0  # >=+100%
_INSTALL_MIN_WINRATE_PCT = 33.0
_INSTALL_MAX_ENTRY_MCAP = 15000
_INSTALL_MAX_ENTRY_TXIDX = 1
_INSTALL_HISTORY_SAMPLES = 10
_INSTALL_TP_PPM = 1_000_000  # +100%
_INSTALL_TP_SELL_PPM = 1_000_000
_INSTALL_SL_PPM = -20000


def _score_mints_sync(
    candidates_sorted: list[Any],
    rpc_url: str,
    fallback_endpoints: tuple[str, ...],
    sample_count: int,
    wallets: list[str],
) -> dict[str, object]:
    """Shared scoring logic over already deduped sorted candidates."""
    sliced = candidates_sorted[:sample_count]
    deduped_mints = [c.mint for c in candidates_sorted]
    # keep stable ordering desc by created_timestamp
    if len(sliced) < sample_count:
        # entity-level fail-closed wording when multi-wallet
        is_entity = len(wallets) > 1
        reason = (
            f"entity {len(sliced)}/{sample_count} (fail-closed)"
            if is_entity
            else f"insufficient launches: {len(sliced)}/{sample_count} (fail-closed)"
        )
        return {
            "status": "abstain",
            "reason": reason,
            "operator_wallet": wallets[0] if wallets else "",
            "entity_wallets": wallets,
            "deduped_mints": deduped_mints,
            "sample_count": sample_count,
            "found": len(sliced),
        }
    rows: list[dict[str, object]] = []
    wins = 0
    ath_vals: list[float] = []
    for cand in sliced:
        try:
            resolved = resolve_token_or_wallet(
                cand.mint, rpc_url=rpc_url, fallback_endpoints=fallback_endpoints
            )
        except Exception:  # noqa: BLE001
            resolved = None
        entry_sol = None
        if resolved is not None and resolved.bundle_buys:
            entry_sol = sum(b.max_sol_cost_lamports for b in resolved.bundle_buys) / 1e9
        # fallback: try single buy size if available else 0
        if entry_sol is None:
            entry_sol = 0.0
        try:
            _, _, mcap, ath_mult = fetch_token_metadata(cand.mint)
        except Exception:  # noqa: BLE001
            mcap, ath_mult = 0.0, 1.0
        ath_mult = float(ath_mult) if ath_mult else 1.0
        ath_vals.append(ath_mult)
        floor_usd = float(mcap) if mcap else 0.0
        # floor: if rugged (<5k) else current mcap as floor proxy
        floor_label = f"${floor_usd:,.0f}" if floor_usd else "—"
        is_win = ath_mult >= _SCORE_WIN_THRESHOLD_MULT
        if is_win:
            wins += 1
        rows.append(
            {
                "mint": cand.mint,
                "symbol": cand.symbol,
                "entry_sol": round(float(entry_sol), 4),
                "ath_mult": round(float(ath_mult), 2),
                "floor_usd": floor_usd,
                "floor_label": floor_label,
                "win": bool(is_win),
            }
        )
    losses = sample_count - wins
    net_ev_pct = wins * 100.0 - losses * _SCORE_LOSS_PCT
    # optimal TP among candidates net of fees/slippage
    # fees: 1% pump each side + 1.5% slippage each side + gas negligible
    fee_drag_pct = 5.0  # approx 1+1+1.5+1.5
    best_tp = _SCORE_TP_CANDIDATES[0]
    best_ev = float("-inf")
    per_tp_ev: dict[int, float] = {}
    for tp in _SCORE_TP_CANDIDATES:
        hits = sum(1 for v in ath_vals if v >= 1.0 + tp / 100.0)
        misses = sample_count - hits
        # net TP after fees
        net_tp = max(0.0, tp - fee_drag_pct)
        ev = hits * net_tp - misses * _SCORE_LOSS_PCT
        per_tp_ev[tp] = ev
        if ev > best_ev:
            best_ev = ev
            best_tp = tp
    # robustesse zone: neighboring TP within 10% of best EV
    robust = (
        [tp for tp, ev in per_tp_ev.items() if ev >= best_ev * 0.9]
        if best_ev > 0
        else [best_tp]
    )
    winrate_pct = wins / sample_count * 100.0
    return {
        "status": "ok",
        "operator_wallet": wallets[0] if wallets else "",
        "entity_wallets": wallets,
        "deduped_mints": deduped_mints,
        "sample_count": sample_count,
        "wins": wins,
        "losses": losses,
        "winrate_pct": winrate_pct,
        "net_ev_pct": net_ev_pct,
        "optimal_tp_pct": best_tp,
        "optimal_ev_pct": best_ev,
        "robust_zone": robust,
        "per_tp_ev": per_tp_ev,
        "rows": rows,
    }


def _score_operator_sync(
    operator_wallet: str,
    rpc_url: str,
    fallback_endpoints: tuple[str, ...],
    sample_count: int = _SCORE_SAMPLE_COUNT,
) -> dict[str, object]:
    """Score last N launches for a single operator wallet (wallet mode)."""
    candidates = fetch_pumpfun_created_tokens(operator_wallet)
    # newest first already; sorting by created_timestamp desc for dedup parity
    sorted_cands = sorted(candidates, key=lambda c: c.created_timestamp, reverse=True)
    return _score_mints_sync(
        sorted_cands, rpc_url, fallback_endpoints, sample_count, [operator_wallet]
    )


def _score_entity_sync(
    wallets: list[str],
    rpc_url: str,
    fallback_endpoints: tuple[str, ...],
    sample_count: int = _SCORE_SAMPLE_COUNT,
) -> dict[str, object]:
    """Score entity: fetch mints across all wallets deduped by mint, take last 10."""
    by_mint: dict[str, Any] = {}
    for w in wallets:
        try:
            cands = fetch_pumpfun_created_tokens(w)
        except Exception:  # noqa: BLE001,S112
            continue
        for c in cands:
            prev = by_mint.get(c.mint)
            if prev is None or int(c.created_timestamp) < int(prev.created_timestamp):
                by_mint[c.mint] = c
    sorted_cands = sorted(
        by_mint.values(), key=lambda c: int(c.created_timestamp), reverse=True
    )
    return _score_mints_sync(
        sorted_cands, rpc_url, fallback_endpoints, sample_count, wallets
    )


def _resolve_install_address(  # noqa: PLR0911
    explicit: str | None,
    mode: str | None,
    resolved: Any,  # noqa: ANN401
    pick: Any | None,  # noqa: ANN401
    funding_rows: list[dict[str, Any]],
) -> tuple[str, str]:
    """Pick tracker address for the chosen mode and return (address, hint)."""
    if explicit and explicit != "__flag__":
        addr = explicit.strip()
        hint = "explicit --install-tracker address"
        return addr, hint
    if mode == "method1":
        # Method1: sniper via funding chain — mother wallet + B0
        mother_candidates = [
            r["from"]
            for r in funding_rows
            if r["lamports"] >= _MOTHER_THRESHOLD_LAMPORTS
        ]
        if mother_candidates:
            return mother_candidates[0], "mother wallet (funding chain, B0 sniper)"
        burners = {resolved.target_wallet, *list(resolved.bundle_wallets)}
        upstream = {r["from"] for r in funding_rows if r["to"] in burners}
        # fallback: upstream funder of burner
        if upstream:
            return next(iter(upstream)), "upstream funder (method1 fallback)"
        return (
            resolved.target_wallet,
            "creator wallet (method1 fallback, funding chain unavailable)",
        )
    # copytrade default: follow bundler / recommended copytrade wallet
    if pick is not None:
        return pick.wallet, "bundler wallet (bundle B0/B1 analysis, copytrade)"
    bundle_wallets = list(resolved.bundle_wallets)
    if bundle_wallets:
        return bundle_wallets[0], "first bundle wallet (copytrade fallback)"
    return resolved.target_wallet, "creator wallet (copytrade fallback)"


def _build_tracker_snippet(address: str, mode: str | None) -> str:
    """Return a ready-to-paste sniper snippet."""
    effective_mode = mode or "copytrade"
    tracking_hint = (
        "track_buys" if effective_mode == "copytrade" else "new_token_creations"
    )
    return (
        f"# Tracker snippet — {effective_mode} — Après ta validation\n"
        f"# Copytrade = follow bundler wallet · Method1 = sniper mother wallet + B0\n"
        f"target:\n"
        f"  kind: wallet\n"
        f"  id: {address}\n"
        f"tracking_mode: {tracking_hint}\n"
        f"strategy:\n"
        f"  history_sample_count: {_INSTALL_HISTORY_SAMPLES}\n"
        f"  max_entry_market_cap_quote_base_units: {_INSTALL_MAX_ENTRY_MCAP}\n"
        f"  max_entry_transaction_index: {_INSTALL_MAX_ENTRY_TXIDX}\n"
        f"rules:\n"
        f"  sell:\n"
        f"    take_profit_levels:\n"
        f"      - trigger_pnl_ppm: {_INSTALL_TP_PPM}  # +100%\n"
        f"        sell_fraction_ppm: {_INSTALL_TP_SELL_PPM}\n"
        f"    stop_loss_levels:\n"
        f"      - trigger_pnl_ppm: {_INSTALL_SL_PPM}\n"
        f"        sell_fraction_ppm: 1000000\n"
        f"    # stop dev-sell: trailing + auto_sell_big_buy handled by rules.sell above\n"
    )


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="rug_check",
        description=(
            "One-liner token check: creation slot/tx, creator, bundle B0/B1, "
            "rugged/ATH verdict, RECOMMENDED copytrade wallet. "
            "Live feed: rug_watch --stream or rug_live or rug_web"
        ),
    )
    p.add_argument("mint", help="Pump mint address to inspect")
    p.add_argument("--json", action="store_true", help="emit machine JSON only")
    p.add_argument("--raw", action="store_true", help="alias for --json")
    p.add_argument("--rpc", help="override SOLANA_RPC_HTTP for this call")
    p.add_argument(
        "--trace-funding",
        action="store_true",
        help="walk funding transfers backwards (100 sigs per wallet) and show relay chain",
    )
    p.add_argument(
        "--score",
        action="store_true",
        help="score operator on last 10 launches (entry, ATH, floor, winrate, TP optimal)",
    )
    p.add_argument(
        "--entity",
        action="store_true",
        help="entity mode for --score: dedup mints across funding-chain wallets (creator/mother/sous-meres/burners) sorted desc, take last 10; fail-closed entity N/10",
    )
    p.add_argument(
        "--wallet",
        help="override operator wallet for --score (default: creator of mint)",
    )
    p.add_argument(
        "--install-tracker",
        nargs="?",
        const="__flag__",
        default=None,
        metavar="ADDRESS",
        help="wizard: output sniper snippet and fail-closed gate (optionally override tracker address)",
    )
    p.add_argument(
        "--mode",
        choices=["copytrade", "method1"],
        default=None,
        help="tracker mode: copytrade (follow bundler) or method1 (sniper funding-chain mother + B0)",
    )
    p.add_argument(
        "--apply",
        action="store_true",
        help="write DB sniper config from the validated snippet (requires passing validation or --force)",
    )
    p.add_argument(
        "--force",
        action="store_true",
        help="bypass winrate gate after explicit validation (Après ta validation)",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    as_json = bool(args.json or args.raw)
    mint: str = args.mint.strip()
    resolve_dotenv()
    providers = load_provider_settings()
    rpc = args.rpc or providers.rpc_http
    fallback = providers.rpc_http_fallbacks

    # Live alias hint: document that rug_live == rug_watch --stream --mode observe
    # (wired via pyproject.toml scripts rug_live).
    try:
        resolved = resolve_token_or_wallet(
            mint, rpc_url=rpc, fallback_endpoints=fallback
        )
    except Exception as exc:  # noqa: BLE001
        if as_json:
            print(json.dumps({"status": "error", "mint": mint, "message": str(exc)}))
        else:
            print(f"[rug_check] {mint} — resolve failed: {exc}", file=sys.stderr)
        return 1

    # Market history (data-based on-chain first)
    mcap: float | None = None
    fdv: float | None = None
    dex_symbol: str | None = None
    dex_name: str | None = None
    rugged: str = "N/A (fail-closed: fetch unavailable)"
    ath_mult: float | None = None
    dex_ok = False
    market_hist: Any | None = None
    try:
        from rugbot.domain.market_data import build_token_market_history as _build_hist

        market_hist = _build_hist(mint, rpc_url=rpc)
        # derive mcap/fdv from on-chain entry if available, else fallback current
        if market_hist.entry_mc_quote_lamports is not None:
            # lamports SOL -> USD approx via current mcap fallback? keep lamports display elsewhere
            # for legacy mcap field, use quote lamports as USD proxy if needed
            mcap = float(
                market_hist.entry_mc_quote_lamports
            )  # keep raw for display compatibility
        if market_hist.floor_mc_quote_lamports is not None and mcap is None:
            mcap = float(market_hist.floor_mc_quote_lamports)
        # peak mult data-based
        if market_hist.entry_price_ppm and market_hist.peak_price_ppm:
            try:
                ath_mult = float(market_hist.peak_price_ppm) / float(
                    market_hist.entry_price_ppm
                )
            except Exception:
                ath_mult = None
        # fallback to dex if still none
        if ath_mult is None or mcap is None:
            try:
                name2, symbol2, market_cap2, ath2 = fetch_token_metadata(mint)
                dex_name, dex_symbol = name2, symbol2
                if mcap is None:
                    mcap = market_cap2
                    fdv = market_cap2
                if ath_mult is None:
                    ath_mult = ath2
                if market_cap2:
                    dex_ok = True
            except Exception:
                pass
        else:
            dex_ok = True
            try:
                name2, symbol2, _, _ = fetch_token_metadata(mint)
                dex_name, dex_symbol = name2, symbol2
            except Exception:
                pass
        if mcap is not None and mcap > 0:
            rugged = "NO" if mcap >= 5000 else "LIKELY (mcap < $5k)"
        else:
            rugged = "N/A (fail-closed: mcap unavailable)"
        if market_hist.migrated:
            rugged += " · MIGRATED (PumpSwap)"
    except Exception:  # noqa: BLE001
        # final fallback to old path
        try:
            name, symbol, market_cap, ath = fetch_token_metadata(mint)
            dex_name, dex_symbol, mcap, ath_mult = name, symbol, market_cap, ath
            fdv = market_cap
            dex_ok = True
            if mcap is not None and mcap > 0:
                rugged = "NO" if mcap >= 5000 else "LIKELY (mcap < $5k)"
            else:
                rugged = "N/A (fail-closed: mcap=0)"
        except Exception:
            pass

    # Bundle derivations
    buys = list(resolved.bundle_buys)
    # Already sorted by txIndex then wallet in resolver
    creator_tx = resolved.creation_transaction_index
    total_sol = sum(b.max_sol_cost_lamports for b in buys)
    # B0/B1 counts
    b0 = sum(
        1 for b in buys if _entry_block_label(b.transaction_index, creator_tx) == "B0"
    )
    b1 = sum(
        1 for b in buys if _entry_block_label(b.transaction_index, creator_tx) == "B1"
    )

    # Degenerate: no executable buys or all 0 tokens
    all_zero = bool(buys) and all(b.token_amount == 0 for b in buys)
    no_copy = len(buys) == 0 or all_zero
    # Pick: earliest non-creator executable (B0 preferred via sort); skip 0-token burns
    executable = [
        b for b in buys if b.token_amount > 0 and b.wallet != resolved.target_wallet
    ]
    if not executable and buys:
        # fallback: if only creator bought, still surface creator as only option
        executable = [b for b in buys if b.token_amount > 0]
    pick = executable[0] if executable else None
    # Why line
    why = ""
    if pick is not None:
        block = _entry_block_label(pick.transaction_index, creator_tx)
        rank = 1  # sorted ascending => first executable is rank 1 among executable
        # staged / repeat not available single-mint; report leader + amount
        leader_tag = (
            "LEADER"
            if block == "B0" and pick.transaction_index == creator_tx
            else "B1"
            if block == "B1"
            else block
        )
        why = f"{leader_tag} rank {rank} · {_format_sol(pick.max_sol_cost_lamports)} · {pick.token_amount} tokens · B0×{b0} B1×{b1}"
    else:
        why = "NO COPY ABORTED — no executable buy in creation slot"

    # 1s candle MC proxy: bundle SOL is floor cost; approximate MC floor ≈ bundle SOL * price? show SOL
    mc_proxy = f"{_format_sol(total_sol)} bundled (1s floor)"

    # Funding trace (opt-in)
    funding_rows: list[dict[str, Any]] = []
    funding_summary: str | None = None
    funding_error: str | None = None
    if bool(getattr(args, "trace_funding", False)):
        trace_wallets = [resolved.target_wallet, *list(resolved.bundle_wallets[:2])]
        # de-dupe preserve order
        seen_w: set[str] = set()
        uniq_w: list[str] = []
        for w in trace_wallets:
            if w not in seen_w:
                seen_w.add(w)
                uniq_w.append(w)
        try:
            if not rpc:
                raise RuntimeError("SOLANA_RPC_HTTP is required for --trace-funding")  # noqa: TRY301
            funding_rows, funding_summary = _build_funding_chain(uniq_w, rpc, fallback)
        except RuntimeError as exc:
            if "429" in str(exc):
                funding_error = "RPC 429 rate-limited (fail-closed)"
            else:
                funding_error = str(exc)
        except Exception as exc:  # noqa: BLE001
            funding_error = str(exc)

    # funding_wallets derived from funding trace (shared dedup path)
    funding_wallets: list[str] = []
    if funding_rows:
        funding_wallets = _resolve_entity_wallets(
            funding_rows, resolved.target_wallet, list(resolved.bundle_wallets[:3])
        )

    # Operator scoring (optional)
    score_result: dict[str, Any] | None = None
    wants_score = bool(getattr(args, "score", False)) or bool(
        getattr(args, "wallet", None)
    )
    is_entity_score = bool(getattr(args, "entity", False))
    if wants_score:
        if is_entity_score:
            # entity mode: need funding_wallets; build funding chain if not yet done
            if not funding_rows and not funding_error:
                trace_wallets = [
                    resolved.target_wallet,
                    *list(resolved.bundle_wallets[:2]),
                ]
                seen_w: set[str] = set()
                uniq_w: list[str] = []
                for w in trace_wallets:
                    if w not in seen_w:
                        seen_w.add(w)
                        uniq_w.append(w)
                try:
                    if not rpc:
                        raise RuntimeError(  # noqa: TRY301
                            "SOLANA_RPC_HTTP required for --score --entity"
                        )
                    funding_rows, funding_summary = _build_funding_chain(
                        uniq_w, rpc, fallback
                    )
                    funding_wallets = _resolve_entity_wallets(
                        funding_rows,
                        resolved.target_wallet,
                        list(resolved.bundle_wallets[:3]),
                    )
                except RuntimeError as exc:
                    funding_error = (
                        "RPC 429 rate-limited (fail-closed)"
                        if "429" in str(exc)
                        else str(exc)
                    )
                except Exception as exc:  # noqa: BLE001
                    funding_error = str(exc)
            wallets_for_entity = funding_wallets or [resolved.target_wallet]
            # dedup preserve order already
            try:
                if not rpc:
                    raise RuntimeError("SOLANA_RPC_HTTP required for --score")  # noqa: TRY301
                score_result = _score_entity_sync(wallets_for_entity, rpc, fallback)  # type: ignore[assignment]
            except Exception as exc:  # noqa: BLE001
                score_result = {
                    "status": "error",
                    "message": str(exc),
                    "entity_wallets": wallets_for_entity,
                }
        else:
            operator_for_score = (
                getattr(args, "wallet", None) or resolved.target_wallet or ""
            ).strip()
            if operator_for_score:
                try:
                    if not rpc:
                        raise RuntimeError("SOLANA_RPC_HTTP required for --score")  # noqa: TRY301
                    score_result = _score_operator_sync(
                        operator_for_score, rpc, fallback
                    )  # type: ignore[assignment]
                except Exception as exc:  # noqa: BLE001
                    score_result = {
                        "status": "error",
                        "message": str(exc),
                        "operator_wallet": operator_for_score,
                    }
            else:
                score_result = {
                    "status": "abstain",
                    "reason": "no operator wallet for scoring",
                }

    # precompute install-tracker gate for both JSON and human paths
    install_info: dict[str, Any] | None = None
    _install_wants = getattr(args, "install_tracker", None) is not None
    _install_mode: str | None = getattr(args, "mode", None)
    if _install_wants and _install_mode is None:
        _install_mode = "copytrade"
    if _install_wants:
        _explicit = args.install_tracker if args.install_tracker != "__flag__" else None
        _addr, _hint = _resolve_install_address(
            _explicit, _install_mode, resolved, pick, funding_rows
        )
        _snippet = _build_tracker_snippet(_addr, _install_mode)
        _gate_passed = True
        _gate_reason = ""
        _abstained = False
        if score_result is not None and score_result.get("status") == "ok":
            _wr = float(score_result.get("winrate_pct", 0.0))  # type: ignore[arg-type]
            if _wr < _INSTALL_MIN_WINRATE_PCT and not bool(
                getattr(args, "force", False)
            ):
                _gate_passed = False
                _gate_reason = f"winrate {_wr:.0f}% < {_INSTALL_MIN_WINRATE_PCT:.0f}% — abstention fail-closed (use --force après ta validation)"
                _abstained = True
        elif (
            score_result is not None
            and score_result.get("status") != "ok"
            and not bool(getattr(args, "force", False))
        ):
            _gate_passed = False
            _gate_reason = f"score {score_result.get('status')}: {score_result.get('reason') or score_result.get('message')} — abstention fail-closed (use --force après ta validation)"
            _abstained = True
        install_info = {
            "requested": True,
            "mode": _install_mode,
            "tracker_address": _addr,
            "hint": _hint,
            "snippet": _snippet,
            "gate_passed": _gate_passed,
            "gate_reason": _gate_reason,
            "abstained": _abstained,
        }

    # ensure funding_wallets populated even when only trace_funding without entity score already done
    if not funding_wallets and funding_rows:
        funding_wallets = _resolve_entity_wallets(
            funding_rows, resolved.target_wallet, list(resolved.bundle_wallets[:3])
        )
    _mh_dict: dict[str, Any] | None = None
    if market_hist is not None:
        _mh_dict = {
            "entry_mc_quote_lamports": market_hist.entry_mc_quote_lamports,
            "entry_price_ppm": market_hist.entry_price_ppm,
            "entry_slot": market_hist.entry_slot,
            "peak_mc_quote_lamports": market_hist.peak_mc_quote_lamports,
            "peak_price_ppm": market_hist.peak_price_ppm,
            "peak_slot": market_hist.peak_slot,
            "floor_mc_quote_lamports": market_hist.floor_mc_quote_lamports,
            "migrated": market_hist.migrated,
            "sources": market_hist.sources,
            "unavailable": list(market_hist.unavailable),
            "ath_unavailable": market_hist.ath_unavailable,
        }
    payload: dict[str, Any] = {
        "mint": mint,
        "is_token": resolved.is_token,
        "symbol": resolved.symbol or dex_symbol,
        "name": resolved.name or dex_name,
        "creator": resolved.target_wallet,
        "creation_slot": resolved.creation_slot,
        "creation_signature": resolved.creation_signature,
        "creation_transaction_index": creator_tx,
        "bonding_curve": resolved.bonding_curve,
        "bundle_size": len(buys),
        "bundle_total_sol_lamports": total_sol,
        "b0_count": b0,
        "b1_count": b1,
        "buys": [
            {
                "wallet": b.wallet,
                "signature": b.signature,
                "transaction_index": b.transaction_index,
                "entry_block": _entry_block_label(b.transaction_index, creator_tx),
                "max_sol_cost_lamports": b.max_sol_cost_lamports,
                "token_amount": b.token_amount,
            }
            for b in buys
        ],
        "mc_proxy_1s": mc_proxy,
        "mcap_usd": mcap,
        "fdv_usd": fdv,
        "ath_multiplier": ath_mult,
        "rugged": rugged,
        "dex_ok": dex_ok,
        "peak_mult": None,
        "market_history": _mh_dict,
        "recommended_wallet": pick.wallet if pick else None,
        "why": why,
        "no_copy_aborted": no_copy,
        "live_hint": "rug_watch --stream  |  rug_live  |  rug_web",
        "funding_chain": funding_rows,
        "funding_summary": funding_summary,
        "funding_error": funding_error,
        "funding_wallets": funding_wallets,
        "score": score_result,
        "install_tracker": install_info,
    }

    if as_json:
        print(json.dumps(payload, sort_keys=True))
        return 0

    # Human one-liner + table
    sym = resolved.symbol or dex_symbol or "PUMP"
    creator_short = _short(resolved.target_wallet)
    slot = resolved.creation_slot if resolved.creation_slot is not None else "—"
    # One-liner
    if no_copy:
        print(
            f"✗ {mint} (${sym}) · slot {slot} txIdx {creator_tx} · creator {creator_short} · NO COPY ABORTED — {why}"
        )
    else:
        pick_short = _short(pick.wallet) if pick else "—"  # type: ignore[union-attr]
        star = "★ RECOMMENDED"
        rugged_tag = rugged if dex_ok else "rugged N/A fail-closed"
        print(
            f"✓ {mint} (${sym}) · slot {slot} txIdx {creator_tx} · creator {creator_short} · "
            f"bundle {len(buys)} buys B0×{b0} B1×{b1} total {_format_sol(total_sol)}"
            f" · 1s MC proxy {mc_proxy} · {rugged_tag} · "
            f"{star} {pick_short} — {why}"
        )

    # Detailed table
    print(
        f"  creation: slot {slot}  txIdx {creator_tx}  sig {resolved.creation_signature or '—'}"
    )
    print(f"  creator:  {resolved.target_wallet}")
    print(f"  bonding_curve: {resolved.bonding_curve or '—'}")
    if market_hist is not None:
        entry_mc_s = (
            _format_sol(market_hist.entry_mc_quote_lamports)
            if market_hist.entry_mc_quote_lamports
            else "—"
        )
        peak_mc_s = (
            _format_sol(market_hist.peak_mc_quote_lamports)
            if market_hist.peak_mc_quote_lamports
            else "—"
        )
        mig_s = " MIGRATED" if market_hist.migrated else ""
        src_s = ",".join(market_hist.sources.keys()) if market_hist.sources else "—"
        unav_s = ",".join(market_hist.unavailable) if market_hist.unavailable else "—"
        ath_s = f"{ath_mult:.2f}x" if ath_mult else "—"
        print(
            f"  market: entry Mc {entry_mc_s} peak Mc {peak_mc_s} ath×{ath_s}{mig_s} sources={src_s} unavailable={unav_s}"
        )
    if dex_ok:
        print(
            f"  dex: {dex_name} (${dex_symbol}) mcap ${mcap:,.0f} ath×{ath_mult} rugged={rugged}"
        )
    else:
        print(f"  dex: unavailable (fail-closed) rugged={rugged}")
    print(f"  bundle: {len(buys)} buys total {_format_sol(total_sol)} B0×{b0} B1×{b1}")
    for b in buys:
        block = _entry_block_label(b.transaction_index, creator_tx)
        mark = " ★" if pick and b.wallet == pick.wallet else ""
        print(
            f"    - {block} txIdx {b.transaction_index}  {_short(b.wallet)}  "
            f"{_format_sol(b.max_sol_cost_lamports)}  tokens {b.token_amount}{mark}"
        )
    if pick:
        print(f"  ★ RECOMMENDED copytrade: {pick.wallet} — {why}")
    else:
        print(f"  NO COPY ABORTED — {why}")
    if bool(getattr(args, "trace_funding", False)):
        print("  funding chain (--trace-funding):")
        if funding_error:
            print(f"    unavailable (fail-closed): {funding_error}")
        elif not funding_rows:
            print(
                "    no incoming funding transfer found in last 100 sigs (fresh or CEX direct)"
            )
        else:
            amt_counts = Counter(r["lamports"] for r in funding_rows)
            # identify mother: any sender that received >=200 SOL
            mother_addrs = {
                r["to"]
                for r in funding_rows
                if r["lamports"] >= _MOTHER_THRESHOLD_LAMPORTS
            }
            burner_set = {resolved.target_wallet, *list(resolved.bundle_wallets)}
            relay_addrs = {r["from"] for r in funding_rows if r["to"] in burner_set}
            # second hop: those that funded relays
            upstream = {r["from"] for r in funding_rows if r["to"] in relay_addrs}
            print(
                f"    {'hop':>3}  {'amount':>10}  {'from → to':<38}  {'slot':>10}  {'sig':<14}  role"
            )
            for idx, r in enumerate(
                sorted(funding_rows, key=lambda x: int(x["slot"])), start=1
            ):
                amt_sol = r["lamports"] / _LAMPORTS_PER_SOL
                amt_str = f"{amt_sol:.3f} SOL"
                if amt_counts[r["lamports"]] >= 2:
                    amt_str += "*"
                from_s = _short(r["from"])
                to_s = _short(r["to"])
                flow = f"{from_s} → {to_s}"
                sig_s = _short_sig(r["sig"])
                slot_s = str(r["slot"])
                # role classification
                if r["to"] in burner_set:
                    if r["from"] in mother_addrs:
                        role = "Master"
                    elif (
                        r["from"] in upstream
                        or r["lamports"] >= _MOTHER_THRESHOLD_LAMPORTS
                    ):
                        role = "Master"
                    else:
                        # if sender also has upstream mother, it's sous-mère
                        is_sub = any(
                            x["from"] in mother_addrs
                            for x in funding_rows
                            if x["to"] == r["from"]
                        )
                        if is_sub or (
                            r["from"] in relay_addrs
                            and r["lamports"] < 20 * _LAMPORTS_PER_SOL
                        ):
                            # heuristic: small recurrent funding from intermediate
                            has_mother_upstream = any(
                                y["to"] == r["from"]
                                and y["lamports"] >= _MOTHER_THRESHOLD_LAMPORTS
                                for y in funding_rows
                            )
                            role = (
                                "Sous-Mère"
                                if has_mother_upstream
                                else "Sous-Mère"
                                if r["lamports"] < 20 * _LAMPORTS_PER_SOL
                                and amt_counts[r["lamports"]] >= 2
                                else "CEX"
                            )
                            # simplify: if amount small recurrent -> CEX/Sous-Mère
                            if role == "CEX" and amt_counts[r["lamports"]] >= 2:
                                role = "Sous-Mère"
                        else:
                            role = (
                                "CEX"
                                if amt_counts[r["lamports"]] >= 1
                                and r["lamports"] < 10 * _LAMPORTS_PER_SOL
                                else "Funder"
                            )
                    # burner target marker
                    role = role + "/Burner→" if False else role
                    # final mapping: keep concise
                    if r["to"] in burner_set:
                        # target is burner, source role as above
                        pass
                elif r["lamports"] >= _MOTHER_THRESHOLD_LAMPORTS:
                    role = "Master"
                elif r["to"] in relay_addrs:
                    role = (
                        "Master→Sous-Mère"
                        if r["lamports"] >= 50 * _LAMPORTS_PER_SOL
                        else "Sous-Mère"
                    )
                else:
                    role = "CEX" if r["lamports"] < 10 * _LAMPORTS_PER_SOL else "Funder"
                # override: if lamports large => Master
                if r["lamports"] >= _MOTHER_THRESHOLD_LAMPORTS:
                    role = "Master"
                elif r["from"] in mother_addrs:
                    role = "Sous-Mère"
                print(
                    f"    {idx:>3}  {amt_str:>10}  {flow:<38}  {slot_s:>10}  {sig_s:<14}  {role}"
                )
            if funding_summary:
                print(f"    signature montants réutilisables: {funding_summary}")
            # chain lines like "CEX 2.495 SOL → Fresh Wallet → Create"
            for target in [resolved.target_wallet, *list(resolved.bundle_wallets[:3])]:
                chain = [r for r in funding_rows if r["to"] == target]
                if chain:
                    c = sorted(chain, key=lambda x: int(x["slot"]))[0]
                    amt = c["lamports"] / _LAMPORTS_PER_SOL
                    src_short = _short(c["from"])
                    tgt_short = _short(c["to"])
                    print(
                        f"    chain: {src_short} {amt:.3f} SOL → {tgt_short} → Create (slot {c['slot']} sig {_short_sig(c['sig'])})"
                    )
    if score_result is not None:
        print("  score (--score) operator last 10 launches:")
        if score_result.get("status") != "ok":
            print(
                f"    fail-closed: {score_result.get('reason') or score_result.get('message')}"
            )
        else:
            print(
                f"    {'mint':<44} {'entry SOL':>10} {'ATH':>8} {'floor':>12} {'win?':>4}"
            )
            for r in score_result["rows"]:  # type: ignore[index]
                mint_s = str(r["mint"])  # type: ignore[index]
                print(
                    f"    {mint_s:<44} {float(r['entry_sol']):>10.3f} {float(r['ath_mult']):>7.2f}x {r['floor_label']!s:>12} {'WIN' if r['win'] else 'LOSS':>4}"
                )
            wr = float(score_result["winrate_pct"])  # type: ignore[arg-type]
            wins = int(score_result["wins"])  # type: ignore[arg-type]
            net_ev = float(score_result["net_ev_pct"])  # type: ignore[arg-type]
            tp = int(score_result["optimal_tp_pct"])  # type: ignore[arg-type]
            robust = score_result.get("robust_zone", [])
            robust_s = ",".join(f"+{x}%" for x in robust) if robust else f"+{tp}%"
            print(
                f"    Winrate {wins}/10 ({wr:.0f}%) · Net EV {net_ev:+.0f}% (bible: 7W+700-3*40=+580%) · TP optimal +{tp}% (robustesse zone {robust_s})"
            )
    # --- install-tracker wizard human output (reuses precomputed install_info) ---
    if install_info is not None:
        _mode = str(install_info["mode"])
        _addr = str(install_info["tracker_address"])
        _hint = str(install_info["hint"])
        _snippet = str(install_info["snippet"])
        _abstained = bool(install_info["abstained"])
        _gate_reason = str(install_info["gate_reason"])
        if not as_json:
            if _abstained:
                print(f"  install-tracker [{_mode}] ABSTAINED — {_gate_reason}")
                print(f"  candidate: {_addr} ({_hint})")
                print(
                    "  Après ta validation: relance avec --force pour poser le tracker"
                )
                print(f"  snippet (not applied):\n{_snippet}")
            else:
                if (
                    score_result is not None
                    and score_result.get("status") == "ok"
                    and not bool(getattr(args, "force", False))
                ):
                    print(
                        f"  install-tracker [{_mode}] — Après ta validation ✓ (winrate {float(score_result['winrate_pct']):.0f}% ≥ {_INSTALL_MIN_WINRATE_PCT:.0f}%)"
                    )
                elif bool(getattr(args, "force", False)):
                    print(
                        f"  install-tracker [{_mode}] — Après ta validation (forcé --force) ✓"
                    )
                else:
                    print(
                        f"  install-tracker [{_mode}] — sans --score (validation manuelle requise) — Après ta validation"
                    )
                if _mode == "method1":
                    print(
                        "  Method1 = sniper via funding chain (mother wallet + B0) — use mother wallet above"
                    )
                else:
                    print(
                        "  copytrade = follow bundler wallet (use bundler address from bundle analysis)"
                    )
                print(f"  tracker: {_addr} ({_hint})")
                print(
                    f"  snippet (filtres: max_entry_market_cap={_INSTALL_MAX_ENTRY_MCAP}, max_entry_transaction_index={_INSTALL_MAX_ENTRY_TXIDX}, history_sample_count={_INSTALL_HISTORY_SAMPLES}; sorties: take_profit +100, stop dev-sell):\n{_snippet}"
                )
                print("  next:")
                print("    uv run rug_watch          # DB sniper config")
                print(
                    f'    curl -X POST http://localhost:8000/api/entity/track -H "Content-Type: application/json" -d \'{{"address": "{_addr}"}}\''
                )
                correct_cmd = f"uv run python -m rugbot.interfaces.cli.check_mint {mint} --score --install-tracker --mode {_mode}"
                if _addr != mint:
                    correct_cmd += f"  # tracker={_addr}"
                print(f"  install cmd: {correct_cmd} [--apply] [--force]")
            if bool(getattr(args, "apply", False)):
                if _abstained:
                    print(
                        "  --apply refusé (fail-closed): validation insuffisante, ajoutez --force après validation",
                        file=sys.stderr,
                    )
                else:
                    try:
                        from rugbot.runtime.config import resolve_state_dir
                        from rugbot.storage.config_store import (
                            load_sniper_config_db,
                            set_config_db,
                            sniper_to_mapping,
                        )

                        state_dir = resolve_state_dir(None)
                        try:
                            cfg = load_sniper_config_db(state_dir)
                            doc = sniper_to_mapping(cfg)
                        except Exception:
                            doc = {}
                        doc["target"] = {"kind": "wallet", "id": _addr}
                        strat = (
                            doc.get("strategy")
                            if isinstance(doc.get("strategy"), dict)
                            else {}
                        )
                        strat["history_sample_count"] = _INSTALL_HISTORY_SAMPLES
                        strat["max_entry_market_cap_quote_base_units"] = (
                            _INSTALL_MAX_ENTRY_MCAP
                        )
                        strat["max_entry_transaction_index"] = _INSTALL_MAX_ENTRY_TXIDX
                        doc["strategy"] = strat
                        rules = (
                            doc.get("rules")
                            if isinstance(doc.get("rules"), dict)
                            else {}
                        )
                        sell = (
                            rules.get("sell")
                            if isinstance(rules.get("sell"), dict)
                            else {}
                        )
                        sell["take_profit_levels"] = [
                            {
                                "trigger_pnl_ppm": _INSTALL_TP_PPM,
                                "sell_fraction_ppm": _INSTALL_TP_SELL_PPM,
                            }
                        ]
                        sell["stop_loss_levels"] = [
                            {
                                "trigger_pnl_ppm": _INSTALL_SL_PPM,
                                "sell_fraction_ppm": 1000000,
                            }
                        ]
                        rules["sell"] = sell
                        doc["rules"] = rules
                        if "execution" not in doc:
                            doc["execution"] = {
                                "mode": "observe",
                                "quote_size_lamports": 25000000,
                            }
                        if "risk" not in doc:
                            doc["risk"] = {
                                "max_buy_lamports": 25000000,
                                "max_exposure_lamports": 25000000,
                                "daily_loss_limit_lamports": 25000000,
                                "max_open_positions": 1,
                                "minimum_wallet_reserve_lamports": 15000000,
                            }
                        set_config_db(state_dir, "sniper", doc)
                        print(f"  ✓ DB sniper config written → {state_dir}/rugbot.db")
                    except Exception as exc:  # noqa: BLE001
                        print(f"  --apply failed (fail-closed): {exc}", file=sys.stderr)
    print("  live: rug_watch --stream  |  rug_live  |  rug_web")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
