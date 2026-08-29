"""Recorded Solscan nomination through finalized RPC confirmation."""

import json
import urllib.error
from email.message import Message

import base58
import pytest

from rugbot.integrations import solscan
from rugbot.integrations.solscan import SolscanClient, SolscanProviderError
from rugbot.runtime.config import SniperConfigError, load_provider_settings
from rugbot.tracker import funder_discovery


class _Clock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture(autouse=True)
def _isolated_solscan_circuit_breaker():
    solscan._reset_circuit_breaker_for_tests()
    yield
    solscan._reset_circuit_breaker_for_tests()


def test_provider_settings_use_one_canonical_environment_contract():
    settings = load_provider_settings(
        {
            "SOLANA_RPC_HTTP": "https://rpc.example",
            "SOLANA_RPC_HTTP_FALLBACKS": (
                "https://fallback-one.example, https://fallback-two.example/path"
            ),
            "SOLANA_RPC_WEBSOCKET": "wss://rpc.example",
            "SOLSCAN_API_KEY": "solscan-secret",
            "GMGN_API_KEY": "gmgn-secret",
            "SOLANA_NODE_RPC_ENDPOINT": "https://ignored.example",
        }
    )

    assert settings.rpc_http == "https://rpc.example"
    assert settings.rpc_http_fallbacks == (
        "https://fallback-one.example",
        "https://fallback-two.example/path",
    )
    assert settings.rpc_websocket == "wss://rpc.example"
    assert settings.solscan_api_key == "solscan-secret"


@pytest.mark.parametrize(
    "fallbacks",
    (
        "wss://not-http.example",
        "https://user:password@rpc.example",
        "https://valid.example,,https://also-valid.example",
        "https://valid.example, ",
    ),
)
def test_provider_settings_reject_invalid_rpc_fallbacks(fallbacks: str) -> None:
    with pytest.raises(SniperConfigError):
        load_provider_settings(
            {
                "SOLANA_RPC_HTTP": "https://rpc.example",
                "SOLANA_RPC_HTTP_FALLBACKS": fallbacks,
            }
        )


def test_solscan_candidate_is_confirmed_by_finalized_rpc(monkeypatch):
    subject = "FJz6SLz8CQBmm692kfp6e8s9FZPuqnKX5ZNcr7k5Kadd"
    funder = "2r2HuRi1vLzVxXnWAffWfsAMDkQpfG1c23KPDgR4wp5p"
    signature = "recorded-funding-signature"

    def recorded_solscan_transport(request, timeout):
        assert request.full_url.startswith(
            "https://pro-api.solscan.io/v2.0/account/funded-by?"
        )
        assert request.get_header("Token") == "test-key"
        assert timeout == 15
        return json.dumps(
            {
                "success": True,
                "data": [
                    {
                        "address": subject,
                        "funded_by": funder,
                        "tx_hash": signature,
                        "block_time": 1_727_000_000,
                    }
                ],
            }
        ).encode()

    candidate = SolscanClient(
        "test-key", transport=recorded_solscan_transport
    ).funded_by((subject,))[0]

    def recorded_rpc(_endpoint, method, params):
        assert method == "getTransaction"
        assert params[0] == signature
        assert params[1]["commitment"] == "finalized"
        return {
            "slot": 440_000_000,
            "blockTime": 1_727_000_000,
            "meta": {"err": None, "innerInstructions": []},
            "transaction": {
                "signatures": [signature],
                "message": {
                    "instructions": [
                        {
                            "parsed": {
                                "type": "transfer",
                                "info": {
                                    "source": funder,
                                    "destination": subject,
                                    "lamports": 10_000_000,
                                },
                            }
                        }
                    ]
                },
            },
        }

    monkeypatch.setattr(funder_discovery, "_rpc_call", recorded_rpc)
    result = funder_discovery._confirm_solscan_funding(
        subject,
        "https://recorded.invalid",
        candidate,
    )

    assert result is not None
    confirmed_funder, evidence, warning = result
    assert confirmed_funder == funder
    assert evidence.signature == signature
    assert evidence.amount_lamports == 10_000_000
    assert warning is None


def test_solscan_429_cooldown_fails_fast_then_recovers():
    address = "FJz6SLz8CQBmm692kfp6e8s9FZPuqnKX5ZNcr7k5Kadd"
    headers = Message()
    headers["Retry-After"] = "12"
    responses = [
        urllib.error.HTTPError(
            "https://pro-api.solscan.io/v2.0/account/funded-by",
            429,
            "Too Many Requests",
            headers,
            None,
        ),
        json.dumps({"success": True, "data": []}).encode(),
    ]
    calls = 0

    def rate_limited_transport(_request, _timeout):
        nonlocal calls
        calls += 1
        result = responses.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    clock = _Clock()
    client = SolscanClient("test-key", transport=rate_limited_transport, clock=clock)

    with pytest.raises(SolscanProviderError, match="HTTP 429"):
        client.funded_by((address,))
    with pytest.raises(SolscanProviderError, match="cooldown is active"):
        client.funded_by((address,))
    assert calls == 1

    clock.now += 12
    assert client.funded_by((address,)) == ()
    assert calls == 2


