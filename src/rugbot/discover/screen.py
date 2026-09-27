"""Bible layer-1 screen over pump.fun's launch listing (the Axiom "Pulse" view).

Pages the newest-first and recently-traded listings, keeps SOL-quoted pump.fun
coins in the age window, and measures each survivor's USD volume from its
1-minute candles (one call per coin). Creator lifetime launches are read only
for coins that pass every cheaper filter.
"""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

from rugbot.domain.pump_curve import TOKEN_SUPPLY_UI, nonstandard_curve_reason
from rugbot.ingest.pump.create_event_decoder import SOL_PUBKEY
from rugbot.integrations.pumpfun_api import get_client

LISTING_PAGE_SIZE = 50
# The listing serves at most ~1000 rows per sort (deeper offsets are empty or
# 429). Newest-first covers ~40 min of launches; recently-traded reaches the
# older coins that still have volume.
MAX_LISTING_OFFSET = 1000
NEWEST_FIRST = "created_timestamp"
LISTING_SORTS = (NEWEST_FIRST, "last_trade_timestamp")
MS_PER_MINUTE = 60_000
PUMP_PROTOCOL = "pump"
VOLUME_CANDLE_INTERVAL = "1m"
VOLUME_CANDLE_LIMIT = 1000
# The 1s candle feed returns a coin's latest 1000 active seconds; the creation
# candle is measurable only when that history starts within this many seconds.
CREATION_CANDLE_MAX_OFFSET_S = 2
# frontend-api allows 60 requests/minute (x-ratelimit-limit); the swap-api
# candles host allows 1000 per short window, so volumes are fetched in parallel.
LISTING_PAGE_SPACING_SECONDS = 1.1
VOLUME_WORKERS = 8


@dataclass(frozen=True, slots=True)
class ScreenFilters:
    """Screen thresholds; a ``None`` bound is not applied."""

    min_age_min: float
    max_age_min: float
    min_volume_usd: float | None
    max_volume_usd: float | None
    max_mc_usd: float | None
    max_dev_launches: int | None
    include_graduated: bool = False
    max_creation_mc_usd: float | None = None


@dataclass(frozen=True, slots=True)
class ScreenedCoin:
    """One coin that passed the screen, with the measurements used."""

    mint: str
    symbol: str
    name: str
    creator: str
    age_min: float
    mc_usd: float
    ath_mc_usd: float
    volume_usd: float
    dev_launches: int | None
    creation_mc_usd: float | None


@dataclass(frozen=True, slots=True)
class ScreenResult:
    """Screen output plus what was looked at and why coins were dropped."""

    coins: tuple[ScreenedCoin, ...]
    listed_in_window: int
    dropped: dict[str, int]


def _volume_usd(mint: str) -> float:
    candles = get_client().fetch_candlesticks(
        mint, interval=VOLUME_CANDLE_INTERVAL, limit=VOLUME_CANDLE_LIMIT
    )
    return sum(float(candle["volume"]) for candle in candles)


def _creation_mc_usd(coin: dict) -> float | None:
    """USD market cap at the creation candle's high, or None if out of reach."""
    candles = get_client().fetch_candlesticks(
        coin["mint"], interval="1s", limit=VOLUME_CANDLE_LIMIT
    )
    if not candles:
        return None
    offset_s = (candles[0]["timestamp"] - coin["created_timestamp"]) / 1000
    if offset_s > CREATION_CANDLE_MAX_OFFSET_S:
        return None
    return float(candles[0]["high"]) * TOKEN_SUPPLY_UI


