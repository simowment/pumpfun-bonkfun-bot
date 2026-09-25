"""CLI: reconstruct an entity's token-creation history from a funder.

Pages a funding wallet's disbursements backward through history, filters to
the staging band, then resolves every funded wallet's creations into a single
token timeline. This is the command that answers "how many tokens did this
operator create" for burner-per-launch entities, whose history is invisible
to any per-wallet view.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from rugbot.integrations.pumpfun_api import get_client
from rugbot.tracker.entity_history import (
    EntityLaunchHistory,
    build_launch_history,
    merge_launch_histories,
)
from rugbot.tracker.funder_discovery import STAGED_MAX_SOL, STAGED_MIN_SOL
from rugbot.tracker.funding_chain import (
    DEFAULT_HISTORY_PAGES,
    DEFAULT_HISTORY_TRANSACTIONS,
    FundingChainError,
    enumerate_funded_paged,
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
    parser.add_argument("--json", action="store_true", help="Emit JSON only.")
    return parser


def _launch_fetch(wallet: str) -> list[object] | None:
    """Fetch one wallet's creator-index coin page."""
    page = get_client().fetch_user_created_coins(wallet, limit=50, offset=0)
    if not isinstance(page, dict):
        return None
    coins = page.get("coins")
    return coins if isinstance(coins, list) else None


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
            per_history = build_launch_history(
                funder,
                transfers=transfers,
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
    if args.json:
        print(json.dumps(_as_payload(history, scanned), indent=2))
        return 0
    _render(history, scanned)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
