"""Unit tests for the Nansen indexed client (no network)."""

import json
import urllib.error
from typing import Any

import pytest

from rugbot.integrations.nansen_client import (
    NansenClient,
    NansenCounterparty,
    NansenProviderError,
    counterparties_to_json,
)

WALLET = "HX2Sr1gKJC53NKEBPEy9KRoW2M1C6NoJT241sVM4AEnA"
OTHER = "7MEy3ii4dYvzG4PWecDUxUc9xoJahZrX6PZGDjiwNhFx"


def _row(wallet: str, **overrides: Any) -> dict[str, Any]:
    """Build one canned counterparty row."""
    row: dict[str, Any] = {
        "counterparty_address": wallet,
        "counterparty_address_label": ["FIGGER Token Deployer"],
        "interaction_count": 1,
        "total_volume_usd": 1683.07,
        "volume_in_usd": 0.0,
        "volume_out_usd": 1683.07,
    }
    row.update(overrides)
    return row


def _client_for(payload: object, calls: list) -> NansenClient:
    """Build a client serving one canned JSON payload."""

    def _fake_transport(request: Any, timeout: int) -> bytes:
        """Record the call and serve canned bytes."""
        calls.append(request.full_url)
        return json.dumps(payload).encode()

    return NansenClient("test-key", transport=_fake_transport)


def test_counterparties_returns_validated_page() -> None:
    """Two rows validate, null labels become empty, pagination passes through."""
    calls: list[str] = []
    client = _client_for(
        {
            "data": [_row(OTHER), _row(WALLET, counterparty_address_label=None)],
            "pagination": {"page": 1, "per_page": 10, "is_last_page": False},
        },
        calls,
    )
    page = client.counterparties(WALLET, date_from="2026-09-01", date_to="2026-09-05")
    assert len(page.counterparties) == 2
    assert page.counterparties[0].wallet == OTHER
    assert page.counterparties[0].labels == ("FIGGER Token Deployer",)
    assert page.counterparties[1].labels == ()
    assert page.is_last_page is False
    assert len(calls) == 1


def test_malformed_row_raises() -> None:
    """A row with a bad wallet fails closed instead of half-parsing."""
    calls: list[str] = []
    client = _client_for({"data": [_row("not-an-address")], "pagination": {}}, calls)
    with pytest.raises(NansenProviderError):
        client.counterparties(WALLET, date_from="2026-09-01", date_to="2026-09-05")


def test_http_429_surfaces_status_for_fail_fast() -> None:
    """Rate limiting raises with the status in the message for callers."""

    def _limited(request: Any, timeout: int) -> bytes:
        """Simulate a throttled provider."""
        raise urllib.error.HTTPError(
            request.full_url,
            429,
            "throttled",
            None,
            None,  # type: ignore[arg-type]
        )

    client = NansenClient("test-key", transport=_limited)
    with pytest.raises(NansenProviderError, match="429"):
        client.counterparties(WALLET, date_from="2026-09-01", date_to="2026-09-05")


def test_empty_data_returns_empty_page() -> None:
    """A fresh wallet yields an empty page, not an error."""
    calls: list[str] = []
    client = _client_for({"data": [], "pagination": {"is_last_page": True}}, calls)
    page = client.counterparties(WALLET, date_from="2026-09-01", date_to="2026-09-05")
    assert page.counterparties == ()
    assert page.is_last_page is True


def test_empty_key_and_bad_dates_rejected() -> None:
    """Constructor and date validation fail fast on garbage input."""
    with pytest.raises(ValueError):
        NansenClient("  ")
    calls: list[str] = []
    client = _client_for({"data": []}, calls)
    with pytest.raises(ValueError):
        client.counterparties(WALLET, date_from="09/01/2026", date_to="2026-09-05")
    with pytest.raises(ValueError):
        client.counterparties(
            WALLET, date_from="2026-09-01", date_to="2026-09-05", page=0
        )
    assert calls == []


def test_counterparties_to_json_serializes_rows() -> None:
    """Rows serialize with labels, flows, and provenance intact."""
    rows = (
        NansenCounterparty(
            wallet=OTHER,
            labels=("FIGGER Token Deployer",),
            interaction_count=2,
            total_volume_usd=10.0,
            volume_in_usd=4.0,
            volume_out_usd=6.0,
        ),
    )
    payload = counterparties_to_json(rows)
    assert payload[0]["wallet"] == OTHER
    assert payload[0]["labels"] == ["FIGGER Token Deployer"]
    assert payload[0]["source"] == "nansen"
