"""Bounded finalized JSON-RPC account-info observation ingestion."""

import base64
import binascii
import inspect
import json
from time import monotonic_ns, time_ns
from typing import TypeAlias
from urllib.parse import urlsplit
from uuid import UUID, uuid4

import base58
from sol_trade_sdk.solana.provider_pool import (
    AiohttpRpcTransport,
    RpcHttpResponse,
    RpcHttpTransport,
)

from rugbot.domain.decisions import AbstainReason, AbstainResult
from rugbot.domain.observations import RawChainObservation

FINALIZED = "finalized"
ACCOUNT_INFO_ENCODING = "base64"
HTTP_OK = 200
SOLANA_ADDRESS_BYTES = 32
ACCOUNT_DATA_PARTS = 2

AccountObservationResult: TypeAlias = RawChainObservation | AbstainResult
AccountObservationsResult: TypeAlias = tuple[RawChainObservation, ...] | AbstainResult
_ACCOUNT_INFO_METHODS = frozenset({"getAccountInfo"})
_MULTIPLE_ACCOUNT_INFO_METHODS = frozenset({"getMultipleAccounts"})
MAX_MULTIPLE_ACCOUNT_ADDRESSES = 100


async def observe_multiple_account_info(  # noqa: PLR0911, PLR0913
    addresses: tuple[str, ...],
    *,
    endpoint: str,
    source_id: str = "solana-http-rpc-multiple-account-info",
    observer_id: str = "rpc-multiple-account-observer",
    boot_id: UUID | None = None,
    receive_sequence_start: int = 0,
    transport: RpcHttpTransport | None = None,
    as_of_slot: int | None = None,
) -> AccountObservationsResult:
    """Fetch a coherent finalized account batch for one context slot.

    ``getMultipleAccounts`` gives every returned account the same RPC context
    slot. That property is required when a pure resolver combines protocol,
    fee, mint, and curve bytes into one paper quote context. A requested slot
    is still an exact requirement; ``minContextSlot`` never turns a newer
    response into historical evidence.
    """

    validation = _validate_multiple_inputs(
        addresses=addresses,
        endpoint=endpoint,
        source_id=source_id,
        observer_id=observer_id,
        boot_id=boot_id,
        receive_sequence_start=receive_sequence_start,
        as_of_slot=as_of_slot,
    )
    if validation is not None:
        return validation

    result = await _read_multiple_account_info(
        transport or AiohttpRpcTransport(),
        endpoint=endpoint,
        addresses=addresses,
        as_of_slot=as_of_slot,
    )
    if isinstance(result, AbstainResult):
        return result

    slot, values, response_body = result
    resolved_boot_id = boot_id or uuid4()
    observations: list[RawChainObservation] = []
    for offset, (address, value) in enumerate(zip(addresses, values, strict=True)):
        if type(value) is not dict:
            return _abstain(
                AbstainReason.MISSING_FEATURE,
                "getMultipleAccounts returned missing account data",
                slot,
            )
        owner = value.get("owner")
        data = value.get("data")
        if type(owner) is not str or not owner or "data" not in value:
            return _abstain(
                AbstainReason.MISSING_FEATURE,
                "getMultipleAccounts returned incomplete account identity",
                slot,
            )
        account_data = _validated_account_data(
            (slot, owner, data),
            as_of_slot=slot,
        )
        if isinstance(account_data, AbstainResult):
            return account_data
        owner_program_id = _decode_pubkey(
            owner,
            field_name="owner",
            as_of_slot=slot,
        )
        if isinstance(owner_program_id, AbstainResult):
            return owner_program_id
        observations.append(
            RawChainObservation(
                raw_id=uuid4(),
                source_id=source_id,
                observer_id=observer_id,
                boot_id=resolved_boot_id,
                receive_sequence=receive_sequence_start + offset + 1,
                slot=slot,
                parent_slot=None,
                blockhash=None,
                signature=None,
                transaction_index=None,
                outer_instruction_index=None,
                inner_instruction_group_index=None,
                inner_instruction_index=None,
                stack_height=None,
                event_ordinal=None,
                commitment=FINALIZED,
                canonical_status="canonical",
                received_wall_ns=time_ns(),
                received_monotonic_ns=monotonic_ns(),
                program_id=None,
                account_pubkey=bytes(base58.b58decode(address)),
                account_owner_program_id=owner_program_id,
                raw_transaction=None,
                raw_transaction_format=None,
                raw_account_data=account_data,
                account_write_version=None,
                source_update_kind="account",
                raw_source_status=None,
                raw_source_payload=response_body,
                decoder_name=None,
                decoder_version=None,
                idl_hash=None,
            )
        )
    return tuple(observations)


