"""UI-agnostic core facade, command layer, and composition factory."""

from rugbot.core.commands import COMMAND_REGISTRY, BotCommand, CommandResult
from rugbot.core.factory import build_ui_runtime
from rugbot.core.rugbot_core import RugbotCore

__all__ = [
    "COMMAND_REGISTRY",
    "BotCommand",
    "CommandResult",
    "RugbotCore",
    "build_ui_runtime",
]
