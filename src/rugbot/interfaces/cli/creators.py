"""Settled-launch creator history scan (measurement report, no verdicts).

Groups settled launches by creator and reports per-creator launch counts,
winrate (ATH multiple >= ``--win-multiple``) and ATH distribution. A launch
is settled when it is older than ``--min-age-min``; younger launches are
excluded and counted, never scored. Reference thresholds appear as reference
values only, never as pass/fail verdicts.
"""

# ruff: noqa: PLR0913

from __future__ import annotations

import argparse
import json
import statistics
import time
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from rugbot.backtest.runners.entry_resolver import trajectory_from_1s_candles
from rugbot.integrations.pumpfun_api import get_client
from rugbot.interfaces.cli.offenders import (
    LISTING_PAGE_SIZE,
    collect_recent_launches,
    default_max_pages,
)
from rugbot.interfaces.cli.triage import cadence_seconds
from rugbot.utils.logger import get_logger

logger = get_logger(__name__)

DEFAULT_SETTLED_TARGET = 300

AthResolver = Callable[[str, int | None], float | None]


def collect_until_age(
    limit: int,
    min_age_s: float,
    now_s: float,
    *,
    settled_target: int = DEFAULT_SETTLED_TARGET,
    max_pages: int | None = None,
    lister: Callable[[int, int], list[dict]] | None = None,
) -> tuple[list[dict], int]:
    """Page the listing back until enough settled launches are collected.

    Grows the collection window one page at a time (reusing the cached
    :func:`collect_recent_launches`) and keeps paging past the first
    settled token until at least ``settled_target`` collected launches are
    at least ``min_age_s`` old. Stops on the settled target, the listing
    budget (``limit``), the page guard, a no-progress page, or an
    exhausted endpoint.

    Args:
        limit: Listing budget: maximum unique launches to collect.
        min_age_s: Settled age threshold in seconds.
        now_s: Current time as Unix seconds.
        settled_target: Desired number of settled launches.
        max_pages: Page guard; defaults to :func:`default_max_pages`.
        lister: ``(limit, offset) -> coins`` callable for tests.

    Returns:
        Tuple of (unique coin dicts, truncated to ``limit``; pages fetched
        by the final collection call).
    """
    cap = default_max_pages(limit) if max_pages is None else max(0, int(max_pages))
    coins: list[dict] = []
    pages = 0
    target = min(max(1, int(limit)), LISTING_PAGE_SIZE)
    prev_count = -1
    while True:
        coins, pages = collect_recent_launches(target, max_pages=cap, lister=lister)
        if not coins:
            break
        settled, _excluded = partition_settled(coins, now_s, min_age_s)
        if (
            len(settled) >= settled_target
            or pages >= cap
            or target >= limit
            or len(coins) <= prev_count
        ):
            break
        prev_count = len(coins)
        target = min(limit, target + LISTING_PAGE_SIZE)
    return coins[:limit], pages


def partition_settled(
    coins: Sequence[Mapping[str, Any]], now_s: float, min_age_s: float
) -> tuple[list[Mapping[str, Any]], int]:
    """Split listed coins into settled launches and excluded young ones.

    Args:
        coins: Listed coin dicts with ``created_timestamp`` (ms).
        now_s: Current time as Unix seconds.
        min_age_s: Settled age threshold in seconds.

    Returns:
        Tuple of (settled coins, unsettled/excluded count). Coins without
        a usable timestamp cannot be proven settled and are excluded.
    """
    settled: list[Mapping[str, Any]] = []
    excluded = 0
    for coin in coins:
        created = coin.get("created_timestamp") if isinstance(coin, Mapping) else None
        if (
            isinstance(created, (int, float))
            and now_s - float(created) / 1000.0 >= min_age_s
        ):
            settled.append(coin)
        else:
            excluded += 1
    return settled, excluded


