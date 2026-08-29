"""Minimal 10s GLOBAL new pairs feed via PumpPortal WS (no RPC, no DB)."""

from __future__ import annotations

import asyncio
import json
import time

import websockets

WS_URL = "wss://pumpportal.fun/api/data"


async def main() -> None:
    end = time.monotonic() + 10
    async with websockets.connect(WS_URL) as ws:
        await ws.send(json.dumps({"method": "subscribeNewToken"}))
        while time.monotonic() < end:
            remaining = end - time.monotonic()
            try:
                msg = await asyncio.wait_for(ws.recv(), timeout=max(0.1, remaining))
            except TimeoutError:
                break
            try:
                p = json.loads(msg)
            except json.JSONDecodeError:
                continue
            if not isinstance(p, dict) or p.get("txType") != "create":
                continue
            out = {
                "mint": p.get("mint"),
                "symbol": p.get("symbol"),
                "creator": p.get("traderPublicKey"),
                "slot": p.get("slot"),
            }
            print(json.dumps(out))


if __name__ == "__main__":
    asyncio.run(main())
