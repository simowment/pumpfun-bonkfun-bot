"""CLI: walk a wallet's funding chain upstream and enumerate the hub's siblings.

Semi-manual Type-2 operator tracing. Given a launch burner, this prints the
relay chain up to the hub wallet, then (optionally) every wallet the hub has
funded -- the sibling relays/burners that carry the operator's launch history.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import TYPE_CHECKING

from rugbot.tracker.funding_chain import (
    DEFAULT_MAX_HOPS,
    DEFAULT_MAX_HUB_TRANSACTIONS,
    HUB_MIN_SIGNATURES,
    MIN_TRANSFER_SOL,
    FundingChainError,
    FundingChainWalk,
    enumerate_funded,
    walk_upstream,
)
from rugbot.utils.logger import get_logger

if TYPE_CHECKING:
    from collections.abc import Sequence

logger = get_logger(__name__)


def _build_parser() -> argparse.ArgumentParser:
    """Build the argument parser for the funding-chain tracer."""
    parser = argparse.ArgumentParser(
        prog="rug_chain",
        description=(
            "Walk a wallet's funding chain upward to the hub and enumerate "
            "the wallets that hub funded (Type-2 operator tracing)."
        ),
    )
    parser.add_argument("address", help="Origin wallet, typically a launch burner.")
    parser.add_argument(
        "--max-hops",
        type=int,
        default=DEFAULT_MAX_HOPS,
        help=f"Maximum wallets visited before stopping (default: {DEFAULT_MAX_HOPS}).",
    )
    parser.add_argument(
        "--hub-min-signatures",
        type=int,
        default=HUB_MIN_SIGNATURES,
        help=(
            "Upstream signature count treated as a hub rather than a "
            f"single-use relay (default: {HUB_MIN_SIGNATURES})."
        ),
    )
    parser.add_argument(
        "--enumerate",
        action="store_true",
        help="Also list the wallets the hub funded (the sibling set).",
    )
    parser.add_argument(
        "--max-transactions",
        type=int,
        default=DEFAULT_MAX_HUB_TRANSACTIONS,
        help=(
            "Maximum hub transactions inspected when enumerating "
            f"(default: {DEFAULT_MAX_HUB_TRANSACTIONS})."
        ),
    )
    parser.add_argument(
        "--min-sol",
        type=float,
        default=MIN_TRANSFER_SOL,
        help=f"Ignore transfers below this SOL amount (default: {MIN_TRANSFER_SOL}).",
    )
    parser.add_argument("--json", action="store_true", help="Emit JSON only.")
    return parser


def _walk_payload(walk: FundingChainWalk) -> dict[str, object]:
    """Serialize a chain walk into a JSON-safe mapping."""
    return {
        "nodes": [
            {
                "wallet": node.wallet,
                "role": node.role,
                "signature_count": node.signature_count,
                "oldest_slot": node.oldest_slot,
                "newest_slot": node.newest_slot,
            }
            for node in walk.nodes
        ],
        "hub": walk.hub,
        "warning": walk.warning,
    }


def _render(walk: FundingChainWalk, funded: list[dict[str, object]]) -> None:
    """Print the human-readable chain, hub, and funded wallets."""
    print("=" * 78)
    print(" UPSTREAM FUNDING CHAIN")
    print("=" * 78)
    for index, node in enumerate(walk.nodes):
        marker = "*" if node.role == "hub" else " "
        print(
            f" {marker}hop {index:2d}  [{node.role:6s}]  {node.wallet}  "
            f"sigs={node.signature_count}  slots={node.oldest_slot}-"
            f"{node.newest_slot}"
        )
    if walk.hub:
        print(f"\n HUB: {walk.hub}")
    else:
        print("\n HUB: not reached")
    if walk.warning:
        print(f" note: {walk.warning}")
    if funded:
        print(f"\n WALLETS FUNDED BY HUB ({len(funded)}):")
        for transfer in funded:
            print(
                f"   {transfer['amount_sol']:>14.6f} SOL -> {transfer['recipient']}  "
                f"slot={transfer['slot']}"
            )


def main(argv: Sequence[str] | None = None) -> int:
    """Run the funding-chain tracer.

    Args:
        argv: Optional argument vector; defaults to ``sys.argv``.

    Returns:
        Process exit code: 0 on success, 1 on validation failure.
    """
    args = _build_parser().parse_args(argv)
    try:
        walk = walk_upstream(
            args.address,
            max_hops=args.max_hops,
            hub_min_signatures=args.hub_min_signatures,
        )
    except FundingChainError as error:
        if args.json:
            print(json.dumps({"error": str(error)}, indent=2))
        else:
            print(f"Funding chain trace failed: {error}", file=sys.stderr)
        return 1

    funded: list[dict[str, object]] = []
    if args.enumerate and walk.hub:
        funded = [
            {
                "recipient": transfer.recipient,
                "amount_sol": transfer.amount_sol,
                "signature": transfer.signature,
                "slot": transfer.slot,
            }
            for transfer in enumerate_funded(
                walk.hub,
                max_transactions=args.max_transactions,
                min_sol=args.min_sol,
            )
        ]
    if args.json:
        payload = _walk_payload(walk)
        payload["funded"] = funded
        print(json.dumps(payload, indent=2))
        return 0
    _render(walk, funded)
    return 0
