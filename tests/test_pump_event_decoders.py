"""Recorded-finalized coverage for the canonical Anchor event reader.

Every assertion here is pinned to the recorded Pump AMM ``BuyEvent`` in
``fixtures/finalized_transactions/pump_swap_event`` or to the fail-closed
branches that guard it. No synthetic amounts are invented: the golden values
were decoded from real finalized chain evidence.
"""

from __future__ import annotations

import base64
import json
from pathlib import Path

import base58

from rugbot.backtest.trajectory.finalized_trade_builder import _TradeEventReader
from rugbot.domain.decisions import AbstainReason, AbstainResult
from rugbot.domain.trades import TradeSide
from rugbot.ingest.pump.swap_event_decoder import (
    EventReader,
    decode_pump_swap_trade_event,
)

FIXTURE = next(Path("fixtures/finalized_transactions/pump_swap_event").glob("*.json"))


def _recorded_event() -> tuple[bytes, int, bytes, int]:
    """Return the recorded payload, slot, signature, and event index."""

    document = json.loads(FIXTURE.read_text(encoding="utf-8"))
    assert document["commitment"] == "finalized"
    return (
        base64.b64decode(document["data_base64"]),
        document["as_of_slot"],
        base58.b58decode(document["signature"]),
        document["event_index"],
    )


def _decode(payload: bytes, *, signature: bytes | None = None):
    _, as_of_slot, recorded_signature, event_index = _recorded_event()
    return decode_pump_swap_trade_event(
        payload,
        as_of_slot=as_of_slot,
        signature=signature or recorded_signature,
        event_index=event_index,
    )


def test_recorded_finalized_pump_swap_event_decodes_to_real_amounts() -> None:
    payload, as_of_slot, signature, event_index = _recorded_event()

    evidence = decode_pump_swap_trade_event(
        payload,
        as_of_slot=as_of_slot,
        signature=signature,
        event_index=event_index,
    )

    assert not isinstance(evidence, AbstainResult)
    assert evidence.as_of_slot == 436899999
    assert evidence.event_index == 0
    assert evidence.signature == signature
    assert evidence.side is TradeSide.BUY
    assert evidence.instruction_name == "buy"
    assert evidence.timestamp == 1785730231
    assert evidence.pool == "ENRfKRctvzQysufes1ityGRX9XkM5EJftVkE3kck4e7y"
    assert evidence.user == "9Hb2XhRcE1uoQa9uCoPpkQ2Tn2LXiZTVK65kieHpk4bq"
    assert evidence.base_amount_base_units == 587410
    assert evidence.quote_amount_base_units == 199421
    assert evidence.user_quote_amount_base_units == 200020
    assert evidence.pool_base_reserves_base_units == 7204471983118
    assert evidence.pool_quote_reserves_base_units == 2428267459655
    assert evidence.virtual_quote_reserves_base_units == 17584505288
    assert evidence.encoded_event == payload


def test_recorded_event_fees_exactly_account_for_the_quote_haircut() -> None:
    """The three fee legs must reconcile the user-paid vs pool-received quote.

    This is the arithmetic the net-EV claim depends on: if any leg were dropped
    or double-counted the identity would break on real evidence.
    """

    evidence = _decode(_recorded_event()[0])
    assert not isinstance(evidence, AbstainResult)

    assert evidence.lp_fee_basis_points == 20
    assert evidence.lp_fee_base_units == 399
    assert evidence.protocol_fee_basis_points == 5
    assert evidence.protocol_fee_base_units == 100
    assert evidence.creator_fee_basis_points == 5
    assert evidence.creator_fee_base_units == 100

    haircut = evidence.user_quote_amount_base_units - evidence.quote_amount_base_units
    fees = (
        evidence.lp_fee_base_units
        + evidence.protocol_fee_base_units
        + evidence.creator_fee_base_units
    )
    assert haircut == 599
    assert fees == haircut


def test_decoder_fail_closes_when_layout_is_not_exactly_pinned() -> None:
    payload, _, _, _ = _recorded_event()

    result = _decode(payload[:-40])

    assert isinstance(result, AbstainResult)
    assert result.reason is AbstainReason.UNSUPPORTED_PROTOCOL_STATE
    assert result.message == "Pump AMM trade event layout is not exactly pinned"


def test_decoder_fail_closes_on_unsupported_discriminator() -> None:
    payload, _, _, _ = _recorded_event()

    result = _decode(b"\x00" * 8 + payload[8:])

    assert isinstance(result, AbstainResult)
    assert result.reason is AbstainReason.UNSUPPORTED_PROTOCOL_STATE
    assert result.message == "unsupported Pump AMM trade event discriminator"


def test_decoder_fail_closes_on_malformed_signature() -> None:
    payload, _, signature, _ = _recorded_event()

    result = _decode(payload, signature=signature[:-1])

    assert isinstance(result, AbstainResult)
    assert result.reason is AbstainReason.MISSING_FEATURE
    assert result.message == "finalized transaction signature is required"


def test_canonical_reader_latches_truncation_and_poisons_later_reads() -> None:
    reader = EventReader(b"\x01\x02\x03\x04")

    assert reader.remaining == 4
    reader.skip_u64(1)

    assert reader.error == "event payload is truncated"
    assert reader.read_u64() == 0
    assert reader.error == "event payload is truncated"


def test_canonical_reader_rejects_malformed_boolean_and_string() -> None:
    reader = EventReader(b"\x02")

    assert reader.read_bool() is False
    assert reader.error == "event boolean is malformed"

    string_reader = EventReader(b"\xff\xff\xff\xff")
    assert string_reader.read_string() == ""
    assert string_reader.error == "event payload is truncated"


def test_trade_event_reader_adds_only_shareholders_to_the_canonical_reader() -> None:
    """One Anchor cursor exists; the Pump variant only extends it."""

    assert _TradeEventReader.__bases__ == (EventReader,)

    own_methods = {
        name
        for name, value in vars(_TradeEventReader).items()
        if callable(value) and not name.startswith("__")
    }
    assert own_methods == {"read_shareholders"}


def test_shareholder_vector_fail_closes_when_count_exceeds_remaining_bytes() -> None:
    reader = _TradeEventReader(b"\xff\xff\xff\xff\x00\x00\x00\x00")

    assert reader.read_shareholders() == ()
    assert reader.error == "shareholder vector is truncated"
