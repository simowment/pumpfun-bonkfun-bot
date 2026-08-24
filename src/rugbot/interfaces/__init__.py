"""UI adapter contracts for the UI-agnostic RugbotCore facade."""

from __future__ import annotations

from rugbot.interfaces.base import BaseAdapter
from rugbot.interfaces.discord import DiscordAdapter
from rugbot.interfaces.web import create_fastapi_app

__all__ = [
    "BaseAdapter",
    "DiscordAdapter",
    "create_fastapi_app",
]
