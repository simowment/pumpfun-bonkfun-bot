"""Aiohttp web bridge exposing the shared RugbotCore facade to a browser.

The adapter owns the HTTP/WebSocket transport only; all tracker and command
behavior lives in the shared ``RugbotCore`` and the command registry. The API
is UI-agnostic and does not duplicate tracker or command logic.
"""

from __future__ import annotations

from dataclasses import fields, is_dataclass
from enum import Enum
from typing import TYPE_CHECKING

from aiohttp import web

from rugbot.core.commands import BotCommand
from rugbot.interfaces.base import BaseAdapter

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from rugbot.core.commands import CommandResult
    from rugbot.core.rugbot_core import RugbotCore
    from rugbot.tracker.events import TrackerEvent


def jsonable(value: object) -> object:
    """Recursively convert a domain value into a JSON-safe structure.

    Dataclasses become mappings, enums become their values, and tuples and
    lists become lists. Nested values are converted recursively. Values that
    are already JSON-safe (str, int, float, bool, None) pass through unchanged.
    """
    if isinstance(value, Enum):
        return value.value
    if is_dataclass(value):
        return {
            field.name: jsonable(getattr(value, field.name)) for field in fields(value)
        }
    if isinstance(value, tuple):
        return [jsonable(item) for item in value]
    if isinstance(value, list):
        return [jsonable(item) for item in value]
    if isinstance(value, dict):
        return {str(key): jsonable(item) for key, item in value.items()}
    return value


class WebAdapter(BaseAdapter):
    """Bridge one RugbotCore to browser clients over HTTP and WebSocket.

    The adapter subscribes to core tracker events and broadcasts them to every
    connected WebSocket client. Inbound JSON command messages are validated and
    dispatched through the shared command registry.
    """

    def __init__(self, core: RugbotCore) -> None:
        """Store the core and initialize the empty client set.

        Args:
            core: The shared UI facade that owns all tracker and command logic.
        """
        self._core = core
        self._clients: set[web.WebSocketResponse] = set()
        self._subscribed = False

    async def connect(self) -> None:
        """Subscribe to every core tracker event."""
        self._core.subscribe(self._on_tracker_event)
        self._subscribed = True

    async def disconnect(self) -> None:
        """Unsubscribe from core events and close every connected client."""
        if self._subscribed:
            self._core.event_bus.unsubscribe("*", self._on_tracker_event)
            self._subscribed = False
        for ws in list(self._clients):
            await ws.close()
        self._clients.clear()

    async def send(self, event: TrackerEvent) -> None:
        """Broadcast one tracker event to every connected WebSocket client.

        Disconnected clients are dropped without raising, so a stale socket
        never crashes the shared event bus.
        """
        payload = {"type": "event", "data": jsonable(event)}
        for ws in list(self._clients):
            if ws.closed:
                self._clients.discard(ws)
                continue
            try:
                await ws.send_json(payload)
            except (ConnectionResetError, RuntimeError):
                self._clients.discard(ws)

    async def on_message(self, message: object) -> CommandResult | None:
        """Validate an inbound JSON-like command and dispatch it to the core.

        Returns the command result, or ``None`` when the message is malformed
        (missing or non-string ``name``, or non-list ``args``).
        """
        if not isinstance(message, dict):
            return None
        name = message.get("name")
        args = message.get("args", [])
        if not isinstance(name, str) or not name:
            return None
        if not isinstance(args, list) or not all(isinstance(arg, str) for arg in args):
            return None
        command = BotCommand(name=name, args=tuple(args), source="web")
        return await self._core.execute_command(command)

    def register_client(self, ws: web.WebSocketResponse) -> None:
        """Track one connected WebSocket client for event broadcast."""
        self._clients.add(ws)

    def unregister_client(self, ws: web.WebSocketResponse) -> None:
        """Drop one WebSocket client from the broadcast set."""
        self._clients.discard(ws)

    async def _on_tracker_event(self, event: TrackerEvent) -> None:
        """Bridge a core tracker event to every connected WebSocket client."""
        await self.send(event)


