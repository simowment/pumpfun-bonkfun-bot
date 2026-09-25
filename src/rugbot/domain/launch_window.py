"""Pure launch-window validity check (domain-owned, CLI re-exports)."""

from __future__ import annotations

from typing import Final

WINDOW_GRACE_MS: Final[int] = 600_000


def launch_window_is_valid(  # noqa: C901 - verbatim canonical rule moved from CLI
    candles: list[dict], created_ms: object
) -> bool:
    """Return True when candles plausibly cover the mint launch window.

    Args:
        candles: Swap-api candle dicts with ``timestamp`` (ms) and ``volume``.
        created_ms: Mint creation timestamp in ms.

    Returns:
        True when the window carries real volume and starts near creation.
    """
    if not candles:
        return False

    def _to_number(value: object) -> float | None:
        if isinstance(value, bool):
            return None
        if isinstance(value, (int, float)):
            return float(value)
        if isinstance(value, str):
            try:
                return float(value)
            except ValueError:
                return None
        return None

    total_volume = 0.0
    for candle in candles:
        volume = _to_number(candle.get("volume"))
        if volume is not None:
            total_volume += volume
    if total_volume <= 0:
        return False
    first_ts = _to_number(candles[0].get("timestamp"))
    created = _to_number(created_ms)
    if first_ts is not None and created is not None:
        if first_ts - created > WINDOW_GRACE_MS:
            return False
    return True
