"""Pairs lab CLI — alpha-extraction report over finalized discover trades.

Paper-only analysis, no orders: labels every recorded launch with executable
net PnL by replaying the exit ladder, then reports base rates and per-feature
tercile lift. Thresholds are outputs of this tool, not inputs.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
from pathlib import Path

from rugbot.backtest.pairs_lab import (
    PairsLabConfig,
    format_human,
    result_to_json,
    run_pairs_lab,
)
from rugbot.discover.store import ensure_discover_schema
from rugbot.storage.database import DatabaseManager
from rugbot.utils.logger import get_logger

logger = get_logger(__name__)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="rug_pairs_lab",
        description="paper only, no orders — alpha-extraction lab on finalized discover_trades (base rates + feature lift)",
        epilog="Reads .state/discover/rugbot.db finalized trades. Fail-closed if insufficient labels.",
    )
    p.add_argument("--state-dir", type=Path, default=Path(".state/discover"))
    p.add_argument("--json", action="store_true", help="emit JSON report")
    p.add_argument(
        "--entry-delay-slots",
        type=int,
        default=None,
        help="decision latency in slots before entry window (default 25)",
    )
    p.add_argument(
        "--entry-window-slots",
        type=int,
        default=None,
        help="entry window length in slots (default 50)",
    )
    p.add_argument(
        "--horizon-slots",
        type=int,
        default=None,
        help="label horizon / vertical barrier in slots (default 750)",
    )
    p.add_argument(
        "--sniper-window-slots",
        type=int,
        default=None,
        help="first-seconds buyer window for sniper_ratio (default 5)",
    )
    p.add_argument(
        "--size-sol", type=float, default=None, help="paper position size in SOL"
    )
    p.add_argument(
        "--tp",
        type=str,
        default=None,
        help="comma-separated take-profit pcts, e.g. 100,400,900",
    )
    p.add_argument("--sl", type=float, default=None, help="stop-loss pct (default 40)")
    p.add_argument(
        "--min-labels",
        type=int,
        default=None,
        help="minimum labeled launches before statistics (default 30)",
    )
    p.add_argument(
        "--min-bucket-count",
        type=int,
        default=None,
        help="minimum launches per tercile bucket (default 30)",
    )
    return p


def _build_config(args: argparse.Namespace) -> PairsLabConfig:
    overrides: dict[str, object] = {}
    if args.entry_delay_slots is not None:
        overrides["entry_delay_slots"] = int(args.entry_delay_slots)
    if args.entry_window_slots is not None:
        overrides["entry_window_slots"] = int(args.entry_window_slots)
    if args.horizon_slots is not None:
        overrides["horizon_slots"] = int(args.horizon_slots)
    if args.sniper_window_slots is not None:
        overrides["sniper_window_slots"] = int(args.sniper_window_slots)
    if args.size_sol is not None:
        overrides["position_size_sol"] = float(args.size_sol)
    if args.tp is not None:
        overrides["tp_levels_pct"] = tuple(
            float(part) for part in str(args.tp).split(",") if part.strip()
        )
    if args.sl is not None:
        overrides["sl_pct"] = float(args.sl)
    if args.min_labels is not None:
        overrides["min_labels"] = int(args.min_labels)
    if args.min_bucket_count is not None:
        overrides["min_bucket_count"] = int(args.min_bucket_count)
    return dataclasses.replace(PairsLabConfig(), **overrides)


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        config = _build_config(args)
    except (TypeError, ValueError) as exc:
        print(json.dumps({"status": "abstain", "message": f"config error: {exc}"}))
        return 1

    state_dir: Path = args.state_dir
    db_path = state_dir / "rugbot.db"
    if not db_path.exists():
        msg = f"no finalized DB at {db_path} (fail-closed)"
        if args.json:
            print(
                json.dumps(
                    {"status": "abstain", "message": msg, "insufficient_data": True}
                )
            )
        else:
            print(msg)
        return 1

    db = DatabaseManager(db_path)
    ensure_discover_schema(db)
    try:
        launches = [
            dict(row)
            for row in db.connection.execute(
                "SELECT * FROM discover_launches ORDER BY created_slot ASC"
            ).fetchall()
        ]
        trades = [
            dict(row)
            for row in db.connection.execute(
                "SELECT * FROM discover_trades ORDER BY slot ASC"
            ).fetchall()
        ]
    finally:
        db.close()

    report = run_pairs_lab(launches=launches, trades=trades, config=config)

    if args.json:
        payload = result_to_json(report)
        payload["state_dir"] = str(state_dir)
        print(json.dumps(payload, sort_keys=True))
    else:
        print(format_human(report))

    if report.insufficient_data:
        return 1
    return 0
