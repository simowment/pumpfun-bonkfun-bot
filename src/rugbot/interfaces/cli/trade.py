"""CLI tool for executing manual or automated Pump.fun Buy/Sell orders."""

# ruff: noqa: C901, PLR0912, PLR0913, BLE001, TRY003

from __future__ import annotations

import argparse
import asyncio
import sys

from rugbot.execution.ports import ExecutionMode
from rugbot.execution.trade_service import BuyOrderSpec, SellOrderSpec, TradingService


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="rug_trade",
        description="Unified Pump.fun Buy/Sell CLI (supports SOL quantity, Slippage, Fees, TP/SL)",
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
        choices=[m.value for m in ExecutionMode],
        default=ExecutionMode.PAPER.value,
        help="Execution mode (default: paper)",
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
        choices=[m.value for m in ExecutionMode],
        default=ExecutionMode.PAPER.value,
        help="Execution mode (default: paper)",
    )

    # Positions command
    subparsers.add_parser(
        "positions", help="List all open positions and active TP/SL levels"
    )

    return parser


async def run_cli(args: argparse.Namespace) -> int:
    service = TradingService()

    if args.command == "buy":
        mode = ExecutionMode(args.mode)
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
        print(
            f"[*] Submitting BUY order for {spec.mint[:8]}... ({spec.amount_sol} SOL | Mode: {spec.mode.value})"
        )
        res = await service.execute_buy(spec)
        if res.ok:
            print("[+] BUY SUCCESSFUL!")
            print(f"    Tokens received: {res.token_amount:,}")
            print(f"    Effective price: {res.effective_price_sol:.10f} SOL")
            print(f"    Signature:       {res.signature}")
            if res.take_profit_pct:
                print(f"    Take Profit:     +{res.take_profit_pct:.1f}%")
            if res.stop_loss_pct:
                print(f"    Stop Loss:       -{res.stop_loss_pct:.1f}%")
            return 0
        print(f"[-] BUY FAILED: {res.error}")
        return 1

    if args.command == "sell":
        mode = ExecutionMode(args.mode)
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
        print(
            f"[*] Submitting SELL order for {spec.mint[:8]}... ({spec.percent}% | Mode: {spec.mode.value})"
        )
        res = await service.execute_sell(spec)
        if res.ok:
            print("[+] SELL SUCCESSFUL!")
            print(f"    Tokens sold:   {res.token_amount:,}")
            print(f"    Proceeds:      ~{res.sol_amount:.4f} SOL")
            print(f"    Signature:     {res.signature}")
            return 0
        print(f"[-] SELL FAILED: {res.error}")
        return 1

    if args.command == "positions":
        positions = service.get_positions()
        if not positions:
            print("No open positions found.")
            return 0
        print(f"[*] Open Positions ({len(positions)}):")
        for p in positions:
            print(
                f"  - Mint: {p['mint']} | Size: {p['token_amount']:,} tokens (~{p['entry_sol']} SOL) | TP: +{p['take_profit_pct']}% | SL: -{p['stop_loss_pct']}%"
            )
        return 0

    return 0


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    code = asyncio.run(run_cli(args))
    sys.exit(code)


if __name__ == "__main__":
    main()
