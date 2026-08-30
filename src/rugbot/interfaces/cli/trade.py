"""CLI tool for executing manual or automated Pump.fun Buy/Sell orders."""

# ruff: noqa: C901, PLR0912

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from solders.pubkey import Pubkey

from rugbot.execution.live import _build_trade_context, _fetch_trade_accounts
from rugbot.execution.ports import ExecutionIntent, ExecutionMode, Slot
from rugbot.execution.trade_service import (
    LAMPORTS_PER_SOL,
    BuyOrderSpec,
    SellOrderSpec,
    TradingService,
)
from rugbot.integrations.solana_rpc import SolanaClient


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="rug_trade",
        description="Unified Pump.fun Buy/Sell CLI with zero duplication across Dry-Run and Live modes",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    # Quote command
    quote_parser = subparsers.add_parser(
        "quote",
        help="Inspect live on-chain bonding curve reserves, market cap, and quotes",
    )
    quote_parser.add_argument("--mint", required=True, help="Token mint address")
    quote_parser.add_argument(
        "--sol",
        type=float,
        default=0.1,
        help="SOL amount to quote for buy (default: 0.1 SOL)",
    )
    quote_parser.add_argument(
        "--tokens", type=int, default=None, help="Token base units to quote for sell"
    )

    # Buy command
    buy_parser = subparsers.add_parser("buy", help="Execute a token purchase")
    buy_parser.add_argument("--mint", required=True, help="Token mint address")
    buy_parser.add_argument(
        "--sol", type=float, required=True, help="Amount in SOL to spend"
    )
    buy_parser.add_argument(
        "--slippage",
        type=float,
        default=5.0,
        help="Slippage percentage (default: 5.0%%)",
    )
    buy_parser.add_argument(
        "--priority-fee", type=float, default=0.0005, help="Priority fee in SOL"
    )
    buy_parser.add_argument(
        "--jito", type=float, default=0.001, help="Jito tip in SOL (default: 0.001)"
    )
    buy_parser.add_argument(
        "--tp",
        type=float,
        default=None,
        help="Take profit percentage (e.g. 50 for +50%%)",
    )
    buy_parser.add_argument(
        "--sl",
        type=float,
        default=None,
        help="Stop loss percentage (e.g. 20 for -20%%)",
    )
    buy_parser.add_argument(
        "--trailing", type=float, default=None, help="Trailing stop percentage"
    )
    buy_parser.add_argument(
        "--routing", choices=["auto", "rpc", "jito"], default="auto"
    )
    buy_parser.add_argument(
        "--mode",
        choices=[m.value for m in ExecutionMode] + ["dry-run"],
        default=ExecutionMode.DRY_RUN.value,
        help="Execution mode (default: dry_run)",
    )
    buy_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Force dry-run simulation mode without spending real SOL",
    )

    # Sell command
    sell_parser = subparsers.add_parser("sell", help="Execute a token sale")
    sell_parser.add_argument("--mint", required=True, help="Token mint address")
    sell_parser.add_argument(
        "--pct",
        type=float,
        default=100.0,
        help="Percent of position to sell (default: 100%%)",
    )
    sell_parser.add_argument(
        "--tokens", type=int, default=None, help="Exact token amount to sell"
    )
    sell_parser.add_argument(
        "--slippage",
        type=float,
        default=10.0,
        help="Slippage percentage (default: 10.0%%)",
    )
    sell_parser.add_argument(
        "--priority-fee", type=float, default=0.0005, help="Priority fee in SOL"
    )
    sell_parser.add_argument(
        "--jito", type=float, default=0.001, help="Jito tip in SOL"
    )
    sell_parser.add_argument(
        "--routing", choices=["auto", "rpc", "jito"], default="auto"
    )
    sell_parser.add_argument(
        "--mode",
        choices=[m.value for m in ExecutionMode] + ["dry-run"],
        default=ExecutionMode.DRY_RUN.value,
        help="Execution mode (default: dry_run)",
    )
    sell_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Force dry-run simulation mode without spending real SOL",
    )

    # Positions command
    subparsers.add_parser(
        "positions",
        help="List all open positions with unrealized PnL and active TP/SL levels",
    )

    # PnL command
    pnl_parser = subparsers.add_parser(
        "pnl", help="Display net PnL summary and closed trade history"
    )
    pnl_parser.add_argument(
        "--plot",
        action="store_true",
        help="Display VectorBT terminal equity curve and export interactive HTML report",
    )

    # Chart command (OHLC)
    chart_parser = subparsers.add_parser(
        "chart",
        help="Fetch OHLC candlesticks and render VectorBT price & execution chart",
    )
    chart_parser.add_argument("--mint", required=True, help="Target token mint address")
    chart_parser.add_argument(
        "--timeframe",
        type=int,
        default=60,
        help="Candle timeframe in seconds (default: 60s)",
    )
    chart_parser.add_argument(
        "--limit",
        type=int,
        default=50,
        help="Maximum candles to fetch (default: 50)",
    )

    # Monitor command
    monitor_parser = subparsers.add_parser(
        "monitor", help="Monitor open positions and auto-trigger TP/SL exits"
    )
    monitor_parser.add_argument(
        "--interval",
        type=float,
        default=2.0,
        help="Check interval in seconds (default: 2.0s)",
    )
    monitor_parser.add_argument(
        "--max-duration",
        type=float,
        default=60.0,
        help="Maximum monitoring seconds (default: 60s)",
    )

    return parser


