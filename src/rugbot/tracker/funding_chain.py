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

from rugbot.ingest.pump.create_decoder import PUMP_PROGRAM_ID
from rugbot.integrations.rpc_access import (
    RpcAccessError,
    RpcEndpoints,
    resolve_rpc_endpoints,
    sync_rpc_result,
)
from rugbot.integrations.rpc_cache import RpcResponseCache
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
# Downstream relay resolution: a relay is a short-lived pass-through wallet that
# forwards most of what it received to one account. Operators chain several
# (including seed-derived accounts) between the hub and the creator burner.
RELAY_MAX_SIGNATURES = 8
RELAY_MAX_HOPS = 5
RELAY_FORWARD_FRACTION = 0.8
PUMP_CREATE_LOG = "Program log: Instruction: Create"
ROLE_ORIGIN = "origin"
ROLE_RELAY = "relay"
ROLE_HUB = "hub"
CEX_MIN_RECIPIENTS = 50

_ERR_ADDRESS = "wallet address must be a non-empty string"
_ERR_MAX_HOPS = "max_hops must be at least 1"
_ERR_NO_ENDPOINT = "no RPC endpoint is configured"
_WARN_CYCLE = "cycle detected in funding chain"
_WARN_HOP_CAP = "hop cap reached before a hub"

_funding_rpc_cache: RpcResponseCache | None = None


def _cache_ttl(method: str, params: Sequence[object]) -> float | None:
    """Return an infinite TTL for an immutable call, else None (do not cache)."""
    if method == "getTransaction":
        return float("inf")
    if method == "getSignaturesForAddress":
        if len(params) >= 2 and isinstance(params[1], dict) and "before" in params[1]:  # noqa: PLR2004
            return float("inf")
        return None
    return None


def _get_funding_rpc_cache() -> RpcResponseCache | None:
    """Return the shared funding-chain RPC cache, building it lazily once."""
    global _funding_rpc_cache  # noqa: PLW0603
    if _funding_rpc_cache is not None:
        return _funding_rpc_cache
    try:
        _funding_rpc_cache = RpcResponseCache()
    except Exception:  # noqa: BLE001
        logger.debug("funding chain rpc cache unavailable")
        return None
    return _funding_rpc_cache


def _fetch_cached(
    method: str,
    params: Sequence[object],
    *,
    endpoints: RpcEndpoints | Sequence[str] | None,
    cache: RpcResponseCache | None,
) -> object:
    """Fetch one RPC result, serving immutable calls from the durable cache."""
    ttl = _cache_ttl(method, params)
    if cache is not None and ttl is not None:
        try:
            hit = cache.lookup(method, params)
        except Exception:  # noqa: BLE001
            logger.debug("funding chain rpc cache lookup failed")
            hit = None
        if isinstance(hit, dict) and "result" in hit:
            return hit["result"]
    result = sync_rpc_result(method, params, endpoints=endpoints)
    if cache is not None and ttl is not None:
        try:
            cache.store(method, params, {"result": result}, ttl_override=ttl)
        except Exception:  # noqa: BLE001
            logger.debug("funding chain rpc cache store failed")
    return result


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
    block_time: int | None = None


@dataclass(frozen=True, slots=True)
class RelayResolution:
    """Where a funded wallet's SOL ended up after single-use relay hops."""

    terminal: str
    relays: tuple[str, ...]


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
        return _fetch_cached(
            method, params, endpoints=endpoints, cache=_get_funding_rpc_cache()
        )
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
                {"maxSupportedTransactionVersion": 1, "encoding": "jsonParsed"},
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


def is_cex_shaped_source(
    *,
    source_creation_count: int,
    source_recipient_count: int,
    min_recipients: int = CEX_MIN_RECIPIENTS,
) -> bool:
    """Return True when a funding source looks like an exchange hot wallet.

    A wallet that creates nothing but pays many wallets is an exchange or
    shared hot wallet; its recipients are unrelated users and MUST NOT be
    attributed to one entity (AGENTS.md section 9.1; bible CEX warning).

    Args:
        source_creation_count: Tokens created by the source wallet itself.
        source_recipient_count: Distinct wallets the source funded.
        min_recipients: Recipient threshold for the CEX shape.

    Returns:
        True when the source created nothing and funded at least
        ``min_recipients`` distinct wallets.
    """
    return source_creation_count == 0 and source_recipient_count >= min_recipients


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
            block_time = entry.get("blockTime")
            transfers.append(
                FundedTransfer(
                    recipient=counterparty,
                    amount_sol=amount_sol,
                    signature=signature,
                    slot=slot_value,
                    block_time=block_time if isinstance(block_time, int) else None,
                )
            )
    return transfers, hydrated


