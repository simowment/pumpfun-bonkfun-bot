"""Unit tests for entity-graph assembly and persistence (no network)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from rugbot.integrations.rpc_access import RpcEndpoints
from rugbot.storage.database import DatabaseManager
from rugbot.storage.tracker import SQLiteTrackerRepository
from rugbot.tracker.entity_graph import ROLE_FUNDED, discover_entity_graph
from rugbot.tracker.models import EntityEdgeRecord, EntityNodeRecord

ENDPOINTS = RpcEndpoints(ordered=("http://seam",), source="test")
LAMPORTS = 1_000_000_000


def _fanout_tx(source: str, recipients: list[str], sol: float, slot: int) -> dict:
    """Build a parsed transaction where ``source`` pays every recipient."""
    lamports = int(sol * LAMPORTS)
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
        "_slot": slot,
    }


def _transport(
    signatures: dict[str, list[dict[str, object]]],
    transactions: dict[str, dict[str, object]],
    slots: dict[str, int] | None = None,
) -> Any:
    """Build a fake transport serving signatures and transactions by key."""
    slot_map = slots or {}

    def transport(endpoint: str, method: str, params: list[object]) -> object:
        if method == "getSignaturesForAddress":
            return signatures.get(str(params[0]), [])
        if method == "getTransaction":
            key = str(params[0])
            payload = transactions.get(key)
            if payload is None:
                return None
            slot = slot_map.get(key)
            if slot is not None:
                return {
                    "meta": payload["meta"],
                    "transaction": payload["transaction"],
                    "slot": slot,
                }
            return payload
        unexpected = f"unexpected method {method}"
        raise AssertionError(unexpected)

    return transport


def test_discover_graph_detects_same_slot_batch() -> None:
    """A one-slot fan-out to many wallets is reported as a funding batch."""
    seed = "SEED"
    recipients = ["A", "B", "C", "D"]
    tx = _fanout_tx(seed, recipients, 0.5, 900)
    signatures = {seed: [{"signature": "sig1", "slot": 900}]}
    graph = discover_entity_graph(
        seed,
        max_hops=1,
        launch_lookup=lambda wallet: 1 if wallet == "A" else 0,
        endpoints=ENDPOINTS,
        transport=_transport(signatures, {"sig1": tx}, {"sig1": 900}),
    )
    assert len(graph.batches) == 1
    assert graph.batches[0].slot == 900
    assert set(graph.batches[0].recipients) == set(recipients)
    assert graph.batches[0].total_sol == 2.0


def test_discover_graph_annotates_launch_counts_and_roles() -> None:
    """Spine wallets keep their walk role; funded wallets resolve launch counts."""
    seed = "SEED"
    tx = _fanout_tx(seed, ["A", "B"], 0.4, 700)
    signatures = {seed: [{"signature": "s1", "slot": 700}]}
    counts = {"A": 3, "B": 0}
    graph = discover_entity_graph(
        seed,
        max_hops=1,
        launch_lookup=counts.get,
        endpoints=ENDPOINTS,
        transport=_transport(signatures, {"s1": tx}, {"s1": 700}),
    )
    by_wallet = {node.wallet: node for node in graph.nodes}
    assert by_wallet["SEED"].role == "origin"
    assert by_wallet["A"].role == ROLE_FUNDED
    assert by_wallet["A"].launch_count == 3
    assert by_wallet["B"].launch_count == 0


def test_discover_graph_dedupes_repeated_recipient_in_one_slot() -> None:
    """A wallet funded twice in the same slot counts once in the batch."""
    source = "SEED"
    keys = [source, "A", "A", "B", "C"]
    lamports = int(0.5 * LAMPORTS)
    tx = {
        "meta": {
            "preBalances": [lamports * 4 + 5_000, 0, 0, 0, 0],
            "postBalances": [5_000, lamports, lamports, lamports, lamports],
        },
        "transaction": {"message": {"accountKeys": keys}},
    }
    signatures = {source: [{"signature": "s", "slot": 42}]}
    graph = discover_entity_graph(
        source,
        max_hops=1,
        batch_min_recipients=3,
        endpoints=ENDPOINTS,
        transport=_transport(signatures, {"s": tx}, {"s": 42}),
    )
    assert len(graph.batches) == 1
    assert sorted(graph.batches[0].recipients) == ["A", "B", "C"]
    assert graph.batches[0].total_sol == 2.0


def test_discover_graph_without_lookup_leaves_counts_zero() -> None:
    """Skipping the launch lookup still produces a graph with zero counts."""
    tx = _fanout_tx("SEED", ["A"], 0.3, 100)
    signatures = {"SEED": [{"signature": "s", "slot": 100}]}
    graph = discover_entity_graph(
        "SEED",
        max_hops=1,
        endpoints=ENDPOINTS,
        transport=_transport(signatures, {"s": tx}, {"s": 100}),
    )
    assert all(node.launch_count == 0 for node in graph.nodes)


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
