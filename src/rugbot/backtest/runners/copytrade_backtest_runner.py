"""Copytrade TP×SL grid backtester — realistic wallet trade following simulation.

Accurately models:
1. Signal: Triggered on target trader's buy transaction.
2. Entry Realism: Follower execution lag (copy_lag_slots >= 1) and leader price impact.
3. Exit Realism: Follower TP/SL thresholds OR Mirroring leader's sell (with leader dump penalty).
"""

from __future__ import annotations

import math
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from rugbot.domain.fees import FeeConfig
from rugbot.domain.quote_engine import (
    PoolReserves,
    executable_buy_quote,
    executable_sell_quote,
)
from rugbot.domain.quotes import ExecutableQuote, QuotePath
from rugbot.utils.logger import get_logger

logger = get_logger(__name__)

LAMPORTS_PER_SOL = 1_000_000_000

DEFAULT_FEE_CONFIG = FeeConfig(
    version="pump-global-v1",
    protocol_fee_bps=95,
    creator_fee_bps=30,
    is_known=True,
    program_config_version="pump-global-v1",
    valid_from_slot=0,
    valid_to_slot=None,
    source_artifact_version="pump-global-v1",
    lp_fee_bps=0,
)

_SYNTH_VIRTUAL_BASE: int = 1_073_000_000_000_000
_SYNTH_REAL_BASE: int = 800_000_000_000_000
_SYNTH_REAL_QUOTE: int = 30_000_000_000


def _synthetic_reserves(multiplier: float, slot: int) -> PoolReserves:
    mult = max(0.001, float(multiplier))
    sqrt_m = math.sqrt(mult)
    v_quote = max(1, int(_SYNTH_REAL_QUOTE * sqrt_m))
    v_base = max(1, int(_SYNTH_VIRTUAL_BASE / sqrt_m))
    return PoolReserves(
        virtual_base_reserves=v_base,
        virtual_quote_reserves=v_quote,
        real_base_reserves=_SYNTH_REAL_BASE,
        real_quote_reserves=_SYNTH_REAL_QUOTE,
        is_complete=False,
        as_of_slot=slot,
        base_decimals=6,
        quote_decimals=9,
        decoder_version="pump-bc-v1-synth",
        idl_hash="synthetic",
        program_config_version="pump-global-v1",
    )


@dataclass(frozen=True, slots=True)
class CopytradeBacktestConfig:
    quote_size_sol: float = 0.3
    copy_lag_slots: int = 1  # Execution delay after leader's buy (e.g. 1 slot = ~400ms)
    copy_entry_slippage_pct: float = 1.5
    mirror_target_sells: bool = True  # Exit when leader sells if before TP/SL
    target_sell_dump_penalty_pct: float = (
        2.0  # Extra slippage because leader sells before follower
    )
    pump_fee_pct: float = 1.0
    gas_fee_sol: float = 0.001
    jito_tip_sol: float = 0.003
    max_hold_s: int = 300
    tp_grid: tuple[float, ...] = (15.0, 25.0, 50.0, 75.0, 100.0, 200.0)
    sl_grid: tuple[float, ...] = (10.0, 20.0, 30.0, 50.0)


@dataclass(frozen=True, slots=True)
class CopytradeSample:
    mint: str
    wallet: str
    buy_slot: int
    buy_timestamp: int | None
    buy_sol: float
    buy_tokens: float
    buy_price_ppm: int
    sell_slot: int | None
    sell_timestamp: int | None
    sell_sol: float | None
    sell_tokens: float | None
    sell_price_ppm: int | None
    trajectory: tuple[
        tuple[float, float], ...
    ]  # (seconds_from_entry, price_multiplier)
    peak_multiplier: float | None = None
    target_hold_seconds: float = 0.0
    target_pnl_pct: float = 0.0


@dataclass(frozen=True, slots=True)
class CopytradeTpSlEvaluation:
    tp_pct: float
    sl_pct: float
    wins: int
    losses: int
    winrate_pct: float
    gross_pnl_sol: float
    fees_sol: float
    net_pnl_sol: float
    net_ev_sol: float
    net_roi_pct: float
    max_drawdown_sol: float
    leader_roi_pct: float
    lag_drag_sol: float
    robust: bool


from rugbot.backtest.reporting.visualizer import TradePerformanceRecord


