"""Assemble a classified operator-entity graph from one seed wallet.

Per the cluster playbook, an entity is more than a funding spine: it is the
set of wallets the operator controls, each classified by deterministic
on-chain invariants. This module expands the graph *bidirectionally* (who a
wallet paid, and who paid it) up to a bounded depth, then classifies every
node into the playbook's wallet roles.

Roles follow the cluster playbook's deterministic heuristics, keyed on the
largest inbound transfer, the inbound count, and the observed mint count.
``REPEAT_BUNDLER`` is deliberately not assigned here: proving a wallet was
co-funded across two or more *separate* launches needs cross-seed evidence
that a single graph cannot establish.

Orchestration is side-effect free (no database, no implicit network):
callers inject the launch lookup and the RPC seams.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

from rugbot.tracker.funding_chain import (
    DEFAULT_MAX_HOPS,
    DEFAULT_MAX_HUB_TRANSACTIONS,
    MIN_TRANSFER_SOL,
    enumerate_funded,
    enumerate_sources,
    walk_upstream,
)
from rugbot.utils.logger import get_logger

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from rugbot.integrations.rpc_access import RpcEndpoints

logger = get_logger(__name__)

DEFAULT_MAX_DEPTH = 2
DEFAULT_MAX_NODES = 200
DEFAULT_BATCH_MIN_RECIPIENTS = 3

DEPLOYER_MIN_SOL = 0.20
DEPLOYER_MAX_SOL = 5.00
DEPLOYER_MAX_INBOUND = 2
TREASURY_MIN_SOL = 5.00
BUNDLED_MIN_SOL = 0.001
BUNDLED_MAX_SOL = 0.15


class WalletClass(StrEnum):
    """Deterministic cluster role per the playbook classification table."""

    ACTIVE_CREATOR = "active_creator"
    NEXT_DEPLOYER = "next_deployer"
    BUNDLED_BUYER = "bundled_buyer"
    TREASURY = "treasury"
    UNCLASSIFIED = "unclassified"


@dataclass(frozen=True, slots=True)
class GraphNode:
    """One classified wallet in the assembled entity graph."""

    wallet: str
    wallet_class: str
    launch_count: int
    staged_sol: float
    inbound_count: int
    depth: int


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
    """Classified entity graph for one seed wallet."""

    seed: str
    nodes: tuple[GraphNode, ...]
    edges: tuple[GraphEdge, ...]
    batches: tuple[FundingBatch, ...]
    warning: str | None


def classify_wallet(
    *,
    launch_count: int,
    staged_sol: float,
    inbound_count: int,
) -> WalletClass:
    """Classify one wallet into its cluster role.

    Args:
        launch_count: Observed mints created by the wallet.
        staged_sol: Largest single inbound transfer in SOL.
        inbound_count: Number of inbound transfers observed.

    Returns:
        The deterministic playbook role for these invariants.
    """
    if launch_count >= 1:
        return WalletClass.ACTIVE_CREATOR
    if staged_sol >= TREASURY_MIN_SOL:
        return WalletClass.TREASURY
    if (
        DEPLOYER_MIN_SOL <= staged_sol <= DEPLOYER_MAX_SOL
        and inbound_count <= DEPLOYER_MAX_INBOUND
    ):
        return WalletClass.NEXT_DEPLOYER
    if BUNDLED_MIN_SOL <= staged_sol <= BUNDLED_MAX_SOL:
        return WalletClass.BUNDLED_BUYER
    return WalletClass.UNCLASSIFIED


def _group_batches(
    edges: list[GraphEdge], min_recipients: int
) -> tuple[FundingBatch, ...]:
    """Group edges into same-slot fan-outs at or above the recipient floor."""
    by_slot: dict[int, dict[str, float]] = {}
    for edge in edges:
        if edge.slot is None:
            continue
        slot_totals = by_slot.setdefault(edge.slot, {})
        slot_totals[edge.to_wallet] = (
            slot_totals.get(edge.to_wallet, 0.0) + edge.amount_sol
        )
    batches: list[FundingBatch] = []
    for slot, per_recipient in by_slot.items():
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


def _resolve_launch_counts(
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


def _expand(  # noqa: PLR0913
    seeds: Sequence[str],
    *,
    max_depth: int,
    max_nodes: int,
    per_node: int,
    min_sol: float,
    endpoints: RpcEndpoints | Sequence[str] | None,
    transport: Callable[[str, str, list[object]], object] | None,
    on_step: Callable[[dict[str, int], list[GraphEdge]], None] | None = None,
) -> tuple[dict[str, int], list[GraphEdge], bool]:
    """Breadth-first expand outbound and inbound edges from seed wallets.

    Args:
        seeds: Wallets to start from (the seed plus its upstream spine).
        on_step: Optional callback invoked with ``(depths, edges)`` after
            each wallet's outbound and inbound expansion completes.

    Returns:
        ``(depth_by_wallet, edges, truncated)`` where ``truncated`` is True
        when the node cap stopped the expansion early.
    """
    depths: dict[str, int] = dict.fromkeys(seeds, 0)
    edges: list[GraphEdge] = []
    queue: deque[tuple[str, int]] = deque((wallet, 0) for wallet in seeds)
    truncated = False
    while queue:
        wallet, depth = queue.popleft()
        if len(depths) >= max_nodes:
            truncated = True
            break
        for transfer in enumerate_funded(
            wallet,
            max_transactions=per_node,
            min_sol=min_sol,
            endpoints=endpoints,
            transport=transport,
        ):
            edges.append(
                GraphEdge(
                    from_wallet=wallet,
                    to_wallet=transfer.recipient,
                    amount_sol=transfer.amount_sol,
                    slot=transfer.slot,
                    signature=transfer.signature,
                )
            )
            _enqueue(transfer.recipient, depth, max_depth, max_nodes, depths, queue)
        for source in enumerate_sources(
            wallet,
            max_transactions=per_node,
            min_sol=min_sol,
            endpoints=endpoints,
            transport=transport,
        ):
            edges.append(
                GraphEdge(
                    from_wallet=source.sender,
                    to_wallet=wallet,
                    amount_sol=source.amount_sol,
                    slot=source.slot,
                    signature=source.signature,
                )
            )
            _enqueue(source.sender, depth, max_depth, max_nodes, depths, queue)
        if on_step is not None:
            on_step(depths, edges)
    return depths, edges, truncated


def _enqueue(  # noqa: PLR0913
    wallet: str,
    depth: int,
    max_depth: int,
    max_nodes: int,
    depths: dict[str, int],
    queue: deque[tuple[str, int]],
) -> None:
    """Schedule a newly seen wallet for expansion within depth and node caps."""
    if wallet in depths or depth >= max_depth or len(depths) >= max_nodes:
        return
    depths[wallet] = depth + 1
    queue.append((wallet, depth + 1))


def _build_nodes(
    depths: dict[str, int],
    edges: list[GraphEdge],
    counts: dict[str, int],
) -> tuple[GraphNode, ...]:
    """Classify every discovered wallet from its observed inbound edges."""
    inbound: dict[str, list[float]] = {}
    for edge in edges:
        inbound.setdefault(edge.to_wallet, []).append(edge.amount_sol)
    nodes: list[GraphNode] = []
    for wallet, depth in sorted(depths.items(), key=lambda item: (item[1], item[0])):
        amounts = inbound.get(wallet, [])
        staged = max(amounts) if amounts else 0.0
        launch_count = counts.get(wallet, 0)
        nodes.append(
            GraphNode(
                wallet=wallet,
                wallet_class=classify_wallet(
                    launch_count=launch_count,
                    staged_sol=staged,
                    inbound_count=len(amounts),
                ).value,
                launch_count=launch_count,
                staged_sol=staged,
                inbound_count=len(amounts),
                depth=depth,
            )
        )
    return tuple(nodes)


def discover_entity_graph(  # noqa: PLR0913
    seed: str,
    *,
    spine_hops: int = DEFAULT_MAX_HOPS,
    max_depth: int = DEFAULT_MAX_DEPTH,
    max_nodes: int = DEFAULT_MAX_NODES,
    per_node: int = DEFAULT_MAX_HUB_TRANSACTIONS,
    min_sol: float = MIN_TRANSFER_SOL,
    batch_min_recipients: int = DEFAULT_BATCH_MIN_RECIPIENTS,
    launch_lookup: Callable[[str], int | None] | None = None,
    endpoints: RpcEndpoints | Sequence[str] | None = None,
    transport: Callable[[str, str, list[object]], object] | None = None,
    on_progress: Callable[[EntityGraph], None] | None = None,
) -> EntityGraph:
    """Assemble and classify the entity graph reachable from one seed.

    Args:
        seed: Wallet to start from (typically a launch burner).
        spine_hops: Upstream spine depth walked before breadth expansion.
        max_depth: Bidirectional expansion depth from each spine wallet.
        max_nodes: Hard cap on discovered wallets.
        per_node: Transactions inspected per wallet per direction.
        min_sol: Ignore transfers below this SOL amount.
        batch_min_recipients: Same-slot fan-out size counted as a batch.
        launch_lookup: Optional ``wallet -> launch count`` resolver.
        endpoints: Resolved endpoints; defaults to precedence resolution.
        transport: Optional test seam replacing the pooled transport.
        on_progress: Optional callback receiving a partial ``EntityGraph``
            after each wallet expansion (launch counts may be zero
            mid-scan). Persistence stays with the caller.

    Returns:
        EntityGraph with classified nodes, edges, and funding batches.
    """
    walk = walk_upstream(
        seed, max_hops=spine_hops, endpoints=endpoints, transport=transport
    )
    spine = [node.wallet for node in walk.nodes] or [seed]

    def on_step(depths: dict[str, int], step_edges: list[GraphEdge]) -> None:
        """Build a partial graph snapshot and forward it to the caller."""
        if on_progress is None:
            return
        on_progress(
            EntityGraph(
                seed=seed,
                nodes=_build_nodes(depths, step_edges, {}),
                edges=tuple(step_edges),
                batches=_group_batches(step_edges, batch_min_recipients),
                warning=None,
            )
        )

    depths, edges, truncated = _expand(
        spine,
        max_depth=max_depth,
        max_nodes=max_nodes,
        per_node=per_node,
        min_sol=min_sol,
        endpoints=endpoints,
        transport=transport,
        on_step=on_step if on_progress is not None else None,
    )
    counts = _resolve_launch_counts(list(depths), launch_lookup)
    batches = _group_batches(edges, batch_min_recipients)
    warning = walk.warning or (
        "node cap reached; graph truncated" if truncated else None
    )
    return EntityGraph(
        seed=seed,
        nodes=_build_nodes(depths, edges, counts),
        edges=tuple(edges),
        batches=batches,
        warning=warning,
    )


__all__ = [
    "DEFAULT_BATCH_MIN_RECIPIENTS",
    "DEFAULT_MAX_DEPTH",
    "DEFAULT_MAX_NODES",
    "EntityGraph",
    "FundingBatch",
    "GraphEdge",
    "GraphNode",
    "WalletClass",
    "classify_wallet",
    "discover_entity_graph",
]