async def observe_account_info(  # noqa: PLR0913
    address: str,
    *,
    endpoint: str,
    source_id: str = "solana-http-rpc-account-info",
    observer_id: str = "rpc-account-observer",
    boot_id: UUID | None = None,
    receive_sequence_start: int = 0,
    transport: RpcHttpTransport | None = None,
    as_of_slot: int | None = None,
) -> AccountObservationResult:
    """Fetch one finalized account and retain its raw state for replay.

    Args:
        address: Base58-encoded Solana account address.
        endpoint: HTTPS JSON-RPC endpoint.
        source_id: Logical source identifier for the observation.
        observer_id: Process or host identifier for the observation.
        boot_id: Process boot identifier; generated when omitted.
        receive_sequence_start: Last receive sequence for this source.
        transport: Optional injected HTTP transport for tests.
        as_of_slot: Exact finalized slot required for the account context. A
            newer context is rejected rather than treated as historical state.

    Returns:
        One immutable raw account observation, or a typed abstention when the
        response cannot prove finalized account identity and bytes.
    """

    validation = _validate_inputs(
        address=address,
        endpoint=endpoint,
        source_id=source_id,
        observer_id=observer_id,
        boot_id=boot_id,
        receive_sequence_start=receive_sequence_start,
        as_of_slot=as_of_slot,
    )
    if validation is not None:
        return validation

    result = await _read_account_info(
        transport or AiohttpRpcTransport(),
        endpoint=endpoint,
        address=address,
        as_of_slot=as_of_slot,
    )
    if isinstance(result, AbstainResult):
        return result

    account, response_body = result
    account_data = _validated_account_data(account, as_of_slot=account[0])
    if isinstance(account_data, AbstainResult):
        return account_data
    owner_program_id = _decode_pubkey(
        account[1],
        field_name="owner",
        as_of_slot=account[0],
    )
    if isinstance(owner_program_id, AbstainResult):
        return owner_program_id

    account_pubkey = bytes(base58.b58decode(address))
    resolved_boot_id = boot_id or uuid4()
    return RawChainObservation(
        raw_id=uuid4(),
        source_id=source_id,
        observer_id=observer_id,
        boot_id=resolved_boot_id,
        receive_sequence=receive_sequence_start + 1,
        slot=account[0],
        parent_slot=None,
        blockhash=None,
        signature=None,
        transaction_index=None,
        outer_instruction_index=None,
        inner_instruction_group_index=None,
        inner_instruction_index=None,
        stack_height=None,
        event_ordinal=None,
        commitment=FINALIZED,
        canonical_status="canonical",
        received_wall_ns=time_ns(),
        received_monotonic_ns=monotonic_ns(),
        program_id=None,
        account_pubkey=account_pubkey,
        account_owner_program_id=owner_program_id,
        raw_transaction=None,
        raw_transaction_format=None,
        raw_account_data=account_data,
        account_write_version=None,
        source_update_kind="account",
        raw_source_status=None,
        raw_source_payload=response_body,
        decoder_name=None,
        decoder_version=None,
        idl_hash=None,
    )


async def _read_account_info(
    transport: RpcHttpTransport,
    *,
    endpoint: str,
    address: str,
    as_of_slot: int | None,
) -> tuple[tuple[int, str, object], bytes] | AbstainResult:
    method = "getAccountInfo"
    if method not in _ACCOUNT_INFO_METHODS:
        return _abstain(
            AbstainReason.UNKNOWN_PROTOCOL_STATE,
            "observer attempted a non-read-only RPC method",
            as_of_slot=-1,
        )

    config: dict[str, object] = {
        "commitment": FINALIZED,
        "encoding": ACCOUNT_INFO_ENCODING,
    }
    if as_of_slot is not None:
        config["minContextSlot"] = as_of_slot
    request_body = _request_body(method, (address, config))
    try:
        response_or_awaitable = transport(endpoint, request_body)
        response = (
            await response_or_awaitable
            if inspect.isawaitable(response_or_awaitable)
            else response_or_awaitable
        )
    except Exception as error:  # noqa: BLE001
        return _abstain(
            AbstainReason.MISSING_FEATURE,
            f"{method} transport failed: {type(error).__name__}",
            as_of_slot=-1,
        )

    if type(response) is not RpcHttpResponse:
        return _abstain(
            AbstainReason.UNKNOWN_PROTOCOL_STATE,
            f"{method} transport returned malformed response",
            as_of_slot=-1,
        )
    if response.status != HTTP_OK or type(response.body) is not bytes:
        return _abstain(
            AbstainReason.MISSING_FEATURE,
            f"{method} returned incomplete HTTP evidence",
            as_of_slot=-1,
        )

    decoded = _decode_rpc_response(response.body, method=method)
    if isinstance(decoded, AbstainResult):
        return decoded
    result, response_body = decoded
    return _validated_result(
        result,
        response_body=response_body,
        expected_slot=as_of_slot,
    )


