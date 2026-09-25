"""Unit tests for entity-graph assembly, classification, and persistence."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from rugbot.integrations.rpc_access import RpcEndpoints
from rugbot.storage.database import DatabaseManager
from rugbot.storage.tracker import SQLiteTrackerRepository
from rugbot.tracker.entity_graph import (
    EntityGraph,
    WalletClass,
    classify_wallet,
    discover_entity_graph,
)
from rugbot.tracker.models import EntityEdgeRecord, EntityNodeRecord

ENDPOINTS = RpcEndpoints(ordered=("http://seam",), source="test")
LAMPORTS = 1_000_000_000


def _tx(source: str, recipients: list[str], amount_sol: float) -> dict[str, Any]:
    """Build a parsed transaction where ``source`` pays every recipient."""
    lamports = int(amount_sol * LAMPORTS)
    keys = [source, *recipients]
    return {
        "meta": {
            "preBalances": [
                lamports * len(recipients) + 5_000,
                *([0] * len(recipients)),
            ],
            "postBalances": [5_000, *([lamports] * len(recipients))],
        },
        "transaction": {"message": {"accountKeys": keys}},
    }


def _transport(
    signatures: dict[str, list[dict[str, object]]],
    transactions: dict[str, dict[str, object]],
) -> Any:
    """Build a fake transport serving signatures and transactions by key."""

    def transport(endpoint: str, method: str, params: list[object]) -> object:
        if method == "getSignaturesForAddress":
            return signatures.get(str(params[0]), [])
        if method == "getTransaction":
            return transactions.get(str(params[0]))
        unexpected = f"unexpected method {method}"
        raise AssertionError(unexpected)

    return transport


def test_classify_wallet_applies_playbook_bands() -> None:
    """Each playbook band maps to its deterministic role."""
    assert (
        classify_wallet(launch_count=1, staged_sol=0.0, inbound_count=1)
        is WalletClass.ACTIVE_CREATOR
    )
    assert (
        classify_wallet(launch_count=0, staged_sol=12.0, inbound_count=3)
        is WalletClass.TREASURY
    )
    assert (
        classify_wallet(launch_count=0, staged_sol=2.5, inbound_count=1)
        is WalletClass.NEXT_DEPLOYER
    )
    assert (
        classify_wallet(launch_count=0, staged_sol=0.05, inbound_count=1)
        is WalletClass.BUNDLED_BUYER
    )
    assert (
        classify_wallet(launch_count=0, staged_sol=0.0, inbound_count=0)
        is WalletClass.UNCLASSIFIED
    )


def test_discover_graph_expands_both_directions_and_classifies() -> None:
    """Outbound recipients and the inbound funder are both graphed."""
    signatures = {
        "SEED": [
            {"signature": "fanout", "slot": 900},
            {"signature": "inbound", "slot": 800},
        ],
    }
    transactions = {
        "fanout": _tx("SEED", ["PAID1", "PAID2"], 0.5),
        "inbound": _tx("FUNDER", ["SEED"], 1.0),
    }
    counts = {"SEED": 2}
    graph = discover_entity_graph(
        "SEED",
        max_depth=1,
        launch_lookup=counts.get,
        endpoints=ENDPOINTS,
        transport=_transport(signatures, transactions),
    )
    by_wallet = {node.wallet: node for node in graph.nodes}
    assert set(by_wallet) == {"SEED", "PAID1", "PAID2", "FUNDER"}
    assert by_wallet["SEED"].wallet_class == WalletClass.ACTIVE_CREATOR.value
    assert by_wallet["PAID1"].wallet_class == WalletClass.NEXT_DEPLOYER.value
    assert by_wallet["PAID1"].staged_sol == 0.5
    assert by_wallet["PAID2"].depth == 1
    assert by_wallet["FUNDER"].wallet_class == WalletClass.UNCLASSIFIED.value


def test_discover_graph_respects_node_cap() -> None:
    """The node cap stops expansion and raises the truncation warning."""
    signatures = {"SEED": [{"signature": "fanout", "slot": 10}]}
    transactions = {"fanout": _tx("SEED", ["A", "B", "C", "D"], 0.5)}
    graph = discover_entity_graph(
        "SEED",
        max_depth=2,
        max_nodes=2,
        endpoints=ENDPOINTS,
        transport=_transport(signatures, transactions),
    )
    assert len(graph.nodes) <= 3
    assert graph.warning is not None


def test_discover_graph_detects_and_dedupes_batch() -> None:
    """A same-slot fan-out counts each recipient once."""
    keys = ["SEED", "A", "A", "B", "C"]
    lamports = int(0.5 * LAMPORTS)
    tx = {
        "meta": {
            "preBalances": [lamports * 4 + 5_000, 0, 0, 0, 0],
            "postBalances": [5_000, lamports, lamports, lamports, lamports],
        },
        "transaction": {"message": {"accountKeys": keys}},
    }
    signatures = {"SEED": [{"signature": "s", "slot": 42}]}
    graph = discover_entity_graph(
        "SEED",
        max_depth=1,
        batch_min_recipients=3,
        endpoints=ENDPOINTS,
        transport=_transport(signatures, {"s": tx}),
    )
    assert len(graph.batches) == 1
    assert sorted(graph.batches[0].recipients) == ["A", "B", "C"]
    assert graph.batches[0].total_sol == 2.0


def _repo(tmp_path: Path) -> SQLiteTrackerRepository:
    """Build a tracker repository on an isolated temp database."""
    return SQLiteTrackerRepository(DatabaseManager(tmp_path / "graph_test.db"))


def _node(wallet: str, count: int, first: str, last: str) -> EntityNodeRecord:
    """Build one canned entity-graph node record."""
    return EntityNodeRecord(
        wallet=wallet,
        seed="SEED",
        role="funded",
        launch_count=count,
        first_seen_at=first,
        last_seen_at=last,
    )


def _edge(signature: str, frm: str, to: str) -> EntityEdgeRecord:
    """Build one canned entity-graph edge record."""
    return EntityEdgeRecord(
        signature=signature,
        from_wallet=frm,
        to_wallet=to,
        amount_lamports=1_000_000,
        slot=10,
        seed="SEED",
        first_seen_at="2026-09-11T00:00:00",
    )


def test_entity_nodes_merge_keeps_max_count_and_first_seen(tmp_path: Path) -> None:
    """A later shallower walk cannot shrink the count or reset first_seen."""
    repo = _repo(tmp_path)
    repo.save_entity_nodes(
        [_node("A", 5, "2026-09-01T00:00:00", "2026-09-01T00:00:00")]
    )
    repo.save_entity_nodes(
        [_node("A", 1, "2026-09-05T00:00:00", "2026-09-05T00:00:00")]
    )
    nodes = repo.get_entity_nodes("SEED")
    assert len(nodes) == 1
    assert nodes[0].launch_count == 5
    assert nodes[0].first_seen_at == "2026-09-01T00:00:00"
    assert nodes[0].last_seen_at == "2026-09-05T00:00:00"


def test_entity_edges_ignore_duplicates(tmp_path: Path) -> None:
    """Re-saving the same transfer does not duplicate the edge row."""
    repo = _repo(tmp_path)
    repo.save_entity_edges([_edge("sig-1", "A", "B")])
    repo.save_entity_edges([_edge("sig-1", "A", "B")])
    assert len(repo.get_entity_edges("SEED")) == 1


def test_on_progress_snapshots_are_subsets_of_final_graph() -> None:
    """Each progress snapshot stays within the final graph for the seed."""
    signatures = {
        "SEED": [
            {"signature": "fanout", "slot": 900},
            {"signature": "inbound", "slot": 800},
        ],
    }
    transactions = {
        "fanout": _tx("SEED", ["PAID1", "PAID2"], 0.5),
        "inbound": _tx("FUNDER", ["SEED"], 1.0),
    }
    seen: list[EntityGraph] = []
    graph = discover_entity_graph(
        "SEED",
        max_depth=1,
        launch_lookup={"SEED": 2}.get,
        endpoints=ENDPOINTS,
        transport=_transport(signatures, transactions),
        on_progress=seen.append,
    )
    assert len(seen) >= 1
    final_nodes = {node.wallet for node in graph.nodes}
    final_edges = {
        (edge.from_wallet, edge.to_wallet, edge.signature) for edge in graph.edges
    }
    for snapshot in seen:
        assert isinstance(snapshot, EntityGraph)
        assert snapshot.seed == "SEED"
        assert {node.wallet for node in snapshot.nodes} <= final_nodes
        assert {
            (edge.from_wallet, edge.to_wallet, edge.signature)
            for edge in snapshot.edges
        } <= final_edges
