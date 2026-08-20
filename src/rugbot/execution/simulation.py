"""RPC simulation gate for unsigned transaction variants."""

# The public function keeps the simulation limits together as one gate.
# ruff: noqa: PLR0913, TRY003, TC001, TC002

from __future__ import annotations

import base64
from dataclasses import dataclass
from typing import Any

from solders.hash import Hash
from solders.instruction import Instruction
from solders.message import Message
from solders.pubkey import Pubkey
from solders.signature import Signature
from solders.transaction import Transaction

from rugbot.execution.rpc_client import SolanaClient


@dataclass(frozen=True, slots=True)
class SimulationResult:
    """Validated response from Solana ``simulateTransaction``."""

    accepted: bool
    err: object | None
    units_consumed: int | None
    loaded_accounts_data_size: int | None
    logs: tuple[str, ...]


class SimulationError(ValueError):
    """Raised when a transaction cannot pass the simulation gate."""


async def simulate_unsigned_transaction(
    client: SolanaClient,
    *,
    payer: Pubkey,
    instructions: tuple[Instruction, ...],
    recent_blockhash: Hash,
    commitment: str = "finalized",
    max_compute_units: int | None = None,
    max_loaded_accounts_data_size: int | None = None,
) -> SimulationResult:
    """Simulate one unsigned transaction without replacing its blockhash.

    Default signatures create the required signature slots while
    ``sigVerify=false`` tells the RPC node not to verify those placeholders.
    The transaction is never submitted by this function.
    """

    message = Message.new_with_blockhash(list(instructions), payer, recent_blockhash)
    unsigned = Transaction.populate(
        message,
        [Signature.default()] * message.header.num_required_signatures,
    )
    encoded = base64.b64encode(bytes(unsigned)).decode("ascii")
    response = await client.post_rpc(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "simulateTransaction",
            "params": [
                encoded,
                {
                    "encoding": "base64",
                    "commitment": commitment,
                    "sigVerify": False,
                    "replaceRecentBlockhash": False,
                },
            ],
        }
    )
    result = _simulation_result(response)
    if not result.accepted:
        raise SimulationError(f"simulation failed: {result.err}")
    if (
        max_compute_units is not None
        and result.units_consumed is not None
        and result.units_consumed > max_compute_units
    ):
        raise SimulationError("simulation exceeded the configured compute-unit limit")
    if (
        max_loaded_accounts_data_size is not None
        and result.loaded_accounts_data_size is not None
        and result.loaded_accounts_data_size > max_loaded_accounts_data_size
    ):
        raise SimulationError(
            "simulation exceeded the configured loaded-account data limit"
        )
    return result


def _simulation_result(response: dict[str, Any] | None) -> SimulationResult:
    if not isinstance(response, dict):
        raise SimulationError("simulation RPC response is malformed")
    if response.get("error") is not None:
        raise SimulationError(f"simulation RPC returned an error: {response['error']}")
    result = response.get("result")
    if not isinstance(result, dict):
        raise SimulationError("simulation RPC result is malformed")
    raw_logs = result.get("logs")
    logs = (
        tuple(item for item in raw_logs if isinstance(item, str))
        if isinstance(raw_logs, list)
        else ()
    )
    units = result.get("unitsConsumed")
    loaded = result.get("loadedAccountsDataSize")
    units_value = units if type(units) is int and units >= 0 else None
    loaded_value = loaded if type(loaded) is int and loaded >= 0 else None
    error = result.get("err")
    return SimulationResult(
        accepted=error is None,
        err=error,
        units_consumed=units_value,
        loaded_accounts_data_size=loaded_value,
        logs=logs,
    )


__all__ = [
    "SimulationError",
    "SimulationResult",
    "simulate_unsigned_transaction",
]
