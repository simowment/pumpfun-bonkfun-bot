"""Canonical Solana JSON-RPC egress: one precedence rule, one shared pool.

Endpoint precedence is explicit and total: a per-call override (a CLI
``--rpc`` value) beats the saved ``.env`` file, which beats an inherited
process default. No implicit public RPC is ever appended, so an unconfigured
process fails closed instead of leaking traffic to a third-party endpoint.

Pools are shared so provider health accumulates across calls: a 429 or 5xx
places that provider in cooldown for every later caller instead of being
rediscovered one request at a time. Sync pools share per endpoint set; async
pools share per running event loop because their pacing lock binds to it.
"""

# Failure messages are the transient vocabulary consumed by
# discover.collector._is_rate_limit_abstain, so they are built at raise site.
# ruff: noqa: TRY003

from __future__ import annotations

import asyncio
import json
import os
import threading
import time
import weakref
from dataclasses import dataclass
from typing import TYPE_CHECKING
from urllib.parse import urlsplit, urlunsplit

from sol_trade_sdk.solana.provider_pool import (
    RpcProviderPool,
    RpcProviderPoolError,
    SyncRpcProviderPool,
)

from rugbot.utils.logger import get_logger

if TYPE_CHECKING:
    from collections.abc import Sequence

    from sol_trade_sdk.solana.provider_pool import (
        RpcEndpointHealth,
        SyncRpcTransport,
    )

logger = get_logger(__name__)

JSONRPC_VERSION = "2.0"
JSONRPC_REQUEST_ID = 1
RPC_TIMEOUT_SECONDS = 15
RPC_HTTP_ENV_KEY = "SOLANA_RPC_HTTP"
HTTP_SUCCESS_MINIMUM = 200
HTTP_SUCCESS_MAXIMUM = 299
HTTP_TOO_MANY_REQUESTS = 429

ENDPOINT_SOURCE_OVERRIDE = "override"
ENDPOINT_SOURCE_DOTENV = "dotenv"
ENDPOINT_SOURCE_ENVIRON = "environ"
ENDPOINT_SOURCE_NONE = "none"

_SYNC_POOLS: dict[tuple[str, ...], SyncRpcProviderPool] = {}
# Async pool state (an asyncio.Lock) binds to the running event loop at first
# await, so pools are keyed weakly by loop: a finished loop's pools vanish
# instead of deadlocking the next loop that retrieves them.
_ASYNC_POOLS: weakref.WeakKeyDictionary[
    asyncio.AbstractEventLoop,
    dict[tuple[tuple[str, ...], float], RpcProviderPool],
] = weakref.WeakKeyDictionary()
_POOL_LOCK = threading.Lock()


@dataclass(frozen=True, slots=True)
class RpcEndpoints:
    """Resolved RPC endpoints in deterministic failover order."""

    ordered: tuple[str, ...]
    source: str

    @property
    def primary(self) -> str | None:
        """Return the first endpoint, or None when nothing is configured."""
        return self.ordered[0] if self.ordered else None


class RpcAccessError(RuntimeError):
    """No configured provider returned a usable JSON-RPC result."""

    def __init__(self, message: str, *, method: str, status: int | None = None) -> None:
        """Attach the failing method and HTTP status to the failure.

        Args:
            message: Human-readable failure description.
            method: JSON-RPC method that failed.
            status: HTTP status of the last response, when one was received.
        """
        super().__init__(message)
        self.method = method
        self.status = status


def resolve_rpc_endpoints(
    primary: str | None = None,
    fallbacks: Sequence[str] | None = None,
) -> RpcEndpoints:
    """Resolve RPC endpoints with explicit precedence.

    Args:
        primary: Per-call override (a CLI ``--rpc`` value). Beats the saved
            file and any inherited process default.
        fallbacks: Per-call ordered failover endpoints. Beat the saved file
            when non-empty.

    Returns:
        Endpoints in failover order plus the label of the layer that supplied
        the primary endpoint: ``override``, ``dotenv``, ``environ``, or
        ``none``. An override replaces the configured primary outright instead
        of being tried alongside it. ``ordered`` is empty when nothing is
        configured; callers fail closed rather than reaching an implicit
        public RPC.

    Raises:
        SniperConfigError: When a configured endpoint URL is invalid.
    """
    # Deferred import: runtime.config pulls decision, which pulls intelligence,
    # which imports this module — a top-level import would be circular. Same
    # precedent as integrations.rpc_cache.resolve_rpc_cache_path.
    from rugbot.runtime.config import (  # noqa: PLC0415
        load_provider_settings,
        resolve_dotenv,
    )

    inherited = os.environ.get(RPC_HTTP_ENV_KEY)
    resolve_dotenv()
    settings = load_provider_settings()
    if primary is not None and primary.strip():
        source = ENDPOINT_SOURCE_OVERRIDE
    elif settings.rpc_http is None:
        source = ENDPOINT_SOURCE_NONE
    elif settings.rpc_http != inherited:
        source = ENDPOINT_SOURCE_DOTENV
    else:
        source = ENDPOINT_SOURCE_ENVIRON
    resolved_fallbacks = tuple(fallbacks) if fallbacks else settings.rpc_http_fallbacks
    override = primary.strip() if primary is not None else ""
    candidates = (override or (settings.rpc_http or ""), *resolved_fallbacks)
    ordered = tuple(
        dict.fromkeys(
            candidate.strip() for candidate in candidates if candidate.strip()
        )
    )
    return RpcEndpoints(ordered=ordered, source=source)


