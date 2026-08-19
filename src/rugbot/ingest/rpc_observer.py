"""Bounded finalized HTTP JSON-RPC observation ingestion."""

import asyncio
import inspect
import json
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from time import monotonic_ns, time_ns
from typing import TypeAlias
from urllib.parse import urlsplit
from uuid import UUID, uuid4

import aiohttp
import base58

from rugbot.domain.decisions import AbstainReason, AbstainResult
from rugbot.domain.observations import RawChainObservation

DEFAULT_MAX_SIGNATURES = 20
DEFAULT_MAX_TRANSACTIONS = 5
DEFAULT_MAX_PAGES = 10
MAX_SIGNATURES = 1000
MAX_TRANSACTIONS = 1000
MAX_PAGES = 100
MAX_RPC_RETRIES = 2
HTTP_OK = 200
HTTP_TOO_MANY_REQUESTS = 429
FINALIZED = "finalized"
JSON_TRANSACTION_FORMAT = "solana_json_rpc_getTransaction_json"
SOLANA_ADDRESS_BYTES = 32
SOLANA_SIGNATURE_BYTES = 64
HELIUS_SIGNATURE_ENTRY_FIELDS = 3
_RPC_METHODS = frozenset(
    {
        "getSlot",
        "getSignaturesForAddress",
        "getTransactionsForAddress",
        "getTransaction",
        "getBlock",
    }
)
SignatureHistoryEntry: TypeAlias = tuple[str, int] | tuple[str, int, int]


@dataclass(frozen=True, slots=True)
class _HeliusFullTransaction:
    signature: str
    slot: int
    transaction_index: int
    result: object
    response_body: bytes


@dataclass(frozen=True, slots=True)
class RpcHttpResponse:
    """Raw HTTP response returned by an injected RPC transport."""

    status: int
    body: bytes


RpcHttpTransport: TypeAlias = Callable[
    [str, bytes], Awaitable[RpcHttpResponse] | RpcHttpResponse
]
RpcObservationResult: TypeAlias = tuple[RawChainObservation, ...] | AbstainResult
FinalizedTransactionResult: TypeAlias = RawChainObservation | None | AbstainResult


class _InvalidHistoryCursorError(ValueError):
    """Raised when a history cursor cannot safely bound a poll."""

    @classmethod
    def invalid_address(cls) -> "_InvalidHistoryCursorError":
        return cls("address is not a valid Solana address")

    @classmethod
    def invalid_source_id(cls) -> "_InvalidHistoryCursorError":
        return cls("source_id is not a non-blank string")

    @classmethod
    def invalid_signature(cls) -> "_InvalidHistoryCursorError":
        return cls("until_signature is not a valid Solana signature")

    @classmethod
    def invalid_sequence(cls) -> "_InvalidHistoryCursorError":
        return cls("receive_sequence is not a non-negative integer")


@dataclass(frozen=True, slots=True)
class AddressHistoryCursor:
    """Checkpoint for one fully consumed finalized address history."""

    address: str
    source_id: str
    until_signature: str | None
    receive_sequence: int

    def __post_init__(self) -> None:
        """Reject a cursor that could not safely bound the next poll."""

        if not _valid_address(self.address):
            raise _InvalidHistoryCursorError.invalid_address()
        if not _non_blank_str(self.source_id):
            raise _InvalidHistoryCursorError.invalid_source_id()
        if self.until_signature is not None and not _valid_signature(
            self.until_signature
        ):
            raise _InvalidHistoryCursorError.invalid_signature()
        if not _non_negative_int(self.receive_sequence):
            raise _InvalidHistoryCursorError.invalid_sequence()


class _InvalidTransportConfigError(ValueError):
    def __init__(self) -> None:
        super().__init__("timeout_seconds must be a positive integer")


class _DuplicateJsonObjectKeyError(ValueError):
    def __init__(self) -> None:
        super().__init__("duplicate JSON object key")


class _FailedFinalizedTransaction:
    """Marker for a validated finalized transaction that did not execute."""


class AiohttpRpcTransport:
    """Small HTTP-only transport for read-only JSON-RPC requests."""

    def __init__(self, *, timeout_seconds: int = 10) -> None:
        """Initialize the transport with a bounded request timeout."""

        if type(timeout_seconds) is not int or timeout_seconds <= 0:
            raise _InvalidTransportConfigError
        self._timeout_seconds = timeout_seconds

    async def __call__(self, endpoint: str, body: bytes) -> RpcHttpResponse:
        """POST one raw JSON-RPC request and return its raw response bytes."""

        timeout = aiohttp.ClientTimeout(total=self._timeout_seconds)
        for attempt in range(MAX_RPC_RETRIES + 1):
            try:
                async with aiohttp.ClientSession(timeout=timeout) as session:
                    async with session.post(
                        endpoint,
                        data=body,
                        headers={"content-type": "application/json"},
                    ) as response:
                        if (
                            response.status == HTTP_TOO_MANY_REQUESTS
                            and attempt < MAX_RPC_RETRIES
                        ):
                            await asyncio.sleep(0.5 * (attempt + 1))
                            continue
                        return RpcHttpResponse(
                            status=response.status, body=await response.read()
                        )
            except Exception:
                if attempt < MAX_RPC_RETRIES:
                    await asyncio.sleep(0.5 * (attempt + 1))
                    continue
                raise
        return RpcHttpResponse(status=HTTP_TOO_MANY_REQUESTS, body=b"{}")


