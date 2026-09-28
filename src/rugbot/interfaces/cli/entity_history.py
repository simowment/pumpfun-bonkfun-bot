"""CLI: reconstruct an entity's token-creation history from a funder.

Pages a funding wallet's disbursements backward through history, filters to
the staging band, then resolves every funded wallet's creations into a single
token timeline. This is the command that answers "how many tokens did this
operator create" for burner-per-launch entities, whose history is invisible
to any per-wallet view.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import statistics
import sys
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from rugbot.backtest.launch_replay import (
    SIGNAL_SELL_RULE,
    LaunchReplay,
    LaunchReplayError,
    ReplayCosts,
    RuleSummary,
    describe_exit_rule,
    describe_take_profit_sweep,
    summarize_rules,
    take_profit_rules,
    trades_from_swap_api,
)
from rugbot.backtest.reporting.launch_chart import PLOTS_DIR, write_launch_charts
from rugbot.backtest.reporting.visualizer import (
    TradePerformanceRecord,
    export_vectorbt_html_report,
)
from rugbot.domain.pump_curve import nonstandard_curve_reason
from rugbot.integrations.pumpfun_api import PumpFunApiError, get_client
from rugbot.tracker.entity_history import (
    EntityLaunchHistory,
    LaunchActivity,
    LaunchEvent,
    build_launch_history,
    creator_launch_history,
    launch_activity,
    merge_launch_histories,
)
from rugbot.tracker.funder_discovery import STAGED_MAX_SOL, STAGED_MIN_SOL
from rugbot.tracker.funding_chain import (
    DEFAULT_HISTORY_PAGES,
    DEFAULT_HISTORY_TRANSACTIONS,
    RELAY_MAX_HOPS,
    FundedTransfer,
    FundingChainError,
    descend_to_creators,
    enumerate_funded_paged,
)
from rugbot.utils.logger import get_logger

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

logger = get_logger(__name__)
DESCENT_WORKERS = 4

TRADE_FETCH_WORKERS = 3
BIBLE_MIN_SAMPLES = 10


def _build_parser() -> argparse.ArgumentParser:
    """Build the argument parser for the entity-history command."""
    parser = argparse.ArgumentParser(
        prog="rug_intel history",
        description=(
            "Reconstruct the token-creation timeline of the wallets a funder "
            "disbursed staging capital to."
        ),
    )
    parser.add_argument(
        "funders", nargs="+", help="Funding wallets to page backward from."
    )
    pages_group = parser.add_mutually_exclusive_group()
    pages_group.add_argument(
        "--pages",
        type=int,
        default=DEFAULT_HISTORY_PAGES,
        help=f"Signature pages to walk back (default: {DEFAULT_HISTORY_PAGES}).",
    )
    pages_group.add_argument(
        "--all-pages",
        action="store_true",
        help="Walk the cursor to exhaustion, still bounded by --max-tx.",
    )
    parser.add_argument(
        "--max-tx",
        type=int,
        default=DEFAULT_HISTORY_TRANSACTIONS,
        help=(
            "Maximum transactions hydrated across pages "
            f"(default: {DEFAULT_HISTORY_TRANSACTIONS})."
        ),
    )
    parser.add_argument(
        "--min-sol",
        type=float,
        default=STAGED_MIN_SOL,
        help=f"Lower staging-band bound in SOL (default: {STAGED_MIN_SOL}).",
    )
    parser.add_argument(
        "--max-sol",
        type=float,
        default=STAGED_MAX_SOL,
        help=f"Upper staging-band bound in SOL (default: {STAGED_MAX_SOL}).",
    )
    parser.add_argument(
        "--slot-from",
        type=int,
        default=None,
        help="Only hydrate signatures at or after this slot (cheap deep scan).",
    )
    parser.add_argument(
        "--slot-to",
        type=int,
        default=None,
        help="Only hydrate signatures at or before this slot.",
    )
    parser.add_argument(
        "--hops",
        type=int,
        default=RELAY_MAX_HOPS,
        help=(
            "Hops followed below each funded wallet through wallets it funded "
            f"from a zero balance, to reach the creators (default: {RELAY_MAX_HOPS})."
        ),
    )
    parser.add_argument(
        "--creator",
        action="store_true",
        help="Treat the addresses as serial creator wallets (Type 1) instead of funders.",
    )
    parser.add_argument(
        "--backtest",
        action="store_true",
        help="Replay every launch from its full trade history and rank exit rules.",
    )
    parser.add_argument(
        "--entry-delay",
        type=int,
        default=ReplayCosts().entry_delay_slots,
        help="Slots after create our buy lands (0 = block 0).",
    )
    parser.add_argument(
        "--size",
        type=float,
        default=ReplayCosts().quote_size_sol,
        help="Buy size in SOL per launch.",
    )
    parser.add_argument(
        "--plot",
        action="store_true",
        help=f"Write the best rule's equity report and per-launch candle charts "
        f"under {PLOTS_DIR}/.",
    )
    parser.add_argument("--json", action="store_true", help="Emit JSON only.")
    return parser


def _launch_fetch(wallet: str) -> list[object] | None:
    """Fetch one wallet's creator-index coin page."""
    page = get_client().fetch_user_created_coins(wallet, limit=50, offset=0)
    if not isinstance(page, dict):
        return None
    coins = page.get("coins")
    return coins if isinstance(coins, list) else None


