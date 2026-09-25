"""RPC-only funding-edge resolver for young wallets (no Solscan).

Solscan-first nomination stalls when the Solscan key is rejected (HTTP 401):
every launch falls into a slow RPC burst. Target wallets are usually young
(2-5 transactions total), so the funding edge is answered directly from RPC:
page ``getSignaturesForAddress`` backward to the oldest page, then hydrate
the oldest few transactions and read the first inbound native SOL transfer.

All reads flow through :func:`rugbot.tracker.funding_chain._rpc_call`, which
serves immutable calls from the shared durable ``RpcResponseCache``
(``getTransaction`` forever, ``before``-cursor signature pages forever; the
newest page is never cached). Callers never fabricate: ``None`` means
unresolved.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Any

from rugbot.tracker.funding_chain import (
    _counterparty_transfers,
    _rpc_call,
)
from rugbot.utils.logger import get_logger

if TYPE_CHECKING:
    from collections.abc import Callable

    from rugbot.integrations.rpc_access import RpcEndpoints

logger = get_logger(__name__)

SIGNATURE_PAGE_LIMIT = 1000
DEFAULT_MAX_PAGES = 3
DEFAULT_MAX_HYDRATE = 3


def select_oldest_signatures(
    page_newest_first: Sequence[Mapping[str, object]],
    *,
    limit: int = DEFAULT_MAX_HYDRATE,
) -> list[str]:
    """Return the oldest ``limit`` signatures, oldest-first (pure).

    Args:
        page_newest_first: One signature page in RPC (newest-first) order.
        limit: Maximum signatures to keep.

    Returns:
        Up to ``limit`` signature strings, oldest first.
    """
    ordered = [
        entry["signature"]
        for entry in page_newest_first
        if isinstance(entry, Mapping) and isinstance(entry.get("signature"), str)
    ]
    keep = ordered[-max(1, int(limit)) :]
    return list(reversed(keep))


def parse_inbound_funding_edge(
    wallet: str, transaction: object
) -> tuple[str, float] | None:
    """Return the largest inbound native SOL transfer crediting ``wallet``.

    Args:
        wallet: Recipient wallet whose funding is read.
        transaction: Parsed ``getTransaction`` payload.

    Returns:
        ``(funder, amount_sol)`` for the largest inbound delta, or None.
    """
    pairs = _counterparty_transfers(
        transaction, wallet=wallet, min_sol=0.0, receiving=True
    )
    if not pairs:
        return None
    return max(pairs, key=lambda pair: pair[1])


def find_outbound_funding_edge(  # noqa: C901, PLR0912, PLR0913
    wallet: str,
    *,
    rpc_url: str | None = None,
    fallback_endpoints: Sequence[str] = (),
    max_pages: int = DEFAULT_MAX_PAGES,
    max_hydrate: int = DEFAULT_MAX_HYDRATE,
    endpoints: RpcEndpoints | Sequence[str] | None = None,
    transport: Callable[[str, str, list[object]], object] | None = None,
) -> tuple[str, float, str] | None:
    """Answer one wallet's funding edge from RPC alone (fail-soft).

    Pages ``getSignaturesForAddress`` backward with the ``before`` cursor
    until a short page or ``max_pages`` (one page for a young wallet), then
    hydrates the oldest few transactions and returns the first inbound
    native SOL transfer found, oldest first.

    Args:
        wallet: Wallet whose funding edge is answered.
        rpc_url: Preferred RPC endpoint; precedence resolution applies.
        fallback_endpoints: Ordered failover endpoints.
        max_pages: Maximum signature pages walked backward.
        max_hydrate: Maximum oldest transactions hydrated.
        endpoints: Resolved endpoints override (test seam).
        transport: Optional test transport replacing pooled RPC.

    Returns:
        ``(funder, amount_sol, signature)`` or None when unresolved.
        RPC errors return None with a warning and never raise.
    """
    if not isinstance(wallet, str) or not wallet.strip():
        return None
    subject = wallet.strip()
    page_budget = max(1, int(max_pages))
    hydrate_budget = max(1, int(max_hydrate))

    if endpoints is None:
        from rugbot.integrations.rpc_access import (  # noqa: PLC0415
            resolve_rpc_endpoints,
        )

        try:
            resolved: RpcEndpoints | Sequence[str] | None = resolve_rpc_endpoints(
                primary=rpc_url,
                fallbacks=tuple(fallback_endpoints) or None,
            )
        except Exception as exc:  # noqa: BLE001 - fail-soft
            logger.warning("funding edge RPC unavailable: %s", exc)
            return None
    else:
        resolved = endpoints

    tx_params: dict[str, object] = {
        "commitment": "finalized",
        "encoding": "jsonParsed",
        "maxSupportedTransactionVersion": 0,
    }
    try:
        oldest_page: list[Any] = []
        before: str | None = None
        for _ in range(page_budget):
            sig_params: dict[str, object] = {
                "limit": SIGNATURE_PAGE_LIMIT,
                "commitment": "finalized",
            }
            if before is not None:
                sig_params["before"] = before
            page = _rpc_call(
                "getSignaturesForAddress",
                [subject, sig_params],
                endpoints=resolved,
                transport=transport,
            )
            if not isinstance(page, list) or not page:
                break
            oldest_page = page
            if len(page) < SIGNATURE_PAGE_LIMIT:
                break
            last = page[-1]
            last_sig = last.get("signature") if isinstance(last, dict) else None
            if not isinstance(last_sig, str):
                break
            before = last_sig
        if not oldest_page:
            return None
        for signature in select_oldest_signatures(oldest_page, limit=hydrate_budget):
            tx = _rpc_call(
                "getTransaction",
                [signature, tx_params],
                endpoints=resolved,
                transport=transport,
            )
            if not isinstance(tx, dict):
                continue
            edge = parse_inbound_funding_edge(subject, tx)
            if edge is not None:
                funder, amount_sol = edge
                return funder, amount_sol, signature
        return None  # noqa: TRY300 - fail-soft terminal value, not try success
    except Exception as exc:  # noqa: BLE001 - fail-soft into the CLI
        logger.warning("funding edge RPC lookup failed for %s: %s", subject, exc)
        return None


find_inbound_funding_edge = find_outbound_funding_edge

__all__ = [
    "DEFAULT_MAX_HYDRATE",
    "DEFAULT_MAX_PAGES",
    "SIGNATURE_PAGE_LIMIT",
    "find_inbound_funding_edge",
    "find_outbound_funding_edge",
    "parse_inbound_funding_edge",
    "select_oldest_signatures",
]
