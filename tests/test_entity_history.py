"""Unit tests for entity launch-history reconstruction (no network)."""

from __future__ import annotations

from typing import Any

from rugbot.integrations.rpc_access import RpcEndpoints
from rugbot.tracker.entity_history import build_launch_history, merge_launch_histories
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


def test_paged_enumeration_all_pages_exhausts_cursor() -> None:
    """max_pages=None walks the before cursor until an empty page."""
    pages = {
        None: [{"signature": "s1", "slot": 400}],
        "s1": [{"signature": "s2", "slot": 300}],
        "s2": [{"signature": "s3", "slot": 200}],
        "s3": [{"signature": "s4", "slot": 100}],
    }
    transactions = {
        "s1": _pay_tx("FUNDER", "BURNER_1", 1.0),
        "s2": _pay_tx("FUNDER", "BURNER_2", 1.0),
        "s3": _pay_tx("FUNDER", "BURNER_3", 1.0),
        "s4": _pay_tx("FUNDER", "BURNER_4", 1.0),
    }

    def run(max_pages: int | None):
        return enumerate_funded_paged(
            "FUNDER",
            max_pages=max_pages,
            max_transactions=20,
            min_sol=0.2,
            max_sol=5.0,
            endpoints=ENDPOINTS,
            transport=_paged_transport(pages, transactions),
        )

    all_transfers = run(None)
    assert {t.recipient for t in all_transfers} == {
        "BURNER_1",
        "BURNER_2",
        "BURNER_3",
        "BURNER_4",
    }
    limited = run(2)
    assert {t.recipient for t in limited} == {"BURNER_1", "BURNER_2"}
    assert len(limited) < len(all_transfers)


def test_merge_launch_histories_dedupes_across_funders() -> None:
    """Cross-funder merge dedupes by mint, keeping first-funder attribution."""
    first = build_launch_history(
        "FUNDER_A",
        transfers=[
            _transfer("BURNER_A", 1.0, 10, "sa"),
            _transfer("BURNER_SHARED", 1.0, 11, "ss"),
        ],
        launch_fetch={
            "BURNER_A": [_coin("mintA", "A", 300)],
            "BURNER_SHARED": [_coin("shared", "S", 100)],
        }.get,
    )
    second = build_launch_history(
        "FUNDER_B",
        transfers=[
            _transfer("BURNER_B", 1.0, 12, "sb"),
            _transfer("BURNER_DUP", 1.0, 13, "sd"),
        ],
        launch_fetch={
            "BURNER_B": [_coin("mintB", "B", 200)],
            "BURNER_DUP": [_coin("shared", "S", 100)],
        }.get,
    )
    merged = merge_launch_histories([first, second])
    assert merged.funders == ("FUNDER_A", "FUNDER_B")
    assert [event.mint for event in merged.launches] == [
        "shared",
        "mintB",
        "mintA",
    ]
    assert next(e for e in merged.launches if e.mint == "shared").funder == ("FUNDER_A")
    assert merged.recipients == first.recipients + second.recipients
