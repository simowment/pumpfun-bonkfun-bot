"""Discord UI adapter driving the shared RugbotCore facade."""

from __future__ import annotations

import asyncio
import logging
import os
from typing import TYPE_CHECKING

import discord

from rugbot.core.commands import BotCommand
from rugbot.core.factory import build_ui_runtime
from rugbot.interfaces.base import BaseAdapter
from rugbot.runtime.config import resolve_config_path, resolve_state_dir
from rugbot.tracker.events import DecisionEvent, LaunchDetected, TrackerEvent

if TYPE_CHECKING:
    from rugbot.core.rugbot_core import RugbotCore

logger = logging.getLogger("rugbot.interfaces.discord")

_COMMAND_PREFIX = "!"


class DiscordConfigError(RuntimeError):
    """Raised when required Discord environment configuration is missing."""


class DiscordAdapter(BaseAdapter):
    """Bridge a Discord bot to RugbotCore using the shared command registry."""

    def __init__(
        self,
        core: RugbotCore,
        *,
        token: str,
        channel_id: int,
        allowed_user_ids: tuple[int, ...] = (),
    ) -> None:
        """Initialize the adapter with the core and Discord connection settings.

        Args:
            core: The shared RugbotCore facade to drive.
            token: Discord bot token used to authenticate the client.
            channel_id: Discord channel id to post tracker events into.
            allowed_user_ids: Optional allowlist of Discord user ids permitted to
                issue commands. When empty, every non-bot user is allowed.
        """
        self._core = core
        self._token = token
        self._channel_id = channel_id
        self._allowed_user_ids = allowed_user_ids
        self._client: discord.Client | None = None
        self._subscribed = False

    async def connect(self) -> None:
        """Start the Discord client, log in, and subscribe to tracker events."""
        intents = discord.Intents.default()
        intents.message_content = True
        client = discord.Client(intents=intents)
        client.event(self._on_ready)
        client.event(self._on_discord_message)
        self._client = client
        self._core.subscribe(self._on_tracker_event)
        self._subscribed = True
        await client.start(self._token)

    async def disconnect(self) -> None:
        """Close the Discord client and unsubscribe from tracker events."""
        if self._subscribed:
            self._core.event_bus.unsubscribe("*", self._on_tracker_event)
            self._subscribed = False
        if self._client is not None:
            await self._client.close()
            self._client = None

    async def send(self, event: TrackerEvent) -> None:
        """Render one tracker event and post it to the configured channel."""
        if self._client is None:
            return
        channel = self._client.get_channel(self._channel_id)
        if channel is None:
            return
        await channel.send(self._render_event(event))

    async def on_message(self, message: object) -> None:
        """Parse an inbound message into a BotCommand and dispatch it to the core."""
        content = getattr(message, "content", "")
        if not content or not content.startswith(_COMMAND_PREFIX):
            return
        parts = content[len(_COMMAND_PREFIX) :].split()
        if not parts:
            return
        command = BotCommand(
            name=parts[0],
            args=tuple(parts[1:]),
            source="discord",
        )
        result = await self._core.execute_command(command)
        channel = getattr(message, "channel", None)
        if channel is not None:
            await channel.send(result.message)

    def _render_event(self, event: TrackerEvent) -> str:
        """Render a tracker event into a short plain-text chat message."""
        if isinstance(event, LaunchDetected):
            symbol = event.data.get("symbol", "")
            mint = event.data.get("mint", "")
            creator = event.data.get("creator", "")
            return f"🚀 LAUNCH {symbol} ({mint}) by {creator}"
        if isinstance(event, DecisionEvent):
            return f"🎯 {event.event_type}: {event.reason}"
        return f"{event.event_type}: {event}"

    async def _on_ready(self) -> None:
        """Log the successful Discord login."""
        user = self._client.user if self._client is not None else None
        logger.info("Discord adapter ready as %s", user)

    async def _on_discord_message(self, message: discord.Message) -> None:
        """Authorize and forward a Discord message to the command handler."""
        if message.author.bot:
            return
        if self._allowed_user_ids and message.author.id not in self._allowed_user_ids:
            return
        if message.channel.id != self._channel_id:
            return
        await self.on_message(message)

    async def _on_tracker_event(self, event: TrackerEvent) -> None:
        """Forward a tracker event to the Discord channel."""
        await self.send(event)


def main() -> None:
    """Run the Discord adapter from environment configuration."""
    token = os.environ.get("DISCORD_TOKEN")
    if not token:
        raise DiscordConfigError(  # noqa: TRY003
            "DISCORD_TOKEN environment variable is required"
        )
    channel_id_raw = os.environ.get("DISCORD_CHANNEL_ID")
    if not channel_id_raw:
        raise DiscordConfigError(  # noqa: TRY003
            "DISCORD_CHANNEL_ID environment variable is required"
        )
    channel_id = int(channel_id_raw)
    allowed_raw = os.environ.get("DISCORD_ALLOWED_USER_IDS", "")
    allowed_user_ids = tuple(
        int(part) for part in allowed_raw.split(",") if part.strip()
    )

    core = build_ui_runtime(
        state_dir=resolve_state_dir(),
        config_path=resolve_config_path(),
    )
    adapter = DiscordAdapter(
        core,
        token=token,
        channel_id=channel_id,
        allowed_user_ids=allowed_user_ids,
    )
    asyncio.run(adapter.connect())


__all__ = ["DiscordAdapter", "main"]
