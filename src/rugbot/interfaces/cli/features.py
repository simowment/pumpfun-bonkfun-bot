"""Per-launch feature table over recent pump.fun launches (measurements only).

One row per launch with identity, deployer, funding, entry/price,
outcome labels from the post-bundle trajectory, and cheap narrative
proxies from the listing row. No models, no verdicts.
"""

# ruff: noqa: PLR0913, C901, PLR0912, PLR0915

from __future__ import annotations

import argparse
import csv
import json
import time
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from rugbot.analysis.store import AnalysisStore
from rugbot.backtest.runners.entry_resolver import trajectory_from_1s_candles
from rugbot.integrations.pumpfun_api import get_client
from rugbot.interfaces.cli.offenders import (
    LISTING_PAGE_SIZE,
    collect_recent_launches,
    default_max_pages,
)
from rugbot.runtime.config import load_provider_settings, resolve_dotenv
from rugbot.tracker.funding_edge_rpc import find_outbound_funding_edge
from rugbot.utils.logger import get_logger

logger = get_logger(__name__)

ENTRY_LATENCY_MS = 1000
ASCII_MAX_ORD = 127
WIN_2X_MULTIPLE = 2.0
WIN_3X_MULTIPLE = 3.0
MIN_CANDLES_WITHOUT_CREATED_MS = 2
PREVIEW_ROWS = 10


def funding_band_for_amount(amount_sol: float | None) -> float | None:
    """Round a funding amount to the nearest 0.5 SOL band.

    Args:
        amount_sol: Funding edge amount in SOL, or None when unknown.

    Returns:
        Amount rounded to the nearest 0.5, or None when unknown.
    """
    if amount_sol is None:
        return None
    try:
        value = float(amount_sol)
    except (TypeError, ValueError):
        return None
    return round(value * 2.0) / 2.0


def narrative_proxies(symbol: object, name: object) -> dict[str, Any]:
    """Derive cheap narrative proxies from the listing symbol/name.

    Args:
        symbol: Token symbol (any type; non-strings become empty).
        name: Token name (any type; non-strings become empty).

    Returns:
        Dict with symbol_len, name_len, symbol_has_digit,
        symbol_all_caps, symbol_has_emoji, name_has_emoji.
    """
    sym = symbol if isinstance(symbol, str) else ""
    nm = name if isinstance(name, str) else ""
    has_alpha = any(c.isalpha() for c in sym)
    return {
        "symbol_len": len(sym),
        "name_len": len(nm),
        "symbol_has_digit": any(c.isdigit() for c in sym),
        "symbol_all_caps": bool(has_alpha) and sym.upper() == sym,
        "symbol_has_emoji": any(ord(c) > ASCII_MAX_ORD for c in sym),
        "name_has_emoji": any(ord(c) > ASCII_MAX_ORD for c in nm),
    }


def _nonempty_str(value: object) -> bool | None:
    """Return True/False for present string fields, None when missing.

    Args:
        value: Raw field value; non-string values yield None, except
            None which also yields None (never fabricated).

    Returns:
        True when a non-blank string, False when blank string, else None.
    """
    if value is None:
        return None
    if not isinstance(value, str):
        return None
    return bool(value.strip())


def _opt_bool(value: object) -> bool | None:
    """Narrow an optional boolean field without fabrication.

    Args:
        value: Raw field value.

    Returns:
        The bool, or None when absent or not a bool.
    """
    return value if isinstance(value, bool) else None


def _opt_float(value: object) -> float | None:
    """Narrow an optional numeric field without fabrication.

    Args:
        value: Raw field value.

    Returns:
        The float, or None when absent or non-numeric.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        try:
            return float(value)
        except (TypeError, ValueError, OverflowError):
            return None
    return None


def _opt_int(value: object) -> int | None:
    """Narrow an optional int field without fabrication.

    Args:
        value: Raw field value.

    Returns:
        The int, or None when absent or non-numeric.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        try:
            return int(value)
        except (TypeError, ValueError, OverflowError):
            return None
    return None


