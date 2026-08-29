"""Unified CLI to systematically discover cluster tokens, analyze operator patterns, and run Bible backtests."""

# ruff: noqa: C901, PLR0912, PLR0915, BLE001, ANN401, PLR2004, PTH103, PTH120, RUF059

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Final

import httpx

from rugbot.backtest.runners.cluster_optimizer import (
    HistoricalTokenSample,
    run_cluster_tp_grid_search,
)
from rugbot.intelligence.token_resolver import (
    fetch_token_metadata,
    resolve_token_or_wallet,
)
from rugbot.runtime.config import load_provider_settings, resolve_dotenv
from rugbot.storage.database import DatabaseManager
from rugbot.storage.tracker import SQLiteTrackerRepository
from rugbot.tracker.models import (
    FunderRecord,
    LaunchRecord,
    TargetExecutionMode,
    TargetExecutionPolicy,
    WalletRecord,
    WalletStatus,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

resolve_dotenv()

PUMP_PROGRAM_ID: Final[str] = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
DEFAULT_RPC_TIMEOUT_SECONDS: Final[float] = 12.0
MAX_SIBLINGS_SCANNED: Final[int] = 30
MAX_TOKENS_EVALUATED: Final[int] = 50


async def _rpc_call_async(
    client: httpx.AsyncClient,
    endpoint: str,
    method: str,
    params: list[Any],
) -> Any:
    """Execute one asynchronous JSON-RPC call with retry."""
    payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    for _ in range(3):
        try:
            resp = await client.post(
                endpoint,
                json=payload,
                timeout=DEFAULT_RPC_TIMEOUT_SECONDS,
            )
            if resp.status_code == 200:
                data = resp.json()
                if "result" in data:
                    return data["result"]
        except Exception:
            await asyncio.sleep(0.15)
    return None


async def _discover_cluster_tokens_and_wallets(
    target_wallet: str,
    root_funder: str,
    endpoint: str,
    known_mint: str | None = None,
) -> tuple[list[dict[str, Any]], set[str], str | None]:
    """Crawl the creator and root funder tree to discover all cluster tokens and staged wallets."""
    limits = httpx.Limits(max_connections=30, max_keepalive_connections=15)
    async with httpx.AsyncClient(limits=limits) as client:
        cluster_wallets: set[str] = {target_wallet, root_funder}
        funder_sigs = await _rpc_call_async(
            client, endpoint, "getSignaturesForAddress", [root_funder, {"limit": 100}]
        )
        staged_deployer: str | None = None

        if funder_sigs:
            sem = asyncio.Semaphore(15)

            async def fetch_tx(sig_info: dict[str, Any]) -> Any:
                async with sem:
                    return await _rpc_call_async(
                        client,
                        endpoint,
                        "getTransaction",
                        [
                            sig_info["signature"],
                            {
                                "encoding": "jsonParsed",
                                "maxSupportedTransactionVersion": 0,
                            },
                        ],
                    )

            tasks = [fetch_tx(s) for s in funder_sigs[:50]]
            txs = await asyncio.gather(*tasks)

            for tx in txs:
                if not tx or not tx.get("meta"):
                    continue
                keys = [
                    k.get("pubkey") if isinstance(k, dict) else k
                    for k in tx["transaction"]["message"]["accountKeys"]
                ]
                pre_bals = tx["meta"].get("preBalances", [])
                post_bals = tx["meta"].get("postBalances", [])
                if root_funder in keys:
                    f_idx = keys.index(root_funder)
                    if pre_bals[f_idx] > post_bals[f_idx]:
                        for idx, k in enumerate(keys):
                            if (
                                k != root_funder
                                and idx < len(post_bals)
                                and (post_bals[idx] - pre_bals[idx]) / 1e9 > 0.05
                            ):
                                cluster_wallets.add(k)

        # Scan each cluster wallet for token creations
        tokens_map: dict[str, dict[str, Any]] = {}
        sem_scan = asyncio.Semaphore(15)

        async def scan_single_wallet(w: str) -> None:
            nonlocal staged_deployer
            async with sem_scan:
                sigs = await _rpc_call_async(
                    client,
                    endpoint,
                    "getSignaturesForAddress",
                    [w, {"limit": 40}],
                )
                if not sigs:
                    if staged_deployer is None and w != root_funder:
                        staged_deployer = w
                    return

                if len(sigs) == 1 and staged_deployer is None and w != root_funder:
                    staged_deployer = w

                for s in sigs:
                    sig = s["signature"]
                    tx = await _rpc_call_async(
                        client,
                        endpoint,
                        "getTransaction",
                        [
                            sig,
                            {
                                "encoding": "jsonParsed",
                                "maxSupportedTransactionVersion": 0,
                            },
                        ],
                    )
                    if not tx or not tx.get("meta"):
                        continue
                    logs = tx["meta"].get("logMessages", [])
                    if any(
                        "Instruction: Create" in log or "Program log: Create" in log
                        for log in logs
                    ):
                        post_toks = tx["meta"].get("postTokenBalances", [])
                        for pt in post_toks:
                            mint = pt.get("mint")
                            if (
                                mint
                                and mint
                                != "So11111111111111111111111111111111111111112"
                                and mint not in tokens_map
                            ):
                                bt = s.get("blockTime")
                                tokens_map[mint] = {
                                    "mint": mint,
                                    "creator": w,
                                    "slot": s.get("slot"),
                                    "block_time": bt,
                                    "time": (
                                        datetime.fromtimestamp(bt, UTC).strftime(
                                            "%Y-%m-%d %H:%M:%S"
                                        )
                                        if bt
                                        else "N/A"
                                    ),
                                    "signature": sig,
                                }

        wallet_list = list(cluster_wallets)[:MAX_SIBLINGS_SCANNED]
        await asyncio.gather(*(scan_single_wallet(w) for w in wallet_list))

        if known_mint and known_mint not in tokens_map:
            tokens_map[known_mint] = {
                "mint": known_mint,
                "creator": target_wallet,
                "slot": 0,
                "block_time": int(time.time()),
                "time": datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S"),
                "signature": "",
            }

        return list(tokens_map.values()), cluster_wallets, staged_deployer


def build_arg_parser() -> argparse.ArgumentParser:
    """Build parser for rug_cluster command."""
    parser = argparse.ArgumentParser(
        prog="rug_cluster",
        description="Systematically trace cluster funding trees, discover all sibling tokens, analyze operator patterns, and run Memecoin Bible backtests.",
    )
    parser.add_argument(
        "target",
        help="Token mint address, creator wallet, or root funder address.",
    )
    parser.add_argument(
        "--size",
        type=float,
        default=0.30,
        help="Simulated entry size in SOL (default: 0.30 SOL).",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit structured machine-readable JSON output for AI/scripts.",
    )
    parser.add_argument(
        "--enroll",
        "-e",
        action="store_true",
        help="Automatically enroll qualified cluster and optimal policy into SQLite tracking DB.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Execute the cluster crawler, pattern analyzer, and Bible backtest."""
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

    args = build_arg_parser().parse_args(argv)
    target_input = args.target.strip()

    providers = load_provider_settings()
    endpoint = providers.rpc_http
    if endpoint is None:
        print("Error: SOLANA_RPC_HTTP endpoint is required.", file=sys.stderr)
        return 1

    # 1. Resolve Target
    resolved = resolve_token_or_wallet(
        target_input,
        rpc_url=endpoint,
        fallback_endpoints=providers.rpc_http_fallbacks,
    )
    creator_wallet = resolved.target_wallet
    root_funder = resolved.root_funder or creator_wallet
    known_mint = target_input if resolved.is_token else None

    # 2. Systematically Discover Cluster Tokens and Sibling Wallets
    discovered_tokens, cluster_wallets, staged_deployer = asyncio.run(
        _discover_cluster_tokens_and_wallets(
            creator_wallet,
            root_funder,
            endpoint,
            known_mint=known_mint,
        )
    )

    # 3. Build Historical Token Samples for Backtesting
    samples: list[HistoricalTokenSample] = []
    for t in discovered_tokens[:MAX_TOKENS_EVALUATED]:
        mint = t["mint"]
        name, sym, mc, ath = fetch_token_metadata(mint)
        sample = HistoricalTokenSample(
            mint=mint,
            symbol=sym or "TOKEN",
            creator_wallet=t.get("creator", creator_wallet),
            created_slot=t.get("slot") or 0,
            created_at=t.get("block_time") or int(time.time()),
            ath_multiplier=ath,
            ath_delay_seconds=45,
            rug_delay_seconds=90,
            entry_mc_usd=5000.0,
            peak_mc_usd=mc,
            is_bundle_b0=True,
            bundle_sol=0.5,
        )
        samples.append(sample)

    # 4. Run Analytical Take-Profit Grid Optimization per Memecoin Bible
    backtest = run_cluster_tp_grid_search(
        root_funder=root_funder,
        samples=samples,
        buy_size_sol=args.size,
        gas_fee_sol=0.001,
        jito_tip_sol=0.002,
    )

    # 5. Handle Enrollment if requested
    enrolled = False
    if args.enroll and backtest.is_bible_qualified:
        db_path = os.environ.get(
            "RUGBOT_DB_PATH",
            r"C:\Users\got\Documents\code\pumpfun-bonkfun-bot\data\tracker.db",
        )
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        repo = SQLiteTrackerRepository(DatabaseManager(db_path))
        now_iso = datetime.now(UTC).isoformat()

        repo.save_funder(
            FunderRecord(
                id=None,
                address=root_funder,
                label=f"Cluster {root_funder[:6]}...",
                enabled=True,
                created_at=now_iso,
                last_seen_at=now_iso,
            )
        )
        for w in cluster_wallets:
            if repo.get_wallet(w) is None:
                repo.save_wallet(
                    WalletRecord(
                        address=w,
                        root_funder=root_funder,
                        parent_wallet=root_funder,
                        depth=1 if w != root_funder else 0,
                        status=WalletStatus.FUNDED
                        if w != root_funder
                        else WalletStatus.FUNDER,
                        discovered_at=now_iso,
                        expires_at=None,
                        last_active_at=now_iso,
                    )
                )
        for t in discovered_tokens:
            if repo.get_launch(t["mint"]) is None:
                repo.save_launch(
                    LaunchRecord(
                        mint=t["mint"],
                        creator_wallet=t.get("creator", creator_wallet),
                        root_funder=root_funder,
                        symbol=t.get("mint", "")[:6],
                        name="Discovered Token",
                        created_signature=t.get("signature"),
                        created_slot=t.get("slot") or 0,
                        created_at=now_iso,
                        depth=1,
                        funding_signature=None,
                        funding_amount_lamports=None,
                        funding_timestamp=None,
                    )
                )
        optimal_tp_ppm = (
            int((backtest.optimal_tp_multiplier - 1.0) * 1_000_000)
            if backtest.optimal_tp_multiplier
            else 1_000_000
        )
        repo.save_target_execution_policy(
            TargetExecutionPolicy(
                funder_address=root_funder,
                monitoring_enabled=True,
                execution_mode=TargetExecutionMode.SIMULATED,
                quote_size_lamports=int(args.size * 1e9),
                take_profit_pnl_ppm=optimal_tp_ppm,
                stop_loss_pnl_ppm=-200_000,
                max_slippage_bps=500,
                priority_fee_microlamports=50_000,
                jito_tip_lamports=2_000_000,
                updated_at=now_iso,
            )
        )
        enrolled = True

    # 6. Structured JSON Output for AI
    if args.json:
        payload = {
            "status": "ok",
            "input": target_input,
            "is_token": resolved.is_token,
            "creator_wallet": creator_wallet,
            "root_funder": root_funder,
            "cluster_wallets_count": len(cluster_wallets),
            "cluster_tokens_count": len(discovered_tokens),
            "staged_deployer_candidate": staged_deployer,
            "tokens": discovered_tokens,
            "operator_patterns": {
                "avg_ath_multiplier": backtest.avg_ath_multiplier,
                "median_ath_multiplier": backtest.median_ath_multiplier,
                "ath_consistency_pct": backtest.ath_consistency_pct,
                "avg_rug_delay_seconds": backtest.avg_rug_delay_seconds,
                "avg_inter_launch_minutes": backtest.avg_inter_launch_minutes,
                "avg_peak_mc_usd": backtest.avg_peak_mc_usd,
            },
            "backtest": {
                "total_tokens_evaluated": backtest.total_tokens_evaluated,
                "optimal_tp": backtest.optimal_tp_label,
                "optimal_tp_multiplier": backtest.optimal_tp_multiplier,
                "optimal_roi_pct": backtest.optimal_roi_pct,
                "optimal_net_ev_sol": backtest.optimal_net_ev_sol,
                "is_net_profitable": backtest.is_net_profitable,
                "is_bible_qualified": backtest.is_bible_qualified,
                "qualification_reason": backtest.qualification_reason,
            },
            "enrolled": enrolled,
        }
        print(json.dumps(payload, indent=2))
        return 0

    # 7. Pretty Terminal Output
    print("\n" + "=" * 78)
    print(" 🌐 RUGBOT CLUSTER & BIBLE PATTERN INTELLIGENCE")
    print("=" * 78)
    print(f" Input:                  {target_input}")
    print(f" Creator Wallet:         {creator_wallet}")
    print(f" Root Funder / Treasury: {root_funder}")
    print(f" Sibling Wallets Found:  {len(cluster_wallets)}")
    print(f" Total Tokens Discovered:{len(discovered_tokens)}")
    if staged_deployer:
        print(f" 🔥 STAGED CLEAN BURNER: {staged_deployer} (Awaiting launch)")
    print("-" * 78)

    print("\n 🏭 DISCOVERED CLUSTER TOKENS:")
    for idx, tok in enumerate(discovered_tokens[:12], start=1):
        print(
            f"   {idx:>2}. Mint: {tok['mint']} | Creator: {tok['creator'][:8]}... | Time: {tok['time']}"
        )
    if len(discovered_tokens) > 12:
        print(f"   ... and {len(discovered_tokens) - 12} more tokens.")

    print("\n" + "-" * 78)
    print(f" 🕒 OPERATOR DYNAMICS ({backtest.total_tokens_evaluated} Launches):")
    print(
        f"   • Average ATH:          {backtest.avg_ath_multiplier:.2f}x (Median: {backtest.median_ath_multiplier:.2f}x)"
    )
    print(f"   • ATH Consistency:      {backtest.ath_consistency_pct:.1f}%")
    print(f"   • Avg Time to Rug:      {backtest.avg_rug_delay_seconds:.0f}s")
    print(f"   • Avg Peak MC:          ${backtest.avg_peak_mc_usd:,.0f}")
    if backtest.avg_inter_launch_minutes > 0:
        print(
            f"   • Launch Cadence:       ~{backtest.avg_inter_launch_minutes:.0f}m between tokens"
        )

    print("\n" + "-" * 78)
    print(
        f" 📊 BIBLE BACKTEST OPTIMIZER ({backtest.total_tokens_evaluated} Launches Evaluated):"
    )
    print(
        f"   • Optimal Take-Profit:  {backtest.optimal_tp_label} ({backtest.optimal_tp_multiplier}x)"
    )
    print(f"   • Net Simulated ROI:    {backtest.optimal_roi_pct:+.1f}%")
    print(f"   • Expected Value (EV):  {backtest.optimal_net_ev_sol:+.4f} SOL / trade")
    print(
        f"   • Bible Qualified:      {'✅ YES' if backtest.is_bible_qualified else '❌ NO'} ({backtest.qualification_reason})"
    )

    if enrolled:
        print("\n" + "=" * 78)
        print("  TARGET AND OPTIMAL POLICY ENROLLED IN TRACKER DATABASE")
        print("=" * 78)
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
