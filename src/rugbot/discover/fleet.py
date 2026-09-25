"""Reconstruct an operator from one launch's bundle wallets (its "fleet").

A coordinated launch is bundled by wallets that belong to the operator. Those
wallets reappear in the operator's other launches, bought in the first slots.
This module identifies the bundle wallets of a launch and lists the Pump coins
a wallet recently bought, so the caller can keep the launches where fleet
wallets were early buyers: the operator's launch history.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from rugbot.backtest.launch_replay import nonstandard_curve_reason
from rugbot.domain.decisions import AbstainResult
from rugbot.ingest.pump.create_decoder import PUMP_PROGRAM_ID
from rugbot.ingest.pump.create_event_decoder import decode_pump_create_event_logs
from rugbot.tracker.funding_chain import (
    parsed_transaction,
    signature_page,
    wallet_birth,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

    from rugbot.backtest.launch_replay import LaunchTrade

PUMP_BUY_LOG = "Program log: Instruction: Buy"
PUMP_MINT_SUFFIX = "pump"


def bundle_wallets(
    trades: Sequence[LaunchTrade],
    *,
    creator: str,
    max_delay_slots: int,
    min_sol: float,
) -> list[str]:
    """Non-dev wallets that bought at least ``min_sol`` within the first slots.

    Returned largest buyer first.
    """
    if not trades:
        return []
    create_slot = trades[0].slot
    spent: dict[str, float] = {}
    for trade in trades:
        if trade.slot > create_slot + max_delay_slots:
            break
        if trade.is_buy and trade.wallet != creator:
            spent[trade.wallet] = spent.get(trade.wallet, 0.0) + trade.amount_sol
    return sorted(
        (wallet for wallet, sol in spent.items() if sol >= min_sol),
        key=lambda wallet: spent[wallet],
        reverse=True,
    )


def recent_pump_buys(wallet: str, *, max_transactions: int) -> list[str]:
    """Pump mints ``wallet`` bought in its latest transactions, newest first."""
    page = signature_page(wallet, endpoints=None, transport=None) or []
    mints: list[str] = []
    for entry in page[:max_transactions]:
        signature = entry.get("signature")
        if not isinstance(signature, str) or entry.get("err") is not None:
            continue
        result = parsed_transaction(signature, endpoints=None, transport=None)
        if not isinstance(result, dict):
            continue
        meta = result.get("meta")
        logs = meta.get("logMessages") if isinstance(meta, dict) else None
        if not isinstance(logs, list) or PUMP_BUY_LOG not in logs:
            continue
        message = result.get("transaction", {}).get("message", {})
        keys = [
            key.get("pubkey") if isinstance(key, dict) else key
            for key in message.get("accountKeys", [])
        ]
        if PUMP_PROGRAM_ID not in keys:
            continue
        for balance in meta.get("postTokenBalances") or []:
            mint = balance.get("mint")
            if (
                balance.get("owner") == wallet
                and isinstance(mint, str)
                and mint.endswith(PUMP_MINT_SUFFIX)
                and mint not in mints
            ):
                mints.append(mint)
    return mints


def create_facts(mint: str) -> tuple[str | None, str | None]:
    """Return ``(creator, skip_reason)`` from a launch's on-chain CreateEvent.

    The create transaction is the mint account's first transaction (the dev
    does not always buy in it, so the first trade is not reliable).
    ``skip_reason`` is set when the launch cannot be replayed on the standard
    curve (Mayhem, non-standard reserves) or the create event is missing.
    """
    create_signature = wallet_birth(mint).first_signature
    if create_signature is None:
        return None, "create transaction not reachable"
    result = parsed_transaction(create_signature, endpoints=None, transport=None)
    meta = result.get("meta") if isinstance(result, dict) else None
    logs = meta.get("logMessages") if isinstance(meta, dict) else None
    slot = result.get("slot") if isinstance(result, dict) else None
    if not isinstance(logs, list) or not isinstance(slot, int):
        return None, "create transaction unavailable"
    event = decode_pump_create_event_logs(logs, as_of_slot=slot)
    if event is None or isinstance(event, AbstainResult):
        return None, "create event not found"
    return event.creator_pubkey, nonstandard_curve_reason(
        event.virtual_sol_reserves * event.virtual_token_reserves,
        mayhem=event.is_mayhem_mode,
    )
