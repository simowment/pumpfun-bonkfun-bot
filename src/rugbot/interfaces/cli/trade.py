"""CLI tool for executing manual or automated Pump.fun Buy/Sell orders."""

# ruff: noqa: C901, PLR0912

from __future__ import annotations

import argparse
import asyncio
import sys

from rugbot.execution.ports import ExecutionMode
from rugbot.execution.trade_service import BuyOrderSpec, SellOrderSpec, TradingService


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="rug_trade",
        description="Unified Pump.fun Buy/Sell CLI with zero duplication across Dry-Run and Live modes",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

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
        "positions", help="List all open positions and active TP/SL levels"
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

    if args.command == "positions":
        positions = service.get_positions()
        if not positions:
            print("No open positions found.")
            return 0
        print(f"[*] Open Positions ({len(positions)}):")
        print("-" * 75)
        print(f"{'MINT':<44} | {'SIZE (SOL)':<10} | {'TOKENS':<14} | {'MODE':<8}")
        print("-" * 75)
        for p in positions:
            print(
                f"{p['mint']:<44} | {p['entry_sol']:<10.4f} | {p['token_amount']:<14,} | {p['mode']:<8}"
            )
        print("-" * 75)
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
