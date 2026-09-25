"""SQLite store for per-launch feature records (read path, no network).

The background collector persists full feature records here; analysis
commands read the store for sub-second responses.
"""

# ruff: noqa: ANN401, S608 - record values are heterogeneous by contract;
# SQL identifiers come from the validated _ALL_COLUMNS allowlist only.

from __future__ import annotations

import sqlite3
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

DEFAULT_STORE_PATH = ".state/analysis/launches.sqlite3"

# Canonical column list matching ``build_feature_record`` keys plus
# bookkeeping columns. Unknown keys are ignored, never crash.
FEATURE_COLUMNS: tuple[str, ...] = (
    "mint",
    "creator",
    "created_at_ms",
    "hour_utc",
    "deployer_lifetime_creations",
    "deployer_is_fresh",
    "funder",
    "funding_amount_sol",
    "funding_band",
    "funding_available",
    "entry_price",
    "entry_mcap_sol",
    "entry_mcap_unavailable_reason",
    "candles_available",
    "n_candles",
    "max_multiple_after_entry",
    "ath_multiple",
    "reached_2x",
    "reached_3x",
    "adverse_multiple",
    "label_unavailable_reason",
    "symbol_len",
    "name_len",
    "symbol_has_digit",
    "symbol_all_caps",
    "symbol_has_emoji",
    "name_has_emoji",
    "has_twitter",
    "twitter_is_status_link",
    "has_website",
    "has_description",
    "description_len",
    "has_image",
    "has_profile_image",
    "reply_count",
    "verified",
    "nsfw",
    "boost_mode",
    "is_currently_live",
    "has_username",
    "market_cap",
    "usd_market_cap",
    "real_sol_reserves",
    "virtual_sol_reserves",
    "complete",
    "ath_market_cap",
    "ath_market_cap_timestamp",
    "ath_multiple_from_api",
    "error",
)

BOOKKEEPING_COLUMNS: tuple[str, ...] = ("first_seen_at", "updated_at")

_ALL_COLUMNS: tuple[str, ...] = FEATURE_COLUMNS + BOOKKEEPING_COLUMNS

_BOOL_COLUMNS = frozenset(
    {
        "deployer_is_fresh",
        "funding_available",
        "candles_available",
        "reached_2x",
        "reached_3x",
        "symbol_has_digit",
        "symbol_all_caps",
        "symbol_has_emoji",
        "name_has_emoji",
        "has_twitter",
        "twitter_is_status_link",
        "has_website",
        "has_description",
        "has_image",
        "has_profile_image",
        "verified",
        "nsfw",
        "is_currently_live",
        "has_username",
        "complete",
    }
)

_INT_COLUMNS = frozenset(
    {
        "created_at_ms",
        "hour_utc",
        "deployer_lifetime_creations",
        "n_candles",
        "symbol_len",
        "name_len",
        "description_len",
        "reply_count",
        "real_sol_reserves",
        "virtual_sol_reserves",
        "ath_market_cap_timestamp",
        "first_seen_at",
        "updated_at",
    }
)

_FLOAT_COLUMNS = frozenset(
    {
        "funding_amount_sol",
        "funding_band",
        "entry_price",
        "entry_mcap_sol",
        "max_multiple_after_entry",
        "ath_multiple",
        "adverse_multiple",
        "market_cap",
        "usd_market_cap",
        "ath_market_cap",
        "ath_multiple_from_api",
    }
)

_COLUMN_TYPES: dict[str, str] = {}
for _col in _ALL_COLUMNS:
    if _col in _BOOL_COLUMNS:
        _COLUMN_TYPES[_col] = "INTEGER"
    elif _col in _INT_COLUMNS:
        _COLUMN_TYPES[_col] = "INTEGER"
    elif _col in _FLOAT_COLUMNS:
        _COLUMN_TYPES[_col] = "REAL"
    else:
        _COLUMN_TYPES[_col] = "TEXT"


def _to_db(col: str, value: Any) -> Any:
    """Convert a record value to a SQLite-storable value."""
    if value is None:
        return None
    if col in _BOOL_COLUMNS:
        if isinstance(value, bool):
            return int(value)
        if isinstance(value, (int, float)):
            return int(bool(value))
        return None
    if isinstance(value, bool):
        return int(value)
    return value


def _from_db(col: str, value: Any) -> Any:
    """Convert a SQLite value back to a record value."""
    if value is None:
        return None
    if col in _BOOL_COLUMNS:
        return bool(value)
    return value


