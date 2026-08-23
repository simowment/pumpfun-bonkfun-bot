"""Pump.fun protocol integration, bonding curve math, and real-time WebSocket feeds."""

# ruff: noqa: TC003

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import Callable
from typing import Any

import websockets
from sol_trade_sdk.pump import (
    PUMP_FEE_RECIPIENT,
    PUMP_PROGRAM_ID,
    derive_bonding_curve_pda,
)

from rugbot.ingest.pump.models import TokenLaunch

PUMP_DEFAULT_FEE_BPS = 100


class PumpPortalStream:
    """Stream real-time Pump.fun token creation and trade events via PumpPortal WebSocket."""

    def __init__(self, ws_url: str = "wss://pumpportal.fun/api/data") -> None:
        self._ws_url = ws_url

    async def listen_new_tokens(
        self, callback: Callable[[dict[str, Any]], None]
    ) -> None:
        """Continuously subscribe and stream new token launch events."""
        while True:
            try:
                async with websockets.connect(
                    self._ws_url, ping_interval=20, ping_timeout=20
                ) as ws:
                    await ws.send(json.dumps({"method": "subscribeNewToken"}))
                    async for message in ws:
                        with contextlib.suppress(Exception):
                            payload = json.loads(message)
                            if isinstance(payload, dict) and (
                                payload.get("txType") == "create" or "mint" in payload
                            ):
                                callback(payload)
            except (OSError, websockets.WebSocketException):
                await asyncio.sleep(2.0)


__all__ = [
    "PUMP_DEFAULT_FEE_BPS",
    "PUMP_FEE_RECIPIENT",
    "PUMP_PROGRAM_ID",
    "PumpPortalStream",
    "TokenLaunch",
    "derive_bonding_curve_pda",
]
