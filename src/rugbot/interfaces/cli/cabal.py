"""Unified CLI for on-chain insider cabal intelligence and copytrading.

Commands:
  discover - Reverse-engineers winners, extracts early buyers, clusters by funding origin, and persists.
  list     - Instant (<1s) local store query of discovered cabal clusters and performance metrics.
  dryrun   - Real-time WebSocket/RPC monitoring, confluence signal gating, and paper execution.
  backtest - Historical candlestick replay against Pump.fun tokens.
  trades   - Bot execution history, winrate, PnL, and copyable mints.
"""

# ruff: noqa: ERA001, C901, PLR0915, PLR2004, PLR0911, ANN401, PLR0912, BLE001

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from datetime import UTC, datetime
from typing import Any

from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from rugbot.discover.cabal import (
    DEFAULT_EARLY_BUYER_LIMIT,
    DEFAULT_MIN_ATH_MCAP,
    DEFAULT_MIN_WINNERS,
    DEFAULT_STORE_PATH,
    CabalStore,
    cluster_early_buyers,
    extract_early_buyers,
    fetch_cabal_wallet_activity,
    fetch_winner_tokens,
)
from rugbot.execution.cabal_executor import CabalExecutor
from rugbot.integrations.pumpfun_api import get_client
from rugbot.intelligence.signal_filter import SignalFilter, SignalFilterConfig
from rugbot.reporting.stats_sheet import (
    TokenStatsSheet,
    TokenTradeStatRow,
    copy_to_clipboard,
)
from rugbot.runtime.cabal_pipeline import (
    CabalPipeline,
    PositionSizingConfig,
    SizingMode,
)
from rugbot.runtime.config import resolve_dotenv
from rugbot.utils.logger import get_logger

logger = get_logger(__name__)


