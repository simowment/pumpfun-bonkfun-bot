"""Pairs lab: alpha-extraction analysis over the discover store (paper-only).

Replays the newpairs exit ladder on recorded finalized trades to label every
launch with executable net PnL (fees + bonding-curve slippage via the same
quote engine as ``rug_scalp``), then reports base rates and per-feature
tercile lift with Wilson confidence intervals. Pure analysis over in-memory
rows: no I/O, no orders. Thresholds are outputs of this tool, not inputs.

Label semantics follow triple-barrier labeling (upper = TP ladder, lower =
stop-loss, vertical = horizon close) so statistics are unconditional and
path-independent: the portfolio circuit breaker never fires here.
"""

# ruff: noqa: C901, PLR0912, PLR0915, TRY003

from __future__ import annotations

import statistics
from collections import Counter
from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING, Any

from rugbot.backtest.scalper_backtest import (
    DEFAULT_FEE_CONFIG,
    LAMPORTS_PER_SOL,
    _price_ppm_from_trade,
    _synthetic_reserves,
)
from rugbot.domain.decisions import AbstainResult
from rugbot.domain.quote_engine import (
    executable_buy_quote,
    executable_sell_quote,
)
from rugbot.domain.quotes import QuotePath
from rugbot.domain.scalper_strategy import (
    ScalperConfig,
    decide_scalper_exit,
    next_filled,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

    from rugbot.domain.fees import FeeConfig

Z_95 = 1.96
FALLBACK_FEE_FRACTION = 0.0125
PEAK_2X = 2.0
PEAK_5X = 5.0
PEAK_10X = 10.0
PEAK_MULTIPLIER_TIERS: tuple[float, ...] = (1.5, PEAK_2X, PEAK_5X, PEAK_10X)

LIFT_FEATURE_NAMES: tuple[str, ...] = (
    "unique_buyers",
    "n_buys",
    "buy_volume_lamports",
    "top_buyer_share",
    "dev_buys",
    "same_slot_buys",
    "sniper_ratio",
    "return_ppm",
    "entry_slot_offset",
)


@dataclass(frozen=True, slots=True)
class PairsLabConfig:
    """Parameters of the labeling replay (paper-only; no order is ever sent).

    entry_delay_slots approximates decision latency (~10 s at 2.5 slots/s);
    horizon_slots is the vertical barrier; sniper_window_slots defines the
    first-seconds buyer window used by sniper_ratio.
    """

    entry_delay_slots: int = 25
    entry_window_slots: int = 50
    horizon_slots: int = 750
    sniper_window_slots: int = 5
    position_size_sol: float = 0.1
    tp_levels_pct: tuple[float, ...] = (100.0, 400.0, 900.0)
    sell_fractions: tuple[float, ...] = (0.34, 0.33, 0.33)
    sl_pct: float = 40.0
    min_labels: int = 30
    min_bucket_count: int = 30

    def __post_init__(self) -> None:
        if self.entry_delay_slots < 0:
            raise ValueError("entry_delay_slots must be >= 0")
        if self.entry_window_slots < 1:
            raise ValueError("entry_window_slots must be >= 1")
        if self.horizon_slots <= self.entry_delay_slots + self.entry_window_slots:
            raise ValueError("horizon_slots must exceed the entry window")
        if not 0 <= self.sniper_window_slots <= self.entry_delay_slots:
            raise ValueError("sniper_window_slots must be within pre-window")
        if self.position_size_sol <= 0:
            raise ValueError("position_size_sol must be positive")
        if self.sl_pct <= 0:
            raise ValueError("sl_pct must be positive")
        if len(self.tp_levels_pct) != len(self.sell_fractions):
            raise ValueError("tp_levels_pct and sell_fractions must align")
        if self.min_labels < 1 or self.min_bucket_count < 1:
            raise ValueError("min_labels and min_bucket_count must be >= 1")

    def scalper_config(self) -> ScalperConfig:
        """Domain exit config with the circuit breaker and internal timeout inert.

        max_hold_slots equals horizon_slots, so the exit ladder's own timeout
        can never fire before the vertical barrier force-close.
        """

        return ScalperConfig(
            position_size_sol=self.position_size_sol,
            tp_levels_pct=self.tp_levels_pct,
            sl_pct=self.sl_pct,
            sell_fractions=self.sell_fractions,
            daily_loss_stop=1,
            max_hold_slots=self.horizon_slots,
        )


@dataclass(frozen=True, slots=True)
class PreEntryFeatures:
    """Gate-observable features from the pre-entry window of one launch.

    Zeroes are neutral defaults for launches with no pre-window prints or no
    valid prices; they are honest observations ("nothing happened"), not gaps.
    """

    mint: str
    n_buys: int
    n_sells: int
    unique_buyers: int
    unique_sellers: int
    buy_volume_lamports: int
    top_buyer_share: float
    dev_buys: int
    same_slot_buys: int
    sniper_buyers: int
    sniper_ratio: float
    first_price_ppm: int
    last_price_ppm: int
    return_ppm: int
    range_multiple: float
    entry_slot_offset: int | None


@dataclass(frozen=True, slots=True)
class LaunchLabel:
    """Executable net-PnL label for one launch (one path, one position)."""

    mint: str
    entry_slot: int
    entry_slot_offset: int
    exit_slot: int
    entry_price_ppm: int
    net_pnl_lamports: int
    pnl_pct: float
    is_win: bool
    exit_reason: str
    tranche_count: int
    peak_multiple: float
    touched_2x: bool
    touched_5x: bool
    touched_10x: bool


@dataclass(frozen=True, slots=True)
class FeatureBucket:
    """Winrate statistics for one tercile of one feature."""

    feature: str
    bucket_index: int
    range_label: str
    count: int
    winrate: float
    wilson_lo: float
    wilson_hi: float
    lift_pp: float
    mean_pnl_sol: float


@dataclass(frozen=True, slots=True)
class PairsLabReport:
    """Complete alpha-extraction report over one dataset and config."""

    config: PairsLabConfig
    coverage: dict[str, int]
    base: dict[str, float]
    exit_reasons: dict[str, int]
    feature_lifts: dict[str, tuple[FeatureBucket, ...]]
    labeled: tuple[tuple[PreEntryFeatures, LaunchLabel], ...]
    insufficient_data: bool
    message: str


def _slot_offset(trade: dict[str, Any], created_slot: int) -> int:
    """Slot distance of a trade from creation (negative impossible in store)."""

    return int(trade.get("slot") or 0) - created_slot


def _trade_sort_key(trade: dict[str, Any]) -> tuple[int, int, int, str]:
    """Deterministic intra-mint trade ordering (slot, tx, event, signature)."""

    return (
        int(trade.get("slot") or 0),
        int(trade.get("tx_index") or 0),
        int(trade.get("event_index") or 0),
        str(trade.get("signature") or ""),
    )


def extract_pre_entry_features(
    *,
    config: PairsLabConfig,
    launch: dict[str, Any],
    trades: Sequence[dict[str, Any]],
) -> PreEntryFeatures:
    """Extract decision-time features from a launch's pre-entry window.

    Pre-window = trades with slot offset in [0, entry_delay_slots); the entry
    window is [entry_delay_slots, entry_delay_slots + entry_window_slots).
    ``trades`` are the mint's rows, sorted ascending by slot (the orchestrator
    guarantees ordering).
    """

    mint = str(launch.get("mint") or "")
    creator = launch.get("creator")
    created_slot = int(launch.get("created_slot") or 0)

    buys: list[dict[str, Any]] = []
    n_sells = 0
    seller_wallets: set[str] = set()
    prices: list[int] = []
    entry_slot_offset: int | None = None
    entry_end = config.entry_delay_slots + config.entry_window_slots

    for trade in trades:
        offset = _slot_offset(trade, created_slot)
        if offset < 0:
            continue
        if offset >= entry_end:
            break
        if offset >= config.entry_delay_slots:
            if entry_slot_offset is None:
                entry_slot_offset = offset
            continue
        side = str(trade.get("side") or "")
        wallet = trade.get("wallet")
        if side == "buy":
            buys.append(trade)
        else:
            n_sells += 1
            if isinstance(wallet, str):
                seller_wallets.add(wallet)
        ppm = _price_ppm_from_trade(trade)
        if ppm > 0:
            prices.append(ppm)

    buy_volume = sum(int(t.get("quote_amount_base_units") or 0) for t in buys)
    buyer_wallets = {t["wallet"] for t in buys if isinstance(t.get("wallet"), str)}
    sniper_wallets = {
        t["wallet"]
        for t in buys
        if isinstance(t.get("wallet"), str)
        and _slot_offset(t, created_slot) < config.sniper_window_slots
    }
    volumes_by_wallet: Counter[str] = Counter()
    for trade in buys:
        wallet = trade.get("wallet")
        if isinstance(wallet, str):
            volumes_by_wallet[wallet] += int(trade.get("quote_amount_base_units") or 0)
    top_buyer_share = (
        max(volumes_by_wallet.values()) / buy_volume if buy_volume else 0.0
    )
    first_ppm = prices[0] if prices else 0
    last_ppm = prices[-1] if prices else 0
    return_ppm = (
        int((last_ppm - first_ppm) / first_ppm * 1_000_000) if first_ppm > 0 else 0
    )
    positive_prices = [p for p in prices if p > 0]
    range_multiple = (
        max(positive_prices) / min(positive_prices) if positive_prices else 1.0
    )
    unique_buyers = len(buyer_wallets)

    return PreEntryFeatures(
        mint=mint,
        n_buys=len(buys),
        n_sells=n_sells,
        unique_buyers=unique_buyers,
        unique_sellers=len(seller_wallets),
        buy_volume_lamports=buy_volume,
        top_buyer_share=top_buyer_share,
        dev_buys=sum(1 for t in buys if t.get("wallet") == creator and creator),
        same_slot_buys=sum(1 for t in buys if _slot_offset(t, created_slot) == 0),
        sniper_buyers=len(sniper_wallets),
        sniper_ratio=(len(sniper_wallets) / unique_buyers if unique_buyers else 0.0),
        first_price_ppm=first_ppm,
        last_price_ppm=last_ppm,
        return_ppm=return_ppm,
        range_multiple=range_multiple,
        entry_slot_offset=entry_slot_offset,
    )


def _execute_sell(
    *,
    price_ppm: int,
    slot: int,
    base_amount: int,
    entry_ppm: int,
    fee_config: FeeConfig,
) -> tuple[int, int]:
    """Sell-quote proceeds for one tranche, mirroring scalper fallbacks.

    Returns (proceeds_lamports, fee_lamports). On abstain or quote error the
    scalper approximation applies (price ratio vs entry, 125 bps fee) so label
    semantics stay identical to ``rug_scalp`` output.
    """

    try:
        quote = executable_sell_quote(
            path=QuotePath.PUMP_BONDING_CURVE,
            reserves=_synthetic_reserves(price_ppm, slot),
            base_input_amount=base_amount,
            fee_config=fee_config,
        )
        if isinstance(quote, AbstainResult):
            proceeds = int(
                base_amount
                * price_ppm
                / max(1, entry_ppm)
                * (1.0 - FALLBACK_FEE_FRACTION)
            )
            fee = int(proceeds * FALLBACK_FEE_FRACTION)
        else:
            proceeds = int(quote.output_amount_base_units)
            fee = int(quote.fee_amount_base_units)
    except Exception:  # noqa: BLE001 - defensive fallback mirrors scalper
        proceeds = int(
            base_amount * price_ppm / max(1, entry_ppm) * (1.0 - FALLBACK_FEE_FRACTION)
        )
        fee = int(proceeds * FALLBACK_FEE_FRACTION)
    return proceeds, fee


def replay_launch_label(
    *,
    config: PairsLabConfig,
    launch: dict[str, Any],
    trades: Sequence[dict[str, Any]],
    fee_config: FeeConfig = DEFAULT_FEE_CONFIG,
) -> LaunchLabel | None:
    """Replay the exit ladder on one launch; None when no entry is possible.

    Entry = first trade print in the entry window; the replay then walks every
    post-entry print within the horizon through ``decide_scalper_exit`` and
    executes fills via the executable quote engine over synthetic reserves
    (same fee model as ``rug_scalp``). Any open remainder force-exits at the
    last observed price when the horizon closes (vertical barrier).
    ``trades`` must be sorted ascending by slot.
    """

    created_slot = int(launch.get("created_slot") or 0)
    entry_index: int | None = None
    entry_end = config.entry_delay_slots + config.entry_window_slots
    for index, trade in enumerate(trades):
        offset = _slot_offset(trade, created_slot)
        if config.entry_delay_slots <= offset < entry_end:
            entry_index = index
            break
        if offset >= entry_end:
            break
    if entry_index is None:
        return None

    entry_trade = trades[entry_index]
    entry_slot = int(entry_trade.get("slot") or 0)
    entry_offset = _slot_offset(entry_trade, created_slot)
    entry_ppm = _price_ppm_from_trade(entry_trade)
    if entry_ppm <= 0:
        return None

    scalper = config.scalper_config()
    position_lamports = int(config.position_size_sol * LAMPORTS_PER_SOL)
    try:
        buy_quote = executable_buy_quote(
            path=QuotePath.PUMP_BONDING_CURVE,
            reserves=_synthetic_reserves(entry_ppm, entry_slot),
            quote_input_amount=position_lamports,
            fee_config=fee_config,
        )
        if isinstance(buy_quote, AbstainResult):
            entry_base = position_lamports
        else:
            entry_base = int(buy_quote.output_amount_base_units)
    except Exception:  # noqa: BLE001 - defensive fallback mirrors scalper
        entry_base = position_lamports

    filled = tuple(False for _ in config.tp_levels_pct)
    sold_base = 0
    realized_quote = 0
    tranche_count = 0
    exit_reason = "hold"
    exit_slot = entry_slot
    peak_ppm = entry_ppm
    last_valid_slot = entry_slot
    last_valid_ppm = entry_ppm
    closed = False

    for trade in trades[entry_index + 1 :]:
        if _slot_offset(trade, created_slot) > config.horizon_slots:
            break
        cur_ppm = _price_ppm_from_trade(trade)
        if cur_ppm <= 0:
            continue
        cur_slot = int(trade.get("slot") or 0)
        last_valid_slot = cur_slot
        last_valid_ppm = cur_ppm
        peak_ppm = max(peak_ppm, cur_ppm)

        signal = decide_scalper_exit(
            config=scalper,
            entry_price_ppm=entry_ppm,
            current_price_ppm=cur_ppm,
            current_slot=cur_slot,
            entry_slot=entry_slot,
            filled=filled,
        )
        if signal.action == "hold":
            continue

        if signal.action == "stop_loss":
            remaining = entry_base - sold_base
            if remaining > 0:
                proceeds, _fee = _execute_sell(
                    price_ppm=cur_ppm,
                    slot=cur_slot,
                    base_amount=remaining,
                    entry_ppm=entry_ppm,
                    fee_config=fee_config,
                )
                realized_quote += proceeds
                sold_base = entry_base
                tranche_count += 1
                exit_slot = cur_slot
            exit_reason = signal.reason
            closed = True
            break

        # take_profit tranche (possibly terminal: fraction None means close all)
        fraction = signal.fraction if signal.fraction is not None else 1.0
        tranche_base = min(max(1, int(entry_base * fraction)), entry_base - sold_base)
        if tranche_base <= 0:
            continue
        proceeds, _fee = _execute_sell(
            price_ppm=cur_ppm,
            slot=cur_slot,
            base_amount=tranche_base,
            entry_ppm=entry_ppm,
            fee_config=fee_config,
        )
        realized_quote += proceeds
        sold_base += tranche_base
        tranche_count += 1
        exit_slot = cur_slot
        exit_reason = signal.reason
        if signal.tranche_index is not None:
            filled = next_filled(filled, signal.tranche_index)
        if all(filled) or sold_base >= entry_base:
            closed = True
            break

    if not closed:
        remaining = entry_base - sold_base
        if remaining > 0:
            proceeds, _fee = _execute_sell(
                price_ppm=last_valid_ppm,
                slot=last_valid_slot,
                base_amount=remaining,
                entry_ppm=entry_ppm,
                fee_config=fee_config,
            )
            realized_quote += proceeds
            sold_base += remaining
            tranche_count += 1
            exit_slot = last_valid_slot
        exit_reason = (
            f"{exit_reason}+horizon_close" if exit_reason != "hold" else "horizon_close"
        )

    net_pnl = realized_quote - position_lamports
    pnl_pct = net_pnl / position_lamports * 100
    peak_multiple = peak_ppm / entry_ppm

    return LaunchLabel(
        mint=str(launch.get("mint") or ""),
        entry_slot=entry_slot,
        entry_slot_offset=entry_offset,
        exit_slot=exit_slot,
        entry_price_ppm=entry_ppm,
        net_pnl_lamports=net_pnl,
        pnl_pct=pnl_pct,
        is_win=net_pnl > 0,
        exit_reason=exit_reason,
        tranche_count=tranche_count,
        peak_multiple=peak_multiple,
        touched_2x=peak_multiple >= PEAK_2X,
        touched_5x=peak_multiple >= PEAK_5X,
        touched_10x=peak_multiple >= PEAK_10X,
    )


def wilson_interval(k: int, n: int, z: float = Z_95) -> tuple[float, float]:
    """Wilson score interval for a binomial proportion, in [0, 1].

    Raises:
        ValueError: When n is not positive (interval is undefined).
    """

    if n <= 0:
        raise ValueError("wilson_interval requires n > 0")
    p = k / n
    z2 = z * z
    denominator = 1.0 + z2 / n
    centre = (p + z2 / (2.0 * n)) / denominator
    spread = (z * ((p * (1.0 - p) / n + z2 / (4.0 * n * n)) ** 0.5)) / denominator
    return (
        max(0.0, centre - spread),
        min(1.0, centre + spread),
    )


def compute_base_rates(labels: Sequence[LaunchLabel]) -> dict[str, float]:
    """Unconditional outcome statistics over labeled launches."""

    if not labels:
        return {
            "labeled": 0.0,
            "winrate": 0.0,
            "winrate_wilson_lo": 0.0,
            "winrate_wilson_hi": 0.0,
            "mean_pnl_sol": 0.0,
            "median_pnl_sol": 0.0,
            "mean_pnl_pct": 0.0,
            "p_peak_ge_1_5x": 0.0,
            "p_peak_ge_2x": 0.0,
            "p_peak_ge_5x": 0.0,
            "p_peak_ge_10x": 0.0,
        }
    count = len(labels)
    wins = sum(1 for label in labels if label.is_win)
    lo, hi = wilson_interval(wins, count)
    pnls_sol = [label.net_pnl_lamports / LAMPORTS_PER_SOL for label in labels]
    peak_counts = {
        tier: sum(1 for label in labels if label.peak_multiple >= tier)
        for tier in PEAK_MULTIPLIER_TIERS
    }
    return {
        "labeled": count,
        "winrate": wins / count,
        "winrate_wilson_lo": lo,
        "winrate_wilson_hi": hi,
        "mean_pnl_sol": statistics.fmean(pnls_sol),
        "median_pnl_sol": statistics.median(pnls_sol),
        "mean_pnl_pct": statistics.fmean([label.pnl_pct for label in labels]),
        "p_peak_ge_1_5x": peak_counts[1.5] / count,
        "p_peak_ge_2x": peak_counts[PEAK_2X] / count,
        "p_peak_ge_5x": peak_counts[PEAK_5X] / count,
        "p_peak_ge_10x": peak_counts[PEAK_10X] / count,
    }


def bucket_lifts(
    labeled: Sequence[tuple[PreEntryFeatures, LaunchLabel]],
    *,
    config: PairsLabConfig,
) -> dict[str, tuple[FeatureBucket, ...]]:
    """Winrate by tercile per feature; buckets below min_bucket_count drop.

    Rank-cut terciles degrade gracefully on tied or discrete features: heavy
    ties collapse into fewer (larger) buckets instead of arbitrary splits.
    """

    if not labeled:
        return {}
    total = len(labeled)
    overall_winrate = sum(1 for _, label in labeled if label.is_win) / total

    result: dict[str, tuple[FeatureBucket, ...]] = {}
    for name in LIFT_FEATURE_NAMES:
        values: list[float] = []
        pairs: list[tuple[float, LaunchLabel]] = []
        for features, label in labeled:
            value = getattr(features, name, None)
            if value is None:
                continue
            numeric = float(value)
            values.append(numeric)
            pairs.append((numeric, label))
        if not values:
            continue
        ordered = sorted(values)
        cut_low = ordered[len(ordered) // 3]
        cut_high = ordered[(2 * len(ordered)) // 3]
        bounds = (
            (float("-inf"), cut_low, f"<= {cut_low:.6g}"),
            (cut_low, cut_high, f"({cut_low:.6g}, {cut_high:.6g}]"),
            (cut_high, float("inf"), f"> {cut_high:.6g}"),
        )
        buckets: list[FeatureBucket] = []
        for bucket_index, (low, high, label_text) in enumerate(bounds, start=1):
            members = [label for value, label in pairs if low < value <= high]
            if len(members) < config.min_bucket_count:
                continue
            bucket_wins = sum(1 for label in members if label.is_win)
            bucket_winrate = bucket_wins / len(members)
            wilson_lo, wilson_hi = wilson_interval(bucket_wins, len(members))
            buckets.append(
                FeatureBucket(
                    feature=name,
                    bucket_index=bucket_index,
                    range_label=label_text,
                    count=len(members),
                    winrate=bucket_winrate,
                    wilson_lo=wilson_lo,
                    wilson_hi=wilson_hi,
                    lift_pp=(bucket_winrate - overall_winrate) * 100.0,
                    mean_pnl_sol=statistics.fmean(
                        [m.net_pnl_lamports / LAMPORTS_PER_SOL for m in members]
                    ),
                )
            )
        if buckets:
            result[name] = tuple(buckets)
    return result


def run_pairs_lab(
    *,
    launches: Sequence[dict[str, Any]],
    trades: Sequence[dict[str, Any]],
    config: PairsLabConfig,
) -> PairsLabReport:
    """Orchestrate feature extraction + label replay + statistics (pure)."""

    trades_by_mint: dict[str, list[dict[str, Any]]] = {}
    for trade in trades:
        trades_by_mint.setdefault(str(trade.get("mint") or ""), []).append(dict(trade))
    for mint_trades in trades_by_mint.values():
        mint_trades.sort(key=_trade_sort_key)

    with_trades = 0
    labeled: list[tuple[PreEntryFeatures, LaunchLabel]] = []
    for launch in launches:
        mint = str(launch.get("mint") or "")
        mint_trades = trades_by_mint.get(mint)
        if not mint_trades:
            continue
        with_trades += 1
        features = extract_pre_entry_features(
            config=config, launch=launch, trades=mint_trades
        )
        label = replay_launch_label(config=config, launch=launch, trades=mint_trades)
        if label is None:
            continue
        labeled.append((features, label))

    labels = [label for _, label in labeled]
    insufficient = len(labeled) < config.min_labels
    message = (
        "insufficient labeled launches for inference (fail-closed)"
        if insufficient
        else "ok"
    )
    return PairsLabReport(
        config=config,
        coverage={
            "launches": len(launches),
            "with_trades": with_trades,
            "entry_able": len(labeled),
            "labeled": len(labeled),
        },
        base=compute_base_rates(labels),
        exit_reasons=dict(Counter(label.exit_reason for label in labels)),
        feature_lifts=bucket_lifts(labeled, config=config),
        labeled=tuple(labeled),
        insufficient_data=insufficient,
        message=message,
    )


def result_to_json(report: PairsLabReport) -> dict[str, Any]:
    """Serializable dict of the full report (stdout output only)."""

    return {
        "config": {
            k: list(v) if isinstance(v, tuple) else v
            for k, v in asdict(report.config).items()
        },
        "coverage": report.coverage,
        "base": {k: round(v, 6) for k, v in report.base.items()},
        "exit_reasons": report.exit_reasons,
        "feature_lifts": {
            name: [
                {
                    "bucket": bucket.bucket_index,
                    "range": bucket.range_label,
                    "count": bucket.count,
                    "winrate": round(bucket.winrate, 4),
                    "wilson_lo": round(bucket.wilson_lo, 4),
                    "wilson_hi": round(bucket.wilson_hi, 4),
                    "lift_pp": round(bucket.lift_pp, 2),
                    "mean_pnl_sol": round(bucket.mean_pnl_sol, 6),
                }
                for bucket in buckets
            ]
            for name, buckets in report.feature_lifts.items()
        },
        "insufficient_data": report.insufficient_data,
        "message": report.message,
        "launches": [
            {
                "mint": label.mint,
                "entry_slot": label.entry_slot,
                "entry_slot_offset": label.entry_slot_offset,
                "exit_slot": label.exit_slot,
                "net_pnl_sol": round(label.net_pnl_lamports / LAMPORTS_PER_SOL, 6),
                "pnl_pct": round(label.pnl_pct, 4),
                "is_win": label.is_win,
                "exit_reason": label.exit_reason,
                "tranches": label.tranche_count,
                "peak_multiple": round(label.peak_multiple, 4),
                "touched_2x": label.touched_2x,
                "touched_5x": label.touched_5x,
                "touched_10x": label.touched_10x,
                "unique_buyers": features.unique_buyers,
                "sniper_ratio": round(features.sniper_ratio, 4),
                "top_buyer_share": round(features.top_buyer_share, 4),
                "dev_buys": features.dev_buys,
                "same_slot_buys": features.same_slot_buys,
                "return_ppm": features.return_ppm,
            }
            for features, label in report.labeled
        ],
    }


def format_human(report: PairsLabReport) -> str:
    """Compact human-readable report for the operator's terminal."""

    lines = [
        "=== PAIRS LAB - newpairs alpha extraction (paper-only replay) ===",
        "coverage: launches={launches} with_trades={with_trades} "
        "labeled={labeled}".format(**report.coverage),
        f"labeled={report.base['labeled']:.0f} "
        f"winrate={report.base['winrate']:.1%} "
        "Wilson95=["
        f"{report.base['winrate_wilson_lo']:.1%}, "
        f"{report.base['winrate_wilson_hi']:.1%}]",
        f"net pnl: mean={report.base['mean_pnl_sol']:+.4f} SOL "
        f"median={report.base['median_pnl_sol']:+.4f} SOL "
        f"(size {report.config.position_size_sol} SOL)",
        f"peak-multiple base rates: "
        f">=1.5x {report.base['p_peak_ge_1_5x']:.1%}  "
        f">=2x {report.base['p_peak_ge_2x']:.1%}  "
        f">=5x {report.base['p_peak_ge_5x']:.1%}  "
        f">=10x {report.base['p_peak_ge_10x']:.1%}",
        "exit reasons: "
        + ", ".join(
            f"{reason}={count}"
            for reason, count in sorted(
                report.exit_reasons.items(), key=lambda item: -item[1]
            )
        ),
    ]
    if report.feature_lifts:
        lines.append(
            "feature lift (winrate by tercile, "
            f"min {report.config.min_bucket_count}/bucket):"
        )
        lines.append(
            "  feature             bucket              n    winrate   "
            "lift(pp)  meanPnL(SOL)"
        )
        for name in LIFT_FEATURE_NAMES:
            for bucket in report.feature_lifts.get(name, ()):
                lines.append(
                    f"  {name:<19} {bucket.range_label[:19]:<19} "
                    f"{bucket.count:<5} {bucket.winrate:.1%}  "
                    f"{bucket.lift_pp:+6.1f}  {bucket.mean_pnl_sol:+.4f}"
                )
    else:
        lines.append("feature lift: no bucket met the minimum count")
    lines.append(
        "note: labels are executable net-PnL replays (fees + curve "
        "slippage); research output, not a live signal"
    )
    if report.insufficient_data:
        lines.append(f"FAIL-CLOSED: {report.message}")
    return "\n".join(lines)
