"""Runnable entry point wiring a TelegramAdapter to a shared RugbotCore."""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

from rugbot.core.factory import build_ui_runtime
from rugbot.interfaces.telegram import TelegramAdapter
from rugbot.runtime.config import (
    resolve_config_path,
    resolve_dotenv,
    resolve_state_dir,
)


def main() -> int:
    """Launch the Telegram bot adapter against the shared RugbotCore."""
    resolve_dotenv()
    token = os.environ.get("TELEGRAM_TOKEN")
    if not token:
        print("TELEGRAM_TOKEN is required", file=sys.stderr)
        return 1
    chat_id_raw = os.environ.get("TELEGRAM_CHAT_ID")
    if not chat_id_raw:
        print("TELEGRAM_CHAT_ID is required", file=sys.stderr)
        return 1
    try:
        chat_id = int(chat_id_raw)
    except ValueError:
        print("TELEGRAM_CHAT_ID must be an integer", file=sys.stderr)
        return 1
    allowed_raw = os.environ.get("TELEGRAM_ALLOWED_USER_IDS", "")
    allowed_user_ids: tuple[int, ...] = ()
    if allowed_raw.strip():
        try:
            allowed_user_ids = tuple(
                int(part) for part in allowed_raw.replace(",", " ").split()
            )
        except ValueError:
            print("TELEGRAM_ALLOWED_USER_IDS must be integers", file=sys.stderr)
            return 1

    state_dir = resolve_state_dir(Path(".state/watch"))
    config_path = resolve_config_path(Path("watch.yaml"))
    core = build_ui_runtime(state_dir=state_dir, config_path=config_path)
    adapter = TelegramAdapter(
        core,
        token=token,
        chat_id=chat_id,
        allowed_user_ids=allowed_user_ids,
    )

    async def _run() -> None:
        await adapter.connect()
        try:
            await asyncio.Event().wait()
        finally:
            await adapter.disconnect()

    asyncio.run(_run())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
