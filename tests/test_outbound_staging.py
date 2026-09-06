"""Unit tests for the question-driven staging linkers (no network)."""

import json
import urllib.request
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

from rugbot.integrations.rpc_cache import RpcResponseCache
from rugbot.integrations.solscan import (
    SolscanProviderError,
    SolscanTransferRow,
)
from rugbot.intelligence.wallet_intelligence import WalletIntelligenceReport
from rugbot.interfaces.cli import wallet as wallet_cli
from rugbot.tracker import funder_discovery as staging_module
from rugbot.tracker.cluster_graph_model import ClusterIntelligenceModel
from rugbot.tracker.funder_discovery import (
    EDGE_MAX_SOL,
    EDGE_MIN_SOL,
    MAX_FUNDING_RPC_CALLS,
    STAGED_MAX_SOL,
    STAGED_MIN_SOL,
    STAGING_ABSTAIN_BUDGET_EXCEEDED,
    STAGING_ABSTAIN_RATE_LIMITED,
    StagedTransferCandidate,
    StagingRateLimitedError,
    _find_all_incoming_transfers,
    _RpcBudget,
    find_funding_edges,
    inbound_staged_to_json,
    link_inbound_staged,
    link_outbound_staged,
    outbound_staged_to_json,
    scan_inbound_staging,
    scan_outbound_staging,
)

SUBJECT = "WalletSubject"


def _transfer_row(
    *,
    source: str,
    destination: str,
    amount_sol: float,
    signature: str = "sigIdx",
    flow: str = "out",
) -> SolscanTransferRow:
    """Build one validated Transfers-tab row fixture."""
    return SolscanTransferRow(
        from_address=source,
        to_address=destination,
        amount_sol=amount_sol,
        signature=signature,
        block_id=99,
        block_time=1700000000,
        flow=flow,
    )


class _TransferScanPage:
    """Fake Solscan client serving one fixed transfer scan."""

    def __init__(
        self,
        rows: list[SolscanTransferRow],
        warning: str | None = None,
    ) -> None:
        """Record the fixture rows served as the bounded index scan."""
        self._rows = rows
        self._warning = warning
        self.calls = 0
        self.flows: list[str] = []

    def account_transfer_scan(
        self, address: str, *, flow: str, max_pages: int = 5
    ) -> tuple[tuple[SolscanTransferRow, ...], str | None]:
        """Serve the fixture rows while counting index scans."""
        self.calls += 1
        self.flows.append(flow)
        return tuple(self._rows), self._warning


def _confirm_tx(
    *,
    source: str,
    destination: str,
    lamports: int,
    slot: int = 42,
    signature: str = "sigConfirm",
) -> dict[str, Any]:
    """Build a finalized jsonParsed confirmation transaction fixture."""
    return {
        "slot": slot,
        "blockTime": 1700000000,
        "meta": {"err": None, "innerInstructions": []},
        "transaction": {
            "signatures": [signature],
            "message": {
                "instructions": [
                    {
                        "parsed": {
                            "type": "transfer",
                            "info": {
                                "source": source,
                                "destination": destination,
                                "lamports": lamports,
                            },
                        }
                    }
                ]
            },
        },
    }


def _recording_transport(
    txs: dict[str, dict[str, Any]],
    calls: list[tuple[str, str]],
    *,
    rate_limited: bool = False,
) -> Any:
    """Build a fake RPC transport serving confirmations by signature."""

    def transport(endpoint: str, method: str, params: list[object]) -> object:
        """Serve one fixture confirmation or fail fast when configured."""
        calls.append((endpoint, method))
        if rate_limited:
            raise StagingRateLimitedError("HTTP 429")  # noqa: TRY003
        assert method == "getTransaction"
        return txs[params[0]]  # type: ignore[index]

    return transport


def _mints_for(*wallets: str) -> Any:
    """Patch target: one indexed mint per staged counterparty wallet."""

    def fetch(wallet: str) -> tuple[Any, ...]:
        """Return a mint nomination for staged wallets, none otherwise."""
        if wallet in wallets:
            return (SimpleNamespace(mint=f"{wallet}Mint"),)
        return ()

    return patch.object(staging_module, "fetch_pumpfun_created_tokens", fetch)