async def observe_address(  # noqa: C901, PLR0911, PLR0912, PLR0913
    address: str,
    *,
    endpoint: str,
    source_id: str = "solana-http-rpc",
    observer_id: str = "rpc-observer",
    boot_id: UUID | None = None,
    receive_sequence_start: int = 0,
    max_signatures: int = DEFAULT_MAX_SIGNATURES,
    max_transactions: int = DEFAULT_MAX_TRANSACTIONS,
    max_pages: int = DEFAULT_MAX_PAGES,
    start_slot: int | None = None,
    end_slot: int | None = None,
    cursor: AddressHistoryCursor | None = None,
    transport: RpcHttpTransport | None = None,
) -> RpcObservationResult:
    """Observe finalized transactions for one address using bounded HTTP RPC.

    The observer performs only finalized read-only RPC calls. Clear Helius RPC
    endpoints use `getTransactionsForAddress` for bounded signature discovery
    and transaction indices; all hydrated transaction bodies still come from
    finalized `getTransaction`. Other endpoints, and cursor-based polling,
    retain the standard `getSignaturesForAddress` path. The observer never
    decodes account layouts or submits transactions. Each emitted observation
    keeps the exact `getTransaction` response body in `raw_source_payload` for
    a later pinned decoder.

    Args:
        address: Base58-encoded Solana address to inspect.
        endpoint: HTTP JSON-RPC endpoint.
        source_id: Logical source identifier for emitted observations.
        observer_id: Process or host identifier for emitted observations.
        boot_id: Process boot identifier; generated when omitted.
        receive_sequence_start: Last receive sequence from the same source.
        max_signatures: Maximum signatures requested per history page.
        max_transactions: Maximum transactions fetched per history page.
        max_pages: Maximum finalized history pages to prove complete.
        start_slot: Inclusive lower bound for a complete slot-window history.
        end_slot: Inclusive upper bound for a complete slot-window history.
        cursor: Checkpoint from the last fully consumed history batch.
        transport: Optional injected HTTP transport for tests.

    Returns:
        An immutable tuple of raw observations, or a typed abstention when the
        finalized evidence is incomplete or inconsistent.
    """

    validation = _validate_inputs(
        address=address,
        endpoint=endpoint,
        source_id=source_id,
        observer_id=observer_id,
        boot_id=boot_id,
        receive_sequence_start=receive_sequence_start,
        max_signatures=max_signatures,
        max_transactions=max_transactions,
        max_pages=max_pages,
        start_slot=start_slot,
        end_slot=end_slot,
        cursor=cursor,
    )
    if validation is not None:
        return validation

    resolved_boot_id = boot_id or uuid4()
    effective_receive_sequence_start = (
        cursor.receive_sequence if cursor is not None else receive_sequence_start
    )
    rpc_transport = transport or AiohttpRpcTransport()
    finalized_slot_result = await _read_result(
        rpc_transport,
        endpoint=endpoint,
        method="getSlot",
        params=({"commitment": FINALIZED},),
        as_of_slot=-1,
    )
    if isinstance(finalized_slot_result, AbstainResult):
        return finalized_slot_result
    finalized_slot_result, _ = finalized_slot_result
    if not _non_negative_int(finalized_slot_result):
        return _abstain(
            AbstainReason.UNKNOWN_PROTOCOL_STATE,
            "getSlot returned an invalid finalized slot",
            as_of_slot=-1,
        )
    finalized_slot = finalized_slot_result

    preloaded_transactions: dict[str, _HeliusFullTransaction] = {}
    if _is_helius_endpoint(endpoint) and cursor is None:
        helius_history = await _read_helius_full_transaction_history(
            rpc_transport,
            endpoint=endpoint,
            address=address,
            finalized_slot=finalized_slot,
            page_limit=max_signatures,
            max_transactions=max_transactions,
            max_pages=max_pages,
            start_slot=start_slot,
            end_slot=end_slot,
        )
        if isinstance(helius_history, AbstainResult):
            return helius_history
        signatures, preloaded_transactions = helius_history
    else:
        signatures = await _read_signature_history(
            rpc_transport,
            endpoint=endpoint,
            address=address,
            finalized_slot=finalized_slot,
            page_limit=max_signatures,
            max_transactions=max_transactions,
            max_pages=max_pages,
            start_slot=start_slot,
            end_slot=end_slot,
            cursor=cursor,
        )
    if isinstance(signatures, AbstainResult):
        return signatures

    highest_signature_slot = max(
        (signature_entry[1] for signature_entry in signatures),
        default=finalized_slot,
    )
    finalized_slot = max(finalized_slot, highest_signature_slot)

    observations: list[RawChainObservation] = []
    block_transaction_indices: dict[int, dict[str, int]] = {}
    for sequence, signature_entry in enumerate(
        signatures,
        start=effective_receive_sequence_start + 1,
    ):
        signature = signature_entry[0]
        transaction_slot = signature_entry[1]
        helius_transaction_index = (
            signature_entry[2]
            if len(signature_entry) == HELIUS_SIGNATURE_ENTRY_FIELDS
            else None
        )
        preloaded = preloaded_transactions.get(signature)
        transaction_result = (
            (preloaded.result, preloaded.response_body)
            if preloaded is not None
            else await _read_result(
                rpc_transport,
                endpoint=endpoint,
                method="getTransaction",
                params=(
                    signature,
                    {
                        "encoding": "json",
                        "commitment": FINALIZED,
                        "maxSupportedTransactionVersion": 0,
                    },
                ),
                as_of_slot=finalized_slot,
            )
        )
        if isinstance(transaction_result, AbstainResult):
            return transaction_result
        transaction_result, response_body = transaction_result
        transaction = _validated_transaction(
            transaction_result,
            requested_signature=signature,
            expected_slot=transaction_slot,
            finalized_slot=finalized_slot,
            response_body=response_body,
        )
        if isinstance(transaction, AbstainResult):
            return transaction
        if isinstance(transaction, _FailedFinalizedTransaction):
            continue
        response_body, observed_slot = transaction
        transaction_index = helius_transaction_index
        if transaction_index is None:
            transaction_indices = block_transaction_indices.get(observed_slot)
            if transaction_indices is None:
                block_result = await _read_result(
                    rpc_transport,
                    endpoint=endpoint,
                    method="getBlock",
                    params=(
                        observed_slot,
                        {
                            "commitment": FINALIZED,
                            "maxSupportedTransactionVersion": 0,
                            "rewards": False,
                            "transactionDetails": "full",
                        },
                    ),
                    as_of_slot=finalized_slot,
                )
                if isinstance(block_result, AbstainResult):
                    return block_result
                block_payload, _ = block_result
                transaction_indices = _validated_block_transaction_indices(
                    block_payload,
                    as_of_slot=finalized_slot,
                )
                if isinstance(transaction_indices, AbstainResult):
                    return transaction_indices
                block_transaction_indices[observed_slot] = transaction_indices
            transaction_index = transaction_indices.get(signature)
            if transaction_index is None:
                return _abstain(
                    AbstainReason.UNKNOWN_PROTOCOL_STATE,
                    "finalized block did not contain requested transaction",
                    as_of_slot=finalized_slot,
                )
        received_wall_ns = time_ns()
        received_monotonic_ns = monotonic_ns()
        observations.append(
            RawChainObservation(
                raw_id=uuid4(),
                source_id=source_id,
                observer_id=observer_id,
                boot_id=resolved_boot_id,
                receive_sequence=sequence,
                slot=observed_slot,
                parent_slot=None,
                blockhash=None,
                signature=_decode_signature(signature),
                transaction_index=transaction_index,
                outer_instruction_index=None,
                inner_instruction_group_index=None,
                inner_instruction_index=None,
                stack_height=None,
                event_ordinal=None,
                commitment=FINALIZED,
                canonical_status="canonical",
                received_wall_ns=received_wall_ns,
                received_monotonic_ns=received_monotonic_ns,
                program_id=None,
                account_pubkey=None,
                account_owner_program_id=None,
                raw_transaction=response_body,
                raw_transaction_format=JSON_TRANSACTION_FORMAT,
                raw_account_data=None,
                account_write_version=None,
                source_update_kind="transaction",
                raw_source_status=None,
                raw_source_payload=response_body,
                decoder_name=None,
                decoder_version=None,
                idl_hash=None,
            )
        )
    return tuple(observations)


