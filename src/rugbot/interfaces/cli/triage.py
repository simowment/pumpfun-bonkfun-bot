"""Measurement data sheet for one candidate mint (no verdicts).

Reports four blocks — entity activity, bundler profile, backtest, heads-up —
as measurements only. Thresholds appear as reference values, never pass/fail.
"""

# ruff: noqa: C901, PLR0912, PLR0915, PLR0913, PLR2004, PLC0415, TRY003, S608, RUF007

from __future__ import annotations

import argparse
import json
import sqlite3
import statistics
import sys
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from rugbot.backtest.runners.creator_backtest_runner import (
    CreatorBacktestConfig,
    resolve_target_samples,
    run_creator_tp_sl_grid_search,
)
from rugbot.integrations.pumpfun_api import get_client
from rugbot.tracker.entity_history import build_launch_history
from rugbot.tracker.funding_chain import (
    enumerate_funded_paged,
    is_cex_shaped_source,
    walk_upstream,
)
from rugbot.utils.logger import get_logger

logger = get_logger(__name__)

REFERENCE_ACTIVITY_N = 10
CEX_FLEET_UNRESOLVED = "unresolved (CEX funder — cannot attribute)"
_DISCOVER_DB_CANDIDATES = (
    Path(".state/discover/rugbot.db"),
    Path(".state/rugbot.db"),
    Path("state.sqlite3"),
)
_BURNER_LOOKUP_CAP = 40
_BUNDLER_WIN_MULT = 2.0


def classify_funding_shape(
    *,
    creator_creations: int,
    upstream_depth: int,
    source_recipient_count: int,
    source_creation_count: int,
) -> str:
    """Classify the funding shape around a creator.

    Args:
        creator_creations: Tokens created by the mint's creator wallet.
        upstream_depth: Wallets visited above the creator on the walk.
        source_recipient_count: Distinct recipients funded by source S.
        source_creation_count: Tokens created by source S itself.

    Returns:
        One of ``"1 direct"``, ``"2 CEX-band"``, ``"3 relay"``.
    """
    del creator_creations, source_recipient_count
    if upstream_depth >= 2:
        return "3 relay"
    if source_creation_count >= 1:
        return "1 direct"
    return "2 CEX-band"


def cadence_seconds(timestamps: Sequence[int | float]) -> float | None:
    """Return the median gap between sorted launch timestamps.

    Args:
        timestamps: Launch timestamps in seconds (or ms, consistently).

    Returns:
        Median gap, or None when fewer than two timestamps exist.
    """
    if len(timestamps) < 2:
        return None
    ordered = sorted(float(t) for t in timestamps)
    gaps = [b - a for a, b in zip(ordered, ordered[1:], strict=False)]
    return float(statistics.median(gaps))


def assemble_sheet(
    *,
    mint: str,
    creator: str,
    archetype: str,
    funding_shape: str,
    funding_source: str | None,
    upstream_depth: int,
    entity_activity: Mapping[str, Any],
    bundler_profile: Mapping[str, Any],
    backtest: Mapping[str, Any],
    next_action: str,
) -> dict[str, Any]:
    """Assemble the measurement sheet payload (no verdict fields).

    Args:
        mint: Candidate mint address.
        creator: Resolved creator wallet.
        archetype: ``"Type 1"`` or ``"Type 2"`` measurement label.
        funding_shape: ``"1 direct"`` / ``"2 CEX-band"`` / ``"3 relay"``.
        funding_source: Funding hub/origin wallet, if resolved.
        upstream_depth: Wallets visited above the creator.
        entity_activity: N launches, cadence, first/last, active flag.
        bundler_profile: Bundler winrate or unavailable reason.
        backtest: Net EV headline, per-TP rows, samples, ATH, entry basis.
        next_action: Measurement follow-up instruction (not a verdict).

    Returns:
        JSON-safe sheet dict with the four blocks and a reference column.
    """
    return {
        "mint": mint,
        "creator": creator,
        "entity_activity": dict(entity_activity),
        "bundler_profile": dict(bundler_profile),
        "backtest": dict(backtest),
        "heads_up": {
            "archetype": archetype,
            "funding_shape": funding_shape,
            "funding_source": funding_source,
            "upstream_depth": upstream_depth,
            "next_action": next_action,
        },
        "reference": {
            "activity_n": REFERENCE_ACTIVITY_N,
            "note": "thresholds are reference lines only, not pass/fail",
        },
    }