def _drop_reason(coin: dict, filters: ScreenFilters) -> str | None:  # noqa: PLR0911
    if coin.get("protocol") != PUMP_PROTOCOL:
        return "not pump.fun"
    if coin.get("complete") is not False and not filters.include_graduated:
        return "graduated to PumpSwap (not a new pair)"
    quote = coin.get("quote_mint")
    if quote is None:
        return "quote unknown"
    if quote != SOL_PUBKEY:
        return "non-SOL quote"
    virtual_sol = coin.get("virtual_sol_reserves")
    virtual_token = coin.get("virtual_token_reserves")
    curve_issue = nonstandard_curve_reason(
        virtual_sol * virtual_token
        if isinstance(virtual_sol, int) and isinstance(virtual_token, int)
        else None,
        mayhem=False,
    )
    if curve_issue is not None:
        return curve_issue
    mc = coin.get("usd_market_cap")
    if filters.max_mc_usd is not None and (
        not isinstance(mc, (int, float)) or mc > filters.max_mc_usd
    ):
        return "market cap above max"
    return None


def screen_launches(filters: ScreenFilters) -> ScreenResult:  # noqa: C901, PLR0912
    """Screen every pump.fun launch whose age falls inside the window."""

    client = get_client()
    now_ms = time.time() * 1000
    seen: set[str] = set()
    in_window: list[tuple[dict, float]] = []
    for sort in LISTING_SORTS:
        for offset in range(0, MAX_LISTING_OFFSET, LISTING_PAGE_SIZE):
            page = client.fetch_recent_launches(
                limit=LISTING_PAGE_SIZE, offset=offset, sort=sort
            )
            time.sleep(LISTING_PAGE_SPACING_SECONDS)
            for coin in page:
                mint = coin.get("mint")
                created = coin.get("created_timestamp")
                if mint in seen or not isinstance(created, (int, float)):
                    continue
                seen.add(mint)
                age = (now_ms - created) / MS_PER_MINUTE
                if filters.min_age_min <= age <= filters.max_age_min:
                    in_window.append((coin, age))
            oldest = min(
                (c["created_timestamp"] for c in page if "created_timestamp" in c),
                default=None,
            )
            if not page or (
                sort == NEWEST_FIRST
                and oldest is not None
                and (now_ms - oldest) / MS_PER_MINUTE > filters.max_age_min
            ):
                break

    dropped: dict[str, int] = {}
    reasons = {coin["mint"]: _drop_reason(coin, filters) for coin, _ in in_window}
    measured = [coin["mint"] for coin, _ in in_window if reasons[coin["mint"]] is None]
    with ThreadPoolExecutor(VOLUME_WORKERS) as pool:
        volumes = dict(zip(measured, pool.map(_volume_usd, measured), strict=True))
    kept: list[ScreenedCoin] = []
    for coin, age in in_window:
        reason = reasons[coin["mint"]]
        volume = volumes.get(coin["mint"], 0.0)
        if reason is None:
            if filters.min_volume_usd is not None and volume < filters.min_volume_usd:
                reason = "volume below min"
            elif filters.max_volume_usd is not None and volume > filters.max_volume_usd:
                reason = "volume above max"
        creation_mc = None
        if reason is None and filters.max_creation_mc_usd is not None:
            creation_mc = _creation_mc_usd(coin)
            if creation_mc is None:
                reason = "creation candle not measurable (busy coin)"
            elif creation_mc > filters.max_creation_mc_usd:
                reason = "creation candle MC above max"
        dev_launches = None
        if reason is None and filters.max_dev_launches is not None:
            count = client.fetch_user_created_coins(coin["creator"], limit=1)["count"]
            dev_launches = count if count > 0 else None
            if dev_launches is not None and dev_launches > filters.max_dev_launches:
                reason = "dev launches above max"
        if reason is not None:
            dropped[reason] = dropped.get(reason, 0) + 1
            continue
        kept.append(
            ScreenedCoin(
                mint=coin["mint"],
                symbol=str(coin.get("symbol", "")),
                name=str(coin.get("name", "")),
                creator=str(coin.get("creator", "")),
                age_min=age,
                mc_usd=float(coin.get("usd_market_cap") or 0),
                ath_mc_usd=float(coin.get("ath_market_cap") or 0),
                volume_usd=volume,
                dev_launches=dev_launches,
                creation_mc_usd=creation_mc,
            )
        )
    kept.sort(key=lambda item: item.volume_usd, reverse=True)
    return ScreenResult(
        coins=tuple(kept), listed_in_window=len(in_window), dropped=dropped
    )
