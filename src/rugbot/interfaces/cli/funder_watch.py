"""Watch funder wallets for outbound staging transfers (observe-only).

Detection half of the Type-2 funder watcher: funder X pays fresh wallet Y
an amount inside the staging band, so Y is the next candidate burner.
Read-only: never places orders, never arms a sniper target, never mutates
execution state. Emits one event per new candidate burner.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from rugbot.integrations.pumpfun_api import get_client
from rugbot.integrations.solscan import SolscanClient
from rugbot.runtime.config import load_provider_settings, resolve_dotenv
from rugbot.tracker.funder_discovery import (
    STAGED_MAX_SOL,
    STAGED_MIN_SOL,
    find_funding_edges,
)
from rugbot.utils.logger import get_logger

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from rugbot.tracker.funder_discovery import StagedTransferCandidate

logger = get_logger(__name__)

OBSERVE_ONLY_MARKER = "observe-only: no orders, no arming, no target mutation"
DEFAULT_STATE_SUBPATH = Path(".state/funder_watch.json")
DEFAULT_INTERVAL_SECONDS = 60


@dataclass(frozen=True, slots=True)
class FunderWatchEvent:
    """One new candidate burner staged by a watched funder."""

    funder: str
    recipient: str
    amount_sol: float
    signature: str
    slot: int
    recipient_lifetime_creations: int
    freshness: str


def freshness_bucket(creation_count: int) -> str:
    """Bucket a recipient by lifetime creation count.

    Args:
        creation_count: Lifetime ``user-created-coins`` count.

    Returns:
        ``"fresh"`` for 0, ``"just_launched"`` for 1, else ``"used"``.
    """
    if creation_count <= 0:
        return "fresh"
    if creation_count == 1:
        return "just_launched"
    return "used"


def in_band(amount_sol: float, min_sol: float, max_sol: float) -> bool:
    """Return True when an amount sits inside the inclusive staging band."""
    return min_sol <= amount_sol <= max_sol


def new_events(
    seen: set[str], candidates: Sequence[FunderWatchEvent]
) -> list[FunderWatchEvent]:
    """Filter candidate events to those not yet seen.

    Args:
        seen: Set of ``"<funder>|<signature>"`` keys already emitted.
        candidates: Freshly built candidate events.

    Returns:
        Events whose key is absent from ``seen``, in input order.
    """
    fresh: list[FunderWatchEvent] = []
    for event in candidates:
        if f"{event.funder}|{event.signature}" not in seen:
            fresh.append(event)
    return fresh


def load_state(path: Path) -> set[str]:
    """Load seen ``funder|signature`` keys, starting empty on absence."""
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return set()
    seen = document.get("seen", []) if isinstance(document, dict) else []
    if not isinstance(seen, list):
        return set()
    return {entry for entry in seen if isinstance(entry, str)}


def save_state(path: Path, seen: set[str]) -> None:
    """Persist seen keys, creating parent dirs; warn instead of raising."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"seen": sorted(seen)}), encoding="utf-8")
    except OSError as exc:
        logger.warning("funder watch state save failed for %s: %s", path, exc)


def event_to_json(event: FunderWatchEvent) -> dict[str, object]:
    """Serialize one event for machine output."""
    return {
        "funder": event.funder,
        "recipient": event.recipient,
        "amount_sol": event.amount_sol,
        "signature": event.signature,
        "slot": event.slot,
        "recipient_lifetime_creations": event.recipient_lifetime_creations,
        "freshness": event.freshness,
        "observe_only": True,
    }


def scan_funder(  # noqa: PLR0913 - scan options are the CLI surface
    funder: str,
    endpoint: str,
    *,
    min_sol: float,
    max_sol: float,
    fresh_only: bool,
    solscan_client: SolscanClient | None = None,
    edge_fn: Callable[..., tuple[list[StagedTransferCandidate], str | None]]
    | None = None,
    creations_fn: Callable[[str], int] | None = None,
) -> tuple[list[FunderWatchEvent], str | None]:
    """Scan one funder for in-band outbound staging transfers.

    Args:
        funder: Watched funder address.
        endpoint: Solana RPC HTTP endpoint for confirmation calls.
        min_sol: Inclusive lower staging bound.
        max_sol: Inclusive upper staging bound.
        fresh_only: Emit only recipients with zero lifetime creations.
        solscan_client: Optional indexed nomination client.
        edge_fn: Test seam replacing ``find_funding_edges``.
        creations_fn: Test seam replacing the lifetime-creations lookup.

    Returns:
        Tuple of candidate events and an optional warning.
    """
    try:
        if edge_fn is not None:
            candidates, warning = edge_fn(funder)
        else:
            candidates, warning = find_funding_edges(
                funder, "outbound", endpoint, solscan_client=solscan_client
            )
    except Exception as exc:  # noqa: BLE001 - fail-soft per funder
        return [], f"{type(exc).__name__}: {exc}"
    events: list[FunderWatchEvent] = []
    for candidate in candidates:
        if not in_band(candidate.amount_sol, min_sol, max_sol):
            continue
        try:
            if creations_fn is not None:
                count = creations_fn(candidate.wallet)
            else:
                listing = get_client().fetch_user_created_coins(
                    candidate.wallet, limit=1, offset=0
                )
                raw = listing.get("count", 0) if isinstance(listing, dict) else 0
                count = int(raw) if isinstance(raw, (int, float)) else 0
        except Exception as exc:  # noqa: BLE001 - fail-soft per recipient
            logger.warning("funder watch freshness lookup failed: %s", exc)
            continue
        bucket = freshness_bucket(count)
        if fresh_only and bucket != "fresh":
            continue
        events.append(
            FunderWatchEvent(
                funder=funder,
                recipient=candidate.wallet,
                amount_sol=candidate.amount_sol,
                signature=candidate.signature,
                slot=candidate.slot,
                recipient_lifetime_creations=count,
                freshness=bucket,
            )
        )
    return events, warning