async def observe_finalized_transaction(  # noqa: C901, PLR0911, PLR0913
    signature: str,
    *,
    expected_slot: int,
    endpoint: str,
    source_id: str,
    observer_id: str = "rpc-observer",
    boot_id: UUID | None = None,
    receive_sequence: int = 1,
    transport: RpcHttpTransport | None = None,
) -> FinalizedTransactionResult:
    """Hydrate one streamed signature through finalized JSON-RPC evidence.

    A processed stream notification is only a trigger. The returned
    observation is emitted only after finalized ``getTransaction`` and
    finalized block ordering agree on the slot and transaction index.

    Returns ``None`` while finalization has not reached ``expected_slot`` or
    when the transaction failed, allowing a stream source to retry or skip.
    """

    if not _valid_signature(signature):
        return _abstain(
            AbstainReason.UNKNOWN_PROTOCOL_STATE,
            "stream notification contained an invalid signature",
            as_of_slot=expected_slot,
        )
    if not _non_negative_int(expected_slot):
        return _abstain(
            AbstainReason.UNKNOWN_PROTOCOL_STATE,
            "stream notification contained an invalid slot",
            as_of_slot=-1,
        )
    rpc_transport = transport or AiohttpRpcTransport()
    finalized_result = await _read_result(
        rpc_transport,
        endpoint=endpoint,
        method="getSlot",
        params=({"commitment": FINALIZED},),
        as_of_slot=expected_slot,
    )
    if isinstance(finalized_result, AbstainResult):
        return finalized_result
    finalized_slot, _ = finalized_result
    if not _non_negative_int(finalized_slot):
        return _abstain(
            AbstainReason.UNKNOWN_PROTOCOL_STATE,
            "getSlot returned an invalid finalized slot",
            as_of_slot=expected_slot,
        )
    if finalized_slot < expected_slot:
        return _abstain(
            AbstainReason.STALE_STATE,
            "finalized slot has not reached streamed transaction",
            as_of_slot=finalized_slot,
        )

    transaction_result = await _read_result(
        rpc_transport,
        endpoint=endpoint,
        method="getTransaction",
        params=(
            signature,
            {
                "commitment": FINALIZED,
                "maxSupportedTransactionVersion": 0,
            },
        ),
        as_of_slot=finalized_slot,
    )
    if isinstance(transaction_result, AbstainResult):
        return transaction_result
    transaction_payload, response_body = transaction_result
    validated_transaction = _validated_transaction(
        transaction_payload,
        requested_signature=signature,
        expected_slot=expected_slot,
        finalized_slot=finalized_slot,
        response_body=response_body,
    )
    if isinstance(validated_transaction, AbstainResult):
        return validated_transaction
    if isinstance(validated_transaction, _FailedFinalizedTransaction):
        return None

    block_result = await _read_result(
        rpc_transport,
        endpoint=endpoint,
        method="getBlock",
        params=(
            expected_slot,
            {
                "commitment": FINALIZED,
                "transactionDetails": "full",
                "rewards": False,
                "maxSupportedTransactionVersion": 0,
            },
        ),
        as_of_slot=finalized_slot,
    )
    if isinstance(block_result, AbstainResult):
        return block_result
    block_payload, _ = block_result
    transaction_indices = _validated_block_transaction_indices(
        block_payload,
        as_of_slot=finalized_slot,
    )
    if isinstance(transaction_indices, AbstainResult):
        return transaction_indices
    transaction_index = transaction_indices.get(signature)
    if transaction_index is None:
        return _abstain(
            AbstainReason.UNKNOWN_PROTOCOL_STATE,
            "finalized block did not contain streamed transaction",
            as_of_slot=finalized_slot,
        )

    resolved_boot_id = boot_id or uuid4()
    return RawChainObservation(
        raw_id=uuid4(),
        source_id=source_id,
        observer_id=observer_id,
        boot_id=resolved_boot_id,
        receive_sequence=receive_sequence,
        slot=expected_slot,
        parent_slot=None,
        blockhash=None,
        signature=_decode_signature(signature),
        transaction_index=transaction_index,
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
        account_pubkey=None,
        account_owner_program_id=None,
        raw_transaction=response_body,
        raw_transaction_format=JSON_TRANSACTION_FORMAT,
        raw_account_data=None,
        account_write_version=None,
        source_update_kind="transaction",
        raw_source_status=None,
        raw_source_payload=response_body,
        decoder_name=None,
        decoder_version=None,
        idl_hash=None,
    )


