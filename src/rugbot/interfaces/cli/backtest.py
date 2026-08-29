"""rug_backtest CLI — Configurable Multi-Mode Backtester (Dev Creation vs Copytrade)."""

from __future__ import annotations

import argparse
import json
from collections.abc import Sequence

from rugbot.backtest.runners.copytrade_backtest_runner import (
    CopytradeBacktestConfig,
    resolve_copytrade_samples,
    run_copytrade_tp_sl_grid_search,
)
from rugbot.backtest.runners.creator_backtest_runner import (
    CreatorBacktestConfig,
    resolve_target_samples,
    run_creator_tp_sl_grid_search,
)
from rugbot.utils.logger import get_logger

logger = get_logger(__name__)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="rug_backtest",
        description="Replay dev creations or copytrade wallet trades and optimize TPxSL grid.",
    )
    p.add_argument("target", help="wallet address or mint (entity via --entity)")
    p.add_argument(
        "--mode",
        choices=["dev_creation", "copytrade", "entity"],
        default="dev_creation",
        help="backtest mode: dev_creation (default), copytrade, or entity",
    )
    p.add_argument(
        "--entity", action="store_true", help="entity mode via funding chain dedup"
    )
    p.add_argument("--optimize", action="store_true", help="run TPxSL grid search")
    p.add_argument("--tp", default="25,50,75,100,200", help="TP grid pct comma list")
    p.add_argument("--sl", default="10,20,30", help="SL grid pct comma list")
    p.add_argument("--quote", type=float, default=0.3, help="quote size SOL")
    p.add_argument("--slippage", type=float, default=1.5, help="slippage pct")
    p.add_argument(
        "--fees",
        type=float,
        default=1.0,
        help="pump fee pct (unused, quote_engine 95+30bps)",
    )
    p.add_argument("--max-hold", type=int, default=90, help="max hold seconds")
    p.add_argument("--entry-offset", default="B0", choices=["B0", "B1", "B2+"])
    p.add_argument(
        "--copy-lag",
        type=int,
        default=1,
        help="copytrade execution delay in slots (default: 1 slot = ~400ms)",
    )
    p.add_argument(
        "--mirror-sells",
        action="store_true",
        default=True,
        help="mirror target wallet sells when leader sells before TP/SL",
    )
    p.add_argument(
        "--no-mirror-sells",
        dest="mirror_sells",
        action="store_false",
        help="do not mirror target wallet sells; rely strictly on follower TP/SL",
    )
    p.add_argument("--json", action="store_true", help="machine JSON output")
    p.add_argument(
        "--plot",
        action="store_true",
        help="display VectorBT terminal equity curve and export interactive HTML report",
    )
    return p


