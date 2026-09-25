"""Manage the observe-only copytrade wallet registry."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import TYPE_CHECKING

from rugbot.analysis.wallet_registry import (
    RegistryWallet,
    WalletRegistry,
    WalletRegistryError,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

DEFAULT_STORE_SUBPATH = Path(".state/copytrade/registry.sqlite3")


def build_parser() -> argparse.ArgumentParser:
    """Build the registry argument parser."""
    parser = argparse.ArgumentParser(
        description="Manage copytrade watched wallets (observe-only).",
    )
    parser.add_argument(
        "command", choices=["add", "list", "enable", "disable", "remove"]
    )
    parser.add_argument("wallet", nargs="?")
    parser.add_argument("--store", type=str, default=str(DEFAULT_STORE_SUBPATH))
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--quote-sol", type=float, default=None)
    parser.add_argument("--tp-pct", type=float, default=None)
    parser.add_argument("--sl-pct", type=float, default=None)
    parser.add_argument("--max-open", type=int, default=None)
    parser.add_argument("--no-mirror-sells", action="store_true")
    parser.add_argument("--note", type=str, default=None)
    return parser


def wallet_to_json(item: RegistryWallet) -> dict[str, object]:
    """Serialize one registry wallet for machine output."""
    return {
        "wallet": item.wallet,
        "enabled": item.enabled,
        "quote_sol": item.quote_sol,
        "tp_pct": item.tp_pct,
        "sl_pct": item.sl_pct,
        "mirror_sells": item.mirror_sells,
        "max_open": item.max_open,
        "note": item.note,
        "added_at": item.added_at,
    }


def main(argv: Sequence[str] | None = None) -> int:  # noqa: C901, PLR0911, PLR0912
    """Run the registry command (returns process exit code)."""
    args = build_parser().parse_args(argv)
    registry = WalletRegistry(args.store)
    try:
        if args.command == "add":
            if not args.wallet:
                print("Error: add requires a wallet address", file=sys.stderr)
                return 2
            try:
                stored = registry.add(
                    args.wallet,
                    quote_sol=args.quote_sol,
                    tp_pct=args.tp_pct,
                    sl_pct=args.sl_pct,
                    mirror_sells=not args.no_mirror_sells,
                    max_open=args.max_open,
                    note=args.note,
                )
            except WalletRegistryError as exc:
                print(f"Error: {exc}", file=sys.stderr)
                return 2
            if args.json:
                print(json.dumps(wallet_to_json(stored)))
            else:
                print(f"added {stored.wallet}")
            return 0
        if args.command == "list":
            wallets = registry.list()
            if args.json:
                for item in wallets:
                    print(json.dumps(wallet_to_json(item)))
            elif not wallets:
                print("no wallets registered")
            else:
                for item in wallets:
                    state = "enabled" if item.enabled else "disabled"
                    print(f"{item.wallet}  {state}")
            return 0
        if not args.wallet:
            print(f"Error: {args.command} requires a wallet address", file=sys.stderr)
            return 2
        if args.command == "enable":
            updated = _set_enabled(registry, args.wallet, enabled=True)
        elif args.command == "disable":
            updated = _set_enabled(registry, args.wallet, enabled=False)
        else:
            try:
                updated = registry.remove(args.wallet)
            except WalletRegistryError as exc:
                print(f"Error: {exc}", file=sys.stderr)
                return 2
            if not updated:
                print(f"Error: unknown wallet {args.wallet}", file=sys.stderr)
                return 1
            print(f"removed {args.wallet.strip()}")
            return 0
        if not updated:
            print(f"Error: unknown wallet {args.wallet}", file=sys.stderr)
            return 1
        print(f"{args.command}d {args.wallet.strip()}")
        return 0
    finally:
        registry.close()


def _set_enabled(registry: WalletRegistry, wallet: str, *, enabled: bool) -> bool:
    """Enable or disable one wallet, reporting invalid input as failure."""
    try:
        return registry.set_enabled(wallet, enabled)
    except WalletRegistryError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return False
