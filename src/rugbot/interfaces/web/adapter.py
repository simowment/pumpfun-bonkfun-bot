"""JSON projection shared by the Svelte web API."""

from __future__ import annotations

from dataclasses import asdict, is_dataclass
from enum import Enum
from pathlib import Path
from typing import TypeAlias

JsonValue: TypeAlias = (
    None | str | int | float | bool | list["JsonValue"] | dict[str, "JsonValue"]
)


def jsonable(value: object) -> JsonValue:
    """Convert domain values to JSON-compatible primitives."""

    if value is None or isinstance(value, str | int | float | bool):
        return value
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Path):
        return str(value)
    if is_dataclass(value) and not isinstance(value, type):
        return jsonable(asdict(value))
    if isinstance(value, dict):
        return {str(key): jsonable(item) for key, item in value.items()}
    if isinstance(value, list | tuple | set | frozenset):
        return [jsonable(item) for item in value]
    raise TypeError(type(value).__name__)


__all__ = ["jsonable"]
