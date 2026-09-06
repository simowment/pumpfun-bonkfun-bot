"""Unit tests for the entity Discord webhook path (no network)."""

import json
import urllib.request
from pathlib import Path
from typing import Any
from unittest.mock import patch

from rugbot.interfaces.cli import entity_watch
from rugbot.interfaces.discord.entity_webhook import (
    EntityEdge,
    EntityGraph,
    EntityWallet,
    EntityWatchState,
    NewMintAlert,
    build_entity_launch_payload,
    post_entity_launch_alert,
)


def _graph() -> EntityGraph:
    """Build the three-wallet operator fixture with funding edges."""
    return EntityGraph(
        name="TestEntity",
        wallets=(
            EntityWallet(address="Wallet3QyG", label="ZCAT Deployer", role="funder"),
            EntityWallet(address="WalletHX2S", label="sniper", role="launcher"),
            EntityWallet(address="Wallet7MEy", label="FIGGER Deployer", role="burner"),
        ),
        edges=(
            EntityEdge(
                source="Wallet3QyG",
                destination="WalletHX2S",
                amount_sol=13.83,
                note="funding",
            ),
            EntityEdge(
                source="WalletHX2S",
                destination="Wallet7MEy",
                amount_sol=16.64,
                note="sweep",
            ),
        ),
    )


def test_payload_carries_mint_creator_links_and_graph() -> None:
    """The embed holds the mint, creator, roster, edges, and out-links."""
    payload = build_entity_launch_payload(
        NewMintAlert(
            mint="MintNew",
            creator="WalletHX2S",
            token_name="Test",  # noqa: S106 - fixture display name, not a secret
            token_symbol="TST",  # noqa: S106 - fixture ticker, not a secret
            market_cap_usd=9900.0,
        ),
        _graph(),
    )
    assert "TestEntity" in str(payload["content"])
    embed = payload["embeds"][0]
    fields = {field["name"]: field["value"] for field in embed["fields"]}
    assert fields["Mint"] == "`MintNew`"
    assert "WalletHX" in fields["Creator"] and "sniper" in fields["Creator"]
    assert "ZCAT Deployer" in fields["Entity: TestEntity"]
    assert "16.64 SOL" in fields["Known Links"] and "sweep" in fields["Known Links"]
    assert "$9.9K" in fields["MCap"]
    assert "dexscreener.com/solana/MintNew" in fields["DexScreener"]
    assert "solscan.io/account/MintNew" in fields["Solscan"]
    assert "pump.fun/coin/MintNew" in fields["Pump"]
    assert "webhook" not in json.dumps(payload).lower()


def test_sender_posts_once_and_fails_soft() -> None:
    """The sender POSTs JSON once; failures and bad URLs return False."""
    calls: list[str] = []

    def _ok(request: urllib.request.Request, timeout: int) -> bytes:
        """Record the POST body target and succeed."""
        calls.append(request.full_url)
        assert request.get_header("Content-type") == "application/json"
        return b"{}"

    assert (
        post_entity_launch_alert(
            "https://discord.com/api/webhooks/x", {"content": "hi"}, transport=_ok
        )
        is True
    )
    assert calls == ["https://discord.com/api/webhooks/x"]

    def _down(request: urllib.request.Request, timeout: int) -> bytes:
        """Simulate a dead webhook endpoint."""
        raise OSError("unreachable")

    assert (
        post_entity_launch_alert(
            "https://discord.com/api/webhooks/x", {"content": "hi"}, transport=_down
        )
        is False
    )
    assert post_entity_launch_alert("http://plain/url", {}) is False


def test_watch_state_dedupes_known_mints() -> None:
    """Seen mints are recorded; only unseen mints alert, once each."""
    state = EntityWatchState()
    assert state.unseen(["MintA", "MintB"]) == ["MintA", "MintB"]
    assert state.unseen(["MintA", "MintC"]) == ["MintC"]
    assert state.unseen(["MintA", "MintB", "MintC"]) == []


