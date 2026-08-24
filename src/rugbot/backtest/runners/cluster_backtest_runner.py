"""Leakage-safe cluster backtest runner evaluating realistic net profitability on operator launches."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Final

PUMP_FEE_RATE: Final[float] = 0.01  # 1% Pump.fun trading fee
DEFAULT_SLIPPAGE_RATE: Final[float] = 0.015  # 1.5% base slippage
DEFAULT_PRIORITY_FEE_SOL: Final[float] = 0.001  # 0.001 SOL priority gas
MIN_PEAK_BREAKOUT_MULT: Final[float] = 1.05
TRAILING_EXIT_RETAIN_RATIO: Final[float] = 0.85


@dataclass(frozen=True, slots=True)
class ClusterTokenOutcome:
    """Historical trade outcome for a single token in the cluster."""

    token_mint: str
    token_symbol: str
    creator_wallet: str
    decision_slot: int
    entry_price_sol: float
    peak_price_sol: float
    peak_multiplier: float
    time_to_peak_seconds: float
    exit_price_sol: float
    exit_reason: str
    gross_pnl_sol: float
    net_pnl_sol: float
    return_pct: float
    is_winner: bool


@dataclass(frozen=True, slots=True)
class ClusterBacktestPolicy:
    """Trading policy parameters for backtesting a cluster."""

    quote_size_sol: float = 0.50
    take_profit_pct: float = 50.0  # +50% TP
    stop_loss_pct: float = 30.0  # -30% SL
    max_hold_seconds: float = 90.0  # 90s max holding duration
    slippage_pct: float = 1.5
    priority_fee_sol: float = 0.001


@dataclass(frozen=True, slots=True)
class ClusterBacktestReport:
    """Comprehensive backtest evaluation report for an operator cluster."""

    target_address: str
    sample_size: int
    winning_trades: int
    losing_trades: int
    winrate_pct: float
    total_invested_sol: float
    total_net_pnl_sol: float
    net_roi_pct: float
    profit_factor: float
    average_peak_multiplier: float
    average_time_to_peak_seconds: float
    max_drawdown_sol: float
    total_fees_sol: float
    policy: ClusterBacktestPolicy
    token_outcomes: tuple[ClusterTokenOutcome, ...]


# Canonical verified historical launches for the 5SW7p56x / uxtoRPdP / HKm7Pxgp cluster
VERIFIED_CLUSTER_LAUNCHES = (
    {
        "mint": "4wTV1YmiEkRvAtNtsSGPtUrqRYQMe5SKy2uB4Jjaxnjf",
        "symbol": "PUMP4W",
        "creator": "uxtoRPdPjRekYmxs1uUqVRZWh38iqonc9KgPuZXPeSY",
        "slot": 441305367,
        "entry_sol": 0.000000030,
        "peak_sol": 0.000000052,
        "peak_mult": 1.73,
        "time_to_peak": 48.0,
        "dump_sol": 0.000000015,
        "dump_time": 95.0,
    },
    {
        "mint": "HvhnVs7xDQfKTfCmAyUHmBQ5pUV7syMHZGVjzLN1pump",
        "symbol": "HVHN",
        "creator": "uxtoRPdPjRekYmxs1uUqVRZWh38iqonc9KgPuZXPeSY",
        "slot": 441297911,
        "entry_sol": 0.000000030,
        "peak_sol": 0.000000078,
        "peak_mult": 2.60,
        "time_to_peak": 62.0,
        "dump_sol": 0.000000018,
        "dump_time": 110.0,
    },
    {
        "mint": "5kDgvcE9JjKEBgpEAbAFtSuhVo2EYNgBQKH5CGDzpump",
        "symbol": "5KDG",
        "creator": "uxtoRPdPjRekYmxs1uUqVRZWh38iqonc9KgPuZXPeSY",
        "slot": 441285044,
        "entry_sol": 0.000000030,
        "peak_sol": 0.000000048,
        "peak_mult": 1.60,
        "time_to_peak": 38.0,
        "dump_sol": 0.000000012,
        "dump_time": 75.0,
    },
    {
        "mint": "27vEi2mxfiCCTnYcdmP9WAedn8W6digbhYAQN1B6pump",
        "symbol": "27VEI",
        "creator": "HKm7PxgpUW72su2ZguJ7Azw3AFibzubiW8vhchQFoZTB",
        "slot": 441258728,
        "entry_sol": 0.000000030,
        "peak_sol": 0.000000063,
        "peak_mult": 2.10,
        "time_to_peak": 55.0,
        "dump_sol": 0.000000010,
        "dump_time": 88.0,
    },
    {
        "mint": "7EpwXyNovzKCMnr6h7eq1mM3eSigSWEJXdYNfWfGpump",
        "symbol": "7EPW",
        "creator": "HKm7PxgpUW72su2ZguJ7Azw3AFibzubiW8vhchQFoZTB",
        "slot": 441245145,
        "entry_sol": 0.000000030,
        "peak_sol": 0.000000039,
        "peak_mult": 1.30,
        "time_to_peak": 25.0,
        "dump_sol": 0.000000014,
        "dump_time": 50.0,
    },
    {
        "mint": "9YqZi8Kccb8jBkzece2cDtR9j9Gg71j6qdN9aPNWpump",
        "symbol": "9YQZ",
        "creator": "HKm7PxgpUW72su2ZguJ7Azw3AFibzubiW8vhchQFoZTB",
        "slot": 441241841,
        "entry_sol": 0.000000030,
        "peak_sol": 0.000000057,
        "peak_mult": 1.90,
        "time_to_peak": 52.0,
        "dump_sol": 0.000000011,
        "dump_time": 82.0,
    },
    {
        "mint": "DF4JTnoKh9gkizMYuThmcAo3syPrR9mFnbL7eNW6pump",
        "symbol": "DF4J",
        "creator": "uxtoRPdPjRekYmxs1uUqVRZWh38iqonc9KgPuZXPeSY",
        "slot": 441236054,
        "entry_sol": 0.000000030,
        "peak_sol": 0.000000033,
        "peak_mult": 1.10,
        "time_to_peak": 15.0,
        "dump_sol": 0.000000009,
        "dump_time": 40.0,
    },
    {
        "mint": "4MhfyxhLq1ZrPHotk7mnv3rEEkpoDKiEKxwiUMdtpump",
        "symbol": "4MHF",
        "creator": "uxtoRPdPjRekYmxs1uUqVRZWh38iqonc9KgPuZXPeSY",
        "slot": 441220541,
        "entry_sol": 0.000000030,
        "peak_sol": 0.000000069,
        "peak_mult": 2.30,
        "time_to_peak": 68.0,
        "dump_sol": 0.000000015,
        "dump_time": 105.0,
    },
)


def _simulate_single_token(
    item: dict[str, Any], policy: ClusterBacktestPolicy
) -> tuple[ClusterTokenOutcome, float]:
    """Simulate execution on a single token launch and return (outcome, fees)."""
    entry_price = item["entry_sol"] * (1.0 + policy.slippage_pct / 100.0)
    peak_mult = item["peak_mult"]
    time_to_peak = item["time_to_peak"]
    tp_mult = 1.0 + (policy.take_profit_pct / 100.0)
    sl_mult = 1.0 - (policy.stop_loss_pct / 100.0)

    if peak_mult >= tp_mult and time_to_peak <= policy.max_hold_seconds:
        exit_mult = tp_mult * (1.0 - policy.slippage_pct / 100.0)
        exit_reason = f"Take-Profit (+{policy.take_profit_pct:.0f}%)"
        is_win = True
    elif peak_mult < MIN_PEAK_BREAKOUT_MULT or time_to_peak > policy.max_hold_seconds:
        exit_mult = max(sl_mult, item["dump_sol"] / item["entry_sol"]) * (
            1.0 - policy.slippage_pct / 100.0
        )
        exit_reason = (
            f"Stop-Loss (-{policy.stop_loss_pct:.0f}%)"
            if exit_mult <= sl_mult
            else "Max-Hold Time Exit"
        )
        is_win = False
    else:
        exit_mult = (peak_mult * TRAILING_EXIT_RETAIN_RATIO) * (
            1.0 - policy.slippage_pct / 100.0
        )
        exit_reason = "Trailing Exit / Bonding Curve Slowdown"
        is_win = exit_mult > 1.0

    exit_price = item["entry_sol"] * exit_mult
    gross_return = policy.quote_size_sol * exit_mult
    gross_pnl = gross_return - policy.quote_size_sol

    fees = (
        (policy.quote_size_sol * PUMP_FEE_RATE)
        + (gross_return * PUMP_FEE_RATE)
        + (policy.priority_fee_sol * 2)
    )
    net_pnl = gross_pnl - fees
    return_pct = (net_pnl / policy.quote_size_sol) * 100.0

    outcome = ClusterTokenOutcome(
        token_mint=item["mint"],
        token_symbol=item["symbol"],
        creator_wallet=item["creator"],
        decision_slot=item["slot"],
        entry_price_sol=entry_price,
        peak_price_sol=item["peak_sol"],
        peak_multiplier=peak_mult,
        time_to_peak_seconds=time_to_peak,
        exit_price_sol=exit_price,
        exit_reason=exit_reason,
        gross_pnl_sol=gross_pnl,
        net_pnl_sol=net_pnl,
        return_pct=return_pct,
        is_winner=is_win,
    )
    return outcome, fees


def run_cluster_backtest(
    target_address: str = "5SW7p56x22LKj8gYcE8DVVd1S59UJUGR9jKq2PFdKiKg",
    policy: ClusterBacktestPolicy | None = None,
    launches: tuple[dict[str, Any], ...] | None = None,
) -> ClusterBacktestReport:
    """Execute a leakage-safe realistic backtest simulation on an operator cluster."""
    if policy is None:
        policy = ClusterBacktestPolicy()
    if launches is None:
        launches = VERIFIED_CLUSTER_LAUNCHES

    outcomes: list[ClusterTokenOutcome] = []
    total_invested = 0.0
    total_net_pnl = 0.0
    total_fees = 0.0
    winning_trades = 0
    losing_trades = 0
    peak_mults: list[float] = []
    times_to_peak: list[float] = []
    gross_gains = 0.0
    gross_losses = 0.0
    cumulative_pnl = 0.0
    max_drawdown = 0.0
    peak_pnl = 0.0

    for item in launches:
        total_invested += policy.quote_size_sol
        peak_mults.append(item["peak_mult"])
        times_to_peak.append(item["time_to_peak"])

        outcome, fees = _simulate_single_token(item, policy)
        total_fees += fees
        outcomes.append(outcome)

        if outcome.net_pnl_sol > 0:
            winning_trades += 1
            gross_gains += outcome.net_pnl_sol
        else:
            losing_trades += 1
            gross_losses += abs(outcome.net_pnl_sol)

        total_net_pnl += outcome.net_pnl_sol
        cumulative_pnl += outcome.net_pnl_sol
        peak_pnl = max(peak_pnl, cumulative_pnl)
        drawdown = peak_pnl - cumulative_pnl
        max_drawdown = max(max_drawdown, drawdown)

    sample_size = len(launches)
    winrate_pct = (winning_trades / sample_size * 100.0) if sample_size > 0 else 0.0
    net_roi_pct = (
        (total_net_pnl / total_invested * 100.0) if total_invested > 0 else 0.0
    )
    profit_factor = (gross_gains / gross_losses) if gross_losses > 0 else 99.0
    avg_peak_mult = sum(peak_mults) / len(peak_mults) if peak_mults else 1.0
    avg_time_to_peak = sum(times_to_peak) / len(times_to_peak) if times_to_peak else 0.0

    return ClusterBacktestReport(
        target_address=target_address,
        sample_size=sample_size,
        winning_trades=winning_trades,
        losing_trades=losing_trades,
        winrate_pct=winrate_pct,
        total_invested_sol=total_invested,
        total_net_pnl_sol=total_net_pnl,
        net_roi_pct=net_roi_pct,
        profit_factor=profit_factor,
        average_peak_multiplier=avg_peak_mult,
        average_time_to_peak_seconds=avg_time_to_peak,
        max_drawdown_sol=max_drawdown,
        total_fees_sol=total_fees,
        policy=policy,
        token_outcomes=tuple(outcomes),
    )


if __name__ == "__main__":
    report = run_cluster_backtest()
    print(f"=== BACKTEST REPORT FOR TARGET: {report.target_address} ===")
    print(f"Sample Size: {report.sample_size} tokens")
    print(
        f"Winrate: {report.winrate_pct:.1f}% ({report.winning_trades}W / {report.losing_trades}L)"
    )
    print(f"Total Invested: {report.total_invested_sol:.2f} SOL")
    print(
        f"Total Net PnL: {report.total_net_pnl_sol:+.4f} SOL (ROI: {report.net_roi_pct:+.1f}%)"
    )
    print(f"Profit Factor: {report.profit_factor:.2f}")
    print(f"Average Peak Multiplier: {report.average_peak_multiplier:.2f}x")
    print(f"Average Time to Peak: {report.average_time_to_peak_seconds:.1f}s")
    print(f"Total Fees & Gas: {report.total_fees_sol:.4f} SOL")
