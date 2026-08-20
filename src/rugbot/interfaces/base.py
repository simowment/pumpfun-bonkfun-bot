"""Minimal UI adapter contract shared by all Rugbot frontends."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from rugbot.tracker.events import TrackerEvent


class BaseAdapter(ABC):
    """Contract every UI adapter must implement to drive RugbotCore."""

    @abstractmethod
    async def connect(self) -> None:
        """Open the adapter's transport and start receiving messages."""

    @abstractmethod
    async def disconnect(self) -> None:
        """Close the adapter's transport and release resources."""

    @abstractmethod
    async def send(self, event: TrackerEvent) -> None:
        """Deliver one tracker event to the UI."""

    @abstractmethod
    async def on_message(self, message: object) -> None:
        """Handle one inbound UI message and dispatch it to the core."""


__all__ = ["BaseAdapter"]
