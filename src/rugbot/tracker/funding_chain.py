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


def _outbound_transfers(
    result: object,
    *,
    hub: str,
    signature: str,
    slot: int | None,
    min_sol: float,
) -> list[FundedTransfer]:
    """Extract SOL recipients that gained funds in one hub transaction."""
    parsed = _parsed_balances(result)
    if parsed is None:
        return []
    keys, pre, post = parsed
    transfers: list[FundedTransfer] = []
    for index in range(min(len(keys), len(pre), len(post))):
        if keys[index] == hub:
            continue
        gained = post[index] - pre[index]
        if gained <= 0:
            continue
        amount_sol = gained / LAMPORTS_PER_SOL
        if amount_sol < min_sol:
            continue
        transfers.append(
            FundedTransfer(
                recipient=keys[index],
                amount_sol=amount_sol,
                signature=signature,
                slot=slot,
            )
        )
    return transfers


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
    hub = _require_address(wallet)
    signatures = _signatures(hub, endpoints=endpoints, transport=transport)
    if not signatures:
        return ()
    transfers: list[FundedTransfer] = []
    seen: set[str] = set()
    for entry in signatures[:max_transactions]:
        signature = entry.get("signature")
        if not isinstance(signature, str) or signature in seen:
            continue
        seen.add(signature)
        slot = entry.get("slot")
        if transport is None:
            time.sleep(PRODUCTION_PACING_SECONDS)
        transfers.extend(
            _outbound_transfers(
                _transaction(signature, endpoints=endpoints, transport=transport),
                hub=hub,
                signature=signature,
                slot=int(slot) if isinstance(slot, int) else None,
                min_sol=min_sol,
            )
        )
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
    "enumerate_funded",
    "walk_upstream",
]
