"""FastAPI surface for the Svelte wallet and entity tracker."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from rugbot.execution.ports import ExecutionMode
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


class TradeBuyRequest(BaseModel):
    """Validated request for buying a token."""

    mint: str = Field(min_length=32, max_length=64)
    amount_sol: float = Field(gt=0)
    slippage_pct: float = Field(default=5.0, ge=0.0, le=100.0)
    priority_fee_sol: float = Field(default=0.0005, ge=0.0)
    jito_tip_sol: float = Field(default=0.001, ge=0.0)
    routing: Literal["auto", "rpc", "jito"] = "auto"
    mode: str = Field(default="paper")
    take_profit_pct: float | None = Field(default=None, gt=0)
    stop_loss_pct: float | None = Field(default=None, gt=0)
    trailing_stop_pct: float | None = Field(default=None, gt=0)


class TradeSellRequest(BaseModel):
    """Validated request for selling a token."""

    mint: str = Field(min_length=32, max_length=64)
    percent: float = Field(default=100.0, gt=0, le=100.0)
    amount_tokens: int | None = Field(default=None, gt=0)
    slippage_pct: float = Field(default=10.0, ge=0.0, le=100.0)
    priority_fee_sol: float = Field(default=0.0005, ge=0.0)
    jito_tip_sol: float = Field(default=0.001, ge=0.0)
    routing: Literal["auto", "rpc", "jito"] = "auto"
    mode: str = Field(default="paper")


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

    @app.post("/api/trade/buy")
    async def api_trade_buy(payload: TradeBuyRequest) -> dict[str, object]:
        """Execute a buy order with slippage, fees, and optional TP/SL brackets."""
        mode_val = payload.mode.lower().strip()
        execution_mode = (
            ExecutionMode.LIVE if mode_val == "live" else ExecutionMode.PAPER
        )
        result = await core.trade_service.buy(
            mint=payload.mint,
            amount_sol=payload.amount_sol,
            slippage_pct=payload.slippage_pct,
            priority_fee_sol=payload.priority_fee_sol,
            jito_tip_sol=payload.jito_tip_sol,
            routing=payload.routing,
            mode=execution_mode,
            take_profit_pct=payload.take_profit_pct,
            stop_loss_pct=payload.stop_loss_pct,
            trailing_stop_pct=payload.trailing_stop_pct,
        )
        if not result.ok:
            raise HTTPException(
                status_code=400, detail=result.error or "Buy order failed"
            )
        return {
            "ok": True,
            "side": result.side.value,
            "mint": result.mint,
            "mode": result.mode.value,
            "sol_amount": result.sol_amount,
            "token_amount": result.token_amount,
            "signature": result.signature,
            "effective_price_sol": result.effective_price_sol,
            "fee_sol": result.fee_sol,
            "take_profit_pct": result.take_profit_pct,
            "stop_loss_pct": result.stop_loss_pct,
            "message": result.message,
        }

    @app.post("/api/trade/sell")
    async def api_trade_sell(payload: TradeSellRequest) -> dict[str, object]:
        """Execute a sell order with slippage and priority fees."""
        mode_val = payload.mode.lower().strip()
        execution_mode = (
            ExecutionMode.LIVE if mode_val == "live" else ExecutionMode.PAPER
        )
        result = await core.trade_service.sell(
            mint=payload.mint,
            percent=payload.percent,
            amount_tokens=payload.amount_tokens,
            slippage_pct=payload.slippage_pct,
            priority_fee_sol=payload.priority_fee_sol,
            jito_tip_sol=payload.jito_tip_sol,
            routing=payload.routing,
            mode=execution_mode,
        )
        if not result.ok:
            raise HTTPException(
                status_code=400, detail=result.error or "Sell order failed"
            )
        return {
            "ok": True,
            "side": result.side.value,
            "mint": result.mint,
            "mode": result.mode.value,
            "sol_amount": result.sol_amount,
            "token_amount": result.token_amount,
            "signature": result.signature,
            "effective_price_sol": result.effective_price_sol,
            "fee_sol": result.fee_sol,
            "message": result.message,
        }

    @app.get("/api/trade/positions")
    async def api_trade_positions() -> dict[str, object]:
        """List all currently active open positions with TP/SL levels."""
        positions = core.trade_service.get_positions()
        return {
            "ok": True,
            "positions": positions,
            "total_open": len(positions),
        }

    @app.delete("/api/trade/positions/{mint}")
    async def api_trade_close_position(mint: str) -> dict[str, object]:
        """Market sell and close 100% of an open position."""
        normalized_mint = mint.strip()
        result = await core.trade_service.sell(mint=normalized_mint, percent=100.0)
        if not result.ok:
            raise HTTPException(
                status_code=400, detail=result.error or "Failed to close position"
            )
        return {
            "ok": True,
            "message": f"Closed position for {normalized_mint}",
            "result": jsonable(result),
        }

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
