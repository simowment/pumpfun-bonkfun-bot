"""Append-only SQLite journal of paper fills, one row per simulated buy or sell."""

from __future__ import annotations

import sqlite3
from dataclasses import astuple, dataclass, fields
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path


@dataclass(frozen=True, slots=True)
class PaperFill:
    """One simulated fill.

    ``quote_lamports`` is what a buy spent (curve fee included) or what a sell
    received (curve fee deducted). ``pnl_lamports`` is set on sells only: the
    slice's proceeds minus its share of the entry cost and its own tx cost.
    """

    at_ms: int
    tracker: str
    mint: str
    side: str
    slot: int
    quote_lamports: int
    tokens: int
    curve_fee_lamports: int
    tx_cost_lamports: int
    market_cap_lamports: int
    reason: str
    pnl_lamports: int | None
    position_closed: bool


@dataclass(frozen=True, slots=True)
class MarketSnapshot:
    """Last known curve state of a held coin, so a restart can keep pricing it."""

    mint: str
    slot: int
    virtual_sol: int
    virtual_token: int
    protocol_fee_bps: int
    creator_fee_bps: int
    last_trade_ms: int


_COLUMNS = tuple(field.name for field in fields(PaperFill))
_MARKET_COLUMNS = tuple(field.name for field in fields(MarketSnapshot))


class PaperJournal:
    """SQLite-backed paper fill journal."""

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(path)
        self._connection.execute(
            "CREATE TABLE IF NOT EXISTS paper_fills ("
            "at_ms INTEGER NOT NULL, tracker TEXT NOT NULL, mint TEXT NOT NULL, "
            "side TEXT NOT NULL, slot INTEGER NOT NULL, "
            "quote_lamports INTEGER NOT NULL, tokens INTEGER NOT NULL, "
            "curve_fee_lamports INTEGER NOT NULL, tx_cost_lamports INTEGER NOT NULL, "
            "market_cap_lamports INTEGER NOT NULL, reason TEXT NOT NULL, "
            "pnl_lamports INTEGER, position_closed INTEGER NOT NULL)"
        )
        self._connection.execute(
            "CREATE TABLE IF NOT EXISTS paper_markets (mint TEXT PRIMARY KEY, "
            "slot INTEGER NOT NULL, virtual_sol INTEGER NOT NULL, "
            "virtual_token INTEGER NOT NULL, protocol_fee_bps INTEGER NOT NULL, "
            "creator_fee_bps INTEGER NOT NULL, last_trade_ms INTEGER NOT NULL)"
        )
        self._connection.commit()

    def record(self, fill: PaperFill) -> None:
        """Append one fill."""

        self._connection.execute(
            f"INSERT INTO paper_fills({','.join(_COLUMNS)}) "
            f"VALUES({','.join('?' * len(_COLUMNS))})",
            astuple(fill),
        )
        self._connection.commit()

    def fills(self, tracker: str | None = None) -> tuple[PaperFill, ...]:
        """Return fills in insertion order, optionally for one tracker."""

        rows = self._connection.execute(
            f"SELECT {','.join(_COLUMNS)} FROM paper_fills "  # noqa: S608
            "WHERE ? IS NULL OR tracker = ? ORDER BY rowid",
            (tracker, tracker),
        ).fetchall()
        return tuple(
            PaperFill(*row[:-1], position_closed=bool(row[-1])) for row in rows
        )

    def save_market(self, snapshot: MarketSnapshot) -> None:
        """Upsert one held coin's latest curve state."""

        self._connection.execute(
            f"INSERT OR REPLACE INTO paper_markets({','.join(_MARKET_COLUMNS)}) "
            f"VALUES({','.join('?' * len(_MARKET_COLUMNS))})",
            astuple(snapshot),
        )
        self._connection.commit()

    def markets(self) -> dict[str, MarketSnapshot]:
        """Return every saved curve state by mint."""

        rows = self._connection.execute(
            f"SELECT {','.join(_MARKET_COLUMNS)} FROM paper_markets"  # noqa: S608
        ).fetchall()
        return {row[0]: MarketSnapshot(*row) for row in rows}

    def close(self) -> None:
        """Close the connection."""

        self._connection.close()