def live_ath_resolver(mint: str, created_ms: int | None) -> float | None:
    """Resolve one mint to its post-bundle ATH multiple via 1s candles.

    Uses the same entry rule as the offenders funnel: entry is the first
    1s candle after the creation candle. Never raises; returns None when
    no entry is reconstructable (counted as ``no_candles`` downstream).

    Args:
        mint: Token mint address.
        created_ms: Creation timestamp in ms (None when unknown).

    Returns:
        ATH multiple from the achievable entry, or None.
    """
    try:
        candles = get_client().fetch_candlesticks(
            mint, interval="1s", limit=300, created_ts=0
        )
    except Exception as exc:  # noqa: BLE001 - fail-soft: no_candles
        logger.debug("candles failed for %s: %s", mint[:8], exc)
        return None
    if not candles:
        return None
    try:
        _traj, ath = trajectory_from_1s_candles(candles, created_ms=created_ms)
    except Exception as exc:  # noqa: BLE001 - fail-soft: no_candles
        logger.debug("trajectory failed for %s: %s", mint[:8], exc)
        return None
    return float(ath) if ath is not None else None


def score_launches(
    settled: Sequence[Mapping[str, Any]],
    ath_resolver: AthResolver,
) -> list[dict[str, Any]]:
    """Resolve settled coins to per-launch measurement rows.

    Args:
        settled: Settled coin dicts from the listing.
        ath_resolver: ``(mint, created_ms) -> ATH multiple or None``.

    Returns:
        Row dicts with creator, mint, ATH multiple (or None), coverage
        flags, start mcap and creation time. Coins without a mint or
        creator are skipped, never fabricated. Never raises per coin.
    """
    rows: list[dict[str, Any]] = []
    for coin in settled:
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
        try:
            ath = ath_resolver(mint, created_ms)
        except Exception as exc:  # noqa: BLE001 - fail-soft: no_candles
            logger.debug("ATH resolve failed for %s: %s", mint[:8], exc)
            ath = None
        rows.append(
            {
                "mint": mint,
                "creator": creator,
                "ath_multiple": float(ath) if ath is not None else None,
                "has_candles": ath is not None,
                "start_mcap": start_mcap,
                "created_ms": created_ms,
            }
        )
    return rows


def group_by_creator(
    rows: Sequence[Mapping[str, Any]], win_multiple: float
) -> list[dict[str, Any]]:
    """Aggregate scored launch rows by creator wallet (verbatim).

    Launches without candles are excluded from the winrate denominator
    and counted as ``no_candles``, never zeroed.

    Args:
        rows: Per-launch rows with creator, ath_multiple, has_candles,
            start_mcap and created_ms keys.
        win_multiple: ATH multiple counting as a win.

    Returns:
        Creator dicts sorted by launches desc, then max ATH (unscored
        creators sort last). No verdict fields.
    """
    grouped: dict[str, list[Mapping[str, Any]]] = {}
    for row in rows:
        creator = row.get("creator")
        if not isinstance(creator, str) or not creator:
            continue
        grouped.setdefault(creator, []).append(row)
    creators: list[dict[str, Any]] = []
    for creator, members in grouped.items():
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
        stamps_ms = [
            int(m["created_ms"])
            for m in members
            if isinstance(m.get("created_ms"), (int, float))
        ]
        stamps_s = [s / 1000.0 for s in stamps_ms]
        creators.append(
            {
                "creator": creator,
                "launches": len(members),
                "scored": scored,
                "wins": wins,
                "winrate_pct": round(wins / scored * 100, 2) if scored else None,
                "median_ath_multiple": float(statistics.median(ath_values))
                if ath_values
                else None,
                "max_ath_multiple": float(max(ath_values)) if ath_values else None,
                "median_start_mcap": float(statistics.median(mcaps)) if mcaps else None,
                "first_launch_s": min(stamps_s) if stamps_s else None,
                "last_launch_s": max(stamps_s) if stamps_s else None,
                "cadence_seconds": cadence_seconds(stamps_s),
                "no_candles": len(members) - scored,
            }
        )
    creators.sort(
        key=lambda c: (
            -int(c["launches"]),
            c["max_ath_multiple"] is None,
            -(
                float(c["max_ath_multiple"])
                if c["max_ath_multiple"] is not None
                else 0.0
            ),
        )
    )
    return creators