def _with_descendant_creators(
    transfers: Sequence[FundedTransfer], *, max_hops: int
) -> tuple[list[FundedTransfer], int]:
    """Add every creator found below each funded wallet, as if funded directly.

    Each funded wallet stays in the list (its own coins come from the creator
    index); creators found hops below it inherit its funding transfer.

    Returns:
        ``(transfers, descendants)`` where ``descendants`` counts the creators
        found below a funded wallet rather than at it.
    """
    first: dict[str, FundedTransfer] = {}
    for transfer in transfers:
        first.setdefault(transfer.recipient, transfer)
    with ThreadPoolExecutor(max_workers=DESCENT_WORKERS) as pool:
        descents = dict(
            zip(
                first,
                pool.map(
                    lambda wallet: descend_to_creators(wallet, max_hops=max_hops),
                    first,
                ),
                strict=True,
            )
        )
    added = [
        dataclasses.replace(first[root], recipient=creator)
        for root, descent in descents.items()
        for creator, hops in descent.creators.items()
        if hops > 0
    ]
    for root, descent in descents.items():
        if descent.truncated:
            logger.warning("%s: descent truncated at wallet cap", root[:8])
    return [*transfers, *added], len(added)


def _format_when(created_at_ms: int | None) -> str:
    """Render an epoch-millisecond stamp as UTC, or a dash when absent."""
    if created_at_ms is None:
        return "unknown time"
    return datetime.fromtimestamp(created_at_ms / 1000, tz=UTC).strftime(
        "%Y-%m-%d %H:%M UTC"
    )


def _as_payload(history: EntityLaunchHistory, scanned: int) -> dict[str, object]:
    """Serialize the history into a JSON-safe mapping."""
    return {
        "funders": list(history.funders),
        "transfers_scanned": scanned,
        "recipients": history.recipients,
        "tokens_created": len(history.launches),
        "warning": history.warning,
        "launches": [
            {
                "mint": event.mint,
                "symbol": event.symbol,
                "name": event.name,
                "creator": event.creator,
                "funder": event.funder,
                "created_at_ms": event.created_at_ms,
                "created_at": _format_when(event.created_at_ms),
                "received_sol": event.received_sol,
                "funding_slot": event.funding_slot,
            }
            for event in history.launches
        ],
    }


def _render(history: EntityLaunchHistory, scanned: int) -> None:
    """Print the human-readable token-creation timeline."""
    print("=" * 78)
    print(" ENTITY TOKEN-CREATION HISTORY")
    print("=" * 78)
    print(f" funders: {', '.join(history.funders)}")
    print(f" transfers scanned: {scanned}   recipients: {history.recipients}")
    print(f" tokens created: {len(history.launches)}")
    if not history.launches:
        print("\n no token creations found among the funded wallets")
    else:
        print()
        for event in history.launches:
            print(
                f"   {_format_when(event.created_at_ms):<20} "
                f"{event.symbol or '(no symbol)':<14} {event.mint}"
            )
            print(
                f"      creator {event.creator}   funder {event.funder}   "
                f"received {event.received_sol:.4f} SOL   "
                f"funding slot {event.funding_slot}"
            )
    if history.warning:
        print(f"\n note: {history.warning}")