def test_solscan_token_creation_contract() -> None:
    mint = "FJz6SLz8CQBmm692kfp6e8s9FZPuqnKX5ZNcr7k5Kadd"
    creator = "43WTM7ddYoHG44cf1rdr3RXLDJUxHh2vNerDgLgTe5uN"
    signature = base58.b58encode(bytes(range(64))).decode("ascii")

    def recorded_transport(request, _timeout):
        assert request.full_url == (
            "https://pro-api.solscan.io/v2.0/token/meta?address=" + mint
        )
        return json.dumps(
            {
                "success": True,
                "data": {
                    "address": mint,
                    "creator": creator,
                    "create_tx": signature,
                    "created_time": 1_780_000_000,
                    "name": "Recorded Token",
                    "symbol": "REC",
                },
            }
        ).encode()

    candidate = SolscanClient("test-key", transport=recorded_transport).token_creation(
        mint
    )

    assert candidate.mint == mint
    assert candidate.creator == creator
    assert candidate.transaction_signature == signature
    assert candidate.name == "Recorded Token"


def test_solscan_nominates_only_transactions_touching_known_mints() -> None:
    address = "FJz6SLz8CQBmm692kfp6e8s9FZPuqnKX5ZNcr7k5Kadd"
    mint = "43WTM7ddYoHG44cf1rdr3RXLDJUxHh2vNerDgLgTe5uN"
    program = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
    signature = base58.b58encode(bytes(range(64))).decode("ascii")

    def recorded_transport(request, _timeout):
        assert request.full_url.startswith(
            "https://pro-api.solscan.io/playground/account/transactions/enhanced?"
        )
        return json.dumps(
            {
                "success": True,
                "data": {
                    "transactions": [
                        {
                            "slot": 441_000_000,
                            "blockTime": 1_780_000_000,
                            "transactionIndex": 7,
                            "transaction": {
                                "signatures": [signature],
                                "message": {"accountKeys": [address, mint, program]},
                            },
                        },
                        {
                            "slot": 441_000_001,
                            "blockTime": 1_780_000_001,
                            "transactionIndex": 8,
                            "transaction": {
                                "signatures": [signature],
                                "message": {"accountKeys": [address, program]},
                            },
                        },
                    ],
                    "cursor": None,
                },
            }
        ).encode()

    discovery = SolscanClient(
        "test-key", transport=recorded_transport
    ).mint_transaction_candidates(
        address,
        program=program,
        mints=frozenset({mint}),
        max_pages=1,
    )

    assert discovery.complete is True
    assert discovery.pages_scanned == 1
    assert discovery.warning is None
    assert discovery.next_cursor is None
    assert len(discovery.candidates) == 1
    assert discovery.candidates[0].signature == signature
    assert discovery.candidates[0].matched_mints == (mint,)
    assert discovery.candidates[0].transaction_index == 7


def test_solscan_mint_history_preserves_pages_before_rate_limit() -> None:
    address = "FJz6SLz8CQBmm692kfp6e8s9FZPuqnKX5ZNcr7k5Kadd"
    mint = "43WTM7ddYoHG44cf1rdr3RXLDJUxHh2vNerDgLgTe5uN"
    program = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
    signature = base58.b58encode(bytes(range(64))).decode("ascii")
    calls = 0

    def recorded_transport(_request, _timeout):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise urllib.error.HTTPError(
                "https://pro-api.solscan.io/playground",
                429,
                "Too Many Requests",
                Message(),
                None,
            )
        return json.dumps(
            {
                "success": True,
                "data": {
                    "transactions": [
                        {
                            "slot": 441_000_000,
                            "blockTime": 1_780_000_000,
                            "transactionIndex": 7,
                            "transaction": {
                                "signatures": [signature],
                                "message": {"accountKeys": [address, mint, program]},
                            },
                        }
                    ],
                    "cursor": "next-page",
                },
            }
        ).encode()

    discovery = SolscanClient(
        "test-key", transport=recorded_transport
    ).mint_transaction_candidates(
        address,
        program=program,
        mints=frozenset({mint}),
        max_pages=3,
        page_pause_seconds=0,
    )

    assert discovery.complete is False
    assert discovery.pages_scanned == 1
    assert len(discovery.candidates) == 1
    assert discovery.warning == "Solscan request failed with HTTP 429"
    assert discovery.next_cursor == "next-page"