async def _read_multiple_account_info(  # noqa: C901, PLR0911
    transport: RpcHttpTransport,
    *,
    endpoint: str,
    addresses: tuple[str, ...],
    as_of_slot: int | None,
) -> tuple[int, tuple[object, ...], bytes] | AbstainResult:
    method = "getMultipleAccounts"
    if method not in _MULTIPLE_ACCOUNT_INFO_METHODS:
        return _abstain(
            AbstainReason.UNKNOWN_PROTOCOL_STATE,
            "observer attempted a non-read-only RPC method",
            as_of_slot=as_of_slot if as_of_slot is not None else -1,
        )

    config: dict[str, object] = {
        "commitment": FINALIZED,
        "encoding": ACCOUNT_INFO_ENCODING,
    }
    if as_of_slot is not None:
        config["minContextSlot"] = as_of_slot
    request_body = _request_body(method, (list(addresses), config))
    try:
        response_or_awaitable = transport(endpoint, request_body)
        response = (
            await response_or_awaitable
            if inspect.isawaitable(response_or_awaitable)
            else response_or_awaitable
        )
    except Exception as error:  # noqa: BLE001
        return _abstain(
            AbstainReason.MISSING_FEATURE,
            f"{method} transport failed: {type(error).__name__}",
            as_of_slot=as_of_slot if as_of_slot is not None else -1,
        )

    if type(response) is not RpcHttpResponse:
        return _abstain(
            AbstainReason.UNKNOWN_PROTOCOL_STATE,
            f"{method} transport returned malformed response",
            as_of_slot=as_of_slot if as_of_slot is not None else -1,
        )
    if response.status != HTTP_OK or type(response.body) is not bytes:
        return _abstain(
            AbstainReason.MISSING_FEATURE,
            f"{method} returned incomplete HTTP evidence",
            as_of_slot=as_of_slot if as_of_slot is not None else -1,
        )

    decoded = _decode_rpc_response(response.body, method=method)
    if isinstance(decoded, AbstainResult):
        return decoded
    result, response_body = decoded
    if type(result) is not dict:
        return _abstain(
            AbstainReason.MISSING_FEATURE,
            f"{method} returned no account result",
            as_of_slot=as_of_slot if as_of_slot is not None else -1,
        )
    context = result.get("context")
    values = result.get("value")
    if type(context) is not dict or type(context.get("slot")) is not int:
        return _abstain(
            AbstainReason.MISSING_FEATURE,
            f"{method} returned no account context",
            as_of_slot=as_of_slot if as_of_slot is not None else -1,
        )
    slot = context["slot"]
    if slot < 0:
        return _abstain(
            AbstainReason.UNKNOWN_PROTOCOL_STATE,
            f"{method} returned an invalid account context slot",
            as_of_slot=-1,
        )
    if as_of_slot is not None and slot != as_of_slot:
        return _abstain(
            AbstainReason.STALE_STATE,
            f"{method} context slot does not match the requested slot",
            as_of_slot=slot,
        )
    if context.get("commitment", FINALIZED) != FINALIZED:
        return _abstain(
            AbstainReason.STALE_STATE,
            f"{method} returned a non-finalized account context",
            as_of_slot=slot,
        )
    if type(values) is not list or len(values) != len(addresses):
        return _abstain(
            AbstainReason.MISSING_FEATURE,
            f"{method} returned an incomplete account list",
            as_of_slot=slot,
        )
    return slot, tuple(values), response_body