class AnalysisStore:
    """SQLite-backed store of per-launch feature records.

    Args:
        path: Filesystem path of the SQLite database.
    """

    def __init__(self, path: str | Path = DEFAULT_STORE_PATH) -> None:
        self.path = Path(path)
        if str(self.path.parent) and str(self.path.parent) != ".":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.path))
        self._conn.row_factory = sqlite3.Row
        self._init_schema()

    def _init_schema(self) -> None:
        """Create the launches table when missing."""
        self._conn.execute(
            'CREATE TABLE IF NOT EXISTS launches ("mint" TEXT PRIMARY KEY, '
            + ", ".join(
                f'"{c}" {_COLUMN_TYPES[c]}' for c in _ALL_COLUMNS if c != "mint"
            )
            + ")"
        )
        # Extend safely when new feature keys appear.
        existing = {row[1] for row in self._conn.execute("PRAGMA table_info(launches)")}
        for col in _ALL_COLUMNS:
            if col not in existing:
                self._conn.execute(
                    f'ALTER TABLE launches ADD COLUMN "{col}" {_COLUMN_TYPES[col]}'
                )
        self._conn.commit()

    def close(self) -> None:
        """Close the underlying connection."""
        self._conn.close()

    def upsert_launches(self, rows: Sequence[Mapping[str, Any]]) -> int:
        """Insert or replace feature records keyed by mint.

        Args:
            rows: Feature record dicts; unknown keys are ignored.

        Returns:
            Number of rows written.
        """
        now_ms = int(time.time() * 1000)
        written = 0
        for row in rows:
            if not isinstance(row, Mapping):
                continue
            mint = row.get("mint")
            if not isinstance(mint, str) or not mint:
                continue
            record: dict[str, Any] = {}
            for col in FEATURE_COLUMNS:
                record[col] = _to_db(col, row.get(col))
            record["updated_at"] = now_ms
            cur = self._conn.execute(
                "SELECT first_seen_at FROM launches WHERE mint = ?", (mint,)
            )
            existing_row = cur.fetchone()
            record["first_seen_at"] = (
                existing_row["first_seen_at"]
                if existing_row is not None
                and existing_row["first_seen_at"] is not None
                else now_ms
            )
            # Build insert explicitly: mint + remaining columns in order.
            ordered = ["mint", *[c for c in _ALL_COLUMNS if c != "mint"]]
            # first_seen_at/updated_at already converted (ints).
            values = [mint, *[record.get(c) for c in ordered[1:]]]
            placeholders = ", ".join(["?"] * len(ordered))
            colnames = ", ".join(f'"{c}"' for c in ordered)
            self._conn.execute(
                f"INSERT OR REPLACE INTO launches ({colnames}) VALUES ({placeholders})",
                values,
            )
            written += 1
        self._conn.commit()
        return written

    def get_launches(
        self, limit: int | None = None, order: str = "created_at_ms DESC"
    ) -> list[dict[str, Any]]:
        """Return stored feature records in the requested order.

        Args:
            limit: Maximum rows, or None for all rows.
            order: ORDER BY clause (validated to ``<col> ASC|DESC``).

        Returns:
            List of feature record dicts.
        """
        parts = order.strip().split()
        col = parts[0] if parts else "created_at_ms"
        direction = parts[1].upper() if len(parts) > 1 else "DESC"
        if col not in _ALL_COLUMNS:
            col = "created_at_ms"
        if direction not in ("ASC", "DESC"):
            direction = "DESC"
        query = f'SELECT * FROM launches ORDER BY "{col}" {direction}'
        if limit is not None:
            query += f" LIMIT {max(0, int(limit))}"
        out: list[dict[str, Any]] = []
        for db_row in self._conn.execute(query):
            record = {
                col_name: _from_db(col_name, db_row[col_name])
                for col_name in db_row.keys()
                if col_name in _ALL_COLUMNS or col_name == "mint"
            }
            out.append(record)
        return out

    def latest_created_at_ms(self) -> int | None:
        """Return the maximum stored ``created_at_ms``, or None when empty."""
        cur = self._conn.execute("SELECT MAX(created_at_ms) FROM launches")
        row = cur.fetchone()
        if row is None or row[0] is None:
            return None
        return int(row[0])

    def count(self) -> int:
        """Return the number of stored launches."""
        cur = self._conn.execute("SELECT COUNT(*) FROM launches")
        row = cur.fetchone()
        return int(row[0]) if row else 0

    def pending_label_count(self, now_ms: int, settle_seconds: int) -> int:
        """Count rows whose outcome labels are still unavailable.

        Args:
            now_ms: Current time in ms.
            settle_seconds: Minimum age in seconds for settled labels.

        Returns:
            Number of rows with ``label_unavailable_reason`` set.
        """
        _ = (now_ms, settle_seconds)
        cur = self._conn.execute(
            "SELECT COUNT(*) FROM launches WHERE label_unavailable_reason IS NOT NULL"
        )
        row = cur.fetchone()
        return int(row[0]) if row else 0