def _state_projection(core: RugbotCore) -> dict[str, object]:
    """Project the core's current state into a JSON-safe mapping."""
    return {
        "targets": jsonable(core.targets()),
        "funders": jsonable(core.funders()),
        "wallets": jsonable(core.wallets()),
        "launches": jsonable(core.launches()),
        "positions": jsonable(core.positions()),
        "snapshot": jsonable(core.snapshot()),
    }


def _json_response(payload: object, *, status: int = 200) -> web.Response:
    """Build a JSON response from an already JSON-safe payload."""
    return web.json_response(jsonable(payload), status=status)


@web.middleware
async def _cors_middleware(
    request: web.Request,
    handler: Callable[[web.Request], Awaitable[web.StreamResponse]],
) -> web.StreamResponse:
    """Add permissive CORS headers and answer OPTIONS preflight requests."""
    if request.method == "OPTIONS":
        response = web.Response(status=204)
    else:
        response = await handler(request)
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type"
    return response


async def _handle_health(_request: web.Request) -> web.Response:
    """Return a lightweight liveness payload."""
    return _json_response({"status": "ok", "service": "rugbot-web"})


async def _handle_state(request: web.Request) -> web.Response:
    """Return the current JSON-safe state projection."""
    core = request.app["rugbot_core"]
    return _json_response(_state_projection(core))


async def _handle_command(request: web.Request) -> web.Response:
    """Validate and dispatch one JSON command body to the core."""
    try:
        payload = await request.json()
    except ValueError:
        return _json_response(
            {"ok": False, "message": "request body must be valid JSON"},
            status=400,
        )
    adapter = request.app["rugbot_adapter"]
    result = await adapter.on_message(payload)
    if result is None:
        return _json_response(
            {
                "ok": False,
                "message": (
                    "malformed command: name must be a string and args must be "
                    "a list of strings"
                ),
            },
            status=400,
        )
    return _json_response(
        {"ok": result.ok, "message": result.message, "data": jsonable(result.data)}
    )


async def _handle_events(request: web.Request) -> web.WebSocketResponse:
    """Serve the live event stream over a WebSocket connection."""
    ws = web.WebSocketResponse()
    await ws.prepare(request)
    adapter = request.app["rugbot_adapter"]
    core = request.app["rugbot_core"]
    await ws.send_json({"type": "state", "data": _state_projection(core)})
    adapter.register_client(ws)
    try:
        async for _message in ws:
            pass
    finally:
        adapter.unregister_client(ws)
    return ws


async def _on_startup(app: web.Application) -> None:
    """Subscribe the adapter to core tracker events on startup."""
    adapter = app["rugbot_adapter"]
    await adapter.connect()


async def _on_cleanup(app: web.Application) -> None:
    """Unsubscribe the adapter, close clients, then release the core on shutdown."""
    adapter = app["rugbot_adapter"]
    await adapter.disconnect()
    core = app["rugbot_core"]
    await core.close()


def create_web_app(core: RugbotCore) -> web.Application:
    """Build an aiohttp application exposing the RugbotCore web bridge.

    Args:
        core: The shared RugbotCore facade to expose over HTTP and WebSocket.

    Returns:
        A configured ``aiohttp.web.Application`` with the API routes wired up.
    """
    adapter = WebAdapter(core)
    app = web.Application(middlewares=[_cors_middleware])
    app["rugbot_core"] = core
    app["rugbot_adapter"] = adapter
    app.router.add_get("/api/health", _handle_health)
    app.router.add_get("/api/state", _handle_state)
    app.router.add_post("/api/command", _handle_command)
    app.router.add_get("/api/events", _handle_events)
    app.on_startup.append(_on_startup)
    app.on_cleanup.append(_on_cleanup)
    return app


__all__ = ["WebAdapter", "create_web_app", "jsonable"]