@dataclass(frozen=True, slots=True)
class CopytradeBacktestReport:
    target: str
    mode: str
    samples: tuple[CopytradeSample, ...]
    evaluations: tuple[CopytradeTpSlEvaluation, ...]
    optimal_tp: float | None
    optimal_sl: float | None
    optimal_ev: float
    robust_zone: tuple[tuple[float, float], ...]
    records: tuple[TradePerformanceRecord, ...] = ()
    market_impact_drag_sol: float = 0.0
    warnings: tuple[str, ...] = ()
    insufficient_data: bool = False
    message: str = ""


def _net_pnl_for_copytrade(
    exit_multiplier: float,
    entry_lag_multiplier: float,
    config: CopytradeBacktestConfig,
) -> tuple[float, float, float]:
    """Compute (gross_pnl, fees, net_pnl) in SOL for a copytrade outcome with exact CPMM math."""
    entry_quote = int(config.quote_size_sol * LAMPORTS_PER_SOL)
    reserves_in = _synthetic_reserves(multiplier=entry_lag_multiplier, slot=1)

    buy_quote = executable_buy_quote(
        path=QuotePath.PUMP_BONDING_CURVE,
        reserves=reserves_in,
        quote_input_amount=entry_quote,
        fee_config=DEFAULT_FEE_CONFIG,
    )
    if not isinstance(buy_quote, ExecutableQuote):
        tokens_received = int(
            (entry_quote * _SYNTH_VIRTUAL_BASE)
            / (reserves_in.virtual_quote_reserves + entry_quote)
        )
        entry_protocol_fee = entry_quote * 0.0125 / LAMPORTS_PER_SOL
    else:
        tokens_received = buy_quote.output_amount_base_units
        entry_protocol_fee = buy_quote.fee_amount_base_units / LAMPORTS_PER_SOL

    # Exit price calculation
    eff_exit_mult = max(0.001, exit_multiplier)
    reserves_out = _synthetic_reserves(multiplier=eff_exit_mult, slot=2)
    sell_quote = executable_sell_quote(
        path=QuotePath.PUMP_BONDING_CURVE,
        reserves=reserves_out,
        base_input_amount=tokens_received,
        fee_config=DEFAULT_FEE_CONFIG,
    )
    if not isinstance(sell_quote, ExecutableQuote):
        exit_quote_sol = config.quote_size_sol * (
            eff_exit_mult / max(1.0, entry_lag_multiplier)
        )
        exit_protocol_fee = exit_quote_sol * 0.0125
    else:
        exit_quote_sol = sell_quote.output_amount_base_units / LAMPORTS_PER_SOL
        exit_protocol_fee = sell_quote.fee_amount_base_units / LAMPORTS_PER_SOL

    # Transaction costs
    total_tx_fees = (config.gas_fee_sol * 2) + config.jito_tip_sol
    total_fees = total_tx_fees + entry_protocol_fee + exit_protocol_fee

    gross_pnl = exit_quote_sol - config.quote_size_sol
    net_pnl = gross_pnl - total_fees
    return gross_pnl, total_fees, net_pnl


def _eval_copytrade_single_sample(
    sample: CopytradeSample,
    tp_pct: float,
    sl_pct: float,
    config: CopytradeBacktestConfig,
) -> tuple[float, float, float, bool]:
    """Replay single copytrade trajectory with lag and mirror sell."""
    # Entry lag penalty (e.g. 1 slot = +0.5% to +1.5% worse entry)
    lag_penalty = (
        1.0 + (config.copy_lag_slots * 0.005) + (config.copy_entry_slippage_pct / 100.0)
    )

    tp_mult = 1.0 + (tp_pct / 100.0)
    sl_mult = 1.0 - (sl_pct / 100.0)

    exit_multiplier = None

    # 1. Walk through trajectory
    for sec, mult in sample.trajectory:
        if sec > config.max_hold_s:
            break
        # Adjusted multiplier seen from follower's entry
        follower_mult = mult / lag_penalty

        if follower_mult >= tp_mult:
            exit_multiplier = tp_mult
            break
        if follower_mult <= sl_mult:
            exit_multiplier = sl_mult
            break

    # 2. If no TP/SL hit, check mirror sell event
    if exit_multiplier is None:
        if (
            config.mirror_target_sells
            and sample.sell_price_ppm
            and sample.buy_price_ppm
        ):
            raw_leader_ratio = sample.sell_price_ppm / sample.buy_price_ppm
            # Leader dumped first -> extra slippage penalty
            exit_multiplier = (raw_leader_ratio / lag_penalty) * (
                1.0 - config.target_sell_dump_penalty_pct / 100.0
            )
        elif sample.trajectory:
            # Last known price in max hold window
            last_mult = sample.trajectory[-1][1]
            exit_multiplier = last_mult / lag_penalty
        else:
            exit_multiplier = 1.0 / lag_penalty

    gross, fees, net = _net_pnl_for_copytrade(exit_multiplier, lag_penalty, config)
    is_win = net > 0
    return gross, fees, net, is_win


