"""Observe watched-wallet trades and measure real detection lag (observe-only).

Builds a WebSocket-first ``WalletTradeSource`` over the registry's enabled
wallets and prints each detected target trade as it arrives. This command
never places orders and imports no execution machinery.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import urlsplit, urlunsplit

from rugbot.analysis.wallet_registry import WalletRegistry
from rugbot.domain.decisions import AbstainResult
from rugbot.ingest.pump.pump_trade_observation import decode_pump_trade_observation
from rugbot.runtime.config import load_provider_settings, resolve_dotenv
from rugbot.runtime.workers.observation_loop import RpcAddressObservationSource
from rugbot.runtime.workers.wallet_trade_source import (
    VALID_COMMITMENTS,
    WalletTradeSource,
    observation_signature_str,
)
from rugbot.utils.logger import get_logger

if TYPE_CHECKING:
    from collections.abc import Sequence

    from rugbot.domain.observations import RawChainObservation

logger = get_logger(__name__)

OBSERVE_ONLY_MARKER = "observe-only: no orders, no execution ports"
DEFAULT_STORE_SUBPATH = Path(".state/copytrade/registry.sqlite3")
DEFAULT_SECONDS = 60
MAX_SECONDS = 300
LAMPORTS_PER_SOL = 1_000_000_000


@dataclass(frozen=True, slots=True)
class DetectedTrade:
    """One observed target-wallet trade with its detection measurement."""

    wallet: str
    signature: str
    slot: int
    transport: str
    lag_ms: int | None
    mint: str | None
    direction: str | None
    sol_amount: float | None


def build_parser() -> argparse.ArgumentParser:
    """Build the copytrade observer argument parser."""
    parser = argparse.ArgumentParser(
        description="Observe registry wallets and measure detection lag.",
    )
    parser.add_argument("--observe", action="store_true")
    parser.add_argument("--registry", type=str, default=str(DEFAULT_STORE_SUBPATH))
    parser.add_argument("--seconds", type=int, default=DEFAULT_SECONDS)
    parser.add_argument("--commitment", type=str, default="processed")
    parser.add_argument("--json", action="store_true")
    return parser


def derive_websocket_endpoint(http_endpoint: str | None) -> str | None:
    """Derive a WSS endpoint from the HTTP endpoint when none is configured."""
    if not http_endpoint:
        return None
    parsed = urlsplit(http_endpoint)
    scheme = "wss" if parsed.scheme == "https" else "ws"
    return urlunsplit(
        (scheme, parsed.netloc, parsed.path, parsed.query, parsed.fragment)
    )


def describe_trade(
    observation: RawChainObservation,
) -> tuple[str | None, str | None, float | None]:
    """Best-effort mint, direction, and SOL amount from a hydrated trade.

    Args:
        observation: Finalized observation that may carry a Pump trade.

    Returns:
        ``(mint, direction, sol_amount)`` with ``None`` fields when the
        transaction does not decode as a Pump trade.
    """
    try:
        decoded = decode_pump_trade_observation(observation)
    except Exception:  # noqa: BLE001 - best-effort display only
        return None, None, None
    if isinstance(decoded, AbstainResult) or not decoded:
        return None, None, None
    evidence = decoded[0]
    mint: str | None = None
    if evidence.account_pubkeys is not None and 0 <= evidence.mint_account_index < len(
        evidence.account_pubkeys
    ):
        mint = evidence.account_pubkeys[evidence.mint_account_index]
    direction = evidence.side.value
    lamports = evidence.quote_amount_base_units
    if lamports is None:
        if direction == "buy":
            lamports = evidence.max_quote_cost_base_units
        else:
            lamports = evidence.min_quote_output_base_units
    sol_amount = (
        float(int(lamports)) / LAMPORTS_PER_SOL if lamports is not None else None
    )
    return mint, direction, sol_amount


def trade_to_json(trade: DetectedTrade) -> dict[str, object]:
    """Serialize one detected trade for machine output."""
    return {
        "wallet": trade.wallet,
        "signature": trade.signature,
        "slot": trade.slot,
        "transport": trade.transport,
        "detection_lag_ms": trade.lag_ms,
        "mint": trade.mint,
        "direction": trade.direction,
        "sol_amount": trade.sol_amount,
        "observe_only": True,
    }


def print_trade(trade: DetectedTrade, *, as_json: bool) -> None:
    """Print one detected trade incrementally (JSON line or human line)."""
    if as_json:
        print(json.dumps(trade_to_json(trade)), flush=True)
        return
    lag = f"{trade.lag_ms}ms" if trade.lag_ms is not None else "lag unknown"
    detail = ""
    if trade.mint is not None or trade.direction is not None:
        amount = f"{trade.sol_amount:.4f} SOL" if trade.sol_amount is not None else "?"
        detail = f" {trade.direction or '?'} {trade.mint or '?'} {amount}"
    print(
        f"[{trade.transport}] {trade.wallet[:8]}... {trade.signature[:16]}... "
        f"slot={trade.slot} {lag}{detail}",
        flush=True,
    )


def print_summary(trades: Sequence[DetectedTrade], *, as_json: bool) -> None:
    """Print the closing lag distribution over all detected trades."""
    lags = sorted(trade.lag_ms for trade in trades if trade.lag_ms is not None)
    transports = sorted({trade.transport for trade in trades})
    if as_json:
        print(
            json.dumps(
                {
                    "type": "summary",
                    "trades": len(trades),
                    "transports": transports,
                    "lag_ms": _lag_stats(lags),
                    "observe_only": True,
                }
            ),
            flush=True,
        )
        return
    print(f"detected {len(trades)} trade(s) via {transports or 'no transport'}")
    if lags:
        stats = _lag_stats(lags)
        print(f"lag ms: min={stats['min']} median={stats['median']} max={stats['max']}")
    else:
        print("lag ms: no measurable samples")


def _lag_stats(lags: Sequence[int]) -> dict[str, int | None]:
    """Return min/median/max over lag samples."""
    if not lags:
        return {"min": None, "median": None, "max": None}
    ordered = sorted(lags)
    return {
        "min": ordered[0],
        "median": ordered[len(ordered) // 2],
        "max": ordered[-1],
    }


async def observe(
    source: WalletTradeSource,
    *,
    seconds: int,
    as_json: bool,
) -> list[DetectedTrade]:
    """Run the source until the deadline, printing each detection as it lands."""
    deadline = time.monotonic() + seconds
    trades: list[DetectedTrade] = []
    while time.monotonic() < deadline:
        result = await source.read()
        if isinstance(result, AbstainResult):
            continue
        for observation in result:
            trade = _trade_from_observation(source, observation)
            if trade is None:
                continue
            trades.append(trade)
            print_trade(trade, as_json=as_json)
    return trades


def _trade_from_observation(
    source: WalletTradeSource, observation: RawChainObservation
) -> DetectedTrade | None:
    """Build a display trade from one observation plus source side-channels."""
    signature = observation_signature_str(observation)
    if signature is None:
        return None
    mint, direction, sol_amount = describe_trade(observation)
    wallet = source.last_wallet or ""
    return DetectedTrade(
        wallet=wallet,
        signature=signature,
        slot=observation.slot,
        transport=source.last_transport or "unknown",
        lag_ms=source.last_detection_lag_ms,
        mint=mint,
        direction=direction,
        sol_amount=sol_amount,
    )


def main(argv: Sequence[str] | None = None) -> int:
    """Run the observe-only copytrade watcher (returns process exit code)."""
    args = build_parser().parse_args(argv)
    if not args.observe:
        print(
            "Error: --observe is required (no live order path exists)",
            file=sys.stderr,
        )
        return 2
    if args.commitment not in VALID_COMMITMENTS:
        print(
            f"Error: --commitment must be one of {sorted(VALID_COMMITMENTS)}",
            file=sys.stderr,
        )
        return 2
    if args.seconds < 1 or args.seconds > MAX_SECONDS:
        print(
            f"Error: --seconds must be between 1 and {MAX_SECONDS}",
            file=sys.stderr,
        )
        return 2
    resolve_dotenv()
    providers = load_provider_settings()
    if not providers.rpc_http:
        print("Error: SOLANA_RPC_HTTP is required", file=sys.stderr)
        return 2
    registry = WalletRegistry(args.registry)
    try:
        wallets = [item.wallet for item in registry.list(enabled_only=True)]
    finally:
        registry.close()
    if not wallets:
        print("Error: no enabled wallets in the registry", file=sys.stderr)
        return 2
    websocket_endpoint = providers.rpc_websocket or derive_websocket_endpoint(
        providers.rpc_http
    )
    poll_sources = {
        wallet: RpcAddressObservationSource(
            address=wallet,
            endpoint=providers.rpc_http,
            max_signatures=5,
            max_transactions=3,
            max_pages=2,
        )
        for wallet in wallets
    }
    source = WalletTradeSource(
        wallets,
        endpoint=providers.rpc_http,
        websocket_endpoint=websocket_endpoint,
        commitment=args.commitment,
        poll_source=poll_sources[wallets[0]],
        poll_source_factory=poll_sources.__getitem__,
    )
    print(f"[{OBSERVE_ONLY_MARKER}]")
    if websocket_endpoint is None:
        print("no WebSocket endpoint: poll-only fallback in use")
    else:
        print(
            f"watching {len(wallets)} wallet(s) "
            f"ws={args.commitment} fallback=finalized poll"
        )
    trades = asyncio.run(observe(source, seconds=args.seconds, as_json=args.json))
    print_summary(trades, as_json=args.json)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
