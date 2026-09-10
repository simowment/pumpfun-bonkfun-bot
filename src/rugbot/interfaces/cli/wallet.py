"""Unified CLI for token/wallet resolution, cluster intelligence, and backtest optimization."""

# ruff: noqa: C901, PLR0912, PLR0915, PLR0911

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Final

from rugbot.backtest.runners.cluster_optimizer import (
    HistoricalTokenSample,
    run_cluster_tp_grid_search,
)
from rugbot.decision.lite_profiler import profile_launches
from rugbot.integrations.nansen_client import (
    NansenClient,
    NansenProviderError,
    counterparties_to_json,
)
from rugbot.integrations.pumpfun_api import PumpFunApiClient, get_client
from rugbot.intelligence.token_resolver import (
    fetch_token_metadata,
    resolve_token_or_wallet,
)
from rugbot.intelligence.wallet_intelligence import (
    WalletIntelligenceReport,
    abstention_to_json,
    scan_wallet_intelligence,
)
from rugbot.runtime.config import (
    TrackerDbPathError,
    load_provider_settings,
    resolve_dotenv,
    resolve_tracker_db_path,
)
from rugbot.storage.database import DatabaseManager
from rugbot.storage.tracker import SQLiteTrackerRepository
from rugbot.tracker.cluster_graph_model import build_cluster_intelligence_model
from rugbot.tracker.funder_discovery import (
    get_shared_rpc_cache,
    inbound_staged_to_json,
    outbound_staged_to_json,
    scan_inbound_staging,
    scan_outbound_staging,
)
from rugbot.tracker.models import (
    EntityGraphSnapshotRecord,
    FunderRecord,
    LaunchRecord,
    OperatorCandidateRecord,
    TargetExecutionMode,
    TargetExecutionPolicy,
    TransferRecord,
    WalletRecord,
    WalletStatus,
)
from rugbot.tracker.operator_graph import (
    find_operator_links,
    operator_links_to_json,
)
from rugbot.utils.logger import get_logger

if TYPE_CHECKING:
    from collections.abc import Sequence

resolve_dotenv()

logger = get_logger(__name__)

HIGH_ATH_CONSISTENCY_THRESHOLD: Final[float] = 70.0
MIN_REPEAT_COORDINATED_LAUNCHES: Final[int] = 2


def build_arg_parser() -> argparse.ArgumentParser:
    """Build the unified target intelligence and backtest command parser."""
    parser = argparse.ArgumentParser(
        description="Rugbot CLI: Resolve tokens/wallets, detect next staged deployers, run backtests, and enroll targets."
    )
    parser.add_argument(
        "target_pos",
        nargs="?",
        default=None,
        help="Token mint address or developer/funder wallet address.",
    )
    parser.add_argument(
        "--target",
        "-t",
        dest="target_opt",
        help="Token mint address or developer/funder wallet address.",
    )
    parser.add_argument(
        "--wallet",
        "-w",
        dest="wallet_opt",
        help="Alias for --target.",
    )
    parser.add_argument(
        "--backtest",
        "-b",
        action="store_true",
        help="Run analytical Take-Profit grid optimization and backtest on cluster launches.",
    )
    parser.add_argument(
        "--enroll",
        "-e",
        action="store_true",
        help="Enroll target and cluster into SQLite tracking repository.",
    )
    parser.add_argument(
        "--size",
        type=float,
        default=0.30,
        help="Simulated or trade buy size in SOL (default: 0.30 SOL).",
    )
    parser.add_argument(
        "--max-mc",
        type=float,
        default=10000.0,
        help="Max entry market cap in USD (default: $10,000).",
    )
    parser.add_argument(
        "--max-transactions",
        type=int,
        default=50,
        help="Max on-chain transaction history items to parse (default: 50).",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Output raw machine-readable JSON.",
    )
    parser.add_argument(
        "--pretty",
        action="store_true",
        default=True,
        help="Format human-readable terminal report (default: True).",
    )
    parser.add_argument(
        "--lite-profile",
        action="store_true",
        help="REST-only lite TP profile over recent launches (estimate).",
    )
    parser.add_argument(
        "--trace-funding",
        action="store_true",
        help="walk funding transfers for staged deployers (extra RPC calls)",
    )
    parser.add_argument(
        "--rpc",
        type=str,
        default=None,
        help="override SOLANA_RPC_HTTP for this call (top precedence).",
    )
    return parser


LITE_PROFILE_MAX_MINTS: Final[int] = 20
# Fixed round-trip cost estimate in basis points (not measured per trade).
LITE_PROFILE_FEE_BPS: Final[int] = 60
# USD threshold above which the MCAP line compacts values to $K.
USD_COMPACT_THOUSAND: Final[float] = 1000.0