async def _read_signature_history(  # noqa: C901, PLR0911, PLR0912, PLR0913
    transport: RpcHttpTransport,
    *,
    endpoint: str,
    address: str,
    finalized_slot: int,
    page_limit: int,
    max_transactions: int,
    max_pages: int,
    start_slot: int | None,
    end_slot: int | None,
    cursor: AddressHistoryCursor | None,
) -> tuple[SignatureHistoryEntry, ...] | AbstainResult:
    """Read bounded finalized history without silently skipping evidence."""

    before_signature: str | None = None
    seen_signatures: set[str] = set()
    collected: list[tuple[str, int]] = []
    boundary = cursor.until_signature if cursor is not None else None
    previous_slot: int | None = None
    for _ in range(max_pages):
        options: dict[str, object] = {
            "commitment": FINALIZED,
            "limit": page_limit,
        }
        if boundary is not None and not _is_helius_endpoint(endpoint):
            # Helius' JSON-RPC proxy can return only the head row for an
            # ``until`` query. Fetch newest pages and locate the boundary
            # locally instead of turning a healthy poll into ABSTAIN.
            options["until"] = boundary
        if before_signature is not None:
            options["before"] = before_signature
        page_result = await _read_result(
            transport,
            endpoint=endpoint,
            method="getSignaturesForAddress",
            params=(address, options),
            as_of_slot=finalized_slot,
        )
        if isinstance(page_result, AbstainResult):
            return page_result
        page_payload, _ = page_result
        page = _validated_signature_entries(
            page_payload,
            max_signatures=page_limit,
            finalized_slot=finalized_slot,
            max_slot=None,
        )
        if isinstance(page, AbstainResult):
            return page
        for _, slot in page:
            if previous_slot is not None and slot > previous_slot:
                return _abstain(
                    AbstainReason.UNKNOWN_PROTOCOL_STATE,
                    "getSignaturesForAddress history was not newest-first",
                    as_of_slot=finalized_slot,
                )
            previous_slot = slot
        if boundary is None:
            if start_slot is None:
                return tuple(page[:max_transactions])

            for signature, slot in page:
                if signature in seen_signatures:
                    return _abstain(
                        AbstainReason.UNKNOWN_PROTOCOL_STATE,
                        "getSignaturesForAddress pagination repeated a signature",
                        as_of_slot=finalized_slot,
                    )
                seen_signatures.add(signature)
                if slot < start_slot:
                    return tuple(collected)
                if end_slot is not None and slot > end_slot:
                    continue
                remaining = max_transactions - len(collected)
                if remaining <= 0:
                    return tuple(collected)
                collected.append((signature, slot))
            if not page or len(page) < page_limit:
                return tuple(collected)
            before_signature = page[-1][0]
            continue
        if not page:
            return tuple(collected)

        boundary_found = False
        for signature, slot in page:
            if signature == boundary:
                boundary_found = True
                break
            if signature in seen_signatures:
                return _abstain(
                    AbstainReason.UNKNOWN_PROTOCOL_STATE,
                    "getSignaturesForAddress pagination repeated a signature",
                    as_of_slot=finalized_slot,
                )
            seen_signatures.add(signature)
            collected.append((signature, slot))
            if len(collected) > max_transactions:
                return _abstain(
                    AbstainReason.UNKNOWN_PROTOCOL_STATE,
                    "getSignaturesForAddress history exceeded the transaction bound",
                    as_of_slot=finalized_slot,
                )
        if boundary_found:
            return tuple(collected)
        if len(page) < page_limit:
            return _abstain(
                AbstainReason.UNKNOWN_PROTOCOL_STATE,
                "getSignaturesForAddress cursor boundary was not found",
                as_of_slot=finalized_slot,
            )
        before_signature = page[-1][0]

    return _abstain(
        AbstainReason.UNKNOWN_PROTOCOL_STATE,
        "getSignaturesForAddress history exceeded the pagination budget",
        as_of_slot=finalized_slot,
    )


