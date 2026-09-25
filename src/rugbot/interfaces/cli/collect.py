"""Background collector persisting per-launch features (network lives here)."""

# ruff: noqa: C901, PLR0912, PLR0915, TRY300 - CLI dispatch stays flat;
# per-launch failures are fail-soft by contract.

from __future__ import annotations

import argparse
import json
import time
from collections.abc import Mapping
from typing import Any

from rugbot.analysis.store import DEFAULT_STORE_PATH, AnalysisStore
from rugbot.backtest.runners.entry_resolver import trajectory_from_1s_candles
from rugbot.integrations.pumpfun_api import get_client
from rugbot.interfaces.cli.features import (
    _fetch_launch_candles,
    _fetch_launch_token,
    build_feature_record,
    entry_close_from_candles,
    entry_mcap_from_token,
    labels_from_trajectory,
    resolve_launch_features,
)
from rugbot.interfaces.cli.offenders import LISTING_PAGE_SIZE
from rugbot.runtime.config import load_provider_settings, resolve_dotenv
from rugbot.utils.logger import get_logger

logger = get_logger(__name__)

DEFAULT_INTERVAL_S = 60
DEFAULT_LISTING_PAGES = 8
DEFAULT_RELABEL_RECENT = 50
SETTLED_TARGET_S = 300


def build_parser() -> argparse.ArgumentParser:
    """Build the argument parser for the collector command."""
    parser = argparse.ArgumentParser(
        prog="rug_collect",
        description="Background collector for per-launch features.",
    )
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--loop", action="store_true")
    parser.add_argument("--interval", type=int, default=DEFAULT_INTERVAL_S)
    parser.add_argument("--store", type=str, default=DEFAULT_STORE_PATH)
    parser.add_argument("--listing-pages", type=int, default=DEFAULT_LISTING_PAGES)
    parser.add_argument("--relabel-recent", type=int, default=DEFAULT_RELABEL_RECENT)
    parser.add_argument("--json", action="store_true")
    return parser


def _sol_price() -> float | None:
    """Fetch USD per SOL, fail-soft."""
    try:
        quote = get_client().fetch_sol_price()
        raw = quote.get("solPrice") if isinstance(quote, dict) else None
        if isinstance(raw, (int, float)) and raw > 0:
            return float(raw)
    except Exception as exc:  # noqa: BLE001 - fail-soft per cycle
        logger.debug("sol price failed: %s", exc)
    return None


def _relabel_row(
    row: Mapping[str, Any], sol_price: float | None, now_ms: int
) -> dict[str, Any] | None:
    """Recompute outcome labels for one pending row, fail-soft.

    Args:
        row: Stored feature record with ``label_unavailable_reason`` set.
        sol_price: USD per SOL, or None when unavailable.
        now_ms: Current time in ms.

    Returns:
        Updated record dict, or None when the relabel failed.
    """
    mint = row.get("mint")
    if not isinstance(mint, str) or not mint:
        return None
    try:
        created_ms = row.get("created_at_ms")
        if not isinstance(created_ms, (int, float)):
            created_ms = None
        else:
            created_ms = int(created_ms)
        candles = _fetch_launch_candles(mint)
        token = _fetch_launch_token(mint)
        candle_list = list(candles) if candles else []
        has_candles = bool(candle_list)
        entry_price = entry_close_from_candles(candle_list, created_ms)
        entry_mcap, mcap_reason = entry_mcap_from_token(entry_price, token, sol_price)
        settled: bool | None = None
        if created_ms is not None:
            settled = (int(now_ms) - int(created_ms)) / 1000.0 >= float(
                SETTLED_TARGET_S
            )
        labels: dict[str, Any] = {
            "max_multiple_after_entry": None,
            "ath_multiple": None,
            "reached_2x": None,
            "reached_3x": None,
            "adverse_multiple": None,
        }
        label_reason: str | None = None
        if not has_candles:
            label_reason = "no candles"
        elif settled is False:
            label_reason = f"unsettled (<{int(SETTLED_TARGET_S)}s)"
        else:
            try:
                points, ath = trajectory_from_1s_candles(
                    candle_list, created_ms=created_ms
                )
            except Exception:  # noqa: BLE001 - fail-soft per launch
                points, ath = (), None
            if not points or ath is None:
                label_reason = "entry unresolvable"
            else:
                labels = labels_from_trajectory(points, float(ath))
        updated = dict(row)
        updated.update(
            {
                "entry_price": entry_price,
                "entry_mcap_sol": entry_mcap,
                "entry_mcap_unavailable_reason": mcap_reason,
                "candles_available": has_candles,
                "n_candles": len(candle_list),
                **labels,
                "label_unavailable_reason": label_reason,
            }
        )
        return updated
    except Exception as exc:  # noqa: BLE001 - fail-soft per launch
        logger.debug("relabel failed for %s: %s", str(mint)[:8], exc)
        return None


