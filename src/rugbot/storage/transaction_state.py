"""Durable transaction intent state for idempotent sniper execution."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from enum import StrEnum
from threading import RLock
from typing import TYPE_CHECKING

from solders.pubkey import Pubkey

from rugbot.execution.ports import ExecutionIntent, validate_execution_intent

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path


class TransactionState(StrEnum):
    """Durable lifecycle of one economic execution decision."""

    INTENT = "INTENT"
    SIGNED = "SIGNED"
    SUBMITTED = "SUBMITTED"
    CONFIRMED = "CONFIRMED"
    RECONCILED = "RECONCILED"
    FAILED = "FAILED"
    EXPIRED = "EXPIRED"
    CANCELLED = "CANCELLED"


@dataclass(frozen=True, slots=True)
class TransactionIntentRecord:
    """One persisted execution intent and its observed lifecycle facts."""

    intent_id: str
    market_id: str
    wallet_pubkey: str
    side: str
    quote_amount_base_units: int | None
    base_amount_base_units: int | None
    max_slippage_bps: int
    reason_codes: tuple[str, ...]
    state: TransactionState
    raw_tx_bytes: bytes | None
    signature: str | None
    blockhash: str | None
    last_valid_block_height: int | None
    created_at_slot: int
    submitted_at_ts: int | None
    landed_slot: int | None
    confirmed_at_ts: int | None
    reconciled_at_ts: int | None
    token_delta_base_units: int | None
    sol_delta_lamports: int | None
    network_fee_lamports: int | None
    jito_tip_lamports: int | None
    ata_rent_lamports: int | None
    protocol_fee_lamports: int | None
    error_code: str | None
    error_message: str | None


class TransactionStateStoreError(ValueError):
    """Raised when durable transaction state violates its contract."""

    @classmethod
    def opened_failed(cls) -> TransactionStateStoreError:
        """Build an error for a database that could not be opened."""

        return cls("transaction state database could not be opened")

    @classmethod
    def invalid(cls, message: str) -> TransactionStateStoreError:
        """Build an error for invalid input or persisted facts."""

        return cls(message)

    @classmethod
    def intent_collision(cls) -> TransactionStateStoreError:
        """Build an error for a reused intent identifier."""

        return cls("intent_id already belongs to a different economic decision")

    @classmethod
    def different_facts(
        cls,
        intent_id: str,
        state: TransactionState,
    ) -> TransactionStateStoreError:
        """Build an error for conflicting facts at an idempotent transition."""

        return cls(f"{intent_id} already reached {state} with different facts")

    @classmethod
    def invalid_transition(
        cls,
        intent_id: str,
        state: TransactionState,
        allowed: str,
    ) -> TransactionStateStoreError:
        """Build an error for an invalid lifecycle transition."""

        return cls(f"cannot transition {intent_id} from {state}; expected {allowed}")

    @classmethod
    def unknown_intent(cls, intent_id: str) -> TransactionStateStoreError:
        """Build an error for an unknown durable intent."""

        return cls(f"unknown intent_id: {intent_id}")

    @classmethod
    def closed(cls) -> TransactionStateStoreError:
        """Build an error for use after close."""

        return cls("transaction state store is closed")

    @classmethod
    def malformed_record(cls) -> TransactionStateStoreError:
        """Build an error for corrupted durable state."""

        return cls("transaction state database contains a malformed record")

    @classmethod
    def invalid_raw_transaction(cls) -> TransactionStateStoreError:
        """Build an error for missing signed transaction bytes."""

        return cls("raw_tx_bytes must be non-empty bytes")

    @classmethod
    def invalid_pubkey(cls, field_name: str) -> TransactionStateStoreError:
        """Build an error for a malformed Solana public key."""

        return cls(f"{field_name} must be a valid Solana public key")

    @classmethod
    def non_empty_text_required(cls, field_name: str) -> TransactionStateStoreError:
        """Build an error for a missing text value."""

        return cls(f"{field_name} must be non-empty text")

    @classmethod
    def non_negative_integer_required(
        cls,
        field_name: str,
    ) -> TransactionStateStoreError:
        """Build an error for a negative or non-integer value."""

        return cls(f"{field_name} must be a non-negative integer")

    @classmethod
    def integer_required(cls, field_name: str) -> TransactionStateStoreError:
        """Build an error for a non-integer value."""

        return cls(f"{field_name} must be an integer")


class SqliteTransactionStateStore:
    """Persist execution state transitions atomically in SQLite."""

    _RECOVERY_STATES = (
        TransactionState.INTENT,
        TransactionState.SIGNED,
        TransactionState.SUBMITTED,
        TransactionState.CONFIRMED,
    )

    def __init__(self, path: Path) -> None:
        """Open the state database and create its fixed transaction table."""

        self._lock = RLock()
        self._connection: sqlite3.Connection | None = None
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            connection = sqlite3.connect(
                path,
                isolation_level=None,
                check_same_thread=False,
            )
            connection.row_factory = sqlite3.Row
            self._connection = connection
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute("PRAGMA synchronous = FULL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS transaction_intents (
                    intent_id TEXT PRIMARY KEY NOT NULL,
                    market_id TEXT NOT NULL,
                    wallet_pubkey TEXT NOT NULL,
                    side TEXT NOT NULL CHECK(side IN ('buy', 'sell')),
                    quote_amount_base_units INTEGER,
                    base_amount_base_units INTEGER,
                    max_slippage_bps INTEGER NOT NULL,
                    reason_codes_json TEXT NOT NULL,
                    state TEXT NOT NULL CHECK(state IN (
                        'INTENT', 'SIGNED', 'SUBMITTED', 'CONFIRMED',
                        'RECONCILED', 'FAILED', 'EXPIRED', 'CANCELLED'
                    )),
                    raw_tx_bytes BLOB,
                    signature TEXT,
                    blockhash TEXT,
                    last_valid_block_height INTEGER,
                    created_at_slot INTEGER NOT NULL,
                    submitted_at_ts INTEGER,
                    landed_slot INTEGER,
                    confirmed_at_ts INTEGER,
                    reconciled_at_ts INTEGER,
                    token_delta_base_units INTEGER,
                    sol_delta_lamports INTEGER,
                    network_fee_lamports INTEGER,
                    jito_tip_lamports INTEGER,
                    ata_rent_lamports INTEGER,
                    protocol_fee_lamports INTEGER,
                    error_code TEXT,
                    error_message TEXT
                );
                """
            )
        except sqlite3.Error as error:
            if self._connection is not None:
                self._connection.close()
            raise TransactionStateStoreError.opened_failed() from error

    def close(self) -> None:
        """Close the database connection."""

        with self._lock:
            if self._connection is not None:
                self._connection.close()
                self._connection = None

    def __enter__(self) -> SqliteTransactionStateStore:
        """Return this store for scoped ownership."""

        return self

    def __exit__(self, *_: object) -> None:
        """Close the store at the end of scoped ownership."""

        self.close()

    def create_intent(
        self,
        intent: ExecutionIntent,
        *,
        wallet_pubkey: str,
    ) -> TransactionIntentRecord:
        """Create an INTENT or return its exact existing durable identity."""

        error = validate_execution_intent(intent)
        if error is not None:
            raise TransactionStateStoreError(error)
        _validate_pubkey(wallet_pubkey, "wallet_pubkey")
        reason_codes_json = json.dumps(
            intent.reason_codes,
            separators=(",", ":"),
        )
        with self._write_transaction() as connection:
            existing = self._get_with_connection(connection, intent.intent_id)
            if existing is not None:
                expected_identity = (
                    intent.market_id,
                    wallet_pubkey,
                    intent.side,
                    intent.quote_amount_base_units,
                    intent.base_amount_base_units,
                    intent.max_slippage_bps,
                    intent.reason_codes,
                    int(intent.as_of_slot),
                )
                if _economic_identity(existing) != expected_identity:
                    raise TransactionStateStoreError.intent_collision()
                return existing
            connection.execute(
                """
                INSERT INTO transaction_intents (
                    intent_id, market_id, wallet_pubkey, side,
                    quote_amount_base_units, base_amount_base_units,
                    max_slippage_bps, reason_codes_json, state, created_at_slot
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    intent.intent_id,
                    intent.market_id,
                    wallet_pubkey,
                    intent.side,
                    intent.quote_amount_base_units,
                    intent.base_amount_base_units,
                    intent.max_slippage_bps,
                    reason_codes_json,
                    TransactionState.INTENT,
                    int(intent.as_of_slot),
                ),
            )
            return self._get_required(connection, intent.intent_id)

    def get(self, intent_id: str) -> TransactionIntentRecord | None:
        """Return one durable intent, if present."""

        _validate_non_empty_text(intent_id, "intent_id")
        with self._locked_connection() as connection:
            return self._get_with_connection(connection, intent_id)

    def list_recovery_pending(self) -> tuple[TransactionIntentRecord, ...]:
        """Return all non-terminal records requiring restart recovery."""

        placeholders = ",".join("?" for _ in self._RECOVERY_STATES)
        with self._locked_connection() as connection:
            rows = connection.execute(
                f"""
                SELECT * FROM transaction_intents
                WHERE state IN ({placeholders})
                ORDER BY rowid
                """,  # noqa: S608 - placeholders are generated, not external input.
                tuple(self._RECOVERY_STATES),
            ).fetchall()
        return tuple(_record_from_row(row) for row in rows)

    def list_all(self) -> tuple[TransactionIntentRecord, ...]:
        """Return the complete durable intent ledger in insertion order."""

        with self._locked_connection() as connection:
            rows = connection.execute(
                "SELECT * FROM transaction_intents ORDER BY rowid"
            ).fetchall()
        return tuple(_record_from_row(row) for row in rows)

    def store_signed(
        self,
        intent_id: str,
        *,
        raw_tx_bytes: bytes,
        signature: str,
        blockhash: str,
        last_valid_block_height: int,
    ) -> TransactionIntentRecord:
        """Persist the exact signed transaction before network submission."""

        if type(raw_tx_bytes) is not bytes or not raw_tx_bytes:
            raise TransactionStateStoreError.invalid_raw_transaction()
        _validate_non_empty_text(signature, "signature")
        _validate_non_empty_text(blockhash, "blockhash")
        _validate_non_negative_int(
            last_valid_block_height,
            "last_valid_block_height",
        )
        return self._transition(
            intent_id,
            source_states=(TransactionState.INTENT,),
            target_state=TransactionState.SIGNED,
            updates={
                "raw_tx_bytes": raw_tx_bytes,
                "signature": signature,
                "blockhash": blockhash,
                "last_valid_block_height": last_valid_block_height,
            },
        )

    def mark_submitted(
        self,
        intent_id: str,
        *,
        submitted_at_ts: int,
    ) -> TransactionIntentRecord:
        """Record that the signed bytes were handed to a network sender."""

        _validate_non_negative_int(submitted_at_ts, "submitted_at_ts")
        return self._transition(
            intent_id,
            source_states=(TransactionState.SIGNED,),
            target_state=TransactionState.SUBMITTED,
            updates={"submitted_at_ts": submitted_at_ts},
        )

    def mark_confirmed(
        self,
        intent_id: str,
        *,
        landed_slot: int,
        confirmed_at_ts: int,
    ) -> TransactionIntentRecord:
        """Record operational confirmation and the landed slot."""

        _validate_non_negative_int(landed_slot, "landed_slot")
        _validate_non_negative_int(confirmed_at_ts, "confirmed_at_ts")
        return self._transition(
            intent_id,
            source_states=(TransactionState.SUBMITTED,),
            target_state=TransactionState.CONFIRMED,
            updates={
                "landed_slot": landed_slot,
                "confirmed_at_ts": confirmed_at_ts,
            },
        )

    def mark_reconciled(  # noqa: PLR0913 - persisted fee attribution is explicit.
        self,
        intent_id: str,
        *,
        reconciled_at_ts: int,
        token_delta_base_units: int,
        sol_delta_lamports: int,
        network_fee_lamports: int,
        jito_tip_lamports: int,
        ata_rent_lamports: int,
        protocol_fee_lamports: int,
    ) -> TransactionIntentRecord:
        """Persist finalized balance deltas and separately attributed fees."""

        _validate_non_negative_int(reconciled_at_ts, "reconciled_at_ts")
        for field_name, value in (
            ("token_delta_base_units", token_delta_base_units),
            ("sol_delta_lamports", sol_delta_lamports),
        ):
            _validate_int(value, field_name)
        for field_name, value in (
            ("network_fee_lamports", network_fee_lamports),
            ("jito_tip_lamports", jito_tip_lamports),
            ("ata_rent_lamports", ata_rent_lamports),
            ("protocol_fee_lamports", protocol_fee_lamports),
        ):
            _validate_non_negative_int(value, field_name)
        return self._transition(
            intent_id,
            source_states=(TransactionState.CONFIRMED,),
            target_state=TransactionState.RECONCILED,
            updates={
                "reconciled_at_ts": reconciled_at_ts,
                "token_delta_base_units": token_delta_base_units,
                "sol_delta_lamports": sol_delta_lamports,
                "network_fee_lamports": network_fee_lamports,
                "jito_tip_lamports": jito_tip_lamports,
                "ata_rent_lamports": ata_rent_lamports,
                "protocol_fee_lamports": protocol_fee_lamports,
            },
        )

    def mark_failed(
        self,
        intent_id: str,
        *,
        error_code: str,
        error_message: str,
    ) -> TransactionIntentRecord:
        """Terminate a pre-confirmation intent with an explicit failure."""

        _validate_non_empty_text(error_code, "error_code")
        _validate_non_empty_text(error_message, "error_message")
        return self._transition(
            intent_id,
            source_states=(
                TransactionState.INTENT,
                TransactionState.SIGNED,
                TransactionState.SUBMITTED,
            ),
            target_state=TransactionState.FAILED,
            updates={
                "error_code": error_code,
                "error_message": error_message,
            },
        )

    def mark_expired(
        self,
        intent_id: str,
        *,
        error_message: str,
    ) -> TransactionIntentRecord:
        """Terminate a signed or submitted transaction after blockhash expiry."""

        _validate_non_empty_text(error_message, "error_message")
        return self._transition(
            intent_id,
            source_states=(TransactionState.SIGNED, TransactionState.SUBMITTED),
            target_state=TransactionState.EXPIRED,
            updates={
                "error_code": "blockhash_expired",
                "error_message": error_message,
            },
        )

    def mark_cancelled(
        self,
        intent_id: str,
        *,
        error_message: str,
    ) -> TransactionIntentRecord:
        """Cancel an intent only while it is safe to know it was not submitted."""

        _validate_non_empty_text(error_message, "error_message")
        return self._transition(
            intent_id,
            source_states=(TransactionState.INTENT, TransactionState.SIGNED),
            target_state=TransactionState.CANCELLED,
            updates={
                "error_code": "cancelled",
                "error_message": error_message,
            },
        )

    def _transition(
        self,
        intent_id: str,
        *,
        source_states: tuple[TransactionState, ...],
        target_state: TransactionState,
        updates: Mapping[str, object],
    ) -> TransactionIntentRecord:
        _validate_non_empty_text(intent_id, "intent_id")
        with self._write_transaction() as connection:
            existing = self._get_required(connection, intent_id)
            if existing.state is target_state:
                if all(
                    getattr(existing, key) == value for key, value in updates.items()
                ):
                    return existing
                raise TransactionStateStoreError.different_facts(
                    intent_id,
                    target_state,
                )
            if existing.state not in source_states:
                allowed = ", ".join(state.value for state in source_states)
                raise TransactionStateStoreError.invalid_transition(
                    intent_id,
                    existing.state,
                    allowed,
                )
            assignments = ", ".join(f"{column} = ?" for column in updates)
            connection.execute(
                f"""
                UPDATE transaction_intents
                SET state = ?, {assignments}
                WHERE intent_id = ?
                """,  # noqa: S608 - columns are fixed by internal callers.
                (target_state, *updates.values(), intent_id),
            )
            return self._get_required(connection, intent_id)

    def _get_required(
        self,
        connection: sqlite3.Connection,
        intent_id: str,
    ) -> TransactionIntentRecord:
        record = self._get_with_connection(connection, intent_id)
        if record is None:
            raise TransactionStateStoreError.unknown_intent(intent_id)
        return record

    @staticmethod
    def _get_with_connection(
        connection: sqlite3.Connection,
        intent_id: str,
    ) -> TransactionIntentRecord | None:
        row = connection.execute(
            "SELECT * FROM transaction_intents WHERE intent_id = ?",
            (intent_id,),
        ).fetchone()
        return None if row is None else _record_from_row(row)

    def _connection_or_raise(self) -> sqlite3.Connection:
        if self._connection is None:
            raise TransactionStateStoreError.closed()
        return self._connection

    def _locked_connection(self) -> _ConnectionLock:
        return _ConnectionLock(self._lock, self._connection_or_raise())

    def _write_transaction(self) -> _WriteTransaction:
        return _WriteTransaction(self._lock, self._connection_or_raise())


class _ConnectionLock:
    def __init__(self, lock: RLock, connection: sqlite3.Connection) -> None:
        self._lock = lock
        self._connection = connection

    def __enter__(self) -> sqlite3.Connection:
        self._lock.acquire()
        return self._connection

    def __exit__(self, *_: object) -> None:
        self._lock.release()


class _WriteTransaction(_ConnectionLock):
    def __enter__(self) -> sqlite3.Connection:
        connection = super().__enter__()
        try:
            connection.execute("BEGIN IMMEDIATE")
        except BaseException:
            self._lock.release()
            raise
        return connection

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        try:
            if exc_type is None:
                self._connection.commit()
            else:
                self._connection.rollback()
        finally:
            self._lock.release()


def _record_from_row(row: sqlite3.Row) -> TransactionIntentRecord:
    try:
        reason_codes = _reason_codes_from_json(row["reason_codes_json"])
        raw_tx_bytes = _optional_bytes(row["raw_tx_bytes"])
        return TransactionIntentRecord(
            intent_id=row["intent_id"],
            market_id=row["market_id"],
            wallet_pubkey=row["wallet_pubkey"],
            side=row["side"],
            quote_amount_base_units=row["quote_amount_base_units"],
            base_amount_base_units=row["base_amount_base_units"],
            max_slippage_bps=row["max_slippage_bps"],
            reason_codes=reason_codes,
            state=TransactionState(row["state"]),
            raw_tx_bytes=raw_tx_bytes,
            signature=row["signature"],
            blockhash=row["blockhash"],
            last_valid_block_height=row["last_valid_block_height"],
            created_at_slot=row["created_at_slot"],
            submitted_at_ts=row["submitted_at_ts"],
            landed_slot=row["landed_slot"],
            confirmed_at_ts=row["confirmed_at_ts"],
            reconciled_at_ts=row["reconciled_at_ts"],
            token_delta_base_units=row["token_delta_base_units"],
            sol_delta_lamports=row["sol_delta_lamports"],
            network_fee_lamports=row["network_fee_lamports"],
            jito_tip_lamports=row["jito_tip_lamports"],
            ata_rent_lamports=row["ata_rent_lamports"],
            protocol_fee_lamports=row["protocol_fee_lamports"],
            error_code=row["error_code"],
            error_message=row["error_message"],
        )
    except (KeyError, TypeError, ValueError) as error:
        raise TransactionStateStoreError.malformed_record() from error


def _reason_codes_from_json(value: object) -> tuple[str, ...]:
    if type(value) is not str:
        raise ValueError
    reasons = json.loads(value)
    if not isinstance(reasons, list) or not all(
        type(reason) is str and reason for reason in reasons
    ):
        raise ValueError
    return tuple(reasons)


def _optional_bytes(value: object) -> bytes | None:
    if value is not None and type(value) is not bytes:
        raise ValueError
    return value


def _economic_identity(record: TransactionIntentRecord) -> tuple[object, ...]:
    return (
        record.market_id,
        record.wallet_pubkey,
        record.side,
        record.quote_amount_base_units,
        record.base_amount_base_units,
        record.max_slippage_bps,
        record.reason_codes,
        record.created_at_slot,
    )


def _validate_pubkey(value: object, field_name: str) -> None:
    _validate_non_empty_text(value, field_name)
    try:
        Pubkey.from_string(value)
    except ValueError as error:
        raise TransactionStateStoreError.invalid_pubkey(field_name) from error


def _validate_non_empty_text(value: object, field_name: str) -> None:
    if type(value) is not str or not value:
        raise TransactionStateStoreError.non_empty_text_required(field_name)


def _validate_non_negative_int(value: object, field_name: str) -> None:
    if type(value) is not int or value < 0:
        raise TransactionStateStoreError.non_negative_integer_required(field_name)


def _validate_int(value: object, field_name: str) -> None:
    if type(value) is not int:
        raise TransactionStateStoreError.integer_required(field_name)


__all__ = [
    "SqliteTransactionStateStore",
    "TransactionIntentRecord",
    "TransactionState",
    "TransactionStateStoreError",
]