def attention_features(listing: Mapping[str, Any] | None) -> dict[str, Any]:
    """Derive attention/social features from the listing row (no calls).

    Unit contract (verified live 2026-09-12 on 3 mints): ``market_cap``
    is SOL (quote), ``usd_market_cap``/``market_cap_usd`` are USD with
    ``usd/market_cap`` ~= live SOL price (~101.53), and
    ``ath_market_cap`` is USD (max 1s-candle high x 1e9 equals it
    exactly). Hence ``ath_multiple_from_api = ath/usd`` is
    unit-consistent (USD/USD) but post-hoc (ATH is observed after the
    listing snapshot), so it MUST be treated as an artifact, not a
    predictive feature.

    Args:
        listing: Listing coin dict, or None when unavailable.

    Returns:
        Dict with attention booleans, snapshot numerics and ATH fields;
        nulls stay null, nothing fabricated, no verdict fields.
    """
    if not isinstance(listing, Mapping):
        return {
            "has_twitter": None,
            "twitter_is_status_link": None,
            "has_website": None,
            "has_description": None,
            "description_len": None,
            "has_image": None,
            "has_profile_image": None,
            "reply_count": None,
            "verified": None,
            "nsfw": None,
            "boost_mode": None,
            "is_currently_live": None,
            "has_username": None,
            "market_cap": None,
            "usd_market_cap": None,
            "real_sol_reserves": None,
            "virtual_sol_reserves": None,
            "complete": None,
            "ath_market_cap": None,
            "ath_market_cap_timestamp": None,
            "ath_multiple_from_api": None,
        }
    twitter = listing.get("twitter")
    has_twitter = _nonempty_str(twitter)
    twitter_is_status: bool | None = None
    if isinstance(twitter, str) and twitter.strip():
        twitter_is_status = "/status/" in twitter
    website = listing.get("website")
    description = listing.get("description")
    image_uri = listing.get("image_uri")
    profile_image = listing.get("profile_image")
    username = listing.get("username")
    description_len: int | None = None
    if isinstance(description, str):
        description_len = len(description)
    ath = _opt_float(listing.get("ath_market_cap"))
    usd = _opt_float(
        listing.get("usd_market_cap")
        if listing.get("usd_market_cap") is not None
        else listing.get("market_cap_usd")
    )
    ath_multiple: float | None = None
    if ath is not None and usd is not None and usd > 0 and ath > 0:
        ath_multiple = float(ath) / float(usd)
    return {
        "has_twitter": has_twitter,
        "twitter_is_status_link": twitter_is_status,
        "has_website": _nonempty_str(website),
        "has_description": _nonempty_str(description),
        "description_len": description_len,
        "has_image": _nonempty_str(image_uri),
        "has_profile_image": _nonempty_str(profile_image),
        "reply_count": _opt_int(listing.get("reply_count")),
        "verified": _opt_bool(listing.get("verified")),
        "nsfw": _opt_bool(listing.get("nsfw")),
        "boost_mode": listing.get("boost_mode")
        if isinstance(listing.get("boost_mode"), str)
        else None,
        "is_currently_live": _opt_bool(listing.get("is_currently_live")),
        "has_username": _nonempty_str(username),
        "market_cap": _opt_float(listing.get("market_cap")),
        "usd_market_cap": usd,
        "real_sol_reserves": _opt_int(listing.get("real_sol_reserves")),
        "virtual_sol_reserves": _opt_int(listing.get("virtual_sol_reserves")),
        "complete": _opt_bool(listing.get("complete")),
        "ath_market_cap": ath,
        "ath_market_cap_timestamp": _opt_int(listing.get("ath_market_cap_timestamp")),
        "ath_multiple_from_api": ath_multiple,
    }


def labels_from_trajectory(
    points: Sequence[tuple[float, float]],
    ath_multiple: float | None,
) -> dict[str, Any]:
    """Derive outcome labels from a post-bundle trajectory.

    Args:
        points: Pessimistic (sec, multiple) points after entry.
        ath_multiple: Resolved ATH multiple, or None when unavailable.

    Returns:
        Dict with max_multiple_after_entry, ath_multiple, reached_2x,
        reached_3x, adverse_multiple; all None/false-free nulls when
        the trajectory is empty or ATH is unknown.
    """
    if not points or ath_multiple is None:
        return {
            "max_multiple_after_entry": None,
            "ath_multiple": None,
            "reached_2x": None,
            "reached_3x": None,
            "adverse_multiple": None,
        }
    try:
        ath = float(ath_multiple)
        mults = [float(m) for _, m in points]
    except (TypeError, ValueError):
        return {
            "max_multiple_after_entry": None,
            "ath_multiple": None,
            "reached_2x": None,
            "reached_3x": None,
            "adverse_multiple": None,
        }
    peak = max(mults) if mults else ath
    floor = min(mults) if mults else None
    return {
        "max_multiple_after_entry": float(peak),
        "ath_multiple": float(ath),
        "reached_2x": bool(ath >= WIN_2X_MULTIPLE),
        "reached_3x": bool(ath >= WIN_3X_MULTIPLE),
        "adverse_multiple": float(floor) if floor is not None else None,
    }