# Pooled sync requests are spaced and retried when every provider is busy.
RPC_MIN_REQUEST_SPACING_SECONDS = 0.12
RPC_BUSY_ATTEMPTS = 5
RPC_BUSY_BACKOFF_SECONDS = 2.0
RPC_BUSY_MAX_WAIT_SECONDS = 30.0
_PACE_LOCK = threading.Lock()
_last_shared_request = 0.0


def shared_sync_pool(endpoints: RpcEndpoints | Sequence[str]) -> SyncRpcProviderPool:
    """Return the process-wide synchronous pool for one endpoint set.

    Args:
        endpoints: Resolved endpoints or a raw ordered endpoint sequence.

    Returns:
        The pool shared by every caller using that endpoint set, so cooldowns
        and consecutive-failure counts persist between calls.

    Raises:
        RpcAccessError: When the endpoint set is empty.
    """
    ordered = _ordered_endpoints(endpoints)
    _require_endpoints(ordered)
    with _POOL_LOCK:
        pool = _SYNC_POOLS.get(ordered)
        if pool is None:
            pool = SyncRpcProviderPool(ordered, timeout_seconds=RPC_TIMEOUT_SECONDS)
            _SYNC_POOLS[ordered] = pool
        return pool


def shared_async_pool(
    endpoints: RpcEndpoints | Sequence[str],
    *,
    minimum_interval_seconds: float = 0.0,
) -> RpcProviderPool:
    """Return the event-loop-scoped async pool for one endpoint set and pace.

    The SDK pool serializes attempts with an ``asyncio.Lock`` that binds to
    the running event loop at first await, so pools are shared per loop: all
    callers on one loop share health, and a finished loop's pools are dropped
    instead of deadlocking the next loop. Constructed outside any loop, a
    fresh pool is returned each call because its eventual loop is unknowable.

    Args:
        endpoints: Resolved endpoints or a raw ordered endpoint sequence.
        minimum_interval_seconds: Minimum spacing between provider attempts.
            Part of the pool identity because it changes request pacing.

    Returns:
        The pool shared by every caller on this event loop using that
        endpoint set and pace.

    Raises:
        RpcAccessError: When the endpoint set is empty.
    """
    ordered = _ordered_endpoints(endpoints)
    _require_endpoints(ordered)
    interval = float(minimum_interval_seconds)
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return RpcProviderPool(
            ordered,
            timeout_seconds=RPC_TIMEOUT_SECONDS,
            minimum_interval_seconds=interval,
        )
    key = (ordered, interval)
    with _POOL_LOCK:
        pools = _ASYNC_POOLS.get(loop)
        if pools is None:
            pools = {}
            _ASYNC_POOLS[loop] = pools
        pool = pools.get(key)
        if pool is None:
            pool = RpcProviderPool(
                ordered,
                timeout_seconds=RPC_TIMEOUT_SECONDS,
                minimum_interval_seconds=interval,
            )
            pools[key] = pool
        return pool


def sync_rpc_result(
    method: str,
    params: Sequence[object],
    *,
    endpoints: RpcEndpoints | Sequence[str] | None = None,
    transport: SyncRpcTransport | None = None,
) -> object:
    """Return one JSON-RPC ``result`` through the shared health-aware pool.

    Args:
        method: JSON-RPC method name.
        params: JSON-RPC params array.
        endpoints: Resolved endpoints; defaults to precedence resolution.
        transport: Test seam replacing the pooled transport. A seam is served
            by a one-off pool so injected failures cannot mutate shared health.

    Returns:
        The decoded ``result`` payload.

    Raises:
        RpcAccessError: When no endpoint is configured, every provider failed
            or is cooling down, or the response is not a successful JSON-RPC
            result envelope.
    """
    resolved = resolve_rpc_endpoints() if endpoints is None else endpoints
    ordered = _ordered_endpoints(resolved)
    _require_endpoints(ordered, method=method)
    body = json.dumps(
        {
            "jsonrpc": JSONRPC_VERSION,
            "id": JSONRPC_REQUEST_ID,
            "method": method,
            "params": list(params),
        }
    ).encode()
    caller = (
        SyncRpcProviderPool(
            ordered, timeout_seconds=RPC_TIMEOUT_SECONDS, transport=transport
        )
        if transport is not None
        else shared_sync_pool(ordered)
    )
    attempts = 1 if transport is not None else RPC_BUSY_ATTEMPTS
    for attempt in range(attempts):
        if transport is None:
            _pace_shared_requests()
        try:
            response = caller(ordered[0], body)
            break
        except RpcProviderPoolError as error:
            if attempt == attempts - 1:
                raise RpcAccessError(
                    f"{method} transport failed: {type(error).__name__}",
                    method=method,
                ) from error
            # Every provider throttled or cooling down: wait instead of dropping
            # the request (callers would otherwise lose data silently).
            time.sleep(
                min(RPC_BUSY_BACKOFF_SECONDS * 2**attempt, RPC_BUSY_MAX_WAIT_SECONDS)
            )
    return _decoded_result(method, response.status, response.body)


