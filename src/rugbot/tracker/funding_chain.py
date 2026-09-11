"""Multi-hop upstream funding-chain tracer for Type-2 operator discovery.

Type-2 operators fund every launch from a fresh burner, and that burner is
itself funded through a chain of single-use relay wallets (each roughly a
two-signature in/out pass-through). Per-wallet launch history is therefore
structurally meaningless for this archetype: the operator's real history
must be reconstructed by walking the funding chain upward to the hub that
funds many relays, then enumerating that hub's outbound transfers.

Every call routes through :func:`rugbot.integrations.rpc_access.sync_rpc_result`
so the configured endpoint pool supplies failover. That is deliberately
different from the bounded free-RPC nomination path, which was observed to
return an empty candidate set for a two-signature wallet whose funding
transaction the primary endpoint finds trivially.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

from rugbot.integrations.rpc_access import (
    RpcAccessError,
    RpcEndpoints,
    resolve_rpc_endpoints,
    sync_rpc_result,
)
from rugbot.tracker.models import LAMPORTS_PER_SOL
from rugbot.utils.logger import get_logger

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

logger = get_logger(__name__)

SIGNATURE_PAGE_LIMIT = 1000
DEFAULT_MAX_HOPS = 10
HUB_MIN_SIGNATURES = 6
MIN_TRANSFER_SOL = 0.01
DEFAULT_MAX_HUB_TRANSACTIONS = 60
DEFAULT_HISTORY_PAGES = 3
DEFAULT_HISTORY_TRANSACTIONS = 200
PRODUCTION_PACING_SECONDS = 0.35
ROLE_ORIGIN = "origin"
ROLE_RELAY = "relay"
ROLE_HUB = "hub"

_ERR_ADDRESS = "wallet address must be a non-empty string"
_ERR_MAX_HOPS = "max_hops must be at least 1"
_ERR_NO_ENDPOINT = "no RPC endpoint is configured"
_WARN_CYCLE = "cycle detected in funding chain"
_WARN_HOP_CAP = "hop cap reached before a hub"


class FundingChainError(ValueError):
    """A funding-chain walk received unusable input or RPC configuration."""


@dataclass(frozen=True, slots=True)
class FundingChainNode:
    """One wallet visited on the walk toward the funding hub."""

    wallet: str
    role: str
    signature_count: int
    oldest_slot: int | None
    newest_slot: int | None


@dataclass(frozen=True, slots=True)
class FundedTransfer:
    """One outbound SOL transfer observed leaving a hub wallet."""

    recipient: str
    amount_sol: float
    signature: str
    slot: int | None


@dataclass(frozen=True, slots=True)
class FundingSource:
    """One inbound SOL transfer that funded a wallet."""

    sender: str
    amount_sol: float
    signature: str
    slot: int | None


@dataclass(frozen=True, slots=True)
class FundingChainWalk:
    """Result of walking upstream from an origin wallet to its hub."""

    nodes: tuple[FundingChainNode, ...]
    hub: str | None
    warning: str | None


def _require_address(address: object) -> str:
    """Narrow one wallet argument to a non-empty string."""
    if not isinstance(address, str) or not address.strip():
        raise FundingChainError(_ERR_ADDRESS)
    return address.strip()


def _rpc_call(
    method: str,
    params: Sequence[object],
    *,
    endpoints: RpcEndpoints | Sequence[str] | None,
    transport: Callable[[str, str, list[object]], object] | None,
) -> object:
    """Perform one JSON-RPC call through the pooled path or a test seam."""
    if transport is None:
        return sync_rpc_result(method, params, endpoints=endpoints)
    resolved = resolve_rpc_endpoints() if endpoints is None else endpoints
    endpoint = resolved.primary if isinstance(resolved, RpcEndpoints) else resolved[0]
    if endpoint is None:
        raise FundingChainError(_ERR_NO_ENDPOINT)
    return transport(endpoint, method, list(params))


def _account_keys(message: object) -> list[str]:
    """Extract account key strings from a jsonParsed message payload."""
    if not isinstance(message, dict):
        return []
    raw_keys = message.get("accountKeys")
    if not isinstance(raw_keys, list):
        return []
    keys: list[str] = []
    for entry in raw_keys:
        if isinstance(entry, str):
            keys.append(entry)
        elif isinstance(entry, dict) and isinstance(entry.get("pubkey"), str):
            keys.append(entry["pubkey"])
    return keys


def _parsed_balances(
    result: object,
) -> tuple[list[str], list[int], list[int]] | None:
    """Return ``(account_keys, pre_balances, post_balances)`` when parseable."""
    if not isinstance(result, dict):
        return None
    meta = result.get("meta")
    if not isinstance(meta, dict):
        return None
    transaction = result.get("transaction")
    message = transaction.get("message") if isinstance(transaction, dict) else None
    keys = _account_keys(message)
    pre = meta.get("preBalances")
    post = meta.get("postBalances")
    if not isinstance(pre, list) or not isinstance(post, list):
        return None
    if not all(isinstance(value, int) for value in pre):
        return None
    if not all(isinstance(value, int) for value in post):
        return None
    return keys, list(pre), list(post)


def _signatures(
    wallet: str,
    *,
    endpoints: RpcEndpoints | Sequence[str] | None,
    transport: Callable[[str, str, list[object]], object] | None,
) -> list[dict[str, object]] | None:
    """Fetch the newest-first signature page, or None when unavailable."""
    try:
        result = _rpc_call(
            "getSignaturesForAddress",
            [wallet, {"limit": SIGNATURE_PAGE_LIMIT, "commitment": "finalized"}],
            endpoints=endpoints,
            transport=transport,
        )
    except RpcAccessError:
        logger.warning("funding chain signature fetch unavailable")
        return None
    if not isinstance(result, list):
        return None
    return [entry for entry in result if isinstance(entry, dict)]


def _transaction(
    signature: str,
    *,
    endpoints: RpcEndpoints | Sequence[str] | None,
    transport: Callable[[str, str, list[object]], object] | None,
) -> object:
    """Fetch one parsed transaction, returning None when unavailable."""
    try:
        return _rpc_call(
            "getTransaction",
            [
                signature,
                {"maxSupportedTransactionVersion": 0, "encoding": "jsonParsed"},
            ],
            endpoints=endpoints,
            transport=transport,
        )
    except RpcAccessError:
        logger.warning("funding chain transaction fetch unavailable")
        return None


def _slots(signatures: list[dict[str, object]]) -> tuple[int | None, int | None]:
    """Return ``(oldest, newest)`` slots from a newest-first signature page."""
    if not signatures:
        return (None, None)
    newest = signatures[0].get("slot")
    oldest = signatures[-1].get("slot")
    return (
        int(oldest) if isinstance(oldest, int) else None,
        int(newest) if isinstance(newest, int) else None,
    )


def _parent_of(
    wallet: str,
    signatures: list[dict[str, object]],
    *,
    endpoints: RpcEndpoints | Sequence[str] | None,
    transport: Callable[[str, str, list[object]], object] | None,
) -> str | None:
    """Return the wallet that funded ``wallet`` via its oldest transaction."""
    if not signatures:
        return None
    oldest = signatures[-1].get("signature")
    if not isinstance(oldest, str):
        return None
    parsed = _parsed_balances(
        _transaction(oldest, endpoints=endpoints, transport=transport)
    )
    if parsed is None:
        return None
    keys, pre, post = parsed
    payer: str | None = None
    largest = 0
    for index in range(min(len(keys), len(pre), len(post))):
        if keys[index] == wallet:
            continue
        outflow = pre[index] - post[index]
        if outflow > largest:
            largest = outflow
            payer = keys[index]
    return payer


def _counterparty_transfers(
    result: object,
    *,
    wallet: str,
    min_sol: float,
    receiving: bool,
) -> list[tuple[str, float]]:
    """Extract the counterparties a wallet transacted SOL with.

    Args:
        result: Parsed transaction payload.
        wallet: Wallet whose perspective is being read.
        min_sol: Ignore movements below this SOL amount.
        receiving: True to read counterparties that PAID ``wallet``;
            False to read counterparties ``wallet`` PAID.

    Returns:
        ``(counterparty, amount_sol)`` pairs for the requested direction.
    """
    parsed = _parsed_balances(result)
    if parsed is None:
        return []
    keys, pre, post = parsed
    pairs: list[tuple[str, float]] = []
    for index in range(min(len(keys), len(pre), len(post))):
        if keys[index] == wallet:
            continue
        delta = post[index] - pre[index]
        # receiving=True means the counterparty PAID the wallet, so it lost
        # funds (negative delta); receiving=False means the wallet paid the
        # counterparty, so the counterparty gained (positive delta).
        amount_lamports = -delta if receiving else delta
        if amount_lamports <= 0:
            continue
        amount_sol = amount_lamports / LAMPORTS_PER_SOL
        if amount_sol < min_sol:
            continue
        pairs.append((keys[index], amount_sol))
    return pairs


def walk_upstream(
    address: str,
    *,
    max_hops: int = DEFAULT_MAX_HOPS,
    hub_min_signatures: int = HUB_MIN_SIGNATURES,
    endpoints: RpcEndpoints | Sequence[str] | None = None,
    transport: Callable[[str, str, list[object]], object] | None = None,
) -> FundingChainWalk:
    """Walk the funding chain upward until a hub wallet is reached.

    Args:
        address: Origin wallet (typically the launch burner).
        max_hops: Maximum wallets to visit before stopping.
        hub_min_signatures: Signature count at or above which an upstream
            wallet is treated as the hub rather than a single-use relay.
        endpoints: Resolved endpoints; defaults to precedence resolution.
        transport: Optional test seam replacing the pooled transport.

    Returns:
        FundingChainWalk with every visited node, the detected hub, and an
        optional warning describing why the walk stopped early.

    Raises:
        FundingChainError: When ``address`` is empty, ``max_hops`` is below
            one, or no RPC endpoint is configured.
    """
    current = _require_address(address)
    if max_hops < 1:
        raise FundingChainError(_ERR_MAX_HOPS)
    nodes: list[FundingChainNode] = []
    hub: str | None = None
    warning: str | None = None
    visited: set[str] = set()
    for hop in range(max_hops):
        if current in visited:
            warning = _WARN_CYCLE
            break
        visited.add(current)
        if transport is None and hop > 0:
            time.sleep(PRODUCTION_PACING_SECONDS)
        signatures = _signatures(current, endpoints=endpoints, transport=transport)
        if signatures is None:
            warning = f"signature fetch failed at {current[:8]}"
            break
        is_hub = hop > 0 and len(signatures) >= hub_min_signatures
        role = ROLE_ORIGIN if hop == 0 else (ROLE_HUB if is_hub else ROLE_RELAY)
        oldest, newest = _slots(signatures)
        nodes.append(
            FundingChainNode(
                wallet=current,
                role=role,
                signature_count=len(signatures),
                oldest_slot=oldest,
                newest_slot=newest,
            )
        )
        if is_hub:
            hub = current
            break
        parent = _parent_of(
            current, signatures, endpoints=endpoints, transport=transport
        )
        if parent is None:
            warning = f"no upstream parent found above {current[:8]}"
            break
        current = parent
    else:
        warning = _WARN_HOP_CAP
    return FundingChainWalk(nodes=tuple(nodes), hub=hub, warning=warning)


def _enumerate_direction(  # noqa: PLR0913
    wallet: str,
    *,
    receiving: bool,
    max_transactions: int,
    min_sol: float,
    endpoints: RpcEndpoints | Sequence[str] | None,
    transport: Callable[[str, str, list[object]], object] | None,
) -> tuple[tuple[str, float, str, int | None], ...]:
    """Enumerate counterparties a wallet paid, or that paid the wallet.

    Returns:
        ``(counterparty, amount_sol, signature, slot)`` tuples, newest-first.
    """
    owner = _require_address(wallet)
    signatures = _signatures(owner, endpoints=endpoints, transport=transport)
    if not signatures:
        return ()
    rows: list[tuple[str, float, str, int | None]] = []
    seen: set[str] = set()
    for entry in signatures[:max_transactions]:
        signature = entry.get("signature")
        if not isinstance(signature, str) or signature in seen:
            continue
        seen.add(signature)
        slot = entry.get("slot")
        slot_value = int(slot) if isinstance(slot, int) else None
        if transport is None:
            time.sleep(PRODUCTION_PACING_SECONDS)
        transaction = _transaction(signature, endpoints=endpoints, transport=transport)
        for counterparty, amount_sol in _counterparty_transfers(
            transaction, wallet=owner, min_sol=min_sol, receiving=receiving
        ):
            rows.append((counterparty, amount_sol, signature, slot_value))
    return tuple(rows)


def enumerate_funded(
    wallet: str,
    *,
    max_transactions: int = DEFAULT_MAX_HUB_TRANSACTIONS,
    min_sol: float = MIN_TRANSFER_SOL,
    endpoints: RpcEndpoints | Sequence[str] | None = None,
    transport: Callable[[str, str, list[object]], object] | None = None,
) -> tuple[FundedTransfer, ...]:
    """List distinct outbound SOL recipients observed leaving a hub wallet.

    This is the hub's sibling set: for a Type-2 operator each recipient is a
    relay or burner whose own mints extend the same launch history.

    Args:
        wallet: Hub wallet whose outbound transfers to enumerate.
        max_transactions: Maximum recent transactions inspected.
        min_sol: Ignore transfers below this SOL amount.
        endpoints: Resolved endpoints; defaults to precedence resolution.
        transport: Optional test seam replacing the pooled transport.

    Returns:
        Funded transfers, newest-first, de-duplicated by signature.

    Raises:
        FundingChainError: When ``wallet`` is empty.
    """
    return tuple(
        FundedTransfer(
            recipient=counterparty,
            amount_sol=amount_sol,
            signature=signature,
            slot=slot,
        )
        for counterparty, amount_sol, signature, slot in _enumerate_direction(
            wallet,
            receiving=False,
            max_transactions=max_transactions,
            min_sol=min_sol,
            endpoints=endpoints,
            transport=transport,
        )
    )


def enumerate_sources(
    wallet: str,
    *,
    max_transactions: int = DEFAULT_MAX_HUB_TRANSACTIONS,
    min_sol: float = MIN_TRANSFER_SOL,
    endpoints: RpcEndpoints | Sequence[str] | None = None,
    transport: Callable[[str, str, list[object]], object] | None = None,
) -> tuple[FundingSource, ...]:
    """List distinct inbound SOL senders observed funding a wallet.

    This surfaces the upstream funder (and, for a >5 SOL sweep, the treasury)
    that the outward-only view hides.

    Args:
        wallet: Wallet whose inbound transfers to enumerate.
        max_transactions: Maximum recent transactions inspected.
        min_sol: Ignore transfers below this SOL amount.
        endpoints: Resolved endpoints; defaults to precedence resolution.
        transport: Optional test seam replacing the pooled transport.

    Returns:
        Inbound funding sources, newest-first, de-duplicated by signature.

    Raises:
        FundingChainError: When ``wallet`` is empty.
    """
    return tuple(
        FundingSource(
            sender=counterparty, amount_sol=amount_sol, signature=signature, slot=slot
        )
        for counterparty, amount_sol, signature, slot in _enumerate_direction(
            wallet,
            receiving=True,
            max_transactions=max_transactions,
            min_sol=min_sol,
            endpoints=endpoints,
            transport=transport,
        )
    )


def _hydrate_transfers(  # noqa: PLR0913
    owner: str,
    entries: list[object],
    *,
    remaining: int,
    min_sol: float,
    max_sol: float,
    min_slot: int | None,
    max_slot: int | None,
    endpoints: RpcEndpoints | Sequence[str] | None,
    transport: Callable[[str, str, list[object]], object] | None,
) -> tuple[list[FundedTransfer], int]:
    """Hydrate one signature page into outbound transfers.

    Entries outside the slot band are skipped before any transaction is
    fetched, so scanning deep history stays cheap: signature pages are one
    call each, and only in-band transactions are hydrated.

    Returns:
        ``(transfers, hydrated)`` for the requested page slice.
    """
    transfers: list[FundedTransfer] = []
    hydrated = 0
    for entry in entries:
        if hydrated >= remaining:
            break
        if not isinstance(entry, dict):
            continue
        signature = entry.get("signature")
        if not isinstance(signature, str):
            continue
        slot = entry.get("slot")
        slot_value = int(slot) if isinstance(slot, int) else None
        if min_slot is not None and (slot_value is None or slot_value < min_slot):
            continue
        if max_slot is not None and (slot_value is None or slot_value > max_slot):
            continue
        if transport is None:
            time.sleep(PRODUCTION_PACING_SECONDS)
        hydrated += 1
        for counterparty, amount_sol in _counterparty_transfers(
            _transaction(signature, endpoints=endpoints, transport=transport),
            wallet=owner,
            min_sol=min_sol,
            receiving=False,
        ):
            if amount_sol > max_sol:
                continue
            transfers.append(
                FundedTransfer(
                    recipient=counterparty,
                    amount_sol=amount_sol,
                    signature=signature,
                    slot=slot_value,
                )
            )
    return transfers, hydrated


def enumerate_funded_paged(  # noqa: PLR0913
    wallet: str,
    *,
    max_pages: int = DEFAULT_HISTORY_PAGES,
    per_page: int = SIGNATURE_PAGE_LIMIT,
    max_transactions: int = DEFAULT_HISTORY_TRANSACTIONS,
    min_sol: float = MIN_TRANSFER_SOL,
    max_sol: float = float("inf"),
    min_slot: int | None = None,
    max_slot: int | None = None,
    endpoints: RpcEndpoints | Sequence[str] | None = None,
    transport: Callable[[str, str, list[object]], object] | None = None,
) -> tuple[FundedTransfer, ...]:
    """Enumerate outbound transfers by paging backward through history.

    The single-page enumerators only see a wallet's newest signatures, which
    on a high-frequency funder hides the transfers that actually preceded a
    launch. This walks ``before``-cursor pages so older dispersals are
    reachable, bounded by pages and total hydrated transactions.

    Args:
        wallet: Wallet whose outbound transfers to enumerate.
        max_pages: Maximum signature pages requested.
        per_page: Signatures requested per page (RPC caps this at 1000).
        max_transactions: Maximum transactions hydrated across all pages.
        min_sol: Ignore transfers below this SOL amount.
        max_sol: Ignore transfers above this SOL amount.
        min_slot: Skip signatures older than this slot before hydrating.
        max_slot: Skip signatures newer than this slot before hydrating.
        endpoints: Resolved endpoints; defaults to precedence resolution.
        transport: Optional test seam replacing the pooled transport.

    Returns:
        Funded transfers, newest-first, de-duplicated by signature.

    Raises:
        FundingChainError: When ``wallet`` is empty.
    """
    owner = _require_address(wallet)
    transfers: list[FundedTransfer] = []
    hydrated = 0
    before: str | None = None
    for _ in range(max_pages):
        if hydrated >= max_transactions:
            break
        params: dict[str, object] = {
            "limit": per_page,
            "commitment": "finalized",
        }
        if before is not None:
            params["before"] = before
        try:
            page = _rpc_call(
                "getSignaturesForAddress",
                [owner, params],
                endpoints=endpoints,
                transport=transport,
            )
        except RpcAccessError:
            logger.warning("paged funding enumeration stopped: rpc unavailable")
            break
        if not isinstance(page, list) or not page:
            break
        page_transfers, used = _hydrate_transfers(
            owner,
            page,
            remaining=max_transactions - hydrated,
            min_sol=min_sol,
            max_sol=max_sol,
            min_slot=min_slot,
            max_slot=max_slot,
            endpoints=endpoints,
            transport=transport,
        )
        transfers.extend(page_transfers)
        hydrated += used
        last = page[-1].get("signature") if isinstance(page[-1], dict) else None
        if not isinstance(last, str):
            break
        before = last
    return tuple(transfers)


__all__ = [
    "DEFAULT_MAX_HOPS",
    "DEFAULT_MAX_HUB_TRANSACTIONS",
    "HUB_MIN_SIGNATURES",
    "MIN_TRANSFER_SOL",
    "ROLE_HUB",
    "ROLE_ORIGIN",
    "ROLE_RELAY",
    "FundedTransfer",
    "FundingChainError",
    "FundingChainNode",
    "FundingChainWalk",
    "FundingSource",
    "enumerate_funded",
    "enumerate_funded_paged",
    "enumerate_sources",
    "walk_upstream",
]
