"""CLI for rug_discover — headless collect daemon + enrich/candidates/dossier/status."""

# ruff: noqa: BLE001, C901, FBT001, PLC0415, PLR0911, PLR0912, PLR0915

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from rugbot.discover.collector import run_collect


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="rug_discover",
        description="rug_discover — headless collect persists via PID file; Web may remain closed. DB WAL allows concurrent candidates/dossier reads.",
        epilog="Collect runs headless (PID .state/discover/rug_discover.pid). Candidates/dossier read same WAL DB concurrently. Web can stay closed.",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    collect = sub.add_parser("collect", help="headless PumpPortal collect daemon")
    collect.add_argument(
        "--state-dir",
        type=Path,
        default=Path(".state/discover"),
        help="state directory (default: .state/discover)",
    )
    collect.add_argument(
        "--jsonl",
        action="store_true",
        help="also append per-mint JSONL observations",
    )
    collect.add_argument(
        "--endpoint",
        type=str,
        default=None,
        help="override SOLANA_RPC_HTTP endpoint",
    )
    collect.add_argument(
        "--duration-seconds",
        type=float,
        default=None,
        help="stop after a bounded duration; omitted means run continuously",
    )

    enrich = sub.add_parser("enrich", help="historique batch enrich for wallet or mint")
    enrich.add_argument("wallet_or_mint", help="wallet pubkey or pump mint address")
    enrich.add_argument("--json", action="store_true", help="emit JSON dossier")
    enrich.add_argument(
        "--entity",
        action="store_true",
        help="dedup mint→earliest across entity wallets",
    )
    enrich.add_argument(
        "--state-dir",
        type=Path,
        default=Path(".state/discover"),
        help="state directory (default: .state/discover)",
    )

    cand = sub.add_parser(
        "candidates",
        help="bible Method 2: bundler wallets for tokens created ~1h ago and dumped",
    )
    cand.add_argument(
        "--role",
        type=str,
        choices=["bundler", "creator"],
        default="bundler",
        help="query role: bundler (default) or creator",
    )
    cand.add_argument(
        "--age-min", type=int, default=50, help="min age minutes (default: 50)"
    )
    cand.add_argument(
        "--age-max", type=int, default=70, help="max age minutes (default: 70)"
    )
    cand.add_argument(
        "--dumped",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="filter dumped only (default: true)",
    )
    cand.add_argument(
        "--mc-le",
        type=int,
        default=15000,
        help="Axiom MC <= threshold USD (default: 15000, 0=disable)",
    )
    cand.add_argument(
        "--vol-min",
        type=int,
        default=20000,
        help="vol min USD (default: 20000, 0=disable)",
    )
    cand.add_argument(
        "--vol-max",
        type=int,
        default=30000,
        help="vol max USD (default: 30000, 0=disable)",
    )
    cand.add_argument(
        "--since",
        type=str,
        default=None,
        help="fallback since window e.g. 24h, 7d (used if age window empty or for broad scan)",
    )
    cand.add_argument("--json", action="store_true", help="emit JSON array")
    cand.add_argument(
        "--max-offset",
        type=int,
        default=4,
        help="max tx_index offset for bundler (default: 4, B0=+1 .. B3=+4)",
    )
    cand.add_argument(
        "--limit", type=int, default=50, help="max rows (default: 50, max 500)"
    )
    cand.add_argument(
        "--max-creations",
        type=int,
        default=5,
        help="bible max creations per dev/bundler (default: 5, 0=disable, 1..100)",
    )
    cand.add_argument(
        "--state-dir",
        type=Path,
        default=Path(".state/discover"),
        help="state directory (default: .state/discover)",
    )

    dossier = sub.add_parser("dossier", help="full dossier for a wallet")
    dossier.add_argument("wallet", help="wallet pubkey")
    dossier.add_argument("--json", action="store_true", help="emit JSON dossier")
    dossier.add_argument(
        "--state-dir",
        type=Path,
        default=Path(".state/discover"),
        help="state directory (default: .state/discover)",
    )

    basket = sub.add_parser(
        "basket",
        help="resume a wallet's cross-token scan against one creator's launches",
    )
    basket.add_argument("wallet", help="wallet whose Pump transactions are scanned")
    basket.add_argument(
        "--creator",
        required=True,
        help="creator wallet whose complete Pump.fun mint index is matched",
    )
    basket.add_argument(
        "--pages",
        type=int,
        default=5,
        choices=range(1, 6),
        metavar="1-5",
        help="Solscan pages to advance in this pass (default: 5)",
    )
    basket.add_argument("--json", action="store_true", help="emit JSON result")
    basket.add_argument(
        "--state-dir",
        type=Path,
        default=Path(".state/discover"),
        help="state directory (default: .state/discover)",
    )

    basket_windows = sub.add_parser(
        "basket-windows",
        help="decode a wallet's trades after cached finalized entity launches",
    )
    basket_windows.add_argument("wallet", help="wallet whose trades are decoded")
    basket_windows.add_argument(
        "--creator",
        required=True,
        help="creator owning the cached finalized launches",
    )
    basket_windows.add_argument(
        "--offset-slots",
        type=int,
        default=120,
        choices=range(1, 301),
        metavar="1-300",
        help="slots after each creation to scan (default: 120)",
    )
    basket_windows.add_argument("--json", action="store_true", help="emit JSON result")
    basket_windows.add_argument(
        "--state-dir",
        type=Path,
        default=Path(".state/discover"),
        help="state directory (default: .state/discover)",
    )

    status = sub.add_parser(
        "status", help="PID alive, health last_heartbeat, launches count"
    )
    status.add_argument("--json", action="store_true", help="emit JSON status")
    status.add_argument(
        "--state-dir",
        type=Path,
        default=Path(".state/discover"),
        help="state directory (default: .state/discover)",
    )

    return parser


