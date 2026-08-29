"""Runnable entry point launching the FastAPI web server for Rugbot."""

from __future__ import annotations

import os
import socket
import sys
from pathlib import Path

import uvicorn

from rugbot.interfaces.web.fastapi_app import create_fastapi_app
from rugbot.runtime.app import build_ui_runtime
from rugbot.runtime.config import resolve_dotenv, resolve_state_dir

MAX_TCP_PORT = 65535


def is_port_available(host: str, port: int) -> bool:
    """Check if a TCP port is free to bind."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind((host, port))
        except OSError:
            return False
        else:
            return True


def find_available_port(host: str, start_port: int, max_attempts: int = 10) -> int:
    """Find an available port starting from start_port."""
    for p in range(start_port, start_port + max_attempts):
        if is_port_available(host, p):
            return p
    return start_port


def main() -> int:
    """Launch the FastAPI server against the shared RugbotCore."""
    resolve_dotenv()
    host = os.environ.get("RUG_WEB_HOST", "127.0.0.1")
    if not host:
        print("RUG_WEB_HOST must be a non-empty host", file=sys.stderr)
        return 1
    port_raw = os.environ.get("RUG_WEB_PORT", "8787")
    try:
        requested_port = int(port_raw)
    except ValueError:
        print("RUG_WEB_PORT must be an integer", file=sys.stderr)
        return 1
    if not 1 <= requested_port <= MAX_TCP_PORT:
        print(f"RUG_WEB_PORT must be between 1 and {MAX_TCP_PORT}", file=sys.stderr)
        return 1

    port = find_available_port(host, requested_port)
    if port != requested_port:
        print(
            f"[NOTICE] Port {requested_port} was busy. Automatically switched to port {port}.",
            flush=True,
        )

    state_dir = resolve_state_dir(
        Path(os.environ.get("RUGBOT_STATE_DIR", ".state/watch"))
    )
    core = build_ui_runtime(
        state_dir=state_dir,
    )
    app = create_fastapi_app(core)

    print("\n" + "=" * 60, flush=True)
    print("  RUGBOT SVELTE ENTITY TRACKER", flush=True)
    print(f"  Access UI at: http://{host}:{port}", flush=True)
    print(f"  Swagger Docs: http://{host}:{port}/docs", flush=True)
    print("=" * 60 + "\n", flush=True)

    try:
        uvicorn.run(app, host=host, port=port, log_level="warning")
    except OSError as e:
        print(
            f"\n[ERROR] Failed to start server on http://{host}:{port}: {e}",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