def _candidate(
    wallet: str, amount_sol: float, slot: int = 100, signature: str = "sig"
) -> StagedTransferCandidate:
    """Build one staged transfer candidate fixture."""
    return StagedTransferCandidate(
        wallet=wallet, amount_sol=amount_sol, slot=slot, signature=signature
    )


def test_in_window_creator_recipient_linked() -> None:
    """An in-window transfer to a later creator links with its mints."""
    edges = link_outbound_staged(
        [_candidate("RecipientA", 1.5, slot=100, signature="sigA")],
        {"RecipientA": ["MintX", "MintY"]},
    )
    assert len(edges) == 1
    assert edges[0].wallet == "RecipientA"
    assert edges[0].linked is True
    assert edges[0].created_mints == ("MintX", "MintY")
    payload = outbound_staged_to_json(edges)
    assert payload[0]["created_mints"] == ["MintX", "MintY"]


def test_out_of_window_transfers_ignored() -> None:
    """Transfers outside the staging window are excluded entirely."""
    edges = link_outbound_staged(
        [
            _candidate("TooSmall", STAGED_MIN_SOL - 0.01),
            _candidate("TooBig", STAGED_MAX_SOL + 0.01),
        ],
        {"TooSmall": ["MintA"], "TooBig": ["MintB"]},
    )
    assert edges == []


def test_window_boundaries_are_inclusive() -> None:
    """Exact window boundary amounts are treated as in-window."""
    edges = link_outbound_staged(
        [
            _candidate("LowerBound", STAGED_MIN_SOL),
            _candidate("UpperBound", STAGED_MAX_SOL),
        ],
        {},
    )
    assert [edge.wallet for edge in edges] == ["LowerBound", "UpperBound"]
    assert all(edge.linked is False for edge in edges)


def test_non_creator_recipient_noted_but_not_linked() -> None:
    """An in-window recipient without indexed mints is noted unlinked."""
    edges = link_outbound_staged([_candidate("RecipientB", 0.5)], {})
    assert len(edges) == 1
    assert edges[0].linked is False
    assert edges[0].created_mints == ()
    payload = outbound_staged_to_json(edges)
    assert payload[0]["wallet"] == "RecipientB"
    assert payload[0]["linked"] is False


def test_in_window_creator_funder_linked() -> None:
    """An in-window inbound funder that created tokens links with mints."""
    edges = link_inbound_staged(
        [_candidate("FunderA", 2.0, slot=50, signature="sigF")],
        {"FunderA": ["MintZ"]},
    )
    assert len(edges) == 1
    assert edges[0].linked is True
    assert edges[0].created_mints == ("MintZ",)
    payload = inbound_staged_to_json(edges)
    assert payload[0]["created_mints"] == ["MintZ"]


def test_inbound_out_of_window_ignored_and_non_creator_noted() -> None:
    """Out-of-window funders are dropped; plain funders are noted unlinked."""
    edges = link_inbound_staged(
        [
            _candidate("TooSmall", STAGED_MIN_SOL - 0.01),
            _candidate("TooBig", STAGED_MAX_SOL + 0.01),
            _candidate("PlainFunder", 1.0),
        ],
        {},
    )
    assert [edge.wallet for edge in edges] == ["PlainFunder"]
    assert edges[0].linked is False


def _transfer_tx(
    *,
    source: str,
    destination: str,
    lamports: int,
    err: Any = None,
) -> dict[str, Any]:
    """Build a minimal finalized jsonParsed transaction fixture."""
    return {
        "slot": 42,
        "blockTime": 1700000000,
        "meta": {"err": err, "innerInstructions": []},
        "transaction": {
            "signatures": ["sigFixture"],
            "message": {
                "instructions": [
                    {
                        "parsed": {
                            "type": "transfer",
                            "info": {
                                "source": source,
                                "destination": destination,
                                "lamports": lamports,
                            },
                        }
                    }
                ]
            },
        },
    }


def test_find_all_incoming_collects_every_crediting_transfer() -> None:
    """All crediting transfers are collected; debits and failures excluded."""
    credit = _transfer_tx(
        source="FunderA", destination="Subject", lamports=1_500_000_000
    )
    assert [e.source for e in _find_all_incoming_transfers(credit, "Subject")] == [
        "FunderA"
    ]
    debit = _transfer_tx(source="Subject", destination="Other", lamports=1_500_000_000)
    assert _find_all_incoming_transfers(debit, "Subject") == []
    failed = _transfer_tx(
        source="FunderB",
        destination="Subject",
        lamports=1_500_000_000,
        err={"InstructionError": [0, "Custom"]},
    )
    assert _find_all_incoming_transfers(failed, "Subject") == []