def test_entity_graph_loads_from_file_and_csv(tmp_path: Path) -> None:
    """Entity files parse; CSV merges without duplicates; empty rejects."""
    entity_file = tmp_path / "entity.json"
    entity_file.write_text(
        json.dumps(
            {
                "name": "FileEntity",
                "wallets": [{"address": "WalletA", "label": "L", "role": "R"}],
                "edges": [
                    {"from": "WalletA", "to": "WalletB", "amount_sol": 1.5, "note": "n"}
                ],
            }
        ),
        encoding="utf-8",
    )
    graph = entity_watch.load_entity_graph("WalletA,WalletB", str(entity_file), "")
    assert graph.name == "FileEntity"
    assert [wallet.address for wallet in graph.wallets] == ["WalletA", "WalletB"]
    assert graph.edges[0].amount_sol == 1.5
    try:
        entity_watch.load_entity_graph("", None, "")
    except ValueError:
        pass
    else:
        raise AssertionError("empty entity must raise")  # noqa: TRY003 - test guard


def test_poll_once_alerts_only_new_mints(tmp_path: Path) -> None:
    """One poll posts per new mint with token meta; known mints stay silent."""
    posts: list[dict[str, Any]] = []

    class _FakeClient:
        """Fake public API client with one mint per wallet."""

        def fetch_user_created_coins(
            self, wallet: str, limit: int = 50, offset: int = 0
        ) -> dict[str, Any]:
            """Serve one canned mint for the launcher wallet."""
            if wallet == "WalletHX2S":
                return {"coins": [{"mint": "MintNew"}, {"mint": "MintOld"}]}
            return {"coins": []}

        def fetch_token(self, mint: str) -> dict[str, Any]:
            """Serve canned token metadata."""
            return {"name": "N", "symbol": "S", "usd_market_cap": 100.0}

    state = EntityWatchState(known_mints={"MintOld"})
    with (
        patch.object(entity_watch, "get_client", return_value=_FakeClient()),
        patch.object(
            entity_watch,
            "post_entity_launch_alert",
            side_effect=lambda url, payload: posts.append(payload) or True,
        ),
    ):
        found, posted = entity_watch.poll_once(_graph(), state, "https://x")
    assert (found, posted) == (1, 1)
    assert "MintNew" in str(posts[0])
    assert "MintOld" not in str(posts[0])
    with (
        patch.object(entity_watch, "get_client", return_value=_FakeClient()),
        patch.object(
            entity_watch,
            "post_entity_launch_alert",
            side_effect=lambda url, payload: posts.append(payload) or True,
        ),
    ):
        found, posted = entity_watch.poll_once(_graph(), state, "https://x")
    assert (found, posted) == (0, 0)
    assert len(posts) == 1


def test_seed_mode_records_without_posting() -> None:
    """Seed runs record current mints silently for clean first deploys."""

    class _SeedClient:
        """Fake public API client with two mints on one wallet."""

        def fetch_user_created_coins(
            self, wallet: str, limit: int = 50, offset: int = 0
        ) -> dict[str, Any]:
            """Serve two canned mints for the launcher wallet."""
            if wallet == "WalletHX2S":
                return {"coins": [{"mint": "MintA"}, {"mint": "MintB"}]}
            return {"coins": []}

        def fetch_token(self, mint: str) -> dict[str, Any]:
            """Fail the run if metadata is fetched while seeding."""
            raise AssertionError(  # noqa: TRY003 - test guard
                "seed must not fetch token metadata"
            )

    state = EntityWatchState()
    with (
        patch.object(entity_watch, "get_client", return_value=_SeedClient()),
        patch.object(
            entity_watch,
            "post_entity_launch_alert",
            side_effect=AssertionError("seed must not post"),
        ),
    ):
        found, posted = entity_watch.poll_once(
            _graph(), state, "https://x", notify=False
        )
    assert (found, posted) == (2, 0)
    assert state.known_mints == {"MintA", "MintB"}