def _parse_grid(raw: str | None, default: tuple[float, ...]) -> tuple[float, ...]:
    """Parse a comma-separated grid, falling back to ``default``."""
    if not raw:
        return default
    vals = [p.strip() for p in raw.split(",") if p.strip()]
    return tuple(float(v) for v in vals) if vals else default


def _launch_fetch(wallet: str) -> Sequence[Mapping[str, object]] | None:
    """Fetch one wallet's creator-index coin page (cached client)."""
    try:
        page = get_client().fetch_user_created_coins(wallet, limit=50, offset=0)
    except Exception:  # noqa: BLE001
        return None
    if not isinstance(page, dict):
        return None
    coins = page.get("coins")
    return coins if isinstance(coins, list) else None


def _coin_count(wallet: str) -> tuple[int, list[int]]:
    """Return ``(count, created_ms list)`` for one wallet's creations."""
    coins = _launch_fetch(wallet)
    if not coins:
        return 0, []
    stamps: list[int] = []
    for coin in coins:
        if not isinstance(coin, Mapping):
            continue
        created = coin.get("created_timestamp")
        if isinstance(created, int):
            stamps.append(created)
    return len(coins), stamps


def creator_only_activity(
    creator_stamps_ms: Sequence[int], *, active_days: float
) -> dict[str, Any]:
    """Build creator-only entity activity for a CEX-shaped funding source.

    A CEX-shaped funder pays many unrelated users, so its fleet MUST NOT be
    attributed to one entity: activity falls back to the creator wallet's own
    lifetime launches and the fleet is marked unresolved.

    Args:
        creator_stamps_ms: Creator's own token creation timestamps (ms).
        active_days: Active window in days.

    Returns:
        Entity-activity mapping with ``fleet`` unresolved and no verdict.
    """
    stamps_s = [s / 1000 for s in creator_stamps_ms if s > 1_000_000_000_000]
    if not stamps_s:
        stamps_s = [float(s) for s in creator_stamps_ms]
    now_s = time.time()
    last_s = max(stamps_s) if stamps_s else None
    first_s = min(stamps_s) if stamps_s else None
    return {
        "n_launches": len(creator_stamps_ms),
        "cadence_seconds": cadence_seconds(stamps_s),
        "first_launch_s": first_s,
        "last_launch_s": last_s,
        "active": bool(last_s is not None and (now_s - last_s) <= active_days * 86400),
        "active_window_days": float(active_days),
        "funded_recipients": None,
        "history_warning": None,
        "fleet": CEX_FLEET_UNRESOLVED,
    }


def _resolve_creator(mint: str) -> str:
    """Resolve a mint to its creator wallet via the canonical resolver."""
    from rugbot.intelligence.token_resolver import resolve_token_or_wallet
    from rugbot.runtime.config import load_provider_settings, resolve_dotenv

    resolve_dotenv()
    providers = load_provider_settings()
    rpc = providers.rpc_http if providers else ""
    fallback = providers.rpc_http_fallbacks if providers else ()
    try:
        resolved = resolve_token_or_wallet(
            mint, rpc_url=rpc, fallback_endpoints=fallback
        )
        if resolved.target_wallet:
            return resolved.target_wallet
    except Exception as exc:  # noqa: BLE001
        logger.debug("token resolver failed for %s: %s", mint[:8], exc)
    token = get_client().fetch_token(mint)
    creator = token.get("creator") if isinstance(token, dict) else None
    if isinstance(creator, str) and creator:
        return creator
    raise ValueError(f"creator unavailable for mint {mint}")