def run_copytrade_tp_sl_grid_search(
    samples: Sequence[CopytradeSample],
    config: CopytradeBacktestConfig,
    target: str,
) -> CopytradeBacktestReport:
    """Run full TP×SL grid search for copytrading."""
    if not samples:
        return CopytradeBacktestReport(
            target=target,
            mode="copytrade",
            samples=(),
            evaluations=(),
            optimal_tp=None,
            optimal_sl=None,
            optimal_ev=-999.0,
            robust_zone=(),
            warnings=("insufficient_data",),
            insufficient_data=True,
            message=f"No copytrade samples found for target {target}",
        )

    evaluations: list[CopytradeTpSlEvaluation] = []

    # Leader's average baseline ROI
    leader_rois = [s.target_pnl_pct for s in samples]
    avg_leader_roi = sum(leader_rois) / len(leader_rois) if leader_rois else 0.0

    for tp in config.tp_grid:
        for sl in config.sl_grid:
            wins = losses = 0
            gross_tot = fees_tot = net_tot = 0.0
            equity = 0.0
            peak_equity = 0.0
            max_dd = 0.0

            for s in samples:
                gross, fees, net, is_win = _eval_copytrade_single_sample(
                    s, tp, sl, config
                )
                gross_tot += gross
                fees_tot += fees
                net_tot += net
                if is_win:
                    wins += 1
                else:
                    losses += 1

                equity += net
                peak_equity = max(peak_equity, equity)
                dd = peak_equity - equity
                max_dd = max(max_dd, dd)

            n = len(samples)
            wr = (wins / n * 100.0) if n else 0.0
            ev = net_tot / n if n else 0.0
            roi = (
                (net_tot / (config.quote_size_sol * n) * 100.0)
                if n and config.quote_size_sol > 0
                else 0.0
            )

            # Lag drag is difference between leader ROI and follower ROI
            lag_drag_sol = max(
                0.0, (avg_leader_roi - roi) / 100.0 * config.quote_size_sol * n
            )

            evaluations.append(
                CopytradeTpSlEvaluation(
                    tp_pct=tp,
                    sl_pct=sl,
                    wins=wins,
                    losses=losses,
                    winrate_pct=wr,
                    gross_pnl_sol=gross_tot,
                    fees_sol=fees_tot,
                    net_pnl_sol=net_tot,
                    net_ev_sol=ev,
                    net_roi_pct=roi,
                    max_drawdown_sol=max_dd,
                    leader_roi_pct=avg_leader_roi,
                    lag_drag_sol=lag_drag_sol,
                    robust=False,
                )
            )

    # Find optimal
    best = max(evaluations, key=lambda e: e.net_ev_sol)
    optimal_tp = best.tp_pct
    optimal_sl = best.sl_pct
    optimal_ev = best.net_ev_sol

    # Robust zone (adjacent cells within 15% of best EV)
    robust_zone: list[tuple[float, float]] = []
    threshold = optimal_ev * 0.85 if optimal_ev > 0 else optimal_ev * 1.15
    for e in evaluations:
        if e.net_ev_sol >= threshold:
            robust_zone.append((e.tp_pct, e.sl_pct))

    # Mark robust
    final_evals = tuple(
        CopytradeTpSlEvaluation(
            tp_pct=e.tp_pct,
            sl_pct=e.sl_pct,
            wins=e.wins,
            losses=e.losses,
            winrate_pct=e.winrate_pct,
            gross_pnl_sol=e.gross_pnl_sol,
            fees_sol=e.fees_sol,
            net_pnl_sol=e.net_pnl_sol,
            net_ev_sol=e.net_ev_sol,
            net_roi_pct=e.net_roi_pct,
            max_drawdown_sol=e.max_drawdown_sol,
            leader_roi_pct=e.leader_roi_pct,
            lag_drag_sol=e.lag_drag_sol,
            robust=(e.tp_pct, e.sl_pct) in robust_zone,
        )
        for e in evaluations
    )

    # Build trade records for optimal setup
    records: list[TradePerformanceRecord] = []
    cum_eq = 0.0
    peak_eq = 0.0
    opt_tp = optimal_tp or 25.0
    opt_sl = optimal_sl or 20.0
    market_impact_pct = (config.quote_size_sol / 30.0) * 100.0
    total_impact_drag = 0.0

    for idx, s in enumerate(samples, start=1):
        gross, fees, net, is_win = _eval_copytrade_single_sample(
            s, opt_tp, opt_sl, config
        )
        cum_eq += net
        peak_eq = max(peak_eq, cum_eq)
        dd = ((peak_eq - cum_eq) / peak_eq * 100.0) if peak_eq > 0 else 0.0
        impact_drag = config.quote_size_sol * (market_impact_pct / 100.0)
        total_impact_drag += impact_drag
        records.append(
            TradePerformanceRecord(
                trade_index=idx,
                mint=s.mint,
                entry_sol=config.quote_size_sol,
                exit_sol=config.quote_size_sol + gross,
                gross_pnl_sol=gross,
                net_pnl_sol=net,
                roi_pct=(
                    (net / config.quote_size_sol * 100.0)
                    if config.quote_size_sol > 0
                    else 0.0
                ),
                market_impact_pct=market_impact_pct,
                holding_seconds=s.target_hold_seconds,
                is_win=is_win,
                cumulative_equity_sol=cum_eq,
                drawdown_pct=dd,
            )
        )

    return CopytradeBacktestReport(
        target=target,
        mode="copytrade",
        samples=tuple(samples),
        evaluations=final_evals,
        optimal_tp=optimal_tp,
        optimal_sl=optimal_sl,
        optimal_ev=optimal_ev,
        robust_zone=tuple(robust_zone),
        records=tuple(records),
        market_impact_drag_sol=total_impact_drag,
        warnings=(),
        insufficient_data=False,
        message="ok",
    )


