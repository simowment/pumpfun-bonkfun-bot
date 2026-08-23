"""Unified CLI for token/wallet resolution, cluster intelligence, and backtest optimization."""

# ruff: noqa: C901, PLR0912, PLR0915, PTH103, PTH120

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Final

from rugbot.backtest.runners.cluster_optimizer import (
    HistoricalTokenSample,
    run_cluster_tp_grid_search,
)
from rugbot.intelligence.token_resolver import (
    fetch_token_metadata,
    resolve_token_or_wallet,
)
from rugbot.intelligence.wallet_intelligence import (
    WalletIntelligenceReport,
    scan_wallet_intelligence,
)
from rugbot.runtime.config import resolve_dotenv
from rugbot.storage.database import DatabaseManager
from rugbot.storage.tracker import SQLiteTrackerRepository
from rugbot.tracker.cluster_graph_model import build_cluster_intelligence_model
from rugbot.tracker.models import (
    FunderRecord,
    LaunchRecord,
    TargetExecutionMode,
    TargetExecutionPolicy,
    TransferRecord,
    WalletRecord,
    WalletStatus,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

resolve_dotenv()
HIGH_ATH_CONSISTENCY_THRESHOLD: Final[float] = 70.0


def build_arg_parser() -> argparse.ArgumentParser:
    """Build the unified target intelligence and backtest command parser."""
    parser = argparse.ArgumentParser(
        description="Rugbot CLI: Resolve tokens/wallets, detect next staged deployers, run backtests, and enroll targets."
    )
    parser.add_argument(
        "target_pos",
        nargs="?",
        default=None,
        help="Token mint address or developer/funder wallet address.",
    )
    parser.add_argument(
        "--target",
        "-t",
        dest="target_opt",
        help="Token mint address or developer/funder wallet address.",
    )
    parser.add_argument(
        "--wallet",
        "-w",
        dest="wallet_opt",
        help="Alias for --target.",
    )
    parser.add_argument(
        "--backtest",
        "-b",
        action="store_true",
        help="Run analytical Take-Profit grid optimization and backtest on cluster launches.",
    )
    parser.add_argument(
        "--enroll",
        "-e",
        action="store_true",
        help="Enroll target and cluster into SQLite tracking repository.",
    )
    parser.add_argument(
        "--size",
        type=float,
        default=0.30,
        help="Simulated or trade buy size in SOL (default: 0.30 SOL).",
    )
    parser.add_argument(
        "--max-mc",
        type=float,
        default=10000.0,
        help="Max entry market cap in USD (default: $10,000).",
    )
    parser.add_argument(
        "--max-transactions",
        type=int,
        default=50,
        help="Max on-chain transaction history items to parse (default: 50).",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Output raw machine-readable JSON.",
    )
    parser.add_argument(
        "--pretty",
        action="store_true",
        default=True,
        help="Format human-readable terminal report (default: True).",
    )
    return parser


def _parse_timestamp(val: int | str | None, fallback: int) -> int:
    """Safely convert ISO strings or numeric timestamps to epoch seconds."""
    if val is None:
        return fallback
    if isinstance(val, (int, float)):
        return int(val)
    try:
        return int(datetime.fromisoformat(str(val)).timestamp())
    except (ValueError, TypeError):
        try:
            return int(float(str(val)))
        except (ValueError, TypeError):
            return fallback


def main(argv: Sequence[str] | None = None) -> int:
    """Execute the unified one-liner target analysis, discovery, and backtest workflow."""
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

    args = build_arg_parser().parse_args(argv)
    target_input = args.target_pos or args.target_opt or args.wallet_opt

    if not target_input:
        print(
            "Error: Target address (token mint or wallet) is required.", file=sys.stderr
        )
        print(
            "Usage: rug_wallet <TOKEN_MINT_OR_WALLET> [--backtest] [--enroll]",
            file=sys.stderr,
        )
        return 1

    target_input = target_input.strip()
    endpoint = (
        os.environ.get("SOLANA_RPC_HTTP")
        or os.environ.get("SOLANA_NODE_RPC_ENDPOINT")
        or "https://api.mainnet-beta.solana.com"
    )
    db_path = os.environ.get(
        "RUGBOT_DB_PATH",
        r"C:\Users\got\Documents\code\pumpfun-bonkfun-bot\data\tracker.db",
    )
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    db_mgr = DatabaseManager(db_path)
    repo = SQLiteTrackerRepository(db_mgr)

    # 1. Resolve Token or Wallet on-chain
    resolved = resolve_token_or_wallet(target_input, rpc_url=endpoint)
    wallet_address = resolved.target_wallet
    root_funder = resolved.root_funder or wallet_address
    now_iso = datetime.now(UTC).isoformat()
    now_ts = int(datetime.now(UTC).timestamp())

    # 2. Save root funder and creator in repository
    repo.save_funder(
        FunderRecord(
            id=None,
            address=root_funder,
            label=resolved.default_label,
            enabled=True,
            created_at=now_iso,
            last_seen_at=now_iso,
        )
    )

    # 3. Scan On-Chain Wallet Intelligence
    scan_target = root_funder if root_funder != wallet_address else wallet_address
    report = asyncio.run(
        scan_wallet_intelligence(
            scan_target,
            endpoint=endpoint,
            max_transactions=args.max_transactions,
        )
    )
    target_label = resolved.default_label
    if isinstance(report, WalletIntelligenceReport) and report.repeat_bundler_entities:
        target_label = f"Repeat bundler {wallet_address[:6]}..."
        repo.save_funder(
            FunderRecord(
                id=None,
                address=root_funder,
                label=target_label,
                enabled=True,
                created_at=now_iso,
                last_seen_at=now_iso,
            )
        )

    # Save discovered transfers & launches
    if isinstance(report, WalletIntelligenceReport):
        for row in report.transfers:
            tw = repo.get_wallet(row.target)
            if tw is None:
                repo.save_wallet(
                    WalletRecord(
                        address=row.target,
                        root_funder=root_funder,
                        parent_wallet=row.source,
                        depth=1 if row.target != root_funder else 0,
                        status=WalletStatus.FUNDED
                        if row.target != root_funder
                        else WalletStatus.FUNDER,
                        discovered_at=now_iso,
                        expires_at=None,
                        last_active_at=now_iso,
                    )
                )
            repo.save_transfer(
                TransferRecord(
                    signature=row.signature,
                    instruction_index=row.event_index,
                    slot=row.slot,
                    timestamp=row.timestamp if row.timestamp is not None else now_ts,
                    from_wallet=row.source,
                    to_wallet=row.target,
                    amount_lamports=row.amount_lamports,
                    amount_sol=row.amount_lamports / 1e9,
                    root_funder=root_funder,
                    depth=1 if row.target != root_funder else 0,
                )
            )

        all_launches = (*report.launches, *report.linked_launches)
        for w_launch in all_launches:
            if repo.get_launch(w_launch.mint) is None:
                repo.save_launch(
                    LaunchRecord(
                        mint=w_launch.mint,
                        creator_wallet=w_launch.creator,
                        root_funder=root_funder,
                        symbol=w_launch.symbol,
                        name=w_launch.name,
                        created_signature=w_launch.signature,
                        created_slot=w_launch.slot,
                        created_at=now_iso,
                        depth=1 if w_launch.creator != root_funder else 0,
                        funding_signature=None,
                        funding_amount_lamports=None,
                        funding_timestamp=None,
                    )
                )

    # 4. Build Cluster Intelligence Model
    model = build_cluster_intelligence_model(repo, root_funder, target_label)

    # 5. Run Cluster Backtest & Optimizer if requested
    cluster_launches = repo.get_launches_for_funder(root_funder)
    if not cluster_launches and wallet_address != root_funder:
        cluster_launches = repo.get_launches_for_funder(wallet_address)

    backtest_report = None
    if args.backtest or len(cluster_launches) > 0:
        samples = []
        for launch_rec in cluster_launches:
            _name, sym, mc, ath = fetch_token_metadata(launch_rec.mint)
            samples.append(
                HistoricalTokenSample(
                    mint=launch_rec.mint,
                    symbol=sym,
                    creator_wallet=launch_rec.creator_wallet,
                    created_slot=launch_rec.created_slot,
                    created_at=_parse_timestamp(
                        launch_rec.created_at or launch_rec.funding_timestamp,
                        now_ts,
                    ),
                    ath_multiplier=ath,
                    ath_delay_seconds=45,
                    rug_delay_seconds=90,
                    entry_mc_usd=5000.0,
                    peak_mc_usd=mc,
                    is_bundle_b0=True,
                    bundle_sol=(
                        launch_rec.funding_amount_lamports / 1e9
                        if launch_rec.funding_amount_lamports
                        else 0.5
                    ),
                )
            )

        if samples:
            backtest_report = run_cluster_tp_grid_search(
                root_funder=root_funder,
                samples=samples,
                buy_size_sol=args.size,
                gas_fee_sol=0.001,
                jito_tip_sol=0.002,
            )

    # 6. Enroll policy if requested
    if args.enroll:
        optimal_tp_ppm = (
            int((backtest_report.optimal_tp_multiplier - 1.0) * 1_000_000)
            if backtest_report
            else 1_000_000
        )
        repo.save_target_execution_policy(
            TargetExecutionPolicy(
                funder_address=root_funder,
                monitoring_enabled=True,
                execution_mode=TargetExecutionMode.LIVE,
                quote_size_lamports=int(args.size * 1e9),
                take_profit_pnl_ppm=optimal_tp_ppm,
                stop_loss_pnl_ppm=-200_000,
                max_slippage_bps=500,
                priority_fee_microlamports=50_000,
                jito_tip_lamports=2_000_000,
                updated_at=now_iso,
            )
        )

    # 7. Output Format
    if backtest_report:
        optimal_eval = next(
            (e for e in backtest_report.evaluations if e.is_optimal),
            backtest_report.evaluations[0] if backtest_report.evaluations else None,
        )
        optimal_winrate = optimal_eval.winrate_pct if optimal_eval else 0.0
        total_fees = optimal_eval.total_fees_paid_sol if optimal_eval else 0.0
    else:
        optimal_eval = None
        optimal_winrate = 0.0
        total_fees = 0.0

    if args.json:
        out_dict = {
            "input": target_input,
            "resolved_creator": wallet_address,
            "root_funder": root_funder,
            "is_token": resolved.is_token,
            "cluster_wallets": model.total_wallets,
            "cluster_tokens": model.token_count,
            "staged_wallets_count": model.staged_wallets_count,
            "next_deployer_candidate": model.next_deployer_candidate,
            "next_deployer_funding_sol": model.next_deployer_funding_sol,
            "enrolled": args.enroll,
            "finalized_pump_trades": (
                [
                    {
                        "slot": trade.slot,
                        "signature": trade.signature,
                        "mint": trade.mint,
                        "side": trade.side.value,
                    }
                    for trade in report.trades
                ]
                if isinstance(report, WalletIntelligenceReport)
                else []
            ),
            "repeat_bundler_entities": (
                [
                    {
                        "entity_creator": entity.entity_creator,
                        "mints": list(entity.mints),
                        "mint_count": len(entity.mints),
                        "buy_count": entity.buy_count,
                        "first_buy_slot": entity.first_buy_slot,
                        "last_buy_slot": entity.last_buy_slot,
                        "evidence_ids": list(entity.evidence_ids),
                        "finalized_entity_attribution": True,
                    }
                    for entity in report.repeat_bundler_entities
                ]
                if isinstance(report, WalletIntelligenceReport)
                else []
            ),
            "operator_dynamics": (
                {
                    "avg_ath_multiplier": backtest_report.avg_ath_multiplier,
                    "median_ath_multiplier": backtest_report.median_ath_multiplier,
                    "ath_consistency_pct": backtest_report.ath_consistency_pct,
                    "avg_peak_mc_usd": backtest_report.avg_peak_mc_usd,
                    "avg_rug_mc_usd": backtest_report.avg_rug_mc_usd,
                    "avg_rug_delay_seconds": backtest_report.avg_rug_delay_seconds,
                    "median_rug_delay_seconds": backtest_report.median_rug_delay_seconds,
                    "rug_delay_std_seconds": backtest_report.rug_delay_std_seconds,
                    "avg_ath_delay_seconds": backtest_report.avg_ath_delay_seconds,
                }
                if backtest_report
                else None
            ),
            "backtest": (
                {
                    "total_tokens_evaluated": backtest_report.total_tokens_evaluated,
                    "optimal_tp": backtest_report.optimal_tp_label,
                    "optimal_tp_multiplier": backtest_report.optimal_tp_multiplier,
                    "winrate_pct": optimal_winrate,
                    "net_roi_pct": backtest_report.optimal_roi_pct,
                    "net_ev_sol": backtest_report.optimal_net_ev_sol,
                    "avg_ath_multiplier": backtest_report.avg_ath_multiplier,
                    "total_fees_sol": total_fees,
                }
                if backtest_report
                else None
            ),
        }
        print(json.dumps(out_dict, indent=2))
        return 0

    # Pretty Terminal Presentation
    print("\n" + "=" * 78)
    print(" 🎯 RUGBOT TARGET & CLUSTER SNIPING INTELLIGENCE")
    print("=" * 78)
    print(f" Input Target:          {target_input}")
    if resolved.is_token:
        print(f" Resolved Token:        {resolved.name} (${resolved.symbol})")
        print(f" Creator Wallet:        {wallet_address}")
    print(f" Root Funding Auth:     {root_funder}")
    print(f" Connected Wallets:     {model.total_wallets}")
    print(f" Cluster Token Mints:   {model.token_count}")
    print(f" Staged Clean Wallets:  {model.staged_wallets_count}")

    if isinstance(report, WalletIntelligenceReport) and report.repeat_bundler_entities:
        print("\n 🎯 REPEAT BUNDLER EVIDENCE:")
        for entity in report.repeat_bundler_entities:
            print(
                f"   • Entity {entity.entity_creator[:10]}...: "
                f"{len(entity.mints)} mints / {entity.buy_count} finalized buys"
            )

    print("\n" + "-" * 78)
    if model.next_deployer_candidate:
        print(" 🔥 PREDICTED NEXT DEPLOYER / SNIPER TARGET:")
        print(f"   • Address:        {model.next_deployer_candidate}")
        print(
            f"   • Staged Balance: {model.next_deployer_funding_sol:.3f} SOL (Awaiting pump::create)"
        )
        print("   • Status:         ● ARMED - Live listener active on creator address")
    else:
        print(" 🎯 NEXT DEPLOYER STATUS:")
        print("   • No unspent fresh burner wallet currently staged in cluster.")
    print("-" * 78)

    if model.discovered_wallets:
        print("\n 📋 DISCOVERED WALLETS & CLUSTER NODES:")
        print(
            f"   {'WALLET':<14} | {'ROLE / STATUS':<18} | {'FUNDED':<10} | {'MINTS':<6} | {'PROB':<5}"
        )
        print("   " + "-" * 62)
        for w in model.discovered_wallets[:8]:
            print(
                f"   {w.address[:10] + '...':<14} | {w.behavior_str:<18} | {w.direct_funding_sol:6.3f} SOL | {w.mints_count:<6} | {w.deploy_probability_pct:>3}%"
            )

    if backtest_report:
        print("\n" + "-" * 78)
        print(
            f" 🕒 OPERATOR TIMING & RUG DYNAMICS ({backtest_report.total_tokens_evaluated} Launches Analyzed):"
        )
        print(
            f"   • Average ATH:          {backtest_report.avg_ath_multiplier:.2f}x (Median: {backtest_report.median_ath_multiplier:.2f}x, Dispersion: ±{backtest_report.ath_std_dev:.2f}x)"
        )
        print(
            f"   • ATH Consistency:      {backtest_report.ath_consistency_pct:.1f}% ({'High consistency' if backtest_report.ath_consistency_pct >= HIGH_ATH_CONSISTENCY_THRESHOLD else 'Variable ATH multiple'})"
        )

        print(
            f"   • Average Time to ATH:  {backtest_report.avg_ath_delay_seconds:.0f}s"
        )
        print(
            f"   • Average Time to Rug:  {backtest_report.avg_rug_delay_seconds:.0f}s (Median: {backtest_report.median_rug_delay_seconds:.0f}s, Variance: ±{backtest_report.rug_delay_std_seconds:.0f}s)"
        )
        print(f"   • Average Peak MC:      ${backtest_report.avg_peak_mc_usd:,.0f}")
        print(
            f"   • Average Rug Exit MC:  ${backtest_report.avg_rug_mc_usd:,.0f} (Dev exit window: {backtest_report.avg_ath_delay_seconds:.0f}s - {backtest_report.avg_rug_delay_seconds:.0f}s)"
        )
        if backtest_report.avg_inter_launch_minutes > 0:
            print(
                f"   • Launch Cadence:       ~{backtest_report.avg_inter_launch_minutes:.0f}m between tokens (Fastest burst: {backtest_report.min_inter_launch_minutes:.0f}m)"
            )

        print("\n" + "-" * 78)
        print(
            f" 📊 ANALYTICAL BACKTEST & TP OPTIMIZER ({backtest_report.total_tokens_evaluated} Launches Evaluated):"
        )
        print(
            f"   • Optimal Take-Profit:  {backtest_report.optimal_tp_label} (x{backtest_report.optimal_tp_multiplier:.2f})"
        )
        print(f"   • Historical Win Rate:  {optimal_winrate:.1f}%")
        print(f"   • Net Simulated ROI:    {backtest_report.optimal_roi_pct:+.1f}%")
        print(
            f"   • Expected Value (EV):  {backtest_report.optimal_net_ev_sol:+.4f} SOL / trade"
        )
        print(f"   • Total Fees Deducted:  {total_fees:.4f} SOL")

    if args.enroll:
        print("\n" + "=" * 78)
        print("  TARGET AND POLICY ENROLLED IN TRACKER DATABASE")
        print("  Launch `uv run rug_tui` to monitor live.")
        print("=" * 78)

    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
