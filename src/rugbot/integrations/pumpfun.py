"""Pump.fun protocol integration, bonding curve math, and real-time WebSocket feeds."""

# ruff: noqa: TC003, C901

from __future__ import annotations

import asyncio
import contextlib
import json
import os
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

    def __init__(
        self,
        ws_url: str = "wss://pumpportal.fun/api/data",
        api_key: str | None = None,
    ) -> None:
        key = (
            api_key
            if api_key is not None
            else os.environ.get("PUMPPORTAL_API_KEY", "").strip()
        )
        if key and "api-key=" not in ws_url:
            separator = "&" if "?" in ws_url else "?"
            self._ws_url = f"{ws_url}{separator}api-key={key}"
        else:
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

    async def listen_account_trades(
        self,
        wallets: list[str],
        callback: Callable[[dict[str, Any]], Any],
        *,
        on_status: Callable[[str, str], Any] | None = None,
        stop_event: asyncio.Event | None = None,
    ) -> None:
        """Continuously subscribe and stream real-time trade events for tracked wallets."""
        if not wallets:
            return
        while stop_event is None or not stop_event.is_set():
            try:
                async with websockets.connect(
                    self._ws_url, ping_interval=20, ping_timeout=20
                ) as ws:
                    await ws.send(
                        json.dumps(
                            {
                                "method": "subscribeAccountTrade",
                                "keys": list(wallets),
                            }
                        )
                    )
                    while stop_event is None or not stop_event.is_set():
                        try:
                            message = await asyncio.wait_for(ws.recv(), timeout=2.0)
                        except TimeoutError:
                            continue
                        with contextlib.suppress(Exception):
                            payload = json.loads(message)
                            if not isinstance(payload, dict):
                                continue
                            if "txType" in payload:
                                res = callback(payload)
                                if asyncio.iscoroutine(res):
                                    await res
                            elif "errors" in payload or "message" in payload:
                                text = str(
                                    payload.get("errors")
                                    or payload.get("message")
                                    or ""
                                )
                                if on_status:
                                    status_res = on_status("warning", text)
                                    if asyncio.iscoroutine(status_res):
                                        await status_res
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