async def _read_helius_full_transaction_history(  # noqa: C901, PLR0911, PLR0912, PLR0913
    transport: RpcHttpTransport,
    *,
    endpoint: str,
    address: str,
    finalized_slot: int,
    page_limit: int,
    max_transactions: int,
    max_pages: int,
    start_slot: int | None,
    end_slot: int | None,
) -> (
    tuple[tuple[tuple[str, int, int], ...], dict[str, _HeliusFullTransaction]]
    | AbstainResult
):
    """Read Helius finalized transactions and hydrate them in the same page."""

    pagination_token: str | None = None
    seen_pagination_tokens: set[str] = set()
    collected: list[tuple[str, int, int]] = []
    preloaded: dict[str, _HeliusFullTransaction] = {}
    previous_slot: int | None = None
    for _ in range(max_pages):
        options: dict[str, object] = {
            "commitment": FINALIZED,
            "limit": page_limit,
            "sortOrder": "desc",
            "transactionDetails": "full",
        }
        if start_slot is not None and end_slot is not None:
            options["filters"] = {"slot": {"gte": start_slot, "lte": end_slot}}
        if pagination_token is not None:
            options["paginationToken"] = pagination_token
        page_result = await _read_result(
            transport,
            endpoint=endpoint,
            method="getTransactionsForAddress",
            params=(address, options),
            as_of_slot=finalized_slot,
        )
        if isinstance(page_result, AbstainResult):
            return page_result
        page_payload, _ = page_result
        page = _validated_helius_full_page(
            page_payload,
            max_signatures=page_limit,
            finalized_slot=finalized_slot,
            start_slot=start_slot,
            end_slot=end_slot,
        )
        if isinstance(page, AbstainResult):
            return page
        entries, next_pagination_token, reached_start_boundary = page
        remaining = max_transactions - len(collected)
        if remaining <= 0:
            return tuple(collected), preloaded
        if len(entries) > remaining:
            entries = entries[:remaining]
        for entry in entries:
            if previous_slot is not None and entry.slot > previous_slot:
                return _abstain(
                    AbstainReason.UNKNOWN_PROTOCOL_STATE,
                    "getTransactionsForAddress history was not newest-first",
                    as_of_slot=finalized_slot,
                )
            previous_slot = entry.slot
            if entry.signature in preloaded:
                return _abstain(
                    AbstainReason.UNKNOWN_PROTOCOL_STATE,
                    "getTransactionsForAddress pagination repeated a signature",
                    as_of_slot=finalized_slot,
                )
            collected.append((entry.signature, entry.slot, entry.transaction_index))
            preloaded[entry.signature] = entry
        if len(collected) >= max_transactions or reached_start_boundary:
            return tuple(collected), preloaded
        if next_pagination_token is None:
            return tuple(collected), preloaded
        pagination_slot = _helius_pagination_token_slot(next_pagination_token)
        if pagination_slot is None:
            return _abstain(
                AbstainReason.UNKNOWN_PROTOCOL_STATE,
                "getTransactionsForAddress returned a malformed pagination token",
                as_of_slot=finalized_slot,
            )
        if start_slot is not None and pagination_slot < start_slot:
            return tuple(collected), preloaded
        if next_pagination_token in seen_pagination_tokens:
            return _abstain(
                AbstainReason.UNKNOWN_PROTOCOL_STATE,
                "getTransactionsForAddress pagination repeated a token",
                as_of_slot=finalized_slot,
            )
        seen_pagination_tokens.add(next_pagination_token)
        pagination_token = next_pagination_token
    return _abstain(
        AbstainReason.UNKNOWN_PROTOCOL_STATE,
        "getTransactionsForAddress history exceeded the pagination budget",
        as_of_slot=finalized_slot,
    )


def _validated_helius_full_page(
    result: object,
    *,
    max_signatures: int,
    finalized_slot: int,
    start_slot: int | None,
    end_slot: int | None,
) -> tuple[tuple[_HeliusFullTransaction, ...], str | None, bool] | AbstainResult:
    """Validate one Helius full-transaction page before it enters replay."""

    if type(result) is not dict or "data" not in result:
        return _abstain(
            AbstainReason.UNKNOWN_PROTOCOL_STATE,
            "getTransactionsForAddress returned an invalid result",
            as_of_slot=finalized_slot,
        )
    data = result["data"]
    pagination_token = result.get("paginationToken")
    if type(data) is not list or len(data) > max_signatures:
        return _abstain(
            AbstainReason.UNKNOWN_PROTOCOL_STATE,
            "getTransactionsForAddress returned an invalid page size",
            as_of_slot=finalized_slot,
        )
    if pagination_token is not None and not _valid_helius_pagination_token(
        pagination_token
    ):
        return _abstain(
            AbstainReason.UNKNOWN_PROTOCOL_STATE,
            "getTransactionsForAddress returned a malformed pagination token",
            as_of_slot=finalized_slot,
        )

    entries: list[_HeliusFullTransaction] = []
    seen_signatures: set[str] = set()
    previous_slot: int | None = None
    reached_start_boundary = False
    for item in data:
        if type(item) is not dict:
            return _abstain(
                AbstainReason.UNKNOWN_PROTOCOL_STATE,
                "getTransactionsForAddress returned malformed evidence",
                as_of_slot=finalized_slot,
            )
        slot = item.get("slot")
        transaction_index = item.get("transactionIndex")
        transaction = item.get("transaction")
        meta = item.get("meta")
        signatures = (
            transaction.get("signatures") if type(transaction) is dict else None
        )
        signature = signatures[0] if type(signatures) is list and signatures else None
        if (
            type(signature) is not str
            or not _valid_signature(signature)
            or not _non_negative_int(slot)
            or slot > finalized_slot
            or (end_slot is not None and slot > end_slot)
            or not _non_negative_int(transaction_index)
            or type(transaction) is not dict
            or type(meta) is not dict
            or "err" not in meta
            or signature in seen_signatures
            or (previous_slot is not None and slot > previous_slot)
        ):
            return _abstain(
                AbstainReason.UNKNOWN_PROTOCOL_STATE,
                "getTransactionsForAddress returned incomplete finalized evidence",
                as_of_slot=finalized_slot,
            )
        seen_signatures.add(signature)
        previous_slot = slot
        if start_slot is not None and slot < start_slot:
            reached_start_boundary = True
            continue
        response_body = _rpc_envelope_for_result(item)
        entries.append(
            _HeliusFullTransaction(
                signature=signature,
                slot=slot,
                transaction_index=transaction_index,
                result=item,
                response_body=response_body,
            )
        )
    return tuple(entries), pagination_token, reached_start_boundary