def _candle_close(candle: Mapping[str, Any]) -> float | None:
    """Return a candle's positive close price, or None.

    Args:
        candle: Single swap-api candle mapping.

    Returns:
        Positive close price, or None when missing or non-positive.
    """
    close = candle.get("close")
    if isinstance(close, bool):
        return None
    try:
        price = float(close)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return price if price > 0 else None


def _candle_after_threshold(candle: Mapping[str, Any], threshold: int) -> bool:
    """Return True when a candle timestamp meets the entry threshold.

    Args:
        candle: Single swap-api candle mapping.
        threshold: Minimum timestamp in ms for the entry candle.

    Returns:
        True when the candle timestamp parses and meets the threshold.
    """
    try:
        return int(candle.get("timestamp", 0)) >= threshold
    except (TypeError, ValueError):
        return False


def entry_close_from_candles(
    candles: Sequence[Mapping[str, Any]],
    created_ms: int | None,
) -> float | None:
    """Return the first achievable post-bundle close price.

    Args:
        candles: Swap-api 1s candle dicts in time order.
        created_ms: Mint creation timestamp in ms, or None for index 1.

    Returns:
        Entry close price, or None when no candle qualifies.
    """
    if not candles:
        return None
    if created_ms is None:
        ordered = list(candles)[1:]
        threshold: int | None = None
    else:
        try:
            threshold = int(created_ms) + ENTRY_LATENCY_MS
        except (TypeError, ValueError):
            return None
        ordered = list(candles)
    if threshold is None and len(ordered) < MIN_CANDLES_WITHOUT_CREATED_MS - 1:
        return None
    for candle in ordered:
        if not isinstance(candle, Mapping):
            continue
        if threshold is not None and not _candle_after_threshold(candle, threshold):
            continue
        price = _candle_close(candle)
        if price is not None:
            return price
    return None


def entry_mcap_from_token(
    entry_price: float | None,
    token: Mapping[str, Any] | None,
    sol_price: float | None,
) -> tuple[float | None, str | None]:
    """Derive entry mcap in SOL from price, supply and SOL quote.

    Args:
        entry_price: Post-bundle entry price in USD per token.
        token: ``fetch_token`` payload with total_supply/base_decimals.
        sol_price: USD per SOL, or None when the quote failed.

    Returns:
        (entry_mcap_sol, unavailable_reason) tuple; reason is None
        when the value is derivable.
    """
    reason: str | None = None
    mcap: float | None = None
    supply_tokens = 0.0
    if entry_price is None or not entry_price > 0:
        reason = "entry price unavailable"
    elif not isinstance(token, Mapping):
        reason = "fetch_token unavailable"
    else:
        supply_raw = token.get("total_supply", 0)
        decimals = token.get("base_decimals", 6)
        if isinstance(supply_raw, bool) or not isinstance(supply_raw, (int, float)):
            reason = "total_supply missing"
        elif isinstance(decimals, bool) or not isinstance(decimals, (int, float)):
            reason = "base_decimals missing"
        else:
            try:
                supply_tokens = float(supply_raw) / (10 ** int(decimals))
            except (TypeError, ValueError, OverflowError):
                reason = "supply scaling failed"
            if reason is None and not supply_tokens > 0:
                reason = "total_supply non-positive"
            elif reason is None and (sol_price is None or not sol_price > 0):
                reason = "sol price unavailable"
            elif reason is None:
                mcap = float(entry_price) * supply_tokens / float(sol_price)
    return mcap, reason


