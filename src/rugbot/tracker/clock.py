"""Deterministic clock protocols and implementations for testing and runtime."""

# ruff: noqa: TRY003

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Protocol


class Clock(Protocol):
    """Protocol for time querying in deterministic state engines."""

    def now(self) -> datetime:
        """Return the current datetime with UTC timezone."""

    def timestamp(self) -> int:
        """Return the current unix timestamp in seconds."""


class SystemClock:
    """Production clock querying the operating system UTC clock."""

    def now(self) -> datetime:
        return datetime.now(UTC)

    def timestamp(self) -> int:
        return int(datetime.now(UTC).timestamp())


class FakeClock:
    """Deterministic controllable clock for instantaneous time testing."""

    def __init__(self, initial_time: datetime | str | int | None = None) -> None:
        if initial_time is None:
            self._current_time = datetime.now(UTC)
        elif isinstance(initial_time, str):
            self._current_time = datetime.fromisoformat(
                initial_time.replace("Z", "+00:00")
            )
        elif isinstance(initial_time, (int, float)):
            self._current_time = datetime.fromtimestamp(initial_time, tz=UTC)
        elif isinstance(initial_time, datetime):
            self._current_time = (
                initial_time
                if initial_time.tzinfo
                else initial_time.replace(tzinfo=UTC)
            )
        else:
            raise TypeError("unsupported initial_time type")

    def now(self) -> datetime:
        return self._current_time

    def timestamp(self) -> int:
        return int(self._current_time.timestamp())

    def advance(self, seconds: int | float) -> datetime:
        """Advance fake clock forward by a given number of seconds."""
        self._current_time = self._current_time + timedelta(seconds=seconds)
        return self._current_time

    def set_time(self, new_time: datetime | str | int) -> datetime:
        """Directly set the fake clock to a given timestamp or datetime."""
        if isinstance(new_time, str):
            self._current_time = datetime.fromisoformat(new_time.replace("Z", "+00:00"))
        elif isinstance(new_time, (int, float)):
            self._current_time = datetime.fromtimestamp(new_time, tz=UTC)
        elif isinstance(new_time, datetime):
            self._current_time = (
                new_time if new_time.tzinfo else new_time.replace(tzinfo=UTC)
            )
        return self._current_time
