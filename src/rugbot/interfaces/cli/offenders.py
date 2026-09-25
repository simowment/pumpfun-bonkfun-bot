"""Repeat-offenders discovery funnel (measurement report, no verdicts).

Groups recent launches by ``(funder, funding-amount band)`` and reports
per-band launch counts, winrate (ATH multiple >= ``--win-multiple``) and
coverage. Reference thresholds (>=3 launches, >=70% winrate) appear as
reference values only, never as pass/fail verdicts.
"""

# ruff: noqa: TRY003, PLR0913

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from rugbot.backtest.runners.entry_resolver import trajectory_from_1s_candles
from rugbot.integrations.pumpfun_api import get_client
from rugbot.integrations.solscan import SolscanClient
from rugbot.interfaces.cli.triage import cadence_seconds
from rugbot.runtime.config import load_provider_settings, resolve_dotenv
from rugbot.tracker.funder_discovery import find_funding_edges
from rugbot.tracker.funding_edge_rpc import find_outbound_funding_edge
from rugbot.utils.logger import get_logger

logger = get_logger(__name__)

REFERENCE_MIN_LAUNCHES = 3
REFERENCE_MIN_WINRATE_PCT = 70.0
LISTING_PAGE_SIZE = 70
MIN_CADENCE_SAMPLES = 2
DEFAULT_ACTIVE_HOURS = 24.0
SECONDS_PER_DAY = 86400.0
SECONDS_PER_HOUR = 3600.0
PEAK_EPSILON = 1e-4


