"""CLI for bounded finalized wallet intelligence reports."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from typing import TYPE_CHECKING

from rugbot.runtime.wallet_intelligence import (
    WalletIntelligenceReport,
    abstention_to_json,
    report_to_json,
    scan_wallet_intelligence,
)

if TYPE_CHECKING:
    from collections.abc import Sequence


def build_arg_parser() -> argparse.ArgumentParser:
    """Build the wallet intelligence command parser."""

    parser = argparse.ArgumentParser(
        description="Inspect a Solana wallet history and direct-link graph."
    )
    parser.add_argument("--wallet", required=True)
    parser.add_argument("--max-transactions", type=int, default=50)
    parser.add_argument("--max-history-pages", type=int, default=10)
    parser.add_argument("--max-linked-wallets", type=int, default=8)
    parser.add_argument("--as-of-slot", type=int)
    parser.add_argument("--pretty", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run one read-only wallet intelligence scan."""

    args = build_arg_parser().parse_args(argv)
    endpoint = os.environ.get("SOLANA_RPC_HTTP") or os.environ.get(
        "SOLANA_NODE_RPC_ENDPOINT"
    )
    if not endpoint:
        payload: dict[str, object] = {
            "status": "abstain",
            "reason": "missing_feature",
            "message": "SOLANA_RPC_HTTP or SOLANA_NODE_RPC_ENDPOINT is required",
            "as_of_slot": -1,
        }
        _print(payload, pretty=args.pretty)
        return 1

    result = asyncio.run(
        scan_wallet_intelligence(
            args.wallet,
            endpoint=endpoint,
            max_transactions=args.max_transactions,
            max_history_pages=args.max_history_pages,
            max_linked_wallets=args.max_linked_wallets,
            as_of_slot=args.as_of_slot,
        )
    )
    payload = (
        report_to_json(result)
        if isinstance(result, WalletIntelligenceReport)
        else abstention_to_json(result)
    )
    _print(payload, pretty=args.pretty)
    return 0 if isinstance(result, WalletIntelligenceReport) else 1


def _print(payload: dict[str, object], *, pretty: bool) -> None:
    print(json.dumps(payload, indent=2 if pretty else None, sort_keys=True))


if __name__ == "__main__":
    raise SystemExit(main())