def _rpc_envelope_for_result(result: object) -> bytes:
    return json.dumps(
        {"jsonrpc": "2.0", "id": 1, "result": result},
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


async def _read_helius_signature_history(  # noqa: C901, PLR0911, PLR0912, PLR0913
    transport: RpcHttpTransport,
    *,
    endpoint: str,
    address: str,
    finalized_slot: int,
    page_limit: int,
    max_transactions: int,
    max_pages: int,
    start_slot: int | None,
    end_slot: int | None,
) -> tuple[tuple[str, int, int], ...] | AbstainResult:
    """Read bounded Helius signature history with finalized slot filters."""

    pagination_token: str | None = None
    seen_pagination_tokens: set[str] = set()
    seen_signatures: set[str] = set()
    collected: list[tuple[str, int, int]] = []
    previous_slot: int | None = None
    for _ in range(max_pages):
        options: dict[str, object] = {
            "commitment": FINALIZED,
            "limit": page_limit,
            "sortOrder": "desc",
            "transactionDetails": "signatures",
        }
        if start_slot is not None and end_slot is not None:
            options["filters"] = {"slot": {"gte": start_slot, "lte": end_slot}}
        if pagination_token is not None:
            options["paginationToken"] = pagination_token
        page_result = await _read_result(
            transport,
            endpoint=endpoint,
            method="getTransactionsForAddress",
            params=(address, options),
            as_of_slot=finalized_slot,
        )
        if isinstance(page_result, AbstainResult):
            return page_result
        page_payload, _ = page_result
        page = _validated_helius_signature_page(
            page_payload,
            max_signatures=page_limit,
            finalized_slot=finalized_slot,
            start_slot=start_slot,
            end_slot=end_slot,
        )
        if isinstance(page, AbstainResult):
            return page
        entries, next_pagination_token, reached_start_boundary = page
        for signature, slot, _ in entries:
            if previous_slot is not None and slot > previous_slot:
                return _abstain(
                    AbstainReason.UNKNOWN_PROTOCOL_STATE,
                    "getTransactionsForAddress history was not newest-first",
                    as_of_slot=finalized_slot,
                )
            previous_slot = slot
            if signature in seen_signatures:
                return _abstain(
                    AbstainReason.UNKNOWN_PROTOCOL_STATE,
                    "getTransactionsForAddress pagination repeated a signature",
                    as_of_slot=finalized_slot,
                )
            seen_signatures.add(signature)

        if start_slot is None:
            return tuple(entries[:max_transactions])
        remaining = max_transactions - len(collected)
        if remaining <= 0:
            return tuple(collected)
        if len(entries) > remaining:
            collected.extend(entries[:remaining])
            return tuple(collected)
        collected.extend(entries)
        if reached_start_boundary:
            return tuple(collected)
        if len(collected) >= max_transactions:
            return tuple(collected)
        if next_pagination_token is None:
            return tuple(collected)
        pagination_slot = _helius_pagination_token_slot(next_pagination_token)
        if pagination_slot is None:
            return _abstain(
                AbstainReason.UNKNOWN_PROTOCOL_STATE,
                "getTransactionsForAddress returned a malformed pagination token",
                as_of_slot=finalized_slot,
            )
        if start_slot is not None and pagination_slot < start_slot:
            return tuple(collected)
        if next_pagination_token in seen_pagination_tokens:
            return _abstain(
                AbstainReason.UNKNOWN_PROTOCOL_STATE,
                "getTransactionsForAddress pagination repeated a token",
                as_of_slot=finalized_slot,
            )
        seen_pagination_tokens.add(next_pagination_token)
        pagination_token = next_pagination_token

    return _abstain(
        AbstainReason.UNKNOWN_PROTOCOL_STATE,
        "getTransactionsForAddress history exceeded the pagination budget",
        as_of_slot=finalized_slot,
    )


async def _read_result(
    transport: RpcHttpTransport,
    *,
    endpoint: str,
    method: str,
    params: Sequence[object],
    as_of_slot: int,
) -> tuple[object, bytes] | AbstainResult:
    if method not in _RPC_METHODS:
        return _abstain(
            AbstainReason.UNKNOWN_PROTOCOL_STATE,
            "observer attempted a non-read-only RPC method",
            as_of_slot=as_of_slot,
        )

    request_body = _request_body(method, params)
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
            as_of_slot=as_of_slot,
        )

    if type(response) is not RpcHttpResponse:
        return _abstain(
            AbstainReason.UNKNOWN_PROTOCOL_STATE,
            f"{method} transport returned malformed response",
            as_of_slot=as_of_slot,
        )
    if response.status != HTTP_OK or type(response.body) is not bytes:
        return _abstain(
            AbstainReason.MISSING_FEATURE,
            f"{method} returned incomplete HTTP evidence",
            as_of_slot=as_of_slot,
        )

    payload = _decode_rpc_response(response.body, method=method)
    if isinstance(payload, AbstainResult):
        return AbstainResult(
            reason=payload.reason,
            message=payload.message,
            as_of_slot=as_of_slot,
        )
    return payload


def _request_body(method: str, params: Sequence[object]) -> bytes:
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
    if payload.get("jsonrpc") != "2.0" or payload.get("id") != 1:
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


def _validated_signature_entries(
    result: object,
    *,
    max_signatures: int,
    finalized_slot: int,
    max_slot: int | None,
) -> tuple[tuple[str, int], ...] | AbstainResult:
    if type(result) is not list:
        return _abstain(
            AbstainReason.UNKNOWN_PROTOCOL_STATE,
            "getSignaturesForAddress returned an invalid result",
            as_of_slot=finalized_slot,
        )
    if len(result) > max_signatures:
        return _abstain(
            AbstainReason.UNKNOWN_PROTOCOL_STATE,
            "getSignaturesForAddress exceeded the configured limit",
            as_of_slot=finalized_slot,
        )

    entries: list[tuple[str, int]] = []
    seen: set[str] = set()
    for item in result:
        if type(item) is not dict:
            return _abstain(
                AbstainReason.UNKNOWN_PROTOCOL_STATE,
                "getSignaturesForAddress returned malformed evidence",
                as_of_slot=finalized_slot,
            )
        signature = item.get("signature")
        slot = item.get("slot")
        if (
            type(signature) is not str
            or not signature
            or not _valid_signature(signature)
            or not _non_negative_int(slot)
            or (max_slot is not None and slot > max_slot)
            or item.get("confirmationStatus") != FINALIZED
            or signature in seen
        ):
            return _abstain(
                AbstainReason.UNKNOWN_PROTOCOL_STATE,
                "getSignaturesForAddress returned incomplete finalized evidence",
                as_of_slot=finalized_slot,
            )
        seen.add(signature)
        entries.append((signature, slot))
    return tuple(entries)


