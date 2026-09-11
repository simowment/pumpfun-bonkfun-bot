"""Unit tests for entity launch-history reconstruction (no network)."""

from __future__ import annotations

from typing import Any

from rugbot.integrations.rpc_access import RpcEndpoints
from rugbot.tracker.entity_history import build_launch_history
from rugbot.tracker.funding_chain import FundedTransfer, enumerate_funded_paged

ENDPOINTS = RpcEndpoints(ordered=("http://seam",), source="test")
LAMPORTS = 1_000_000_000


def _transfer(recipient: str, amount: float, slot: int, sig: str) -> FundedTransfer:
    """Build one canned outbound transfer."""
    return FundedTransfer(
        recipient=recipient, amount_sol=amount, signature=sig, slot=slot
    )


def _coin(mint: str, symbol: str, created: int) -> dict[str, object]:
    """Build one canned creator-index coin entry."""
    return {
        "mint": mint,
        "symbol": symbol,
        "name": symbol,
        "created_timestamp": created,
    }


def test_build_history_merges_and_sorts_creations() -> None:
    """Creations from funded wallets merge into one oldest-first timeline."""
    transfers = [
        _transfer("BURNER_A", 3.3, 900, "s1"),
        _transfer("BURNER_B", 7.6, 950, "s2"),
    ]
    catalog = {
        "BURNER_A": [_coin("mintA", "COTE", 1_700_000_200_000)],
        "BURNER_B": [
            _coin("mintB", "MINIJUG", 1_700_000_100_000),
            _coin("mintC", "DUPE", 1_700_000_100_000),
        ],
    }

    def fetch(wallet: str):
        return catalog.get(wallet)

    history = build_launch_history("FUNDER", transfers=transfers, launch_fetch=fetch)
    assert history.recipients == 2
    assert [event.mint for event in history.launches] == ["mintB", "mintC", "mintA"]
    assert history.launches[-1].symbol == "COTE"
    assert history.launches[-1].received_sol == 3.3
    assert history.launches[-1].funding_slot == 900
    assert history.warning is None


def test_build_history_dedupes_mint_and_sums_funding() -> None:
    """A mint created once appears once; repeat funding is summed."""
    transfers = [
        _transfer("BURNER_A", 1.0, 10, "s1"),
        _transfer("BURNER_A", 2.0, 20, "s2"),
    ]
    catalog = {"BURNER_A": [_coin("mintA", "X", 500)]}
    history = build_launch_history(
        "FUNDER",
        transfers=transfers,
        launch_fetch=catalog.get,
    )
    assert history.recipients == 1
    assert len(history.launches) == 1
    assert history.launches[0].received_sol == 3.0
    assert history.launches[0].funding_slot == 10


def test_build_history_survives_lookup_failure() -> None:
    """One failing lookup warns without dropping the other wallets' events."""
    transfers = [_transfer("BAD", 1.0, 10, "s1"), _transfer("GOOD", 1.0, 11, "s2")]

    def fetch(wallet: str):
        if wallet == "BAD":
            raise RuntimeError("boom")
        return [_coin("mintG", "GOOD", 100)]

    history = build_launch_history("FUNDER", transfers=transfers, launch_fetch=fetch)
    assert [event.mint for event in history.launches] == ["mintG"]
    assert history.warning is not None


def _paged_transport(
    pages: dict[str | None, list[dict[str, object]]],
    transactions: dict[str, dict[str, object]],
) -> Any:
    """Fake transport serving before-cursor pages of signatures."""

    def transport(endpoint: str, method: str, params: list[object]) -> object:
        if method == "getSignaturesForAddress":
            cursor = None
            if isinstance(params[1], dict):
                cursor = params[1].get("before")
            return pages.get(cursor, [])
        if method == "getTransaction":
            return transactions.get(str(params[0]))
        unexpected = f"unexpected method {method}"
        raise AssertionError(unexpected)

    return transport


def _pay_tx(source: str, recipient: str, sol: float) -> dict[str, object]:
    """Build a parsed transaction where ``source`` pays ``recipient``."""
    lamports = int(sol * LAMPORTS)
    return {
        "meta": {
            "preBalances": [lamports + 5_000, 0],
            "postBalances": [5_000, lamports],
        },
        "transaction": {"message": {"accountKeys": [source, recipient]}},
    }


def test_paged_enumeration_walks_before_cursor() -> None:
    """Older pages are reachable via the before cursor."""
    pages = {
        None: [{"signature": "new", "slot": 200}],
        "new": [{"signature": "old", "slot": 100}],
    }
    transactions = {
        "new": _pay_tx("FUNDER", "BURNER_NEW", 3.0),
        "old": _pay_tx("FUNDER", "BURNER_OLD", 4.0),
    }
    transfers = enumerate_funded_paged(
        "FUNDER",
        max_pages=3,
        max_transactions=10,
        min_sol=0.2,
        max_sol=5.0,
        endpoints=ENDPOINTS,
        transport=_paged_transport(pages, transactions),
    )
    recipients = {t.recipient: t.amount_sol for t in transfers}
    assert recipients == {"BURNER_NEW": 3.0, "BURNER_OLD": 4.0}


def test_paged_enumeration_applies_upper_band() -> None:
    """Transfers above the staging band are filtered out."""
    pages = {None: [{"signature": "big", "slot": 5}]}
    transactions = {"big": _pay_tx("FUNDER", "TREASURY", 18.0)}
    transfers = enumerate_funded_paged(
        "FUNDER",
        max_pages=1,
        max_transactions=5,
        min_sol=0.2,
        max_sol=5.0,
        endpoints=ENDPOINTS,
        transport=_paged_transport(pages, transactions),
    )
    assert transfers == ()
