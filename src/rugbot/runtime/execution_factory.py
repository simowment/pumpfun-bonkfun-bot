"""Canonical execution-port composition for runtime entrypoints."""

# Configuration failures are operator-facing boundary errors.
# ruff: noqa: TRY003

from __future__ import annotations

import os
from typing import TYPE_CHECKING

from rugbot.execution.live import LivePumpExecutionPort
from rugbot.execution.observe import ObserveExecutionPort
from rugbot.execution.sender import JitoSender, RoutingPolicy
from rugbot.runtime.config import ExecutionMode, SniperExecution
from rugbot.simulation.paper import PaperExecutionPort
from rugbot.simulation.route_simulation import SimulationPumpExecutionPort

if TYPE_CHECKING:
    from pathlib import Path

    from rugbot.execution.ports import ExecutionPort


def build_execution_port(
    mode: ExecutionMode,
    endpoint: str,
    *,
    expected_signer_pubkey: str | None = None,
    execution: SniperExecution | None = None,
    transaction_state_path: Path | None = None,
) -> ExecutionPort:
    """Construct one mode-specific execution port with strict signer gates."""

    if mode is ExecutionMode.OBSERVE:
        return ObserveExecutionPort()
    if mode is ExecutionMode.PAPER:
        return PaperExecutionPort()
    if mode in (ExecutionMode.SIMULATION, ExecutionMode.DRY_RUN):
        settings = execution or SniperExecution(
            mode=mode,
            quote_size_lamports=1,
        )
        signer = expected_signer_pubkey or "11111111111111111111111111111111"
        return SimulationPumpExecutionPort(
            endpoint=endpoint,
            signer_pubkey=signer,
            fixed_priority_fee_microlamports=settings.priority_fee_microlamports,
            jito_tip_lamports=settings.jito_tip_lamports,
            compute_unit_limit=settings.compute_unit_limit,
            loaded_accounts_data_size_limit=settings.loaded_accounts_data_size_limit,
            routing_policy=RoutingPolicy(settings.routing_policy),
            jito_block_engine_url=settings.jito_block_engine_url
            or JitoSender.DEFAULT_BLOCK_ENGINE_URL,
        )
    if mode is ExecutionMode.LIVE:
        private_key = os.environ.get("SOLANA_PRIVATE_KEY")
        if not private_key:
            raise ValueError("SOLANA_PRIVATE_KEY is required for live execution")
        if expected_signer_pubkey is None:
            raise ValueError("execution.signer_pubkey is required for live execution")
        settings = execution or SniperExecution(
            mode=ExecutionMode.LIVE,
            quote_size_lamports=1,
        )
        port = LivePumpExecutionPort(
            endpoint=endpoint,
            private_key=private_key,
            fixed_priority_fee_microlamports=settings.priority_fee_microlamports,
            jito_tip_lamports=settings.jito_tip_lamports,
            compute_unit_limit=settings.compute_unit_limit,
            loaded_accounts_data_size_limit=settings.loaded_accounts_data_size_limit,
            routing_policy=RoutingPolicy(settings.routing_policy),
            jito_block_engine_url=settings.jito_block_engine_url
            or JitoSender.DEFAULT_BLOCK_ENGINE_URL,
            transaction_state_path=transaction_state_path,
        )
        if port.signer_pubkey != expected_signer_pubkey:
            raise ValueError(
                "configured signer_pubkey does not match SOLANA_PRIVATE_KEY"
            )
        return port
    raise ValueError(f"unsupported execution mode: {mode}")


__all__ = ["build_execution_port"]
