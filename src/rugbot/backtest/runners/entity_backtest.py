"""Entity-level statistical distribution engine and dynamic policy optimizer."""

from __future__ import annotations

import statistics
from dataclasses import dataclass

from rugbot.domain.entities import OperatorStats

TARGET_2X_MULTIPLIER: float = 2.0
TARGET_3X_MULTIPLIER: float = 3.0
TARGET_MODERATE_ATH: float = 1.5
MIN_WINRATE_SAMPLE_THRESHOLD: float = 50.0
TRAILING_EXIT_THRESHOLD: float = 1.2
TRAILING_EXIT_MULTIPLIER: float = 1.15
DEFAULT_TP_PCT: float = 50.0
DEFAULT_SL_PCT: float = 30.0
MIN_HOLD_SECONDS: float = 20.0
HOLD_BUFFER_RATIO: float = 0.85


@dataclass(frozen=True, slots=True)
class LaunchMetric:
    """Core empirical metrics measured for a single token launch."""

    mint: str
    symbol: str
    ath_multiplier: float
    seconds_to_ath: float
    seconds_to_first_entity_sell: float
    seconds_to_dump: float
    max_drawdown: float
    creator_wallet: str = ""
    launch_timestamp: int = 0


@dataclass(frozen=True, slots=True)
class EntityBacktestResult:
    """Full entity-level statistical backtest evaluation."""

    entity_id: str
    stats: OperatorStats
    launches: tuple[LaunchMetric, ...]
    net_profit_sol: float
    net_roi_pct: float
    winrate_pct: float
    optimal_tp_pct: float
    optimal_sl_pct: float
    optimal_max_hold_s: float


def compute_operator_stats(
    launches: list[LaunchMetric] | tuple[LaunchMetric, ...],
) -> OperatorStats:
    """Compute statistical distributions (P(2x), P(3x), medians, drawdown) across launches."""
    if not launches:
        return OperatorStats()

    aths = [m.ath_multiplier for m in launches]
    times_to_ath = [m.seconds_to_ath for m in launches]
    times_to_sell = [m.seconds_to_first_entity_sell for m in launches]
    times_to_dump = [m.seconds_to_dump for m in launches]
    drawdowns = [m.max_drawdown for m in launches]

    n = len(launches)
    p_2x = (sum(1 for a in aths if a >= TARGET_2X_MULTIPLIER) / n) * 100.0
    p_3x = (sum(1 for a in aths if a >= TARGET_3X_MULTIPLIER) / n) * 100.0

    med_ath = float(statistics.median(aths))
    med_time_ath = float(statistics.median(times_to_ath))
    med_time_sell = float(statistics.median(times_to_sell))
    med_time_dump = float(statistics.median(times_to_dump))
    max_dd = max(drawdowns) * 100.0

    # Optimize dynamic take-profit and maximum holding time based on distributions
    if p_2x >= MIN_WINRATE_SAMPLE_THRESHOLD:
        recommended_tp = min((med_ath - 1.0) * 100.0, 100.0)
    elif med_ath >= TARGET_MODERATE_ATH:
        recommended_tp = DEFAULT_TP_PCT
    else:
        recommended_tp = DEFAULT_SL_PCT

    # Max hold time should exit safely before operator dumps
    recommended_hold = max(MIN_HOLD_SECONDS, med_time_sell * HOLD_BUFFER_RATIO)

    return OperatorStats(
        sample_size=n,
        prob_2x_pct=p_2x,
        prob_3x_pct=p_3x,
        median_ath=med_ath,
        median_time_to_ath_s=med_time_ath,
        median_time_to_first_sell_s=med_time_sell,
        median_time_to_dump_s=med_time_dump,
        max_drawdown_pct=max_dd,
        recommended_tp_pct=recommended_tp,
        recommended_sl_pct=DEFAULT_SL_PCT,
        recommended_max_hold_s=recommended_hold,
    )


def backtest_operator_entity(  # noqa: PLR0913
    entity_id: str,
    launches: list[LaunchMetric] | tuple[LaunchMetric, ...],
    quote_size_sol: float = 0.50,
    slippage_pct: float = 1.5,
    gas_fee_sol: float = 0.0010,
    pump_fee_pct: float = 1.0,
) -> EntityBacktestResult:
    """Execute realistic backtest across entity launches using statistical distribution optimization."""
    if not launches:
        raise ValueError("No launch metrics provided for backtest evaluation")  # noqa: TRY003
    launch_list = tuple(launches)
    stats = compute_operator_stats(launch_list)

    total_pnl = 0.0
    wins = 0
    total_cost = 0.0

    tp_target = 1.0 + (stats.recommended_tp_pct / 100.0)
    sl_target = 1.0 - (stats.recommended_sl_pct / 100.0)

    for m in launch_list:
        capital = quote_size_sol
        total_cost += capital

        # Realistic entry
        entry_eff = (
            capital * (1.0 - (slippage_pct / 100.0)) * (1.0 - (pump_fee_pct / 100.0))
            - gas_fee_sol
        )

        if (
            m.ath_multiplier >= tp_target
            and m.seconds_to_ath <= stats.recommended_max_hold_s
        ):
            # TP reached
            exit_eff = (
                entry_eff
                * tp_target
                * (1.0 - (slippage_pct / 100.0))
                * (1.0 - (pump_fee_pct / 100.0))
                - gas_fee_sol
            )
            net_trade = exit_eff - capital
            wins += 1
        elif (
            m.ath_multiplier >= TRAILING_EXIT_THRESHOLD
            and m.seconds_to_first_entity_sell >= stats.recommended_max_hold_s
        ):
            # Trailing exit before dump
            exit_eff = (
                entry_eff
                * TRAILING_EXIT_MULTIPLIER
                * (1.0 - (slippage_pct / 100.0))
                * (1.0 - (pump_fee_pct / 100.0))
                - gas_fee_sol
            )
            net_trade = exit_eff - capital
            if net_trade > 0:
                wins += 1
        else:
            # Adverse stop-loss / dump
            exit_eff = (
                entry_eff
                * sl_target
                * (1.0 - (slippage_pct / 100.0))
                * (1.0 - (pump_fee_pct / 100.0))
                - gas_fee_sol
            )
            net_trade = exit_eff - capital

        total_pnl += net_trade

    sample = len(launch_list)
    winrate = (wins / sample * 100.0) if sample > 0 else 0.0
    roi = (total_pnl / total_cost * 100.0) if total_cost > 0 else 0.0

    return EntityBacktestResult(
        entity_id=entity_id,
        stats=stats,
        launches=launch_list,
        net_profit_sol=total_pnl,
        net_roi_pct=roi,
        winrate_pct=winrate,
        optimal_tp_pct=stats.recommended_tp_pct,
        optimal_sl_pct=stats.recommended_sl_pct,
        optimal_max_hold_s=stats.recommended_max_hold_s,
    )


__all__ = [
    "EntityBacktestResult",
    "LaunchMetric",
    "backtest_operator_entity",
    "compute_operator_stats",
]
