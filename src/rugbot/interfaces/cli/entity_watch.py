"""Watch entity wallets for new token creations, alerting via Discord webhook.

Polls the public creator-coin listing per wallet (no RPC, no key),
diffs against persisted known mints under ``.state/entity_watches/``,
and posts one rich embed per new mint with the entity roster plus
known funding links. Fail-soft throughout: alert failures never fail
the run and never re-alert on retry.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import TYPE_CHECKING

from rugbot.integrations.pumpfun_api import PumpFunApiClient, get_client
from rugbot.interfaces.discord.entity_webhook import (
    EntityEdge,
    EntityGraph,
    EntityWallet,
    EntityWatchState,
    NewMintAlert,
    build_entity_launch_payload,
    post_entity_launch_alert,
)
from rugbot.runtime.config import resolve_dotenv, resolve_state_dir
from rugbot.utils.logger import get_logger

if TYPE_CHECKING:
    from collections.abc import Sequence

logger = get_logger(__name__)

ENTITY_WATCH_SUBDIR = Path(".state/entity_watches")
POLL_INTERVAL_SECONDS = 60
CREATOR_LISTING_LIMIT = 50
USD_COMPACT_THOUSAND = 1000.0


def build_parser() -> argparse.ArgumentParser:
    """Build the entity-watch argument parser."""
    parser = argparse.ArgumentParser(
        description="Watch entity wallets for new mints; alert Discord webhook.",
    )
    parser.add_argument(
        "--wallets",
        type=str,
        default="",
        help="Comma-separated entity wallet addresses.",
    )
    parser.add_argument(
        "--entity-file",
        type=str,
        default=None,
        help="JSON entity graph (name, wallets[{address,label,role}], "
        "edges[{from,to,amount_sol,note}]).",
    )
    parser.add_argument(
        "--name",
        type=str,
        default="",
        help="Entity name for alerts and state file (default: first wallet).",
    )
    parser.add_argument(
        "--webhook",
        type=str,
        default=None,
        help="Discord webhook URL (default: DISCORD_ENTITY_WEBHOOK_URL).",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Single poll pass, then exit (cron-friendly).",
    )
    parser.add_argument(
        "--seed",
        action="store_true",
        help="Record current mints as known without posting alerts.",
    )
    parser.add_argument(
        "--history",
        action="store_true",
        help="Print the full indexed launch history (no alerts, no state).",
    )
    parser.add_argument(
        "--loop",
        action="store_true",
        help="Poll forever until interrupted.",
    )
    parser.add_argument(
        "--interval",
        type=int,
        default=POLL_INTERVAL_SECONDS,
        help="Seconds between loop polls (default: 60).",
    )
    parser.add_argument(
        "--test-ping",
        action="store_true",
        help="Post one online notice, then exit.",
    )
    return parser


def load_entity_graph(
    wallets_csv: str, entity_file: str | None, name: str
) -> EntityGraph:
    """Build the entity graph from CLI args (raises on empty/invalid input)."""
    wallets: list[EntityWallet] = []
    edges: list[EntityEdge] = []
    graph_name = name
    if entity_file:
        document = json.loads(Path(entity_file).read_text(encoding="utf-8"))
        if not isinstance(document, dict):
            raise ValueError(  # noqa: TRY003 - CLI input error surfaced to stderr
                "entity file must hold one JSON object"
            )
        graph_name = graph_name or str(document.get("name", ""))
        for entry in document.get("wallets", []):
            if not isinstance(entry, dict) or not entry.get("address"):
                raise ValueError(  # noqa: TRY003 - CLI input error, see above
                    "entity wallets need address entries"
                )
            wallets.append(
                EntityWallet(
                    address=str(entry["address"]),
                    label=str(entry.get("label", "")),
                    role=str(entry.get("role", "")),
                )
            )
        for entry in document.get("edges", []):
            if not isinstance(entry, dict) or not entry.get("from"):
                raise ValueError(  # noqa: TRY003 - CLI input error, see above
                    "entity edges need from/to entries"
                )
            amount = entry.get("amount_sol", 0.0)
            edges.append(
                EntityEdge(
                    source=str(entry["from"]),
                    destination=str(entry.get("to", "")),
                    amount_sol=float(amount)
                    if isinstance(amount, (int, float))
                    else 0.0,
                    note=str(entry.get("note", "")),
                )
            )
    for address in [part.strip() for part in wallets_csv.split(",") if part.strip()]:
        if address not in [wallet.address for wallet in wallets]:
            wallets.append(EntityWallet(address=address))
    if not wallets:
        raise ValueError(  # noqa: TRY003 - CLI input error, see above
            "no entity wallets: pass --wallets or --entity-file"
        )
    return EntityGraph(
        name=graph_name or wallets[0].address[:8],
        wallets=tuple(wallets),
        edges=tuple(edges),
    )


def _state_path(name: str) -> Path:
    """Resolve the persisted known-mint file for one entity."""
    slug = "".join(char for char in name.lower() if char.isalnum()) or "entity"
    state_dir = resolve_state_dir(ENTITY_WATCH_SUBDIR)
    return state_dir / f"{slug}.json"


def load_watch_state(path: Path) -> EntityWatchState:
    """Load known mints, starting empty when the file is absent/invalid."""
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
        known = document.get("known_mints", [])
        if isinstance(known, list):
            return EntityWatchState(
                known_mints={mint for mint in known if isinstance(mint, str)}
            )
    except (OSError, ValueError):
        pass
    return EntityWatchState()


def save_watch_state(path: Path, state: EntityWatchState) -> None:
    """Persist known mints, logging instead of raising on failure."""
    try:
        path.write_text(
            json.dumps({"known_mints": sorted(state.known_mints)}), encoding="utf-8"
        )
    except OSError as exc:
        logger.warning("entity watch state save failed for %s: %s", path, exc)


def poll_once(
    graph: EntityGraph,
    state: EntityWatchState,
    webhook_url: str,
    *,
    notify: bool = True,
) -> tuple[int, int]:
    """Poll all entity wallets once; post alerts for new mints.

    Args:
        graph: The watched entity roster.
        state: Persisted known-mint set, updated in place.
        webhook_url: Discord webhook URL for alerts.
        notify: Post alerts (False records silently for seeding).

    Returns:
        Tuple of (new mints found, alerts posted).
    """

    client = get_client()
    found = 0
    posted = 0
    for wallet in graph.wallets:
        try:
            listing = client.fetch_user_created_coins(
                wallet.address, limit=CREATOR_LISTING_LIMIT, offset=0
            )
        except Exception as exc:  # noqa: BLE001 - poll is additive fail-soft
            logger.warning("entity watch listing failed: %s", type(exc).__name__)
            continue
        coins = listing.get("coins") if isinstance(listing, dict) else None
        if not isinstance(coins, list):
            continue
        mints = [
            str(coin.get("mint", coin.get("address", "")))
            for coin in coins
            if isinstance(coin, dict)
        ]
        mints = [mint for mint in mints if mint]
        for mint in state.unseen(mints):
            found += 1
            if not notify:
                continue
            token_name = token_symbol = ""
            market_cap_usd = 0.0
            pool_address = ""
            try:
                meta = client.fetch_token(mint)
            except Exception as exc:  # noqa: BLE001 - meta is best effort
                logger.warning("entity watch token meta failed: %s", type(exc).__name__)
                meta = {}
            if isinstance(meta, dict):
                token_name = str(meta.get("name", ""))
                token_symbol = str(meta.get("symbol", ""))
                market_cap_usd = _to_float(meta.get("usd_market_cap"))
                pool_address = str(
                    meta.get("pump_swap_pool")
                    or (meta.get("pool_address") if meta.get("complete") else None)
                    or meta.get("bonding_curve")
                    or ""
                )
            payload = build_entity_launch_payload(
                NewMintAlert(
                    mint=mint,
                    creator=wallet.address,
                    token_name=token_name,
                    token_symbol=token_symbol,
                    market_cap_usd=market_cap_usd,
                    pool_address=pool_address,
                ),
                graph,
            )
            if post_entity_launch_alert(webhook_url, payload):
                posted += 1
    return found, posted


def _to_float(value: object) -> float:
    """Parse a numeric metadata field, defaulting to zero."""
    try:
        parsed = float(str(value))
    except (TypeError, ValueError):
        return 0.0
    return parsed if parsed >= 0 else 0.0


def _collect_entity_mints(
    client: PumpFunApiClient, wallet: EntityWallet, *, max_pages: int
) -> list[str]:
    """Collect every indexed mint for one wallet across listing pages."""
    mints: list[str] = []
    offset = 0
    for _ in range(max_pages):
        try:
            listing = client.fetch_user_created_coins(
                wallet.address, limit=CREATOR_LISTING_LIMIT, offset=offset
            )
        except Exception as exc:  # noqa: BLE001 - history is additive fail-soft
            logger.warning("history listing failed: %s", type(exc).__name__)
            break
        coins = listing.get("coins") if isinstance(listing, dict) else None
        if not isinstance(coins, list) or not coins:
            break
        for coin in coins:
            if not isinstance(coin, dict):
                continue
            mint = coin.get("mint", coin.get("address", ""))
            if isinstance(mint, str) and mint:
                mints.append(mint)
        if len(coins) < CREATOR_LISTING_LIMIT:
            break
        offset += CREATOR_LISTING_LIMIT
    return mints


def _mint_display_line(client: PumpFunApiClient, mint: str) -> str:
    """Render one history line for a mint with best-effort metadata."""
    name = symbol = mcap = ""
    try:
        meta = client.fetch_token(mint)
    except Exception as exc:  # noqa: BLE001 - meta is best effort
        logger.warning("history meta failed: %s", type(exc).__name__)
        meta = {}
    if isinstance(meta, dict):
        name = str(meta.get("name", ""))
        symbol = str(meta.get("symbol", ""))
        market_cap = _to_float(meta.get("usd_market_cap"))
        if market_cap >= USD_COMPACT_THOUSAND:
            mcap = f" ${market_cap / USD_COMPACT_THOUSAND:.1f}K"
        elif market_cap > 0:
            mcap = f" ${market_cap:.2f}"
    tag = f" {name} ({symbol})" if name or symbol else ""
    return f"    {mint[:12]}...{tag}{mcap}"


def print_entity_history(graph: EntityGraph, *, max_pages: int = 10) -> int:
    """Print every indexed mint per entity wallet with token metadata.

    Read-only: never posts alerts and never mutates watch state. Used for
    the full-history dossier behind ``--history``.

    Returns:
        Total mint count across all entity wallets.
    """
    client = get_client()
    total = 0
    print(f"entity {graph.name}: full launch history ({len(graph.wallets)} wallets)")
    for wallet in graph.wallets:
        mints = _collect_entity_mints(client, wallet, max_pages=max_pages)
        label = f" {wallet.label}" if wallet.label else ""
        print(f"- {wallet.address[:8]}...{label}: {len(mints)} mints")
        for mint in mints:
            print(_mint_display_line(client, mint))
        total += len(mints)
    print(f"entity {graph.name}: {total} mints total")
    return total


def _run_once_mode(
    graph: EntityGraph,
    state: EntityWatchState,
    webhook_url: str,
    args: argparse.Namespace,
) -> int:
    """Run one poll pass: seed silently or alert, then persist and report."""
    if args.seed:
        found, _ = poll_once(graph, state, webhook_url, notify=False)
        save_watch_state(_state_path(graph.name), state)
        print(f"entity {graph.name}: seeded {found} mints, 0 alerts posted")
        return 0
    found, posted = poll_once(graph, state, webhook_url)
    save_watch_state(_state_path(graph.name), state)
    print(f"entity {graph.name}: {found} new mints, {posted} alerts posted")
    return 0


def _run_watch_loop(
    graph: EntityGraph, state: EntityWatchState, webhook_url: str, interval: int
) -> int:
    """Poll forever until interrupted, posting new-mint alerts."""
    print(f"entity {graph.name}: watching {len(graph.wallets)} wallets")
    try:
        while True:
            found, posted = poll_once(graph, state, webhook_url)
            save_watch_state(_state_path(graph.name), state)
            if found:
                print(f"entity {graph.name}: {found} new mints, {posted} posted")
            time.sleep(interval)
    except KeyboardInterrupt:
        print("entity watch stopped")
        return 0


def main(argv: Sequence[str] | None = None) -> int:
    """Run the entity watcher (returns process exit code)."""
    args = build_parser().parse_args(argv)
    resolve_dotenv()
    try:
        graph = load_entity_graph(args.wallets, args.entity_file, args.name)
    except (OSError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2
    if args.history:
        print_entity_history(graph)
        return 0
    webhook_url = args.webhook or os.environ.get("DISCORD_ENTITY_WEBHOOK_URL", "")
    if not webhook_url:
        print(
            "Error: Discord webhook required (--webhook or "
            "DISCORD_ENTITY_WEBHOOK_URL).",
            file=sys.stderr,
        )
        return 2
    if args.test_ping:
        posted = post_entity_launch_alert(
            webhook_url,
            {
                "content": f"📡 rugbot entity watch online: **{graph.name}** "
                f"({len(graph.wallets)} wallets)"
            },
        )
        print("test ping posted" if posted else "test ping failed")
        return 0 if posted else 1
    state = load_watch_state(_state_path(graph.name))
    if args.seed or args.once or not args.loop:
        return _run_once_mode(graph, state, webhook_url, args)
    return _run_watch_loop(graph, state, webhook_url, max(args.interval, 10))


if __name__ == "__main__":
    raise SystemExit(main())