def default_max_pages(limit: int) -> int:
    """Return the page guard for a listing collection of ``limit`` launches.

    Args:
        limit: Desired number of launches.

    Returns:
        ``ceil(limit / 70) + 2`` (at least 1): one page per full window
        plus two spare pages for dedupe drift while the newest window
        shifts underneath the scan.
    """
    return max(1, -(-max(1, int(limit)) // LISTING_PAGE_SIZE) + 2)


def collect_recent_launches(
    limit: int,
    *,
    max_pages: int | None = None,
    lister: Callable[[int, int], list[dict]] | None = None,
) -> tuple[list[dict], int]:
    """Page the newest-launch listing until ``limit`` unique coins collect.

    The listing endpoint caps each response (~70 rows), so one call can
    never satisfy a large ``--limit``. Pages advance by ``offset`` while
    the newest window shifts, so coins are deduped by mint across pages.

    Args:
        limit: Desired number of unique launches.
        max_pages: Page guard; defaults to :func:`default_max_pages`.
        lister: ``(limit, offset) -> coins`` callable; defaults to the
            shared client's cached ``fetch_recent_launches``.

    Returns:
        Tuple of (unique coin dicts, truncated to ``limit``; pages
        actually fetched). Stops early when a page is empty, adds no new
        mint, or the collection reaches ``limit``. Never raises: a lister
        failure ends the scan with what is already collected.
    """
    if lister is None:
        lister = get_client().fetch_recent_launches
    cap = default_max_pages(limit) if max_pages is None else max(0, int(max_pages))
    seen: set[str] = set()
    coins: list[dict] = []
    pages = 0
    for page in range(cap):
        try:
            batch = lister(limit=LISTING_PAGE_SIZE, offset=page * LISTING_PAGE_SIZE)
        except Exception as exc:  # noqa: BLE001 - fail-soft: keep collected
            logger.debug("listing page %d failed: %s", page, exc)
            break
        pages += 1
        if not batch:
            break
        new = 0
        for coin in batch:
            if not isinstance(coin, Mapping):
                continue
            mint = coin.get("mint")
            if not isinstance(mint, str) or not mint or mint in seen:
                continue
            seen.add(mint)
            coins.append(coin)
            new += 1
        if new == 0 or len(coins) >= limit:
            break
    return coins[:limit], pages


def band_key(amount_sol: float, band: float) -> int:
    """Return the funding-amount band index for one edge amount.

    Args:
        amount_sol: Funding edge amount in SOL.
        band: Band width in SOL; must be positive.

    Returns:
        Integer band index ``round(amount_sol / band)``.

    Raises:
        ValueError: When ``band`` is not positive.
    """
    if band <= 0:
        raise ValueError("band must be positive")
    return round(float(amount_sol) / float(band))


def summarize_coverage(rows: Sequence[Mapping[str, Any]]) -> dict[str, int]:
    """Summarize funding-edge and candle coverage over launch rows.

    Args:
        rows: Per-launch rows with ``has_funding`` / ``has_candles`` flags.

    Returns:
        Coverage counts: launches, with_funding_edge, with_candles,
        no_funding_edge, no_candles.
    """
    with_funding = sum(1 for r in rows if bool(r.get("has_funding")))
    with_candles = sum(1 for r in rows if bool(r.get("has_candles")))
    total = len(rows)
    return {
        "launches": total,
        "with_funding_edge": with_funding,
        "with_candles": with_candles,
        "no_funding_edge": total - with_funding,
        "no_candles": total - with_candles,
    }


def aggregate_bands(
    rows: Sequence[Mapping[str, Any]],
    win_multiple: float,
    band: float = 0.5,
    *,
    now_s: float | None = None,
) -> list[dict[str, Any]]:
    """Aggregate scored launch rows by ``(funder, band index)``.

    Only rows with a resolved funder and amount join a band; the rest are
    reported via :func:`summarize_coverage`, never fabricated. A win is a
    launch with ``ath_multiple >= win_multiple``. Launches without candles
    are excluded from the winrate denominator and counted as ``no_candles``.

    Args:
        rows: Per-launch rows with funder, amount_sol, ath_multiple,
            has_funding, has_candles, created_ms and start_mcap keys.
        win_multiple: ATH multiple counting as a win.
        band: Band width in SOL used for ``band_key``.
        now_s: Current timestamp for activity calculation; defaults to now.

    Returns:
        Band dicts sorted by launches desc, each with funder, band index,
        amount range, launches, wins, winrate_pct, median/max ATH multiple,
        median start mcap, cadence, daily launch rate, active flag and
        coverage counts. No verdict fields.
    """
    grouped: dict[tuple[str, int], list[Mapping[str, Any]]] = {}
    for row in rows:
        funder = row.get("funder")
        amount = row.get("amount_sol")
        if not isinstance(funder, str) or not funder:
            continue
        if not isinstance(amount, (int, float)):
            continue
        key = (funder, band_key(float(amount), band))
        grouped.setdefault(key, []).append(row)
    current_time = now_s if now_s is not None else time.time()
    bands: list[dict[str, Any]] = []
    for (funder, index), members in grouped.items():
        ath_values = [
            float(m["ath_multiple"])
            for m in members
            if isinstance(m.get("ath_multiple"), (int, float))
        ]
        scored = len(ath_values)
        wins = sum(1 for v in ath_values if v >= win_multiple)
        mcaps = [
            float(m["start_mcap"])
            for m in members
            if isinstance(m.get("start_mcap"), (int, float))
        ]
        peak_mcaps = [
            float(m["peak_mcap"])
            for m in members
            if isinstance(m.get("peak_mcap"), (int, float))
        ]
        peak_seconds = [
            float(m["peak_sec"])
            for m in members
            if isinstance(m.get("peak_sec"), (int, float))
        ]
        no_candles = sum(1 for m in members if not bool(m.get("has_candles")))

        stamps_s = [
            float(m["created_ms"]) / 1000.0
            for m in members
            if isinstance(m.get("created_ms"), (int, float))
        ]
        first_launch_s = min(stamps_s) if stamps_s else None
        last_launch_s = max(stamps_s) if stamps_s else None
        cadence_s = (
            cadence_seconds(stamps_s) if len(stamps_s) >= MIN_CADENCE_SAMPLES else None
        )
        cadence_hours = (
            round(cadence_s / SECONDS_PER_HOUR, 2) if cadence_s is not None else None
        )

        if cadence_s is not None and cadence_s > 0:
            daily_rate = round(SECONDS_PER_DAY / cadence_s, 2)
        elif (
            len(members) >= MIN_CADENCE_SAMPLES
            and first_launch_s is not None
            and last_launch_s is not None
            and (last_launch_s - first_launch_s) > 0
        ):
            daily_rate = round(
                len(members) / ((last_launch_s - first_launch_s) / SECONDS_PER_DAY), 2
            )
        else:
            daily_rate = None

        last_launch_hours_ago = (
            round((current_time - last_launch_s) / SECONDS_PER_HOUR, 2)
            if last_launch_s is not None
            else None
        )
        active = bool(
            last_launch_hours_ago is not None
            and last_launch_hours_ago <= DEFAULT_ACTIVE_HOURS
        )

        mean_ath = float(round(statistics.mean(ath_values), 2)) if ath_values else None
        median_ath = (
            float(round(statistics.median(ath_values), 2)) if ath_values else None
        )
        min_ath = float(round(min(ath_values), 2)) if ath_values else None
        max_ath = float(round(max(ath_values), 2)) if ath_values else None

        mean_peak_sec = (
            float(round(statistics.mean(peak_seconds), 1)) if peak_seconds else None
        )
        median_peak_sec = (
            float(round(statistics.median(peak_seconds), 1)) if peak_seconds else None
        )

        mean_start_mcap = float(round(statistics.mean(mcaps), 1)) if mcaps else None
        median_start_mcap = float(round(statistics.median(mcaps), 1)) if mcaps else None

        mean_peak_mcap = (
            float(round(statistics.mean(peak_mcaps), 1)) if peak_mcaps else None
        )

        bands.append(
            {
                "funder": funder,
                "band_index": index,
                "band_sol": float(band),
                "amount_lo_sol": round((index - 0.5) * band, 4),
                "amount_hi_sol": round((index + 0.5) * band, 4),
                "launches": len(members),
                "scored": scored,
                "wins": wins,
                "winrate_pct": round(wins / scored * 100, 2) if scored else None,
                "mean_ath_multiple": mean_ath,
                "median_ath_multiple": median_ath,
                "min_ath_multiple": min_ath,
                "max_ath_multiple": max_ath,
                "mean_peak_seconds": mean_peak_sec,
                "median_peak_seconds": median_peak_sec,
                "mean_start_mcap": mean_start_mcap,
                "median_start_mcap": median_start_mcap,
                "mean_peak_mcap": mean_peak_mcap,
                "first_launch_s": first_launch_s,
                "last_launch_s": last_launch_s,
                "cadence_seconds": cadence_s,
                "cadence_hours": cadence_hours,
                "daily_launch_rate": daily_rate,
                "last_launch_hours_ago": last_launch_hours_ago,
                "active": active,
                "no_candles": no_candles,
                "no_funding_edge": 0,
            }
        )
    bands.sort(key=lambda b: (-int(b["launches"]), str(b["funder"])))
    return bands


def _resolve_candles(
    mint: str,
    created_ms: int | None,
    start_mcap: float | None,
) -> tuple[float | None, float | None, float | None, bool]:
    """Fetch candles and resolve ATH multiple, peak seconds, and peak mcap.

    Returns:
        Tuple of (ath, peak_sec, peak_mcap, has_candles).
    """
    try:
        candles = get_client().fetch_candlesticks(
            mint, interval="1s", limit=300, created_ts=0
        )
    except Exception as exc:  # noqa: BLE001 - fail-soft: no_candles
        logger.debug("candles failed for %s: %s", mint[:8], exc)
        return None, None, None, False

    if not candles:
        return None, None, None, False
    traj, resolved = trajectory_from_1s_candles(candles, created_ms=created_ms)
    if resolved is None:
        return None, None, None, False
    ath = float(resolved)
    peak_sec: float | None = None
    for sec, mult in traj:
        if abs(mult - ath) < PEAK_EPSILON:
            peak_sec = float(sec)
            break
    if peak_sec is None and traj:
        peak_sec = float(max(traj, key=lambda p: p[1])[0])
    peak_mcap = round(float(start_mcap) * ath, 2) if start_mcap is not None else None
    return ath, peak_sec, peak_mcap, True


def resolve_launch_row(
    *,
    mint: str,
    creator: str,
    created_ms: int | None,
    start_mcap: float | None,
    endpoint: str,
    solscan_client: SolscanClient | None,
    use_solscan: bool = False,
) -> dict[str, Any]:
    """Resolve one launch to a measurement row, fail-soft per launch.

    Args:
        mint: Token mint address.
        creator: Creator wallet address.
        created_ms: Creation timestamp in ms (None when unknown).
        start_mcap: Start market cap from the listing, if available.
        endpoint: Solana RPC HTTP endpoint for funding-edge confirmation.
        solscan_client: Optional Solscan client for edge nomination.
        use_solscan: Opt-in to the Solscan path; default answers the edge
            from RPC alone and never attempts Solscan.

    Returns:
        Row dict with funder/amount (or None), ath_multiple (or None),
        peak_sec, peak_mcap, created_ms, coverage flags and start mcap. Never raises.
    """
    funder: str | None = None
    amount: float | None = None
    try:
        edge = find_outbound_funding_edge(creator, rpc_url=endpoint or None)
        if edge is not None:
            funder, amount, _signature = edge[0], float(edge[1]), edge[2]
        else:
            edges, _warning = find_funding_edges(
                creator,
                "inbound",
                endpoint,
                solscan_client=solscan_client if use_solscan else None,
            )
            if edges:
                funder = edges[0].wallet
                amount = float(edges[0].amount_sol)
    except Exception as exc:  # noqa: BLE001 - fail-soft: coverage_miss
        logger.debug("funding edge failed for %s: %s", mint[:8], exc)

    ath, peak_sec, peak_mcap, has_candles = _resolve_candles(
        mint, created_ms, start_mcap
    )
    return {
        "mint": mint,
        "creator": creator,
        "funder": funder,
        "amount_sol": amount,
        "ath_multiple": ath,
        "peak_sec": peak_sec,
        "start_mcap": start_mcap,
        "peak_mcap": peak_mcap,
        "created_ms": created_ms,
        "has_funding": funder is not None and amount is not None,
        "has_candles": has_candles,
    }


def build_parser() -> argparse.ArgumentParser:
    """Build the argument parser for the offenders command."""
    parser = argparse.ArgumentParser(
        prog="rug_offenders",
        description="Repeat-offenders funnel: (funder, amount band) measurements.",
    )
    parser.add_argument("--limit", type=int, default=25)
    parser.add_argument("--band", type=float, default=0.5)
    parser.add_argument("--min-launches", type=int, default=3)
    parser.add_argument("--win-multiple", type=float, default=2.0)
    parser.add_argument(
        "--min-daily-rate",
        type=float,
        default=None,
        help="filter funders with estimated daily launch rate >= N (e.g. 1.0)",
    )
    parser.add_argument(
        "--max-cadence-hours",
        type=float,
        default=None,
        help="filter funders with median launch cadence <= N hours (e.g. 24.0)",
    )
    parser.add_argument(
        "--active-only",
        action="store_true",
        help="filter funders whose last launch was within the active window",
    )
    parser.add_argument(
        "--active-hours",
        type=float,
        default=24.0,
        help="active window in hours for --active-only (default: 24.0)",
    )
    parser.add_argument(
        "--full-address",
        action="store_true",
        help="display full funder address instead of truncated prefix",
    )
    parser.add_argument("--json", action="store_true")
    parser.add_argument(
        "--max-pages",
        type=int,
        default=None,
        help="listing page guard (default: ceil(limit/70)+2)",
    )
    parser.add_argument(
        "--use-solscan",
        action="store_true",
        help="opt in to Solscan edge nomination (default is RPC-only)",
    )
    return parser


def resolve_coins_to_rows(
    coins: Sequence[Mapping[str, Any]],
    endpoint: str,
    solscan_client: SolscanClient | None,
    *,
    use_solscan: bool = False,
) -> list[dict[str, Any]]:
    """Resolve raw listed coin dicts into measurement rows."""
    rows: list[dict[str, Any]] = []
    for coin in coins:
        if not isinstance(coin, Mapping):
            continue
        mint = coin.get("mint")
        creator = coin.get("creator")
        if not isinstance(mint, str) or not mint:
            continue
        if not isinstance(creator, str) or not creator:
            continue
        created = coin.get("created_timestamp")
        created_ms = int(created) if isinstance(created, (int, float)) else None
        mcap = coin.get("usd_market_cap", coin.get("market_cap"))
        start_mcap = float(mcap) if isinstance(mcap, (int, float)) else None
        rows.append(
            resolve_launch_row(
                mint=mint,
                creator=creator,
                created_ms=created_ms,
                start_mcap=start_mcap,
                endpoint=endpoint,
                solscan_client=solscan_client,
                use_solscan=use_solscan,
            )
        )
    return rows


def filter_bands(
    bands: Sequence[dict[str, Any]],
    *,
    min_daily_rate: float | None = None,
    max_cadence_hours: float | None = None,
    active_only: bool = False,
    active_hours: float = DEFAULT_ACTIVE_HOURS,
) -> list[dict[str, Any]]:
    """Filter aggregated bands by cadence and activity constraints."""
    filtered: list[dict[str, Any]] = []
    for b in bands:
        if min_daily_rate is not None:
            rate = b.get("daily_launch_rate")
            if rate is None or rate < min_daily_rate:
                continue
        if max_cadence_hours is not None:
            cad_h = b.get("cadence_hours")
            if cad_h is None or cad_h > max_cadence_hours:
                continue
        if active_only:
            ago = b.get("last_launch_hours_ago")
            if ago is None or ago > active_hours:
                continue
        filtered.append(b)
    return filtered


def render_report(
    coverage: Mapping[str, Any],
    bands: Sequence[Mapping[str, Any]],
    *,
    full_address: bool = False,
) -> None:
    """Print the human-readable repeat-offenders report."""
    print("=" * 78)
    print(" RUG OFFENDERS  (funder, amount-band measurements)")
    print("=" * 78)
    print(
        f"launches: {coverage['launches']} (listed {coverage['launches_listed']} "
        f"over {coverage['pages_fetched']} pages)  funding edges: "
        f"{coverage['with_funding_edge']}  candles: {coverage['with_candles']}"
    )
    for item in bands:
        funder_display = item["funder"] if full_address else f"{item['funder'][:8]}..."
        cadence_str = (
            f"cadence ~{item['cadence_hours']}h ({item['daily_launch_rate']}/day)"
            if item.get("cadence_hours") is not None
            else "cadence N/A"
        )
        last_str = (
            f"last {item['last_launch_hours_ago']}h ago"
            if item.get("last_launch_hours_ago") is not None
            else "last N/A"
        )
        active_str = "ACTIVE" if item.get("active") else "INACTIVE"
        winrate_str = (
            f"{item['winrate_pct']}%" if item.get("winrate_pct") is not None else "N/A"
        )
        ath_str = (
            f"avg ATH {item['mean_ath_multiple']:.2f}x (med {item['median_ath_multiple']:.2f}x, max {item['max_ath_multiple']:.2f}x)"
            if item.get("mean_ath_multiple") is not None
            else "ATH N/A"
        )
        peak_str = (
            f" | peak in ~{item['mean_peak_seconds']:.0f}s"
            if item.get("mean_peak_seconds") is not None
            else ""
        )
        print(
            f"  funder {funder_display} band {item['band_index']} "
            f"({item['amount_lo_sol']}-{item['amount_hi_sol']} SOL): "
            f"launches {item['launches']} scored {item['scored']} "
            f"wins {item['wins']} winrate {winrate_str} | "
            f"{ath_str}{peak_str} | "
            f"{cadence_str} | {last_str} | {active_str}"
        )
    print(
        "reference: >=3 launches and >=70% winrate are reference lines only, "
        "not pass/fail"
    )


def main(argv: Sequence[str] | None = None) -> int:
    """Run the repeat-offenders measurement report.

    Args:
        argv: Optional argument vector; defaults to ``sys.argv``.

    Returns:
        Process exit code (0 normally).
    """
    args = build_parser().parse_args(argv)
    limit = max(1, int(args.limit))
    band = float(args.band)
    if band <= 0:
        print("[rug_offenders] --band must be positive", file=sys.stderr)
        return 1
    win_multiple = float(args.win_multiple)

    resolve_dotenv()
    providers = load_provider_settings()
    endpoint = providers.rpc_http if providers else ""
    solscan_client = (
        SolscanClient(providers.solscan_api_key)
        if providers and providers.solscan_api_key and bool(args.use_solscan)
        else None
    )

    coins, pages_fetched = collect_recent_launches(
        limit,
        max_pages=int(args.max_pages) if args.max_pages is not None else None,
    )
    rows = resolve_coins_to_rows(
        coins, endpoint, solscan_client, use_solscan=bool(args.use_solscan)
    )
    bands = aggregate_bands(rows, win_multiple, band)

    filtered_bands = filter_bands(
        bands,
        min_daily_rate=float(args.min_daily_rate)
        if args.min_daily_rate is not None
        else None,
        max_cadence_hours=float(args.max_cadence_hours)
        if args.max_cadence_hours is not None
        else None,
        active_only=bool(args.active_only),
        active_hours=float(args.active_hours),
    )

    coverage = summarize_coverage(rows)
    coverage["pages_fetched"] = pages_fetched
    coverage["launches_listed"] = len(coins)
    payload = {
        "bands": filtered_bands,
        "coverage": coverage,
        "params": {
            "limit": limit,
            "band_sol": band,
            "win_multiple": win_multiple,
            "min_daily_rate": args.min_daily_rate,
            "max_cadence_hours": args.max_cadence_hours,
            "active_only": bool(args.active_only),
            "active_hours": float(args.active_hours),
        },
        "reference": {
            "min_launches": int(args.min_launches),
            "min_winrate_pct": REFERENCE_MIN_WINRATE_PCT,
            "reference_min_launches": REFERENCE_MIN_LAUNCHES,
            "note": "thresholds are reference lines only, not pass/fail",
        },
    }
    if bool(args.json):
        print(json.dumps(payload, sort_keys=True))
        return 0
    render_report(coverage, filtered_bands, full_address=bool(args.full_address))
    return 0
