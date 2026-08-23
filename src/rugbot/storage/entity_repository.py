"""Canonical entity and tracker repository storing funding trees, transfers, launches, and policies."""

from __future__ import annotations

from rugbot.storage.tracker import SQLiteTrackerRepository
from rugbot.tracker.repository import TrackerRepository

EntityRepository = SQLiteTrackerRepository

__all__ = [
    "EntityRepository",
    "SQLiteTrackerRepository",
    "TrackerRepository",
]
