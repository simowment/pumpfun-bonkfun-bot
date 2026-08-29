"""Telegram UI adapter driving a shared RugbotCore through the BaseAdapter contract."""

from __future__ import annotations

from typing import TYPE_CHECKING

from telegram import Update
from telegram.ext import Application, ContextTypes, MessageHandler, filters

from rugbot.application.commands import BotCommand
from rugbot.interfaces.base import BaseAdapter
from rugbot.tracker.events import DecisionEvent, LaunchDetected, TrackerEvent

if TYPE_CHECKING:
    from rugbot.runtime.app import RugbotApp


class TelegramAdapter(BaseAdapter):
    """Drive one RugbotApp through a Telegram bot, sending and receiving messages.

    The adapter owns the Telegram transport only; all command behavior lives in
    the shared ``RugbotApp`` and the command registry.
    """

    def __init__(
        self,
        core: RugbotApp,
        *,
        token: str,
        chat_id: int,
        allowed_user_ids: tuple[int, ...] = (),
    ) -> None:
        """Store the core and Telegram connection parameters.

        Args:
            core: The shared UI facade that owns all tracker and command logic.
            token: Telegram bot token issued by BotFather.
            chat_id: Destination chat for outbound tracker-event messages.
            allowed_user_ids: Optional allowlist of Telegram user ids. When
                non-empty, inbound messages from any other user are ignored.
        """
        self._core = core
        self._token = token
        self._chat_id = chat_id
        self._allowed_user_ids = allowed_user_ids
        self._app: Application | None = None

    async def connect(self) -> None:
        """Build the Telegram application, subscribe to the core, and poll."""
        app = Application.builder().token(self._token).build()
        app.add_handler(MessageHandler(filters.COMMAND, self._handle_update))
        self._core.subscribe(self._on_tracker_event)
        self._app = app
        await app.initialize()
        await app.start()
        await app.updater.start_polling()

    async def disconnect(self) -> None:
        """Stop polling, release the application, and unsubscribe from the core."""
        app = self._app
        self._app = None
        if app is None:
            return
        self._core.event_bus.unsubscribe("*", self._on_tracker_event)
        await app.updater.stop()
        await app.stop()
        await app.shutdown()

    async def send(self, event: TrackerEvent) -> None:
        """Render one tracker event and deliver it to the configured chat."""
        if self._app is None:
            return
        text = self._render_event(event)
        await self._app.bot.send_message(chat_id=self._chat_id, text=text)

    async def on_message(self, message: object) -> None:
        """Parse an inbound Telegram command, dispatch it, and reply with the result.

        Unauthorized senders are ignored (fail closed) when an allowlist is set.
        """
        if not isinstance(message, Update) or message.message is None:
            return
        if message.effective_user is not None and self._allowed_user_ids:
            if message.effective_user.id not in self._allowed_user_ids:
                return
        text = message.message.text
        if not text or not text.strip():
            return
        parts = text.strip().split()
        cmd = BotCommand(
            name=parts[0].lstrip("/"),
            args=tuple(parts[1:]),
            source="telegram",
        )
        result = await self._core.execute_command(cmd)
        await message.message.reply_text(result.message)

    async def _handle_update(
        self,
        update: Update,
        _context: ContextTypes.DEFAULT_TYPE,
    ) -> None:
        """Adapt the Telegram handler callback to the BaseAdapter contract."""
        await self.on_message(update)

    async def _on_tracker_event(self, event: TrackerEvent) -> None:
        """Bridge a core tracker event to the outbound Telegram channel."""
        await self.send(event)

    def _render_event(self, event: TrackerEvent) -> str:
        """Render one tracker event into a short plain-text chat message."""
        if isinstance(event, LaunchDetected):
            symbol = event.data.get("symbol", "")
            mint = event.data.get("mint", "")
            creator = event.data.get("creator", "")
            return f"🚀 LAUNCH {symbol} ({mint}) by {creator}"
        if isinstance(event, DecisionEvent):
            return f"🎯 {event.event_type}: {event.reason}"
        return f"{event.event_type}: {event}"


__all__ = ["TelegramAdapter"]