def _format_usd_compact(usd_value: float) -> str:
    """Format a USD value compactly for the lite-profile MCAP line.

    Args:
        usd_value: Value in USD.

    Returns:
        Compact string such as ``"$3.5K"``, ``"$42.50"``, or ``"$0.5000"``.
    """
    if usd_value >= USD_COMPACT_THOUSAND:
        return f"${usd_value / USD_COMPACT_THOUSAND:.1f}K"
    if usd_value >= 1:
        return f"${usd_value:.2f}"
    return f"${usd_value:.4f}"


def _lite_mint_mcap_sol(
    token: object,
    candles: list[dict],
    sol_price: float,
) -> tuple[float, float] | None:
    """Return ``(entry, ATH)`` mcap in SOL for one mint.

    Scaling (pinned 2026-09-03 with live read-only GETs): swap-api candle
    prices are USD per token (last close x 1e9 equalled ``market_cap_usd``),
    so ``mcap_sol = price_usd * supply_tokens / sol_price`` with
    ``supply_tokens = total_supply / 10**base_decimals`` from ``fetch_token``.
    Entry uses the first-candle close (first buyable print; the open is the
    fixed curve-start constant). ATH is recomputed from max candle high
    because ``ath_market_cap`` units are ambiguous.

    Args:
        token: Prefetched token payload carrying ``total_supply`` and
            ``base_decimals`` (avoids a duplicate per-mint fetch).
        candles: Candle dicts carrying ``open``/``high``/``close`` prices.
        sol_price: USD per SOL from ``fetch_sol_price``.

    Returns:
        Entry/ATH mcap pair in SOL, or None when unknown (fail-soft).
    """
    if not isinstance(token, dict):
        return None
    supply_raw = token.get("total_supply", 0)
    decimals = token.get("base_decimals", 6)
    if isinstance(supply_raw, bool) or not isinstance(supply_raw, (int, float)):
        return None
    if isinstance(decimals, bool) or not isinstance(decimals, (int, float)):
        return None
    supply_tokens = float(supply_raw) / (10 ** int(decimals))
    if not supply_tokens > 0:
        return None
    try:
        entry_usd = float(candles[0].get("close"))
        peak_usd = max(float(candle.get("high")) for candle in candles)
    except (ValueError, TypeError, AttributeError, IndexError):
        return None
    if not (entry_usd > 0 and peak_usd > 0):
        return None
    entry_mcap = entry_usd * supply_tokens / sol_price
    ath_mcap = peak_usd * supply_tokens / sol_price
    return (entry_mcap, ath_mcap)


LITE_PROFILE_CANDLE_INTERVALS: Final[tuple[str, ...]] = ("1s", "1m", "5m", "1h")
LITE_PROFILE_WINDOW_GRACE_MS: Final[int] = 600_000


def _launch_window_is_valid(candles: list[dict], created_ms: object) -> bool:
    """Reject candle windows that are not the mint's launch window.

    The candle endpoint returns a *recent* window for tokens whose launch
    is older than its retention, i.e. a zero-volume dead tail. Profiling
    that tail reports a spurious 1.00x ATH because entry (first window
    close) equals the flat tail price. A window is trusted only when it
    carries real volume and starts at (or near) the mint's creation time.
    """
    if not candles:
        return False

    def _to_number(value: object) -> float | None:
        if isinstance(value, bool):
            return None
        if isinstance(value, (int, float)):
            return float(value)
        if isinstance(value, str):
            try:
                return float(value)
            except ValueError:
                return None
        return None

    total_volume = 0.0
    for candle in candles:
        volume = _to_number(candle.get("volume"))
        if volume is not None:
            total_volume += volume
    if total_volume <= 0:
        return False
    first_ts = _to_number(candles[0].get("timestamp"))
    created = _to_number(created_ms)
    if first_ts is not None and created is not None:
        if first_ts - created > LITE_PROFILE_WINDOW_GRACE_MS:
            return False
    return True


def _fetch_candles_with_fallback(
    client: PumpFunApiClient, mint: str
) -> tuple[list[dict], str | None]:
    """Fetch candles finest-first, falling back to coarser grains.

    The 1s endpoint retains only recent history; older launches return
    empty while 1m/5m/1h pages survive. Returns the first non-empty page
    with its interval, or ([], None) when every grain is empty.
    """
    for interval in LITE_PROFILE_CANDLE_INTERVALS:
        candles = client.fetch_candlesticks(mint, interval=interval, limit=300)
        if candles:
            return candles, interval
    return [], None


def _interval_mix_label(intervals: dict[str, str]) -> str:
    """Summarize candle granularity mix, e.g. ", 5mx4, 1hx2", or ""."""
    counts: dict[str, int] = {}
    for interval in intervals.values():
        counts[interval] = counts.get(interval, 0) + 1
    if not counts or set(counts) == {"1s"}:
        return ""
    parts = [
        f"{interval}x{counts[interval]}"
        for interval in LITE_PROFILE_CANDLE_INTERVALS
        if interval in counts
    ]
    return f", candles {', '.join(parts)}"


