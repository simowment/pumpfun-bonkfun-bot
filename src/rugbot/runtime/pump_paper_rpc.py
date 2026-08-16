"""Read-only finalized account acquisition for Pump paper resolution.

This module is deliberately an acquisition boundary.  It does not decode
account layouts, resolve protocol versions, construct quotes, or submit
transactions.  The pure protocol modules consume the immutable observations
returned here.
"""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, TypeAlias

from rugbot.domain.decisions import AbstainReason, AbstainResult
from rugbot.domain.launches import LaunchCreatedV2
from rugbot.domain.observations import RawChainObservation
from rugbot.ingest.rpc_account_observer import (
    AccountObservationResult,
    observe_account_info,
)
from rugbot.protocol.pump.create_decoder import CREATE_V2_ACCOUNT_NAMES
from rugbot.protocol.pump.fee_config_account import PUMP_FEE_CONFIG_PDA

if TYPE_CHECKING:
    from uuid import UUID

    from rugbot.ingest.rpc_observer import RpcHttpTransport


@dataclass(frozen=True, slots=True)
class PumpPaperAccountObservations:
    """Exact finalized account observations needed by the pure paper resolver.

    The fields contain raw observations rather than decoded protocol objects.
    Decoding and protocol/fee resolution remain in the pure modules so the
    online and replay paths consume the same evidence contract.
    """

    as_of_slot: int
    global_account: RawChainObservation
    fee_config_account: RawChainObservation
    mint_account: RawChainObservation


PumpPaperRpcResult: TypeAlias = PumpPaperAccountObservations | AbstainResult
AccountObserver: TypeAlias = Callable[
    ..., AccountObservationResult | Awaitable[AccountObservationResult]
]


async def resolve_pump_paper_accounts(  # noqa: PLR0913
    *,
    launch: LaunchCreatedV2,
    create_observation: RawChainObservation,
    endpoint: str,
    source_id: str = "solana-http-rpc-account-info",
    observer_id: str = "pump-paper-account-resolver",
    boot_id: UUID | None = None,
    receive_sequence_start: int = 0,
    transport: RpcHttpTransport | None = None,
    observer: AccountObserver | None = None,
) -> PumpPaperRpcResult:
    """Fetch the exact finalized Pump accounts required for paper resolution.

    ``launch`` must have been produced by the pinned finalized ``create_v2``
    decoder and ``create_observation`` must be the matching canonical
    transaction observation.  Every account request uses the launch slot as
    ``as_of_slot``; a response at a newer (or otherwise different) context
    slot is rejected and never enters the pure resolver.

    No signing key is loaded and no transaction-capable RPC method is called.
    """

    validation = _validate_create_evidence(launch, create_observation)
    if validation is not None:
        return validation

    addresses = _required_addresses(launch)
    if isinstance(addresses, AbstainResult):
        return addresses

    account_observer = observer or observe_account_info
    observations: dict[str, RawChainObservation] = {}
    for sequence_offset, (role, address) in enumerate(addresses):
        result = account_observer(
            address,
            endpoint=endpoint,
            source_id=source_id,
            observer_id=observer_id,
            boot_id=boot_id,
            receive_sequence_start=receive_sequence_start + sequence_offset,
            transport=transport,
            as_of_slot=launch.as_of_slot,
        )
        if inspect.isawaitable(result):
            result = await result
        if isinstance(result, AbstainResult):
            return result
        if type(result) is not RawChainObservation:
            return _abstain(
                AbstainReason.UNKNOWN_PROTOCOL_STATE,
                f"account observer returned malformed {role} evidence",
                launch.as_of_slot,
            )
        if result.slot != launch.as_of_slot:
            return _abstain(
                AbstainReason.STALE_STATE,
                f"{role} account context slot does not match the create slot",
                result.slot,
            )
        observations[role] = result

    return PumpPaperAccountObservations(
        as_of_slot=launch.as_of_slot,
        global_account=observations["global"],
        fee_config_account=observations["fee_config"],
        mint_account=observations["mint"],
    )


