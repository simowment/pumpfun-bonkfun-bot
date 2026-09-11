"""Unit tests for the multi-hop funding-chain tracer (no network)."""

from __future__ import annotations

from typing import Any

from rugbot.integrations.rpc_access import RpcEndpoints
from rugbot.tracker.funding_chain import (
    ROLE_HUB,
    ROLE_ORIGIN,
    ROLE_RELAY,
    enumerate_funded,
    walk_upstream,
)

ENDPOINTS = RpcEndpoints(ordered=("http://seam",), source="test")
LAMPORTS = 1_000_000_000


def _sig(name: str, slot: int) -> dict[str, object]:
    """Build one newest-first signature entry."""
    return {"signature": name, "slot": slot}


def _funding_tx(parent: str, child: str, amount_sol: float) -> dict[str, object]:
    """Build a parsed transaction where ``parent`` paid ``child``."""
    lamports = int(amount_sol * LAMPORTS)
    return {
        "meta": {
            "preBalances": [lamports + 5_000, 0],
            "postBalances": [5_000, lamports],
        },
        "transaction": {"message": {"accountKeys": [parent, child]}},
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


def test_walk_detects_hub_above_relay_chain() -> None:
    """Burner -> relay -> relay -> hub identifies the hub and stops there."""
    signatures = {
        "BURNER": [
            _sig("b3", 4004),
            _sig("b2", 4003),
            _sig("b1", 4002),
            _sig("b0", 4001),
        ],
        "relay1": [_sig("r1b", 4002), _sig("r1a", 4001)],
        "relay2": [_sig("r2b", 3998), _sig("r2a", 3997)],
        "hub": [_sig(f"h{i}", 3900 + i) for i in range(8)],
    }
    transactions = {
        "b0": _funding_tx("relay1", "BURNER", 4.0),
        "r1a": _funding_tx("relay2", "relay1", 4.0),
        "r2a": _funding_tx("hub", "relay2", 4.0),
    }
    walk = walk_upstream(
        "BURNER",
        max_hops=6,
        hub_min_signatures=6,
        endpoints=ENDPOINTS,
        transport=_transport(signatures, transactions),
    )
    roles = [node.role for node in walk.nodes]
    assert roles == [ROLE_ORIGIN, ROLE_RELAY, ROLE_RELAY, ROLE_HUB]
    assert [node.wallet for node in walk.nodes] == [
        "BURNER",
        "relay1",
        "relay2",
        "hub",
    ]
    assert walk.hub == "hub"
    assert walk.warning is None


def test_walk_stops_when_no_parent_found() -> None:
    """A wallet whose oldest transaction has no payer ends the walk cleanly."""
    signatures = {"BURNER": [_sig("b0", 10)]}
    transactions = {
        "b0": {
            "meta": {"preBalances": [0], "postBalances": [0]},
            "transaction": {"message": {"accountKeys": ["BURNER"]}},
        }
    }
    walk = walk_upstream(
        "BURNER",
        max_hops=4,
        endpoints=ENDPOINTS,
        transport=_transport(signatures, transactions),
    )
    assert walk.hub is None
    assert len(walk.nodes) == 1
    assert walk.warning is not None and "no upstream parent" in walk.warning


def test_walk_reports_hop_cap() -> None:
    """An endless relay chain stops at the hop cap with a warning."""
    signatures = {f"w{i}": [_sig(f"s{i}", 100 - i)] for i in range(8)}
    transactions = {f"s{i}": _funding_tx(f"w{i + 1}", f"w{i}", 1.0) for i in range(8)}
    walk = walk_upstream(
        "w0",
        max_hops=3,
        hub_min_signatures=6,
        endpoints=ENDPOINTS,
        transport=_transport(signatures, transactions),
    )
    assert walk.hub is None
    assert len(walk.nodes) == 3
    assert walk.warning is not None and "hop cap" in walk.warning


def test_enumerate_funded_lists_recipients() -> None:
    """Hub outbound transfers list each funded recipient above the floor."""
    signatures = {
        "hub": [_sig("t1", 500), _sig("t2", 499)],
    }
    transactions = {
        "t1": _funding_tx("hub", "siblingA", 3.5),
        "t2": _funding_tx("hub", "siblingB", 0.0000001),
    }
    funded = enumerate_funded(
        "hub",
        max_transactions=10,
        min_sol=0.01,
        endpoints=ENDPOINTS,
        transport=_transport(signatures, transactions),
    )
    recipients = [transfer.recipient for transfer in funded]
    assert recipients == ["siblingA"]
    assert funded[0].amount_sol == 3.5
