"""Runnable entry point wiring the aiohttp web bridge to a shared RugbotCore."""

from __future__ import annotations

import os
import sys
from pathlib import Path

from aiohttp import web

from rugbot.interfaces.web.adapter import create_web_app
from rugbot.runtime.app import build_ui_runtime
from rugbot.runtime.config import (
    resolve_config_path,
    resolve_dotenv,
    resolve_state_dir,
)

MAX_TCP_PORT = 65535


def main() -> int:
    """Launch the aiohttp web bridge against the shared RugbotCore."""
    resolve_dotenv()
    host = os.environ.get("RUG_WEB_HOST", "127.0.0.1")
    if not host:
        print("RUG_WEB_HOST must be a non-empty host", file=sys.stderr)
        return 1
    port_raw = os.environ.get("RUG_WEB_PORT", "8787")
    try:
        port = int(port_raw)
    except ValueError:
        print("RUG_WEB_PORT must be an integer", file=sys.stderr)
        return 1
    if not 1 <= port <= MAX_TCP_PORT:
        print(f"RUG_WEB_PORT must be between 1 and {MAX_TCP_PORT}", file=sys.stderr)
        return 1

    state_dir = resolve_state_dir(
        Path(os.environ.get("RUGBOT_STATE_DIR", ".state/web"))
    )
    config_path = resolve_config_path(
        Path(os.environ.get("RUGBOT_CONFIG", "watch.yaml"))
    )
    core = build_ui_runtime(
        state_dir=state_dir,
        config_path=config_path if config_path.exists() else None,
    )
    app = create_web_app(core)
    web.run_app(app, host=host, port=port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
