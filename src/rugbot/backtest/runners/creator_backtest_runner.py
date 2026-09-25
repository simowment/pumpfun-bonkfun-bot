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

_FIXED_STOP_SCENARIO_WARNING = (
    "fixed-SL grid rows are scenario-only and not executable on rugs: "
    "there is no fill at the stop level; the observed (no-stop) model "
    "exits at the dev/bundle sell-leg print and is the primary EV."
)

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
    entry_basis: str = "unknown"


@dataclass(frozen=True, slots=True)
class CreatorTpSlEvaluation:
    tp_pct: float
    sl_pct: float | None
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
    entry_basis_counts: tuple[tuple[str, int], ...] = ()
    tp_only_evaluations: tuple[CreatorTpSlEvaluation, ...] = ()
    optimal_tp_observed: float | None = None
    optimal_ev_observed: float = 0.0
    exit_models: tuple[str, ...] = ("observed_no_stop", "scenario_fixed_stop")


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


def _simulate_observed_exit(
    sample: CreatorSample, tp_pct: float, config: CreatorBacktestConfig
) -> tuple[str, float, float]:
    """Simulate exit with no fixed stop (observed adverse print).

    If a trajectory point within ``max_hold_s`` reaches the TP multiplier,
    exit at TP (win). Otherwise exit at the observed adverse extreme — the
    minimum multiplier over points within ``max_hold_s`` (the real
    dev/bundle dump print / floor), floored at 0.01. Empty trajectories
    fall back to the ``ath_multiplier`` behaviour.
    """
    tp_mult = 1.0 + tp_pct / 100.0
    traj = sample.trajectory
    if not traj:
        ath = sample.ath_multiplier if sample.ath_multiplier is not None else 1.0
        if ath >= tp_mult:
            net, fees = _net_pnl_for_multiplier(tp_mult, config)
            return "win", net, fees
        exit_mult = max(0.01, ath)
        net, fees = _net_pnl_for_multiplier(exit_mult, config)
        return "loss", net, fees
    in_window = [(s, m) for s, m in traj if s <= config.max_hold_s]
    if not in_window:
        exit_mult = max(0.01, min(m for _, m in traj))
        net, fees = _net_pnl_for_multiplier(exit_mult, config)
        return "loss", net, fees
    for _, mult in sorted(in_window, key=lambda x: x[0]):
        if mult >= tp_mult:
            net, fees = _net_pnl_for_multiplier(tp_mult, config)
            return "win", net, fees
    exit_mult = max(0.01, min(m for _, m in in_window))
    net, fees = _net_pnl_for_multiplier(exit_mult, config)
    return "loss", net, fees


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


def _entry_basis_counts(
    samples: Sequence[CreatorSample],
) -> tuple[tuple[str, int], ...]:
    """Count samples per entry_basis label."""
    counts: dict[str, int] = {}
    for sample in samples:
        counts[sample.entry_basis] = counts.get(sample.entry_basis, 0) + 1
    return tuple(sorted(counts.items()))