def assemble_payload(
    *,
    creators: Sequence[Mapping[str, Any]],
    coverage: Mapping[str, Any],
    limit: int,
    min_age_min: float,
    win_multiple: float,
    min_launches: int,
) -> dict[str, Any]:
    """Assemble the machine-readable measurement payload (no verdicts).

    Args:
        creators: Per-creator measurement dicts.
        coverage: Coverage counts for the run.
        limit: Requested launch limit.
        min_age_min: Settled age threshold in minutes.
        win_multiple: ATH multiple counting as a win.
        min_launches: Display threshold, reported as reference only.

    Returns:
        JSON-safe payload with creators, coverage, params and reference.
    """
    return {
        "creators": [dict(c) for c in creators],
        "coverage": dict(coverage),
        "params": {
            "limit": limit,
            "min_age_min": min_age_min,
            "win_multiple": win_multiple,
        },
        "reference": {
            "min_launches": min_launches,
            "win_multiple": win_multiple,
            "note": "thresholds are reference lines only, not pass/fail",
        },
    }


def build_parser() -> argparse.ArgumentParser:
    """Build the argument parser for the creators command."""
    parser = argparse.ArgumentParser(
        prog="rug_creators",
        description="Settled-launch creator history measurements.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=2500,
        help="listing budget: max rows to page (not the primary stop)",
    )
    parser.add_argument("--min-age-min", type=float, default=60.0)
    parser.add_argument("--min-launches", type=int, default=2)
    parser.add_argument("--win-multiple", type=float, default=2.0)
    parser.add_argument(
        "--settled-target",
        type=int,
        default=DEFAULT_SETTLED_TARGET,
        help="stop paging once this many settled launches collect",
    )
    parser.add_argument("--json", action="store_true")
    parser.add_argument(
        "--max-pages",
        type=int,
        default=None,
        help="listing page guard (default: ceil(limit/70)+2)",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run the settled-launch creator history report.

    Args:
        argv: Optional argument vector; defaults to ``sys.argv``.

    Returns:
        Process exit code (0 normally).
    """
    args = build_parser().parse_args(argv)
    limit = max(1, int(args.limit))
    min_age_s = max(0.0, float(args.min_age_min)) * 60.0
    win_multiple = float(args.win_multiple)
    min_launches = max(1, int(args.min_launches))
    settled_target = max(1, int(args.settled_target))
    now_s = time.time()

    coins, pages = collect_until_age(
        limit,
        min_age_s,
        now_s,
        settled_target=settled_target,
        max_pages=int(args.max_pages) if args.max_pages is not None else None,
    )
    settled, unsettled = partition_settled(coins, now_s, min_age_s)
    rows = score_launches(settled, live_ath_resolver)
    creators = group_by_creator(rows, win_multiple)
    shown = [c for c in creators if int(c["launches"]) >= min_launches]
    no_candles = sum(1 for r in rows if not bool(r.get("has_candles")))
    coverage = {
        "launches_listed": len(coins),
        "pages_fetched": pages,
        "settled": len(settled),
        "settled_target": settled_target,
        "unsettled_excluded": unsettled,
        "no_candles": no_candles,
        "with_candles": len(rows) - no_candles,
    }
    payload = assemble_payload(
        creators=shown,
        coverage=coverage,
        limit=limit,
        min_age_min=float(args.min_age_min),
        win_multiple=win_multiple,
        min_launches=min_launches,
    )
    if bool(args.json):
        print(json.dumps(payload, sort_keys=True))
        return 0
    print("=" * 78)
    print(" RUG CREATORS  (settled-launch history per creator)")
    print("=" * 78)
    print(
        f"listed: {coverage['launches_listed']} over {coverage['pages_fetched']} "
        f"pages  settled: {coverage['settled']}  "
        f"unsettled excluded: {coverage['unsettled_excluded']}  "
        f"candles: {coverage['with_candles']}"
    )
    for item in shown:
        print(
            f"  creator {item['creator'][:8]}… launches {item['launches']} "
            f"scored {item['scored']} wins {item['wins']} "
            f"winrate {item['winrate_pct']} median ATH "
            f"{item['median_ath_multiple']} max ATH {item['max_ath_multiple']} "
            f"cadence {item['cadence_seconds']} no_candles {item['no_candles']}"
        )
    print("reference: min-launches and win multiple are reference lines only")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