def build_parser() -> argparse.ArgumentParser:
    """Build the argument parser for cabal CLI."""
    parser = argparse.ArgumentParser(
        prog="cabal",
        description="On-chain insider cabal discovery, monitoring, and paper execution.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    # Subcommand: discover
    p_discover = subparsers.add_parser(
        "discover",
        help="Discover insider cabals by backtracing early buyers of winner tokens.",
    )
    p_discover.add_argument(
        "--min-winners",
        type=int,
        default=DEFAULT_MIN_WINNERS,
        help="Minimum number of winner tokens a wallet must appear early on (default: 2).",
    )
    p_discover.add_argument(
        "--top-buyers",
        type=int,
        default=DEFAULT_EARLY_BUYER_LIMIT,
        help="Earliest N unique buyers to extract per token (default: 25).",
    )
    p_discover.add_argument(
        "--min-mcap",
        type=float,
        default=DEFAULT_MIN_ATH_MCAP,
        help="Minimum ATH market cap in USD to qualify as a winner (default: 50000).",
    )
    p_discover.add_argument(
        "--limit-winners",
        type=int,
        default=30,
        help="Maximum winner tokens to inspect (default: 30).",
    )
    p_discover.add_argument(
        "--db",
        type=str,
        default=str(DEFAULT_STORE_PATH),
        help="SQLite database path for durable cluster persistence.",
    )
    p_discover.add_argument(
        "--enroll",
        action="store_true",
        default=True,
        help="Enroll discovered clusters into copytrade registry and tracker repository (default: True).",
    )
    p_discover.add_argument(
        "--json", action="store_true", help="Output results as JSON."
    )

    # Subcommand: list
    p_list = subparsers.add_parser(
        "list",
        help="List all persisted cabal clusters and their performance measurements.",
    )
    p_list.add_argument(
        "--db",
        type=str,
        default=str(DEFAULT_STORE_PATH),
        help="SQLite database path.",
    )
    p_list.add_argument("--json", action="store_true", help="Output results as JSON.")

    # Subcommand: dryrun
    p_dryrun = subparsers.add_parser(
        "dryrun",
        help="Execute live paper trading simulation: stream real-time Solana transactions, apply confluence filters, and simulate copytrades with live balance tracking.",
    )
    p_dryrun.add_argument(
        "--db",
        type=str,
        default=str(DEFAULT_STORE_PATH),
        help="SQLite database path.",
    )
    p_dryrun.add_argument(
        "--discover",
        action="store_true",
        default=False,
        help="Run discovery of top cabal clusters before starting dry run (chains discover -> track -> copytrade).",
    )
    p_dryrun.add_argument(
        "--limit-winners",
        type=int,
        default=30,
        help="Maximum winner tokens to inspect during auto-discovery (default: 30).",
    )
    p_dryrun.add_argument(
        "--min-winners",
        type=int,
        default=DEFAULT_MIN_WINNERS,
        help="Minimum winner tokens an early buyer must appear in to form a cluster (default: 2).",
    )
    p_dryrun.add_argument(
        "--early-limit",
        type=int,
        default=DEFAULT_EARLY_BUYER_LIMIT,
        help="Earliest N unique buyers to extract per token during auto-discovery (default: 25).",
    )
    p_dryrun.add_argument(
        "--min-mcap",
        type=float,
        default=DEFAULT_MIN_ATH_MCAP,
        help="Minimum ATH market cap in USD to qualify as a winner during auto-discovery (default: 50000).",
    )
    p_dryrun.add_argument(
        "--seconds",
        type=int,
        default=60,
        help="Monitoring window in seconds (0 for indefinite, default: 60).",
    )
    p_dryrun.add_argument(
        "--observe",
        action="store_true",
        default=True,
        help="Run in observe/paper mode (default: True).",
    )
    p_dryrun.add_argument(
        "--profitable",
        action="store_true",
        default=True,
        help="Enforce +59%% Net EV settings: multi-wallet confluence >= 2, min buy 0.1 SOL, exclude dust clusters (default: True).",
    )
    p_dryrun.add_argument(
        "--min-buy-sol",
        type=float,
        default=0.10,
        help="Minimum buy size in SOL to trigger paper copytrade (default: 0.10).",
    )
    p_dryrun.add_argument(
        "--min-cluster-buy",
        type=float,
        default=0.10,
        help="Minimum cluster historical typical buy in SOL to avoid dust sprayers (default: 0.10).",
    )
    p_dryrun.add_argument(
        "--require-confluence",
        action="store_true",
        default=True,
        help="Require >= 2 wallets from cluster to co-buy within 30s window (default: True).",
    )
    p_dryrun.add_argument(
        "--tp2x",
        type=float,
        default=0.50,
        help="Fraction of position to sell at 2.0x (+100%%) to secure 100%% principal (default: 0.50).",
    )
    p_dryrun.add_argument(
        "--tp5x",
        type=float,
        default=0.25,
        help="Fraction of position to sell at 5.0x (+400%%) (default: 0.25).",
    )
    p_dryrun.add_argument(
        "--trail",
        type=float,
        default=15.0,
        help="Trailing stop loss percent drop from high-water mark (default: 15.0).",
    )
    p_dryrun.add_argument(
        "--size-mode",
        type=str,
        choices=["fixed", "proportional", "balance_pct"],
        default="fixed",
        help="Position sizing strategy: fixed, proportional, or balance_pct (default: fixed).",
    )
    p_dryrun.add_argument(
        "--size-sol",
        type=float,
        default=0.25,
        help="Fixed trade size in SOL when --size-mode=fixed (default: 0.25).",
    )
    p_dryrun.add_argument(
        "--copy-ratio",
        type=float,
        default=0.50,
        help="Fraction of insider's buy size when --size-mode=proportional (default: 0.50).",
    )
    p_dryrun.add_argument(
        "--balance-pct",
        type=float,
        default=10.0,
        help="Percentage of available balance per trade when --size-mode=balance_pct (default: 10.0).",
    )
    p_dryrun.add_argument(
        "--paper-balance",
        type=float,
        default=2.00,
        help="Initial simulated paper trading balance in SOL (default: 2.00).",
    )
    p_dryrun.add_argument(
        "--max-size-sol",
        type=float,
        default=1.00,
        help="Maximum cap in SOL per trade across all modes (default: 1.00).",
    )
    p_dryrun.add_argument(
        "--min-size-sol",
        type=float,
        default=0.05,
        help="Minimum floor in SOL per trade to cover gas and fees (default: 0.05).",
    )

    # Subcommand: backtest
    p_backtest = subparsers.add_parser(
        "backtest",
        help="Run empirical backtest against real historical Pump.fun candlestick data for cabal tokens.",
    )
    p_backtest.add_argument(
        "--mint",
        type=str,
        default="",
        help="Specific token mint address to backtest (default: top tokens from discovered clusters).",
    )
    p_backtest.add_argument(
        "--cluster",
        type=str,
        default="",
        help="Specific cabal cluster ID to backtest (e.g. cabal-001).",
    )
    p_backtest.add_argument(
        "--limit",
        type=int,
        default=10,
        help="Maximum number of historical tokens to backtest (default: 10).",
    )
    p_backtest.add_argument(
        "--tp2x",
        type=float,
        default=0.50,
        help="Fraction of position to sell at 2.0x (+100%%) (default: 0.50).",
    )
    p_backtest.add_argument(
        "--tp5x",
        type=float,
        default=0.25,
        help="Fraction of position to sell at 5.0x (+400%%) (default: 0.25).",
    )
    p_backtest.add_argument(
        "--trail",
        type=float,
        default=15.0,
        help="Trailing stop loss percent drop from high-water mark (default: 15.0).",
    )
    p_backtest.add_argument(
        "--size-sol",
        type=float,
        default=0.25,
        help="Fixed trade size in SOL per token backtest (default: 0.25).",
    )
    p_backtest.add_argument(
        "--paper-balance",
        type=float,
        default=2.00,
        help="Starting paper trading balance in SOL (default: 2.00).",
    )
    p_backtest.add_argument(
        "--interval",
        type=str,
        default="1m",
        choices=["1s", "15s", "30s", "1m", "5m"],
        help="Candlestick timeframe interval to backtest (default: 1m).",
    )
    p_backtest.add_argument(
        "--parquet",
        type=str,
        nargs="?",
        const=".state/reports/cabal_backtest.parquet",
        default="",
        help="Export backtest performance sheet to Apache Parquet file (default: .state/reports/cabal_backtest.parquet).",
    )
    p_backtest.add_argument(
        "--csv",
        type=str,
        default="",
        help="Export backtest sheet to CSV file path.",
    )
    p_backtest.add_argument(
        "--full-mint",
        action="store_true",
        default=False,
        help="Show full 44-character mint address in the main table.",
    )
    p_backtest.add_argument(
        "--copy",
        dest="copy_mint",
        type=int,
        default=0,
        help="Copy the token mint at given row index (1-based) directly to system clipboard.",
    )
    p_backtest.add_argument(
        "--mints",
        action="store_true",
        default=False,
        help="Output only the raw list of token mint addresses (one per line, copyable/pipable).",
    )
    p_backtest.add_argument(
        "--no-mints-panel",
        action="store_true",
        default=False,
        help="Hide the copyable mints panel in Rich view.",
    )
    p_backtest.add_argument(
        "--plain",
        action="store_true",
        default=False,
        help="Render simple monospace ASCII table instead of Rich terminal formatting.",
    )
    p_backtest.add_argument(
        "--json",
        action="store_true",
        default=False,
        help="Output backtest results as JSON.",
    )
    p_backtest.add_argument(
        "--db",
        type=str,
        default=str(DEFAULT_STORE_PATH),
        help="SQLite database path.",
    )

    # Subcommand: trades
    p_trades = subparsers.add_parser(
        "trades",
        help="View bot trade executions, winrate, PnL, copyable mints, or tracked cabal on-chain activity.",
    )
    p_trades.add_argument(
        "--cabal",
        action="store_true",
        default=False,
        help="Display recent on-chain transactions and trades executed by tracked cabal wallets.",
    )
    p_trades.add_argument(
        "--wallet",
        type=str,
        default="",
        help="Specific wallet address to inspect on-chain activity for.",
    )
    p_trades.add_argument(
        "--limit",
        type=int,
        default=20,
        help="Maximum records to display (default: 20).",
    )
    p_trades.add_argument(
        "--paper-balance",
        type=float,
        default=2.00,
        help="Initial simulated paper trading balance in SOL for portfolio tracking (default: 2.00).",
    )
    p_trades.add_argument(
        "--csv",
        type=str,
        default="",
        help="Export execution trades to CSV file path.",
    )
    p_trades.add_argument(
        "--parquet",
        type=str,
        nargs="?",
        const=".state/reports/bot_stats.parquet",
        default="",
        help="Export execution trades to Apache Parquet file (default: .state/reports/bot_stats.parquet).",
    )
    p_trades.add_argument(
        "--copy",
        dest="copy_mint",
        type=int,
        default=0,
        help="Copy the token mint at given row index (1-based) directly to system clipboard.",
    )
    p_trades.add_argument(
        "--mints",
        action="store_true",
        default=False,
        help="Output only the raw list of token mint addresses (one per line, copyable/pipable).",
    )
    p_trades.add_argument(
        "--full-mint",
        action="store_true",
        default=False,
        help="Show full 44-character mint address in the main table without truncation.",
    )
    p_trades.add_argument(
        "--plain",
        action="store_true",
        default=False,
        help="Render simple monospace ASCII table instead of Rich terminal formatting.",
    )
    p_trades.add_argument(
        "--no-mints-panel",
        action="store_true",
        default=False,
        help="Hide the copyable mints panel in Rich view.",
    )
    p_trades.add_argument(
        "--db",
        type=str,
        default=str(DEFAULT_STORE_PATH),
        help="SQLite database path.",
    )
    p_trades.add_argument(
        "--json",
        action="store_true",
        help="Output results as JSON.",
    )

    return parser


def run_discover(args: argparse.Namespace) -> int:
    """Execute the winner-backtracing discovery workflow."""
    resolve_dotenv()
    client = get_client()
    store = CabalStore(db_path=args.db)

    print(
        f"Scanning for winner tokens with ATH market cap >= ${args.min_mcap:,.0f}...",
        flush=True,
    )
    winners = fetch_winner_tokens(
        client, min_ath_mcap=args.min_mcap, limit=args.limit_winners
    )
    print(f"Found {len(winners)} qualified winner tokens.\n", flush=True)

    if not winners:
        print("No winner tokens matched criteria.")
        return 0

    winner_early_buyers = []
    print("Extracting early buyers from trade history...", flush=True)
    for w in winners:
        mint = w.get("mint", "")
        symbol = w.get("symbol", "UNKNOWN")
        ath_mc = float(w.get("ath_market_cap") or w.get("usd_market_cap") or 0.0)
        buyers = extract_early_buyers(client, mint, buyer_limit=args.top_buyers)
        print(
            f"  {symbol:<10} ({mint[:8]}...): {len(buyers)} early buyers, ATH mcap ${ath_mc:,.0f}",
            flush=True,
        )
        winner_early_buyers.append((mint, buyers))

    print(
        "\nCross-referencing multi-winner wallets and clustering by funding origin...",
        flush=True,
    )
    clusters = cluster_early_buyers(
        winner_early_buyers,
        min_winners=args.min_winners,
        client=client,
    )

    if not clusters:
        print("No repeat early buyers met the multi-winner threshold.")
        return 0

    enroll = getattr(args, "enroll", True)
    store.save_clusters(clusters, sync_stores=enroll)
    if enroll:
        print(
            f"\nPersisted {len(clusters)} Cabal Clusters to {args.db} and enrolled into stores.\n",
            flush=True,
        )
    else:
        print(f"\nPersisted {len(clusters)} Cabal Clusters to {args.db}.\n", flush=True)

    if args.json:
        print(json.dumps([c.to_dict() for c in clusters], indent=2))
        return 0

    print_clusters_table(clusters)
    return 0


def run_list(args: argparse.Namespace) -> int:
    """Read and display clusters from local SQLite store (<1s)."""
    store = CabalStore(db_path=args.db)
    clusters = store.list_clusters()

    if not clusters:
        print(f"No cabal clusters found in {args.db}. Run 'rug_cabal discover' first.")
        return 0

    if args.json:
        print(json.dumps([c.to_dict() for c in clusters], indent=2))
        return 0

    print_clusters_table(clusters)
    return 0


def print_clusters_table(clusters: list[Any]) -> None:
    """Format and print an aligned terminal report for cabal clusters."""
    header = (
        f"{'Cabal ID':<20} | {'Funder':<10} | {'Wallets':<7} | {'Tokens (N)':<10} | "
        f"{'Median ATH':<10} | {'2x Win%':<7} | {'5x Win%':<7} | {'Typical Buy':<11}"
    )
    print("=" * len(header))
    print(header)
    print("-" * len(header))

    for c in clusters:
        funder_short = f"{c.funder[:8]}.."
        print(
            f"{c.cluster_id:<20} | {funder_short:<10} | {len(c.wallets):<7} | "
            f"{c.token_count:<10} | {c.median_ath:>8.2f}x | {c.winrate_2x:>6.1f}% | "
            f"{c.winrate_5x:>6.1f}% | {c.typical_buy_sol:>8.3f} SOL"
        )
    print("=" * len(header))


def print_watch_banner(
    pipeline: CabalPipeline,
    sig_config: SignalFilterConfig,
    args: argparse.Namespace,
    wallets_count: int,
) -> None:
    """Render a compact, professional setup panel for cabal monitoring."""
    stats = pipeline.executor.get_session_stats()
    sizing = pipeline.sizing_config
    if sizing.mode == SizingMode.FIXED:
        sizing_str = f"Fixed ({sizing.fixed_size_sol:.2f} SOL/trade, max cap: {sizing.max_position_sol:.2f} SOL)"
    elif sizing.mode == SizingMode.PROPORTIONAL:
        sizing_str = f"Proportional ({sizing.copy_ratio * 100:.0f}% of insider buy, max cap: {sizing.max_position_sol:.2f} SOL)"
    elif sizing.mode == SizingMode.BALANCE_PCT:
        sizing_str = f"Balance % ({sizing.balance_pct:.1f}% portfolio, max cap: {sizing.max_position_sol:.2f} SOL)"
    else:
        sizing_str = str(sizing.mode)

    confluence_str = (
        ">= 2 co-buys within 30s" if sig_config.require_confluence else "OFF"
    )
    window_str = f"{args.seconds}s" if getattr(args, "seconds", 0) > 0 else "Indefinite"
    mode_str = "Live Dry Run (Paper Execution)"

    grid = Table.grid(padding=(0, 2))
    grid.add_column(style="bold cyan", justify="right")
    grid.add_column(style="white")
    grid.add_column(style="bold cyan", justify="right")
    grid.add_column(style="white")

    clusters_count = len(pipeline.store.list_clusters())
    pnl_style = "green" if stats["realized_pnl_sol"] >= 0 else "red"
    pnl_str = f"[{pnl_style}]{stats['realized_pnl_sol']:+.4f} SOL[/{pnl_style}]"

    grid.add_row(
        "Mode:",
        f"{mode_str} ({window_str})",
        "Tracked:",
        f"{wallets_count} insider wallets in {clusters_count} clusters",
    )
    grid.add_row(
        "Dry Run Balance:",
        f"{stats['cash_balance_sol']:.4f} SOL",
        "Session PnL:",
        f"{pnl_str} ({stats['open_positions']} open)",
    )
    grid.add_row(
        "Position Sizing:",
        sizing_str,
        "Signal Gates:",
        f"Confluence: {confluence_str} | Min Buy: >= {sig_config.min_buy_sol:.2f} SOL",
    )
    grid.add_row(
        "Exit Ladder:",
        f"TP: {args.tp2x * 100:.0f}% @ 2x, {args.tp5x * 100:.0f}% @ 5x | Trail: {args.trail:.1f}%",
        "Adverse Rug Exit:",
        "Instant liquidation on insider/dev sell signature",
    )
    grid.add_row(
        "Ingestion Feeds:",
        "Helius RPC (every 3.0s) + PumpPortal WS",
        "Heartbeat:",
        "Every 15 seconds with live balance & PnL",
    )

    console = Console()
    console.print()
    console.print(
        Panel(
            grid,
            title="[bold yellow]⚡ CABAL DRY RUN: LIVE ON-CHAIN MONITOR ⚡[/bold yellow]",
            subtitle="[dim]Live Paper Trading · Real-Time Solana Ingestion (Helius RPC + WS)[/dim]",
            border_style="bright_blue",
            expand=False,
        )
    )
    console.print()


def print_session_summary(pipeline: CabalPipeline) -> None:
    """Print an aligned Rich summary of dry run financial performance upon exit."""
    stats = pipeline.executor.get_session_stats()
    grid = Table.grid(padding=(0, 2))
    grid.add_column(style="bold cyan", justify="right")
    grid.add_column(style="white")

    pnl_style = "green" if stats["realized_pnl_sol"] >= 0 else "red"
    roi_style = "green" if stats["net_roi_pct"] >= 0 else "red"

    grid.add_row("Starting Paper Balance:", f"{stats['initial_balance_sol']:.4f} SOL")
    grid.add_row("Ending Cash Balance:", f"{stats['cash_balance_sol']:.4f} SOL")
    grid.add_row("Portfolio Equity:", f"{stats['equity_sol']:.4f} SOL")
    grid.add_row(
        "Net Realized PnL:",
        f"[{pnl_style}]{stats['realized_pnl_sol']:+.4f} SOL[/{pnl_style}] ([{roi_style}]{stats['net_roi_pct']:+.2f}%[/{roi_style}])",
    )
    if stats["unrealized_pnl_sol"] != 0:
        grid.add_row("Open Floating PnL:", f"{stats['unrealized_pnl_sol']:+.4f} SOL")

    winrate_str = f"{stats['winrate_pct']:.1f}% ({stats['wins']}W / {stats['losses']}L)"
    grid.add_row(
        "Paper Trades:", f"{stats['total_trades']} executed | Winrate: {winrate_str}"
    )
    grid.add_row("Open Positions:", f"{stats['open_positions']} remaining")

    console = Console()
    console.print()
    console.print(
        Panel(
            grid,
            title="[bold green]📊 CABAL DRY RUN SESSION PERFORMANCE SUMMARY 📊[/bold green]",
            border_style="green",
            expand=False,
        )
    )
    console.print()


async def _watch_loop(
    pipeline: CabalPipeline,
    seconds: int,
    sig_config: SignalFilterConfig,
    args: argparse.Namespace,
) -> None:
    """Async monitoring loop for cabal trade activity."""
    if getattr(args, "discover", False):
        print(
            "\n⚡ Running automated cabal discovery before starting dry run stream...\n",
            flush=True,
        )
        limit_winners = getattr(args, "limit_winners", 30)
        early_limit = getattr(args, "early_limit", DEFAULT_EARLY_BUYER_LIMIT)
        min_mcap = getattr(args, "min_mcap", DEFAULT_MIN_ATH_MCAP)
        min_winners = getattr(args, "min_winners", DEFAULT_MIN_WINNERS)
        winners = fetch_winner_tokens(
            pipeline.pump_client,
            min_ath_mcap=min_mcap,
            limit=limit_winners,
        )
        if winners:
            winner_early_buyers = []
            for token in winners:
                mint = token.get("mint", "")
                if not mint:
                    continue
                buyers = extract_early_buyers(
                    pipeline.pump_client,
                    mint,
                    limit=early_limit,
                )
                if buyers:
                    winner_early_buyers.append((mint, buyers))
            clusters = cluster_early_buyers(
                winner_early_buyers,
                min_winners=min_winners,
                client=pipeline.pump_client,
            )
            if clusters:
                pipeline.store.save_clusters(clusters, sync_stores=True)
                print(
                    f"Discovered and enrolled {len(clusters)} cabal clusters into stores.\n",
                    flush=True,
                )

    count = pipeline.reload_clusters()
    if count == 0:
        print(
            "No cabal wallets tracked. Run 'cabal dryrun --discover' to automatically find and monitor clusters, or 'cabal discover' first."
        )
        return

    print_watch_banner(pipeline, sig_config, args, count)
    stop_event = asyncio.Event()

    def _handle_trade_evaluated(
        decision: Any, position: Any, payload: dict[str, Any]
    ) -> None:
        now_str = datetime.now(UTC).strftime("%H:%M:%S UTC")
        tx_type = str(payload.get("txType") or "buy").upper()
        src = str(payload.get("source") or "stream")
        w_short = f"{decision.wallet[:8]}.."
        m_short = f"{decision.mint[:8]}.."
        print(
            f"[{now_str}] [CABAL ACTIVITY] Wallet {w_short} | Type: {tx_type} | Mint: {m_short} | Size: {decision.amount_sol:.3f} SOL ({src})"
        )
        print(
            f"          -> Confluence: {decision.confluence_count} wallet(s) | Score: {decision.score:.2f} | Action: {decision.action}"
        )
        if decision.action != "BUY" and decision.reasons:
            print(f"          -> Filter Reason: {', '.join(decision.reasons)}")
        if decision.action == "BUY" and position:
            stats = pipeline.executor.get_session_stats()
            print(
                f"          -> [PAPER SNIPE] Position #{position.position_id[:8]} opened: {position.entry_sol_amount:.3f} SOL | Remaining Cash: {stats['cash_balance_sol']:.4f} SOL"
            )
            print("          -> [ALERTS] Dispatched to Discord & Telegram")
        print(flush=True)

    def _handle_insider_dump(trader: str, mint: str, closed_pos: Any) -> None:
        now_str = datetime.now(UTC).strftime("%H:%M:%S UTC")
        stats = pipeline.executor.get_session_stats()
        print(
            f"\n[{now_str}] [INSIDER DUMP DETECTED] Wallet {trader[:8]}.. sold {mint[:8]}.."
        )
        print(
            f"          -> [ADVERSE EXIT] Closed position #{closed_pos.position_id[:8]}! Realized PnL: {closed_pos.realized_pnl_sol:+.4f} SOL ({closed_pos.current_roi_pct:+.1f}% ROI)"
        )
        print(
            f"          -> [BALANCE UPDATE] Cash: {stats['cash_balance_sol']:.4f} SOL | Net Session PnL: {stats['realized_pnl_sol']:+.4f} SOL ({stats['net_roi_pct']:+.1f}%)"
        )
        print(flush=True)

    def _handle_status_update(level: str, message: str) -> None:
        now_str = datetime.now(UTC).strftime("%H:%M:%S UTC")
        print(f"[{now_str}] [STREAM {level.upper()}] {message}", flush=True)

    pipeline.on_trade_evaluated = _handle_trade_evaluated
    pipeline.on_insider_dump = _handle_insider_dump
    pipeline.on_status_update = _handle_status_update

    async def _heartbeat() -> None:
        while not stop_event.is_set():
            await asyncio.sleep(15.0)
            if stop_event.is_set():
                break
            now_hb = datetime.now(UTC).strftime("%H:%M:%S UTC")
            stats = pipeline.executor.get_session_stats()
            open_pos = stats["open_positions"]
            pnl_style = "+" if stats["realized_pnl_sol"] >= 0 else ""
            pnl_str = f"{pnl_style}{stats['realized_pnl_sol']:.4f} SOL"
            roi_style = "+" if stats["net_roi_pct"] >= 0 else ""
            roi_str = f"{roi_style}{stats['net_roi_pct']:.1f}%"
            print(
                f"[{now_hb}] [HEARTBEAT] Bal: {stats['cash_balance_sol']:.4f} SOL | PnL: {pnl_str} ({roi_str}) | Trades: {stats['closed_trades']} ({stats['wins']}W/{stats['losses']}L) | Open: {open_pos} | Feed: Helius RPC ({count} wallets) + WS",
                flush=True,
            )

    async def _tick_positions() -> None:
        start_time = time.time()
        while not stop_event.is_set():
            if seconds > 0 and (time.time() - start_time) >= seconds:
                stop_event.set()
                break
            for pos in list(pipeline.executor.active_positions):
                current_price = pos.high_price_seen * 1.05
                updated_pos, receipt = await pipeline.executor.update_price_tick(
                    pos.position_id, current_price
                )
                if receipt and updated_pos.is_closed:
                    now_str = datetime.now(UTC).strftime("%H:%M:%S UTC")
                    stats = pipeline.executor.get_session_stats()
                    print(
                        f"\n[{now_str}] [PAPER EXIT: {updated_pos.exit_reason.upper()}] Position #{updated_pos.position_id[:8]} Closed | Realized: {updated_pos.realized_pnl_sol:+.4f} SOL | Cash: {stats['cash_balance_sol']:.4f} SOL",
                        flush=True,
                    )
            await asyncio.sleep(2.0)

    try:
        await asyncio.gather(
            pipeline.stream_live(seconds=seconds, stop_event=stop_event),
            _tick_positions(),
            _heartbeat(),
        )
    except asyncio.CancelledError:
        pass


async def _replay_candlestick_series(
    pipeline: CabalPipeline,
    client: Any,
    mint: str,
    args: argparse.Namespace,
) -> dict[str, Any] | None:
    """Replay real historical candlesticks for a specific token mint."""
    # 1. Fetch real token metadata from Pump.fun API
    meta = client.fetch_token(mint)
    symbol = meta.get("symbol") or mint[:8]
    name = meta.get("name") or "Token"

    # 2. Fetch real candlesticks
    interval = getattr(args, "interval", "1m")
    candles = client.fetch_candlesticks(
        mint, interval=interval, limit=300, created_ts=0
    )
    if not candles or len(candles) < 2:
        return None

    # Sort chronologically
    candles.sort(key=lambda x: int(x.get("timestamp") or 0))

    first_candle = candles[0]
    first_open = float(first_candle.get("open") or 0.0)
    if first_open <= 0:
        return None

    raw_first_ts = float(first_candle.get("timestamp") or 0.0)
    first_ts = raw_first_ts / 1000.0 if raw_first_ts > 1e11 else raw_first_ts

    c_ms = float(meta.get("created_timestamp") or 0.0)
    launch_ts = c_ms / 1000.0 if c_ms > 1e11 else (c_ms if c_ms > 0 else 0.0)
    launch_str = (
        datetime.fromtimestamp(launch_ts, tz=UTC).strftime("%Y-%m-%d %H:%M")
        if launch_ts > 0
        else "N/A"
    )
    entry_str = (
        datetime.fromtimestamp(first_ts, tz=UTC).strftime("%H:%M:%S")
        if first_ts > 0
        else "N/A"
    )
    entry_age_sec = (
        max(0.0, first_ts - launch_ts) if (first_ts > 0 and launch_ts > 0) else 0.0
    )

    entry_sol = getattr(args, "size_sol", 0.25)
    # Enter paper trade at actual opening price
    pos, _ = await pipeline.executor.enter_position(
        mint=mint,
        cabal_cluster_id="historical-backtest",
        amount_sol=entry_sol,
        initial_price_sol=first_open,
    )

    peak_price = first_open
    peak_ts = first_ts
    tp_hit = False

    # Step through each real candle chronologically
    for c in candles[1:]:
        c_high = float(c.get("high") or 0.0)
        c_low = float(c.get("low") or 0.0)
        raw_c_ts = float(c.get("timestamp") or 0.0)
        c_ts = raw_c_ts / 1000.0 if raw_c_ts > 1e11 else raw_c_ts

        if c_high > peak_price:
            peak_price = c_high
            peak_ts = c_ts

        # 1. Take-profit ladder (2.0x) check using candle high
        if not tp_hit and c_high >= first_open * 2.0:
            await pipeline.executor.update_price_tick(pos.position_id, first_open * 2.0)
            tp_hit = True

        # 2. Trailing stop check using candle low
        if pos and not pos.is_closed and peak_price > first_open:
            drop_pct = (peak_price - c_low) / peak_price * 100.0
            if drop_pct >= getattr(args, "trail", 15.0):
                exit_price = peak_price * (1.0 - (getattr(args, "trail", 15.0) / 100.0))
                await pipeline.executor.exit_position(
                    pos.position_id,
                    exit_price,
                    reason="trailing_stop",
                )
                break

    # If position still open at the end of the candle series, close at last candle close
    if pos and not pos.is_closed:
        last_close = float(candles[-1].get("close") or first_open)
        await pipeline.executor.exit_position(
            pos.position_id,
            last_close,
            reason="end_of_candles",
        )

    ath_mult = peak_price / first_open if first_open > 0 else 1.0
    time_to_peak_sec = max(0.0, peak_ts - first_ts)
    roi_pct = (
        (pos.realized_pnl_sol / pos.entry_sol_amount) * 100.0
        if pos.entry_sol_amount > 0
        else 0.0
    )

    stat_row = TokenTradeStatRow(
        mint=mint,
        symbol=symbol,
        token_name=name,
        launch_time=launch_str,
        launch_timestamp=launch_ts,
        entry_time=entry_str,
        entry_timestamp=first_ts,
        entry_age_seconds=entry_age_sec,
        entry_price_sol=first_open,
        entry_sol_amount=pos.entry_sol_amount,
        entry_mcap_usd=float(meta.get("usd_market_cap") or 0.0),
        ath_mcap_usd=float(meta.get("ath_market_cap") or 0.0),
        ath_multiplier=ath_mult,
        time_to_peak_seconds=time_to_peak_sec,
        confluence_count=1,
        cabal_cluster_id="historical-backtest",
        buyer_wallet="",
        exit_time=datetime.now(UTC).strftime("%H:%M:%S"),
        exit_reason=pos.exit_reason,
        realized_pnl_sol=pos.realized_pnl_sol,
        net_roi_pct=roi_pct,
        is_win=pos.realized_pnl_sol > 0,
        status="CLOSED",
    )

    return {
        "mint": mint,
        "symbol": symbol,
        "name": name,
        "candles_count": len(candles),
        "entry_price": first_open,
        "peak_price": peak_price,
        "ath_multiplier": ath_mult,
        "exit_reason": pos.exit_reason,
        "entry_sol": pos.entry_sol_amount,
        "realized_pnl_sol": pos.realized_pnl_sol,
        "roi_pct": roi_pct,
        "stat_row": stat_row,
    }


def run_backtest(args: argparse.Namespace) -> int:
    """Execute empirical backtest against real on-chain Pump.fun candlestick data."""
    resolve_dotenv()
    store = CabalStore(db_path=args.db)
    client = get_client()

    executor = CabalExecutor(
        trailing_stop_pct=getattr(args, "trail", 15.0),
        tp_levels=(
            (100.0, getattr(args, "tp2x", 0.50)),
            (400.0, getattr(args, "tp5x", 0.25)),
        ),
        paper_balance_sol=getattr(args, "paper_balance", 2.00),
    )
    pipeline = CabalPipeline(
        store=store,
        signal_filter=SignalFilter(),
        executor=executor,
    )

    tokens_to_backtest: list[str] = []
    if getattr(args, "mint", ""):
        tokens_to_backtest.append(args.mint.strip())
    elif getattr(args, "cluster", ""):
        cluster = store.get_cluster(args.cluster.strip())
        if cluster:
            tokens_to_backtest.extend(list(cluster.winner_tokens))
    else:
        clusters = store.list_clusters()
        for c in clusters:
            tokens_to_backtest.extend(list(c.winner_tokens))
        tokens_to_backtest = list(dict.fromkeys(tokens_to_backtest))[
            : getattr(args, "limit", 10)
        ]

    if not tokens_to_backtest:
        print(
            "No tokens found to backtest. Discover clusters first or specify --mint <address>."
        )
        return 1

    console = Console()
    console.print()
    console.print(
        Panel(
            f"[bold cyan]Backtesting {len(tokens_to_backtest)} real cabal tokens against authentic Pump.fun candlestick history[/bold cyan]\n"
            f"[dim]Timeframe: {getattr(args, 'interval', '1m')} · Sizing: {getattr(args, 'size_sol', 0.25):.2f} SOL · Starting Paper Balance: {getattr(args, 'paper_balance', 2.0):.2f} SOL · Trail: {getattr(args, 'trail', 15.0):.1f}%[/dim]",
            title="[bold yellow]📊 CABAL HISTORICAL ON-CHAIN BACKTEST 📊[/bold yellow]",
            border_style="bright_blue",
            expand=False,
        )
    )
    console.print()

    async def _run() -> list[dict[str, Any]]:
        results = []
        for mint in tokens_to_backtest:
            res = await _replay_candlestick_series(pipeline, client, mint, args)
            if res:
                results.append(res)
        return results

    results = asyncio.run(_run())
    if not results:
        print("No candlestick data available for selected token(s).")
        return 1

    stat_rows = [r["stat_row"] for r in results if "stat_row" in r]
    sheet = TokenStatsSheet(
        title="CABAL HISTORICAL BACKTEST PERFORMANCE SHEET", rows=stat_rows
    )

    # 1. Quick-copy specific mint if requested
    if getattr(args, "copy_mint", 0) > 0:
        row_idx = args.copy_mint - 1
        if 0 <= row_idx < len(sheet.rows):
            target_row = sheet.rows[row_idx]
            mint_addr = target_row.mint
            copied = copy_to_clipboard(mint_addr)
            if copied:
                print(
                    f"📋 Copied #{args.copy_mint} {target_row.symbol} mint to clipboard: {mint_addr}"
                )
            else:
                print(f"Mint #{args.copy_mint} ({target_row.symbol}): {mint_addr}")
            return 0
        print(
            f"Error: row index {args.copy_mint} out of range (1 to {len(sheet.rows)})."
        )
        return 1

    # 2. Raw mints stream
    if getattr(args, "mints", False):
        for mint_addr in sheet.get_mints():
            print(mint_addr)
        return 0

    # 3. Parquet export
    if getattr(args, "parquet", ""):
        target_parquet = args.parquet
        if target_parquet is True or target_parquet == "":
            target_parquet = ".state/reports/cabal_backtest.parquet"
        saved_path = sheet.to_parquet(file_path=target_parquet)
        print(
            f"Exported backtest sheet with {len(sheet.rows)} records to Parquet: {saved_path}\n"
        )

    # 4. CSV export
    if getattr(args, "csv", ""):
        sheet.to_csv(file_path=args.csv)
        print(
            f"Exported backtest sheet with {len(sheet.rows)} records to CSV: {args.csv}\n"
        )

    # 5. JSON output
    if getattr(args, "json", False):
        print(sheet.to_json(indent=2))
        return 0

    # 6. Render Terminal output
    if getattr(args, "plain", False):
        print(sheet.to_table())
    else:
        sheet.print_rich(
            full_mint=getattr(args, "full_mint", False),
            show_mints_panel=not getattr(args, "no_mints_panel", False),
        )

    print_session_summary(pipeline)
    return 0


def run_dryrun(args: argparse.Namespace) -> int:
    """Run real-time observer and paper copytrading pipeline."""
    resolve_dotenv()
    store = CabalStore(db_path=args.db)

    if args.profitable:
        sig_config = SignalFilterConfig.profitable_preset()
    else:
        sig_config = SignalFilterConfig(
            min_buy_sol=args.min_buy_sol,
            min_cluster_typical_buy_sol=args.min_cluster_buy,
            require_confluence=args.require_confluence,
        )

    sig_filter = SignalFilter(config=sig_config)
    executor = CabalExecutor(
        trailing_stop_pct=args.trail,
        tp_levels=(
            (100.0, args.tp2x),
            (400.0, args.tp5x),
        ),
        paper_balance_sol=getattr(args, "paper_balance", 2.00),
    )
    sizing_config = PositionSizingConfig(
        mode=SizingMode(args.size_mode),
        fixed_size_sol=args.size_sol,
        copy_ratio=args.copy_ratio,
        balance_pct=args.balance_pct,
        max_position_sol=args.max_size_sol,
        min_position_sol=args.min_size_sol,
    )
    pipeline = CabalPipeline(
        store=store,
        signal_filter=sig_filter,
        executor=executor,
        sizing_config=sizing_config,
    )

    try:
        asyncio.run(_watch_loop(pipeline, args.seconds, sig_config, args))
    except KeyboardInterrupt:
        print("\nMonitoring stopped by user.")
    finally:
        print_session_summary(pipeline)
    return 0


def print_executions_table(
    executions: list[dict[str, Any]], paper_balance: float = 2.00
) -> None:
    """Format and print an aligned terminal report for bot executions."""
    header = (
        f"{'Position ID':<12} | {'Mint':<10} | {'Wallet':<10} | {'Entry SOL':<10} | "
        f"{'Exit Reason':<18} | {'Realized PnL':<14} | {'ROI %':<8} | {'Status':<8}"
    )
    print("=" * len(header))
    print("  BOT EXECUTION HISTORY (Paper / Live Trades)")
    print("=" * len(header))
    print(header)
    print("-" * len(header))

    total_pnl = 0.0
    wins = 0
    closed_count = 0

    for ex in executions:
        pnl = float(ex.get("realized_pnl_sol") or 0.0)
        roi = float(ex.get("roi_pct") or 0.0)
        is_closed = bool(ex.get("is_closed"))
        status = "CLOSED" if is_closed else "OPEN"
        mint_short = f"{ex['mint'][:8]}.."
        wallet_short = f"{ex['wallet_address'][:8]}.."
        exit_r = str(ex.get("exit_reason") or "holding")[:18]

        if is_closed:
            total_pnl += pnl
            closed_count += 1
            if pnl > 0:
                wins += 1

        print(
            f"{ex['position_id']:<12} | {mint_short:<10} | {wallet_short:<10} | "
            f"{ex['entry_sol_amount']:>8.3f} SOL | {exit_r:<18} | "
            f"{pnl:>+10.4f} SOL | {roi:>+6.1f}% | {status:<8}"
        )

    print("=" * len(header))
    if closed_count > 0:
        winrate = (wins / closed_count) * 100.0
        current_bal = paper_balance + total_pnl
        net_roi = (total_pnl / paper_balance) * 100.0 if paper_balance > 0 else 0.0
        print(
            f"Summary: {closed_count} closed trades | Winrate: {winrate:.1f}% ({wins}/{closed_count}) | Net Realized PnL: {total_pnl:+.4f} SOL"
        )
        print(
            f"Paper Portfolio: Starting: {paper_balance:.4f} SOL | Current: {current_bal:.4f} SOL | Net ROI: {net_roi:+.2f}%\n"
        )


def print_cabal_activity_table(
    activity: list[dict[str, Any]], wallet: str = ""
) -> None:
    """Format and print an aligned report for cabal on-chain activity."""
    header = (
        f"{'Time (UTC)':<20} | {'Ago':<8} | {'Program':<10} | {'SOL Delta':<11} | "
        f"{'Tokens Involved':<24} | {'Signature':<14}"
    )
    print("=" * len(header))
    title = f"  CABAL ON-CHAIN ACTIVITY {f'({wallet[:8]}...)' if wallet else ''}"
    print(title)
    print("=" * len(header))
    print(header)
    print("-" * len(header))

    for act in activity:
        tokens_str = ", ".join(t[:8] + ".." for t in act.get("tokens", [])) or "None"
        if len(tokens_str) > 24:
            tokens_str = tokens_str[:21] + "..."
        sig_short = f"{act['signature'][:12]}.."
        print(
            f"{act['timestamp']:<20} | {act['ago_hours']:>5.1f}h | "
            f"{act['program']:<10} | {act['sol_delta']:>+9.4f} | "
            f"{tokens_str:<24} | {sig_short:<14}"
        )
    print("=" * len(header) + "\n")


def run_trades(args: argparse.Namespace) -> int:
    """Query and display execution history or cabal wallet activity."""
    resolve_dotenv()
    store = CabalStore(db_path=args.db)

    # 1. Cabal wallet on-chain history mode
    if args.cabal or args.wallet:
        wallets_to_check: list[str] = []
        if args.wallet:
            wallets_to_check.append(args.wallet.strip())
        else:
            clusters = store.list_clusters()
            for c in clusters:
                wallets_to_check.extend(list(c.wallets))

        if not wallets_to_check:
            print(
                "No cabal wallets to inspect. Run 'cabal discover' first or specify --wallet <addr>."
            )
            return 0

        print(
            f"Fetching recent on-chain activity for {len(wallets_to_check)} cabal wallet(s)...",
            flush=True,
        )
        all_activity: list[dict[str, Any]] = []
        for w in wallets_to_check:
            act = fetch_cabal_wallet_activity(
                w, limit=max(3, args.limit // len(wallets_to_check))
            )
            for item in act:
                item["wallet"] = w
            all_activity.extend(act)

        all_activity.sort(key=lambda x: x.get("ago_hours", 9999))
        if args.json:
            print(json.dumps(all_activity, indent=2))
            return 0

        if not all_activity:
            print("No recent on-chain transactions found for target wallet(s).")
            return 0

        print_cabal_activity_table(all_activity[: args.limit], wallet=args.wallet)
        return 0

    # 2. Bot execution history mode (default)
    executions = store.list_executions(limit=args.limit)
    if args.json:
        print(json.dumps(executions, indent=2))
        return 0

    if not executions:
        print("No bot executions recorded yet in database.")
        print(
            "Run 'cabal dryrun' to execute paper copytrades, or pass '--cabal' to view tracked insider activity."
        )
        return 0

    client = get_client()
    sheet = TokenStatsSheet.from_executions(executions, client=client)

    # Quick-copy specific mint to clipboard
    if getattr(args, "copy_mint", 0) > 0:
        row_idx = args.copy_mint - 1
        if 0 <= row_idx < len(sheet.rows):
            target_row = sheet.rows[row_idx]
            mint_addr = target_row.mint
            copied = copy_to_clipboard(mint_addr)
            if copied:
                print(
                    f"📋 Copied #{args.copy_mint} {target_row.symbol} mint to clipboard: {mint_addr}"
                )
            else:
                print(f"Mint #{args.copy_mint} ({target_row.symbol}): {mint_addr}")
            return 0
        print(
            f"Error: row index {args.copy_mint} out of range (1 to {len(sheet.rows)})."
        )
        return 1

    # Output raw mint list (pipable / copyable)
    if getattr(args, "mints", False):
        for mint_addr in sheet.get_mints():
            print(mint_addr)
        return 0

    # Parquet export
    if getattr(args, "parquet", ""):
        saved_path = sheet.to_parquet(file_path=args.parquet)
        print(
            f"Exported performance sheet with {len(sheet.rows)} records to Parquet: {saved_path}\n"
        )

    # CSV export
    if getattr(args, "csv", ""):
        sheet.to_csv(file_path=args.csv)
        print(
            f"Exported performance sheet with {len(sheet.rows)} records to CSV: {args.csv}\n"
        )

    # Terminal output: Rich (default) or plain ASCII table
    if getattr(args, "plain", False):
        print_executions_table(
            executions, paper_balance=getattr(args, "paper_balance", 2.0)
        )
    else:
        sheet.print_rich(
            full_mint=getattr(args, "full_mint", False),
            show_mints_panel=not getattr(args, "no_mints_panel", False),
        )
    return 0


def main(argv: list[str] | None = None) -> int:
    """CLI entry point for cabal."""
    if sys.platform == "win32":
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except Exception:
            logger.debug("Failed setting stdout encoding to utf-8")

    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == "discover":
        return run_discover(args)
    if args.command == "list":
        return run_list(args)
    if args.command == "dryrun":
        return run_dryrun(args)
    if args.command == "backtest":
        return run_backtest(args)
    if args.command == "trades":
        return run_trades(args)

    parser.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