def test_question_driven_scan_links_two_staged_among_noise() -> None:
    """Two staged recipients link with at most 4 RPC calls (index excluded)."""
    rows = [
        _transfer_row(
            source=SUBJECT,
            destination="RecipientA",
            amount_sol=1.5,
            signature="sigA",
        ),
        _transfer_row(
            source=SUBJECT, destination="Dust", amount_sol=0.05, signature="sigD"
        ),
        _transfer_row(
            source=SUBJECT,
            destination="Whale",
            amount_sol=16.644376885,
            signature="sigW",
        ),
        _transfer_row(
            source="FunderZ",
            destination=SUBJECT,
            amount_sol=1.0,
            signature="sigZ",
            flow="in",
        ),
        _transfer_row(
            source=SUBJECT,
            destination="RecipientB",
            amount_sol=0.5,
            signature="sigB",
        ),
    ]
    page = _TransferScanPage(rows)
    rpc_calls: list[tuple[str, str]] = []
    transport = _recording_transport(
        {
            "sigA": _confirm_tx(
                source=SUBJECT,
                destination="RecipientA",
                lamports=1_500_000_000,
                signature="sigA",
            ),
            "sigB": _confirm_tx(
                source=SUBJECT,
                destination="RecipientB",
                lamports=500_000_000,
                signature="sigB",
            ),
            "sigW": _confirm_tx(
                source=SUBJECT,
                destination="Whale",
                lamports=16_644_376_885,
                signature="sigW",
            ),
        },
        rpc_calls,
    )
    with _mints_for("RecipientA", "RecipientB"):
        edges, warning, calls_made = scan_outbound_staging(
            SUBJECT,
            "http://localhost:8899",
            solscan_client=page,  # type: ignore[arg-type]
            transport=transport,
        )
    assert warning is None
    assert page.calls == 1
    assert page.flows == ["out"]
    assert len(rpc_calls) <= 4
    assert calls_made == len(rpc_calls) == 3
    assert calls_made <= MAX_FUNDING_RPC_CALLS
    linked = {edge.wallet: edge for edge in edges}
    assert set(linked) == {"RecipientA", "RecipientB", "Whale"}
    assert linked["RecipientA"].linked is True
    assert linked["RecipientB"].linked is True
    assert linked["Whale"].linked is False
    assert linked["Whale"].amount_sol == 16.644376885
    assert all(edge.source == "solscan-transfer" for edge in linked.values())


def test_sweep_size_edge_links_in_explicit_window() -> None:
    """A 13.83 SOL sweep links under the edge window, not the candidacy one."""
    edges = link_outbound_staged(
        [_candidate("Sweeper", 13.83, slot=7, signature="sigS")],
        {"Sweeper": ["MintS"]},
        min_sol=EDGE_MIN_SOL,
        max_sol=EDGE_MAX_SOL,
    )
    assert len(edges) == 1
    assert edges[0].linked is True
    assert edges[0].amount_sol == 13.83
    assert edges[0].created_mints == ("MintS",)


def test_oldest_row_wins_per_wallet() -> None:
    """Oldest-first scan order keeps the earliest edge per wallet."""
    rows = [
        _transfer_row(
            source=SUBJECT,
            destination="Repeat",
            amount_sol=1.0,
            signature="sigOld",
        ),
        _transfer_row(
            source=SUBJECT,
            destination="Repeat",
            amount_sol=2.0,
            signature="sigNew",
        ),
    ]
    page = _TransferScanPage(rows)
    transport = _recording_transport(
        {
            "sigOld": _confirm_tx(
                source=SUBJECT,
                destination="Repeat",
                lamports=1_000_000_000,
                signature="sigOld",
            ),
            "sigNew": _confirm_tx(
                source=SUBJECT,
                destination="Repeat",
                lamports=2_000_000_000,
                signature="sigNew",
            ),
        },
        [],
    )
    with _mints_for("Repeat"):
        edges, warning, _ = scan_outbound_staging(
            SUBJECT,
            "http://localhost:8899",
            solscan_client=page,  # type: ignore[arg-type]
            transport=transport,
        )
    assert warning is None
    assert len(edges) == 1
    assert edges[0].signature == "sigOld"
    assert edges[0].amount_sol == 1.0


