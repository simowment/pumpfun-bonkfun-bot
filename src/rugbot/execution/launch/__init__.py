"""Automated token launch, bundle assembly, and exit orchestration."""

from __future__ import annotations

from rugbot.execution.launch.bundle_assembler import (
    AssembledLaunchBundle,
    assemble_launch_bundle,
    calculate_initial_buy_tokens,
)
from rugbot.execution.launch.exit_controller import (
    DEFAULT_DEAD_LAUNCH_TIMEOUT_SECONDS,
    DEFAULT_TP_LEVELS,
    DEFAULT_TRAILING_STOP_PCT,
    ExitAction,
    LaunchExitSignal,
    LaunchPositionState,
    build_launch_sell_instructions,
)
from rugbot.execution.launch.metadata_generator import (
    TokenMetadata,
    generate_metadata,
    generate_template_metadata,
    sanitize_name,
    sanitize_ticker,
    upload_metadata_to_ipfs,
)

__all__ = [
    "DEFAULT_DEAD_LAUNCH_TIMEOUT_SECONDS",
    "DEFAULT_TP_LEVELS",
    "DEFAULT_TRAILING_STOP_PCT",
    "AssembledLaunchBundle",
    "ExitAction",
    "LaunchExitSignal",
    "LaunchPositionState",
    "TokenMetadata",
    "assemble_launch_bundle",
    "build_launch_sell_instructions",
    "calculate_initial_buy_tokens",
    "generate_metadata",
    "generate_template_metadata",
    "sanitize_name",
    "sanitize_ticker",
    "upload_metadata_to_ipfs",
]
