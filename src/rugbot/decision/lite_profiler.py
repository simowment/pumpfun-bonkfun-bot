"""Lite launch profiler over REST candle shapes (estimate, not executable proof).

Lane B of the REST-first simplification. Operates on plain dicts matching
``docs/PUMPFUN_API_REFERENCE.md`` candle shapes so this module can run in
parallel with the REST-client lane without importing ``pumpfun_api``.

Unit note (pinned 2026-09-03 with live read-only GETs on mint
``5ipdE2DsuykvwBaGoHEv6x7Z28qLhQUgdsaCfd5Ppump``): swap-api candle
open/high/low/close are USD per token (last close x 1e9 equalled
``market_cap_usd`` and max high x 1e9 equalled ``ath_market_cap`` exactly;
reserve-implied SOL price x ``solPrice`` matched the candle close). Hence
``mcap_sol = price_usd * supply_tokens / sol_price`` with
``supply_tokens = total_supply / 10**base_decimals``. ``ath_market_cap``
units are ambiguous, so callers recompute ATH mcap from candles instead of
trusting that field.

Entry definition (redefined 2026-09-04 after live verification on dev
``HX2Sr1gKJC53NKEBPEy9KRoW2M1C6NoJT241sVM4AEnA``): entry is the
first-candle CLOSE, the first 1s print after block-0 activity and the
earliest achievable snipe fill proxy. The first-candle OPEN clusters at the
fixed curve-start price (~28 SOL mcap) on every mint — a constant no
outsider can fill at, since dev and satellite bundle buys land in block 0
before any buyable print. TP multiples are measured from the close entry.

Optimal-TP method (no hardcoded grid): candidates are the observed per-mint
ATH multiples. For candidate t, winrate is the share of mints with ATH >= t
and ``EV(t) = W*t - (1-W)*1.0 - fee`` (full-loss stop, fee from ``fee_bps``).
The optimum is argmax EV subject to winrate >= 70% (Bible floor), with ties
broken toward the lower t (safer exit). The lowest observed ATH always has
W=100%, so a result always exists whenever at least one launch is scored.

All outputs are lite estimates, not executable proof: no quote simulation,
no liquidity or slippage modelling, and no tradability guarantee.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation

_logger = logging.getLogger(__name__)

PPM_DENOMINATOR = 1_000_000
FULL_LOSS_STOP_PPM = 1_000_000
OPTIMAL_WINRATE_FLOOR_PCT = 70.0
OPTIMAL_QUALIFY_MIN_LAUNCHES = 10


@dataclass(frozen=True)
class LiteLaunchResult:
    """Per-mint lite outcome (estimate, not executable proof).

    Attributes:
        mint: Token mint address.
        entry_price_sol: Entry fill proxy in SOL from the first candle
            close (the open is the unachievable curve-start constant).
        ath_multiple: Max candle high divided by the entry price.
        hits: One bool per requested TP level, True when ATH reached it.
        entry_mcap_sol: Precomputed entry market cap in SOL (0.0 if unknown).
        ath_mcap_sol: Precomputed ATH market cap in SOL (0.0 if unknown).
    """

    mint: str
    entry_price_sol: Decimal
    ath_multiple: Decimal
    hits: tuple[bool, ...]
    entry_mcap_sol: float = 0.0
    ath_mcap_sol: float = 0.0


@dataclass(frozen=True)
class LiteTpEstimate:
    """Aggregate lite TP stats (estimate, not executable proof).

    Attributes:
        tp_ppm: Take-profit level in parts per million above entry.
        hits: Number of launches whose ATH reached this TP level.
        winrate_ppm: Floor-truncated hit rate scaled to parts per million.
        ev_net_ppm: Net expectancy estimate in parts per million.
    """

    tp_ppm: int
    hits: int
    winrate_ppm: int
    ev_net_ppm: int


@dataclass(frozen=True)
class LiteOptimalTp:
    """Single computed optimal TP (estimate, not executable proof).

    Attributes:
        tp_multiple: Optimal exit multiple over entry.
        winrate_pct: Share of scored launches with ATH >= tp_multiple.
        ev_multiple: Net expectancy in multiples of entry stake.
        launch_count: Scored launches the optimum was computed over.
        qualifies: True when launch_count >= 10 and winrate >= 70%.
    """

    tp_multiple: float
    winrate_pct: float
    ev_multiple: float
    launch_count: int
    qualifies: bool


@dataclass(frozen=True)
class LiteProfileReport:
    """Aggregated lite profile (estimate, not executable proof).

    Attributes:
        launch_count: Number of mints profiled (empty candles abstained).
        skipped_count: Number of mints skipped for invalid/empty candles.
        launches: Per-mint lite outcomes in input order.
        per_tp: One aggregate estimate per requested TP level, same order.
        ath_avg: Mean ATH multiple over scored launches (0.0 when none).
        ath_max: Max ATH multiple over scored launches (0.0 when none).
        ath_min: Min ATH multiple over scored launches (0.0 when none).
        ath_median: Median ATH multiple over scored launches (0.0 when none).
        entry_mcap_sol_avg: Mean entry mcap in SOL over scored launches
            with mcap input (0.0 when none).
        entry_mcap_sol_min: Min entry mcap in SOL (0.0 when none).
        entry_mcap_sol_max: Max entry mcap in SOL (0.0 when none).
        ath_mcap_sol_avg: Mean ATH mcap in SOL (0.0 when none).
        ath_mcap_sol_median: Median ATH mcap in SOL (0.0 when none).
        ath_mcap_sol_max: Max ATH mcap in SOL (0.0 when none).
        ath_mcap_sol_min: Min ATH mcap in SOL (0.0 when none).
        mcap_scored_count: Scored launches carrying mcap input.
        optimal_tp: Computed optimum over observed ATH multiples, or None
            when no launch was scored.
    """

    launch_count: int
    skipped_count: int
    launches: tuple[LiteLaunchResult, ...] = field(default_factory=tuple)
    per_tp: tuple[LiteTpEstimate, ...] = field(default_factory=tuple)
    ath_avg: float = 0.0
    ath_max: float = 0.0
    ath_min: float = 0.0
    ath_median: float = 0.0
    entry_mcap_sol_avg: float = 0.0
    entry_mcap_sol_min: float = 0.0
    entry_mcap_sol_max: float = 0.0
    ath_mcap_sol_avg: float = 0.0
    ath_mcap_sol_median: float = 0.0
    ath_mcap_sol_max: float = 0.0
    ath_mcap_sol_min: float = 0.0
    mcap_scored_count: int = 0
    optimal_tp: LiteOptimalTp | None = None


def _to_decimal(value: object) -> Decimal | None:
    """Narrow a raw candle field to Decimal.

    Args:
        value: Raw field value expected to be a decimal-like str/int/float.

    Returns:
        Parsed Decimal, or None when the value is missing or unparsable.
    """
    if isinstance(value, Decimal):
        return value
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float, str)):
        try:
            parsed = Decimal(str(value))
        except (InvalidOperation, ValueError):
            return None
        if parsed.is_nan() or parsed.is_infinite():
            return None
        return parsed
    return None


def _median(values: list[float]) -> float:
    """Return the median of a sorted float list.

    Args:
        values: Sorted floats in ascending order.

    Returns:
        Median value, or 0.0 when the list is empty.
    """
    if not values:
        return 0.0
    middle = len(values) // 2
    if len(values) % 2:
        return values[middle]
    return (values[middle - 1] + values[middle]) / 2


def _valid_mcap_pair(value: object) -> tuple[float, float] | None:
    """Narrow a caller-supplied mcap pair to finite positive SOL floats.

    Args:
        value: Expected ``(entry_mcap_sol, ath_mcap_sol)`` pair.

    Returns:
        Validated float pair, or None when missing or non-positive.
    """
    if not isinstance(value, (tuple, list)):
        return None
    try:
        entry, ath = value
    except ValueError:
        return None
    if isinstance(entry, bool) or isinstance(ath, bool):
        return None
    if not isinstance(entry, (int, float)) or not isinstance(ath, (int, float)):
        return None
    entry_mcap, ath_mcap = float(entry), float(ath)
    if not (
        math.isfinite(entry_mcap)
        and math.isfinite(ath_mcap)
        and entry_mcap > 0
        and ath_mcap > 0
    ):
        return None
    return (entry_mcap, ath_mcap)


def _profile_single_launch(
    mint: str,
    candles: list[dict[str, object]],
    tp_levels_ppm: tuple[int, ...],
    mcap_sol: tuple[float, float] | None = None,
) -> LiteLaunchResult | None:
    """Profile one mint from its candle list.

    Args:
        mint: Token mint address.
        candles: Candle dicts with str ``open``/``high``/``close`` prices;
            entry is read from the first ``close``.
        tp_levels_ppm: TP levels in parts per million above entry.
        mcap_sol: Optional precomputed ``(entry, ATH)`` mcap in SOL.

    Returns:
        LiteLaunchResult, or None to abstain on empty/invalid candles.
    """
    if not candles:
        return None
    entry = _to_decimal(candles[0].get("close"))
    if entry is None or entry <= 0:
        _logger.debug("Skipping mint %s: invalid entry close", mint)
        return None
    max_high: Decimal | None = None
    for candle in candles:
        high = _to_decimal(candle.get("high"))
        if high is None or high <= 0:
            _logger.debug("Skipping mint %s: invalid candle high", mint)
            return None
        if max_high is None or high > max_high:
            max_high = high
    if max_high is None:
        return None
    ath_multiple = max_high / entry
    hits = tuple(
        ath_multiple >= (Decimal(PPM_DENOMINATOR + tp) / Decimal(PPM_DENOMINATOR))
        for tp in tp_levels_ppm
    )
    mcap_pair = _valid_mcap_pair(mcap_sol)
    return LiteLaunchResult(
        mint=mint,
        entry_price_sol=entry,
        ath_multiple=ath_multiple,
        hits=hits,
        entry_mcap_sol=mcap_pair[0] if mcap_pair else 0.0,
        ath_mcap_sol=mcap_pair[1] if mcap_pair else 0.0,
    )


def _select_optimal_tp(ath_values: list[float], fee_bps: int) -> LiteOptimalTp | None:
    """Select the argmax-EV observed ATH multiple above the winrate floor.

    Args:
        ath_values: Per-launch ATH multiples (unsorted).
        fee_bps: Round-trip fee in basis points (100 bps = 0.01 multiple).

    Returns:
        LiteOptimalTp with ties broken toward the lower multiple, or None
        when no launch was scored.
    """
    if not ath_values:
        return None
    total = len(ath_values)
    optimal: LiteOptimalTp | None = None
    for candidate in sorted(set(ath_values)):
        wins = sum(1 for value in ath_values if value >= candidate)
        winrate_pct = wins / total * 100
        if winrate_pct < OPTIMAL_WINRATE_FLOOR_PCT:
            continue
        ev_multiple = (
            winrate_pct / 100 * candidate - (1 - winrate_pct / 100) - fee_bps / 10_000
        )
        if optimal is None or ev_multiple > optimal.ev_multiple:
            optimal = LiteOptimalTp(
                tp_multiple=candidate,
                winrate_pct=winrate_pct,
                ev_multiple=ev_multiple,
                launch_count=total,
                qualifies=(
                    total >= OPTIMAL_QUALIFY_MIN_LAUNCHES
                    and winrate_pct >= OPTIMAL_WINRATE_FLOOR_PCT
                ),
            )
    return optimal


def profile_launches(
    candles_by_mint: dict[str, list[dict[str, object]]],
    tp_levels_ppm: list[int] | None = None,
    fee_bps: int = 0,
    stop_ppm: int = FULL_LOSS_STOP_PPM,
    mcap_sol_by_mint: dict[str, tuple[float, float]] | None = None,
) -> LiteProfileReport:
    """Aggregate lite TP estimates across mints (estimate, not executable proof).

    Lite expectancy estimate per TP level, explicitly NOT executable proof:
    no quote engine, no liquidity/slippage modelling, no fill guarantee.

    Args:
        candles_by_mint: Mint to candle list, each candle carrying str
            ``open``/``high``/``close`` prices per the API reference
            shapes; entry is the first ``close`` (first buyable print).
        tp_levels_ppm: Optional explicit grid levels retained for per_tp;
            pass None for the single computed optimum only.
        fee_bps: Round-trip fee in basis points, fetched once per run by
            the caller. One basis point equals 100 ppm.
        stop_ppm: Loss level in parts per million applied to misses;
            defaults to a full loss.
        mcap_sol_by_mint: Optional mint to precomputed
            ``(entry_mcap_sol, ath_mcap_sol)`` in SOL. Mints without a
            valid pair stay scored for TP but are excluded from mcap stats.

    Returns:
        LiteProfileReport with per-TP winrate and net EV estimates where
        ``ev_net_ppm = winrate_ppm * tp_ppm / 1_000_000 - (1_000_000 -
        winrate_ppm) * stop_ppm / 1_000_000 - fee_bps * 100``,
        floor-truncated (ceil-free) with Decimal arithmetic, plus the
        single computed ``optimal_tp`` over observed ATH multiples.
    """
    tp_tuple = tuple(tp_levels_ppm or ())
    mcap_map = mcap_sol_by_mint or {}
    launches: list[LiteLaunchResult] = []
    skipped = 0
    for mint, candles in candles_by_mint.items():
        if not isinstance(candles, list) or not candles:
            skipped += 1
            _logger.debug("Skipping mint %s: empty candles, abstained", mint)
            continue
        result = _profile_single_launch(mint, candles, tp_tuple, mcap_map.get(mint))
        if result is None:
            skipped += 1
            continue
        launches.append(result)
    launch_count = len(launches)
    fee_ppm = Decimal(fee_bps * 100)
    per_tp: list[LiteTpEstimate] = []
    for index, tp_ppm in enumerate(tp_tuple):
        hits = sum(1 for launch in launches if launch.hits[index])
        winrate_ppm = (hits * PPM_DENOMINATOR // launch_count) if launch_count else 0
        winrate = Decimal(winrate_ppm)
        ev_net = (
            winrate * Decimal(tp_ppm) / Decimal(PPM_DENOMINATOR)
            - (Decimal(PPM_DENOMINATOR) - winrate)
            * Decimal(stop_ppm)
            / Decimal(PPM_DENOMINATOR)
            - fee_ppm
        )
        per_tp.append(
            LiteTpEstimate(
                tp_ppm=tp_ppm,
                hits=hits,
                winrate_ppm=winrate_ppm,
                ev_net_ppm=int(ev_net),
            )
        )
    ath_values = sorted(float(launch.ath_multiple) for launch in launches)
    if ath_values:
        ath_avg = sum(ath_values) / len(ath_values)
        ath_max = ath_values[-1]
        ath_min = ath_values[0]
        ath_median = _median(ath_values)
    else:
        ath_avg = ath_max = ath_min = ath_median = 0.0
    mcap_entries = sorted(
        launch.entry_mcap_sol for launch in launches if launch.entry_mcap_sol > 0
    )
    mcap_aths = sorted(
        launch.ath_mcap_sol for launch in launches if launch.ath_mcap_sol > 0
    )
    mcap_scored_count = min(len(mcap_entries), len(mcap_aths))
    if mcap_entries:
        entry_mcap_sol_avg = sum(mcap_entries) / len(mcap_entries)
        entry_mcap_sol_min = mcap_entries[0]
        entry_mcap_sol_max = mcap_entries[-1]
    else:
        entry_mcap_sol_avg = entry_mcap_sol_min = 0.0
        entry_mcap_sol_max = 0.0
    if mcap_aths:
        ath_mcap_sol_avg = sum(mcap_aths) / len(mcap_aths)
        ath_mcap_sol_max = mcap_aths[-1]
        ath_mcap_sol_min = mcap_aths[0]
        ath_mcap_sol_median = _median(mcap_aths)
    else:
        ath_mcap_sol_avg = ath_mcap_sol_max = 0.0
        ath_mcap_sol_min = ath_mcap_sol_median = 0.0
    optimal_tp = _select_optimal_tp(ath_values, fee_bps)
    return LiteProfileReport(
        launch_count=launch_count,
        skipped_count=skipped,
        launches=tuple(launches),
        per_tp=tuple(per_tp),
        ath_avg=ath_avg,
        ath_max=ath_max,
        ath_min=ath_min,
        ath_median=ath_median,
        entry_mcap_sol_avg=entry_mcap_sol_avg,
        entry_mcap_sol_min=entry_mcap_sol_min,
        entry_mcap_sol_max=entry_mcap_sol_max,
        ath_mcap_sol_avg=ath_mcap_sol_avg,
        ath_mcap_sol_median=ath_mcap_sol_median,
        ath_mcap_sol_max=ath_mcap_sol_max,
        ath_mcap_sol_min=ath_mcap_sol_min,
        mcap_scored_count=mcap_scored_count,
        optimal_tp=optimal_tp,
    )
