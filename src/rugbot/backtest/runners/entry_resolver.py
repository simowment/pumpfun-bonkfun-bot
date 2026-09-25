"""Tiered entry resolution for creator backtest samples.

Tier 1 uses 1s swap-api candles inside a valid launch window; Tier 2
reconstructs entry from earliest on-chain trades. 1m candles never define
entry. Samples without a reconstructable entry are excluded (None).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

from rugbot.domain.launch_window import launch_window_is_valid
from rugbot.domain.market_data import fetch_early_launch_trades
from rugbot.utils.logger import get_logger

if TYPE_CHECKING:
    from rugbot.backtest.runners.creator_backtest_runner import CreatorSample

logger = get_logger(__name__)

_CREATED_AT_MS_THRESHOLD: int = 10_000_000_000

# Outsiders cannot fill inside the creation/bundle second; the first
# achievable fill is one candle after the mint's creation second.
ENTRY_LATENCY_SECONDS: int = 1

_MIN_CANDLES_WITHOUT_CREATED_MS: int = 2


class CandleClient(Protocol):
    """Minimal swap-api candle client used for Tier 1 entry."""

    def fetch_candlesticks(self, mint: str, **kwargs: object) -> list[dict]:
        """Fetch candles for a mint."""
        ...


def _to_float(value: object) -> float | None:
    """Parse a positive float from str/int/float, else None."""
    if isinstance(value, bool):
        return None
    try:
        parsed = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if parsed <= 0:
        return None
    return parsed


def _candle_ts(candle: dict) -> int | None:
    """Return a candle's timestamp in ms, or None when unparsable."""
    try:
        return int(candle.get("timestamp", 0))
    except (TypeError, ValueError):
        return None


def _find_entry_index(candles: list[dict], created_ms: int | None) -> int | None:
    """Return the index of the first achievable entry candle.

    Args:
        candles: Swap-api 1s candle dicts in time order.
        created_ms: Mint creation timestamp in ms, or None for index 1.

    Returns:
        Entry candle index, or None when no candle qualifies.
    """
    if created_ms is None:
        if len(candles) < _MIN_CANDLES_WITHOUT_CREATED_MS:
            return None
        return 1
    try:
        threshold_ms = int(created_ms) + ENTRY_LATENCY_SECONDS * 1000
    except (TypeError, ValueError):
        return None
    for index, candle in enumerate(candles):
        ts = _candle_ts(candle)
        if ts is not None and ts >= threshold_ms:
            return index
    return None


def _points_after_entry(
    candles: list[dict], entry: float, entry_ts: int
) -> tuple[list[tuple[float, float]], float]:
    """Build pessimistic low-before-high points from post-entry candles.

    Args:
        candles: Swap-api 1s candle dicts in time order.
        entry: Achievable entry price (first fillable second's close).
        entry_ts: Entry candle timestamp in ms.

    Returns:
        (points, ath_multiplier); the entry candle feeds ATH but emits
        no point since its low predates the fill.
    """
    points: list[tuple[float, float]] = []
    ath = 0.0
    for candle in candles:
        ts = _candle_ts(candle)
        if ts is None or ts < entry_ts:
            continue
        high = _to_float(candle.get("high"))
        if high is not None:
            ath = max(ath, float(high) / entry)
        if ts == entry_ts:
            continue
        low = _to_float(candle.get("low"))
        if low is None or high is None:
            continue
        sec = (ts - entry_ts) / 1000.0
        points.append((float(sec), float(low) / entry))
        points.append((float(sec), float(high) / entry))
    return points, ath


def trajectory_from_1s_candles(
    candles: list[dict],
    *,
    created_ms: int | None = None,
) -> tuple[tuple[tuple[float, float], ...], float | None]:
    """Build a pessimistic trajectory from 1s candles.

    The entry candle is the first achievable second: the first candle at
    or after ``creation_seconds + ENTRY_LATENCY_SECONDS`` (the creation /
    bundle candle itself is the dev's fill and is never tradable). When
    ``created_ms`` is None, ``candles[1]`` is the entry candle.

    Args:
        candles: Swap-api 1s candle dicts (timestamp ms, low/high/close).
        created_ms: Mint creation timestamp in ms, or None for index-1.

    Returns:
        (trajectory, ath_multiplier) with low-before-high point ordering.
        Points come from post-entry candles only; the entry candle emits
        no point (its low predates the fill). Returns ((), None) when no
        achievable entry exists.
    """
    if not candles:
        return (), None
    entry_index = _find_entry_index(candles, created_ms)
    if entry_index is None:
        return (), None
    entry_candle = None
    entry = None
    entry_ts = 0
    for index in range(entry_index, len(candles)):
        candidate = candles[index]
        parsed = _to_float(candidate.get("close"))
        if parsed is None:
            continue
        ts = _candle_ts(candidate)
        if ts is None:
            continue
        entry_candle = candidate
        entry = parsed
        entry_ts = ts
        break
    if entry_candle is None or entry is None:
        return (), None
    points, ath = _points_after_entry(candles, entry, entry_ts)
    if not points:
        return (), None
    return tuple(points), float(ath)