def enumerate_funded_paged(  # noqa: PLR0913
    wallet: str,
    *,
    max_pages: int | None = DEFAULT_HISTORY_PAGES,
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
        max_pages: Maximum signature pages requested. None walks the cursor
            to exhaustion, bounded by ``max_transactions``.
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
    pages_walked = 0
    while max_pages is None or pages_walked < max_pages:
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
        pages_walked += 1
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


def _created_pump_token(result: object, wallet: str) -> bool:
    """Return True when ``wallet`` fee-paid a Pump create in this transaction."""
    if not isinstance(result, dict):
        return False
    transaction = result.get("transaction")
    message = transaction.get("message") if isinstance(transaction, dict) else None
    keys = _account_keys(message)
    meta = result.get("meta")
    logs = meta.get("logMessages") if isinstance(meta, dict) else None
    return (
        bool(keys)
        and keys[0] == wallet
        and PUMP_PROGRAM_ID in keys
        and isinstance(logs, list)
        and any(
            isinstance(line, str) and line.startswith(PUMP_CREATE_LOG) for line in logs
        )
    )


def resolve_relay_terminal(
    wallet: str,
    *,
    received_sol: float,
    max_hops: int = RELAY_MAX_HOPS,
    endpoints: RpcEndpoints | Sequence[str] | None = None,
    transport: Callable[[str, str, list[object]], object] | None = None,
) -> RelayResolution:
    """Follow a funded wallet forward through single-use relay hops.

    A wallet is treated as a relay while it has at most
    ``RELAY_MAX_SIGNATURES`` signatures, never fee-paid a Pump create, and
    forwarded at least ``RELAY_FORWARD_FRACTION`` of the SOL it received to one
    account. Value is followed through balance deltas, so hops through
    seed-derived accounts or decoy program calls are not lost.

    Args:
        wallet: Wallet that received SOL from the funder.
        received_sol: SOL it received; the forwarding threshold scales from it.
        max_hops: Maximum relay hops followed.
        endpoints: Resolved endpoints; defaults to precedence resolution.
        transport: Optional test seam replacing the pooled transport.

    Returns:
        The terminal wallet (candidate creator) and the relays crossed.
    """
    current = _require_address(wallet)
    relays: list[str] = []
    amount_sol = received_sol
    for _ in range(max_hops):
        signatures = _signatures(current, endpoints=endpoints, transport=transport)
        if signatures is None or len(signatures) > RELAY_MAX_SIGNATURES:
            break
        forward: tuple[str, float] | None = None
        created = False
        for entry in signatures:
            signature = entry.get("signature")
            if not isinstance(signature, str) or entry.get("err") is not None:
                continue
            if transport is None:
                time.sleep(PRODUCTION_PACING_SECONDS)
            result = _transaction(signature, endpoints=endpoints, transport=transport)
            if _created_pump_token(result, current):
                created = True
                break
            for counterparty, sent_sol in _counterparty_transfers(
                result,
                wallet=current,
                min_sol=amount_sol * RELAY_FORWARD_FRACTION,
                receiving=False,
            ):
                if forward is None or sent_sol > forward[1]:
                    forward = (counterparty, sent_sol)
        if created or forward is None or forward[0] in relays:
            break
        relays.append(current)
        current, amount_sol = forward
    return RelayResolution(terminal=current, relays=tuple(relays))


__all__ = [
    "CEX_MIN_RECIPIENTS",
    "DEFAULT_MAX_HOPS",
    "DEFAULT_MAX_HUB_TRANSACTIONS",
    "HUB_MIN_SIGNATURES",
    "MIN_TRANSFER_SOL",
    "RELAY_MAX_HOPS",
    "ROLE_HUB",
    "ROLE_ORIGIN",
    "ROLE_RELAY",
    "FundedTransfer",
    "FundingChainError",
    "FundingChainNode",
    "FundingChainWalk",
    "FundingSource",
    "RelayResolution",
    "enumerate_funded",
    "enumerate_funded_paged",
    "enumerate_sources",
    "is_cex_shaped_source",
    "resolve_relay_terminal",
    "walk_upstream",
]
