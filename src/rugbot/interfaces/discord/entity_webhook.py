"""Discord webhook alerts for new tokens created by a watched entity.

Raw webhook POST path (no discord.py login): the operator supplies a
channel webhook URL and an entity graph (wallets + labels + funding
edges); the watcher polls creator listings and posts one rich embed per
new mint. Presentation lives here; polling/state lives in the entity
watch CLI. Never logs or embeds secrets.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field

from rugbot.integrations.axiom import build_axiom_url
from rugbot.utils.logger import get_logger

logger = get_logger(__name__)

WEBHOOK_TIMEOUT_SECONDS = 15
EMBED_COLOR_NEW_LAUNCH = 0x22C55E
# USD threshold above which embed values compact to $K.
USD_COMPACT_THOUSAND = 1000.0


@dataclass(frozen=True, slots=True)
class EntityWallet:
    """One wallet belonging to the watched entity."""

    address: str
    label: str = ""
    role: str = ""


@dataclass(frozen=True, slots=True)
class EntityEdge:
    """One known funding/mint relationship between entity wallets."""

    source: str
    destination: str
    amount_sol: float = 0.0
    note: str = ""


@dataclass(frozen=True, slots=True)
class EntityGraph:
    """The watched entity: wallets plus their known links."""

    name: str
    wallets: tuple[EntityWallet, ...] = ()
    edges: tuple[EntityEdge, ...] = ()

    def label_for(self, address: str) -> str:
        """Return the display label for a wallet, or its short address."""
        for wallet in self.wallets:
            if wallet.address == address and wallet.label:
                return wallet.label
        return f"{address[:8]}..."

    def short(self, address: str) -> str:
        """Return the short address form used inside embed fields."""
        return f"`{address[:8]}...`"


@dataclass(frozen=True, slots=True)
class NewMintAlert:
    """One new mint detected on an entity wallet."""

    mint: str
    creator: str
    token_name: str = ""
    token_symbol: str = ""
    market_cap_usd: float = 0.0
    pool_address: str = ""


def _usd_compact(value: float) -> str:
    """Format a USD value compactly for embed fields."""
    if value >= USD_COMPACT_THOUSAND:
        return f"${value / USD_COMPACT_THOUSAND:.1f}K"
    if value >= 1.0:
        return f"${value:.2f}"
    return f"${value:.4f}"


def build_entity_launch_payload(
    alert: NewMintAlert, graph: EntityGraph
) -> dict[str, object]:
    """Build the Discord webhook JSON for one entity mint (pure).

    Args:
        alert: The new mint and its token metadata.
        graph: The entity roster plus known funding edges.

    Returns:
        Webhook payload mapping with content plus one embed.
    """

    title_symbol = alert.token_symbol or "New Token"
    axiom_url = build_axiom_url(alert.mint, pool_address=alert.pool_address or None)
    fields: list[dict[str, object]] = [
        {"name": "Mint", "value": f"`{alert.mint}`", "inline": False},
        {
            "name": "Creator",
            "value": f"{graph.short(alert.creator)} {graph.label_for(alert.creator)}",
            "inline": True,
        },
    ]
    if alert.token_name or alert.token_symbol:
        fields.append(
            {
                "name": "Token",
                "value": f"**{alert.token_name or '?'}** ({alert.token_symbol or '?'})",
                "inline": True,
            }
        )
    if alert.market_cap_usd > 0:
        fields.append(
            {
                "name": "MCap",
                "value": _usd_compact(alert.market_cap_usd),
                "inline": True,
            }
        )
    roster = "\n".join(
        f"• {graph.short(wallet.address)} {wallet.label or ''}"
        f"{f' — {wallet.role}' if wallet.role else ''}".rstrip()
        for wallet in graph.wallets
    )
    if roster:
        fields.append(
            {"name": f"Entity: {graph.name}", "value": roster, "inline": False}
        )
    if graph.edges:
        edge_lines = []
        for edge in graph.edges:
            amount = f" {edge.amount_sol:g} SOL" if edge.amount_sol > 0 else ""
            note = f" ({edge.note})" if edge.note else ""
            edge_lines.append(
                f"• {graph.short(edge.source)} → {graph.short(edge.destination)}"
                f"{amount}{note}"
            )
        fields.append(
            {"name": "Known Links", "value": "\n".join(edge_lines), "inline": False}
        )
    fields.extend(
        [
            {
                "name": "Axiom",
                "value": f"[trade]({axiom_url})",
                "inline": True,
            },
            {
                "name": "DexScreener",
                "value": f"[chart](https://dexscreener.com/solana/{alert.mint})",
                "inline": True,
            },
            {
                "name": "Pump",
                "value": f"[pump](https://pump.fun/coin/{alert.mint})",
                "inline": True,
            },
            {
                "name": "Solscan",
                "value": f"[txs](https://solscan.io/account/{alert.mint})",
                "inline": True,
            },
        ]
    )
    return {
        "content": f"🚀 **{graph.name}** new launch: **[{title_symbol}]({axiom_url})**",
        "embeds": [
            {
                "title": f"🚀 Entity Launch · {title_symbol}",
                "url": axiom_url,
                "color": EMBED_COLOR_NEW_LAUNCH,
                "fields": fields,
                "footer": {"text": "rugbot entity watch · observe-only alert"},
            }
        ],
    }


WebhookTransport = Callable[[urllib.request.Request, int], bytes]


def post_entity_launch_alert(
    webhook_url: str,
    payload: dict[str, object],
    *,
    transport: WebhookTransport | None = None,
    timeout_seconds: int = WEBHOOK_TIMEOUT_SECONDS,
) -> bool:
    """POST one entity alert to a Discord webhook URL (fail-soft).

    Args:
        webhook_url: Full Discord webhook URL (never logged).
        payload: Webhook JSON from :func:`build_entity_launch_payload`.
        transport: Optional test seam replacing the single HTTP call.
        timeout_seconds: HTTP timeout for the live call.

    Returns:
        True when Discord accepted the post, False otherwise (never raises).
    """

    if not webhook_url.startswith("https://"):
        logger.warning("entity webhook refused: non-https URL")
        return False
    body = json.dumps(payload).encode()
    request = urllib.request.Request(  # noqa: S310 - https enforced below
        webhook_url,
        data=body,
        headers={"Content-Type": "application/json", "User-Agent": "rugbot/2.0"},
        method="POST",
    )
    try:
        if transport is not None:
            transport(request, timeout_seconds)
        else:
            with urllib.request.urlopen(  # noqa: S310 - https enforced above
                request, timeout=timeout_seconds
            ):
                pass
    except Exception as exc:  # noqa: BLE001 - sender is fail-soft by contract
        logger.warning("entity webhook post failed: %s", type(exc).__name__)
        return False
    return True


@dataclass(slots=True)
class EntityWatchState:
    """Persisted known-mint set for one watched entity."""

    known_mints: set[str] = field(default_factory=set)

    def unseen(self, mints: list[str]) -> list[str]:
        """Return mints not yet seen, recording everything observed."""
        fresh = [mint for mint in mints if mint not in self.known_mints]
        self.known_mints.update(mints)
        return fresh