def _validated_helius_signature_page(  # noqa: C901, PLR0911
    result: object,
    *,
    max_signatures: int,
    finalized_slot: int,
    start_slot: int | None,
    end_slot: int | None,
) -> tuple[tuple[tuple[str, int, int], ...], str | None, bool] | AbstainResult:
    """Validate one Helius signatures-only history page."""

    if type(result) is not dict or "data" not in result:
        return _abstain(
            AbstainReason.UNKNOWN_PROTOCOL_STATE,
            "getTransactionsForAddress returned an invalid result",
            as_of_slot=finalized_slot,
        )
    if "paginationToken" not in result:
        return _abstain(
            AbstainReason.UNKNOWN_PROTOCOL_STATE,
            "getTransactionsForAddress omitted pagination evidence",
            as_of_slot=finalized_slot,
        )
    data = result["data"]
    pagination_token = result["paginationToken"]
    if type(data) is not list or len(data) > max_signatures:
        return _abstain(
            AbstainReason.UNKNOWN_PROTOCOL_STATE,
            "getTransactionsForAddress returned an invalid page size",
            as_of_slot=finalized_slot,
        )
    if pagination_token is not None and not _valid_helius_pagination_token(
        pagination_token
    ):
        return _abstain(
            AbstainReason.UNKNOWN_PROTOCOL_STATE,
            "getTransactionsForAddress returned a malformed pagination token",
            as_of_slot=finalized_slot,
        )
    if not data and pagination_token is not None:
        pagination_slot = _helius_pagination_token_slot(pagination_token)
        if (
            start_slot is None
            or pagination_slot is None
            or pagination_slot >= start_slot
        ):
            return _abstain(
                AbstainReason.UNKNOWN_PROTOCOL_STATE,
                "getTransactionsForAddress returned empty data with pagination",
                as_of_slot=finalized_slot,
            )

    entries: list[tuple[str, int, int]] = []
    seen_signatures: set[str] = set()
    previous_slot: int | None = None
    reached_start_boundary = False
    for item in data:
        if type(item) is not dict:
            return _abstain(
                AbstainReason.UNKNOWN_PROTOCOL_STATE,
                "getTransactionsForAddress returned malformed evidence",
                as_of_slot=finalized_slot,
            )
        signature = item.get("signature")
        slot = item.get("slot")
        transaction_index = item.get("transactionIndex")
        if (
            type(signature) is not str
            or not _valid_signature(signature)
            or not _non_negative_int(slot)
            or slot > finalized_slot
            or (end_slot is not None and slot > end_slot)
            or not _non_negative_int(transaction_index)
            or item.get("confirmationStatus") != FINALIZED
            or signature in seen_signatures
            or (previous_slot is not None and slot > previous_slot)
        ):
            return _abstain(
                AbstainReason.UNKNOWN_PROTOCOL_STATE,
                "getTransactionsForAddress returned incomplete finalized evidence",
                as_of_slot=finalized_slot,
            )
        seen_signatures.add(signature)
        previous_slot = slot
        if start_slot is not None and slot < start_slot:
            if end_slot is not None:
                return _abstain(
                    AbstainReason.UNKNOWN_PROTOCOL_STATE,
                    "getTransactionsForAddress crossed the requested slot range",
                    as_of_slot=finalized_slot,
                )
            reached_start_boundary = True
            continue
        entries.append((signature, slot, transaction_index))
    return tuple(entries), pagination_token, reached_start_boundary


def _validated_transaction(  # noqa: PLR0911
    result: object,
    *,
    requested_signature: str,
    expected_slot: int,
    finalized_slot: int,
    response_body: bytes,
) -> tuple[bytes, int] | AbstainResult:
    if type(result) is not dict:
        return _abstain(
            AbstainReason.MISSING_FEATURE,
            "getTransaction returned no complete finalized transaction",
            as_of_slot=finalized_slot,
        )
    slot = result.get("slot")
    transaction = result.get("transaction")
    meta = result.get("meta")
    if type(meta) is not dict:
        return _abstain(
            AbstainReason.MISSING_FEATURE,
            "getTransaction returned no provable execution metadata",
            as_of_slot=finalized_slot,
        )
    if "err" not in meta:
        return _abstain(
            AbstainReason.MISSING_FEATURE,
            "getTransaction execution metadata omitted err",
            as_of_slot=finalized_slot,
        )
    if not _non_negative_int(slot) or slot > finalized_slot or slot != expected_slot:
        return _abstain(
            AbstainReason.UNKNOWN_PROTOCOL_STATE,
            "getTransaction returned an inconsistent finalized slot",
            as_of_slot=finalized_slot,
        )
    if type(transaction) is not dict:
        return _abstain(
            AbstainReason.MISSING_FEATURE,
            "getTransaction returned incomplete transaction evidence",
            as_of_slot=finalized_slot,
        )
    signatures = transaction.get("signatures")
    if (
        type(signatures) is not list
        or not signatures
        or any(type(signature) is not str for signature in signatures)
        or any(not _valid_signature(signature) for signature in signatures)
        or requested_signature not in signatures
    ):
        return _abstain(
            AbstainReason.UNKNOWN_PROTOCOL_STATE,
            "getTransaction signature evidence does not match the request",
            as_of_slot=finalized_slot,
        )
    if type(meta) is dict and meta.get("err") is not None:
        return _FailedFinalizedTransaction()
    return response_body, slot


