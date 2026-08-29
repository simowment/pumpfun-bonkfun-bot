"""Creator-index nomination through finalized entity mint confirmation."""

import json

import pytest

from rugbot.integrations.pumpfun_creator_index import (
    PumpfunCreatedTokenCandidate,
    fetch_pumpfun_created_tokens,
)
from rugbot.intelligence import entity_mint_index
from rugbot.intelligence.entity_mint_index import discover_finalized_entity_mints
from rugbot.intelligence.token_resolver import ResolvedTarget

TARGET = "47NQKzPusChmXE79aBhyLRfuhaMN4KF8QSLZ4tffMj13"
LINKED = "D8V1T12tYwtwn3yxihzGnAN3ar3Jr9pBrbByj6qmT6Rw"
TARGET_MINT = "7Ea62CKGbKXCEiMZmRXkGyhjdxjGc4PCAHZGj49fpump"
LINKED_MINT = "8KiXkQXRYcFKVYuyioFqdxs6cK6k1qPigPTaEHRmpump"


class _RecordedResponse:
    def __init__(self, payload: object) -> None:
        self._payload = json.dumps(payload).encode()

    def __enter__(self):
        return self

    def __exit__(self, *_args) -> None:
        return None

    def read(self) -> bytes:
        return self._payload


def test_pumpfun_creator_index_parses_recorded_live_contract(monkeypatch) -> None:
    def recorded_urlopen(request, *, timeout):
        assert request.full_url.startswith("https://frontend-api-v3.pump.fun/coins?")
        assert "creator=47NQKz" in request.full_url
        assert timeout == 15
        return _RecordedResponse(
            [
                {
                    "mint": TARGET_MINT,
                    "creator": TARGET,
                    "name": "RoboCop Leek",
                    "symbol": "COPLEEK",
                    "created_timestamp": 1_787_441_000,
                }
            ]
        )

    monkeypatch.setattr(
        "rugbot.integrations.pumpfun_creator_index.urllib.request.urlopen",
        recorded_urlopen,
    )

    tokens = fetch_pumpfun_created_tokens(TARGET)

    assert tokens == (
        PumpfunCreatedTokenCandidate(
            mint=TARGET_MINT,
            creator=TARGET,
            name="RoboCop Leek",
            symbol="COPLEEK",
            created_timestamp=1_787_441_000,
        ),
    )


@pytest.mark.anyio
async def test_entity_mints_require_finalized_creator_confirmation(monkeypatch) -> None:
    candidates = {
        TARGET: (
            PumpfunCreatedTokenCandidate(
                TARGET_MINT,
                TARGET,
                "Target Token",
                "TARGET",
                100,
            ),
        ),
        LINKED: (
            PumpfunCreatedTokenCandidate(
                LINKED_MINT,
                LINKED,
                "Linked Token",
                "LINKED",
                200,
            ),
        ),
    }

    monkeypatch.setattr(
        entity_mint_index,
        "fetch_pumpfun_created_tokens",
        lambda wallet: candidates[wallet],
    )

    def resolved(mint, **_kwargs):
        creator = TARGET if mint == TARGET_MINT else LINKED
        return ResolvedTarget(
            input_address=mint,
            target_wallet=creator,
            is_token=True,
            creation_slot=1000 if creator == TARGET else 2000,
            creation_signature="confirmed-signature",
            bonding_curve=f"curve-{mint}",
        )

    monkeypatch.setattr(entity_mint_index, "resolve_token_or_wallet", resolved)

    discovery = await discover_finalized_entity_mints(
        target_wallet=TARGET,
        graph_wallets=(LINKED,),
        endpoint="https://recorded.invalid",
    )

    assert discovery.warnings == ()
    assert [mint.mint for mint in discovery.mints] == [TARGET_MINT, LINKED_MINT]
    assert [mint.relation for mint in discovery.mints] == [
        "target_creator",
        "linked_graph_creator",
    ]
    assert [mint.bonding_curve for mint in discovery.mints] == [
        f"curve-{TARGET_MINT}",
        f"curve-{LINKED_MINT}",
    ]


@pytest.mark.anyio
async def test_entity_mint_confirmation_is_bounded_to_newest_fifteen(
    monkeypatch,
) -> None:
    candidates = tuple(
        PumpfunCreatedTokenCandidate(
            mint=f"Mint{index:02d}",
            creator=TARGET,
            name=f"Token {index}",
            symbol=f"T{index}",
            created_timestamp=index,
        )
        for index in range(20)
    )
    monkeypatch.setattr(
        entity_mint_index,
        "fetch_pumpfun_created_tokens",
        lambda _wallet: candidates,
    )
    monkeypatch.setattr(
        entity_mint_index,
        "resolve_token_or_wallet",
        lambda mint, **_kwargs: ResolvedTarget(
            input_address=mint,
            target_wallet=TARGET,
            is_token=True,
            creation_slot=int(mint[-2:]),
            creation_signature=f"signature-{mint}",
            bonding_curve=f"curve-{mint}",
        ),
    )

    discovery = await discover_finalized_entity_mints(
        target_wallet=TARGET,
        graph_wallets=(),
        endpoint="https://recorded.invalid",
    )

    assert len(discovery.mints) == 15
    assert {mint.mint for mint in discovery.mints} == {
        f"Mint{index:02d}" for index in range(5, 20)
    }
    assert discovery.warnings == (
        "entity mint confirmation limited to newest 15 of 20 indexed mints",
    )

    anchored = await discover_finalized_entity_mints(
        target_wallet=TARGET,
        graph_wallets=(),
        endpoint="https://recorded.invalid",
        max_mints=3,
        anchor_mint="Mint10",
    )
    assert {mint.mint for mint in anchored.mints} == {"Mint09", "Mint10", "Mint11"}
    assert anchored.warnings == (
        "entity mint confirmation limited to around anchor 3 of 20 indexed mints",
    )
