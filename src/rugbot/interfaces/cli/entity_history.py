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
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from rugbot.backtest.launch_replay import (
    LaunchReplay,
    LaunchReplayError,
    ReplayCosts,
    RuleSummary,
    default_exit_rules,
    summarize_rules,
    trades_from_swap_api,
)
from rugbot.backtest.reporting.visualizer import (
    TradePerformanceRecord,
    export_vectorbt_html_report,
)
from rugbot.integrations.pumpfun_api import PumpFunApiError, get_client
from rugbot.tracker.entity_history import (
    EntityLaunchHistory,
    LaunchActivity,
    build_launch_history,
    launch_activity,
    merge_launch_histories,
)
from rugbot.tracker.funder_discovery import STAGED_MAX_SOL, STAGED_MIN_SOL
from rugbot.tracker.funding_chain import (
    DEFAULT_HISTORY_PAGES,
    DEFAULT_HISTORY_TRANSACTIONS,
    FundedTransfer,
    FundingChainError,
    enumerate_funded_paged,
    resolve_relay_terminal,
)
from rugbot.utils.logger import get_logger

if TYPE_CHECKING:
    from collections.abc import Sequence

logger = get_logger(__name__)


def _build_parser() -> argparse.ArgumentParser:
    """Build the argument parser for the entity-history command."""
    parser = argparse.ArgumentParser(
        prog="rug_entity_history",
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
        "--backtest",
        action="store_true",
        help="Replay every launch from its full trade history and rank exit rules.",
    )
    parser.add_argument(
        "--entry-delay",
        type=int,
        default=ReplayCosts.entry_delay_slots,
        help="Slots after create our buy lands (0 = block 0).",
    )
    parser.add_argument(
        "--size",
        type=float,
        default=ReplayCosts.quote_size_sol,
        help="Buy size in SOL per launch.",
    )
    parser.add_argument(
        "--plot",
        action="store_true",
        help="Write an HTML equity report for the best rule under .state/.",
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


def _through_relays(
    transfers: Sequence[FundedTransfer],
) -> tuple[list[FundedTransfer], int]:
    """Re-point each transfer at the wallet its SOL reached after relay hops.

    Returns:
        ``(transfers, relayed)`` where ``relayed`` counts recipients that were
        relays rather than the final holder of the funds.
    """
    received: dict[str, float] = {}
    for transfer in transfers:
        received[transfer.recipient] = max(
            received.get(transfer.recipient, 0.0), transfer.amount_sol
        )
    terminals = {
        recipient: resolve_relay_terminal(recipient, received_sol=amount_sol)
        for recipient, amount_sol in received.items()
    }
    relayed = sum(1 for resolution in terminals.values() if resolution.relays)
    return [
        dataclasses.replace(transfer, recipient=terminals[transfer.recipient].terminal)
        for transfer in transfers
    ], relayed


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
    """Fetch full trade histories and build replays; failures are reported."""
    client = get_client()
    replays: list[LaunchReplay] = []
    skipped: list[str] = []
    for event in history.launches:
        try:
            trades = trades_from_swap_api(client.fetch_all_trades(event.mint))
            replays.append(
                LaunchReplay(
                    event.mint,
                    create_slot=trades[0].slot,
                    creator=event.creator,
                    trades=trades,
                    costs=costs,
                )
            )
        except (PumpFunApiError, LaunchReplayError, IndexError, OSError) as error:
            skipped.append(f"{event.symbol or event.mint[:8]}: {error}")
    return replays, skipped


def _describe_rule(summary: RuleSummary) -> str:
    rule = summary.rule
    if rule.exit_on_dev_sell:
        return "exit on dev/bundle sell"
    tp = f"TP +{rule.take_profit_pct:.0f}%" if rule.take_profit_pct else "no TP"
    sl = f"SL -{rule.stop_loss_pct:.0f}%" if rule.stop_loss_pct else "no SL"
    hold = f"hold {rule.max_hold_s / 60:.0f}m" if rule.max_hold_s else "no max hold"
    return f"{tp}, {sl}, {hold}"


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
        for summary in summaries[:8]:
            print(
                f"   {_describe_rule(summary):<38} N={summary.samples:<3} "
                f"win {summary.winrate:5.0%}  cons.EV {summary.conservative_ev_sol:+.4f}"
                f"  EV {summary.net_ev_sol:+.4f}  EV-best "
                f"{summary.ev_without_best_sol:+.4f}  ROI {summary.roi_pct:+6.1f}%"
            )
        dev_rule = next(s for s in summaries if s.rule.exit_on_dev_sell)
        print(
            f"   (comparison) {_describe_rule(dev_rule)}: "
            f"win {dev_rule.winrate:.0%}  EV {dev_rule.net_ev_sol:+.4f} SOL"
        )
    for reason in skipped:
        print(f"   skipped {reason}")


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
        mode=_describe_rule(summary),
        records=records,
        total_fees_sol=summary.fees_sol,
        market_impact_drag_sol=0.0,
        output_path=Path(".state") / f"entity_backtest_{funder[:8]}.html",
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
        for funder in args.funders:
            transfers = enumerate_funded_paged(
                funder,
                max_pages=None if args.all_pages else args.pages,
                max_transactions=args.max_tx,
                min_sol=args.min_sol,
                max_sol=args.max_sol,
                min_slot=args.slot_from,
                max_slot=args.slot_to,
            )
            resolved, relayed = _through_relays(transfers)
            if relayed:
                logger.info(
                    "%s: %d recipients were relays; followed to terminal wallets",
                    funder[:8],
                    relayed,
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
        summaries = summarize_rules(replays, default_exit_rules())
        _render_backtest(replays, summaries, skipped)
        if args.plot and summaries:
            print(
                f"\n report: {_export_plot(args.funders[0], summaries[0], args.size)}"
            )
    return 0