def _print_status(state_dir: Path, as_json: bool) -> int:
    pid_path = state_dir / "rug_discover.pid"
    health_path = state_dir / "health.json"
    db_path = state_dir / "rugbot.db"

    pid_alive = False
    pid_val: int | None = None
    if pid_path.exists():
        try:
            pid_val = int(pid_path.read_text(encoding="utf-8").strip())
            # check alive (best-effort)
            import os

            try:
                os.kill(pid_val, 0)
                pid_alive = True
            except OSError:
                pid_alive = False
            except Exception:
                pid_alive = False
        except Exception:
            pid_val = None

    health: dict[str, object] | None = None
    if health_path.exists():
        try:
            health = json.loads(health_path.read_text(encoding="utf-8"))
        except Exception:
            health = None

    launches_count: int | None = None
    try:
        from rugbot.discover.store import ensure_discover_schema
        from rugbot.storage.database import DatabaseManager

        db = DatabaseManager(db_path)
        ensure_discover_schema(db)
        row = db.connection.execute(
            "SELECT COUNT(*) as c FROM discover_launches"
        ).fetchone()
        launches_count = int(row["c"]) if row is not None else 0
    except Exception:
        launches_count = None

    payload = {
        "state_dir": str(state_dir),
        "pid": pid_val,
        "pid_alive": pid_alive,
        "health": health,
        "launches_count": launches_count,
        "note": "collect runs headless via PID file; candidates/dossier read same WAL DB concurrently; Web may remain closed.",
    }
    if as_json:
        print(json.dumps(payload, sort_keys=True))
    else:
        print(f"state_dir: {state_dir}")
        print(f"pid: {pid_val} alive={pid_alive}")
        print(f"health: {json.dumps(health) if health else '-'}")
        print(f"launches: {launches_count if launches_count is not None else '-'}")
        print(
            "note: collect runs headless; Web may remain closed (WAL concurrent reads)"
        )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "collect":
        state_dir: Path = args.state_dir
        if not isinstance(state_dir, Path):
            parser.error("--state-dir must be a path")
        try:
            asyncio.run(
                run_collect(
                    state_dir,
                    use_jsonl=bool(args.jsonl),
                    endpoint=args.endpoint,
                    duration_seconds=args.duration_seconds,
                )
            )
        except KeyboardInterrupt:
            return 0
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return 1
        return 0
    if args.command == "enrich":
        state_dir: Path = args.state_dir
        wallet_or_mint: str = args.wallet_or_mint
        as_json: bool = bool(args.json)
        use_entity: bool = bool(args.entity)
        try:
            from rugbot.discover.enricher import enrich_wallet

            report = enrich_wallet(
                wallet_or_mint, state_dir=state_dir, use_entity=use_entity
            )
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return 1
        except Exception as exc:
            print(f"enrich failed: {exc}", file=sys.stderr)
            return 1
        if as_json:
            print(json.dumps(report, sort_keys=True))
        else:
            print(f"wallet: {report.get('wallet')}")
            print(f"mints: {len(report.get('historical_mints', []))}")
            print(
                f"score: {json.dumps(report.get('score'))[:200] if report.get('score') else '-'}"
            )
            print(f"funding rows: {len(report.get('funding_chain', []))}")
            print(f"launches db: {len(report.get('launches', []))}")
        return 0
    if args.command == "candidates":
        state_dir: Path = args.state_dir
        limit: int = args.limit
        as_json: bool = bool(args.json)
        role: str = str(getattr(args, "role", "bundler"))
        age_min: int = int(getattr(args, "age_min", 50))
        age_max: int = int(getattr(args, "age_max", 70))
        dumped: bool = bool(getattr(args, "dumped", True))
        mc_le_raw = getattr(args, "mc_le", 15000)
        vol_min_raw = getattr(args, "vol_min", 20000)
        vol_max_raw = getattr(args, "vol_max", 30000)
        mc_le = None if int(mc_le_raw) == 0 else int(mc_le_raw)
        vol_min = None if int(vol_min_raw) == 0 else int(vol_min_raw)
        vol_max = None if int(vol_max_raw) == 0 else int(vol_max_raw)
        since = getattr(args, "since", None)
        max_offset = int(getattr(args, "max_offset", 4))
        max_creations_raw = getattr(args, "max_creations", 5)
        max_creations = None if int(max_creations_raw) == 0 else int(max_creations_raw)
        spam_excluded = 0
        try:
            from rugbot.discover.candidates import (
                query_bundler_candidates,
                query_creator_candidates,
            )

            if role == "creator":
                rows, _launches, fail_msg, spam_excluded = query_creator_candidates(
                    state_dir=state_dir,
                    age_min=age_min,
                    age_max=age_max,
                    dumped=dumped,
                    mc_le=mc_le,
                    vol_min=vol_min,
                    vol_max=vol_max,
                    since=since,
                    limit=limit,
                    max_creations=max_creations,
                )
            else:
                rows, _launches, fail_msg, spam_excluded = query_bundler_candidates(
                    state_dir=state_dir,
                    age_min=age_min,
                    age_max=age_max,
                    dumped=dumped,
                    mc_le=mc_le,
                    vol_min=vol_min,
                    vol_max=vol_max,
                    since=since,
                    limit=limit,
                    max_offset=max_offset,
                    max_creations=max_creations,
                )
            if fail_msg is not None:
                if as_json:
                    print(json.dumps([], sort_keys=True))
                else:
                    print(fail_msg)
                return 0
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return 1
        except Exception as exc:
            print(f"candidates failed: {exc}", file=sys.stderr)
            return 1
        if as_json:
            # bible max-creations: include spam count without breaking array for old consumers
            # emit object when filter active so caller can see exclusion
            payload: object = rows
            if max_creations is not None:
                key = (
                    "spam_creators_excluded"
                    if role == "creator"
                    else "spam_bundlers_excluded"
                )
                payload = {
                    "candidates": rows,
                    key: spam_excluded,
                    "max_creations": max_creations,
                    "role": role,
                }
            print(json.dumps(payload, sort_keys=True))
        elif not rows:
            print(
                f"no candidates (role={role} age {age_min}-{age_max}m dumped={dumped})"
            )
        else:
            print(
                f"{'#':>3} {'wallet':<44} {'mints':>5} {'SOL':>8} {'creators':>8} {'sells':>5} {'last_seen':<20}"
            )
            for idx, r in enumerate(rows, start=1):
                wallet = str(r.get("wallet", ""))[:44]
                mints = str(r.get("mints_count", "-"))
                sol = (
                    f"{float(r.get('total_sol', 0)):.3f}"
                    if isinstance(r.get("total_sol"), (int, float))
                    else "-"
                )
                creators = str(r.get("cross_entity", "-"))
                sells = str(r.get("sells", "-"))
                last_seen = str(r.get("last_seen") or "-")[:19]
                print(
                    f"{idx:>3} {wallet:<44} {mints:>5} {sol:>8} {creators:>8} {sells:>5} {last_seen:<20}"
                )
            if spam_excluded:
                label = (
                    "spam_creators_excluded"
                    if role == "creator"
                    else "spam_bundlers_excluded"
                )
                print(f"{label}: {spam_excluded} (max_creations={max_creations})")
        return 0
    if args.command == "dossier":
        state_dir: Path = args.state_dir
        wallet: str = args.wallet
        as_json: bool = bool(args.json)
        try:
            from rugbot.discover.candidates import load_dossier

            report = load_dossier(wallet, state_dir=state_dir)
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return 1
        except Exception as exc:
            print(f"dossier failed: {exc}", file=sys.stderr)
            return 1
        if as_json:
            print(json.dumps(report, sort_keys=True))
        else:
            print(f"wallet: {report.get('wallet')}")
            cand = report.get("candidate")
            if cand:
                print(f"candidate: {cand}")
            launches = report.get("launches") or report.get("launches_db") or []
            print(f"launches: {len(launches)}")
            if report.get("score"):
                print(f"score: {json.dumps(report.get('score'))[:300]}")
        return 0
    if args.command == "basket":
        state_dir: Path = args.state_dir
        wallet: str = args.wallet
        creator: str = args.creator
        pages: int = args.pages
        as_json: bool = bool(args.json)
        try:
            from rugbot.discover.basket import scan_wallet_basket
            from rugbot.integrations.pumpfun_creator_index import (
                fetch_pumpfun_created_tokens,
            )

            indexed_tokens = fetch_pumpfun_created_tokens(creator)
            report = scan_wallet_basket(
                wallet,
                entity_mints=frozenset(token.mint for token in indexed_tokens),
                state_dir=state_dir,
                max_pages=pages,
            )
            report["creator"] = creator
            report["indexed_mint_count"] = len(indexed_tokens)
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return 1
        except Exception as exc:
            print(f"basket failed: {exc}", file=sys.stderr)
            return 1
        if as_json:
            print(json.dumps(report, sort_keys=True))
        else:
            print(f"wallet: {wallet}")
            print(f"creator: {creator}")
            print(f"indexed mints: {len(indexed_tokens)}")
            print(f"pages scanned: {report['pages_scanned']}")
            print(f"candidate transactions: {report['candidate_count']}")
            print(f"status: {report['status']}")
            if report["warning"]:
                print(f"warning: {report['warning']}")
        return 0
    if args.command == "basket-windows":
        state_dir: Path = args.state_dir
        wallet: str = args.wallet
        creator: str = args.creator
        try:
            from rugbot.discover.basket import scan_wallet_launch_windows
            from rugbot.discover.store import (
                ensure_discover_schema,
                fetch_entity_mint_windows,
            )
            from rugbot.storage.database import DatabaseManager

            database = DatabaseManager(state_dir / "rugbot.db")
            ensure_discover_schema(database)
            launch_windows = fetch_entity_mint_windows(database, creator)
            if not launch_windows:
                print(
                    "no finalized entity mints are cached; "
                    "run rug_discover enrich first",
                    file=sys.stderr,
                )
                return 1
            report = asyncio.run(
                scan_wallet_launch_windows(
                    wallet,
                    launch_windows=launch_windows,
                    state_dir=state_dir,
                    offset_slots=args.offset_slots,
                )
            )
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return 1
        except Exception as exc:
            print(f"basket window scan failed: {exc}", file=sys.stderr)
            return 1
        if bool(args.json):
            print(json.dumps(report, sort_keys=True))
        else:
            print(f"wallet: {wallet}")
            print(f"creator: {creator}")
            print(
                f"windows complete: {report['complete_window_count']}/"
                f"{report['window_count']}"
            )
            print(f"tokens traded: {report['participating_token_count']}")
        return 0
    if args.command == "status":
        state_dir: Path = args.state_dir
        as_json: bool = bool(args.json)
        return _print_status(state_dir, as_json)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