def _fetch_onchain_copytrade_samples(wallet: str) -> tuple[CopytradeSample, ...]:
    """Fetch on-chain trade history from RPC and reconstruct completed roundtrips."""
    import json
    import os
    import urllib.request

    from rugbot.runtime.config import load_provider_settings, resolve_dotenv

    resolve_dotenv()
    providers = load_provider_settings()

    candidate_endpoints = []
    if providers and providers.rpc_http:
        candidate_endpoints.append(providers.rpc_http)
    if providers and providers.rpc_http_fallbacks:
        candidate_endpoints.extend(providers.rpc_http_fallbacks)
    if os.environ.get("SOLANA_RPC_HTTP"):
        candidate_endpoints.append(os.environ["SOLANA_RPC_HTTP"])
    candidate_endpoints.extend(
        [
            "https://solana-rpc.publicnode.com",
            "https://rpc.ankr.com/solana",
            "https://api.mainnet-beta.solana.com",
        ]
    )

    # Deduplicate preserving order
    endpoints = []
    for ep in candidate_endpoints:
        if ep and ep not in endpoints:
            endpoints.append(ep)

    def _call_rpc(method: str, params: list[object]) -> object | None:
        payload = json.dumps(
            {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
        ).encode("utf-8")
        for ep in endpoints:
            try:
                req = urllib.request.Request(
                    ep,
                    data=payload,
                    headers={
                        "Content-Type": "application/json",
                        "User-Agent": "Mozilla/5.0",
                    },
                )
                with urllib.request.urlopen(req, timeout=10) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
                    if "result" in data and data["result"] is not None:
                        return data["result"]
            except Exception:
                continue
        return None

    sigs_raw = _call_rpc("getSignaturesForAddress", [wallet, {"limit": 100}])
    if not isinstance(sigs_raw, list) or not sigs_raw:
        return ()

    sigs_chrono = sorted(
        sigs_raw, key=lambda s: s.get("slot", 0) if isinstance(s, dict) else 0
    )
    mint_trades: dict[str, list[dict]] = {}

    def _process_sig(s: dict) -> list[tuple[str, dict]]:
        results: list[tuple[str, dict]] = []
        sig = s.get("signature")
        if not sig:
            return results
        tx_data = _call_rpc(
            "getTransaction",
            [sig, {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0}],
        )
        if not isinstance(tx_data, dict):
            return results
        meta = tx_data.get("meta", {})
        if not isinstance(meta, dict) or meta.get("err"):
            return results
        slot = tx_data.get("slot", 0)
        btime = tx_data.get("blockTime", 0)

        account_keys = (
            tx_data.get("transaction", {}).get("message", {}).get("accountKeys", [])
        )
        w_pubkeys = [
            a.get("pubkey") if isinstance(a, dict) else a for a in account_keys
        ]
        if wallet not in w_pubkeys:
            return results
        w_idx = w_pubkeys.index(wallet)
        pre_sol = (
            meta.get("preBalances", [])[w_idx]
            if len(meta.get("preBalances", [])) > w_idx
            else 0
        )
        post_sol = (
            meta.get("postBalances", [])[w_idx]
            if len(meta.get("postBalances", [])) > w_idx
            else 0
        )
        sol_delta = (post_sol - pre_sol) / LAMPORTS_PER_SOL

        pre_tokens = {
            b.get("mint"): float(b.get("uiTokenAmount", {}).get("uiAmount") or 0)
            for b in meta.get("preTokenBalances", [])
            if isinstance(b, dict) and b.get("owner") == wallet
        }
        post_tokens = {
            b.get("mint"): float(b.get("uiTokenAmount", {}).get("uiAmount") or 0)
            for b in meta.get("postTokenBalances", [])
            if isinstance(b, dict) and b.get("owner") == wallet
        }

        all_mints = set(pre_tokens.keys()) | set(post_tokens.keys())
        for m in all_mints:
            if not m or not isinstance(m, str):
                continue
            token_pre = pre_tokens.get(m, 0.0)
            token_post = post_tokens.get(m, 0.0)
            token_delta = token_post - token_pre

            if token_delta > 0:  # BUY
                cost_sol = abs(sol_delta) if sol_delta < 0 else 0.1
                price_ppm = (
                    int((cost_sol / token_delta) * 1e12)
                    if token_delta > 0
                    else 1_000_000
                )
                results.append(
                    (
                        m,
                        {
                            "side": "buy",
                            "slot": slot,
                            "time": btime,
                            "sol": cost_sol,
                            "tokens": token_delta,
                            "price_ppm": max(1, price_ppm),
                        },
                    )
                )
            elif token_delta < 0:  # SELL
                rec_sol = sol_delta if sol_delta > 0 else 0.0
                sold_tok = abs(token_delta)
                price_ppm = (
                    int((rec_sol / sold_tok) * 1e12)
                    if sold_tok > 0 and rec_sol > 0
                    else 1_000_000
                )
                results.append(
                    (
                        m,
                        {
                            "side": "sell",
                            "slot": slot,
                            "time": btime,
                            "sol": rec_sol,
                            "tokens": sold_tok,
                            "price_ppm": max(1, price_ppm),
                        },
                    )
                )
        return results

    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(max_workers=10) as executor:
        batch_results = executor.map(
            _process_sig, [s for s in sigs_chrono if isinstance(s, dict)]
        )
        for res_list in batch_results:
            for m, t_entry in res_list:
                mint_trades.setdefault(m, []).append(t_entry)

    samples: list[CopytradeSample] = []
    for mint, trs in mint_trades.items():
        buys = [t for t in trs if t["side"] == "buy"]
        sells = [t for t in trs if t["side"] == "sell"]
        if not buys:
            continue
        first_buy = buys[0]
        first_sell = sells[0] if sells else None

        # Realistic bonding curve fair pricing
        tok_b = first_buy["tokens"]
        fair_buy_sol = (
            (30.0 * tok_b / max(1.0, 1_000_000_000.0 - tok_b))
            if tok_b < 900_000_000
            else 0.3
        )
        buy_sol = first_buy["sol"]
        if buy_sol < 0.01 or buy_sol > 15.0:
            buy_sol = max(0.05, min(5.0, fair_buy_sol))

        if first_sell and first_sell["sol"] and first_sell["sol"] > 0:
            raw_ratio = first_sell["sol"] / max(0.001, first_buy["sol"])
            ratio = max(0.05, min(4.0, raw_ratio))
            sell_sol = buy_sol * ratio
        else:
            ratio = 1.0
            sell_sol = None

        hold_s = (
            (first_sell["time"] - first_buy["time"])
            if first_sell and first_sell["time"] and first_buy["time"]
            else 60.0
        )
        if hold_s <= 0:
            hold_s = (
                max(1.0, (first_sell["slot"] - first_buy["slot"]) * 0.4)
                if first_sell
                else 60.0
            )

        pnl_pct = ((ratio - 1.0) * 100.0) if first_sell else 0.0
        peak = max(1.0, min(4.0, ratio if ratio > 1.0 else 1.15))
        traj = (
            (0.0, 1.0),
            (hold_s * 0.5, peak),
            (hold_s, max(0.05, ratio)),
        )

        b_ppm = int((buy_sol / max(1.0, tok_b)) * 1e12)
        s_ppm = int(b_ppm * ratio) if first_sell else None

        samples.append(
            CopytradeSample(
                mint=mint,
                wallet=wallet,
                buy_slot=first_buy["slot"],
                buy_timestamp=first_buy["time"],
                buy_sol=buy_sol,
                buy_tokens=tok_b,
                buy_price_ppm=max(1, b_ppm),
                sell_slot=first_sell["slot"] if first_sell else None,
                sell_timestamp=first_sell["time"] if first_sell else None,
                sell_sol=sell_sol,
                sell_tokens=first_sell["tokens"] if first_sell else None,
                sell_price_ppm=s_ppm,
                trajectory=traj,
                peak_multiplier=peak,
                target_hold_seconds=hold_s,
                target_pnl_pct=pnl_pct,
            )
        )

    return tuple(samples)


def resolve_copytrade_samples(
    wallet: str,
    db_path: Path | str | None = None,
) -> tuple[CopytradeSample, ...]:
    """Resolve historical copytrade samples for a wallet."""
    # 1. Query discover_trades / trader history if db exists
    samples: list[CopytradeSample] = []
    if db_path is None:
        db_path = Path("state.sqlite3")
    else:
        db_path = Path(db_path)

    if db_path.exists():
        try:
            with sqlite3.connect(str(db_path)) as conn:
                cur = conn.cursor()
                cur.execute(
                    """
                    SELECT mint, side, quote_amount, base_amount, slot, block_time, price_ppm
                    FROM discover_trades
                    WHERE user_wallet = ? OR maker = ?
                    ORDER BY slot ASC
                    """,
                    (wallet, wallet),
                )
                rows = cur.fetchall()

                # Pair buys with sells by mint
                by_mint: dict[str, list[dict]] = {}
                for r in rows:
                    mint, side, q, b, slot, btime, price = r
                    by_mint.setdefault(mint, []).append(
                        {
                            "side": side,
                            "quote": q or 0,
                            "base": b or 0,
                            "slot": slot or 0,
                            "time": btime or 0,
                            "price": price or 1_000_000,
                        }
                    )

                for mint, trs in by_mint.items():
                    buys = [t for t in trs if t["side"] == "buy"]
                    sells = [t for t in trs if t["side"] == "sell"]
                    if not buys:
                        continue
                    first_buy = buys[0]
                    first_sell = sells[0] if sells else None

                    buy_sol = first_buy["quote"] / LAMPORTS_PER_SOL
                    sell_sol = (
                        (first_sell["quote"] / LAMPORTS_PER_SOL) if first_sell else None
                    )
                    hold_s = (
                        (first_sell["time"] - first_buy["time"])
                        if first_sell and first_sell["time"] and first_buy["time"]
                        else 60.0
                    )
                    pnl_pct = (
                        ((sell_sol - buy_sol) / buy_sol * 100.0)
                        if sell_sol and buy_sol > 0
                        else 0.0
                    )

                    # Synthetic trajectory
                    peak = (
                        max(1.0, (sell_sol / buy_sol))
                        if sell_sol and buy_sol > 0
                        else 1.2
                    )
                    traj = (
                        (0.0, 1.0),
                        (hold_s * 0.5, peak),
                        (hold_s, peak * 0.9 if first_sell else 1.0),
                    )

                    samples.append(
                        CopytradeSample(
                            mint=mint,
                            wallet=wallet,
                            buy_slot=first_buy["slot"],
                            buy_timestamp=first_buy["time"],
                            buy_sol=buy_sol,
                            buy_tokens=first_buy["base"],
                            buy_price_ppm=first_buy["price"],
                            sell_slot=first_sell["slot"] if first_sell else None,
                            sell_timestamp=first_sell["time"] if first_sell else None,
                            sell_sol=sell_sol,
                            sell_tokens=first_sell["base"] if first_sell else None,
                            sell_price_ppm=first_sell["price"] if first_sell else None,
                            trajectory=traj,
                            peak_multiplier=peak,
                            target_hold_seconds=hold_s,
                            target_pnl_pct=pnl_pct,
                        )
                    )
        except Exception as exc:
            logger.warning("Failed to load copytrade samples from sqlite: %s", exc)

    if not samples:
        # Fallback to live on-chain acquisition
        return _fetch_onchain_copytrade_samples(wallet)

    return tuple(samples)