def _parse_grid(s: str) -> tuple[float, ...]:
    parts = [p.strip() for p in s.split(",") if p.strip()]
    vals: list[float] = []
    for p in parts:
        vals.append(float(p))
    return tuple(vals)


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    tp_grid = _parse_grid(args.tp)
    sl_grid = _parse_grid(args.sl)

    mode = "entity" if args.entity else args.mode

    if mode == "copytrade":
        copy_config = CopytradeBacktestConfig(
            quote_size_sol=float(args.quote),
            copy_lag_slots=int(args.copy_lag),
            copy_entry_slippage_pct=float(args.slippage),
            mirror_target_sells=bool(args.mirror_sells),
            pump_fee_pct=float(args.fees),
            max_hold_s=int(args.max_hold),
            tp_grid=tp_grid,
            sl_grid=sl_grid,
        )
        copy_samples = resolve_copytrade_samples(args.target)
        report = run_copytrade_tp_sl_grid_search(
            copy_samples, copy_config, target=args.target
        )

        if bool(args.json):
            payload = {
                "status": "abstain" if report.insufficient_data else "ok",
                "target": report.target,
                "mode": report.mode,
                "samples": len(report.samples),
                "message": report.message,
                "optimal_tp": report.optimal_tp,
                "optimal_sl": report.optimal_sl,
                "optimal_ev": report.optimal_ev,
                "robust_zone": [list(x) for x in report.robust_zone],
                "market_impact_drag_sol": report.market_impact_drag_sol,
                "evaluations": [
                    {
                        "tp_pct": e.tp_pct,
                        "sl_pct": e.sl_pct,
                        "wins": e.wins,
                        "losses": e.losses,
                        "winrate_pct": e.winrate_pct,
                        "gross_pnl_sol": e.gross_pnl_sol,
                        "fees_sol": e.fees_sol,
                        "net_pnl_sol": e.net_pnl_sol,
                        "net_ev_sol": e.net_ev_sol,
                        "net_roi_pct": e.net_roi_pct,
                        "max_drawdown_sol": e.max_drawdown_sol,
                        "leader_roi_pct": e.leader_roi_pct,
                        "lag_drag_sol": e.lag_drag_sol,
                        "robust": e.robust,
                    }
                    for e in report.evaluations
                ],
                "warnings": list(report.warnings),
            }
            print(json.dumps(payload, indent=2, sort_keys=True))
            return 0 if not report.insufficient_data else 1

        if report.insufficient_data:
            print(f"ABSTAIN: {report.message} - samples {len(report.samples)}")
            return 1

        print(
            f"=== rug_backtest {report.target} mode=COPYTRADE (lag={args.copy_lag} slots) samples={len(report.samples)} ==="
        )
        header = "TP\\SL | " + " | ".join(f"-{int(sl)}% " for sl in copy_config.sl_grid)
        print(header)
        print("-" * len(header))
        for tp in copy_config.tp_grid:
            row_cells: list[str] = []
            for sl in copy_config.sl_grid:
                ev = next(
                    (
                        e
                        for e in report.evaluations
                        if e.tp_pct == tp and e.sl_pct == sl
                    ),
                    None,
                )
                if ev is None:
                    row_cells.append("  -")
                else:
                    mark = "*" if ev.robust else " "
                    row_cells.append(
                        f"{ev.winrate_pct:.0f}% EV{ev.net_ev_sol:+.3f}{mark}"
                    )
            print(f"+{int(tp)}% | " + " | ".join(row_cells))
        print(
            f"Optimal (TP,SL) = +{report.optimal_tp}% / -{report.optimal_sl}% | EV {report.optimal_ev:+.4f} SOL | zone robuste {list(report.robust_zone)}"
        )

        if args.plot and report.records:
            from pathlib import Path
            from rugbot.backtest.reporting.visualizer import (
                export_vectorbt_html_report,
                generate_terminal_equity_chart,
            )

            print("\n" + generate_terminal_equity_chart(list(report.records)))
            total_fees = sum(
                e.fees_sol
                for e in report.evaluations
                if e.tp_pct == report.optimal_tp and e.sl_pct == report.optimal_sl
            )
            html_out = Path(".state") / f"backtest_{report.target[:8]}.html"
            export_vectorbt_html_report(
                target=report.target,
                mode=report.mode,
                records=list(report.records),
                total_fees_sol=total_fees,
                market_impact_drag_sol=report.market_impact_drag_sol,
                output_path=html_out,
            )
            print(f"\n[+] Interactive VectorBT HTML Report saved to: {html_out}")
        return 0

    # Default: Dev Creation / Entity Mode
    config = CreatorBacktestConfig(
        quote_size_sol=float(args.quote),
        slippage_pct=float(args.slippage),
        pump_fee_pct=float(args.fees),
        max_hold_s=int(args.max_hold),
        entry_offset=str(args.entry_offset),
        tp_grid=tp_grid,
        sl_grid=sl_grid,
    )
    samples = resolve_target_samples(
        args.target, entity=bool(args.entity or mode == "entity")
    )
    report = run_creator_tp_sl_grid_search(
        samples,
        config,
        target=args.target,
        mode="entity" if (args.entity or mode == "entity") else "wallet",
    )

    if bool(args.json):
        payload = {
            "status": "abstain" if report.insufficient_data else "ok",
            "target": report.target,
            "mode": report.mode,
            "samples": len(report.samples),
            "message": report.message,
            "optimal_tp": report.optimal_tp,
            "optimal_sl": report.optimal_sl,
            "optimal_ev": report.optimal_ev,
            "robust_zone": [list(x) for x in report.robust_zone],
            "evaluations": [
                {
                    "tp_pct": e.tp_pct,
                    "sl_pct": e.sl_pct,
                    "wins": e.wins,
                    "losses": e.losses,
                    "winrate_pct": e.winrate_pct,
                    "gross_pnl_sol": e.gross_pnl_sol,
                    "fees_sol": e.fees_sol,
                    "net_pnl_sol": e.net_pnl_sol,
                    "net_ev_sol": e.net_ev_sol,
                    "net_roi_pct": e.net_roi_pct,
                    "max_drawdown_sol": e.max_drawdown_sol,
                    "robust": e.robust,
                }
                for e in report.evaluations
            ],
            "warnings": list(report.warnings),
        }
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0 if not report.insufficient_data else 1

    # human
    if report.insufficient_data:
        print(f"ABSTAIN: {report.message} - samples {len(report.samples)}")
        return 1
    print(
        f"=== rug_backtest {report.target} mode={report.mode} samples={len(report.samples)} ==="
    )
    # matrix header
    header = "TP\\SL | " + " | ".join(f"-{int(sl)}% " for sl in config.sl_grid)
    print(header)
    print("-" * len(header))
    for tp in config.tp_grid:
        row_cells: list[str] = []
        for sl in config.sl_grid:
            ev = next(
                (e for e in report.evaluations if e.tp_pct == tp and e.sl_pct == sl),
                None,
            )
            if ev is None:
                row_cells.append("  -")
            else:
                mark = "*" if ev.robust else " "
                row_cells.append(f"{ev.winrate_pct:.0f}% EV{ev.net_ev_sol:+.3f}{mark}")
        print(f"+{int(tp)}% | " + " | ".join(row_cells))
    print(
        f"Optimal (TP,SL) = +{report.optimal_tp}% / -{report.optimal_sl}% | EV {report.optimal_ev:+.4f} SOL | zone robuste {list(report.robust_zone)}"
    )
    if report.warnings:
        for w in report.warnings:
            print(f"warn: {w}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