def build_parser() -> argparse.ArgumentParser:
    """Build the funder-watch argument parser."""
    parser = argparse.ArgumentParser(
        description="Watch funders for outbound staging transfers (observe-only).",
    )
    parser.add_argument("--funder", action="append", default=[])
    parser.add_argument("--min-sol", type=float, default=STAGED_MIN_SOL)
    parser.add_argument("--max-sol", type=float, default=STAGED_MAX_SOL)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--loop", action="store_true")
    parser.add_argument("--interval", type=int, default=DEFAULT_INTERVAL_SECONDS)
    parser.add_argument("--state", type=str, default=str(DEFAULT_STATE_SUBPATH))
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--fresh-only", action="store_true", default=True)
    parser.add_argument("--no-fresh-only", action="store_false", dest="fresh_only")
    parser.add_argument(
        "--use-solscan",
        action="store_true",
        help="opt in to Solscan edge nomination (default is RPC-only)",
    )
    return parser


def print_events(events: Sequence[FunderWatchEvent], *, as_json: bool) -> None:
    """Print new candidate-burner events (JSON lines or human table)."""
    print(f"[{OBSERVE_ONLY_MARKER}]")
    if as_json:
        for event in events:
            print(json.dumps(event_to_json(event)))
        return
    if not events:
        print("no new candidate burners")
        return
    header = (
        f"{'FUNDER':<10} {'RECIPIENT':<10} {'AMOUNT':>8} "
        f"{'SLOT':>10} {'CREATIONS':>9} FRESHNESS  SIGNATURE"
    )
    print(header)
    for event in events:
        print(
            f"{event.funder[:8]:<10} {event.recipient[:8]:<10} "
            f"{event.amount_sol:>8.3f} {event.slot:>10} "
            f"{event.recipient_lifetime_creations:>9} {event.freshness:<11}"
            f"{event.signature[:16]}..."
        )


def run_scan(  # noqa: PLR0913 - scan options are the CLI surface
    funders: Sequence[str],
    seen: set[str],
    *,
    min_sol: float,
    max_sol: float,
    fresh_only: bool,
    endpoint: str,
    solscan_client: SolscanClient | None,
    as_json: bool,
) -> list[FunderWatchEvent]:
    """Scan all funders once, emit new events, persist state as we go.

    Args:
        funders: Watched funder addresses.
        seen: Mutable seen-key set, updated in place and persisted per funder.
        min_sol: Inclusive lower staging bound.
        max_sol: Inclusive upper staging bound.
        fresh_only: Emit only fresh recipients.
        endpoint: Solana RPC HTTP endpoint.
        solscan_client: Optional indexed nomination client.
        as_json: Machine output flag (controls warning rendering).

    Returns:
        New events emitted this pass.
    """
    emitted: list[FunderWatchEvent] = []
    for funder in funders:
        events, warning = scan_funder(
            funder,
            endpoint,
            min_sol=min_sol,
            max_sol=max_sol,
            fresh_only=fresh_only,
            solscan_client=solscan_client,
        )
        if warning:
            line = json.dumps({"funder": funder, "warning": warning})
            if as_json:
                print(line)
            else:
                print(f"warning for {funder[:8]}...: {warning}", file=sys.stderr)
        fresh = new_events(seen, events)
        for event in fresh:
            seen.add(f"{event.funder}|{event.signature}")
        emitted.extend(fresh)
    return emitted


def main(argv: Sequence[str] | None = None) -> int:  # noqa: C901
    """Run the funder watcher (returns process exit code)."""
    args = build_parser().parse_args(argv)
    resolve_dotenv()
    funders: list[str] = args.funder or []
    if not funders:
        print("Error: at least one --funder address is required", file=sys.stderr)
        return 2
    if args.min_sol < 0 or args.max_sol < args.min_sol:
        print("Error: require 0 <= --min-sol <= --max-sol", file=sys.stderr)
        return 2
    providers = load_provider_settings()
    if not providers.rpc_http:
        print("Error: SOLANA_RPC_HTTP is required", file=sys.stderr)
        return 2
    solscan_client: SolscanClient | None = None
    if bool(args.use_solscan) and providers.solscan_api_key:
        try:
            solscan_client = SolscanClient(providers.solscan_api_key)
        except ValueError as exc:
            logger.warning("funder watch solscan client unavailable: %s", exc)
    state_path = Path(args.state)
    seen = load_state(state_path)
    do_loop = args.loop and not args.once

    def one_pass() -> int:
        emitted = run_scan(
            funders,
            seen,
            min_sol=args.min_sol,
            max_sol=args.max_sol,
            fresh_only=args.fresh_only,
            endpoint=providers.rpc_http or "",
            solscan_client=solscan_client,
            as_json=args.json,
        )
        save_state(state_path, seen)
        print_events(emitted, as_json=args.json)
        return 0

    if not do_loop:
        return one_pass()
    print(f"[{OBSERVE_ONLY_MARKER}] watching {len(funders)} funder(s)")
    try:
        while True:
            emitted = run_scan(
                funders,
                seen,
                min_sol=args.min_sol,
                max_sol=args.max_sol,
                fresh_only=args.fresh_only,
                endpoint=providers.rpc_http or "",
                solscan_client=solscan_client,
                as_json=args.json,
            )
            save_state(state_path, seen)
            if emitted:
                print_events(emitted, as_json=args.json)
            time.sleep(max(args.interval, 10))
    except KeyboardInterrupt:
        print("funder watch stopped")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
