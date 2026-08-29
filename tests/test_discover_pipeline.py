"""Recorded-finalized integration coverage for the headless discovery path."""

from __future__ import annotations

import json
from pathlib import Path
from time import monotonic_ns, time_ns
from uuid import uuid4

import base58

from rugbot.backtest.trajectory.finalized_trade_builder import (
    decode_pump_trade_event_proofs,
)
from rugbot.discover.candidates import query_candidates
from rugbot.discover.collector import _created_at, _transaction_actors
from rugbot.discover.store import (
    append_observation,
    ensure_discover_schema,
    fetch_entity_mint_windows,
    fetch_wallet_basket_scan,
    save_entity_mints,
    save_mint_transaction_candidates,
    save_wallet_basket_scan,
    upsert_launch,
    upsert_trade,
    upsert_wallet_launch_participation,
)
from rugbot.domain.decisions import AbstainResult
from rugbot.domain.observations import RawChainObservation
from rugbot.integrations.solscan import SolscanMintTransactionCandidate
from rugbot.intelligence.entity_mint_index import FinalizedEntityMint
from rugbot.storage.database import DatabaseManager

FIXTURE = next(Path("fixtures/finalized_transactions/pump_create_v2").glob("*.json"))


def _recorded_observation() -> RawChainObservation:
    fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
    response = fixture["json_parsed_transaction_response"]
    response_body = json.dumps(
        {"jsonrpc": "2.0", "id": 1, "result": response},
        separators=(",", ":"),
    ).encode()
    return RawChainObservation(
        raw_id=uuid4(),
        source_id="recorded-solana-rpc",
        observer_id="discover-integration-test",
        boot_id=uuid4(),
        receive_sequence=1,
        slot=response["slot"],
        parent_slot=None,
        blockhash=None,
        signature=base58.b58decode(response["transaction"]["signatures"][0]),
        transaction_index=0,
        outer_instruction_index=None,
        inner_instruction_group_index=None,
        inner_instruction_index=None,
        stack_height=None,
        event_ordinal=None,
        commitment="finalized",
        canonical_status="canonical",
        received_wall_ns=time_ns(),
        received_monotonic_ns=monotonic_ns(),
        program_id=None,
        account_pubkey=None,
        account_owner_program_id=None,
        raw_transaction=response_body,
        raw_transaction_format="solana.getTransaction.jsonParsed.v1",
        raw_account_data=None,
        account_write_version=None,
        source_update_kind="transaction",
        raw_source_status=None,
        raw_source_payload=response_body,
        decoder_name=None,
        decoder_version=None,
        idl_hash=None,
    )


def test_recorded_finalized_trade_is_decoded_with_real_amounts_and_actors() -> None:
    observation = _recorded_observation()

    events = decode_pump_trade_event_proofs(observation)
    assert not isinstance(events, AbstainResult)
    assert len(events) == 1
    _, event = events[0]
    assert event.is_buy is True
    assert event.sol_amount_base_units == 10_000
    assert event.token_amount_base_units == 357_666_547
    assert event.user == "CvoPbuS2AghzVBYJx7HfQGhALiqif4YwWgHvXmhehuJZ"

    fee_payer, signers = _transaction_actors(observation)
    assert fee_payer is not None
    assert signers[0] == fee_payer
    assert _created_at(observation) is not None


def test_store_keeps_multiple_events_per_signature_and_mint_jsonl(
    tmp_path: Path,
) -> None:
    database = DatabaseManager(tmp_path / "rugbot.db")
    ensure_discover_schema(database)
    signature = "recorded-signature"
    common = {
        "db": database,
        "mint": "GGYkiUJGopoH9DZUjGFtLiKpXXW5AQovqZ7eVcjEpump",
        "signature": signature,
        "slot": 123,
        "side": "buy",
        "quote_amount_base_units": 10_000,
        "signers_json": "[]",
    }

    assert upsert_trade(event_index=0, **common) is True
    assert upsert_trade(event_index=1, **common) is True
    assert upsert_trade(event_index=1, **common) is False
    count = database.connection.execute(
        "SELECT COUNT(*) AS count FROM discover_trades"
    ).fetchone()
    assert count["count"] == 2

    observation = _recorded_observation()
    mint = common["mint"]
    assert append_observation(tmp_path, observation, mint=mint) is True
    assert (tmp_path / "observations" / f"{mint}.jsonl").exists()
    assert not (tmp_path / "observations" / "unknown.jsonl").exists()


