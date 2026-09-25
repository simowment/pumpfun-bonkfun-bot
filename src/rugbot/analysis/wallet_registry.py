"""Durable SQLite registry of wallets watched by the observe-only copier.

Each row stores one wallet plus its paper-copy settings. The registry only
persists watch intent; it never authorizes order placement.
"""

# ruff: noqa: TRY003, ANN401, FBT001, FBT002 - small validated settings surface
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import base58

from rugbot.utils.logger import get_logger

logger = get_logger(__name__)

SOLANA_ADDRESS_BYTES = 32
_SCHEMA = """
CREATE TABLE IF NOT EXISTS copytrade_wallets (
  wallet TEXT PRIMARY KEY,
  enabled INTEGER NOT NULL DEFAULT 1,
  quote_sol REAL,
  tp_pct REAL,
  sl_pct REAL,
  mirror_sells INTEGER DEFAULT 1,
  max_open INTEGER,
  note TEXT,
  added_at TEXT
)
"""


class WalletRegistryError(ValueError):
    """Raised when a registry wallet or setting is invalid."""


def canonical_wallet(wallet: str) -> str:
    """Validate and return the canonical wallet string.

    Args:
        wallet: Candidate base58 Solana address.

    Returns:
        The stripped wallet string.

    Raises:
        WalletRegistryError: When the value is not 32-byte base58.
    """
    candidate = wallet.strip()
    try:
        decoded = base58.b58decode(candidate)
    except (ValueError, TypeError) as error:
        raise WalletRegistryError(f"wallet is not base58: {wallet}") from error
    if len(decoded) != SOLANA_ADDRESS_BYTES:
        raise WalletRegistryError(f"wallet is not 32 bytes: {wallet}")
    return candidate


@dataclass(frozen=True, slots=True)
class RegistryWallet:
    """One registered wallet with its paper-copy settings."""

    wallet: str
    enabled: bool
    quote_sol: float | None
    tp_pct: float | None
    sl_pct: float | None
    mirror_sells: bool
    max_open: int | None
    note: str | None
    added_at: str | None


class WalletRegistry:
    """SQLite-backed wallet registry with per-event durability."""

    def __init__(self, path: str | Path) -> None:
        """Open (creating parents) the registry database at ``path``."""
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(str(self._path))
        self._connection.row_factory = sqlite3.Row
        self._connection.execute(_SCHEMA)
        self._connection.commit()

    def close(self) -> None:
        """Close the underlying database connection."""
        self._connection.close()

    def add(self, wallet: str, **settings: Any) -> RegistryWallet:
        """Insert or replace one wallet row, flushing immediately.

        Args:
            wallet: Base58 Solana address.
            settings: Optional ``quote_sol``, ``tp_pct``, ``sl_pct``,
                ``mirror_sells``, ``max_open``, ``note``, ``enabled``.

        Returns:
            The stored row.

        Raises:
            WalletRegistryError: On invalid address or setting values.
        """
        address = canonical_wallet(wallet)
        row = _validated_settings(settings)
        added_at = datetime.now(UTC).isoformat()
        self._connection.execute(
            """
            INSERT INTO copytrade_wallets
              (wallet, enabled, quote_sol, tp_pct, sl_pct,
               mirror_sells, max_open, note, added_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(wallet) DO UPDATE SET
              enabled=excluded.enabled,
              quote_sol=excluded.quote_sol,
              tp_pct=excluded.tp_pct,
              sl_pct=excluded.sl_pct,
              mirror_sells=excluded.mirror_sells,
              max_open=excluded.max_open,
              note=excluded.note
            """,
            (
                address,
                1 if row["enabled"] else 0,
                row["quote_sol"],
                row["tp_pct"],
                row["sl_pct"],
                1 if row["mirror_sells"] else 0,
                row["max_open"],
                row["note"],
                added_at,
            ),
        )
        self._connection.commit()
        stored = self.get(address)
        if stored is None:  # pragma: no cover - insert just succeeded
            raise WalletRegistryError(f"wallet was not stored: {address}")
        return stored

    def remove(self, wallet: str) -> bool:
        """Delete one wallet row, flushing immediately."""
        address = canonical_wallet(wallet)
        cursor = self._connection.execute(
            "DELETE FROM copytrade_wallets WHERE wallet = ?", (address,)
        )
        self._connection.commit()
        return cursor.rowcount > 0

    def set_enabled(self, wallet: str, enabled: bool) -> bool:
        """Enable or disable one wallet row, flushing immediately."""
        address = canonical_wallet(wallet)
        cursor = self._connection.execute(
            "UPDATE copytrade_wallets SET enabled = ? WHERE wallet = ?",
            (1 if enabled else 0, address),
        )
        self._connection.commit()
        return cursor.rowcount > 0

    def get(self, wallet: str) -> RegistryWallet | None:
        """Return one row by wallet, or ``None`` when absent."""
        address = canonical_wallet(wallet)
        cursor = self._connection.execute(
            "SELECT * FROM copytrade_wallets WHERE wallet = ?", (address,)
        )
        row = cursor.fetchone()
        return _row_to_wallet(row) if row is not None else None

    def list(self, enabled_only: bool = False) -> tuple[RegistryWallet, ...]:
        """Return all rows, optionally only enabled ones, ordered by wallet."""
        if enabled_only:
            cursor = self._connection.execute(
                "SELECT * FROM copytrade_wallets WHERE enabled = 1 ORDER BY wallet"
            )
        else:
            cursor = self._connection.execute(
                "SELECT * FROM copytrade_wallets ORDER BY wallet"
            )
        return tuple(_row_to_wallet(row) for row in cursor.fetchall())

    def count(self) -> int:
        """Return the total number of registered wallets."""
        cursor = self._connection.execute("SELECT COUNT(*) FROM copytrade_wallets")
        row = cursor.fetchone()
        return int(row[0]) if row is not None else 0