def _run_lite_profile(target_input: str) -> int:
    """Run the REST-only lite TP profile and print the compact table.

    Args:
        target_input: Token mint or creator wallet address.

    Returns:
        Process exit code (always 0; failures abstain without traceback).
    """
    try:
        client = get_client()
        token = client.fetch_token(target_input)
        creator = token.get("creator") if token else None
        creator_wallet = (
            str(creator) if isinstance(creator, str) and creator else target_input
        )
        page = client.fetch_user_created_coins(creator_wallet, limit=50, offset=0)
        coins = page.get("coins", []) if isinstance(page, dict) else []
        mints: list[str] = []
        for coin in coins:
            if not isinstance(coin, dict):
                continue
            mint = coin.get("mint") or coin.get("address")
            if isinstance(mint, str) and mint and mint not in mints:
                mints.append(mint)
            if len(mints) >= LITE_PROFILE_MAX_MINTS:
                break
        if not mints:
            print(f"Lite profile abstained: no launches for {creator_wallet}")
            return 0
        sol_quote = client.fetch_sol_price()
        sol_price = sol_quote.get("solPrice") if isinstance(sol_quote, dict) else None
        sol_usd = (
            float(sol_price)
            if isinstance(sol_price, (int, float)) and sol_price > 0
            else None
        )
        candles_by_mint: dict[str, list[dict]] = {}
        candle_interval_by_mint: dict[str, str] = {}
        mcap_sol_by_mint: dict[str, tuple[float, float]] = {}
        mcap_missing = 0
        stale_windows = 0
        for mint in mints:
            try:
                token = client.fetch_token(mint)
            except Exception:  # noqa: BLE001 — per-mint fetch is fail-soft
                token = None
            candles, interval = _fetch_candles_with_fallback(client, mint)
            created_ms = (
                token.get("created_timestamp") if isinstance(token, dict) else None
            )
            if interval is None or not _launch_window_is_valid(candles, created_ms):
                stale_windows += 1
                continue
            candles_by_mint[mint] = candles
            candle_interval_by_mint[mint] = interval
            if sol_usd is not None:
                pair = _lite_mint_mcap_sol(token, candles, sol_usd)
                if pair is None:
                    mcap_missing += 1
                else:
                    mcap_sol_by_mint[mint] = pair
            else:
                mcap_missing += 1
        report = profile_launches(
            candles_by_mint,
            fee_bps=LITE_PROFILE_FEE_BPS,
            mcap_sol_by_mint=mcap_sol_by_mint,
        )
        if report.launch_count == 0 or report.optimal_tp is None:
            stale_note = (
                f" ({stale_windows} stale/unavailable windows)" if stale_windows else ""
            )
            print(
                f"Lite profile abstained: no usable candles for "
                f"{creator_wallet}{stale_note}"
            )
            return 0
        optimal = report.optimal_tp
        mix_note = _interval_mix_label(candle_interval_by_mint)
        stale_note = f", {stale_windows} stale/unavailable" if stale_windows else ""
        print(
            f"Lite profile for {creator_wallet} "
            f"(N={report.launch_count}{mix_note}{stale_note})"
        )
        if report.mcap_scored_count and sol_usd:
            missing_note = f", {mcap_missing} without mcap" if mcap_missing else ""
            print(
                f"MCAP — entry avg "
                f"{_format_usd_compact(report.entry_mcap_sol_avg * sol_usd)} "
                f"(~{report.entry_mcap_sol_avg:.1f} SOL), ATH avg "
                f"{_format_usd_compact(report.ath_mcap_sol_avg * sol_usd)} "
                f"(~{report.ath_mcap_sol_avg:.1f} SOL), median "
                f"{_format_usd_compact(report.ath_mcap_sol_median * sol_usd)}, "
                f"max {_format_usd_compact(report.ath_mcap_sol_max * sol_usd)}, "
                f"min {_format_usd_compact(report.ath_mcap_sol_min * sol_usd)} "
                f"(N={report.mcap_scored_count} scored{missing_note})"
            )
        elif report.mcap_scored_count:
            # sol_price fetch failed after mcap stats were scored: keep the
            # SOL-only line rather than converting USD with a stale price.
            missing_note = f", {mcap_missing} without mcap" if mcap_missing else ""
            print(
                f"MCAP SOL — entry avg (1st-close) "
                f"{report.entry_mcap_sol_avg:.2f}, "
                f"min {report.entry_mcap_sol_min:.2f}, "
                f"max {report.entry_mcap_sol_max:.2f}; ATH avg "
                f"{report.ath_mcap_sol_avg:.2f}, "
                f"median {report.ath_mcap_sol_median:.2f}, "
                f"max {report.ath_mcap_sol_max:.2f}, "
                f"min {report.ath_mcap_sol_min:.2f} "
                f"(N={report.mcap_scored_count} scored{missing_note})"
            )
        else:
            print(
                f"ATH x — avg {report.ath_avg:.2f}, "
                f"median {report.ath_median:.2f}, "
                f"max {report.ath_max:.2f}, min {report.ath_min:.2f} "
                f"(N={report.launch_count} scored)"
            )
        exit_note = ""
        if report.entry_mcap_sol_avg > 0:
            exit_sol = optimal.tp_multiple * report.entry_mcap_sol_avg
            if sol_usd:
                exit_note = (
                    f" (~{_format_usd_compact(exit_sol * sol_usd)} at avg entry)"
                )
            else:
                exit_note = f" (~{exit_sol:.1f} SOL at avg entry)"
        qualify_note = (
            "QUALIFIED" if optimal.qualifies else "needs N>=10 — NOT QUALIFIED"
        )
        print(
            f"OPTIMAL TP — {optimal.tp_multiple:.2f}x{exit_note}, "
            f"winrate {optimal.winrate_pct:.0f}%, "
            f"EV {optimal.ev_multiple:+.2f}x, N={optimal.launch_count} "
            f"({qualify_note})"
        )
        print("ESTIMATE — not executable proof")
        return 0  # noqa: TRY300
    except Exception:  # noqa: BLE001
        print(f"Lite profile abstained: lookup failed for {target_input}")
        return 0


