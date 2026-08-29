"""Telegram interface package."""

from __future__ import annotations

from rugbot.interfaces.telegram.adapter import TelegramAdapter
from rugbot.interfaces.telegram.runner import main

__all__ = ["TelegramAdapter", "main"]
