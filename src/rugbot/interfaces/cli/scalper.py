"""Paper-only scalper backtest CLI — reads finalized discover_trades and reports PnL.

No live orders. Validates expectancy before any live authorization.
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

from rugbot.backtest.scalper_backtest import (
    format_human,
    result_to_json,
    run_scalper_backtest,
)
from rugbot.discover.store import ensure_discover_schema
from rugbot.storage.config_store import load_scalper_config_db
from rugbot.storage.database import DatabaseManager
from rugbot.utils.logger import get_logger

logger = get_logger(__name__)

SINCE_RE = re.compile(r"^\s*(\d+)\s*([hHdDmMsS])\s*$")


def _parse_since(since: str | None) -> str | None:
    if since is None:
        return None
    m = SINCE_RE.match(since)
    if not m:
        if "T" in since or "-" in since:
            return since
        raise ValueError(f"invalid --since value: {since!r} (expected e.g. 24h, 7d)")
    val = int(m.group(1))
    unit = m.group(2).lower()
    import datetime as dt

    now = dt.datetime.now(dt.UTC)
    if unit == "h":
        since_dt = now - dt.timedelta(hours=val)
    elif unit == "d":
        since_dt = now - dt.timedelta(days=val)
    elif unit == "m":
        since_dt = now - dt.timedelta(minutes=val)
    elif unit == "s":
        since_dt = now - dt.timedelta(seconds=val)
    else:
        raise ValueError(f"unknown unit {unit}")
    return since_dt.isoformat()


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="rug_scalp",
        description="paper only, no orders, validates expectancy before any live authorization — scalper backtest on finalized discover_trades",
        epilog="Reads .state/discover/rugbot.db finalized trades. Fail-closed if insufficient data.",
    )
    p.add_argument("--state-dir", type=Path, default=Path(".state/discover"))
    p.add_argument(
        "--since", type=str, default="24h", help="window e.g. 24h, 7d (default 24h)"
    )
    p.add_argument("--json", action="store_true", help="emit JSON report")
    p.add_argument(
        "--min-trades", type=int, default=None, help="override min_trades_for_entry"
    )
    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        since_iso = _parse_since(args.since) if args.since else None
    except ValueError as exc:
        print(json.dumps({"status": "abstain", "message": str(exc)}))
        return 1

    try:
        # DB-backed scalper config (fail-closed)
        cfg = load_scalper_config_db(args.state_dir)
        if args.min_trades is not None:
            import dataclasses

            cfg = dataclasses.replace(cfg, min_trades_for_entry=int(args.min_trades))
    except Exception as exc:
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
        if since_iso is not None:
            trades_rows = db.connection.execute(
                "SELECT * FROM discover_trades WHERE slot >= (SELECT COALESCE(MAX(created_slot),0) FROM discover_launches) - 200000 OR 1=1 ORDER BY slot ASC"
            ).fetchall()
            launches_all = db.connection.execute(
                "SELECT * FROM discover_launches WHERE created_at >= ? ORDER BY created_slot ASC",
                (since_iso,),
            ).fetchall()
            mints_in_window = {str(r["mint"]) for r in launches_all}
            if mints_in_window:
                trades_rows = [
                    r for r in trades_rows if str(r["mint"]) in mints_in_window
                ]
            launches = [dict(r) for r in launches_all]
        else:
            trades_rows = db.connection.execute(
                "SELECT * FROM discover_trades ORDER BY slot ASC"
            ).fetchall()
            launches_all = db.connection.execute(
                "SELECT * FROM discover_launches ORDER BY created_slot ASC"
            ).fetchall()
            launches = [dict(r) for r in launches_all]
        trades = [dict(r) for r in trades_rows]
    finally:
        db.close()

    result = run_scalper_backtest(trades=trades, launches=launches, config=cfg)

    if args.json:
        payload = result_to_json(result)
        payload["state_dir"] = str(state_dir)
        payload["since"] = args.since
        payload["since_iso"] = since_iso
        print(json.dumps(payload, sort_keys=True))
    else:
        print(format_human(result))

    if result.insufficient_data:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