def _render_activity(activity: LaunchActivity) -> None:
    """Print whether the entity is still launching."""
    if activity.last_launch_s is None:
        return
    last = datetime.fromtimestamp(activity.last_launch_s, tz=UTC)
    cadence = (
        f"{activity.median_interval_s / 60:.1f} min"
        if activity.median_interval_s is not None
        else "n/a"
    )
    print(
        f"\n activity: {'ACTIVE' if activity.active else 'DORMANT'}   "
        f"last launch {last:%Y-%m-%d %H:%M UTC}   "
        f"launches in last 7d: {activity.launches_last_7d}   "
        f"median interval: {cadence}"
    )


def _replays(
    history: EntityLaunchHistory, costs: ReplayCosts
) -> tuple[list[LaunchReplay], list[str]]:
    """Fetch full trade histories concurrently and build replays.

    Failures are reported per launch, never dropped silently. Concurrency is
    bounded because pump.fun rate-limits the trades endpoint.
    """
    client = get_client()

    def build(event: LaunchEvent) -> LaunchReplay | str:
        reason = nonstandard_curve_reason(event.curve_invariant, mayhem=event.mayhem)
        if reason is not None:
            return f"{event.symbol or event.mint[:8]}: {reason}"
        try:
            trades = trades_from_swap_api(client.fetch_all_trades(event.mint))
            return LaunchReplay(
                event.mint,
                create_slot=trades[0].slot,
                creator=event.creator,
                trades=trades,
                costs=costs,
            )
        except (PumpFunApiError, LaunchReplayError, IndexError, OSError) as error:
            return f"{event.symbol or event.mint[:8]}: {error}"

    with ThreadPoolExecutor(max_workers=TRADE_FETCH_WORKERS) as pool:
        outcomes = list(pool.map(build, history.launches))
    replays = [outcome for outcome in outcomes if isinstance(outcome, LaunchReplay)]
    skipped = [outcome for outcome in outcomes if isinstance(outcome, str)]
    return replays, skipped


def _render_backtest(
    replays: list[LaunchReplay], summaries: list[RuleSummary], skipped: list[str]
) -> None:
    """Print launch profiles and the best exit rules by net EV."""
    print("\n BACKTEST")
    if not replays:
        print("   no replayable launches")
    else:
        costs = replays[0].costs
        profiles = [replay.profile for replay in replays]
        insider = [p.first_insider_sell_s for p in profiles if p.first_insider_sell_s]
        print(
            f"   entry: block +{costs.entry_delay_slots}, {costs.quote_size_sol} SOL, "
            f"exit fills {costs.reaction_slots} slots after trigger"
        )
        print(
            f"   launches {len(profiles)}   median entry MC "
            f"{statistics.median(p.entry_mc_sol for p in profiles):.1f} SOL   "
            f"median ATH {statistics.median(p.ath_multiple for p in profiles):.2f}x   "
            f"max ATH {max(p.ath_multiple for p in profiles):.2f}x"
        )
        print(
            f"   median time to ATH "
            f"{statistics.median(p.seconds_to_ath for p in profiles):.0f}s   "
            f"graduated {sum(p.graduated for p in profiles)}   median first dev/"
            f"bundle sell {statistics.median(insider) if insider else 'n/a'}s"
        )
        print(
            "\n   best exit rules, ranked by conservative EV (winrate at its 95%"
            " lower bound; SOL per trade after pump fees + tx costs):"
        )
        shown: set[tuple[float, ...]] = set()
        distinct = []
        for summary in summaries:
            outcome = tuple(round(r.net_pnl_sol, 9) for r in summary.results)
            if outcome not in shown:
                shown.add(outcome)
                distinct.append(summary)
        for summary in distinct[:8]:
            print(
                f"   {describe_exit_rule(summary.rule):<38} N={summary.samples:<3} "
                f"win {summary.winrate:5.0%}  cons.EV {summary.conservative_ev_sol:+.4f}"
                f"  EV {summary.net_ev_sol:+.4f}  EV-best "
                f"{summary.ev_without_best_sol:+.4f}  ROI {summary.roi_pct:+6.1f}%"
            )
        if len(profiles) < BIBLE_MIN_SAMPLES:
            print(
                f"   WARNING: {len(profiles)} launches < {BIBLE_MIN_SAMPLES} "
                "(Bible minimum); results are not evidence of an edge"
            )
        print("   TP sweep (best stop/hold per level, % of stake after fees):")
        for line in describe_take_profit_sweep(summaries, costs.quote_size_sol):
            print(f"     {line}")
        dev_rule = summarize_rules(replays, [SIGNAL_SELL_RULE])[0]
        print(
            f"   (comparison) {describe_exit_rule(dev_rule.rule)}: "
            f"win {dev_rule.winrate:.0%}  EV {dev_rule.net_ev_sol:+.4f} SOL"
        )
    for reason, count in Counter(
        entry.split(": ", 1)[-1] for entry in skipped
    ).most_common():
        print(f"   skipped {count} launch(es): {reason}")


