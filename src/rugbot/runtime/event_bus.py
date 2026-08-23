"""Event bus for decoupled asynchronous and synchronous publish-subscribe routing."""

# ruff: noqa: BLE001, TRY400

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

    def subscribe(
        self,
        event_type: str | Callable[[Any], Any],
        handler: Callable[[Any], Any] | None = None,
    ) -> Callable[[], None]:
        """Register a handler for a specific event type or '*' for all events."""
        if handler is None:
            real_handler = event_type  # callable
            event_name = "*"
        else:
            real_handler = handler
            event_name = str(event_type)
        self._subscribers[event_name].append(real_handler)
        return lambda: self.unsubscribe(event_name, real_handler)

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
                        pass
                else:
                    handler(event)
            except Exception as exc:
                logger.error("EventBus handler error for %s: %s", event_name, exc)


__all__ = ["EventBus"]