def test_budget_exceeded_returns_partial_with_note() -> None:
    """An exhausted RPC budget returns confirmed edges plus a note."""
    page = _TransferScanPage(
        [
            _transfer_row(
                source=SUBJECT,
                destination="RecipientA",
                amount_sol=1.5,
                signature="sigA",
            ),
            _transfer_row(
                source=SUBJECT,
                destination="RecipientB",
                amount_sol=0.5,
                signature="sigB",
            ),
        ]
    )
    rpc_calls: list[tuple[str, str]] = []
    transport = _recording_transport(
        {
            "sigA": _confirm_tx(
                source=SUBJECT,
                destination="RecipientA",
                lamports=1_500_000_000,
            ),
            "sigB": _confirm_tx(
                source=SUBJECT,
                destination="RecipientB",
                lamports=500_000_000,
            ),
        },
        rpc_calls,
    )
    candidates, warning = find_funding_edges(
        SUBJECT,
        "outbound",
        "http://localhost:8899",
        solscan_client=page,  # type: ignore[arg-type]
        transport=transport,
        budget=_RpcBudget(limit=1),
    )
    assert warning == STAGING_ABSTAIN_BUDGET_EXCEEDED
    assert [candidate.wallet for candidate in candidates] == ["RecipientA"]
    assert len(rpc_calls) == 1


def test_edge_json_carries_nomination_source() -> None:
    """Serialized edges report which path nominated them."""
    edges = link_inbound_staged(
        [
            StagedTransferCandidate(
                wallet="FunderA",
                amount_sol=1.0,
                slot=5,
                signature="sigIdx",
                nominated_by="solscan",
            )
        ],
        {"FunderA": ["MintZ"]},
    )
    payload = inbound_staged_to_json(edges)
    assert payload[0]["source"] == "solscan"


def test_rate_limit_abstains_after_exactly_one_rpc_attempt() -> None:
    """A 429 on the first confirmation abstains with zero retries."""
    page = _TransferScanPage(
        [
            _transfer_row(
                source=SUBJECT,
                destination="RecipientA",
                amount_sol=1.5,
                signature="sigA",
            )
        ]
    )
    rpc_calls: list[tuple[str, str]] = []
    transport = _recording_transport({}, rpc_calls, rate_limited=True)
    edges, warning, calls_made = scan_outbound_staging(
        SUBJECT,
        "http://localhost:8899",
        solscan_client=page,  # type: ignore[arg-type]
        transport=transport,
    )
    assert edges == []
    assert warning == STAGING_ABSTAIN_RATE_LIMITED
    assert page.calls == 1
    assert len(rpc_calls) == 1
    assert calls_made == 1


def test_solscan_rate_limit_abstains_before_any_rpc_call() -> None:
    """A Solscan 429 abstains the inbound scan without RPC calls."""
    rpc_calls: list[tuple[str, str]] = []

    def transport(endpoint: str, method: str, params: list[object]) -> object:
        """Fake transport recording every RPC attempt."""
        rpc_calls.append((endpoint, method))
        return []

    solscan_calls: list[tuple[str, ...]] = []

    class RateLimitedSolscan:
        """Fake Solscan client that is always rate limited."""

        def account_transfer_scan(
            self, address: str, *, flow: str, max_pages: int = 5
        ) -> tuple[tuple[SolscanTransferRow, ...], str | None]:
            """Record the index attempt then fail with HTTP 429."""
            solscan_calls.append((address, flow))
            raise SolscanProviderError(  # noqa: TRY003
                "Solscan request failed with HTTP 429"
            )

    edges, warning, calls_made = scan_inbound_staging(
        "WalletX",
        "http://localhost:8899",
        solscan_api_key="key",
        transport=transport,
        solscan_client=RateLimitedSolscan(),  # type: ignore[arg-type]
    )
    assert edges == []
    assert warning == STAGING_ABSTAIN_RATE_LIMITED
    assert len(solscan_calls) == 1
    assert rpc_calls == []
    assert calls_made == 0