def test_candidates_require_finalized_time_and_do_not_auto_qualify(
    tmp_path: Path,
) -> None:
    database = DatabaseManager(tmp_path / "rugbot.db")
    ensure_discover_schema(database)
    common = {
        "db": database,
        "creator": "CvoPbuS2AghzVBYJx7HfQGhALiqif4YwWgHvXmhehuJZ",
        "created_signature": "signature",
        "created_slot": 123,
    }
    upsert_launch(
        mint="GGYkiUJGopoH9DZUjGFtLiKpXXW5AQovqZ7eVcjEpump",
        created_at="2026-08-26T08:00:00+00:00",
        **common,
    )
    upsert_launch(
        mint="11111111111111111111111111111111",
        created_at=None,
        **common,
    )

    rows = query_candidates(
        state_dir=tmp_path,
        since="2026-08-01",
        limit=10,
    )

    assert [row["mint"] for row in rows] == [
        "GGYkiUJGopoH9DZUjGFtLiKpXXW5AQovqZ7eVcjEpump"
    ]
    assert "_bible_pass" not in rows[0]


def test_wallet_basket_checkpoint_and_indexed_candidates_are_durable(
    tmp_path: Path,
) -> None:
    database = DatabaseManager(tmp_path / "rugbot.db")
    ensure_discover_schema(database)
    wallet = "CvoPbuS2AghzVBYJx7HfQGhALiqif4YwWgHvXmhehuJZ"
    candidate = SolscanMintTransactionCandidate(
        signature="recorded-signature",
        slot=123,
        transaction_index=4,
        block_time=1_780_000_000,
        matched_mints=("GGYkiUJGopoH9DZUjGFtLiKpXXW5AQovqZ7eVcjEpump",),
    )

    save_mint_transaction_candidates(
        database,
        wallet=wallet,
        candidates=(candidate,),
    )
    save_wallet_basket_scan(
        database,
        wallet=wallet,
        cursor="next-page",
        pages_scanned=5,
        total_candidates=1,
        complete=False,
        warning="rate limited",
    )

    checkpoint = fetch_wallet_basket_scan(database, wallet)
    assert checkpoint is not None
    assert checkpoint["cursor"] == "next-page"
    assert checkpoint["pages_scanned"] == 5
    assert checkpoint["complete"] == 0
    row = database.connection.execute(
        "SELECT * FROM discover_mint_transaction_candidates"
    ).fetchone()
    assert row["signature"] == "recorded-signature"
    assert row["confirmed"] == 0


def test_finalized_entity_windows_and_wallet_participation_are_durable(
    tmp_path: Path,
) -> None:
    database = DatabaseManager(tmp_path / "rugbot.db")
    ensure_discover_schema(database)
    mint = FinalizedEntityMint(
        mint="GGYkiUJGopoH9DZUjGFtLiKpXXW5AQovqZ7eVcjEpump",
        creator="CvoPbuS2AghzVBYJx7HfQGhALiqif4YwWgHvXmhehuJZ",
        name="Recorded",
        symbol="REC",
        created_timestamp=1_780_000_000,
        creation_slot=123,
        creation_signature="recorded-creation-signature",
        creation_transaction_index=7,
        bonding_curve="11111111111111111111111111111111",
        relation="target_creator",
    )
    save_entity_mints(database, (mint,))
    upsert_wallet_launch_participation(
        database,
        wallet="FJz6SLz8CQBmm692kfp6e8s9FZPuqnKX5ZNcr7k5Kadd",
        mint=mint.mint,
        creation_slot=123,
        window_end_slot=243,
        transactions_cached=4,
        buy_count=1,
        sell_count=1,
        first_buy_slot=125,
        last_sell_slot=140,
        buy_quote_lamports=10,
        sell_quote_lamports=12,
        complete=True,
        warning=None,
    )

    assert fetch_entity_mint_windows(database, mint.creator) == ((mint.mint, 123),)
    row = database.connection.execute(
        "SELECT * FROM discover_wallet_launch_participation"
    ).fetchone()
    assert row["first_buy_slot"] == 125
    assert row["complete"] == 1