def _validate_create_evidence(
    launch: object,
    observation: object,
) -> AbstainResult | None:
    if (
        type(launch) is not LaunchCreatedV2
        or type(observation) is not RawChainObservation
    ):
        return _abstain(
            AbstainReason.UNKNOWN_PROTOCOL_STATE,
            "finalized Pump create evidence is required",
            -1,
        )
    if (
        type(launch.as_of_slot) is not int
        or launch.as_of_slot < 0
        or observation.slot != launch.as_of_slot
    ):
        return _abstain(
            AbstainReason.STALE_STATE,
            "launch and create observation must use the same slot",
            observation.slot,
        )
    if (
        observation.commitment != "finalized"
        or observation.canonical_status != "canonical"
        or observation.source_update_kind != "transaction"
    ):
        return _abstain(
            AbstainReason.STALE_STATE,
            "Pump paper account resolution requires finalized canonical create evidence",
            observation.slot,
        )
    if (
        launch.instruction_name != "create_v2"
        or launch.creation_instruction_type != "create_v2"
        or launch.decoder_version != "pump-create-v2-instruction-v1"
        or not launch.idl_hash
    ):
        return _abstain(
            AbstainReason.DECODER_MISMATCH,
            "pinned finalized Pump create_v2 evidence is required",
            launch.as_of_slot,
        )
    if (
        launch.transaction_index is None
        or observation.transaction_index != launch.transaction_index
        or (
            launch.signature is not None
            and observation.signature is not None
            and launch.signature != observation.signature
        )
    ):
        return _abstain(
            AbstainReason.UNKNOWN_PROTOCOL_STATE,
            "launch and create observation identity does not match",
            launch.as_of_slot,
        )
    return None


def _required_addresses(
    launch: LaunchCreatedV2,
) -> tuple[tuple[str, str], ...] | AbstainResult:
    if launch.required_account_names != CREATE_V2_ACCOUNT_NAMES:
        return _abstain(
            AbstainReason.DECODER_MISMATCH,
            "create_v2 account-role layout is not the pinned layout",
            launch.as_of_slot,
        )

    global_address = _address_at(
        launch,
        launch.global_account_index,
        role="global",
    )
    if isinstance(global_address, AbstainResult):
        return global_address
    if global_address != _address_at(
        launch,
        launch.account_indices[launch.global_account_index]
        if 0 <= launch.global_account_index < len(launch.account_indices)
        else -1,
        role="global",
    ):
        return _abstain(
            AbstainReason.UNKNOWN_PROTOCOL_STATE,
            "global account index provenance is inconsistent",
            launch.as_of_slot,
        )

    mint_address = _address_at(launch, launch.mint_account_index, role="mint")
    if isinstance(mint_address, AbstainResult):
        return mint_address
    if mint_address != launch.mint_pubkey:
        return _abstain(
            AbstainReason.UNKNOWN_PROTOCOL_STATE,
            "mint account index provenance is inconsistent",
            launch.as_of_slot,
        )

    return (
        ("global", global_address),
        ("fee_config", PUMP_FEE_CONFIG_PDA),
        ("mint", mint_address),
    )


def _address_at(
    launch: LaunchCreatedV2,
    account_index: object,
    *,
    role: str,
) -> str | AbstainResult:
    if type(account_index) is not int or not 0 <= account_index < len(
        launch.account_pubkeys
    ):
        return _abstain(
            AbstainReason.UNKNOWN_PROTOCOL_STATE,
            f"{role} account index is outside the proven create account list",
            launch.as_of_slot,
        )
    address = launch.account_pubkeys[account_index]
    if type(address) is not str or not address:
        return _abstain(
            AbstainReason.UNKNOWN_PROTOCOL_STATE,
            f"{role} account address is missing from create evidence",
            launch.as_of_slot,
        )
    return address


def _abstain(
    reason: AbstainReason,
    message: str,
    as_of_slot: int,
) -> AbstainResult:
    return AbstainResult(reason=reason, message=message, as_of_slot=as_of_slot)


__all__ = [
    "AccountObserver",
    "PumpPaperAccountObservations",
    "PumpPaperRpcResult",
    "resolve_pump_paper_accounts",
]