def parse_execution_mode(raw_mode: str, dry_run_flag: bool) -> ExecutionMode:
    """Normalize execution mode argument."""
    if dry_run_flag:
        return ExecutionMode.DRY_RUN
    cleaned = raw_mode.strip().lower().replace("-", "_")
    if cleaned in ("dry_run", "dryrun"):
        return ExecutionMode.DRY_RUN
    return ExecutionMode(cleaned)


from rugbot.runtime.config import resolve_dotenv


async def run_cli(args: argparse.Namespace) -> int:
    resolve_dotenv(include_signing=True)
    service = TradingService()

    if args.command == "buy":
        mode = parse_execution_mode(args.mode, args.dry_run)
        spec = BuyOrderSpec(
            mint=args.mint,
            amount_sol=args.sol,
            slippage_pct=args.slippage,
            priority_fee_sol=args.priority_fee,
            jito_tip_sol=args.jito,
            routing=args.routing,
            mode=mode,
            take_profit_pct=args.tp,
            stop_loss_pct=args.sl,
            trailing_stop_pct=args.trailing,
        )
        print("=" * 60)
        print(" 🚀 PUMP.FUN UNIFIED ORDER: BUY")
        print("=" * 60)
        print(f" Target Mint:     {spec.mint}")
        print(f" Size:            {spec.amount_sol:.4f} SOL")
        print(
            f" Max Slippage:    {spec.slippage_pct:.1f}% ({spec.max_slippage_bps} bps)"
        )
        print(f" Execution Mode:  {spec.mode.value.upper()}")
        print(f" Priority Fee:    {spec.priority_fee_sol:.5f} SOL")
        print(f" Jito MEV Tip:    {spec.jito_tip_sol:.5f} SOL")
        if spec.take_profit_pct:
            print(f" Take Profit:     +{spec.take_profit_pct:.1f}%")
        if spec.stop_loss_pct:
            print(f" Stop Loss:       -{spec.stop_loss_pct:.1f}%")
        print("-" * 60)
        print("[*] Fetching on-chain bonding curve & calculating CPMM quote...")

        res = await service.execute_buy(spec)
        if res.ok:
            print("[+] ORDER FILLED SUCCESSFULLY!")
            print(f"    Tokens received: {res.token_amount:,}")
            print(f"    Effective price: {res.effective_price_sol:.10f} SOL/token")
            print(f"    Signature:       {res.signature}")
            print(f"    Estimated Fee:   {res.fee_sol:.6f} SOL")
            print(f"    Slot:            {res.slot}")
            print(f"    Details:         {res.message}")
            print("=" * 60)
            return 0

        print(f"[-] ORDER FAILED: {res.error}")
        print("=" * 60)
        return 1

    if args.command == "sell":
        mode = parse_execution_mode(args.mode, args.dry_run)
        spec = SellOrderSpec(
            mint=args.mint,
            percent=args.pct,
            amount_tokens=args.tokens,
            slippage_pct=args.slippage,
            priority_fee_sol=args.priority_fee,
            jito_tip_sol=args.jito,
            routing=args.routing,
            mode=mode,
        )
        print("=" * 60)
        print(" 📉 PUMP.FUN UNIFIED ORDER: SELL")
        print("=" * 60)
        print(f" Target Mint:     {spec.mint}")
        print(
            f" Amount:          {spec.percent:.1f}% (or {spec.amount_tokens or 'all'} tokens)"
        )
        print(f" Max Slippage:    {spec.slippage_pct:.1f}%")
        print(f" Execution Mode:  {spec.mode.value.upper()}")
        print("-" * 60)
        print("[*] Computing CPMM sell proceeds on live curve...")

        res = await service.execute_sell(spec)
        if res.ok:
            print("[+] SELL FILLED SUCCESSFULLY!")
            print(f"    Tokens sold:     {res.token_amount:,}")
            print(f"    SOL Proceeds:    {res.sol_amount:.6f} SOL")
            print(f"    Effective price: {res.effective_price_sol:.10f} SOL/token")
            print(f"    Signature:       {res.signature}")
            print(f"    Details:         {res.message}")
            print("=" * 60)
            return 0

        print(f"[-] SELL FAILED: {res.error}")
        print("=" * 60)
        return 1

    if args.command == "quote":
        mint_str = args.mint.strip()
        print("=" * 60)
        print(" 🔍 PUMP.FUN ON-CHAIN MARKET QUOTE")
        print("=" * 60)
        print(f" Target Mint:     {mint_str}")
        print("-" * 60)

        try:
            mint_pk = Pubkey.from_string(mint_str)
            client = SolanaClient(service.endpoint)
            slot, accounts = await _fetch_trade_accounts(client, mint_pk)
            dummy_intent = ExecutionIntent(
                intent_id="cli_quote",
                as_of_slot=Slot(0),
                market_id=mint_str,
                side="buy",
                quote_amount_base_units=int(args.sol * LAMPORTS_PER_SOL),
                base_amount_base_units=None,
                max_slippage_bps=500,
                reason_codes=("quote",),
            )
            _, reserves = _build_trade_context(
                accounts=accounts,
                mint=mint_pk,
                user=Pubkey.from_string("11111111111111111111111111111111"),
                intent=dummy_intent,
            )
            await client.close()

            # Exact integer CPMM calculations for Pump.fun bonding curve
            spendable_sol_lamports = int(args.sol * LAMPORTS_PER_SOL)
            net_sol_lamports = (spendable_sol_lamports * 99) // 100
            tokens_out_base = (
                int(reserves.virtual_base_reserves) * net_sol_lamports
            ) // (int(reserves.virtual_quote_reserves) + net_sol_lamports)
            tokens_for_buy = tokens_out_base / 1_000_000.0
            price_buy = args.sol / tokens_for_buy if tokens_for_buy > 0 else 0.0

            sell_tokens = args.tokens or int(tokens_out_base)
            sol_out_gross = (int(reserves.virtual_quote_reserves) * sell_tokens) // (
                int(reserves.virtual_base_reserves) + sell_tokens
            )
            sol_out_net = (sol_out_gross * 99) // 100
            sol_for_sell = sol_out_net / LAMPORTS_PER_SOL
            price_sell = (
                sol_for_sell / (sell_tokens / 1_000_000.0) if sell_tokens > 0 else 0.0
            )

            # Market Cap
            mcap_sol = (reserves.virtual_quote_reserves / LAMPORTS_PER_SOL) * (
                1_000_000_000.0 / (reserves.virtual_base_reserves / 1_000_000.0)
            )

            print(f" Slot:                   {slot}")
            print(
                f" Virtual SOL Reserves:   {reserves.virtual_quote_reserves / LAMPORTS_PER_SOL:.4f} SOL"
            )
            print(
                f" Real SOL Reserves:      {reserves.real_quote_reserves / LAMPORTS_PER_SOL:.4f} SOL"
            )
            print(
                f" Curve Status:           {'MIGRATED / COMPLETE' if reserves.is_complete else 'ACTIVE (Bonding Curve)'}"
            )
            print(f" Est. Market Cap:        ~{mcap_sol:.2f} SOL")
            print("-" * 60)
            print(
                f" Buy Quote ({args.sol:.4f} SOL):   {tokens_for_buy:,.2f} tokens (@ {price_buy:.10f} SOL/token)"
            )
            print(
                f" Sell Quote ({sell_tokens / 1_000_000:,.2f} tokens): {sol_for_sell:.6f} SOL (@ {price_sell:.10f} SOL/token)"
            )
            print("=" * 60)
            return 0
        except Exception as exc:
            print(f"[-] Failed to fetch quote: {exc}")
            print("=" * 60)
            return 1

    if args.command == "positions":
        positions = service.get_positions()
        if not positions:
            print("No open positions found.")
            return 0
        print(f"[*] Open Positions ({len(positions)}):")
        print("-" * 85)
        print(
            f"{'MINT':<44} | {'ENTRY (SOL)':<11} | {'TOKENS':<14} | {'UNREALIZED PNL':<16} | {'MODE':<8}"
        )
        print("-" * 85)
        for p in positions:
            pnl_str = (
                f"{'+' if p['current_pnl_pct'] >= 0 else ''}{p['current_pnl_pct']:.2f}%"
            )
            print(
                f"{p['mint']:<44} | {p['entry_sol']:<11.4f} | {p['token_amount']:<14,} | {pnl_str:<16} | {p['mode']:<8}"
            )
        print("-" * 85)
        return 0

    if args.command == "pnl":
        summary = service.get_pnl_summary()
        trades = service.get_closed_trades()
        print("=" * 60)
        print(" 📊 PUMP.FUN TRADING PERFORMANCE & PNL SUMMARY")
        print("=" * 60)
        print(f" Total Closed Trades:    {summary['total_trades']}")
        print(
            f" Wins / Losses:          {summary['wins']} W / {summary['losses']} L (Winrate: {summary['winrate_pct']:.1f}%)"
        )
        print(f" Total Fees Paid:        {summary['total_fees_sol']:.6f} SOL")
        print(f" Net Realized PnL:       {summary['realized_pnl_sol']:+.6f} SOL")
        print(f" Open Positions Value:   {summary['unrealized_pnl_sol']:+.6f} SOL")
        print(f" Net Total Portfolio:    {summary['total_net_pnl_sol']:+.6f} SOL")
        print("=" * 60)

        if getattr(args, "plot", False):
            from rugbot.backtest.reporting.visualizer import (
                TradePerformanceRecord,
                export_vectorbt_html_report,
                generate_terminal_equity_chart,
            )

            if not trades:
                print("\n[!] No closed trades to plot equity curve.")
                return 0

            perf_records: list[TradePerformanceRecord] = []
            cum_equity = 0.0
            peak_equity = 0.0
            for idx, t in enumerate(trades, 1):
                net_pnl = float(t.get("realized_pnl_sol", 0.0))
                cum_equity += net_pnl
                peak_equity = max(peak_equity, cum_equity)
                dd = (
                    ((cum_equity - peak_equity) / peak_equity * 100.0)
                    if peak_equity > 0
                    else 0.0
                )
                gross_pnl = float(t.get("sol_proceeds", 0.0)) - float(
                    t.get("cost_basis_sol", 0.0)
                )
                perf_records.append(
                    TradePerformanceRecord(
                        trade_index=idx,
                        mint=t.get("mint", "unknown"),
                        entry_sol=float(t.get("cost_basis_sol", 0.0)),
                        exit_sol=float(t.get("sol_proceeds", 0.0)),
                        gross_pnl_sol=gross_pnl,
                        net_pnl_sol=net_pnl,
                        roi_pct=float(t.get("realized_pnl_pct", 0.0)),
                        market_impact_pct=0.0,
                        holding_seconds=0.0,
                        is_win=net_pnl > 0,
                        cumulative_equity_sol=cum_equity,
                        drawdown_pct=dd,
                    )
                )

            print("\n" + generate_terminal_equity_chart(perf_records))
            html_out = Path(".state/trading_pnl_report.html")
            export_vectorbt_html_report(
                target="live_trading_portfolio",
                mode="execution",
                records=perf_records,
                total_fees_sol=summary["total_fees_sol"],
                market_impact_drag_sol=0.0,
                output_path=html_out,
            )
            print(f"\n[+] Interactive VectorBT HTML Report saved to: {html_out}")
        return 0

    if args.command == "chart":
        from rugbot.backtest.reporting.visualizer import (
            TradePerformanceRecord,
            export_vectorbt_ohlc_report,
            generate_terminal_candlestick_chart,
        )
        from rugbot.domain.ohlc import fetch_token_ohlc_candles

        mint = args.mint.strip()
        print(
            f"[*] Fetching OHLC candles for {mint[:8]}... (Timeframe: {args.timeframe}s, Limit: {args.limit})"
        )
        candles = await fetch_token_ohlc_candles(
            mint, timeframe_seconds=args.timeframe, max_candles=args.limit
        )

        if not candles:
            print(f"[!] No OHLC candlestick data available for {mint}")
            return 1

        print("\n" + generate_terminal_candlestick_chart(candles))

        # Check for matching closed trades
        trades = service.get_closed_trades()
        mint_trades = [t for t in trades if t.get("mint") == mint]
        perf_records: list[TradePerformanceRecord] = []
        for idx, t in enumerate(mint_trades, 1):
            net_pnl = float(t.get("realized_pnl_sol", 0.0))
            perf_records.append(
                TradePerformanceRecord(
                    trade_index=idx,
                    mint=mint,
                    entry_sol=float(t.get("cost_basis_sol", 0.0)),
                    exit_sol=float(t.get("sol_proceeds", 0.0)),
                    gross_pnl_sol=float(t.get("sol_proceeds", 0.0))
                    - float(t.get("cost_basis_sol", 0.0)),
                    net_pnl_sol=net_pnl,
                    roi_pct=float(t.get("realized_pnl_pct", 0.0)),
                    market_impact_pct=0.0,
                    holding_seconds=0.0,
                    is_win=net_pnl > 0,
                    cumulative_equity_sol=net_pnl,
                    drawdown_pct=0.0,
                )
            )

        html_out = Path(".state/token_ohlc_report.html")
        export_vectorbt_ohlc_report(
            target="live_trading",
            mint=mint,
            candles=candles,
            records=perf_records,
            total_fees_sol=0.0015 * len(perf_records),
            output_path=html_out,
        )
        print(
            f"\n[+] Interactive VectorBT OHLC Candlestick Report saved to: {html_out}"
        )
        return 0

    if args.command == "monitor":
        print(
            f"[*] Starting Position Monitor (Check interval: {args.interval:.1f}s)..."
        )
        elapsed = 0.0
        while elapsed < args.max_duration:
            triggered = await service.tick()
            if triggered:
                for t in triggered:
                    print(
                        f"[!] Triggered Exit Trade: {t.mint[:8]}... | OK: {t.ok} | Msg: {t.message}"
                    )
            await asyncio.sleep(args.interval)
            elapsed += args.interval
        return 0

    return 0


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8")
    parser = build_parser()
    args = parser.parse_args()
    code = asyncio.run(run_cli(args))
    sys.exit(code)


if __name__ == "__main__":
    main()
