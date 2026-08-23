"""Finalized transaction landing observation."""

# This module is an intentionally small RPC boundary with explicit error
# messages and positional immutable result construction.
# ruff: noqa: TRY003, FBT003, TC001, TC003

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from rugbot.integrations.solana_rpc import SolanaClient


@dataclass(frozen=True, slots=True)
class FinalizedLanding:
    """Finalized status for one submitted signature."""

    signature: str
    finalized: bool
    slot: int | None
    err: object | None
    transaction_found: bool


class LandingObservationError(ValueError):
    """Raised when finalized RPC evidence is malformed."""


async def observe_finalized_signature(
    client: SolanaClient,
    signature: str,
) -> FinalizedLanding:
    """Observe one signature through finalized commitment."""

    results = await observe_finalized_signatures(client, (signature,))
    return results[0]


async def wait_for_finalized_signatures(
    client: SolanaClient,
    signatures: Sequence[str],
    *,
    poll_interval_seconds: float = 0.4,
    max_polls: int = 25,
) -> tuple[FinalizedLanding, ...]:
    """Poll finalized status until a winner is found or the budget expires."""

    if poll_interval_seconds <= 0 or max_polls < 1:
        raise LandingObservationError("landing poll configuration is invalid")
    latest = await observe_finalized_signatures(client, signatures)
    for _ in range(max_polls):
        if any(item.finalized and item.err is None for item in latest):
            return latest
        if all(item.finalized for item in latest):
            return latest
        await asyncio.sleep(poll_interval_seconds)
        latest = await observe_finalized_signatures(client, signatures)
    return latest


async def observe_finalized_signatures(
    client: SolanaClient,
    signatures: Sequence[str],
) -> tuple[FinalizedLanding, ...]:
    """Observe all submitted variants using ``getSignatureStatuses``."""

    if not signatures or any(
        type(signature) is not str or not signature for signature in signatures
    ):
        raise LandingObservationError("at least one non-empty signature is required")
    response = await client.post_rpc(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "getSignatureStatuses",
            "params": [list(signatures), {"searchTransactionHistory": True}],
        }
    )
    raw_values = _status_values(response, len(signatures))
    observed: list[FinalizedLanding] = []
    for signature, raw_status in zip(signatures, raw_values, strict=True):
        if raw_status is None:
            observed.append(FinalizedLanding(signature, False, None, None, False))
            continue
        if not isinstance(raw_status, dict):
            raise LandingObservationError("signature status entry is malformed")
        confirmation = raw_status.get("confirmationStatus")
        slot = raw_status.get("slot")
        slot_value = slot if type(slot) is int and slot >= 0 else None
        observed.append(
            FinalizedLanding(
                signature=signature,
                finalized=confirmation == "finalized" and slot_value is not None,
                slot=slot_value,
                err=raw_status.get("err"),
                transaction_found=True,
            )
        )
    return tuple(observed)


def _status_values(
    response: dict[str, Any] | None,
    expected_count: int,
) -> list[object | None]:
    if not isinstance(response, dict) or response.get("error") is not None:
        raise LandingObservationError("signature status RPC response is malformed")
    result = response.get("result")
    if not isinstance(result, dict) or not isinstance(result.get("value"), list):
        raise LandingObservationError("signature status result is malformed")
    values = result["value"]
    if len(values) != expected_count:
        raise LandingObservationError("signature status count does not match request")
    return values


__all__ = [
    "FinalizedLanding",
    "LandingObservationError",
    "observe_finalized_signature",
    "observe_finalized_signatures",
    "wait_for_finalized_signatures",
]
