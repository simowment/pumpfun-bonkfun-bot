"""Cluster-wide multi-token historical backtest and Take-Profit optimizer.

The optimal TP is computed analytically by evaluating EV at each unique ATH
breakpoint in the sample data — not via an arbitrary discrete grid.

Proof of optimality: EV(tp) is piecewise constant in wins between consecutive
ATH values. The global maximum therefore lies exactly at one of the N observed
ATH multipliers, making an exhaustive grid search both unnecessary and lossy.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    from collections.abc import Sequence

PUMP_SWAP_FEE_PCT = 0.01  # 1% Pump.fun swap fee
DEFAULT_REALIZED_DUMP_LOSS_PCT = 0.75  # Realistic 75% adverse loss on bonding curve rug
BIBLE_MIN_LAUNCH_COUNT = 10
BIBLE_MIN_WINRATE_PCT = 33.0
BIBLE_MAX_FIRST_CANDLE_MC = 15_000.0
MIN_SAMPLE_COUNT_FOR_STD: Final[int] = 2


@dataclass(frozen=True, slots=True)
class HistoricalTokenSample:
    """Historical token trajectory evidence across any wallet in the cluster."""

    mint: str
    symbol: str
    creator_wallet: str
    created_slot: int
    created_at: int
    ath_multiplier: float  # e.g. 2.45 = +145%
    ath_delay_seconds: int  # Seconds to reach ATH
    rug_delay_seconds: int | None  # Seconds before dev dumped / liquidity pulled
    entry_mc_usd: float
    peak_mc_usd: float
    is_bundle_b0: bool = False
    bundle_sol: float = 0.0


@dataclass(frozen=True, slots=True)
class TpGridEvaluation:
    """Evaluation result for one specific Take-Profit threshold across all cluster tokens."""

    tp_multiplier: float  # e.g. 1.75 = +75% TP
    tp_pct_label: str  # e.g. "+75%"
    wins: int
    losses: int
    total_tokens: int
    winrate_pct: float
    gross_gains_sol: float
    gross_losses_sol: float
    total_fees_paid_sol: float
    total_net_pnl_sol: float
    net_ev_sol_per_trade: float
    net_roi_pct: float
    max_drawdown_sol: float
    is_optimal: bool


@dataclass(frozen=True, slots=True)
class ClusterBacktestReport:
    """Complete cluster multi-token backtest and optimization report."""

    root_funder: str
    cluster_wallets_count: int
    total_tokens_evaluated: int
    buy_size_sol: float
    realized_dump_loss_pct: float
    jito_tip_sol: float
    gas_fee_sol: float
    dex_fee_pct: float
    samples: tuple[HistoricalTokenSample, ...]
    evaluations: tuple[TpGridEvaluation, ...]
    optimal_tp_multiplier: float | None
    optimal_tp_label: str
    optimal_net_ev_sol: float
    optimal_roi_pct: float
    is_net_profitable: bool
    is_bible_qualified: bool
    qualification_reason: str
    summary_message: str
    avg_ath_multiplier: float = 1.0
    median_ath_multiplier: float = 1.0
    ath_std_dev: float = 0.0
    ath_consistency_pct: float = 0.0
    avg_peak_mc_usd: float = 0.0
    avg_rug_delay_seconds: float = 0.0
    median_rug_delay_seconds: float = 0.0
    rug_delay_std_seconds: float = 0.0
    avg_rug_mc_usd: float = 0.0
    avg_ath_delay_seconds: float = 0.0
    avg_inter_launch_minutes: float = 0.0
    min_inter_launch_minutes: float = 0.0


def _eval_tp(  # noqa: PLR0913
    tp: float,
    samples: Sequence[HistoricalTokenSample],
    *,
    buy_size_sol: float,
    realized_dump_loss_pct: float,
    jito_tip_sol: float,
    gas_fee_sol: float,
    is_optimal: bool = False,
) -> TpGridEvaluation:
    """Compute all metrics for a single TP multiplier across the full sample set."""
    entry_fee = buy_size_sol * PUMP_SWAP_FEE_PCT + jito_tip_sol + gas_fee_sol
    wins = losses = 0
    gross_gains = gross_losses = total_fees = total_net_pnl = 0.0
    current_drawdown = max_drawdown = 0.0

    for token in samples:
        if token.ath_multiplier >= tp:
            gross_gain = buy_size_sol * (tp - 1.0)
            exit_fee = buy_size_sol * tp * PUMP_SWAP_FEE_PCT + gas_fee_sol
            trade_net = gross_gain - entry_fee - exit_fee
            wins += 1
            gross_gains += gross_gain
            total_fees += entry_fee + exit_fee
            total_net_pnl += trade_net
        else:
            gross_loss = buy_size_sol * realized_dump_loss_pct
            exit_fee = (
                buy_size_sol * (1.0 - realized_dump_loss_pct) * PUMP_SWAP_FEE_PCT
                + gas_fee_sol
            )
            trade_net = -(gross_loss + entry_fee + exit_fee)
            losses += 1
            gross_losses += gross_loss
            total_fees += entry_fee + exit_fee
            total_net_pnl += trade_net

        if trade_net < 0:
            current_drawdown += abs(trade_net)
            max_drawdown = max(max_drawdown, current_drawdown)
        else:
            current_drawdown = max(0.0, current_drawdown - trade_net)

    n = len(samples)
    winrate = (wins / n) * 100 if n > 0 else 0.0
    net_ev = total_net_pnl / n if n > 0 else 0.0
    total_invested = n * buy_size_sol
    net_roi = (total_net_pnl / total_invested * 100) if total_invested > 0 else 0.0
    tp_label = f"+{round((tp - 1.0) * 100):.0f}%"

    return TpGridEvaluation(
        tp_multiplier=tp,
        tp_pct_label=tp_label,
        wins=wins,
        losses=losses,
        total_tokens=n,
        winrate_pct=winrate,
        gross_gains_sol=round(gross_gains, 5),
        gross_losses_sol=round(gross_losses, 5),
        total_fees_paid_sol=round(total_fees, 5),
        total_net_pnl_sol=round(total_net_pnl, 5),
        net_ev_sol_per_trade=round(net_ev, 5),
        net_roi_pct=round(net_roi, 1),
        max_drawdown_sol=round(max_drawdown, 5),
        is_optimal=is_optimal,
    )


def run_cluster_tp_grid_search(  # noqa: PLR0913
    root_funder: str,
    samples: Sequence[HistoricalTokenSample],
    *,
    buy_size_sol: float = 0.025,
    realized_dump_loss_pct: float = DEFAULT_REALIZED_DUMP_LOSS_PCT,
    jito_tip_sol: float = 0.001,
    gas_fee_sol: float = 0.0005,
    tp_grid: Sequence[float] | None = None,
) -> ClusterBacktestReport:
    """Find the analytically optimal TP and produce a full display table.

    The optimal TP is found by evaluating EV at each unique ATH multiplier
    in the sample — the exact breakpoints where the win count changes. This
    is provably optimal (O(N log N)) and requires no fixed grid.

    An optional display-only tp_grid can be merged into the table rows, but
    the reported optimal always comes from the ATH-derived candidates.
    """
    if not samples:
        return ClusterBacktestReport(
            root_funder=root_funder,
            cluster_wallets_count=1,
            total_tokens_evaluated=0,
            buy_size_sol=buy_size_sol,
            realized_dump_loss_pct=realized_dump_loss_pct,
            jito_tip_sol=jito_tip_sol,
            gas_fee_sol=gas_fee_sol,
            dex_fee_pct=PUMP_SWAP_FEE_PCT * 100,
            samples=(),
            evaluations=(),
            optimal_tp_multiplier=None,
            optimal_tp_label="N/A",
            optimal_net_ev_sol=0.0,
            optimal_roi_pct=0.0,
            is_net_profitable=False,
            is_bible_qualified=False,
            qualification_reason="No historical tokens found.",
            summary_message="No historical tokens found for this cluster.",
        )

    eval_kwargs = {
        "buy_size_sol": buy_size_sol,
        "realized_dump_loss_pct": realized_dump_loss_pct,
        "jito_tip_sol": jito_tip_sol,
        "gas_fee_sol": gas_fee_sol,
    }

    # Step 1: Find the exact optimal TP at ATH breakpoints
    ath_candidates = sorted({s.ath_multiplier for s in samples})
    best_ev = -float("inf")
    best_tp = ath_candidates[0]
    for tp in ath_candidates:
        ev = _eval_tp(tp, samples, **eval_kwargs).net_ev_sol_per_trade
        if ev > best_ev:
            best_ev = ev
            best_tp = tp

    # Step 2: Build display table — ATH breakpoints merged with optional display grid
    display_tps = sorted(set(ath_candidates) | set(tp_grid or []))
    evaluations = [
        _eval_tp(tp, samples, is_optimal=(tp == best_tp and best_ev > 0), **eval_kwargs)
        for tp in display_tps
    ]

    best_eval = next(e for e in evaluations if e.tp_multiplier == best_tp)
    is_profitable = best_ev > 0.0
    opt_label = best_eval.tp_pct_label if is_profitable else "UNPROFITABLE"
    opt_mult = best_eval.tp_multiplier if is_profitable else None
    opt_roi = best_eval.net_roi_pct if is_profitable else 0.0
    opt_winrate = best_eval.winrate_pct

    # Memecoin Bible qualification
    avg_entry_mc = sum(s.entry_mc_usd for s in samples) / len(samples)
    bible_passed = (
        len(samples) >= BIBLE_MIN_LAUNCH_COUNT
        and opt_winrate >= BIBLE_MIN_WINRATE_PCT
        and avg_entry_mc <= BIBLE_MAX_FIRST_CANDLE_MC
        and is_profitable
    )

    reasons: list[str] = []
    if len(samples) < BIBLE_MIN_LAUNCH_COUNT:
        reasons.append(
            f"Sample size {len(samples)} < {BIBLE_MIN_LAUNCH_COUNT} tokens (monitoring)"
        )
    if opt_winrate < BIBLE_MIN_WINRATE_PCT:
        reasons.append(
            f"Winrate {opt_winrate:.1f}% < {BIBLE_MIN_WINRATE_PCT:.0f}% min threshold"
        )
    if avg_entry_mc > BIBLE_MAX_FIRST_CANDLE_MC:
        reasons.append(
            f"Avg Entry MC ${avg_entry_mc:,.0f} > ${BIBLE_MAX_FIRST_CANDLE_MC:,.0f} cap"
        )
    if not is_profitable:
        reasons.append("Net Expected Value is negative after adverse dump slippage")

    qual_reason = (
        " · ".join(reasons)
        if reasons
        else "BIBLE QUALIFIED: Meets all sample size, winrate & MC thresholds"
    )

    unique_wallets = len({s.creator_wallet for s in samples})
    summary = (
        f"Cluster {root_funder[:8]}... ({len(samples)} tokens across {unique_wallets} wallets): "
        f"Optimal TP is {opt_label} (Winrate: {opt_winrate:.1f}%, "
        f"Net EV: {best_ev:+.5f} SOL/trade, Realized Dump: -{realized_dump_loss_pct * 100:.0f}%)"
        if is_profitable
        else (
            f"Cluster {root_funder[:8]}... with {len(samples)} tokens: "
            f"UNPROFITABLE under realistic -{realized_dump_loss_pct * 100:.0f}% dump slippage."
        )
    )

    # Operator Timing & Consistency Statistics
    aths = [s.ath_multiplier for s in samples]
    avg_ath = float(statistics.mean(aths))
    median_ath = float(statistics.median(aths))
    ath_std = (
        float(statistics.stdev(aths)) if len(aths) >= MIN_SAMPLE_COUNT_FOR_STD else 0.0
    )
    ath_consistency = (
        max(0.0, min(100.0, (1.0 - (ath_std / avg_ath)) * 100.0))
        if avg_ath > 0
        else 100.0
    )

    peak_mcs = [s.peak_mc_usd for s in samples]
    avg_peak_mc = float(statistics.mean(peak_mcs)) if peak_mcs else 0.0

    rug_delays = [
        float(s.rug_delay_seconds) for s in samples if s.rug_delay_seconds is not None
    ]
    avg_rug_delay = float(statistics.mean(rug_delays)) if rug_delays else 0.0
    median_rug_delay = float(statistics.median(rug_delays)) if rug_delays else 0.0
    rug_delay_std = (
        float(statistics.stdev(rug_delays))
        if len(rug_delays) >= MIN_SAMPLE_COUNT_FOR_STD
        else 0.0
    )

    avg_rug_mc = (
        float(
            statistics.mean(
                [s.peak_mc_usd * (1.0 - realized_dump_loss_pct) for s in samples]
            )
        )
        if samples
        else 0.0
    )

    ath_delays = [
        float(s.ath_delay_seconds) for s in samples if s.ath_delay_seconds is not None
    ]
    avg_ath_delay = float(statistics.mean(ath_delays)) if ath_delays else 0.0

    # Inter-launch cadence calculation
    sorted_creation_times = sorted(
        [float(s.created_at) for s in samples if s.created_at > 0]
    )
    intervals_minutes = [
        (sorted_creation_times[i] - sorted_creation_times[i - 1]) / 60.0
        for i in range(1, len(sorted_creation_times))
        if sorted_creation_times[i] > sorted_creation_times[i - 1]
    ]
    avg_interval = (
        float(statistics.mean(intervals_minutes)) if intervals_minutes else 0.0
    )
    min_interval = float(min(intervals_minutes)) if intervals_minutes else 0.0

    return ClusterBacktestReport(
        root_funder=root_funder,
        cluster_wallets_count=unique_wallets,
        total_tokens_evaluated=len(samples),
        buy_size_sol=buy_size_sol,
        realized_dump_loss_pct=realized_dump_loss_pct,
        jito_tip_sol=jito_tip_sol,
        gas_fee_sol=gas_fee_sol,
        dex_fee_pct=PUMP_SWAP_FEE_PCT * 100,
        samples=tuple(samples),
        evaluations=tuple(evaluations),
        optimal_tp_multiplier=opt_mult,
        optimal_tp_label=opt_label,
        optimal_net_ev_sol=round(best_ev, 5) if is_profitable else 0.0,
        optimal_roi_pct=opt_roi,
        is_net_profitable=is_profitable,
        is_bible_qualified=bible_passed,
        qualification_reason=qual_reason,
        summary_message=summary,
        avg_ath_multiplier=round(avg_ath, 2),
        median_ath_multiplier=round(median_ath, 2),
        ath_std_dev=round(ath_std, 2),
        ath_consistency_pct=round(ath_consistency, 1),
        avg_peak_mc_usd=round(avg_peak_mc, 1),
        avg_rug_delay_seconds=round(avg_rug_delay, 1),
        median_rug_delay_seconds=round(median_rug_delay, 1),
        rug_delay_std_seconds=round(rug_delay_std, 1),
        avg_rug_mc_usd=round(avg_rug_mc, 1),
        avg_ath_delay_seconds=round(avg_ath_delay, 1),
        avg_inter_launch_minutes=round(avg_interval, 1),
        min_inter_launch_minutes=round(min_interval, 1),
    )