def _validated_result(  # noqa: PLR0911
    result: object,
    *,
    response_body: bytes,
    expected_slot: int | None,
) -> tuple[tuple[int, str, object], bytes] | AbstainResult:
    if type(result) is not dict:
        return _abstain(
            AbstainReason.MISSING_FEATURE,
            "getAccountInfo returned no account result",
            as_of_slot=-1,
        )

    context = result.get("context")
    value = result.get("value")
    if type(context) is not dict or "slot" not in context:
        return _abstain(
            AbstainReason.MISSING_FEATURE,
            "getAccountInfo returned no account context",
            as_of_slot=-1,
        )
    slot = context["slot"]
    if type(slot) is not int or slot < 0:
        return _abstain(
            AbstainReason.UNKNOWN_PROTOCOL_STATE,
            "getAccountInfo returned an invalid account context slot",
            as_of_slot=-1,
        )
    if expected_slot is not None and slot != expected_slot:
        return _abstain(
            AbstainReason.STALE_STATE,
            "getAccountInfo context slot does not match the requested slot",
            as_of_slot=slot,
        )
    if "commitment" in context and context["commitment"] != FINALIZED:
        return _abstain(
            AbstainReason.STALE_STATE,
            "getAccountInfo returned a non-finalized account context",
            as_of_slot=slot,
        )
    if type(value) is not dict:
        return _abstain(
            AbstainReason.MISSING_FEATURE,
            "getAccountInfo returned missing account data",
            as_of_slot=slot,
        )
    owner = value.get("owner")
    if type(owner) is not str or not owner:
        return _abstain(
            AbstainReason.MISSING_FEATURE,
            "getAccountInfo returned no account owner",
            as_of_slot=slot,
        )
    if "data" not in value:
        return _abstain(
            AbstainReason.MISSING_FEATURE,
            "getAccountInfo returned no raw account data",
            as_of_slot=slot,
        )
    return (slot, owner, value["data"]), response_body


def _validated_account_data(
    account: tuple[int, str, object],
    *,
    as_of_slot: int,
) -> bytes | AbstainResult:
    data = account[2]
    if type(data) is not list or len(data) != ACCOUNT_DATA_PARTS:
        return _abstain(
            AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
            "getAccountInfo returned an unsupported account encoding",
            as_of_slot=as_of_slot,
        )
    encoded_data, encoding = data
    if encoding != ACCOUNT_INFO_ENCODING:
        return _abstain(
            AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
            "getAccountInfo returned an unsupported account encoding",
            as_of_slot=as_of_slot,
        )
    if type(encoded_data) is not str:
        return _abstain(
            AbstainReason.UNKNOWN_PROTOCOL_STATE,
            "getAccountInfo returned malformed raw account data",
            as_of_slot=as_of_slot,
        )
    try:
        return bytes(base64.b64decode(encoded_data, validate=True))
    except (binascii.Error, UnicodeEncodeError, ValueError):
        return _abstain(
            AbstainReason.UNKNOWN_PROTOCOL_STATE,
            "getAccountInfo returned malformed base64 account data",
            as_of_slot=as_of_slot,
        )


def _decode_pubkey(
    value: str,
    *,
    field_name: str,
    as_of_slot: int,
) -> bytes | AbstainResult:
    try:
        decoded = bytes(base58.b58decode(value))
    except ValueError:
        return _abstain(
            AbstainReason.UNKNOWN_PROTOCOL_STATE,
            f"getAccountInfo returned an invalid {field_name} pubkey",
            as_of_slot=as_of_slot,
        )
    if len(decoded) != SOLANA_ADDRESS_BYTES:
        return _abstain(
            AbstainReason.UNKNOWN_PROTOCOL_STATE,
            f"getAccountInfo returned an invalid {field_name} pubkey",
            as_of_slot=as_of_slot,
        )
    return decoded


def _decode_rpc_response(
    body: bytes,
    *,
    method: str,
) -> tuple[object, bytes] | AbstainResult:
    try:
        payload = json.loads(body, object_pairs_hook=_strict_json_object)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
        return _abstain(
            AbstainReason.UNKNOWN_PROTOCOL_STATE,
            f"{method} returned invalid JSON-RPC evidence",
            as_of_slot=-1,
        )
    if type(payload) is not dict:
        return _abstain(
            AbstainReason.UNKNOWN_PROTOCOL_STATE,
            f"{method} returned a non-object JSON-RPC payload",
            as_of_slot=-1,
        )
    if (
        payload.get("jsonrpc") != "2.0"
        or type(payload.get("id")) is not int
        or payload.get("id") != 1
    ):
        return _abstain(
            AbstainReason.UNKNOWN_PROTOCOL_STATE,
            f"{method} returned an invalid JSON-RPC envelope",
            as_of_slot=-1,
        )
    if "error" in payload or "result" not in payload:
        return _abstain(
            AbstainReason.MISSING_FEATURE,
            f"{method} returned incomplete JSON-RPC evidence",
            as_of_slot=-1,
        )
    return payload["result"], body


