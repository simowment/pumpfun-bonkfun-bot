"""CLI: graph an operator entity from one seed wallet and persist it.

Given a launch burner (or any wallet in a hit-and-run cluster), this walks
the funding spine, enumerates what each spine node funded, resolves launch
counts, detects same-slot funding batches, prints the graph, and merges every
discovered node and edge into the tracker database.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from rugbot.integrations.pumpfun_api import get_client
from rugbot.storage.database import DatabaseManager
from rugbot.storage.tracker import SQLiteTrackerRepository
from rugbot.tracker.entity_graph import (
    DEFAULT_BATCH_MIN_RECIPIENTS,
    EntityGraph,
    discover_entity_graph,
)
from rugbot.tracker.funding_chain import (
    DEFAULT_MAX_HOPS,
    DEFAULT_MAX_HUB_TRANSACTIONS,
    MIN_TRANSFER_SOL,
    FundingChainError,
)
from rugbot.tracker.models import (
    LAMPORTS_PER_SOL,
    EntityEdgeRecord,
    EntityNodeRecord,
)
from rugbot.utils.logger import get_logger

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

logger = get_logger(__name__)

DEFAULT_STATE_DB = Path(".state/watch/rugbot.db")


def _build_parser() -> argparse.ArgumentParser:
    """Build the argument parser for the entity-graph command."""
    parser = argparse.ArgumentParser(
        prog="rug_graph",
        description=(
            "Graph an operator entity: funding spine, funded wallets, launch "
            "counts, and same-slot funding batches."
        ),
    )
    parser.add_argument("seed", help="Seed wallet, typically a launch burner.")
    parser.add_argument(
        "--max-hops",
        type=int,
        default=DEFAULT_MAX_HOPS,
        help=f"Upstream spine depth (default: {DEFAULT_MAX_HOPS}).",
    )
    parser.add_argument(
        "--per-node",
        type=int,
        default=DEFAULT_MAX_HUB_TRANSACTIONS,
        help=(
            "Transactions inspected per spine node when enumerating outbound "
            f"transfers (default: {DEFAULT_MAX_HUB_TRANSACTIONS})."
        ),
    )
    parser.add_argument(
        "--min-sol",
        type=float,
        default=MIN_TRANSFER_SOL,
        help=f"Ignore transfers below this SOL amount (default: {MIN_TRANSFER_SOL}).",
    )
    parser.add_argument(
        "--batch-min",
        type=int,
        default=DEFAULT_BATCH_MIN_RECIPIENTS,
        help=(
            "Same-slot fan-out size treated as a funding batch "
            f"(default: {DEFAULT_BATCH_MIN_RECIPIENTS})."
        ),
    )
    parser.add_argument(
        "--no-launches",
        action="store_true",
        help="Skip the launch-count lookup (RPC-only, no REST calls).",
    )
    parser.add_argument(
        "--no-persist",
        action="store_true",
        help="Print the graph without writing it to the tracker database.",
    )
    parser.add_argument("--json", action="store_true", help="Emit JSON only.")
    return parser


def _launch_lookup(*, enabled: bool) -> Callable[[str], int | None] | None:
    """Build the wallet to launch-count resolver used during discovery."""
    if not enabled:
        return None
    client = get_client()

    def lookup(wallet: str) -> int | None:
        page = client.fetch_user_created_coins(wallet, limit=50, offset=0)
        if not isinstance(page, dict):
            return None
        count = page.get("count")
        return count if isinstance(count, int) else None

    return lookup


def _persist(graph: EntityGraph) -> tuple[int, int]:
    """Merge the graph into the tracker database, returning saved counts."""
    now = datetime.now(UTC).isoformat()
    repo = SQLiteTrackerRepository(DatabaseManager(DEFAULT_STATE_DB))
    nodes = [
        EntityNodeRecord(
            wallet=node.wallet,
            seed=graph.seed,
            role=node.role,
            launch_count=node.launch_count,
            first_seen_at=now,
            last_seen_at=now,
        )
        for node in graph.nodes
    ]
    edges = [
        EntityEdgeRecord(
            signature=edge.signature,
            from_wallet=edge.from_wallet,
            to_wallet=edge.to_wallet,
            amount_lamports=int(edge.amount_sol * LAMPORTS_PER_SOL),
            slot=edge.slot if edge.slot is not None else -1,
            seed=graph.seed,
            first_seen_at=now,
        )
        for edge in graph.edges
    ]
    return (repo.save_entity_nodes(nodes), repo.save_entity_edges(edges))


def _as_payload(graph: EntityGraph) -> dict[str, object]:
    """Serialize an entity graph into a JSON-safe mapping."""
    return {
        "seed": graph.seed,
        "warning": graph.warning,
        "spine": [
            {
                "wallet": node.wallet,
                "role": node.role,
                "signature_count": node.signature_count,
                "oldest_slot": node.oldest_slot,
                "newest_slot": node.newest_slot,
            }
            for node in graph.spine
        ],
        "nodes": [
            {
                "wallet": node.wallet,
                "role": node.role,
                "launch_count": node.launch_count,
            }
            for node in graph.nodes
        ],
        "edges": [
            {
                "from": edge.from_wallet,
                "to": edge.to_wallet,
                "amount_sol": edge.amount_sol,
                "slot": edge.slot,
                "signature": edge.signature,
            }
            for edge in graph.edges
        ],
        "batches": [
            {
                "slot": batch.slot,
                "recipients": list(batch.recipients),
                "total_sol": batch.total_sol,
            }
            for batch in graph.batches
        ],
    }


def _render(graph: EntityGraph, persisted: tuple[int, int] | None) -> None:
    """Print the human-readable entity graph."""
    print("=" * 78)
    print(" ENTITY GRAPH")
    print("=" * 78)
    print(f" seed: {graph.seed}")
    print(
        f" spine depth: {len(graph.spine)}  nodes: {len(graph.nodes)}  "
        f"edges: {len(graph.edges)}  batches: {len(graph.batches)}"
    )
    print("\n SPINE (seed -> upstream):")
    for index, node in enumerate(graph.spine):
        print(
            f"   {index:2d} [{node.role:6s}] {node.wallet}  sigs={node.signature_count}"
        )
    launchers = [n for n in graph.nodes if n.launch_count > 0]
    print(f"\n WALLETS WITH LAUNCHES ({len(launchers)}):")
    for node in sorted(launchers, key=lambda n: -n.launch_count)[:20]:
        print(f"   {node.launch_count:>4} launches  {node.wallet}")
    if not launchers:
        print("   none discovered in this walk")
    if graph.batches:
        print(f"\n FUNDING BATCHES ({len(graph.batches)}) - hit-and-run signature:")
        for batch in graph.batches:
            print(
                f"   slot {batch.slot}: {len(batch.recipients)} wallets, "
                f"{batch.total_sol:.4f} SOL total"
            )
            for recipient in batch.recipients:
                print(f"        {recipient}")
    if graph.warning:
        print(f"\n note: {graph.warning}")
    if persisted is not None:
        print(f"\n persisted: {persisted[0]} nodes, {persisted[1]} edges")


def main(argv: Sequence[str] | None = None) -> int:
    """Run the entity-graph command.

    Args:
        argv: Optional argument vector; defaults to ``sys.argv``.

    Returns:
        Process exit code: 0 on success, 1 on validation failure.
    """
    args = _build_parser().parse_args(argv)
    try:
        graph = discover_entity_graph(
            args.seed,
            max_hops=args.max_hops,
            max_funded_per_node=args.per_node,
            min_sol=args.min_sol,
            batch_min_recipients=args.batch_min,
            launch_lookup=_launch_lookup(enabled=not args.no_launches),
        )
    except FundingChainError as error:
        if args.json:
            print(json.dumps({"error": str(error)}, indent=2))
        else:
            print(f"Entity graph failed: {error}", file=sys.stderr)
        return 1

    persisted: tuple[int, int] | None = None
    if not args.no_persist:
        try:
            persisted = _persist(graph)
        except Exception as error:  # noqa: BLE001 - persistence never breaks output
            logger.warning("entity graph persist failed: %s", type(error).__name__)

    if args.json:
        payload = _as_payload(graph)
        payload["persisted"] = (
            {"nodes": persisted[0], "edges": persisted[1]} if persisted else None
        )
        print(json.dumps(payload, indent=2))
        return 0
    _render(graph, persisted)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
