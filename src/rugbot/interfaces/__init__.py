"""UI adapter contracts for the UI-agnostic RugbotCore facade."""

from rugbot.interfaces.base import BaseAdapter
from rugbot.interfaces.web import WebAdapter, create_web_app

__all__ = ["BaseAdapter", "WebAdapter", "create_web_app"]