def _validated_block_transaction_indices(  # noqa: PLR0911
    result: object,
    *,
    as_of_slot: int,
) -> dict[str, int] | AbstainResult:
    if type(result) is not dict:
        return _abstain(
            AbstainReason.MISSING_FEATURE,
            "getBlock returned no complete finalized block",
            as_of_slot=as_of_slot,
        )
    transactions = result.get("transactions")
    if type(transactions) is not list:
        return _abstain(
            AbstainReason.MISSING_FEATURE,
            "getBlock returned incomplete transaction ordering evidence",
            as_of_slot=as_of_slot,
        )
    indices: dict[str, int] = {}
    for transaction_index, entry in enumerate(transactions):
        if type(entry) is not dict:
            return _abstain(
                AbstainReason.UNKNOWN_PROTOCOL_STATE,
                "getBlock returned malformed transaction ordering evidence",
                as_of_slot=as_of_slot,
            )
        transaction = entry.get("transaction")
        if type(transaction) is not dict:
            return _abstain(
                AbstainReason.UNKNOWN_PROTOCOL_STATE,
                "getBlock transaction ordering evidence is malformed",
                as_of_slot=as_of_slot,
            )
        signatures = transaction.get("signatures")
        if type(signatures) is not list or any(
            type(signature) is not str or not _valid_signature(signature)
            for signature in signatures
        ):
            return _abstain(
                AbstainReason.UNKNOWN_PROTOCOL_STATE,
                "getBlock transaction signatures are malformed",
                as_of_slot=as_of_slot,
            )
        for signature in signatures:
            if signature in indices:
                return _abstain(
                    AbstainReason.UNKNOWN_PROTOCOL_STATE,
                    "getBlock contained duplicate transaction signatures",
                    as_of_slot=as_of_slot,
                )
            indices[signature] = transaction_index
    return indices


def _validate_inputs(  # noqa: PLR0911, PLR0913
    *,
    address: str,
    endpoint: str,
    source_id: str,
    observer_id: str,
    boot_id: UUID | None,
    receive_sequence_start: int,
    max_signatures: int,
    max_transactions: int,
    max_pages: int,
    start_slot: int | None,
    end_slot: int | None,
    cursor: AddressHistoryCursor | None,
) -> AbstainResult | None:
    if not _non_blank_str(address) or not _valid_address(address):
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
    if (
        not _bounded_positive_int(max_signatures, MAX_SIGNATURES)
        or not _bounded_positive_int(max_transactions, MAX_TRANSACTIONS)
        or not _bounded_positive_int(max_pages, MAX_PAGES)
    ):
        return _abstain(
            AbstainReason.MISSING_FEATURE,
            "history limits are outside the bounded range",
            as_of_slot=-1,
        )
    if (start_slot is None and end_slot is not None) or (
        start_slot is not None
        and (
            not _non_negative_int(start_slot)
            or (
                end_slot is not None
                and (not _non_negative_int(end_slot) or end_slot < start_slot)
            )
        )
    ):
        return _abstain(
            AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
            "slot window bounds are malformed",
            as_of_slot=-1,
        )
    if cursor is not None and type(cursor) is not AddressHistoryCursor:
        return _abstain(
            AbstainReason.MISSING_FEATURE,
            "history cursor is malformed",
            as_of_slot=-1,
        )
    if cursor is not None and start_slot is not None:
        return _abstain(
            AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
            "history cursor cannot be combined with a slot window",
            as_of_slot=-1,
        )
    if cursor is not None and (
        cursor.address != address or cursor.source_id != source_id
    ):
        return _abstain(
            AbstainReason.UNKNOWN_PROTOCOL_STATE,
            "history cursor does not match the requested source",
            as_of_slot=-1,
        )
    return None


def _valid_address(value: str) -> bool:
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


def _is_helius_endpoint(value: str) -> bool:
    """Return whether an HTTPS endpoint is clearly a Helius RPC host."""

    try:
        hostname = urlsplit(value).hostname
    except ValueError:
        return False
    if hostname is None:
        return False
    hostname = hostname.rstrip(".").lower()
    return hostname == "helius-rpc.com" or hostname.endswith(".helius-rpc.com")


def _valid_helius_pagination_token(value: object) -> bool:
    return _helius_pagination_token_slot(value) is not None


def _helius_pagination_token_slot(value: object) -> int | None:
    if type(value) is not str:
        return None
    slot, separator, position = value.partition(":")
    if separator != ":" or not _ascii_digits(slot) or not _ascii_digits(position):
        return None
    try:
        return int(slot)
    except ValueError:
        return None


def _valid_signature(value: str) -> bool:
    if type(value) is not str:
        return False
    try:
        return len(base58.b58decode(value)) == SOLANA_SIGNATURE_BYTES
    except ValueError:
        return False


def _decode_signature(value: str) -> bytes:
    return bytes(base58.b58decode(value))


def _strict_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateJsonObjectKeyError
        result[key] = value
    return result


def _abstain(reason: AbstainReason, message: str, *, as_of_slot: int) -> AbstainResult:
    return AbstainResult(reason=reason, message=message, as_of_slot=as_of_slot)


def _non_negative_int(value: object) -> bool:
    return type(value) is int and value >= 0


def _ascii_digits(value: str) -> bool:
    return bool(value) and all(character in "0123456789" for character in value)


def _bounded_positive_int(value: object, maximum: int) -> bool:
    return type(value) is int and 0 < value <= maximum


def _non_blank_str(value: object) -> bool:
    return type(value) is str and bool(value.strip())