def build_feature_record(
    *,
    mint: str,
    creator: str,
    created_ms: int | None,
    symbol: object,
    name: object,
    deployer_count: int | None,
    funding_edge: tuple[str, float] | None,
    candles: Sequence[Mapping[str, Any]],
    token: Mapping[str, Any] | None,
    sol_price: float | None,
    now_ms: int | None = None,
    settled_target_s: int = 300,
    listing: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build one measurement-only feature record (pure, no network).

    Args:
        mint: Token mint address.
        creator: Creator wallet address.
        created_ms: Creation timestamp in ms, or None when unknown.
        symbol: Listing symbol for narrative proxies.
        name: Listing name for narrative proxies.
        deployer_count: Creator-index lifetime count, or None.
        funding_edge: (funder, amount_sol) or None when unresolved.
        candles: 1s candle dicts for entry/outcome derivation.
        token: fetch_token payload for entry-mcap derivation.
        sol_price: USD per SOL, or None when unavailable.
        now_ms: Current time in ms for the settled check.
        settled_target_s: Minimum age in seconds for settled labels.
        listing: Listing coin dict for attention/snapshot fields.

    Returns:
        Feature record dict with nulls (never fabricated) for anything
        not obtainable. No verdict fields.
    """
    attention = attention_features(listing)
    hour_utc: int | None = None
    if created_ms is not None:
        try:
            hour_utc = datetime.fromtimestamp(int(created_ms) / 1000, tz=UTC).hour
        except (TypeError, ValueError, OverflowError, OSError):
            hour_utc = None

    funder: str | None = None
    amount: float | None = None
    if funding_edge is not None:
        try:
            funder, raw_amount = funding_edge
            funder = str(funder) if funder else None
            amount = float(raw_amount)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            funder, amount = None, None

    candle_list = list(candles) if candles else []
    has_candles = bool(candle_list)
    entry_price = entry_close_from_candles(candle_list, created_ms)
    entry_mcap, mcap_reason = entry_mcap_from_token(entry_price, token, sol_price)

    settled: bool | None = None
    if created_ms is not None and now_ms is not None:
        try:
            settled = (int(now_ms) - int(created_ms)) / 1000.0 >= float(
                settled_target_s
            )
        except (TypeError, ValueError):
            settled = None

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
        label_reason = f"unsettled (<{int(settled_target_s)}s)"
    else:
        try:
            points, ath = trajectory_from_1s_candles(candle_list, created_ms=created_ms)
        except Exception:  # noqa: BLE001 - pure helper stays fail-soft
            points, ath = (), None
        if not points or ath is None:
            label_reason = "entry unresolvable"
        else:
            labels = labels_from_trajectory(points, float(ath))

    record: dict[str, Any] = {
        "mint": mint,
        "creator": creator,
        "created_at_ms": created_ms,
        "hour_utc": hour_utc,
        "deployer_lifetime_creations": deployer_count,
        "deployer_is_fresh": (deployer_count == 1)
        if isinstance(deployer_count, int)
        else None,
        "funder": funder,
        "funding_amount_sol": amount,
        "funding_band": funding_band_for_amount(amount),
        "funding_available": funder is not None and amount is not None,
        "entry_price": entry_price,
        "entry_mcap_sol": entry_mcap,
        "entry_mcap_unavailable_reason": mcap_reason,
        "candles_available": has_candles,
        "n_candles": len(candle_list),
        **labels,
        "label_unavailable_reason": label_reason,
        **attention,
        **narrative_proxies(symbol, name),
        "error": None,
    }
    return record


def summarize_coverage(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Summarize coverage and 2x/3x base rates over feature rows.

    Args:
        rows: Feature records from :func:`build_feature_record`.

    Returns:
        Coverage dict with row counts, availability counts and base
        rates computed over settled rows with non-null labels only.
    """
    total = len(rows)
    candles = sum(1 for r in rows if bool(r.get("candles_available")))
    funding = sum(1 for r in rows if bool(r.get("funding_available")))
    mcap = sum(1 for r in rows if r.get("entry_mcap_sol") is not None)
    scored = [
        r
        for r in rows
        if r.get("reached_2x") is not None and r.get("reached_3x") is not None
    ]
    n_scored = len(scored)
    hits_2x = sum(1 for r in scored if bool(r.get("reached_2x")))
    hits_3x = sum(1 for r in scored if bool(r.get("reached_3x")))
    return {
        "rows": total,
        "candles_available": candles,
        "funding_available": funding,
        "entry_mcap_available": mcap,
        "labels_scored": n_scored,
        "labels_unscored": total - n_scored,
        "reached_2x_hits": hits_2x,
        "reached_3x_hits": hits_3x,
        "reached_2x_rate": round(hits_2x / n_scored, 4) if n_scored else None,
        "reached_3x_rate": round(hits_3x / n_scored, 4) if n_scored else None,
    }


def _fetch_launch_token(mint: str) -> Mapping[str, Any] | None:
    """Fetch one mint's token payload, fail-soft.

    Args:
        mint: Token mint address.

    Returns:
        Token dict, or None when the fetch fails.
    """
    try:
        fetched = get_client().fetch_token(mint)
        return fetched if isinstance(fetched, dict) else None
    except Exception as exc:  # noqa: BLE001 - fail-soft per launch
        logger.debug("fetch_token failed for %s: %s", mint[:8], exc)
        return None


def _fetch_deployer_count(creator: str) -> int | None:
    """Fetch a creator's lifetime creation count, fail-soft.

    Args:
        creator: Creator wallet address.

    Returns:
        Lifetime count, or None when the index read fails.
    """
    try:
        page = get_client().fetch_user_created_coins(creator, limit=1)
    except Exception as exc:  # noqa: BLE001 - fail-soft per launch
        logger.debug("creator index failed for %s: %s", creator[:8], exc)
        return None
    raw_count = page.get("count") if isinstance(page, dict) else None
    if isinstance(raw_count, bool):
        return None
    if isinstance(raw_count, (int, float)):
        return int(raw_count)
    return None


def _fetch_launch_funding(creator: str, endpoint: str) -> tuple[str, float] | None:
    """Resolve one creator's funding edge, fail-soft.

    Args:
        creator: Creator wallet address.
        endpoint: Solana RPC HTTP endpoint.

    Returns:
        (funder, amount_sol) or None when unresolved.
    """
    try:
        edge = find_outbound_funding_edge(creator, rpc_url=endpoint or None)
        if edge is None:
            return None
        return (str(edge[0]), float(edge[1]))
    except Exception as exc:  # noqa: BLE001 - fail-soft per launch
        logger.debug("funding edge failed for %s: %s", creator[:8], exc)
        return None


def _fetch_launch_candles(mint: str) -> list[dict]:
    """Fetch one mint's 1s candles, fail-soft.

    Args:
        mint: Token mint address.

    Returns:
        Candle dicts (possibly empty when unavailable).
    """
    try:
        fetched = get_client().fetch_candlesticks(
            mint, interval="1s", limit=300, created_ts=0
        )
    except Exception as exc:  # noqa: BLE001 - fail-soft per launch
        logger.debug("candles failed for %s: %s", mint[:8], exc)
        return []
    if isinstance(fetched, list):
        return [c for c in fetched if isinstance(c, dict)]
    return []


def _listing_text(
    coin: Mapping[str, Any], token: Mapping[str, Any] | None, key: str
) -> str:
    """Return listing text, falling back to the token payload.

    Args:
        coin: Listing coin dict.
        token: Optional fetch_token payload.
        key: ``"symbol"`` or ``"name"`` field name.

    Returns:
        Text value, or "" when neither source carries a string.
    """
    value = coin.get(key, "")
    if isinstance(value, str):
        return value
    if isinstance(token, Mapping):
        maybe = token.get(key, "")
        if isinstance(maybe, str):
            return maybe
    return ""


def resolve_launch_features(
    coin: Mapping[str, Any],
    *,
    endpoint: str,
    sol_price: float | None,
    settled_target_s: int,
    now_ms: int,
) -> dict[str, Any]:
    """Resolve one listing row to a feature record, fail-soft.

    Args:
        coin: Listing coin dict with mint/creator/created_timestamp.
        endpoint: Solana RPC HTTP endpoint for the funding edge.
        sol_price: USD per SOL, or None when unavailable.
        settled_target_s: Minimum age in seconds for settled labels.
        now_ms: Current time in ms.

    Returns:
        Feature record; a launch that errors is recorded with
        ``error`` set, never dropped silently.
    """
    mint = coin.get("mint")
    creator = coin.get("creator")
    if not isinstance(mint, str) or not mint:
        return {"mint": None, "creator": None, "error": "missing mint"}
    if not isinstance(creator, str) or not creator:
        return {"mint": mint, "creator": None, "error": "missing creator"}
    try:
        created_raw = coin.get("created_timestamp")
        created_ms = int(created_raw) if isinstance(created_raw, (int, float)) else None
        token = _fetch_launch_token(mint)
        if created_ms is None and isinstance(token, Mapping):
            raw = token.get("created_timestamp")
            if isinstance(raw, (int, float)):
                created_ms = int(raw)
        return build_feature_record(
            mint=mint,
            creator=creator,
            created_ms=created_ms,
            symbol=_listing_text(coin, token, "symbol"),
            name=_listing_text(coin, token, "name"),
            deployer_count=_fetch_deployer_count(creator),
            funding_edge=_fetch_launch_funding(creator, endpoint),
            candles=_fetch_launch_candles(mint),
            token=token,
            sol_price=sol_price,
            now_ms=now_ms,
            settled_target_s=settled_target_s,
            listing=coin if isinstance(coin, Mapping) else None,
        )
    except Exception as exc:  # noqa: BLE001 - fail-soft: record, don't drop
        logger.debug("feature row failed for %s: %s", mint[:8], exc)
        return {"mint": mint, "creator": creator, "error": str(exc)}


def build_parser() -> argparse.ArgumentParser:
    """Build the argument parser for the features command."""
    parser = argparse.ArgumentParser(
        prog="rug_features",
        description="Per-launch feature table (measurements only).",
    )
    parser.add_argument("--limit", type=int, default=500)
    parser.add_argument(
        "--settled-target",
        type=int,
        default=300,
        help="min age in seconds for settled labels",
    )
    parser.add_argument(
        "--min-age-min",
        type=float,
        default=0.0,
        help="skip launches younger than this (minutes)",
    )
    parser.add_argument("--out", type=str, default=None)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--from-store", action="store_true")
    parser.add_argument(
        "--store",
        type=str,
        default=".state/analysis/launches.sqlite3",
    )
    parser.add_argument(
        "--max-pages",
        type=int,
        default=None,
        help=(
            "listing page guard "
            f"(default: ceil(limit/{LISTING_PAGE_SIZE})+2 "
            "via default_max_pages)"
        ),
    )
    return parser


def _write_csv(path: str, rows: Sequence[Mapping[str, Any]]) -> None:
    """Write feature rows to a CSV file.

    Args:
        path: Destination CSV path.
        rows: Feature records to write.
    """
    field_order = [
        "mint",
        "creator",
        "created_at_ms",
        "hour_utc",
        "deployer_lifetime_creations",
        "deployer_is_fresh",
        "funder",
        "funding_amount_sol",
        "funding_band",
        "funding_available",
        "entry_price",
        "entry_mcap_sol",
        "entry_mcap_unavailable_reason",
        "candles_available",
        "n_candles",
        "max_multiple_after_entry",
        "ath_multiple",
        "reached_2x",
        "reached_3x",
        "adverse_multiple",
        "label_unavailable_reason",
        "symbol_len",
        "name_len",
        "symbol_has_digit",
        "symbol_all_caps",
        "symbol_has_emoji",
        "name_has_emoji",
        "has_twitter",
        "twitter_is_status_link",
        "has_website",
        "has_description",
        "description_len",
        "has_image",
        "has_profile_image",
        "reply_count",
        "verified",
        "nsfw",
        "boost_mode",
        "is_currently_live",
        "has_username",
        "market_cap",
        "usd_market_cap",
        "real_sol_reserves",
        "virtual_sol_reserves",
        "complete",
        "ath_market_cap",
        "ath_market_cap_timestamp",
        "ath_multiple_from_api",
        "error",
    ]
    with Path(path).open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=field_order, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def _print_preview(rows: Sequence[Mapping[str, Any]]) -> None:
    """Print the first preview rows of the feature table.

    Args:
        rows: Feature records to preview.
    """
    print("=" * 78)
    print(" RUG FEATURES  (per-launch measurements, no verdicts)")
    print("=" * 78)
    for row in rows[:PREVIEW_ROWS]:
        print(
            f"  {str(row.get('mint'))[:8]}… "
            f"entry={row.get('entry_price')} "
            f"ath={row.get('ath_multiple')} "
            f"2x={row.get('reached_2x')} 3x={row.get('reached_3x')} "
            f"fund={row.get('funding_amount_sol')} "
            f"band={row.get('funding_band')} "
            f"mcap={row.get('entry_mcap_sol')}"
        )
    if len(rows) > PREVIEW_ROWS:
        print(f"  … ({len(rows) - PREVIEW_ROWS} more rows)")


def main(argv: Sequence[str] | None = None) -> int:
    """Run the per-launch feature extraction.

    Args:
        argv: Optional argument vector; defaults to ``sys.argv``.

    Returns:
        Process exit code (0 normally).
    """
    args = build_parser().parse_args(argv)
    limit = max(1, int(args.limit))
    settled_target_s = max(0, int(args.settled_target))
    min_age_min = max(0.0, float(args.min_age_min))

    if bool(getattr(args, "from_store", False)):
        store = AnalysisStore(str(getattr(args, "store", "")))
        try:
            rows = store.get_launches(limit=limit)
        finally:
            store.close()
        coverage = summarize_coverage(rows)
        coverage["pages_fetched"] = 0
        coverage["launches_listed"] = len(rows)
        if args.out:
            _write_csv(str(args.out), rows)
        if bool(args.json):
            print(
                json.dumps(
                    {"rows": rows, "coverage": coverage}, sort_keys=True, default=str
                )
            )
        else:
            _print_preview(rows)
        print(
            f"[coverage] rows={coverage['rows']} "
            f"candles={coverage['candles_available']} "
            f"funding={coverage['funding_available']} "
            f"mcap={coverage['entry_mcap_available']} "
            f"scored={coverage['labels_scored']} "
            f"2x_rate={coverage['reached_2x_rate']} "
            f"({coverage['reached_2x_hits']}/{coverage['labels_scored']}) "
            f"3x_rate={coverage['reached_3x_rate']} "
            f"({coverage['reached_3x_hits']}/{coverage['labels_scored']})"
        )
        return 0

    resolve_dotenv()
    providers = load_provider_settings()
    endpoint = providers.rpc_http if providers else ""

    try:
        quote = get_client().fetch_sol_price()
        raw_price = quote.get("solPrice") if isinstance(quote, dict) else None
        sol_price = (
            float(raw_price)
            if isinstance(raw_price, (int, float)) and raw_price > 0
            else None
        )
    except Exception:  # noqa: BLE001 - fail-soft: mcap reasons record it
        sol_price = None

    coins, pages_fetched = collect_recent_launches(
        limit,
        max_pages=int(args.max_pages) if args.max_pages is not None else None,
    )
    now_ms = int(time.time() * 1000)
    rows: list[dict[str, Any]] = []
    for coin in coins:
        if not isinstance(coin, Mapping):
            continue
        raw_created = coin.get("created_timestamp")
        if min_age_min > 0 and isinstance(raw_created, (int, float)):
            age_min = (now_ms - int(raw_created)) / 60000.0
            if age_min < min_age_min:
                continue
        rows.append(
            resolve_launch_features(
                coin,
                endpoint=endpoint,
                sol_price=sol_price,
                settled_target_s=settled_target_s,
                now_ms=now_ms,
            )
        )
        time.sleep(0.02)

    coverage = summarize_coverage(rows)
    coverage["pages_fetched"] = pages_fetched
    coverage["launches_listed"] = len(coins)

    if args.out:
        _write_csv(str(args.out), rows)

    if bool(args.json):
        print(
            json.dumps(
                {"rows": rows, "coverage": coverage}, sort_keys=True, default=str
            )
        )
    else:
        _print_preview(rows)
    print(
        f"[coverage] rows={coverage['rows']} "
        f"candles={coverage['candles_available']} "
        f"funding={coverage['funding_available']} "
        f"mcap={coverage['entry_mcap_available']} "
        f"scored={coverage['labels_scored']} "
        f"2x_rate={coverage['reached_2x_rate']} "
        f"({coverage['reached_2x_hits']}/{coverage['labels_scored']}) "
        f"3x_rate={coverage['reached_3x_rate']} "
        f"({coverage['reached_3x_hits']}/{coverage['labels_scored']})"
    )
    _ = default_max_pages  # page-cap contract lives in collect_recent_launches
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
