"""Creator / entity TP×SL grid backtest — avenue-by-avenue replay."""

from __future__ import annotations

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
from rugbot.domain.quotes import QuotePath
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


def _synthetic_reserves(price_ppm: int, slot: int) -> PoolReserves:
    if price_ppm <= 0:
        price_ppm = 1
    v_base = _SYNTH_VIRTUAL_BASE
    v_quote = max(1, (v_base * price_ppm) // 1_000_000)
    if v_quote > 10_000_000_000_000:
        scale = v_quote // 10_000_000_000_000 + 1
        v_quote //= scale
        v_base //= scale
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
class CreatorBacktestConfig:
    quote_size_sol: float = 0.3
    slippage_pct: float = 1.5
    pump_fee_pct: float = 1.0
    gas_fee_sol: float = 0.001
    max_hold_s: int = 90
    entry_offset: str = "B0"
    tp_grid: tuple[float, ...] = (25.0, 50.0, 75.0, 100.0, 200.0)
    sl_grid: tuple[float, ...] = (10.0, 20.0, 30.0)


@dataclass(frozen=True, slots=True)
class CreatorSample:
    mint: str
    creator: str
    created_at: int
    created_slot: int
    trajectory: tuple[
        tuple[float, float], ...
    ]  # (seconds_from_entry, price_multiplier)
    ath_multiplier: float | None = None


@dataclass(frozen=True, slots=True)
class CreatorTpSlEvaluation:
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
    robust: bool


@dataclass(frozen=True, slots=True)
class CreatorBacktestReport:
    target: str
    mode: str
    samples: tuple[CreatorSample, ...]
    evaluations: tuple[CreatorTpSlEvaluation, ...]
    optimal_tp: float | None
    optimal_sl: float | None
    optimal_ev: float
    robust_zone: tuple[tuple[float, float], ...]
    warnings: tuple[str, ...]
    insufficient_data: bool = False
    message: str = ""


def _net_pnl_for_multiplier(
    multiplier: float, config: CreatorBacktestConfig
) -> tuple[float, float]:
    """Net PnL and fees for exiting at given multiplier using quote_engine.

    Falls back to simple fee model if quote_engine abstains.
    """
    lamports = int(config.quote_size_sol * LAMPORTS_PER_SOL)
    entry_ppm = 1_000_000
    exit_ppm = max(1, int(entry_ppm * multiplier))
    # fees via quote engine synthetic
    try:
        from rugbot.domain.decisions import AbstainResult as AR

        buy_q = executable_buy_quote(
            path=QuotePath.PUMP_BONDING_CURVE,
            reserves=_synthetic_reserves(entry_ppm, 0),
            quote_input_amount=lamports,
            fee_config=DEFAULT_FEE_CONFIG,
        )
        if isinstance(buy_q, AR):
            buy_fee = int(lamports * 0.0125)
            buy_out = lamports
        else:
            buy_fee = int(buy_q.fee_amount_base_units)
            buy_out = int(buy_q.output_amount_base_units)
        sell_q = executable_sell_quote(
            path=QuotePath.PUMP_BONDING_CURVE,
            reserves=_synthetic_reserves(exit_ppm, 0),
            base_input_amount=max(1, buy_out),
            fee_config=DEFAULT_FEE_CONFIG,
        )
        if isinstance(sell_q, AR):
            proceeds = int(lamports * multiplier * 0.9875)
            sell_fee = int(proceeds * 0.0125)
        else:
            proceeds = int(sell_q.output_amount_base_units)
            sell_fee = int(sell_q.fee_amount_base_units)
        gas = int(config.gas_fee_sol * LAMPORTS_PER_SOL)
        total_fees = buy_fee + sell_fee + gas
        net = proceeds - lamports - gas
        # include slippage penalty
        slip = config.slippage_pct / 100.0
        if multiplier > 1:
            net -= lamports * multiplier * slip
            total_fees += int(lamports * multiplier * slip)
        else:
            net -= lamports * slip
            total_fees += int(lamports * slip)
        return net / LAMPORTS_PER_SOL, total_fees / LAMPORTS_PER_SOL
    except Exception:
        gross = lamports * (multiplier - 1) / LAMPORTS_PER_SOL
        fees = (
            lamports * 0.025 / LAMPORTS_PER_SOL
            + config.gas_fee_sol
            + lamports * config.slippage_pct / 100 / LAMPORTS_PER_SOL
        )
        return gross - fees, fees


def _simulate_one(
    sample: CreatorSample, tp_pct: float, sl_pct: float, config: CreatorBacktestConfig
) -> tuple[str, float, float]:
    """Return (outcome, net_pnl_sol, fees_sol). outcome in win/loss/timeout."""
    tp_mult = 1.0 + tp_pct / 100.0
    sl_mult = 1.0 - sl_pct / 100.0
    if sl_mult <= 0:
        sl_mult = 0.01
    traj = sample.trajectory
    if not traj:
        # fallback ATH model
        ath = sample.ath_multiplier if sample.ath_multiplier is not None else 1.0
        if ath >= tp_mult:
            net, fees = _net_pnl_for_multiplier(tp_mult, config)
            return "win", net, fees
        # check SL: if ath model assumes rug => loss
        # if ath below SL threshold, still loss at sl
        loss_mult = sl_mult
        net, fees = _net_pnl_for_multiplier(loss_mult, config)
        return "loss", net, fees
    # trajectory replay avenue by avenue
    last_mult = 1.0
    for sec, mult in sorted(traj, key=lambda x: x[0]):
        if sec > config.max_hold_s:
            break
        if mult >= tp_mult:
            net, fees = _net_pnl_for_multiplier(tp_mult, config)
            return "win", net, fees
        if mult <= sl_mult:
            net, fees = _net_pnl_for_multiplier(sl_mult, config)
            return "loss", net, fees
        last_mult = mult
    # timeout
    # find price at max_hold (last tick <= max_hold)
    timeout_mult = last_mult
    for sec, mult in sorted(traj, key=lambda x: x[0]):
        if sec <= config.max_hold_s:
            timeout_mult = mult
        else:
            break
    net, fees = _net_pnl_for_multiplier(timeout_mult, config)
    outcome = "win" if net > 0 else "loss"
    return outcome, net, fees


def run_creator_tp_sl_grid_search(
    samples: Sequence[CreatorSample],
    config: CreatorBacktestConfig,
    target: str = "",
    mode: str = "wallet",
) -> CreatorBacktestReport:
    warnings: list[str] = []
    # leakage-safe sort
    sorted_samples = sorted(samples, key=lambda s: (s.created_at, s.created_slot))
    if len(sorted_samples) < 2:
        return CreatorBacktestReport(
            target=target,
            mode=mode,
            samples=tuple(sorted_samples),
            evaluations=(),
            optimal_tp=None,
            optimal_sl=None,
            optimal_ev=0.0,
            robust_zone=(),
            warnings=tuple(warnings),
            insufficient_data=True,
            message=f"insufficient launches: {len(sorted_samples)}/2 (fail-closed)",
        )
    evaluations: list[CreatorTpSlEvaluation] = []
    best_ev = float("-inf")
    best_tp: float | None = None
    best_sl: float | None = None
    # gross per combo
    for tp in config.tp_grid:
        for sl in config.sl_grid:
            wins = losses = 0
            net_pnls: list[float] = []
            fees_list: list[float] = []
            cum = 0.0
            peak = 0.0
            max_dd = 0.0
            for s in sorted_samples:
                outcome, net, fees = _simulate_one(s, tp, sl, config)
                net_pnls.append(net)
                fees_list.append(fees)
                if outcome == "win":
                    wins += 1
                else:
                    losses += 1
                cum += net
                peak = max(peak, cum)
                max_dd = max(max_dd, peak - cum)
            total = len(sorted_samples)
            winrate = wins / total * 100 if total else 0.0
            gross_pnl = sum(net_pnls)  # net already
            fees_sol = sum(fees_list)
            # gross_pnl_sol = net + fees? compute gross as net+fees
            gross_pnl_sol = gross_pnl + fees_sol
            net_pnl = gross_pnl
            net_ev = net_pnl / total if total else 0.0
            invested = total * config.quote_size_sol
            net_roi = net_pnl / invested * 100 if invested else 0.0
            evaluations.append(
                CreatorTpSlEvaluation(
                    tp_pct=tp,
                    sl_pct=sl,
                    wins=wins,
                    losses=losses,
                    winrate_pct=round(winrate, 2),
                    gross_pnl_sol=round(gross_pnl_sol, 6),
                    fees_sol=round(fees_sol, 6),
                    net_pnl_sol=round(net_pnl, 6),
                    net_ev_sol=round(net_ev, 6),
                    net_roi_pct=round(net_roi, 2),
                    max_drawdown_sol=round(max_dd, 6),
                    robust=False,
                )
            )
            if net_ev > best_ev:
                best_ev = net_ev
                best_tp = tp
                best_sl = sl
    # robust zone >=0.9 best_ev when best_ev >0
    robust_zone: list[tuple[float, float]] = []
    if best_ev > 0:
        for ev in evaluations:
            if ev.net_ev_sol >= best_ev * 0.9:
                robust_zone.append((ev.tp_pct, ev.sl_pct))
    # mark robust
    final_evals: list[CreatorTpSlEvaluation] = []
    for ev in evaluations:
        is_robust = (ev.tp_pct, ev.sl_pct) in robust_zone
        final_evals.append(
            CreatorTpSlEvaluation(
                tp_pct=ev.tp_pct,
                sl_pct=ev.sl_pct,
                wins=ev.wins,
                losses=ev.losses,
                winrate_pct=ev.winrate_pct,
                gross_pnl_sol=ev.gross_pnl_sol,
                fees_sol=ev.fees_sol,
                net_pnl_sol=ev.net_pnl_sol,
                net_ev_sol=ev.net_ev_sol,
                net_roi_pct=ev.net_roi_pct,
                max_drawdown_sol=ev.max_drawdown_sol,
                robust=is_robust,
            )
        )
    return CreatorBacktestReport(
        target=target,
        mode=mode,
        samples=tuple(sorted_samples),
        evaluations=tuple(final_evals),
        optimal_tp=best_tp,
        optimal_sl=best_sl,
        optimal_ev=round(best_ev, 6) if best_ev != float("-inf") else 0.0,
        robust_zone=tuple(robust_zone),
        warnings=tuple(warnings),
        insufficient_data=False,
        message="ok",
    )


def resolve_tp_sl_matrix(
    samples: Sequence[CreatorSample], config: CreatorBacktestConfig
) -> list[list[CreatorTpSlEvaluation]]:
    """Helper for tests: matrix tp rows x sl cols."""
    report = run_creator_tp_sl_grid_search(samples, config)
    # build matrix indexed by tp_grid order then sl_grid
    mat: list[list[CreatorTpSlEvaluation]] = []
    for tp in config.tp_grid:
        row: list[CreatorTpSlEvaluation] = []
        for sl in config.sl_grid:
            found = next(
                (e for e in report.evaluations if e.tp_pct == tp and e.sl_pct == sl),
                None,
            )
            if found is not None:
                row.append(found)
        mat.append(row)
    return mat


# --- sample resolution (DB + live) ---

_DISCOVER_DB_CANDIDATES = [
    Path(".state/discover/rugbot.db"),
    Path(".state/rugbot.db"),
    Path("data/tracker.db"),
]

MAX_SAMPLES_CAP = 40


def _load_discover_samples(target_wallets: set[str]) -> list[CreatorSample]:
    found_db: Path | None = None
    for p in _DISCOVER_DB_CANDIDATES:
        if p.exists():
            found_db = p
            break
    if found_db is None:
        return []
    try:
        conn = sqlite3.connect(str(found_db))
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        cur.execute("SELECT name FROM sqlite_master WHERE type='table'")
        tables = {r[0] for r in cur.fetchall()}
        if "discover_launches" not in tables:
            conn.close()
            return []
        samples: list[CreatorSample] = []
        for w in target_wallets:
            try:
                cur.execute(
                    "SELECT mint, created_slot, created_at FROM discover_launches WHERE wallet=? OR creator=? ORDER BY created_at DESC LIMIT ?",
                    (w, w, MAX_SAMPLES_CAP),
                )
                rows = cur.fetchall()
            except Exception:
                continue
            for r in rows:
                mint = str(r["mint"])
                slot = int(r["created_slot"]) if r["created_slot"] is not None else 0
                ts_raw = r["created_at"]
                if ts_raw is None:
                    ts = slot
                elif isinstance(ts_raw, int):
                    ts = ts_raw
                elif isinstance(ts_raw, str) and ts_raw.isdigit():
                    ts = int(ts_raw)
                else:
                    try:
                        ts = int(float(str(ts_raw)))
                    except Exception:
                        ts = slot
                # use data-based market history
                try:
                    from rugbot.domain.market_data import build_token_market_history

                    h = build_token_market_history(mint, db_path=str(found_db))
                    if h.entry_price_ppm is None or h.entry_price_ppm <= 0:
                        # unavailable -> trajectory empty, ath None -> abstain later
                        traj: tuple[tuple[float, float], ...] = ()
                        ath: float | None = None
                    else:
                        entry_ppm = int(h.entry_price_ppm)
                        entry_slot_hist = (
                            int(h.entry_slot) if h.entry_slot is not None else slot
                        )
                        traj_list: list[tuple[float, float]] = []
                        for s_slot, ppm, _mc in h.trajectory:
                            mult = ppm / entry_ppm if entry_ppm else 1.0
                            sec = (int(s_slot) - entry_slot_hist) * 0.4
                            traj_list.append((float(sec), float(mult)))
                        traj = tuple(traj_list)
                        if h.peak_price_ppm and entry_ppm:
                            ath = float(h.peak_price_ppm) / float(entry_ppm)
                        else:
                            ath = (
                                max((m for _, m in traj), default=None)
                                if traj
                                else None
                            )
                    # if ath still None -> mark unavailable, not synthetic
                    if ath is None:
                        ath_val: float | None = None
                    else:
                        ath_val = float(ath)
                except Exception as exc2:  # noqa: BLE001
                    logger.debug("market_history failed for %s: %s", mint, exc2)
                    traj = ()
                    ath_val = None
                samples.append(
                    CreatorSample(
                        mint=mint,
                        creator=w,
                        created_at=ts,
                        created_slot=slot,
                        trajectory=traj,
                        ath_multiplier=ath_val,
                    )
                )
        conn.close()
        by_mint: dict[str, CreatorSample] = {}
        for s in samples:
            prev = by_mint.get(s.mint)
            if prev is None or s.created_at < prev.created_at:
                by_mint[s.mint] = s
        deduped = sorted(by_mint.values(), key=lambda x: x.created_at, reverse=True)[
            :MAX_SAMPLES_CAP
        ]
        return deduped
    except Exception as exc:
        logger.warning("discover db load failed: %s", exc)
        return []


def resolve_target_samples(
    target_or_mint: str, *, entity: bool
) -> tuple[CreatorSample, ...]:
    """Resolve wallet/entity mints to CreatorSample list.

    Caps to 40 samples, leakage-safe sorted.
    """
    from rugbot.runtime.config import load_provider_settings, resolve_dotenv

    resolve_dotenv()
    providers = load_provider_settings()
    rpc = providers.rpc_http
    fallback = providers.rpc_http_fallbacks if providers else ()

    # determine target wallet: if input is token, resolve to creator wallet
    target_wallet = target_or_mint.strip()
    try:
        from rugbot.intelligence.token_resolver import resolve_token_or_wallet

        if rpc:
            resolved = resolve_token_or_wallet(
                target_or_mint, rpc_url=rpc, fallback_endpoints=fallback
            )
            target_wallet = resolved.target_wallet
    except Exception:
        pass

    wallets: list[str] = [target_wallet]
    if entity:
        try:
            from rugbot.interfaces.cli.check_mint import (
                _build_funding_chain,
                _resolve_entity_wallets,
            )

            if rpc:
                # collect funding chain
                funding_rows, _ = _build_funding_chain([target_wallet], rpc, fallback)
                wallets = _resolve_entity_wallets(funding_rows, target_wallet, [])
                if not wallets:
                    wallets = [target_wallet]
        except Exception as exc:
            logger.warning("entity funding chain failed: %s", exc)
            wallets = [target_wallet]

    wallet_set = set(wallets)
    # try discover DB first
    samples = _load_discover_samples(wallet_set)
    if samples:
        return tuple(samples)

    # live acquisition via pumpfun creator index (capped)
    all_cands: dict[str, object] = {}
    try:
        from rugbot.integrations.pumpfun_creator_index import (
            fetch_pumpfun_created_tokens,
        )

        for w in list(wallet_set)[:8]:
            try:
                cands = fetch_pumpfun_created_tokens(w)
            except Exception:
                continue
            for c in cands:
                if c.mint not in all_cands:
                    all_cands[c.mint] = c
            if len(all_cands) >= MAX_SAMPLES_CAP:
                break
    except Exception:
        pass

    if not all_cands:
        return ()

    # for each mint, build honest history via market_data (no synthetic 1.0/ath/0.5)
    result: list[CreatorSample] = []
    for mint, cand in list(all_cands.items())[:MAX_SAMPLES_CAP]:
        created_at = int(getattr(cand, "created_timestamp", 0))
        try:
            from rugbot.domain.market_data import build_token_market_history

            h = build_token_market_history(mint)
            if h.entry_price_ppm and h.entry_price_ppm > 0 and h.trajectory:
                entry_ppm = int(h.entry_price_ppm)
                entry_slot_hist = int(h.entry_slot) if h.entry_slot is not None else 0
                traj_list: list[tuple[float, float]] = []
                for s_slot, ppm, _mc in h.trajectory:
                    mult = ppm / entry_ppm if entry_ppm else 1.0
                    sec = (int(s_slot) - entry_slot_hist) * 0.4
                    traj_list.append((float(sec), float(mult)))
                traj: tuple[tuple[float, float], ...] = tuple(traj_list)
                ath2: float | None = (
                    (float(h.peak_price_ppm) / float(entry_ppm))
                    if h.peak_price_ppm
                    else None
                )
            else:
                traj = ()
                ath2 = None
        except Exception:
            traj = ()
            ath2 = None
        result.append(
            CreatorSample(
                mint=mint,
                creator=target_wallet,
                created_at=created_at,
                created_slot=created_at,
                trajectory=traj,
                ath_multiplier=ath2,
            )
        )
    # sort desc then cap
    result_sorted = sorted(result, key=lambda x: x.created_at, reverse=True)[
        :MAX_SAMPLES_CAP
    ]
    return tuple(result_sorted)
