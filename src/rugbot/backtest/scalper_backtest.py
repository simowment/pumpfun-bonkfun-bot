"""Paper replay of scalper strategy on finalized discover_trades.

Fills use the standard-curve math at each trade's price (fees and impact).
No live orders. Deterministic over finalized SQLite trades.
"""

from __future__ import annotations

from dataclasses import dataclass

from rugbot.domain.amounts import LAMPORTS_PER_SOL
from rugbot.domain.pump_curve import curve_buy, curve_sell, price_multiple
from rugbot.domain.scalper_strategy import (
    ScalperConfig,
    decide_scalper_exit,
    next_filled,
)
from rugbot.utils.logger import get_logger

logger = get_logger(__name__)

# Trade prices here are lamports per token base unit x PRICE_PPM.
PRICE_PPM = 1_000_000


@dataclass(frozen=True, slots=True)
class ScalperTradeOutcome:
    mint: str
    entry_slot: int
    exit_slot: int
    entry_price_ppm: int
    exit_price_ppm: int
    entry_quote_lamports: int
    exit_quote_lamports: int
    pnl_lamports: int
    pnl_pct: float
    is_win: bool
    exit_reason: str
    tranche_count: int


@dataclass(frozen=True, slots=True)
class ScalperBacktestResult:
    sample_size: int
    winning_trades: int
    losing_trades: int
    winrate_pct: float
    pnl_net_sol: float
    expectancy_sol: float
    avg_win_sol: float
    avg_loss_sol: float
    avg_win_pct: float
    avg_loss_pct: float
    profit_factor: float
    max_drawdown_sol: float
    total_fees_sol: float
    break_even_winrate_pct: float
    outcomes: tuple[ScalperTradeOutcome, ...]
    config: ScalperConfig
    insufficient_data: bool = False
    message: str = ""


