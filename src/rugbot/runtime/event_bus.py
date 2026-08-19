"""Event bus for decoupled asynchronous and synchronous publish-subscribe routing."""

from __future__ import annotations

import asyncio
import inspect
import logging
from collections import defaultdict
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable

    from rugbot.tracker.events import TrackerEvent

logger = logging.getLogger("rugbot.events")


class EventBus:
    """Asynchronous and synchronous pub/sub event router."""

    def __init__(self) -> None:
        self._subscribers: dict[str, list[Callable[[Any], Any]]] = defaultdict(list)
        self._background_tasks: set[asyncio.Task[Any]] = set()

    def subscribe(self, event_type: str, handler: Callable[[Any], Any]) -> None:
        """Register a handler for a specific event type or '*' for all events."""
        self._subscribers[event_type].append(handler)

    def unsubscribe(self, event_type: str, handler: Callable[[Any], Any]) -> None:
        """Remove a registered handler."""
        if event_type in self._subscribers and handler in self._subscribers[event_type]:
            self._subscribers[event_type].remove(handler)

    def publish(self, event: TrackerEvent) -> None:
        """Dispatch an event synchronously to all matching handlers and schedule async ones."""
        event_name = getattr(event, "event_type", type(event).__name__)
        handlers = self._subscribers.get(event_name, []) + self._subscribers.get(
            "*", []
        )

        for handler in handlers:
            try:
                if inspect.iscoroutinefunction(handler):
                    try:
                        loop = asyncio.get_running_loop()
                        task = loop.create_task(handler(event))
                        self._background_tasks.add(task)
                        task.add_done_callback(self._background_tasks.discard)
                    except RuntimeError:
                        asyncio.run(handler(event))
                else:
                    handler(event)
            except Exception:
                logger.exception(
                    "Error executing event handler %s for event %s", handler, event
                )