def _free_rpc_transport(
    sigs: list[dict[str, Any]],
    txs: dict[str, dict[str, Any]],
    calls: list[tuple[str, str]],
) -> Any:
    """Build a fake free-RPC transport serving pages plus confirmations."""

    def transport(endpoint: str, method: str, params: list[object]) -> object:
        """Serve one fixture page or confirmation per call."""
        calls.append((endpoint, method))
        if method == "getSignaturesForAddress":
            return sigs
        assert method == "getTransaction"
        return txs[params[0]]  # type: ignore[index]

    return transport


def test_free_rpc_fallback_without_key_links_edges() -> None:
    """No Solscan key nominates via bounded free-RPC paging with a note."""
    sigs = [{"signature": "sigF"}, {"signature": "sigNoise"}]
    rpc_calls: list[tuple[str, str]] = []
    transport = _free_rpc_transport(
        sigs,
        {
            "sigF": _confirm_tx(
                source=SUBJECT,
                destination="Staged",
                lamports=1_000_000_000,
                signature="sigF",
            ),
            "sigNoise": _confirm_tx(
                source=SUBJECT,
                destination="Dust",
                lamports=10_000_000,
                signature="sigNoise",
            ),
        },
        rpc_calls,
    )
    with _mints_for("Staged"):
        edges, warning, calls_made = scan_outbound_staging(
            SUBJECT,
            "http://localhost:8899",
            transport=transport,
        )
    assert warning is not None and "free-RPC" in warning
    assert calls_made == len(rpc_calls) == 3
    assert calls_made <= MAX_FUNDING_RPC_CALLS
    assert [edge.wallet for edge in edges] == ["Staged"]
    assert edges[0].linked is True
    assert edges[0].source == "rpc"


def test_solscan_401_falls_back_to_free_rpc() -> None:
    """A Solscan 401 degrades to free-RPC nomination instead of empty."""

    class UnauthorizedSolscan:
        """Fake Solscan client rejecting every call with HTTP 401."""

        def account_transfer_scan(
            self, address: str, *, flow: str, max_pages: int = 5
        ) -> tuple[tuple[SolscanTransferRow, ...], str | None]:
            """Record the attempt then fail with HTTP 401."""
            raise SolscanProviderError(  # noqa: TRY003
                "Solscan request failed with HTTP 401"
            )

    rpc_calls: list[tuple[str, str]] = []
    transport = _free_rpc_transport(
        [{"signature": "sigF"}],
        {
            "sigF": _confirm_tx(
                source="FunderQ",
                destination=SUBJECT,
                lamports=2_000_000_000,
                signature="sigF",
            ),
        },
        rpc_calls,
    )
    with _mints_for("FunderQ"):
        edges, warning, _calls_made = scan_inbound_staging(
            SUBJECT,
            "http://localhost:8899",
            transport=transport,
            solscan_client=UnauthorizedSolscan(),  # type: ignore[arg-type]
        )
    assert warning is not None and "401" in warning and "free-RPC" in warning
    assert [edge.wallet for edge in edges] == ["FunderQ"]
    assert edges[0].linked is True
    assert rpc_calls, "fallback must issue free-RPC calls"


def test_staging_rpc_cache_serves_repeat_calls_without_network(
    tmp_path: Any,
) -> None:
    """A repeated production-path call is served from cache (1 urlopen hit)."""
    hits: list[str] = []
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "result": {"slot": 7}}).encode()

    class _FakeResponse:
        """Minimal context-manager stub for urlopen responses."""

        def __enter__(self) -> Any:
            """Enter the fake response context."""
            return self

        def __exit__(self, *args: Any) -> bool:
            """Exit the fake response context."""
            return False

        def read(self) -> bytes:
            """Return the canned JSON-RPC body."""
            return body

    def _fake_urlopen(req: Any, timeout: int = 0) -> Any:
        """Count network hits while serving the canned body."""
        hits.append(req.full_url)
        return _FakeResponse()

    cache = RpcResponseCache(db_path=tmp_path / "staging_cache.sqlite3")
    try:
        old_singleton = staging_module._STAGING_RPC_CACHE
        staging_module._STAGING_RPC_CACHE = cache
        old_urlopen = urllib.request.urlopen
        urllib.request.urlopen = _fake_urlopen  # type: ignore[assignment]
        try:
            params: list[object] = ["sigCached", {"commitment": "finalized"}]
            first = staging_module._staging_rpc_call(
                "http://localhost:8899", "getTransaction", params
            )
            second = staging_module._staging_rpc_call(
                "http://localhost:8899", "getTransaction", params
            )
        finally:
            urllib.request.urlopen = old_urlopen  # type: ignore[assignment]
            staging_module._STAGING_RPC_CACHE = old_singleton
    finally:
        cache.close()
    assert first == {"slot": 7}
    assert second == {"slot": 7}
    assert len(hits) == 1


