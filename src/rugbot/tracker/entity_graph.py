"""Assemble a full operator-entity graph from one seed wallet.

Chains the upstream funding-spine walk with per-node outbound enumeration and
an optional launch-count lookup, then groups same-slot fan-outs into funding
batches. A hit-and-run operator's fingerprint is exactly that batch: many
wallets funded a small amount in one slot immediately before a launch, which
is invariant to the wallet rotation that defeats pure graph walking.

Orchestration is intentionally side-effect free (no database, no implicit
network): callers inject the launch lookup and the RPC seams.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from rugbot.tracker.funding_chain import (
    DEFAULT_MAX_HOPS,
    DEFAULT_MAX_HUB_TRANSACTIONS,
    MIN_TRANSFER_SOL,
    FundingChainNode,
    enumerate_funded,
    walk_upstream,
)
from rugbot.utils.logger import get_logger

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from rugbot.integrations.rpc_access import RpcEndpoints

logger = get_logger(__name__)

ROLE_FUNDED = "funded"
DEFAULT_BATCH_MIN_RECIPIENTS = 3


@dataclass(frozen=True, slots=True)
class GraphNode:
    """One wallet in the assembled entity graph."""

    wallet: str
    role: str
    launch_count: int


@dataclass(frozen=True, slots=True)
class GraphEdge:
    """One funding transfer observed between two graphed wallets."""

    from_wallet: str
    to_wallet: str
    amount_sol: float
    slot: int | None
    signature: str


@dataclass(frozen=True, slots=True)
class FundingBatch:
    """A same-slot fan-out: the hit-and-run pre-launch funding signature."""

    slot: int
    recipients: tuple[str, ...]
    total_sol: float


@dataclass(frozen=True, slots=True)
class EntityGraph:
    """Assembled entity graph for one seed wallet."""

    seed: str
    spine: tuple[FundingChainNode, ...]
    nodes: tuple[GraphNode, ...]
    edges: tuple[GraphEdge, ...]
    batches: tuple[FundingBatch, ...]
    warning: str | None


def _group_batches(
    edges: list[GraphEdge], min_recipients: int
) -> tuple[FundingBatch, ...]:
    """Group edges into same-slot fan-outs at or above the recipient floor."""
    by_slot: dict[int, list[GraphEdge]] = {}
    for edge in edges:
        if edge.slot is None:
            continue
        by_slot.setdefault(edge.slot, []).append(edge)
    batches: list[FundingBatch] = []
    for slot, group in by_slot.items():
        per_recipient: dict[str, float] = {}
        for edge in group:
            per_recipient[edge.to_wallet] = (
                per_recipient.get(edge.to_wallet, 0.0) + edge.amount_sol
            )
        if len(per_recipient) < min_recipients:
            continue
        batches.append(
            FundingBatch(
                slot=slot,
                recipients=tuple(per_recipient),
                total_sol=sum(per_recipient.values()),
            )
        )
    return tuple(sorted(batches, key=lambda batch: batch.slot))


def _count_launches(
    wallets: Sequence[str],
    launch_lookup: Callable[[str], int | None] | None,
) -> dict[str, int]:
    """Resolve launch counts for each wallet, defaulting to zero."""
    if launch_lookup is None:
        return dict.fromkeys(wallets, 0)
    counts: dict[str, int] = {}
    for wallet in wallets:
        try:
            value = launch_lookup(wallet)
        except Exception:  # noqa: BLE001 - a lookup failure is not fatal here
            logger.warning("launch lookup failed for %s", wallet[:8])
            value = None
        counts[wallet] = int(value) if isinstance(value, int) and value > 0 else 0
    return counts


def discover_entity_graph(  # noqa: PLR0913
    seed: str,
    *,
    max_hops: int = DEFAULT_MAX_HOPS,
    max_funded_per_node: int = DEFAULT_MAX_HUB_TRANSACTIONS,
    min_sol: float = MIN_TRANSFER_SOL,
    batch_min_recipients: int = DEFAULT_BATCH_MIN_RECIPIENTS,
    launch_lookup: Callable[[str], int | None] | None = None,
    endpoints: RpcEndpoints | Sequence[str] | None = None,
    transport: Callable[[str, str, list[object]], object] | None = None,
) -> EntityGraph:
    """Assemble the entity graph reachable from one seed wallet.

    Args:
        seed: Wallet to start from (typically a launch burner).
        max_hops: Upstream spine depth.
        max_funded_per_node: Transactions inspected per spine node.
        min_sol: Ignore transfers below this SOL amount.
        batch_min_recipients: Same-slot fan-out size counted as a batch.
        launch_lookup: Optional ``wallet -> launch count`` resolver.
        endpoints: Resolved endpoints; defaults to precedence resolution.
        transport: Optional test seam replacing the pooled transport.

    Returns:
        EntityGraph with spine, nodes, edges, and detected funding batches.
    """
    walk = walk_upstream(
        seed, max_hops=max_hops, endpoints=endpoints, transport=transport
    )
    spine_wallets = [node.wallet for node in walk.nodes]
    edges: list[GraphEdge] = []
    for node in walk.nodes:
        for transfer in enumerate_funded(
            node.wallet,
            max_transactions=max_funded_per_node,
            min_sol=min_sol,
            endpoints=endpoints,
            transport=transport,
        ):
            edges.append(
                GraphEdge(
                    from_wallet=node.wallet,
                    to_wallet=transfer.recipient,
                    amount_sol=transfer.amount_sol,
                    slot=transfer.slot,
                    signature=transfer.signature,
                )
            )
    batches = _group_batches(edges, batch_min_recipients)
    recipients = [edge.to_wallet for edge in edges]
    wallets = list(dict.fromkeys([*spine_wallets, *recipients]))
    counts = _count_launches(wallets, launch_lookup)
    spine_roles = {node.wallet: node.role for node in walk.nodes}
    nodes = tuple(
        GraphNode(
            wallet=wallet,
            role=spine_roles.get(wallet, ROLE_FUNDED),
            launch_count=counts.get(wallet, 0),
        )
        for wallet in wallets
    )
    return EntityGraph(
        seed=walk.nodes[0].wallet if walk.nodes else seed,
        spine=walk.nodes,
        nodes=nodes,
        edges=tuple(edges),
        batches=batches,
        warning=walk.warning,
    )


__all__ = [
    "DEFAULT_BATCH_MIN_RECIPIENTS",
    "ROLE_FUNDED",
    "EntityGraph",
    "FundingBatch",
    "GraphEdge",
    "GraphNode",
    "discover_entity_graph",
]
