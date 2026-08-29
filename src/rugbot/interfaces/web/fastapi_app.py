"""FastAPI surface for the Svelte wallet and entity tracker."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
from typing import TYPE_CHECKING

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from rugbot.interfaces.web.adapter import JsonValue, jsonable
from rugbot.utils.logger import get_logger

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from rugbot.runtime.app import RugbotApp
    from rugbot.tracker.events import TrackerEvent

logger = get_logger(__name__)
SCAN_TIMEOUT_SECONDS = 120
ENTITY_QUERY_MIN_LENGTH = 32
ENTITY_QUERY_MAX_LENGTH = 64
SCAN_HISTORY_MAX_LIMIT = 1000


class ScanEntityRequest(BaseModel):
    """Validated request for a finalized wallet or mint scan."""

    query: str = Field(
        min_length=ENTITY_QUERY_MIN_LENGTH,
        max_length=ENTITY_QUERY_MAX_LENGTH,
    )
    max_transactions: int = Field(default=100, ge=1, le=1000)


class TrackEntityRequest(BaseModel):
    """Validated request to enroll a qualified tracking address."""

    address: str = Field(min_length=32, max_length=64)
    label: str = Field(default="Tracked entity", max_length=120)


def create_fastapi_app(  # noqa: C901, PLR0915
    core: RugbotApp, dist_dir: Path | None = None
) -> FastAPI:
    """Create the API and serve the compiled Svelte application when present."""

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        await core.start()
        try:
            yield
        finally:
            await core.close()

    app = FastAPI(title="Rugbot entity tracker", lifespan=lifespan)
    resolved_addresses: set[str] = set()

    def state_payload() -> dict[str, JsonValue]:
        """Project only persisted or currently observed engine state."""
        return {
            "target_history": jsonable(core.target_scans()),
            "launches": jsonable(core.launches()),
            "observation": {
                "status": core.observation_status,
                "addresses": list(core.observed_addresses),
            },
        }

    @app.get("/api/health")
    async def api_health() -> dict[str, str]:
        return {
            "status": "ok",
            "service": "rugbot-fastapi",
            "observation": core.observation_status,
        }

    @app.get("/api/state")
    async def api_state() -> dict[str, object]:
        return state_payload()

    @app.get("/api/entity/{address}/scans")
    async def api_entity_scan_history(
        address: str, limit: int = 100
    ) -> dict[str, object]:
        normalized_address = address.strip()
        if (
            not ENTITY_QUERY_MIN_LENGTH
            <= len(normalized_address)
            <= ENTITY_QUERY_MAX_LENGTH
        ):
            raise HTTPException(status_code=422, detail="invalid entity address length")
        if not 1 <= limit <= SCAN_HISTORY_MAX_LIMIT:
            raise HTTPException(status_code=422, detail="invalid scan history limit")
        return {
            "ok": True,
            "entity_address": normalized_address,
            "scans": jsonable(
                core.target_scan_history(normalized_address, limit=limit)
            ),
        }

    @app.post("/api/entity/scan")
    async def api_entity_scan(payload: ScanEntityRequest) -> dict[str, object]:
        query = payload.query.strip()
        try:
            async with asyncio.timeout(SCAN_TIMEOUT_SECONDS):
                result = await core.analyze_wallet(
                    query,
                    max_transactions=payload.max_transactions,
                )
        except TimeoutError:
            return {
                "ok": False,
                "message": f"finalized scan exceeded {SCAN_TIMEOUT_SECONDS} seconds",
                "data": None,
            }

        data = jsonable(result.data)
        if result.ok and isinstance(result.data, dict):
            address = result.data.get("tracking_address")
            if isinstance(address, str):
                resolved_addresses.add(address)
        return {"ok": result.ok, "message": result.message, "data": data}

    @app.get("/api/entity/cache")
    async def api_entity_cache(query: str) -> dict[str, object]:
        normalized_query = query.strip()
        if (
            not ENTITY_QUERY_MIN_LENGTH
            <= len(normalized_query)
            <= ENTITY_QUERY_MAX_LENGTH
        ):
            raise HTTPException(status_code=422, detail="invalid entity query length")
        result = core.cached_entity_report(normalized_query)
        if not result.ok:
            raise HTTPException(status_code=404, detail=result.message)
        return {
            "ok": True,
            "message": result.message,
            "data": jsonable(result.data),
        }

    @app.get("/api/wallet/balance")
    async def api_wallet_balance(address: str) -> dict[str, object]:
        normalized_address = address.strip()
        if (
            not ENTITY_QUERY_MIN_LENGTH
            <= len(normalized_address)
            <= ENTITY_QUERY_MAX_LENGTH
        ):
            raise HTTPException(status_code=422, detail="invalid wallet address length")
        result = await core.wallet_balance(normalized_address)
        if not result.ok:
            raise HTTPException(status_code=404, detail=result.message)
        return {
            "ok": True,
            "message": result.message,
            "data": jsonable(result.data),
        }

    @app.post("/api/entity/track")
    async def api_entity_track(payload: TrackEntityRequest) -> dict[str, object]:
        address = payload.address.strip()
        if address not in resolved_addresses:
            raise HTTPException(
                status_code=409,
                detail="Address must be resolved before tracking",
            )
        if core.get_funder(address) is None:
            result = core.watch(address, label=payload.label.strip())
            if not result.ok:
                raise HTTPException(status_code=409, detail=result.message)
        await core.refresh_observation()
        return {
            "ok": True,
            "message": f"Watching {address} in observe-only mode",
            "state": state_payload(),
        }

    @app.post("/api/entity/backtest")
    async def api_entity_backtest() -> None:
        raise HTTPException(
            status_code=409,
            detail=(
                "No finalized launch-outcome dataset is available for this entity; "
                "a backtest would be fabricated"
            ),
        )

    @app.websocket("/api/events")
    async def ws_events(websocket: WebSocket) -> None:
        await websocket.accept()
        queue: asyncio.Queue[TrackerEvent] = asyncio.Queue(maxsize=100)

        def enqueue(event: TrackerEvent) -> None:
            if not queue.full():
                queue.put_nowait(event)

        unsubscribe = core.subscribe(enqueue)
        try:
            await websocket.send_json({"type": "state", "data": state_payload()})
            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=15)
                except TimeoutError:
                    await websocket.send_json(
                        {
                            "type": "heartbeat",
                            "observation": core.observation_status,
                        }
                    )
                    continue
                await websocket.send_json(
                    {"type": event.event_type, "data": jsonable(event)}
                )
        except (WebSocketDisconnect, RuntimeError):
            logger.debug("Web tracker client disconnected")
        finally:
            unsubscribe()

    candidates = (
        dist_dir,
        Path.cwd() / "frontend" / "dist",
        Path(__file__).resolve().parents[4] / "frontend" / "dist",
    )
    resolved_dist = next(
        (
            candidate
            for candidate in candidates
            if candidate is not None and candidate.is_dir()
        ),
        None,
    )
    if resolved_dist is not None:
        app.mount(
            "/",
            StaticFiles(directory=str(resolved_dist), html=True),
            name="static",
        )
    else:
        logger.warning("Svelte build not found; FastAPI is running in API-only mode")

    return app


__all__ = ["create_fastapi_app"]