def _request_body(method: str, params: tuple[object, ...]) -> bytes:
    return json.dumps(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": method,
            "params": list(params),
        },
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _validate_inputs(  # noqa: PLR0913
    *,
    address: str,
    endpoint: str,
    source_id: str,
    observer_id: str,
    boot_id: UUID | None,
    receive_sequence_start: int,
    as_of_slot: int | None,
) -> AbstainResult | None:
    if not _valid_address(address):
        return _abstain(
            AbstainReason.UNKNOWN_PROTOCOL_STATE,
            "address is not a valid base58 Solana address",
            as_of_slot=-1,
        )
    if not _valid_http_endpoint(endpoint) or not _non_blank_str(source_id):
        return _abstain(
            AbstainReason.MISSING_FEATURE,
            "endpoint and source_id are required",
            as_of_slot=-1,
        )
    if not _non_blank_str(observer_id) or (
        boot_id is not None and type(boot_id) is not UUID
    ):
        return _abstain(
            AbstainReason.MISSING_FEATURE,
            "observer identity is incomplete",
            as_of_slot=-1,
        )
    if not _non_negative_int(receive_sequence_start):
        return _abstain(
            AbstainReason.MISSING_FEATURE,
            "receive sequence start is invalid",
            as_of_slot=-1,
        )
    if as_of_slot is not None and not _non_negative_int(as_of_slot):
        return _abstain(
            AbstainReason.MISSING_FEATURE,
            "as_of_slot must be a non-negative integer",
            as_of_slot=-1,
        )
    return None


def _validate_multiple_inputs(  # noqa: PLR0913
    *,
    addresses: object,
    endpoint: str,
    source_id: str,
    observer_id: str,
    boot_id: UUID | None,
    receive_sequence_start: int,
    as_of_slot: int | None,
) -> AbstainResult | None:
    if type(addresses) is not tuple or not addresses:
        return _abstain(
            AbstainReason.MISSING_FEATURE,
            "account address tuple is required",
            as_of_slot if type(as_of_slot) is int else -1,
        )
    if len(addresses) > MAX_MULTIPLE_ACCOUNT_ADDRESSES or any(
        not _valid_address(address) for address in addresses
    ):
        return _abstain(
            AbstainReason.UNKNOWN_PROTOCOL_STATE,
            "account address tuple is malformed or exceeds the batch bound",
            as_of_slot if type(as_of_slot) is int else -1,
        )
    return _validate_inputs(
        address=addresses[0],
        endpoint=endpoint,
        source_id=source_id,
        observer_id=observer_id,
        boot_id=boot_id,
        receive_sequence_start=receive_sequence_start,
        as_of_slot=as_of_slot,
    )


def _valid_address(value: object) -> bool:
    if type(value) is not str:
        return False
    try:
        return len(base58.b58decode(value)) == SOLANA_ADDRESS_BYTES
    except ValueError:
        return False


def _valid_http_endpoint(value: object) -> bool:
    if not _non_blank_str(value):
        return False
    try:
        parsed = urlsplit(value)
    except ValueError:
        return False
    return parsed.scheme == "https" and bool(parsed.netloc)


def _strict_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateJsonObjectKeyError
        result[key] = value
    return result


class _DuplicateJsonObjectKeyError(ValueError):
    """Raised when JSON evidence contains duplicate object keys."""


def _abstain(reason: AbstainReason, message: str, *, as_of_slot: int) -> AbstainResult:
    return AbstainResult(reason=reason, message=message, as_of_slot=as_of_slot)


def _non_negative_int(value: object) -> bool:
    return type(value) is int and value >= 0


def _non_blank_str(value: object) -> bool:
    return type(value) is str and bool(value.strip())


__all__ = [
    "AccountObservationResult",
    "AccountObservationsResult",
    "observe_account_info",
    "observe_multiple_account_info",
]