def _validated_settings(settings: dict[str, Any]) -> dict[str, Any]:
    """Validate paper-copy settings, rejecting unknown or illegal values."""
    allowed = {
        "enabled",
        "quote_sol",
        "tp_pct",
        "sl_pct",
        "mirror_sells",
        "max_open",
        "note",
    }
    unknown = set(settings) - allowed
    if unknown:
        raise WalletRegistryError(f"unknown wallet settings: {sorted(unknown)}")
    enabled = settings.get("enabled", True)
    mirror_sells = settings.get("mirror_sells", True)
    quote_sol = settings.get("quote_sol")
    tp_pct = settings.get("tp_pct")
    sl_pct = settings.get("sl_pct")
    max_open = settings.get("max_open")
    note = settings.get("note")
    if not isinstance(enabled, bool):
        raise WalletRegistryError("enabled must be a bool")
    if not isinstance(mirror_sells, bool):
        raise WalletRegistryError("mirror_sells must be a bool")
    for name, value in (("quote_sol", quote_sol), ("tp_pct", tp_pct)):
        if value is not None and (
            isinstance(value, bool) or not isinstance(value, (int, float))
        ):
            raise WalletRegistryError(f"{name} must be a number")
    if sl_pct is not None and (
        isinstance(sl_pct, bool) or not isinstance(sl_pct, (int, float))
    ):
        raise WalletRegistryError("sl_pct must be a number")
    if quote_sol is not None and float(quote_sol) <= 0:
        raise WalletRegistryError("quote_sol must be positive")
    if max_open is not None and (
        isinstance(max_open, bool) or not isinstance(max_open, int) or max_open < 1
    ):
        raise WalletRegistryError("max_open must be a positive integer")
    if note is not None and not isinstance(note, str):
        raise WalletRegistryError("note must be a string")
    return {
        "enabled": enabled,
        "quote_sol": float(quote_sol) if quote_sol is not None else None,
        "tp_pct": float(tp_pct) if tp_pct is not None else None,
        "sl_pct": float(sl_pct) if sl_pct is not None else None,
        "mirror_sells": mirror_sells,
        "max_open": max_open,
        "note": note,
    }


def _row_to_wallet(row: sqlite3.Row) -> RegistryWallet:
    """Convert one database row into a ``RegistryWallet``."""
    return RegistryWallet(
        wallet=str(row["wallet"]),
        enabled=bool(row["enabled"]),
        quote_sol=float(row["quote_sol"]) if row["quote_sol"] is not None else None,
        tp_pct=float(row["tp_pct"]) if row["tp_pct"] is not None else None,
        sl_pct=float(row["sl_pct"]) if row["sl_pct"] is not None else None,
        mirror_sells=bool(row["mirror_sells"])
        if row["mirror_sells"] is not None
        else True,
        max_open=int(row["max_open"]) if row["max_open"] is not None else None,
        note=str(row["note"]) if row["note"] is not None else None,
        added_at=str(row["added_at"]) if row["added_at"] is not None else None,
    )


__all__ = [
    "RegistryWallet",
    "WalletRegistry",
    "WalletRegistryError",
    "canonical_wallet",
]
