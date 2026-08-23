"""Web interface dashboard and REST API."""

from __future__ import annotations

from rugbot.interfaces.web.adapter import (
    WebAdapter,
    create_web_app,
    jsonable,
)

__all__ = [
    "WebAdapter",
    "create_web_app",
    "jsonable",
]
