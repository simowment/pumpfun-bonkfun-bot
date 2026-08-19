"""SQLite integration tests for durable sniper transaction state."""

from pathlib import Path

import pytest
from solders.keypair import Keypair

from rugbot.execution.ports import ExecutionIntent
from rugbot.storage.transaction_state import (
    SqliteTransactionStateStore,
    TransactionState,
    TransactionStateStoreError,
)


def _intent(
    *,
    intent_id: str = "launch-100-buy",
    quote_amount_base_units: int = 25_000_000,
) -> ExecutionIntent:
    return ExecutionIntent(
        intent_id=intent_id,
        as_of_slot=100,
        market_id=str(Keypair().pubkey()),
        side="buy",
        quote_amount_base_units=quote_amount_base_units,
        base_amount_base_units=None,
        max_slippage_bps=500,
        reason_codes=("known_operator_wallet",),
    )


def test_full_lifecycle_survives_database_reopen(tmp_path: Path) -> None:
    database_path = tmp_path / "transactions.sqlite3"
    intent = _intent()
    wallet_pubkey = str(Keypair().pubkey())

    with SqliteTransactionStateStore(database_path) as store:
        created = store.create_intent(intent, wallet_pubkey=wallet_pubkey)
        assert created.state is TransactionState.INTENT
        signed = store.store_signed(
            intent.intent_id,
            raw_tx_bytes=b"signed-versioned-transaction",
            signature="5signature",
            blockhash="5blockhash",
            last_valid_block_height=150,
        )
        assert signed.raw_tx_bytes == b"signed-versioned-transaction"
        store.mark_submitted(intent.intent_id, submitted_at_ts=1_000)
        store.mark_confirmed(
            intent.intent_id,
            landed_slot=102,
            confirmed_at_ts=1_100,
        )

    with SqliteTransactionStateStore(database_path) as reopened:
        pending = reopened.list_recovery_pending()
        assert len(pending) == 1
        assert pending[0].state is TransactionState.CONFIRMED
        reconciled = reopened.mark_reconciled(
            intent.intent_id,
            reconciled_at_ts=1_200,
            token_delta_base_units=123_456,
            sol_delta_lamports=-25_700_000,
            network_fee_lamports=5_000,
            jito_tip_lamports=100_000,
            ata_rent_lamports=2_039_280,
            protocol_fee_lamports=250_000,
        )
        assert reconciled.state is TransactionState.RECONCILED
        assert reconciled.token_delta_base_units == 123_456
        assert reconciled.sol_delta_lamports == -25_700_000
        assert reopened.list_recovery_pending() == ()


def test_duplicate_identity_is_idempotent_but_collision_is_rejected(
    tmp_path: Path,
) -> None:
    store = SqliteTransactionStateStore(tmp_path / "transactions.sqlite3")
    wallet_pubkey = str(Keypair().pubkey())
    intent = _intent()

    first = store.create_intent(intent, wallet_pubkey=wallet_pubkey)
    duplicate = store.create_intent(intent, wallet_pubkey=wallet_pubkey)
    assert duplicate == first

    collision = ExecutionIntent(
        intent_id=intent.intent_id,
        as_of_slot=intent.as_of_slot,
        market_id=intent.market_id,
        side="buy",
        quote_amount_base_units=50_000_000,
        base_amount_base_units=None,
        max_slippage_bps=intent.max_slippage_bps,
        reason_codes=intent.reason_codes,
    )
    with pytest.raises(TransactionStateStoreError, match="different economic"):
        store.create_intent(collision, wallet_pubkey=wallet_pubkey)


def test_signed_facts_are_idempotent_and_cannot_be_replaced(tmp_path: Path) -> None:
    store = SqliteTransactionStateStore(tmp_path / "transactions.sqlite3")
    intent = _intent()
    wallet_pubkey = str(Keypair().pubkey())
    store.create_intent(intent, wallet_pubkey=wallet_pubkey)
    signed = store.store_signed(
        intent.intent_id,
        raw_tx_bytes=b"signed-versioned-transaction",
        signature="5signature",
        blockhash="5blockhash",
        last_valid_block_height=150,
    )

    assert (
        store.store_signed(
            intent.intent_id,
            raw_tx_bytes=b"signed-versioned-transaction",
            signature="5signature",
            blockhash="5blockhash",
            last_valid_block_height=150,
        )
        == signed
    )
    with pytest.raises(TransactionStateStoreError, match="different facts"):
        store.store_signed(
            intent.intent_id,
            raw_tx_bytes=b"different-signed-transaction",
            signature="6signature",
            blockhash="5blockhash",
            last_valid_block_height=150,
        )


@pytest.mark.parametrize(
    ("crash_state", "expected_state"),
    [
        ("intent", TransactionState.INTENT),
        ("signed", TransactionState.SIGNED),
        ("submitted", TransactionState.SUBMITTED),
    ],
)
def test_crash_boundaries_are_recoverable(
    tmp_path: Path,
    crash_state: str,
    expected_state: TransactionState,
) -> None:
    database_path = tmp_path / f"{crash_state}.sqlite3"
    intent = _intent(intent_id=f"{crash_state}-intent")
    wallet_pubkey = str(Keypair().pubkey())

    store = SqliteTransactionStateStore(database_path)
    store.create_intent(intent, wallet_pubkey=wallet_pubkey)
    if crash_state in {"signed", "submitted"}:
        store.store_signed(
            intent.intent_id,
            raw_tx_bytes=b"exact-signed-bytes",
            signature="5signature",
            blockhash="5blockhash",
            last_valid_block_height=150,
        )
    if crash_state == "submitted":
        store.mark_submitted(intent.intent_id, submitted_at_ts=1_000)
    store.close()

    with SqliteTransactionStateStore(database_path) as recovered:
        pending = recovered.list_recovery_pending()
        assert len(pending) == 1
        assert pending[0].state is expected_state
        if crash_state != "intent":
            assert pending[0].raw_tx_bytes == b"exact-signed-bytes"
            assert pending[0].signature == "5signature"


def test_invalid_transition_does_not_mutate_durable_state(tmp_path: Path) -> None:
    store = SqliteTransactionStateStore(tmp_path / "transactions.sqlite3")
    intent = _intent()
    store.create_intent(intent, wallet_pubkey=str(Keypair().pubkey()))

    with pytest.raises(TransactionStateStoreError, match="cannot transition"):
        store.mark_confirmed(
            intent.intent_id,
            landed_slot=102,
            confirmed_at_ts=1_100,
        )

    assert store.get(intent.intent_id).state is TransactionState.INTENT
