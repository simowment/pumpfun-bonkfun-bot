"""Runtime entrypoint for the Rugbot Terminal User Interface (TUI)."""

from __future__ import annotations

import argparse
import contextlib
import sys
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence

from rugbot.runtime.app import build_ui_runtime
from rugbot.runtime.config import (
    ExecutionMode,
    SniperConfigError,
    load_provider_settings,
    load_sniper_config,
    resolve_config_path,
    resolve_dotenv,
    resolve_state_dir,
)
from rugbot.runtime.sniper_runtime import SniperRuntimeError, build_sniper_runtime
from rugbot.tui.app import RugbotTuiApp

__all__ = [
    "main",
    "parse_args",
]


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    """Parse command line arguments for the wallet TUI."""
    parser = argparse.ArgumentParser(
        prog="rug_wallet_tui",
        description="Deterministic on-chain funder tracker and wallet intelligence TUI.",
    )
    parser.add_argument(
        "--wallet", default=None, help="Target developer wallet address to track."
    )
    parser.add_argument(
        "--config", type=Path, default=Path("watch.yaml"), help="Path to config YAML."
    )
    parser.add_argument(
        "--state-dir", type=Path, default=Path(".state/watch"), help="State directory."
    )
    parser.add_argument("--max-transactions", type=int, default=100)
    parser.add_argument("--max-linked-wallets", type=int, default=8)
    parser.add_argument("--refresh-seconds", type=int, default=15)
    parser.add_argument("--as-of-slot", type=int, default=None)
    parser.add_argument(
        "--rpc-http",
        default=None,
        help="Override the read-only Solana HTTP RPC endpoint.",
    )
    parser.add_argument(
        "--rpc-wss",
        default=None,
        help="Override the native Solana WebSocket endpoint.",
    )
    parser.add_argument("--theme", default="textual-dark")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    """Launch the interactive TUI application."""
    if sys.platform == "win32":
        with contextlib.suppress(Exception):
            sys.stdout.reconfigure(encoding="utf-8")
            sys.stderr.reconfigure(encoding="utf-8")
    resolve_dotenv()
    args = parse_args(argv)
    config_path = resolve_config_path(args.config)
    state_dir = resolve_state_dir(args.state_dir)
    wallet = args.wallet
    try:
        config = load_sniper_config(config_path)
    except SniperConfigError as error:
        print(f"Invalid sniper config: {error}", file=sys.stderr)
        return 1
    if wallet is None:
        wallet = config.target.id

    try:
        providers = load_provider_settings()
    except SniperConfigError as error:
        print(f"Invalid provider config: {error}", file=sys.stderr)
        return 1
    endpoint = args.rpc_http or providers.rpc_http
    websocket_endpoint = args.rpc_wss or providers.rpc_websocket
    if endpoint is None:
        print("SOLANA_RPC_HTTP is required", file=sys.stderr)
        return 1

    if config.execution.mode is ExecutionMode.LIVE:
        resolve_dotenv(include_signing=True)
    try:
        sniper_runtime = build_sniper_runtime(
            config=config,
            endpoint=endpoint,
            state_dir=state_dir,
        )
    except (OSError, SniperRuntimeError, ValueError) as error:
        print(f"Sniper runtime unavailable: {error}", file=sys.stderr)
        return 1

    core = build_ui_runtime(
        state_dir=state_dir,
        wallet=wallet,
        config_path=config_path,
        sniper_runtime=sniper_runtime,
        endpoint=endpoint,
        fallback_endpoints=providers.rpc_http_fallbacks,
        websocket_endpoint=websocket_endpoint,
    )
    app = RugbotTuiApp(
        wallet,
        endpoint=endpoint,
        fallback_endpoints=providers.rpc_http_fallbacks,
        websocket_endpoint=websocket_endpoint,
        max_transactions=args.max_transactions,
        max_linked_wallets=args.max_linked_wallets,
        refresh_seconds=args.refresh_seconds,
        as_of_slot=args.as_of_slot,
        config_path=config_path,
        state_dir=state_dir,
        theme=args.theme,
        core=core,
    )
    app.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
