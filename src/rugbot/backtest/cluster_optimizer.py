"""Cluster-wide multi-token historical backtest and Take-Profit grid optimizer with realistic adverse dump modeling."""

# ruff: noqa: C901, PLR0912, PLR0913, PLR0915

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence

PUMP_SWAP_FEE_PCT = 0.01  # 1% Pump.fun swap fee
DEFAULT_REALIZED_DUMP_LOSS_PCT = 0.75  # Realistic 75% adverse loss on bonding curve rug
BIBLE_MIN_LAUNCH_COUNT = 10
BIBLE_MIN_WINRATE_PCT = 33.0
BIBLE_MAX_FIRST_CANDLE_MC = 15_000.0


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


def run_cluster_tp_grid_search(
    root_funder: str,
    samples: Sequence[HistoricalTokenSample],
    *,
    buy_size_sol: float = 0.025,
    realized_dump_loss_pct: float = DEFAULT_REALIZED_DUMP_LOSS_PCT,  # 75% realistic rug dump loss
    jito_tip_sol: float = 0.001,
    gas_fee_sol: float = 0.0005,
    tp_grid: Sequence[float] = (1.25, 1.50, 1.75, 2.00, 2.25, 2.50, 3.00, 4.00),
) -> ClusterBacktestReport:
    """Evaluate Take-Profit levels across all historical tokens with realistic adverse dump modeling."""
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

    evaluations: list[TpGridEvaluation] = []
    total_tokens = len(samples)
    best_ev = -float("inf")
    best_eval: TpGridEvaluation | None = None
    total_capital_per_trade = buy_size_sol

    for tp in tp_grid:
        wins = 0
        losses = 0
        total_gross_gains = 0.0
        total_gross_losses = 0.0
        total_fees = 0.0
        total_net_pnl = 0.0
        current_drawdown = 0.0
        max_drawdown = 0.0

        for token in samples:
            # Entry fee = Buy size * 1% + Jito + Priority gas
            entry_fee = (buy_size_sol * PUMP_SWAP_FEE_PCT) + jito_tip_sol + gas_fee_sol

            if token.ath_multiplier >= tp:
                # WIN TRADE: Sold at target TP before rug
                gross_gain = buy_size_sol * (tp - 1.0)
                exit_fee = (buy_size_sol * tp * PUMP_SWAP_FEE_PCT) + gas_fee_sol
                trade_fees = entry_fee + exit_fee
                trade_net = gross_gain - trade_fees

                wins += 1
                total_gross_gains += gross_gain
                total_fees += trade_fees
                total_net_pnl += trade_net
            else:
                # REALISTIC ADVERSE LOSS: Dev dumped on bonding curve (e.g. -75% floor loss)
                gross_loss = buy_size_sol * realized_dump_loss_pct
                exit_fee = (
                    buy_size_sol * (1.0 - realized_dump_loss_pct) * PUMP_SWAP_FEE_PCT
                ) + gas_fee_sol
                trade_fees = entry_fee + exit_fee
                trade_net = -(gross_loss + trade_fees)

                losses += 1
                total_gross_losses += gross_loss
                total_fees += trade_fees
                total_net_pnl += trade_net

            if trade_net < 0:
                current_drawdown += abs(trade_net)
                max_drawdown = max(max_drawdown, current_drawdown)
            else:
                current_drawdown = max(0.0, current_drawdown - trade_net)

        winrate = (wins / total_tokens) * 100 if total_tokens > 0 else 0.0
        net_ev = total_net_pnl / total_tokens if total_tokens > 0 else 0.0
        total_invested = total_tokens * total_capital_per_trade
        net_roi = (total_net_pnl / total_invested * 100) if total_invested > 0 else 0.0
        tp_label = f"+{int((tp - 1.0) * 100)}%"

        eval_res = TpGridEvaluation(
            tp_multiplier=tp,
            tp_pct_label=tp_label,
            wins=wins,
            losses=losses,
            total_tokens=total_tokens,
            winrate_pct=winrate,
            gross_gains_sol=round(total_gross_gains, 5),
            gross_losses_sol=round(total_gross_losses, 5),
            total_fees_paid_sol=round(total_fees, 5),
            total_net_pnl_sol=round(total_net_pnl, 5),
            net_ev_sol_per_trade=round(net_ev, 5),
            net_roi_pct=round(net_roi, 1),
            max_drawdown_sol=round(max_drawdown, 5),
            is_optimal=False,
        )
        evaluations.append(eval_res)

        if net_ev > best_ev:
            best_ev = net_ev
            best_eval = eval_res

    final_evals: list[TpGridEvaluation] = []
    for ev in evaluations:
        if best_eval and ev.tp_multiplier == best_eval.tp_multiplier and best_ev > 0:
            final_evals.append(
                TpGridEvaluation(
                    tp_multiplier=ev.tp_multiplier,
                    tp_pct_label=ev.tp_pct_label,
                    wins=ev.wins,
                    losses=ev.losses,
                    total_tokens=ev.total_tokens,
                    winrate_pct=ev.winrate_pct,
                    gross_gains_sol=ev.gross_gains_sol,
                    gross_losses_sol=ev.gross_losses_sol,
                    total_fees_paid_sol=ev.total_fees_paid_sol,
                    total_net_pnl_sol=ev.total_net_pnl_sol,
                    net_ev_sol_per_trade=ev.net_ev_sol_per_trade,
                    net_roi_pct=ev.net_roi_pct,
                    max_drawdown_sol=ev.max_drawdown_sol,
                    is_optimal=True,
                )
            )
        else:
            final_evals.append(ev)

    is_profitable = best_ev > 0.0
    opt_label = (
        best_eval.tp_pct_label if (best_eval and is_profitable) else "UNPROFITABLE"
    )
    opt_mult = best_eval.tp_multiplier if (best_eval and is_profitable) else None
    opt_roi = best_eval.net_roi_pct if (best_eval and is_profitable) else 0.0

    # Memecoin Bible Qualification Checks:
    # 1. Total tokens >= 10
    # 2. Optimal Winrate >= 33%
    # 3. Average Entry MC <= $15k
    avg_entry_mc = (
        sum(s.entry_mc_usd for s in samples) / total_tokens if total_tokens > 0 else 0.0
    )
    opt_winrate = best_eval.winrate_pct if best_eval else 0.0

    bible_passed = (
        total_tokens >= BIBLE_MIN_LAUNCH_COUNT
        and opt_winrate >= BIBLE_MIN_WINRATE_PCT
        and avg_entry_mc <= BIBLE_MAX_FIRST_CANDLE_MC
        and is_profitable
    )

    reasons: list[str] = []
    if total_tokens < BIBLE_MIN_LAUNCH_COUNT:
        reasons.append(
            f"Sample size {total_tokens} < {BIBLE_MIN_LAUNCH_COUNT} tokens (monitoring)"
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

    summary = (
        f"Cluster {root_funder[:8]}... ({total_tokens} tokens across {len({s.creator_wallet for s in samples})} wallets): "
        f"Optimal TP is {opt_label} (Winrate: {opt_winrate:.1f}%, Net EV: {best_ev:+.5f} SOL/trade, Realized Dump: -{realized_dump_loss_pct * 100:.0f}%)"
        if (best_eval and is_profitable)
        else f"Cluster {root_funder[:8]}... with {total_tokens} tokens: UNPROFITABLE under realistic -{realized_dump_loss_pct * 100:.0f}% dump slippage."
    )

    return ClusterBacktestReport(
        root_funder=root_funder,
        cluster_wallets_count=len({s.creator_wallet for s in samples}),
        total_tokens_evaluated=total_tokens,
        buy_size_sol=buy_size_sol,
        realized_dump_loss_pct=realized_dump_loss_pct,
        jito_tip_sol=jito_tip_sol,
        gas_fee_sol=gas_fee_sol,
        dex_fee_pct=PUMP_SWAP_FEE_PCT * 100,
        samples=tuple(samples),
        evaluations=tuple(final_evals),
        optimal_tp_multiplier=opt_mult,
        optimal_tp_label=opt_label,
        optimal_net_ev_sol=round(best_ev, 5) if is_profitable else 0.0,
        optimal_roi_pct=opt_roi,
        is_net_profitable=is_profitable,
        is_bible_qualified=bible_passed,
        qualification_reason=qual_reason,
        summary_message=summary,
    )