def _parse_timestamp(val: int | str | None, fallback: int) -> int:
    """Safely convert ISO strings or numeric timestamps to epoch seconds."""
    if val is None:
        return fallback
    if isinstance(val, (int, float)):
        return int(val)
    try:
        return int(datetime.fromisoformat(str(val)).timestamp())
    except (ValueError, TypeError):
        try:
            return int(float(str(val)))
        except (ValueError, TypeError):
            return fallback


def main(argv: Sequence[str] | None = None) -> int:
    """Execute the unified one-liner target analysis, discovery, and backtest workflow."""
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

    args = build_arg_parser().parse_args(argv)
    target_input = args.target_pos or args.target_opt or args.wallet_opt

    if not target_input:
        print(
            "Error: Target address (token mint or wallet) is required.", file=sys.stderr
        )
        print(
            "Usage: rug_wallet <TOKEN_MINT_OR_WALLET> [--backtest] [--enroll]",
            file=sys.stderr,
        )
        return 1

    target_input = target_input.strip()
    if args.lite_profile:
        return _run_lite_profile(target_input)
    providers = load_provider_settings()
    # Explicit CLI flag beats the saved file; the saved file beats any
    # inherited process default. See runtime.config.resolve_dotenv.
    endpoint = args.rpc or providers.rpc_http
    if endpoint is None:
        print("Error: SOLANA_RPC_HTTP is required.", file=sys.stderr)
        return 1
    try:
        db_path = resolve_tracker_db_path()
    except TrackerDbPathError as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1
    db_mgr = DatabaseManager(db_path)
    repo = SQLiteTrackerRepository(db_mgr)

    # 1. Resolve Token or Wallet on-chain
    resolved = resolve_token_or_wallet(
        target_input,
        rpc_url=endpoint,
        fallback_endpoints=providers.rpc_http_fallbacks,
    )
    wallet_address = resolved.target_wallet
    root_funder = resolved.root_funder or wallet_address
    now_iso = datetime.now(UTC).isoformat()
    now_ts = int(datetime.now(UTC).timestamp())

    # 1b. Funding-edge staging scans run BEFORE the heavy intelligence scan
    # below. The handoff proof is a few RPC calls away on a fresh quota
    # window, while the intelligence scan can exhaust burst quota first and
    # leave staging to fail-fast abstain on leftovers. Results are assigned
    # to the cluster model in 4b; every call is cached, so repeats are free.
    trace_enabled = bool(args.trace_funding)
    _staged_outbound: list = []
    _staged_inbound: list = []
    _outbound_warning: str | None = None
    _inbound_warning: str | None = None
    _staging_rpc_calls = 0
    if trace_enabled:
        try:
            _staged_outbound, _outbound_warning, _calls = scan_outbound_staging(
                wallet_address,
                endpoint,
                solscan_api_key=providers.solscan_api_key,
            )
            _staging_rpc_calls += _calls
        except Exception:  # noqa: BLE001 — staging scan is additive fail-soft
            _staged_outbound = []
        try:
            _staged_inbound, _inbound_warning, _calls = scan_inbound_staging(
                wallet_address,
                endpoint,
                solscan_api_key=providers.solscan_api_key,
            )
            _staging_rpc_calls += _calls
        except Exception:  # noqa: BLE001 — staging scan is additive fail-soft
            _staged_inbound = []

    # 2. Scan finalized on-chain wallet intelligence before mutating tracking.
    scan_target = root_funder if root_funder != wallet_address else wallet_address
    report = asyncio.run(
        scan_wallet_intelligence(
            scan_target,
            endpoint=endpoint,
            max_transactions=args.max_transactions,
            fallback_endpoints=providers.rpc_http_fallbacks,
        )
    )
    if not isinstance(report, WalletIntelligenceReport):
        payload = abstention_to_json(report)
        payload["enrolled"] = False
        payload["enrollment_rejection_reason"] = report.message
        if args.json:
            print(json.dumps(payload, indent=2))
        else:
            print(f"Wallet intelligence abstained: {payload['message']}")
        return 2
    target_label = resolved.default_label
    if isinstance(report, WalletIntelligenceReport) and report.repeat_bundler_entities:
        target_label = f"Repeat bundler {wallet_address[:6]}..."

    all_launches = (
        (*report.launches, *report.linked_launches)
        if isinstance(report, WalletIntelligenceReport)
        else ()
    )
    finalized_launch_mints = {launch.mint for launch in all_launches}
    repeat_bundler_mints = {
        mint
        for entity in (
            report.repeat_bundler_entities
            if isinstance(report, WalletIntelligenceReport)
            else ()
        )
        for mint in entity.mints
    }
    enrollment_eligible = (
        len(finalized_launch_mints) >= MIN_REPEAT_COORDINATED_LAUNCHES
        and len(repeat_bundler_mints) >= MIN_REPEAT_COORDINATED_LAUNCHES
    )
    enrolled = args.enroll and enrollment_eligible
    enrollment_rejection_reason = (
        None
        if not args.enroll or enrolled
        else "Finalized evidence did not prove at least two coordinated launches."
    )

    # 3. Persist only repeat coordinated operators explicitly requested for tracking.
    if enrolled:
        repo.save_funder(
            FunderRecord(
                id=None,
                address=root_funder,
                label=target_label,
                enabled=True,
                created_at=now_iso,
                last_seen_at=now_iso,
            )
        )

    if enrolled and isinstance(report, WalletIntelligenceReport):
        for row in report.transfers:
            tw = repo.get_wallet(row.target)
            if tw is None:
                repo.save_wallet(
                    WalletRecord(
                        address=row.target,
                        root_funder=root_funder,
                        parent_wallet=row.source,
                        depth=1 if row.target != root_funder else 0,
                        status=WalletStatus.FUNDED
                        if row.target != root_funder
                        else WalletStatus.FUNDER,
                        discovered_at=now_iso,
                        expires_at=None,
                        last_active_at=now_iso,
                    )
                )
            repo.save_transfer(
                TransferRecord(
                    signature=row.signature,
                    instruction_index=row.event_index,
                    slot=row.slot,
                    timestamp=row.timestamp if row.timestamp is not None else now_ts,
                    from_wallet=row.source,
                    to_wallet=row.target,
                    amount_lamports=row.amount_lamports,
                    amount_sol=row.amount_lamports / 1e9,
                    root_funder=root_funder,
                    depth=1 if row.target != root_funder else 0,
                )
            )

        for w_launch in all_launches:
            if repo.get_launch(w_launch.mint) is None:
                repo.save_launch(
                    LaunchRecord(
                        mint=w_launch.mint,
                        creator_wallet=w_launch.creator,
                        root_funder=root_funder,
                        symbol=w_launch.symbol,
                        name=w_launch.name,
                        created_signature=w_launch.signature,
                        created_slot=w_launch.slot,
                        created_at=now_iso,
                        depth=1 if w_launch.creator != root_funder else 0,
                        funding_signature=None,
                        funding_amount_lamports=None,
                        funding_timestamp=None,
                    )
                )

    # 4. Build Cluster Intelligence Model
    model = build_cluster_intelligence_model(repo, root_funder, target_label)

    # 4b. Staging assignment (scans already ran in 1b on fresh quota).
    # Opt-in via --trace-funding only; the default path issues no staging
    # RPC calls. The operator graph stays here: it is REST-based and
    # unaffected by RPC burst quota.
    staging_skipped = not trace_enabled
    staging_warning = _outbound_warning or _inbound_warning
    staging_rpc_calls = _staging_rpc_calls
    operator_linked: list[dict[str, object]] = []
    operator_graph_warning: str | None = None
    operator_graph_calls = 0
    nansen_linked: list[dict[str, object]] = []
    nansen_warning: str | None = None
    nansen_calls = 0
    if staging_skipped:
        model.outbound_staged = []
        model.inbound_staged = []
    else:
        model.outbound_staged = outbound_staged_to_json(_staged_outbound)
        model.inbound_staged = inbound_staged_to_json(_staged_inbound)
        try:
            graph_client = PumpFunApiClient(page_cache=get_shared_rpc_cache())
            operator_links, operator_graph_warning, operator_graph_calls = (
                find_operator_links(wallet_address, client=graph_client)
            )
            operator_linked = operator_links_to_json(operator_links)
        except Exception:  # noqa: BLE001 — operator graph is additive fail-soft
            operator_linked = []
            operator_graph_warning = "operator graph failed"
        try:
            nansen_key = providers.nansen_api_key
            if not nansen_key:
                nansen_warning = "nansen API key required for counterparties"
            else:
                end_day = datetime.now(UTC).date()
                start_day = end_day - timedelta(days=7)
                nansen_page = NansenClient(nansen_key).counterparties(
                    wallet_address,
                    date_from=start_day.isoformat(),
                    date_to=end_day.isoformat(),
                )
                nansen_linked = counterparties_to_json(nansen_page.counterparties)
                nansen_calls = 1
        except NansenProviderError as exc:
            nansen_warning = f"nansen counterparties failed: {type(exc).__name__}"
        except Exception:  # noqa: BLE001 — nansen pass is additive fail-soft
            nansen_linked = []
            nansen_warning = "nansen counterparties failed"

    # 5. Run Cluster Backtest & Optimizer if requested
    cluster_launches = repo.get_launches_for_funder(root_funder)
    if not cluster_launches and wallet_address != root_funder:
        cluster_launches = repo.get_launches_for_funder(wallet_address)

    backtest_report = None
    if args.backtest or len(cluster_launches) > 0:
        samples = []
        for launch_rec in cluster_launches:
            _name, sym, mc, ath = fetch_token_metadata(launch_rec.mint)
            samples.append(
                HistoricalTokenSample(
                    mint=launch_rec.mint,
                    symbol=sym,
                    creator_wallet=launch_rec.creator_wallet,
                    created_slot=launch_rec.created_slot,
                    created_at=_parse_timestamp(
                        launch_rec.created_at or launch_rec.funding_timestamp,
                        now_ts,
                    ),
                    ath_multiplier=ath,
                    ath_delay_seconds=45,
                    rug_delay_seconds=90,
                    entry_mc_usd=5000.0,
                    peak_mc_usd=mc,
                    is_bundle_b0=True,
                    bundle_sol=(
                        launch_rec.funding_amount_lamports / 1e9
                        if launch_rec.funding_amount_lamports
                        else 0.5
                    ),
                )
            )

        if samples:
            backtest_report = run_cluster_tp_grid_search(
                root_funder=root_funder,
                samples=samples,
                buy_size_sol=args.size,
                gas_fee_sol=0.001,
                jito_tip_sol=0.002,
            )

    # 6. Enroll policy if requested
    if enrolled:
        optimal_tp_multiplier = (
            backtest_report.optimal_tp_multiplier if backtest_report else None
        )
        optimal_tp_ppm = (
            int((optimal_tp_multiplier - 1.0) * 1_000_000)
            if optimal_tp_multiplier is not None
            else 1_000_000
        )
        repo.save_target_execution_policy(
            TargetExecutionPolicy(
                funder_address=root_funder,
                monitoring_enabled=True,
                execution_mode=TargetExecutionMode.SIMULATED,
                quote_size_lamports=int(args.size * 1e9),
                take_profit_pnl_ppm=optimal_tp_ppm,
                stop_loss_pnl_ppm=-200_000,
                max_slippage_bps=500,
                priority_fee_microlamports=50_000,
                jito_tip_lamports=2_000_000,
                updated_at=now_iso,
            )
        )

    # 7. Output Format
    optimal_eval = backtest_report.optimal_evaluation if backtest_report else None
    optimal_winrate = optimal_eval.winrate_pct if optimal_eval else None
    total_fees = optimal_eval.total_fees_paid_sol if optimal_eval else None

    out_dict = {
        "input": target_input,
        "resolved_creator": wallet_address,
        "root_funder": root_funder,
        "is_token": resolved.is_token,
        "cluster_wallets": model.total_wallets,
        "cluster_tokens": model.token_count,
        "staged_wallets_count": model.staged_wallets_count,
        "next_deployer_candidate": model.next_deployer_candidate,
        "outbound_staged": model.outbound_staged,
        "inbound_staged": model.inbound_staged,
        "staging_skipped": staging_skipped,
        "staging_warning": staging_warning,
        "staging_rpc_calls": staging_rpc_calls,
        "operator_linked_wallets": operator_linked,
        "operator_graph_warning": operator_graph_warning,
        "operator_graph_calls": operator_graph_calls,
        "nansen_counterparties": nansen_linked,
        "nansen_warning": nansen_warning,
        "nansen_calls": nansen_calls,
        "next_deployer_funding_sol": model.next_deployer_funding_sol,
        "enrolled": enrolled,
        "enrollment_rejection_reason": enrollment_rejection_reason,
        "finalized_pump_trades": (
            [
                {
                    "slot": trade.slot,
                    "signature": trade.signature,
                    "mint": trade.mint,
                    "side": trade.side.value,
                }
                for trade in report.trades
            ]
            if isinstance(report, WalletIntelligenceReport)
            else []
        ),
        "repeat_bundler_entities": (
            [
                {
                    "bundler_wallet": entity.bundler_wallet,
                    "entity_creator": entity.entity_creator,
                    "mints": list(entity.mints),
                    "mint_count": len(entity.mints),
                    "buy_count": entity.buy_count,
                    "first_buy_slot": entity.first_buy_slot,
                    "last_buy_slot": entity.last_buy_slot,
                    "evidence_ids": list(entity.evidence_ids),
                    "finalized_entity_attribution": True,
                }
                for entity in report.repeat_bundler_entities
            ]
            if isinstance(report, WalletIntelligenceReport)
            else []
        ),
        "operator_dynamics": (
            {
                "avg_ath_multiplier": backtest_report.avg_ath_multiplier,
                "median_ath_multiplier": backtest_report.median_ath_multiplier,
                "ath_consistency_pct": backtest_report.ath_consistency_pct,
                "avg_peak_mc_usd": backtest_report.avg_peak_mc_usd,
                "avg_rug_mc_usd": backtest_report.avg_rug_mc_usd,
                "avg_rug_delay_seconds": backtest_report.avg_rug_delay_seconds,
                "median_rug_delay_seconds": backtest_report.median_rug_delay_seconds,
                "rug_delay_std_seconds": backtest_report.rug_delay_std_seconds,
                "avg_ath_delay_seconds": backtest_report.avg_ath_delay_seconds,
            }
            if backtest_report
            else None
        ),
        "backtest": (
            {
                "total_tokens_evaluated": backtest_report.total_tokens_evaluated,
                "optimal_tp": backtest_report.optimal_tp_label,
                "optimal_tp_multiplier": backtest_report.optimal_tp_multiplier,
                "winrate_pct": optimal_winrate,
                "net_roi_pct": backtest_report.optimal_roi_pct,
                "net_ev_sol": backtest_report.optimal_net_ev_sol,
                "avg_ath_multiplier": backtest_report.avg_ath_multiplier,
                "total_fees_sol": total_fees,
            }
            if backtest_report
            else None
        ),
    }
    if not staging_skipped:
        try:
            repo.save_entity_graph(
                EntityGraphSnapshotRecord(
                    wallet=wallet_address,
                    query=target_input,
                    graph_json=json.dumps(out_dict, default=str),
                    created_at=now_iso,
                    updated_at=now_iso,
                )
            )
        except Exception as exc:  # noqa: BLE001 — snapshot save never breaks output
            logger.warning("entity graph snapshot save failed: %s", type(exc).__name__)
    if operator_linked:
        try:
            repo.save_operator_candidates(
                [
                    OperatorCandidateRecord(
                        wallet=str(link["wallet"]),
                        source_entity=wallet_address,
                        created_count=int(link.get("created_count") or 0),
                        first_seen_at=now_iso,
                        last_seen_at=now_iso,
                    )
                    for link in operator_linked
                    if link.get("wallet") and (link.get("created_count") or 0) >= 1
                ]
            )
        except Exception as exc:  # noqa: BLE001 — candidate save never breaks output
            logger.warning("operator candidate save failed: %s", type(exc).__name__)
    if args.json:
        print(json.dumps(out_dict, indent=2))
        return 2 if args.enroll and not enrolled else 0

    # Pretty Terminal Presentation
    print("\n" + "=" * 78)
    print(" 🎯 RUGBOT TARGET & CLUSTER SNIPING INTELLIGENCE")
    print("=" * 78)
    print(f" Input Target:          {target_input}")
    if resolved.is_token:
        print(f" Resolved Token:        {resolved.name} (${resolved.symbol})")
        print(f" Creator Wallet:        {wallet_address}")
    print(f" Root Funding Auth:     {root_funder}")
    print(f" Connected Wallets:     {model.total_wallets}")
    print(f" Cluster Token Mints:   {model.token_count}")
    print(f" Staged Clean Wallets:  {model.staged_wallets_count}")

    if isinstance(report, WalletIntelligenceReport) and report.repeat_bundler_entities:
        print("\n 🎯 REPEAT BUNDLER EVIDENCE:")
        for entity in report.repeat_bundler_entities:
            print(
                f"   • Bundler {entity.bundler_wallet[:10]}... for "
                f"entity {entity.entity_creator[:10]}...: "
                f"{len(entity.mints)} mints / {entity.buy_count} finalized buys"
            )

    print("\n" + "-" * 78)
    if model.next_deployer_candidate:
        print(" 🔥 PREDICTED NEXT DEPLOYER / SNIPER TARGET:")
        print(f"   • Address:        {model.next_deployer_candidate}")
        print(
            f"   • Staged Balance: {model.next_deployer_funding_sol:.3f} SOL (Awaiting pump::create)"
        )
        print("   • Status:         ● ARMED - Live listener active on creator address")
    else:
        print(" 🎯 NEXT DEPLOYER STATUS:")
        print("   • No unspent fresh burner wallet currently staged in cluster.")
    print("-" * 78)

    if model.discovered_wallets:
        print("\n 📋 DISCOVERED WALLETS & CLUSTER NODES:")
        print(
            f"   {'WALLET':<14} | {'ROLE / STATUS':<18} | {'FUNDED':<10} | {'MINTS':<6} | {'PROB':<5}"
        )
        print("   " + "-" * 62)
        for w in model.discovered_wallets[:8]:
            print(
                f"   {w.address[:10] + '...':<14} | {w.behavior_str:<18} | {w.direct_funding_sol:6.3f} SOL | {w.mints_count:<6} | {w.deploy_probability_pct:>3}%"
            )

    if backtest_report:
        print("\n" + "-" * 78)
        print(
            f" 🕒 OPERATOR TIMING & RUG DYNAMICS ({backtest_report.total_tokens_evaluated} Launches Analyzed):"
        )
        print(
            f"   • Average ATH:          {backtest_report.avg_ath_multiplier:.2f}x (Median: {backtest_report.median_ath_multiplier:.2f}x, Dispersion: ±{backtest_report.ath_std_dev:.2f}x)"
        )
        print(
            f"   • ATH Consistency:      {backtest_report.ath_consistency_pct:.1f}% ({'High consistency' if backtest_report.ath_consistency_pct >= HIGH_ATH_CONSISTENCY_THRESHOLD else 'Variable ATH multiple'})"
        )

        print(
            f"   • Average Time to ATH:  {backtest_report.avg_ath_delay_seconds:.0f}s"
        )
        print(
            f"   • Average Time to Rug:  {backtest_report.avg_rug_delay_seconds:.0f}s (Median: {backtest_report.median_rug_delay_seconds:.0f}s, Variance: ±{backtest_report.rug_delay_std_seconds:.0f}s)"
        )
        print(f"   • Average Peak MC:      ${backtest_report.avg_peak_mc_usd:,.0f}")
        print(
            f"   • Average Rug Exit MC:  ${backtest_report.avg_rug_mc_usd:,.0f} (Dev exit window: {backtest_report.avg_ath_delay_seconds:.0f}s - {backtest_report.avg_rug_delay_seconds:.0f}s)"
        )
        if backtest_report.avg_inter_launch_minutes > 0:
            print(
                f"   • Launch Cadence:       ~{backtest_report.avg_inter_launch_minutes:.0f}m between tokens (Fastest burst: {backtest_report.min_inter_launch_minutes:.0f}m)"
            )

        print("\n" + "-" * 78)
        print(
            f" 📊 ANALYTICAL BACKTEST & TP OPTIMIZER ({backtest_report.total_tokens_evaluated} Launches Evaluated):"
        )
        print(
            "   • Optimal Take-Profit:  "
            + (
                f"{backtest_report.optimal_tp_label} "
                f"(x{backtest_report.optimal_tp_multiplier:.2f})"
                if backtest_report.optimal_tp_multiplier is not None
                else f"{backtest_report.optimal_tp_label} (no profitable target)"
            )
        )
        print(
            "   • Historical Win Rate:  "
            + (
                f"{optimal_winrate:.1f}%"
                if optimal_winrate is not None
                else "unmeasured (no profitable TP row)"
            )
        )
        print(f"   • Net Simulated ROI:    {backtest_report.optimal_roi_pct:+.1f}%")
        print(
            f"   • Expected Value (EV):  {backtest_report.optimal_net_ev_sol:+.4f} SOL / trade"
        )
        print(
            "   • Fee Breakdown:        "
            f"{backtest_report.jito_tip_sol:.4f} SOL Jito tip · "
            f"{backtest_report.gas_fee_sol:.4f} SOL gas/priority · "
            f"{backtest_report.dex_fee_pct:.2f}% DEX per leg"
        )
        print(
            "   • Total Fees Deducted:  "
            + (
                f"{total_fees:.4f} SOL"
                if total_fees is not None
                else "unmeasured (no profitable TP row)"
            )
        )
        print(
            f"   • Bible Qualified:      {'✅ YES' if backtest_report.is_bible_qualified else '❌ NO'} ({backtest_report.qualification_reason})"
        )

    if enrolled:
        print("\n" + "=" * 78)
        print("  TARGET AND POLICY ENROLLED IN TRACKER DATABASE")
        print("  Launch `uv run rug_tui` to monitor live.")
        print("=" * 78)

    if enrollment_rejection_reason is not None:
        print("\n" + "=" * 78)
        print("  TARGET NOT ENROLLED")
        print(f"  {enrollment_rejection_reason}")
        print("=" * 78)

    print()
    return 2 if args.enroll and not enrolled else 0


if __name__ == "__main__":
    raise SystemExit(main())