def run_creator_tp_sl_grid_search(
    samples: Sequence[CreatorSample],
    config: CreatorBacktestConfig,
    target: str = "",
    mode: str = "wallet",
) -> CreatorBacktestReport:
    warnings: list[str] = []
    # leakage-safe sort
    sorted_samples = sorted(samples, key=lambda s: (s.created_at, s.created_slot))
    basis_counts = _entry_basis_counts(sorted_samples)
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
            entry_basis_counts=basis_counts,
        )
    evaluations: list[CreatorTpSlEvaluation] = []
    best_ev = float("-inf")
    best_tp: float | None = None
    best_sl: float | None = None
    if not any(s.trajectory or s.ath_multiplier is not None for s in sorted_samples):
        return CreatorBacktestReport(
            target=target,
            mode=mode,
            samples=tuple(sorted_samples),
            evaluations=(),
            optimal_tp=None,
            optimal_sl=None,
            optimal_ev=0.0,
            robust_zone=(),
            warnings=(),
            insufficient_data=True,
            message=(
                f"no usable price history: {len(sorted_samples)} samples lack "
                "trajectory and ATH (fail-closed)"
            ),
            entry_basis_counts=basis_counts,
        )
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
            if ev.sl_pct is not None and ev.net_ev_sol >= best_ev * 0.9:
                robust_zone.append((ev.tp_pct, ev.sl_pct))
    # observed (no-stop) TP-only model: primary EV, additive to SL grid.
    tp_only_evals: list[CreatorTpSlEvaluation] = []
    best_ev_obs = float("-inf")
    best_tp_obs: float | None = None
    for tp in config.tp_grid:
        wins = losses = 0
        net_pnls: list[float] = []
        fees_list: list[float] = []
        cum = 0.0
        peak = 0.0
        max_dd = 0.0
        for s in sorted_samples:
            outcome, net, fees = _simulate_observed_exit(s, tp, config)
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
        gross_pnl = sum(net_pnls)
        fees_sol = sum(fees_list)
        gross_pnl_sol = gross_pnl + fees_sol
        net_pnl = gross_pnl
        net_ev = net_pnl / total if total else 0.0
        invested = total * config.quote_size_sol
        net_roi = net_pnl / invested * 100 if invested else 0.0
        tp_only_evals.append(
            CreatorTpSlEvaluation(
                tp_pct=tp,
                sl_pct=None,
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
        if net_ev > best_ev_obs:
            best_ev_obs = net_ev
            best_tp_obs = tp
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
        warnings=tuple([*warnings, _FIXED_STOP_SCENARIO_WARNING]),
        insufficient_data=False,
        message="ok",
        entry_basis_counts=basis_counts,
        tp_only_evaluations=tuple(tp_only_evals),
        optimal_tp_observed=best_tp_obs,
        optimal_ev_observed=round(best_ev_obs, 6)
        if best_ev_obs != float("-inf")
        else 0.0,
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
        try:
            from rugbot.backtest.runners.entry_resolver import build_entry_sample
            from rugbot.integrations.pumpfun_api import get_client

            _entry_client = get_client()
        except Exception:
            _entry_client = None  # type: ignore[assignment]
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
                # Tiered entry: 1s candles, else on-chain early trades.
                # No reconstructable entry -> excluded, never a loss.
                if _entry_client is None:
                    continue
                try:
                    built = build_entry_sample(
                        mint,
                        creator=w,
                        created_at=ts,
                        created_slot=slot,
                        client=_entry_client,
                    )
                except Exception as exc2:  # noqa: BLE001
                    logger.debug("entry_sample failed for %s: %s", mint, exc2)
                    built = None
                if built is None:
                    continue
                samples.append(built)
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


def _creator_only_on_cex_source(
    target_wallet: str,
    wallets: list[str],
    funding_rows: list[dict[str, object]],
) -> list[str]:
    """Fall back to the creator wallet when the funding source is CEX-shaped.

    A funding source that creates nothing but pays many wallets is an
    exchange or shared hot wallet; its recipients are unrelated users and
    MUST NOT backtest as one entity. On any lookup failure the entity
    wallet set is returned unchanged (no fabricated attribution).

    Args:
        target_wallet: Creator wallet the entity was seeded from.
        wallets: Entity wallets resolved from the funding chain.
        funding_rows: Funding transfers observed around the entity.

    Returns:
        ``[target_wallet]`` when the primary funder is CEX-shaped, else
        ``wallets`` unchanged.
    """
    if len(wallets) <= 1 or not funding_rows:
        return wallets
    try:
        from collections import Counter

        from rugbot.integrations.pumpfun_creator_index import (
            fetch_pumpfun_created_tokens,
        )
        from rugbot.tracker.funding_chain import (
            enumerate_funded,
            is_cex_shaped_source,
        )

        funders = [
            str(row["from"])
            for row in funding_rows
            if isinstance(row.get("from"), str) and row.get("from") != "unknown"
        ]
        if not funders:
            return wallets
        source = Counter(funders).most_common(1)[0][0]
        source_creations = len(fetch_pumpfun_created_tokens(source))
        source_recipients = len({t.recipient for t in enumerate_funded(source)})
        if is_cex_shaped_source(
            source_creation_count=source_creations,
            source_recipient_count=source_recipients,
        ):
            logger.warning(
                "entity backtest fell back to creator-only: "
                "funding source %s is CEX-shaped",
                source[:8],
            )
            return [target_wallet]
    except Exception as exc:
        logger.debug("cex-shaped funding guard skipped: %s", exc)
    return wallets


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
    funding_rows: list[dict[str, object]] = []
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

    if entity:
        wallets = _creator_only_on_cex_source(target_wallet, wallets, funding_rows)

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

    # for each mint, resolve tiered entry (1s candles, else on-chain).
    # Samples without reconstructable entry are excluded, never losses.
    from rugbot.backtest.runners.entry_resolver import build_entry_sample
    from rugbot.integrations.pumpfun_api import get_client

    try:
        _live_client = get_client()
    except Exception:
        _live_client = None  # type: ignore[assignment]
    result: list[CreatorSample] = []
    for mint, cand in list(all_cands.items())[:MAX_SAMPLES_CAP]:
        created_at = int(getattr(cand, "created_timestamp", 0))
        if _live_client is None:
            continue
        try:
            built = build_entry_sample(
                mint,
                creator=target_wallet,
                created_at=created_at,
                created_slot=created_at,
                client=_live_client,
                rpc_url=rpc,
            )
        except Exception:
            built = None
        if built is None:
            continue
        result.append(built)
    # sort desc then cap
    result_sorted = sorted(result, key=lambda x: x.created_at, reverse=True)[
        :MAX_SAMPLES_CAP
    ]
    return tuple(result_sorted)