def collect_once(
    store: AnalysisStore,
    *,
    listing_pages: int,
    relabel_recent: int,
    as_json: bool = False,
) -> dict[str, int]:
    """Run one collector cycle: ingest new launches, relabel recent rows.

    Args:
        store: Destination analysis store.
        listing_pages: Maximum listing pages to scan per cycle.
        relabel_recent: Maximum pending rows to relabel per cycle.
        as_json: Unused; kept for CLI symmetry.

    Returns:
        Dict with new/updated/total counts.
    """
    _ = as_json
    now_ms = int(time.time() * 1000)
    resolve_dotenv()
    providers = load_provider_settings()
    endpoint = providers.rpc_http if providers else ""
    sol_price = _sol_price()

    stored = store.get_launches(limit=None, order="created_at_ms DESC")
    known = {r.get("mint") for r in stored if isinstance(r.get("mint"), str)}
    new_rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    pages = max(0, int(listing_pages))
    try:
        lister = get_client().fetch_recent_launches
    except Exception as exc:  # noqa: BLE001 - fail-soft per cycle
        logger.debug("listing client failed: %s", exc)
        lister = None
    stop = False
    if lister is not None:
        for page in range(pages):
            try:
                batch = lister(limit=LISTING_PAGE_SIZE, offset=page * LISTING_PAGE_SIZE)
            except Exception as exc:  # noqa: BLE001 - fail-soft per cycle
                logger.debug("listing page %d failed: %s", page, exc)
                break
            if not batch:
                break
            for coin in batch:
                if not isinstance(coin, Mapping):
                    continue
                mint = coin.get("mint")
                if not isinstance(mint, str) or not mint or mint in seen:
                    continue
                seen.add(mint)
                if mint in known:
                    stop = True
                    continue
                try:
                    record = resolve_launch_features(
                        coin,
                        endpoint=endpoint,
                        sol_price=sol_price,
                        settled_target_s=SETTLED_TARGET_S,
                        now_ms=now_ms,
                    )
                except Exception as exc:  # noqa: BLE001 - fail-soft per launch
                    logger.debug("feature row failed: %s", exc)
                    continue
                if isinstance(record.get("mint"), str):
                    new_rows.append(record)
                    known.add(mint)
                    try:
                        store.upsert_launches([record])
                    except Exception as exc:  # noqa: BLE001 - fail-soft
                        logger.debug("upsert failed: %s", exc)
            if stop:
                break
            time.sleep(0.02)
    new_count = len(new_rows)
    if new_rows:
        try:
            store.upsert_launches(new_rows)
        except Exception as exc:  # noqa: BLE001 - fail-soft per cycle
            logger.debug("final upsert failed: %s", exc)

    updated_count = 0
    try:
        recent = store.get_launches(limit=None, order="created_at_ms DESC")
        pending = [r for r in recent if r.get("label_unavailable_reason") is not None][
            : max(0, int(relabel_recent))
        ]
        relabeled: list[dict[str, Any]] = []
        for row in pending:
            updated = _relabel_row(row, sol_price, now_ms)
            if updated is not None:
                relabeled.append(updated)
        if relabeled:
            store.upsert_launches(relabeled)
            updated_count = len(relabeled)
    except Exception as exc:  # noqa: BLE001 - fail-soft per cycle
        logger.debug("relabel cycle failed: %s", exc)

    total = store.count()
    _ = build_feature_record  # feature-logic reuse contract lives here
    return {"new": new_count, "updated": updated_count, "total": total}


def main(argv: list[str] | None = None) -> int:
    """Run the background collector.

    Args:
        argv: Optional argument vector; defaults to ``sys.argv``.

    Returns:
        Process exit code (0 normally).
    """
    args = build_parser().parse_args(argv)
    store = AnalysisStore(str(args.store))
    try:
        loop = bool(args.loop)
        if loop:
            interval = max(1, int(args.interval))
            while True:
                try:
                    result = collect_once(
                        store,
                        listing_pages=int(args.listing_pages),
                        relabel_recent=int(args.relabel_recent),
                    )
                except Exception as exc:  # noqa: BLE001 - never crash loop
                    logger.debug("collector cycle failed: %s", exc)
                    result = {"new": 0, "updated": 0, "total": store.count()}
                line = (
                    f"new={result['new']} "
                    f"updated={result['updated']} total={result['total']}"
                )
                if bool(args.json):
                    print(json.dumps(result, sort_keys=True))
                else:
                    print(line)
                time.sleep(interval)
        result = collect_once(
            store,
            listing_pages=int(args.listing_pages),
            relabel_recent=int(args.relabel_recent),
        )
        if bool(args.json):
            print(json.dumps(result, sort_keys=True))
        else:
            print(
                f"new={result['new']} "
                f"updated={result['updated']} total={result['total']}"
            )
        return 0
    finally:
        store.close()


if __name__ == "__main__":
    raise SystemExit(main())
