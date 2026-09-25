"""Tests for the RPC-only funding-edge resolver (no network)."""

from __future__ import annotations

from typing import Any

from rugbot.integrations.rpc_access import RpcEndpoints
from rugbot.tracker.funding_edge_rpc import (
    find_outbound_funding_edge,
    parse_inbound_funding_edge,
    select_oldest_signatures,
)

ENDPOINTS = RpcEndpoints(ordered=("http://seam",), source="test")
LAMPORTS = 1_000_000_000


def _sig(name: str, slot: int) -> dict[str, object]:
    """Build one newest-first signature entry."""
    return {"signature": name, "slot": slot}


def _funding_tx(
    funder: str, wallet: str, amount_sol: float, others: tuple = ()
) -> dict[str, object]:
    """Build a balance-shaped tx where ``funder`` paid ``wallet``."""
    lamports = int(amount_sol * LAMPORTS)
    keys = [funder, wallet, *[other for other, _ in others]]
    pre = [lamports + 5_000, 0, *[0 for _ in others]]
    post = [5_000, lamports, *[int(a * LAMPORTS) for _, a in others]]
    return {
        "slot": 99,
        "meta": {"preBalances": pre, "postBalances": post},
        "transaction": {"signatures": ["sig-x"], "message": {"accountKeys": keys}},
    }


def _transport(
    pages: list[list[dict[str, object]]],
    transactions: dict[str, dict[str, object]],
    calls: dict[str, int],
) -> Any:
    """Build a fake transport serving canned pages then txs, counting calls."""

    def transport(endpoint: str, method: str, params: list[object]) -> object:
        calls[method] = calls.get(method, 0) + 1
        if method == "getSignaturesForAddress":
            opts = params[1] if len(params) > 1 else {}
            before = opts.get("before") if isinstance(opts, dict) else None
            if before is None:
                return pages[0]
            for index, page in enumerate(pages):
                sigs = [e["signature"] for e in page]
                if before in sigs and index + 1 < len(pages):
                    return pages[index + 1]
            return []
        if method == "getTransaction":
            return transactions.get(str(params[0]))
        raise AssertionError("unexpected method")  # noqa: TRY003 - test seam

    return transport


def test_oldest_signature_selection() -> None:
    """Oldest entries of a newest-first page come back oldest-first."""
    page = [_sig(f"s{i}", i) for i in range(5, 0, -1)]
    assert select_oldest_signatures(page, limit=3) == ["s1", "s2", "s3"]
    assert select_oldest_signatures(page, limit=10) == [
        "s1",
        "s2",
        "s3",
        "s4",
        "s5",
    ]


def test_inbound_delta_parsing_picks_largest() -> None:
    """Largest inbound delta wins; funder and amount reported."""
    tx = _funding_tx("FunderA", "WalletB", 1.5, others=(("Small", 0.1),))
    assert parse_inbound_funding_edge("WalletB", tx) == ("FunderA", 1.5)


def test_inbound_none_when_no_credit() -> None:
    """A tx where the wallet only sends (never credited) resolves to None."""
    lamports = int(1.5 * LAMPORTS)
    tx = {
        "slot": 99,
        "meta": {
            "preBalances": [lamports + 5_000, 0],
            "postBalances": [5_000, lamports],
        },
        "transaction": {
            "signatures": ["sig-x"],
            "message": {"accountKeys": ["WalletB", "SomeoneElse"]},
        },
    }
    assert parse_inbound_funding_edge("WalletB", tx) is None
    assert parse_inbound_funding_edge("WalletB", {}) is None


def test_resolver_returns_oldest_funding_edge() -> None:
    """Young wallet: one short page, oldest tx hydrated first."""
    pages = [[_sig("new", 3), _sig("mid", 2), _sig("genesis", 1)]]
    transactions = {
        "genesis": _funding_tx("FunderA", "WalletB", 2.0),
        "mid": _funding_tx("FunderA", "WalletB", 0.5),
        "new": _funding_tx("FunderA", "WalletB", 0.5),
    }
    calls: dict[str, int] = {}
    edge = find_outbound_funding_edge(
        "WalletB",
        max_pages=3,
        endpoints=ENDPOINTS,
        transport=_transport(pages, transactions, calls),
    )
    assert edge == ("FunderA", 2.0, "genesis")
    assert calls["getSignaturesForAddress"] == 1
    assert calls["getTransaction"] == 1


def test_resolver_none_on_no_inbound() -> None:
    """Nothing inbound across the oldest window returns None, never fake."""
    pages = [[_sig("only", 1)]]
    calls: dict[str, int] = {}
    edge = find_outbound_funding_edge(
        "WalletB",
        max_pages=3,
        endpoints=ENDPOINTS,
        transport=_transport(pages, {}, calls),
    )
    assert edge is None


def test_page_bound_honoured() -> None:
    """Full pages stop at max_pages; short pages stop early."""
    full = [_sig(f"p0-{j}", j) for j in range(1000)]
    pages = [full, full]
    transactions = {f"p1-{j}": _funding_tx("F", "W", 1.0) for j in range(3)}
    calls: dict[str, int] = {}
    find_outbound_funding_edge(
        "W",
        max_pages=1,
        endpoints=ENDPOINTS,
        transport=_transport(pages, transactions, calls),
    )
    assert calls["getSignaturesForAddress"] == 1

    calls.clear()
    short = [_sig("a", 2), _sig("b", 1)]
    find_outbound_funding_edge(
        "W",
        max_pages=3,
        endpoints=ENDPOINTS,
        transport=_transport([short], {}, calls),
    )
    assert calls["getSignaturesForAddress"] == 1


def test_resolver_fail_soft_on_rpc_error() -> None:
    """Transport failure returns None instead of raising."""

    def boom(endpoint: str, method: str, params: list[object]) -> object:
        raise RuntimeError("rpc down")  # noqa: TRY003 - test seam signal

    assert find_outbound_funding_edge("W", endpoints=ENDPOINTS, transport=boom) is None
    assert find_outbound_funding_edge("", endpoints=ENDPOINTS) is None