def _export_plot(funder: str, summary: RuleSummary, stake_sol: float) -> Path:
    """Write the best rule's per-launch equity curve as an HTML report."""
    records: list[TradePerformanceRecord] = []
    equity = peak = 0.0
    for index, result in enumerate(summary.results, start=1):
        equity += result.net_pnl_sol
        peak = max(peak, equity)
        records.append(
            TradePerformanceRecord(
                trade_index=index,
                mint=result.mint,
                entry_sol=stake_sol,
                exit_sol=stake_sol + result.net_pnl_sol,
                gross_pnl_sol=result.net_pnl_sol + result.fees_sol,
                net_pnl_sol=result.net_pnl_sol,
                roi_pct=100 * result.net_pnl_sol / stake_sol,
                market_impact_pct=0.0,
                holding_seconds=result.held_s,
                is_win=result.net_pnl_sol > 0,
                cumulative_equity_sol=equity,
                drawdown_pct=100 * (peak - equity) / stake_sol,
            )
        )
    return export_vectorbt_html_report(
        target=funder,
        mode=describe_exit_rule(summary.rule),
        records=records,
        total_fees_sol=summary.fees_sol,
        market_impact_drag_sol=0.0,
        output_path=PLOTS_DIR / f"history_equity_{funder[:8]}.html",
    )


def main(argv: Sequence[str] | None = None) -> int:
    """Run the entity-history command.

    Args:
        argv: Optional argument vector; defaults to ``sys.argv``.

    Returns:
        Process exit code: 0 on success, 1 on validation failure.
    """
    args = _build_parser().parse_args(argv)
    try:
        collected: list[tuple[EntityLaunchHistory, int]] = []
        for creator in args.funders if args.creator else ():
            collected.append(
                (creator_launch_history(creator, _launch_fetch(creator) or []), 0)
            )
        for funder in () if args.creator else args.funders:
            transfers = enumerate_funded_paged(
                funder,
                max_pages=None if args.all_pages else args.pages,
                max_transactions=args.max_tx,
                min_sol=args.min_sol,
                max_sol=args.max_sol,
                min_slot=args.slot_from,
                max_slot=args.slot_to,
            )
            resolved, descendants = _with_descendant_creators(
                transfers, max_hops=args.hops
            )
            if descendants:
                logger.info(
                    "%s: %d creators found below funded wallets",
                    funder[:8],
                    descendants,
                )
            per_history = build_launch_history(
                funder,
                transfers=resolved,
                launch_fetch=_launch_fetch,
            )
            collected.append((per_history, len(transfers)))
    except FundingChainError as error:
        if args.json:
            print(json.dumps({"error": str(error)}, indent=2))
        else:
            print(f"Entity history failed: {error}", file=sys.stderr)
        return 1

    history = merge_launch_histories([entry[0] for entry in collected])
    scanned = sum(entry[1] for entry in collected)
    activity = launch_activity(history, now_s=int(time.time()))
    if args.json:
        print(json.dumps(_as_payload(history, scanned), indent=2))
        return 0
    _render(history, scanned)
    _render_activity(activity)
    if args.backtest:
        costs = ReplayCosts(
            quote_size_sol=args.size, entry_delay_slots=args.entry_delay
        )
        replays, skipped = _replays(history, costs)
        summaries = summarize_rules(
            replays, take_profit_rules(r.profile.ath_multiple for r in replays)
        )
        _render_backtest(replays, summaries, skipped)
        if args.plot and summaries:
            funder = args.funders[0]
            print(f"\n equity: {_export_plot(funder, summaries[0], args.size)}")
            charts = write_launch_charts(
                replays, summaries[0].rule, PLOTS_DIR / f"history_{funder[:8]}.html"
            )
            print(f" charts: {charts}")
    return 0