def _bundler_profile(entity_mints: Sequence[str]) -> dict[str, Any]:
    """Compute best-effort operator winrate from persisted trade evidence.

    Never fabricates: with no ``discover_trades`` rows for the entity's
    mints the profile reports unavailable with the reason.
    """
    db_path: Path | None = next(
        (p for p in _DISCOVER_DB_CANDIDATES if p.exists()), None
    )
    if db_path is None or not entity_mints:
        return {"status": "unavailable", "reason": "no trade data"}
    try:
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        tables = {
            r[0]
            for r in cur.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        if "discover_trades" not in tables:
            conn.close()
            return {"status": "unavailable", "reason": "no trade data"}
        placeholders = ",".join("?" for _ in entity_mints)
        rows = cur.execute(
            "SELECT mint, slot, side, wallet, price_ppm FROM discover_trades "
            f"WHERE mint IN ({placeholders})",
            tuple(entity_mints),
        ).fetchall()
        conn.close()
    except Exception as exc:  # noqa: BLE001
        logger.debug("bundler profile query failed: %s", exc)
        return {"status": "unavailable", "reason": "no trade data"}
    if not rows:
        return {"status": "unavailable", "reason": "no trade data"}
    by_mint: dict[str, list[Any]] = {}
    for row in rows:
        by_mint.setdefault(str(row["mint"]), []).append(row)
    wins = 0
    scored = 0
    hold_gaps: list[float] = []
    dump_gaps: list[float] = []
    for mint_rows in by_mint.values():
        prices = [int(r["price_ppm"]) for r in mint_rows if r["price_ppm"]]
        if len(prices) < 2:
            continue
        scored += 1
        entry = min(prices)
        peak = max(prices)
        if entry > 0 and peak >= entry * _BUNDLER_WIN_MULT:
            wins += 1
        slots = sorted(int(r["slot"]) for r in mint_rows if r["slot"] is not None)
        if len(slots) >= 2:
            hold_gaps.append(float(slots[-1] - slots[0]))
        sells = sorted(
            int(r["slot"])
            for r in mint_rows
            if r["side"] == "sell" and r["slot"] is not None
        )
        if slots and sells:
            dump_gaps.append(float(sells[0] - slots[0]))
    if not scored:
        return {"status": "unavailable", "reason": "no trade data"}
    return {
        "status": "ok",
        "source": "discover_trades",
        "mints_with_trades": scored,
        "wins": wins,
        "winrate_pct": round(wins / scored * 100, 2),
        "avg_hold_slots": round(sum(hold_gaps) / len(hold_gaps), 2)
        if hold_gaps
        else None,
        "median_dump_gap_slots": (
            round(float(statistics.median(dump_gaps)), 2) if dump_gaps else None
        ),
    }


def build_parser() -> argparse.ArgumentParser:
    """Build the argument parser for the triage command."""
    parser = argparse.ArgumentParser(
        prog="rug_triage",
        description="Measurement data sheet for one candidate mint (no verdicts).",
    )
    parser.add_argument("mint", help="Candidate mint address.")
    parser.add_argument("--json", action="store_true", help="Emit machine JSON only.")
    parser.add_argument(
        "--active-days",
        type=float,
        default=7.0,
        help="Active window in days (default: 7).",
    )
    parser.add_argument("--quote", type=float, default=0.3, help="Quote size SOL.")
    parser.add_argument("--tp", default=None, help="TP grid pct comma list.")
    parser.add_argument("--sl", default=None, help="SL grid pct comma list.")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run the triage measurement sheet.

    Args:
        argv: Optional argument vector; defaults to ``sys.argv``.

    Returns:
        Process exit code: 0 normally, 1 when the mint cannot be resolved.
    """
    args = build_parser().parse_args(argv)
    mint = str(args.mint).strip()
    try:
        creator = _resolve_creator(mint)
    except Exception as exc:  # noqa: BLE001
        if bool(args.json):
            print(json.dumps({"mint": mint, "error": str(exc)}))
        else:
            print(f"[rug_triage] resolve failed: {exc}", file=sys.stderr)
        return 1

    try:
        walk = walk_upstream(creator)
    except Exception as exc:  # noqa: BLE001
        logger.debug("walk_upstream failed: %s", exc)
        walk = None
    nodes = list(walk.nodes) if walk is not None else []
    hub = walk.hub if walk is not None else None
    source = hub or (nodes[-1].wallet if nodes else creator)
    upstream_depth = max(0, len(nodes) - 1)

    creator_creations, creator_stamps = _coin_count(creator)
    source_creations, _ = (
        _coin_count(source) if source != creator else (creator_creations, [])
    )

    try:
        transfers = enumerate_funded_paged(source)
    except Exception as exc:  # noqa: BLE001
        logger.debug("enumerate_funded_paged failed: %s", exc)
        transfers = ()
    recipients = sorted({t.recipient for t in transfers})
    cex_source = is_cex_shaped_source(
        source_creation_count=source_creations,
        source_recipient_count=len(recipients),
    )

    shape = classify_funding_shape(
        creator_creations=creator_creations,
        upstream_depth=upstream_depth,
        source_recipient_count=len(recipients),
        source_creation_count=source_creations,
    )

    if cex_source:
        # CEX-shaped funder: its recipients are unrelated users, so the fleet
        # is not enumerated and activity is the creator's own launches.
        entity_activity = creator_only_activity(
            creator_stamps, active_days=float(args.active_days)
        )
    else:
        history = build_launch_history(
            source, transfers=transfers, launch_fetch=_launch_fetch
        )
        stamps_ms = [
            e.created_at_ms for e in history.launches if e.created_at_ms is not None
        ]
        stamps_s = [s / 1000 for s in stamps_ms]
        n_launches = len(history.launches)
        if n_launches == 0:
            # Fall back to the creator's own creations when the funder has none.
            stamps_s = [s / 1000 for s in creator_stamps if s > 1_000_000_000_000]
            if not stamps_s:
                stamps_s = [float(s) for s in creator_stamps]
            n_launches = len(creator_stamps)
        cadence = cadence_seconds(stamps_s)
        now_s = time.time()
        last_s = max(stamps_s) if stamps_s else None
        first_s = min(stamps_s) if stamps_s else None
        active = (
            last_s is not None and (now_s - last_s) <= float(args.active_days) * 86400
        )
        entity_activity = {
            "n_launches": n_launches,
            "cadence_seconds": cadence,
            "first_launch_s": first_s,
            "last_launch_s": last_s,
            "active": bool(active),
            "active_window_days": float(args.active_days),
            "funded_recipients": len(recipients),
            "history_warning": history.warning,
            "fleet": f"{len(recipients)} funded recipients",
        }

    archetype = "Type 1" if shape == "1 direct" and creator_creations >= 2 else "Type 2"
    if archetype == "Type 1":
        next_action = (
            f"re-arm listener on dev wallet {creator} (observe-only; no auto-arm)"
        )
    elif cex_source:
        next_action = (
            f"funding source {source} is CEX-shaped (shared hot wallet) — "
            f"cannot attribute a fleet; score creator wallet {creator} alone "
            "(manual; observe-only; no auto-arm)"
        )
    elif source:
        next_action = (
            f"watch funding source {source} for staged transfers to fresh burners "
            "(manual; observe-only; no auto-arm)"
        )
    else:
        next_action = (
            f"trace upstream funder of {creator} to detect staged burners "
            "(manual; observe-only; no auto-arm)"
        )

    samples = resolve_target_samples(mint, entity=not cex_source)
    default_config = CreatorBacktestConfig()
    config = CreatorBacktestConfig(
        quote_size_sol=float(args.quote),
        tp_grid=_parse_grid(args.tp, default_config.tp_grid),
        sl_grid=_parse_grid(args.sl, default_config.sl_grid),
    )
    report = run_creator_tp_sl_grid_search(
        samples, config, target=mint, mode="wallet" if cex_source else "entity"
    )
    entity_mints = [s.mint for s in report.samples] or [mint]
    bundler = _bundler_profile(entity_mints)
    backtest = {
        "net_ev_sol": report.optimal_ev,
        "optimal_tp": report.optimal_tp,
        "optimal_sl": report.optimal_sl,
        "n_samples": len(report.samples),
        "insufficient_data": report.insufficient_data,
        "message": report.message,
        "per_tp": [
            {
                "tp_pct": e.tp_pct,
                "sl_pct": e.sl_pct,
                "net_ev_sol": e.net_ev_sol,
                "net_pnl_sol": e.net_pnl_sol,
                "winrate_pct": e.winrate_pct,
                "fees_sol": e.fees_sol,
                "max_drawdown_sol": e.max_drawdown_sol,
            }
            for e in report.evaluations
        ],
        "per_sample_ath": [
            {"mint": s.mint, "ath_multiplier": s.ath_multiplier} for s in report.samples
        ],
        "entry_basis_counts": [list(x) for x in report.entry_basis_counts],
    }

    sheet = assemble_sheet(
        mint=mint,
        creator=creator,
        archetype=archetype,
        funding_shape=shape,
        funding_source=source,
        upstream_depth=upstream_depth,
        entity_activity=entity_activity,
        bundler_profile=bundler,
        backtest=backtest,
        next_action=next_action,
    )

    if bool(args.json):
        print(json.dumps(sheet, sort_keys=True))
        return 0
    print("=" * 78)
    print(f" RUG TRIAGE  {mint}")
    print("=" * 78)
    print("ENTITY ACTIVITY")
    print(f"  launches: {entity_activity['n_launches']}")
    print(f"  cadence (median gap s): {entity_activity['cadence_seconds']}")
    print(
        f"  first: {entity_activity['first_launch_s']}  last: {entity_activity['last_launch_s']}"
    )
    print(
        f"  active (within {entity_activity['active_window_days']}d): "
        f"{entity_activity['active']}"
    )
    print("BUNDLER PROFILE")
    if bundler.get("status") == "ok":
        print(
            f"  bundler winrate: {bundler['winrate_pct']}% "
            f"({bundler['wins']}/{bundler['mints_with_trades']}, source=discover_trades)"
        )
        print(f"  avg hold (slots): {bundler.get('avg_hold_slots')}")
        print(
            f"  dump timing (median sell gap slots): {bundler.get('median_dump_gap_slots')}"
        )
    else:
        print(f"  bundler winrate: unavailable ({bundler.get('reason')})")
    print("BACKTEST")
    print(
        f"  net EV (primary): {backtest['net_ev_sol']} SOL "
        f"(TP={backtest['optimal_tp']} SL={backtest['optimal_sl']})"
    )
    print(f"  samples: {backtest['n_samples']}  {backtest['message']}")
    for row in backtest["per_tp"]:
        print(
            f"    TP +{row['tp_pct']}% / SL -{row['sl_pct']}%: "
            f"net EV {row['net_ev_sol']}  net PnL {row['net_pnl_sol']}  "
            f"winrate {row['winrate_pct']}%  fees {row['fees_sol']}  "
            f"maxDD {row['max_drawdown_sol']}"
        )
    print("HEADS-UP")
    print(f"  archetype: {archetype}")
    print(f"  funding shape: {shape}  source: {source}  depth: {upstream_depth}")
    print(f"  next action: {next_action}")
    print("  reference: N>=10 is an activity/sample-size indicator only")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
