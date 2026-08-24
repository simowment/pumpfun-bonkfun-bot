"""Svelte web dashboard REST and WebSocket API."""

from __future__ import annotations

from rugbot.interfaces.web.adapter import jsonable
from rugbot.interfaces.web.fastapi_app import create_fastapi_app

__all__ = [
    "create_fastapi_app",
    "jsonable",
]
