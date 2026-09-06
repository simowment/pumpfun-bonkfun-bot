"""SQLite-backed cache for Solana JSON-RPC responses."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from rugbot.utils.logger import get_logger

if TYPE_CHECKING:
    from collections.abc import Callable

logger = get_logger(__name__)

VOLATILE_TTL_SECONDS = 60.0

IMMUTABLE_METHODS = frozenset({"getTransaction", "getSignaturesForAddress"})

_RPC_CACHE_FILENAME = "rpc_cache.sqlite3"


def resolve_rpc_cache_path(db_path: Path | str | None = None) -> Path:
    """Resolve the SQLite file for the RPC cache.

    Args:
        db_path: Explicit override path, used by tests.

    Returns:
        Filesystem path of the cache database.
    """
    if db_path is not None:
        candidate = Path(db_path)
        candidate.parent.mkdir(parents=True, exist_ok=True)
        return candidate
    try:
        # Deferred import: runtime.config pulls decision/execution, which
        # imports solana_rpc — a top-level import would be circular.
        from rugbot.runtime.config import (  # noqa: PLC0415
            resolve_state_dir,
        )
    except ImportError:
        state_dir: Path | None = None
    else:
        try:
            state_dir = resolve_state_dir()
        except OSError:
            state_dir = None
    if state_dir is None:
        fallback = Path.cwd() / ".state" / _RPC_CACHE_FILENAME
        fallback.parent.mkdir(parents=True, exist_ok=True)
        return fallback
    candidate = Path(state_dir) / _RPC_CACHE_FILENAME
    candidate.parent.mkdir(parents=True, exist_ok=True)
    return candidate


def canonical_params_hash(params: object) -> str:
    """Hash canonical JSON of RPC params for stable cache keys.

    Args:
        params: The ``params`` payload of a JSON-RPC body.

    Returns:
        Hex SHA-256 digest of the canonical encoding.
    """
    canonical = json.dumps(params, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def cache_key(method: str, params: object) -> str:
    """Build the SQLite key for a (method, params) pair.

    Args:
        method: JSON-RPC method name.
        params: The ``params`` payload of the request body.

    Returns:
        Stable ``"<method>:<params-hash>"`` key.
    """
    return f"{method}:{canonical_params_hash(params)}"


def _params_commitment(params: object) -> str | None:
    """Extract the commitment level from RPC params, if present."""
    if isinstance(params, list):
        for entry in params:
            if isinstance(entry, dict) and isinstance(entry.get("commitment"), str):
                return str(entry["commitment"])
    elif isinstance(params, dict) and isinstance(params.get("commitment"), str):
        return str(params["commitment"])
    return None


def is_immutable_request(method: str, params: object) -> bool:
    """Return True when a finalized immutable response may be cached forever.

    Args:
        method: JSON-RPC method name.
        params: The ``params`` payload of the request body.

    Returns:
        True only for finalized getTransaction/getSignaturesForAddress calls.
    """
    if method not in IMMUTABLE_METHODS:
        return False
    return _params_commitment(params) == "finalized"


class RpcResponseCache:
    """SQLite response cache keyed by (method, canonical-params-hash)."""

    def __init__(
        self,
        db_path: Path | str | None = None,
        *,
        now_fn: Callable[[], float] | None = None,
    ) -> None:
        """Initialize the cache, creating the SQLite file and table.

        Args:
            db_path: Explicit database path override (tests use temp files).
            now_fn: Clock injection for TTL tests; defaults to time.time.
        """
        self._db_path = resolve_rpc_cache_path(db_path)
        self._now_fn = now_fn or time.time
        self._conn = sqlite3.connect(str(self._db_path))
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS rpc_cache ("
            "cache_key TEXT PRIMARY KEY, "
            "method TEXT NOT NULL, "
            "response_json TEXT NOT NULL, "
            "stored_at REAL NOT NULL, "
            "ttl_seconds REAL)"
        )
        self._conn.commit()

    def close(self) -> None:
        """Close the underlying SQLite connection."""
        try:
            self._conn.close()
        except Exception:  # noqa: BLE001 - close must never raise.
            logger.debug("RPC cache close failed for %s", self._db_path)

    def lookup(self, method: str, params: object) -> dict[str, Any] | None:
        """Return the cached response for (method, params), if fresh.

        Args:
            method: JSON-RPC method name.
            params: The ``params`` payload of the request body.

        Returns:
            Cached response mapping, or None on miss/expiry.
        """
        key = cache_key(method, params)
        try:
            row = self._conn.execute(
                "SELECT response_json, stored_at, ttl_seconds FROM rpc_cache"
                " WHERE cache_key = ?",
                (key,),
            ).fetchone()
        except Exception:  # noqa: BLE001 - cache must never break reads.
            logger.debug("RPC cache lookup failed for %s", method)
            return None
        if row is None:
            return None
        response_json, stored_at, ttl_seconds = row
        if ttl_seconds is not None:
            try:
                age = self._now_fn() - float(stored_at)
            except Exception:  # noqa: BLE001 - clock failure means miss.
                return None
            if age > float(ttl_seconds):
                try:
                    self._conn.execute(
                        "DELETE FROM rpc_cache WHERE cache_key = ?", (key,)
                    )
                    self._conn.commit()
                except Exception:  # noqa: BLE001 - expiry cleanup is best effort.
                    logger.debug("RPC cache expiry cleanup failed for %s", method)
                return None
        try:
            parsed = json.loads(str(response_json))
        except (TypeError, ValueError):
            return None
        return parsed if isinstance(parsed, dict) else None

    def store(
        self,
        method: str,
        params: object,
        response: dict[str, Any],
        *,
        ttl_override: float | None = None,
    ) -> None:
        """Persist a successful response; failures must never call this.

        Args:
            method: JSON-RPC method name.
            params: The ``params`` payload of the request body.
            response: Successful JSON-RPC response mapping.
            ttl_override: Explicit TTL in seconds, bypassing the auto rule
                (e.g. immutable REST pages that never expire). ``None``
                keeps the auto rule (forever for finalized RPC, 60s else).
        """
        if ttl_override is not None:
            ttl: float | None = ttl_override
        else:
            ttl = None if is_immutable_request(method, params) else VOLATILE_TTL_SECONDS
        key = cache_key(method, params)
        try:
            self._conn.execute(
                "INSERT OR REPLACE INTO rpc_cache"
                " (cache_key, method, response_json, stored_at, ttl_seconds)"
                " VALUES (?, ?, ?, ?, ?)",
                (key, method, json.dumps(response, default=str), self._now_fn(), ttl),
            )
            self._conn.commit()
        except Exception:  # noqa: BLE001 - cache must never break writes.
            logger.debug("RPC cache store failed for %s", method)