def _price_ppm_from_trade(row: dict[str, object]) -> int:
    ppm = row.get("price_ppm")
    if isinstance(ppm, int) and ppm > 0:
        return ppm
    # fallback: quote/base
    quote = row.get("quote_amount_base_units")
    base = row.get("base_amount")
    if isinstance(quote, int) and isinstance(base, int) and base > 0:
        return max(1, (quote * 1_000_000) // base)
    return 0


def run_scalper_backtest(
    *,
    trades: list[dict[str, object]],
    launches: list[dict[str, object]] | None = None,
    config: ScalperConfig | None = None,
) -> ScalperBacktestResult:
    """Replay finalized trades through scalper strategy.

    Groups trades by mint ordered by slot, enters at first eligible trade,
    then evaluates TP/SL/tranches on subsequent trades.
    Fills use the standard-curve math at each trade's price.
    """
    if config is None:
        config = ScalperConfig()
    if not trades:
        return ScalperBacktestResult(
            sample_size=0,
            winning_trades=0,
            losing_trades=0,
            winrate_pct=0.0,
            pnl_net_sol=0.0,
            expectancy_sol=0.0,
            avg_win_sol=0.0,
            avg_loss_sol=0.0,
            avg_win_pct=0.0,
            avg_loss_pct=0.0,
            profit_factor=0.0,
            max_drawdown_sol=0.0,
            total_fees_sol=0.0,
            break_even_winrate_pct=0.0,
            outcomes=(),
            config=config,
            insufficient_data=True,
            message="no finalized trades available (fail-closed)",
        )

    # Group by mint
    by_mint: dict[str, list[dict[str, object]]] = {}
    for t in trades:
        mint = str(t.get("mint", ""))
        if not mint:
            continue
        by_mint.setdefault(mint, []).append(t)
    for mint_trades in by_mint.values():
        mint_trades.sort(key=lambda r: int(r.get("slot", 0)))

    # Launch slot map for entry offset check
    launch_slot_map: dict[str, int] = {}
    if launches:
        for row in launches:
            m = str(row.get("mint", ""))
            s = row.get("created_slot")
            if m and isinstance(s, int):
                launch_slot_map[m] = s

    outcomes: list[ScalperTradeOutcome] = []
    consecutive_losses = 0
    total_fees_lamports = 0

    # To simulate quote_engine fees, we compute buy quote fee once per entry
    position_size_lamports = int(config.position_size_sol * LAMPORTS_PER_SOL)

    for mint, mint_trades in sorted(
        by_mint.items(), key=lambda kv: int(kv[1][0].get("slot", 0))
    ):
        if consecutive_losses >= config.daily_loss_stop:
            logger.info("circuit breaker hit, skipping remaining mints")
            break
        if len(mint_trades) < config.min_trades_for_entry:
            continue
        first = mint_trades[0]
        entry_slot = int(first.get("slot", 0))
        entry_ppm = _price_ppm_from_trade(first)
        if entry_ppm <= 0:
            continue
        # entry offset vs launch
        launch_slot = launch_slot_map.get(mint)
        slot_offset = 0 if launch_slot is None else entry_slot - launch_slot
        slot_offset = max(slot_offset, 0)
        if slot_offset > config.max_entry_slot_offset:
            continue
        # entry MC proxy via quote lamports if available
        quote_lamports = first.get("quote_amount_base_units")
        if (
            isinstance(quote_lamports, int)
            and quote_lamports > config.entry_max_quote_lamports
        ):
            # still allow if not too high? skip
            pass

        # Simulate buy execution with quote_engine to get fee
        buy_output, buy_fee = curve_buy(
            price_multiple(entry_ppm / PRICE_PPM), position_size_lamports
        )
        entry_quote = position_size_lamports
        total_fees_lamports += buy_fee
        # entry base amount (tokens) proportional to buy_output
        entry_base = buy_output if buy_output > 0 else 1

        # Track tranches
        n_levels = len(config.tp_levels_pct)
        filled: tuple[bool, ...] = tuple(False for _ in range(n_levels))
        sold_base = 0
        realized_quote = 0
        tranche_count = 0
        exit_reason = "hold"
        exit_slot = entry_slot
        exit_ppm = entry_ppm

        closed = False
        for trade in mint_trades[1:]:
            cur_slot = int(trade.get("slot", 0))
            cur_ppm = _price_ppm_from_trade(trade)
            if cur_ppm <= 0:
                continue
            sig = decide_scalper_exit(
                config=config,
                entry_price_ppm=entry_ppm,
                current_price_ppm=cur_ppm,
                current_slot=cur_slot,
                entry_slot=entry_slot,
                filled=filled,
                consecutive_losses=consecutive_losses,
            )
            if sig.action == "hold":
                if sig.reason == "circuit_breaker":
                    break
                continue
            if sig.action == "stop_loss":
                # sell remaining base one-shot
                remaining = entry_base - sold_base
                if remaining <= 0:
                    closed = True
                    exit_reason = sig.reason
                    exit_slot = cur_slot
                    exit_ppm = cur_ppm
                    break
                proceeds, fee2 = curve_sell(
                    price_multiple(cur_ppm / PRICE_PPM), remaining
                )
                realized_quote += proceeds
                total_fees_lamports += fee2
                tranche_count += 1
                exit_reason = sig.reason
                exit_slot = cur_slot
                exit_ppm = cur_ppm
                closed = True
                break
            if sig.action == "take_profit":
                # sell fraction tranche
                frac = sig.fraction or 0.0
                # tranche base amount
                tranche_base = int(entry_base * frac)
                if tranche_base <= 0:
                    tranche_base = max(1, int(entry_base * frac))
                tranche_base = min(tranche_base, entry_base - sold_base)
                if tranche_base <= 0:
                    continue
                proceeds, fee2 = curve_sell(
                    price_multiple(cur_ppm / PRICE_PPM), tranche_base
                )
                realized_quote += proceeds
                total_fees_lamports += fee2
                sold_base += tranche_base
                tranche_count += 1
                exit_slot = cur_slot
                exit_ppm = cur_ppm
                if sig.tranche_index is not None:
                    filled = next_filled(filled, sig.tranche_index)
                # if all tranches filled, close
                if all(filled) or sold_base >= entry_base:
                    exit_reason = sig.reason
                    closed = True
                    break
                # else continue to next TP
                exit_reason = sig.reason

        # If not closed but sold some, force exit at last observed price
        if not closed and sold_base > 0 and sold_base < entry_base:
            last = mint_trades[-1]
            cur_ppm = _price_ppm_from_trade(last)
            cur_slot = int(last.get("slot", 0))
            remaining = entry_base - sold_base
            proceeds, fee2 = curve_sell(price_multiple(cur_ppm / PRICE_PPM), remaining)
            realized_quote += proceeds
            total_fees_lamports += fee2
            tranche_count += 1
            exit_slot = cur_slot
            exit_ppm = cur_ppm
            exit_reason = (
                exit_reason + "+tail" if exit_reason != "hold" else "tail_exit"
            )
            closed = True
        elif not closed and sold_base == 0:
            # No TP hit: evaluate final pnl at last price, count as loss/timeout
            last = mint_trades[-1]
            cur_ppm = _price_ppm_from_trade(last)
            cur_slot = int(last.get("slot", 0))
            # force sell all at last price (timeout)
            remaining = entry_base
            proceeds, fee2 = curve_sell(price_multiple(cur_ppm / PRICE_PPM), remaining)
            realized_quote = proceeds
            total_fees_lamports += fee2
            tranche_count = 1
            exit_slot = cur_slot
            exit_ppm = cur_ppm
            # determine reason
            pnl_tmp = (cur_ppm - entry_ppm) / entry_ppm * 100 if entry_ppm else 0
            exit_reason = "timeout_profit" if pnl_tmp > 0 else "timeout_loss"
            closed = True

        if not closed:
            continue

        pnl_lamports = realized_quote - entry_quote
        pnl_pct = (pnl_lamports / entry_quote * 100) if entry_quote else 0.0
        is_win = pnl_lamports > 0
        if is_win:
            consecutive_losses = 0
        else:
            consecutive_losses += 1

        outcomes.append(
            ScalperTradeOutcome(
                mint=mint,
                entry_slot=entry_slot,
                exit_slot=exit_slot,
                entry_price_ppm=entry_ppm,
                exit_price_ppm=exit_ppm,
                entry_quote_lamports=entry_quote,
                exit_quote_lamports=realized_quote,
                pnl_lamports=pnl_lamports,
                pnl_pct=pnl_pct,
                is_win=is_win,
                exit_reason=exit_reason,
                tranche_count=tranche_count,
            )
        )

    sample_size = len(outcomes)
    if sample_size == 0:
        # insufficient data fail-closed: not enough eligible entries
        return ScalperBacktestResult(
            sample_size=0,
            winning_trades=0,
            losing_trades=0,
            winrate_pct=0.0,
            pnl_net_sol=0.0,
            expectancy_sol=0.0,
            avg_win_sol=0.0,
            avg_loss_sol=0.0,
            avg_win_pct=0.0,
            avg_loss_pct=0.0,
            profit_factor=0.0,
            max_drawdown_sol=0.0,
            total_fees_sol=total_fees_lamports / LAMPORTS_PER_SOL,
            break_even_winrate_pct=0.0,
            outcomes=(),
            config=config,
            insufficient_data=True,
            message="no eligible entries after filters (fail-closed)",
        )

    wins = sum(1 for o in outcomes if o.is_win)
    losses = sample_size - wins
    winrate = wins / sample_size * 100 if sample_size else 0.0
    pnl_net_lamports = sum(o.pnl_lamports for o in outcomes)
    pnl_net_sol = pnl_net_lamports / LAMPORTS_PER_SOL
    expectancy_sol = pnl_net_sol / sample_size if sample_size else 0.0
    avg_win = (
        sum(o.pnl_lamports for o in outcomes if o.is_win)
        / max(1, wins)
        / LAMPORTS_PER_SOL
    )
    avg_loss = (
        sum(o.pnl_lamports for o in outcomes if not o.is_win)
        / max(1, losses)
        / LAMPORTS_PER_SOL
    )
    avg_win_pct = sum(o.pnl_pct for o in outcomes if o.is_win) / max(1, wins)
    avg_loss_pct = sum(o.pnl_pct for o in outcomes if not o.is_win) / max(1, losses)
    gross_gains = sum(o.pnl_lamports for o in outcomes if o.is_win)
    gross_losses = abs(sum(o.pnl_lamports for o in outcomes if not o.is_win))
    profit_factor = gross_gains / gross_losses if gross_losses > 0 else 99.0
    # max drawdown on cumulative
    cum = 0
    peak = 0
    max_dd = 0
    for o in outcomes:
        cum += o.pnl_lamports / LAMPORTS_PER_SOL
        peak = max(peak, cum)
        dd = peak - cum
        max_dd = max(max_dd, dd)
    # break-even winrate = avg_loss / (avg_win - avg_loss)  ; using absolute
    if avg_win > 0 and avg_loss < 0:
        be = abs(avg_loss) / (avg_win + abs(avg_loss)) * 100
    else:
        be = 0.0

    return ScalperBacktestResult(
        sample_size=sample_size,
        winning_trades=wins,
        losing_trades=losses,
        winrate_pct=winrate,
        pnl_net_sol=pnl_net_sol,
        expectancy_sol=expectancy_sol,
        avg_win_sol=avg_win if wins else 0.0,
        avg_loss_sol=avg_loss if losses else 0.0,
        avg_win_pct=avg_win_pct if wins else 0.0,
        avg_loss_pct=avg_loss_pct if losses else 0.0,
        profit_factor=profit_factor,
        max_drawdown_sol=max_dd,
        total_fees_sol=total_fees_lamports / LAMPORTS_PER_SOL,
        break_even_winrate_pct=be,
        outcomes=tuple(outcomes),
        config=config,
        insufficient_data=False,
        message="ok",
    )


def result_to_json(result: ScalperBacktestResult) -> dict[str, object]:
    return {
        "sample_size": result.sample_size,
        "winning_trades": result.winning_trades,
        "losing_trades": result.losing_trades,
        "winrate_pct": round(result.winrate_pct, 2),
        "pnl_net_sol": round(result.pnl_net_sol, 6),
        "expectancy_sol": round(result.expectancy_sol, 6),
        "avg_win_sol": round(result.avg_win_sol, 6),
        "avg_loss_sol": round(result.avg_loss_sol, 6),
        "avg_win_pct": round(result.avg_win_pct, 2),
        "avg_loss_pct": round(result.avg_loss_pct, 2),
        "profit_factor": round(result.profit_factor, 3),
        "max_drawdown_sol": round(result.max_drawdown_sol, 6),
        "total_fees_sol": round(result.total_fees_sol, 6),
        "break_even_winrate_pct": round(result.break_even_winrate_pct, 2),
        "insufficient_data": result.insufficient_data,
        "message": result.message,
        "note": "paper only, no orders, validates expectancy before any live authorization",
        "outcomes": [
            {
                "mint": o.mint,
                "entry_slot": o.entry_slot,
                "exit_slot": o.exit_slot,
                "pnl_sol": round(o.pnl_lamports / LAMPORTS_PER_SOL, 6),
                "pnl_pct": round(o.pnl_pct, 2),
                "is_win": o.is_win,
                "exit_reason": o.exit_reason,
                "tranches": o.tranche_count,
            }
            for o in result.outcomes[:50]
        ],
    }


def format_human(result: ScalperBacktestResult) -> str:
    lines = [
        "=== SCALPER PAPER BACKTEST (paper only, no orders) ===",
        f"sample_size: {result.sample_size}  wins:{result.winning_trades} losses:{result.losing_trades} winrate:{result.winrate_pct:.1f}% (BE {result.break_even_winrate_pct:.1f}%)",
        f"pnl_net: {result.pnl_net_sol:.4f} SOL  expectancy: {result.expectancy_sol:.4f} SOL/trade  profit_factor: {result.profit_factor:.2f}",
        f"avg_win: {result.avg_win_sol:.4f} SOL ({result.avg_win_pct:.1f}%)  avg_loss: {result.avg_loss_sol:.4f} SOL ({result.avg_loss_pct:.1f}%)",
        f"max_drawdown: {result.max_drawdown_sol:.4f} SOL  fees: {result.total_fees_sol:.4f} SOL",
        f"config: size={result.config.position_size_sol} SOL TP={result.config.tp_levels_pct} SL={result.config.sl_pct}% fractions={result.config.sell_fractions} daily_stop={result.config.daily_loss_stop}",
    ]
    if result.insufficient_data:
        lines.append(f"INSUFFICIENT DATA (fail-closed): {result.message}")
    else:
        lines.append("outcomes (first 10):")
        for o in result.outcomes[:10]:
            lines.append(
                f"  {o.mint[:12]}.. pnl {o.pnl_lamports / LAMPORTS_PER_SOL:+.4f} SOL ({o.pnl_pct:+.1f}%) {o.exit_reason} tranches={o.tranche_count}"
            )
    lines.append(
        "note: paper only, no orders, validates expectancy before any live authorization"
    )
    return "\n".join(lines)