def _pace_shared_requests() -> None:
    """Space pooled sync requests process-wide (free RPC tiers cap ~10 req/s)."""
    global _last_shared_request  # noqa: PLW0603
    with _PACE_LOCK:
        wait = _last_shared_request + RPC_MIN_REQUEST_SPACING_SECONDS - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        _last_shared_request = time.monotonic()


def rpc_health(
    endpoints: RpcEndpoints | Sequence[str],
) -> tuple[RpcEndpointHealth, ...]:
    """Return read-only provider health for one endpoint set.

    Args:
        endpoints: Resolved endpoints or a raw ordered endpoint sequence.

    Returns:
        One snapshot per configured endpoint, in failover order.
    """
    ordered = _ordered_endpoints(endpoints)
    _require_endpoints(ordered)
    return shared_sync_pool(ordered).health


def clear_shared_pools() -> None:
    """Drop every cached pool so health state does not cross a run boundary."""
    with _POOL_LOCK:
        _SYNC_POOLS.clear()
        _ASYNC_POOLS.clear()


def _ordered_endpoints(endpoints: RpcEndpoints | Sequence[str]) -> tuple[str, ...]:
    """Return the failover order of a resolved set or a raw endpoint tuple."""
    if isinstance(endpoints, RpcEndpoints):
        return endpoints.ordered
    return tuple(dict.fromkeys(endpoint for endpoint in endpoints if endpoint))


def _require_endpoints(ordered: tuple[str, ...], *, method: str | None = None) -> None:
    """Fail closed when no RPC endpoint is configured.

    Args:
        ordered: Endpoints in failover order.
        method: JSON-RPC method name, when the caller is mid-request.

    Raises:
        RpcAccessError: Always, when ``ordered`` is empty.
    """
    if ordered:
        return
    message = f"{RPC_HTTP_ENV_KEY} is required; no implicit public RPC fallback"
    if method is None:
        raise RpcAccessError(message, method="rpc")
    raise RpcAccessError(message, method=method)


def _decoded_result(method: str, status: int, body: bytes) -> object:
    """Decode one HTTP response into a JSON-RPC ``result`` payload.

    Args:
        method: JSON-RPC method name, used for failure vocabulary.
        status: HTTP status of the response.
        body: Raw response body.

    Returns:
        The decoded ``result`` payload.

    Raises:
        RpcAccessError: On rate limiting, a non-2xx status, a malformed body,
            or a JSON-RPC error object.
    """
    if status == HTTP_TOO_MANY_REQUESTS:
        raise RpcAccessError(
            f"{method} was rate-limited by the available RPC providers",
            method=method,
            status=status,
        )
    if not HTTP_SUCCESS_MINIMUM <= status <= HTTP_SUCCESS_MAXIMUM:
        raise RpcAccessError(
            f"{method} transport failed: HTTP {status}", method=method, status=status
        )
    try:
        payload = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise RpcAccessError(
            f"{method} transport failed: malformed JSON-RPC body",
            method=method,
            status=status,
        ) from error
    if type(payload) is not dict:
        raise RpcAccessError(
            f"{method} transport failed: JSON-RPC body is not one object",
            method=method,
            status=status,
        )
    if "error" in payload:
        raise RpcAccessError(
            f"{method} transport failed: {payload['error']}",
            method=method,
            status=status,
        )
    if "result" not in payload:
        raise RpcAccessError(
            f"{method} transport failed: response omitted result",
            method=method,
            status=status,
        )
    return payload["result"]


def resolve_websocket_endpoint(http_endpoint: str | None) -> str | None:
    """Return the configured Solana WSS URL, or derive it from the HTTP endpoint.

    ``SOLANA_RPC_WEBSOCKET`` wins when set; otherwise the HTTP URL's scheme is
    swapped (https -> wss, http -> ws) keeping host, path and query (API key).
    """
    from rugbot.runtime.config import load_provider_settings  # noqa: PLC0415

    configured = load_provider_settings().rpc_websocket
    if configured:
        return configured
    if not http_endpoint:
        return None
    parsed = urlsplit(http_endpoint)
    scheme = "wss" if parsed.scheme == "https" else "ws"
    return urlunsplit(
        (scheme, parsed.netloc, parsed.path, parsed.query, parsed.fragment)
    )
