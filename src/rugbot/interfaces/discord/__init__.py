"""Discord bot trading interface, interactive views, and cockpit."""

from __future__ import annotations

from rugbot.interfaces.discord.adapter import (
    COLOR_DANGER,
    COLOR_INFO,
    COLOR_NEUTRAL,
    COLOR_SUCCESS,
    COLOR_WARNING,
    SOLANA_ADDRESS_REGEX,
    CockpitHomeView,
    DiscordAdapter,
    DiscordConfigError,
    PositionActionView,
    QuickBuyView,
    SettingsView,
    main,
)

run_bot = main

__all__ = [
    "COLOR_DANGER",
    "COLOR_INFO",
    "COLOR_NEUTRAL",
    "COLOR_SUCCESS",
    "COLOR_WARNING",
    "SOLANA_ADDRESS_REGEX",
    "CockpitHomeView",
    "DiscordAdapter",
    "DiscordConfigError",
    "PositionActionView",
    "QuickBuyView",
    "SettingsView",
    "main",
    "run_bot",
]