def trajectory_from_early_trades(  # noqa: C901
    trades: list[dict],
) -> tuple[tuple[tuple[float, float], ...], float | None]:
    """Build a trajectory from earliest on-chain trades.

    Args:
        trades: Trade dicts with slot, side, and price_ppm.

    Returns:
        (trajectory, ath_multiplier); entry is the first trade price.
        Returns ((), None) when no valid points exist.
    """
    if not trades:
        return (), None

    def _side_rank(trade: dict) -> int:
        return 0 if str(trade.get("side")) == "buy" else 1

    def _slot(trade: dict) -> int:
        try:
            return int(trade.get("slot", 0))
        except (TypeError, ValueError):
            return 0

    ordered = sorted(trades, key=lambda t: (_slot(t), _side_rank(t)))
    entry_slot = _slot(ordered[0])
    try:
        entry_ppm = int(ordered[0].get("price_ppm", 0))
    except (TypeError, ValueError):
        return (), None
    if entry_ppm <= 0:
        return (), None
    points: list[tuple[float, float]] = []
    ath = 0.0
    for trade in ordered:
        try:
            ppm = int(trade.get("price_ppm", 0))
        except (TypeError, ValueError):
            continue
        if ppm <= 0:
            continue
        sec = float(_slot(trade) - entry_slot) * 0.4
        mult = float(ppm) / float(entry_ppm)
        points.append((sec, mult))
        ath = max(ath, mult)
    if not points:
        return (), None
    return tuple(points), float(ath)


def build_entry_sample(  # noqa: PLR0913
    mint: str,
    *,
    creator: str,
    created_at: int,
    created_slot: int,
    client: CandleClient,
    rpc_url: str | None = None,
) -> CreatorSample | None:
    """Resolve one mint to a CreatorSample via Tier 1 then Tier 2.

    Args:
        mint: Token mint address.
        creator: Creator wallet address.
        created_at: Creation timestamp (s or ms; ms passed to window check).
        created_slot: Creation slot (fallback sort key).
        client: PumpFun swap-api client.
        rpc_url: Optional RPC endpoint override for Tier 2.

    Returns:
        CreatorSample with entry_basis "1s" or "onchain_early", or None
        when no entry is reconstructable. Never raises.
    """
    from rugbot.backtest.runners.creator_backtest_runner import (  # noqa: PLC0415
        CreatorSample,
    )

    try:
        created_ms = (
            created_at * 1000 if created_at < _CREATED_AT_MS_THRESHOLD else created_at
        )
        candles: list[dict] = []
        try:
            fetched = client.fetch_candlesticks(
                mint, interval="1s", limit=300, created_ts=0
            )
            if isinstance(fetched, list):
                candles = fetched
        except TypeError:
            fetched = client.fetch_candlesticks(mint, interval="1s", limit=300)
            if isinstance(fetched, list):
                candles = fetched
        except Exception as exc:  # noqa: BLE001 - Tier 1 fail-soft, fall to Tier 2
            logger.debug("tier1 candles failed for %s: %s", mint, exc)
            candles = []
        if candles and launch_window_is_valid(candles, created_ms):
            traj, ath = trajectory_from_1s_candles(candles, created_ms=int(created_ms))
            if traj:
                return CreatorSample(
                    mint=mint,
                    creator=creator,
                    created_at=created_at,
                    created_slot=created_slot,
                    trajectory=traj,
                    ath_multiplier=ath,
                    entry_basis="1s",
                )
        trades = fetch_early_launch_trades(mint, rpc_url=rpc_url)
        if trades:
            traj, ath = trajectory_from_early_trades(trades)
            if traj:
                return CreatorSample(
                    mint=mint,
                    creator=creator,
                    created_at=created_at,
                    created_slot=created_slot,
                    trajectory=traj,
                    ath_multiplier=ath,
                    entry_basis="onchain_early",
                )
        return None  # noqa: TRY300 - single fail-soft exit after tiered attempts
    except Exception as exc:  # noqa: BLE001 - fail-soft: mint excluded, not crashed
        logger.debug("build_entry_sample failed for %s: %s", mint, exc)
        return None