def _wallet_report(wallet: str) -> WalletIntelligenceReport:
    """Build a minimal finalized wallet intelligence report fixture."""
    return WalletIntelligenceReport(
        as_of_slot=1,
        target_wallet=wallet,
        history_limit=50,
        scanned_transaction_count=0,
        successful_transaction_count=0,
        first_seen_slot=None,
        last_seen_slot=None,
        launch_count=0,
        direct_linked_wallet_count=0,
        linked_creator_wallet_count=0,
        wallet_switch_candidate=False,
        native_in_lamports=0,
        native_out_lamports=0,
        launches=(),
        nodes=(),
        edges=(),
        warnings=(),
    )


def _run_wallet_json(
    monkeypatched_scans: dict[str, Any],
    argv: list[str],
    capsys: Any,
) -> dict[str, Any]:
    """Run rug_wallet --json with heavy dependencies stubbed (no network)."""
    wallet = "HX2Sr1gKJC53NKEBPEy9KRoW2M1C6NoJT241sVM4AEnA"
    providers = SimpleNamespace(
        rpc_http="http://localhost:8899",
        rpc_http_fallbacks=(),
        solscan_api_key=None,
    )
    resolved = SimpleNamespace(
        target_wallet=wallet,
        root_funder=None,
        is_token=False,
        default_label="Dev",
        name=None,
        symbol=None,
    )
    repo = MagicMock()
    repo.get_launches_for_funder.return_value = ()
    model = ClusterIntelligenceModel(root_address=wallet)
    with (
        patch.object(wallet_cli, "load_provider_settings", return_value=providers),
        patch.object(
            wallet_cli, "resolve_tracker_db_path", return_value="dummy.sqlite3"
        ),
        patch.object(wallet_cli, "DatabaseManager"),
        patch.object(wallet_cli, "SQLiteTrackerRepository", return_value=repo),
        patch.object(wallet_cli, "resolve_token_or_wallet", return_value=resolved),
        patch.object(
            wallet_cli,
            "scan_wallet_intelligence",
            AsyncMock(return_value=_wallet_report(wallet)),
        ),
        patch.object(
            wallet_cli,
            "build_cluster_intelligence_model",
            return_value=model,
        ),
        patch.object(
            wallet_cli,
            "scan_outbound_staging",
            **monkeypatched_scans["outbound"],
        ),
        patch.object(
            wallet_cli,
            "scan_inbound_staging",
            **monkeypatched_scans["inbound"],
        ),
    ):
        assert wallet_cli.main([wallet, "--json", *argv]) == 0
    return json.loads(capsys.readouterr().out)


def test_flag_off_issues_zero_scan_calls_and_skipped_note(capsys: Any) -> None:
    """Default --json path never calls staging scans and notes the skip."""
    payload = _run_wallet_json(
        {
            "outbound": {"side_effect": AssertionError("must not scan")},
            "inbound": {"side_effect": AssertionError("must not scan")},
        },
        [],
        capsys,
    )
    assert payload["outbound_staged"] == []
    assert payload["inbound_staged"] == []
    assert payload["staging_skipped"] is True


def test_flag_on_runs_staging_scans(capsys: Any) -> None:
    """--trace-funding runs both staging scans and clears the skip note."""
    payload = _run_wallet_json(
        {
            "outbound": {"return_value": ([], None, 0)},
            "inbound": {"return_value": ([], None, 0)},
        },
        ["--trace-funding"],
        capsys,
    )
    assert payload["outbound_staged"] == []
    assert payload["inbound_staged"] == []
    assert payload["staging_skipped"] is False
    assert payload["staging_warning"] is None
