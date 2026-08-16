"""Decode finalized RPC observations into pinned Pump.fun create_v2 launches."""

# Strict evidence validation is intentionally branch-heavy and fail-closed.
# ruff: noqa: C901, PLR0911, PLR0912, PLR0913

import json
from collections.abc import Mapping, Sequence

import base58

from rugbot.domain.account_roles import AccountRoleProof
from rugbot.domain.amounts import Slot
from rugbot.domain.decisions import AbstainReason, AbstainResult
from rugbot.domain.launches import (
    LaunchActorProof,
    LaunchActorRole,
    LaunchCreatedV2,
)
from rugbot.domain.observations import RawChainObservation
from rugbot.ingest.rpc_observer import JSON_TRANSACTION_FORMAT
from rugbot.market_state.pump_create import (
    PumpCreateMarketState,
    reconstruct_pump_create_market_state,
)
from rugbot.protocol.pump.create_decoder import (
    CREATE_V2_ACCOUNT_NAMES,
    CREATE_V2_DISCRIMINATOR,
    PINNED_PUMP_IDL_SHA256,
    PUMP_PROGRAM_ID,
    SPL_2022_PROGRAM_ID,
    CompiledPumpCreateV2Instruction,
    decode_pump_create_v2_instruction,
)
from rugbot.protocol.pump.create_event_decoder import (
    PumpCreateEvent,
    decode_pump_create_event_logs,
)
from rugbot.protocol.pump.metadata_resolver import (
    PumpFinalizedMintMetadataEvidence,
)

LiveCreateDecodeResult = LaunchCreatedV2 | AbstainResult | None
LiveCreateMarketStateResult = PumpCreateMarketState | AbstainResult | None
SIGNATURE_LENGTH = 64
TOKEN_2022_INITIALIZE_MINT2 = 20
TOKEN_2022_INITIALIZE_MINT2_NONE_LEN = 35
TOKEN_2022_INITIALIZE_MINT2_SOME_LEN = 67
TOKEN_2022_MAX_SUPPORTED_DECIMALS = 18
TOKEN_2022_MINT_DECIMALS_ARTIFACT = (
    "solana-token-2022-initialize-mint2-finalized-transaction"  # noqa: S105
)


class _DuplicateJsonKeyError(ValueError):
    """Raised when RPC evidence contains duplicate JSON object keys."""


def decode_pump_create_v2_observation(
    observation: RawChainObservation,
) -> LiveCreateDecodeResult:
    """Decode one finalized HTTP observation without additional I/O.

    Returns None for a valid transaction without a Pump create_v2 instruction.
    Malformed or ambiguous Pump evidence returns an abstention.
    """

    validation = _validate_observation(observation)
    if validation is not None:
        return validation
    try:
        envelope = json.loads(
            observation.raw_source_payload,
            object_pairs_hook=_strict_json_object,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
        return _abstain(
            "transaction observation contains invalid JSON",
            observation.slot,
        )
    transaction = _transaction_result(envelope, observation)
    if isinstance(transaction, AbstainResult):
        return transaction

    message, account_pubkeys, fee_payer, signature_text = transaction
    instructions = message.get("instructions")
    if not _sequence(instructions):
        return _abstain("transaction instructions are missing", observation.slot)

    matches: list[CompiledPumpCreateV2Instruction] = []
    for outer_index, raw_instruction in enumerate(instructions):
        candidate = _create_instruction(
            raw_instruction,
            observation=observation,
            account_pubkeys=account_pubkeys,
            fee_payer=fee_payer,
            signature_text=signature_text,
            outer_index=outer_index,
        )
        if isinstance(candidate, AbstainResult):
            return candidate
        if candidate is not None:
            matches.append(candidate)

    if not matches:
        return None
    if len(matches) != 1:
        return _abstain(
            "transaction contains multiple Pump create_v2 instructions",
            observation.slot,
        )
    return decode_pump_create_v2_instruction(
        matches[0],
        idl_hash=PINNED_PUMP_IDL_SHA256,
    )


def decode_pump_create_mint_metadata_observation(
    observation: RawChainObservation,
    *,
    mint_pubkey: str,
) -> PumpFinalizedMintMetadataEvidence | AbstainResult:
    """Decode mint decimals proven by the launch transaction's Token-2022 CPI.

    The decoder accepts only the pinned ``InitializeMint2`` instruction for
    the mint created by the same finalized Pump ``create_v2`` transaction. It
    does not use current account state or infer decimals from Pump defaults.
    """

    launch = decode_pump_create_v2_observation(observation)
    if launch is None:
        return _abstain(
            "mint metadata requires a Pump create_v2 transaction",
            observation.slot,
            AbstainReason.MISSING_FEATURE,
        )
    if isinstance(launch, AbstainResult):
        return launch
    if type(mint_pubkey) is not str or not mint_pubkey:
        return _abstain(
            "mint metadata mint pubkey is required",
            observation.slot,
            AbstainReason.MISSING_FEATURE,
        )
    if launch.mint_pubkey != mint_pubkey:
        return _abstain(
            "mint metadata request does not match the Pump launch mint",
            observation.slot,
            AbstainReason.STALE_STATE,
        )

    validation = _validate_observation(observation)
    if validation is not None:
        return validation
    try:
        envelope = json.loads(
            observation.raw_source_payload,
            object_pairs_hook=_strict_json_object,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
        return _abstain(
            "transaction observation contains invalid JSON",
            observation.slot,
        )
    transaction = _transaction_result(envelope, observation)
    if isinstance(transaction, AbstainResult):
        return transaction
    _, account_pubkeys, _, signature_text = transaction
    result = envelope.get("result")
    if not isinstance(result, Mapping):
        return _abstain("getTransaction result is malformed", observation.slot)
    meta = result.get("meta")
    if not isinstance(meta, Mapping):
        return _abstain("getTransaction metadata is missing", observation.slot)
    groups = meta.get("innerInstructions")
    if not _sequence(groups):
        return _abstain(
            "Token-2022 mint initialization evidence is missing",
            observation.slot,
            AbstainReason.MISSING_FEATURE,
        )

    decimals: int | None = None
    for group in groups:
        if not isinstance(group, Mapping):
            return _abstain("inner instruction group is malformed", observation.slot)
        instructions = group.get("instructions")
        if not _sequence(instructions):
            return _abstain("inner instruction list is malformed", observation.slot)
        for raw_instruction in instructions:
            candidate = _initialize_mint2_candidate(
                raw_instruction,
                account_pubkeys=account_pubkeys,
                mint_pubkey=mint_pubkey,
                as_of_slot=observation.slot,
            )
            if isinstance(candidate, AbstainResult):
                return candidate
            if candidate is None:
                continue
            if decimals is not None:
                return _abstain(
                    "transaction contains multiple mint initializations",
                    observation.slot,
                    AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
                )
            decimals = candidate

    if decimals is None:
        return _abstain(
            "Token-2022 InitializeMint2 for the launch mint is missing",
            observation.slot,
            AbstainReason.MISSING_FEATURE,
        )
    return PumpFinalizedMintMetadataEvidence(
        as_of_slot=Slot(observation.slot),
        mint_pubkey=mint_pubkey,
        owner_program_id=SPL_2022_PROGRAM_ID,
        decimals=decimals,
        source_artifact=(f"{TOKEN_2022_MINT_DECIMALS_ARTIFACT}:{signature_text}"),
        commitment="finalized",
    )


def decode_pump_create_market_state_observation(
    observation: RawChainObservation,
) -> LiveCreateMarketStateResult:
    """Reconstruct exact initial reserves from one finalized create transaction.

    The external create instruction supplies identity and the CPI CreateEvent
    supplies initial reserves. Both must agree before a market state is emitted.
    """

    launch = decode_pump_create_v2_observation(observation)
    if launch is None or isinstance(launch, AbstainResult):
        return launch

    log_messages = _transaction_log_messages(observation)
    if isinstance(log_messages, AbstainResult):
        return log_messages
    event = decode_pump_create_event_logs(
        log_messages,
        as_of_slot=observation.slot,
    )
    if event is None:
        return _abstain(
            "Pump create CPI event is missing from finalized transaction logs",
            observation.slot,
            AbstainReason.MISSING_FEATURE,
        )
    if isinstance(event, AbstainResult):
        return event
    context_error = _validate_create_event_context(log_messages, event)
    if context_error is not None:
        return context_error
    return reconstruct_pump_create_market_state(
        launch=launch,
        create_event=event,
    )


def _validate_observation(
    observation: object,
) -> AbstainResult | None:
    if type(observation) is not RawChainObservation:
        return _abstain("create decoder received malformed observation", -1)
    if (
        observation.commitment != "finalized"
        or observation.canonical_status != "canonical"
    ):
        return _abstain(
            "create decoder requires finalized canonical evidence",
            observation.slot,
        )
    if (
        observation.source_update_kind != "transaction"
        or observation.raw_transaction_format != JSON_TRANSACTION_FORMAT
        or type(observation.raw_source_payload) is not bytes
    ):
        return _abstain(
            "create decoder requires raw getTransaction JSON",
            observation.slot,
        )
    if (
        type(observation.transaction_index) is not int
        or observation.transaction_index < 0
        or type(observation.signature) is not bytes
        or len(observation.signature) != SIGNATURE_LENGTH
    ):
        return _abstain("transaction identity is incomplete", observation.slot)
    return None


def _transaction_result(
    envelope: object,
    observation: RawChainObservation,
) -> tuple[Mapping[str, object], tuple[str, ...], str, str] | AbstainResult:
    if not isinstance(envelope, Mapping) or envelope.get("jsonrpc") != "2.0":
        return _abstain("getTransaction envelope is malformed", observation.slot)
    result = envelope.get("result")
    if not isinstance(result, Mapping) or result.get("slot") != observation.slot:
        return _abstain(
            "getTransaction slot does not match observation",
            observation.slot,
        )
    meta = result.get("meta")
    if not isinstance(meta, Mapping) or meta.get("err") is not None:
        return _abstain(
            "getTransaction execution evidence is incomplete",
            observation.slot,
        )
    transaction = result.get("transaction")
    if not isinstance(transaction, Mapping):
        return _abstain("getTransaction transaction is missing", observation.slot)
    message = transaction.get("message")
    if not isinstance(message, Mapping):
        return _abstain("transaction message is missing", observation.slot)

    signatures = transaction.get("signatures")
    if not _sequence(signatures) or any(type(item) is not str for item in signatures):
        return _abstain("transaction signatures are malformed", observation.slot)
    signature_text = base58.b58encode(observation.signature).decode("ascii")
    if signatures[0] != signature_text:
        return _abstain(
            "transaction signature does not match observation",
            observation.slot,
        )

    account_pubkeys = _account_pubkeys(message, meta, observation.slot)
    if isinstance(account_pubkeys, AbstainResult):
        return account_pubkeys
    return message, account_pubkeys, account_pubkeys[0], signature_text


def _transaction_log_messages(
    observation: RawChainObservation,
) -> Sequence[object] | AbstainResult:
    try:
        envelope = json.loads(
            observation.raw_source_payload,
            object_pairs_hook=_strict_json_object,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
        return _abstain(
            "transaction observation contains invalid JSON",
            observation.slot,
            AbstainReason.UNKNOWN_PROTOCOL_STATE,
        )
    if not isinstance(envelope, Mapping) or envelope.get("jsonrpc") != "2.0":
        return _abstain(
            "getTransaction envelope is malformed",
            observation.slot,
            AbstainReason.UNKNOWN_PROTOCOL_STATE,
        )
    result = envelope.get("result")
    if not isinstance(result, Mapping) or result.get("slot") != observation.slot:
        return _abstain(
            "getTransaction slot does not match observation",
            observation.slot,
            AbstainReason.STALE_STATE,
        )
    meta = result.get("meta")
    if not isinstance(meta, Mapping) or meta.get("err") is not None:
        return _abstain(
            "getTransaction execution evidence is incomplete",
            observation.slot,
            AbstainReason.UNKNOWN_PROTOCOL_STATE,
        )
    log_messages = meta.get("logMessages")
    if not _sequence(log_messages):
        return _abstain(
            "Pump create CPI log evidence is missing",
            observation.slot,
            AbstainReason.MISSING_FEATURE,
        )
    return log_messages


def _validate_create_event_context(
    log_messages: Sequence[object],
    event: PumpCreateEvent,
) -> AbstainResult | None:
    create_instruction_indices = [
        index
        for index, message in enumerate(log_messages)
        if message == "Program log: Instruction: CreateV2"
    ]
    if len(create_instruction_indices) != 1:
        return _abstain(
            "Pump create CPI event has no unique CreateV2 invocation context",
            event.as_of_slot,
            AbstainReason.UNKNOWN_PROTOCOL_STATE,
        )
    if event.log_index <= create_instruction_indices[0]:
        return _abstain(
            "Pump create CPI event precedes the external CreateV2 invocation",
            event.as_of_slot,
            AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
        )
    return None


def _initialize_mint2_candidate(
    raw_instruction: object,
    *,
    account_pubkeys: tuple[str, ...],
    mint_pubkey: str,
    as_of_slot: int,
) -> int | AbstainResult | None:
    if not isinstance(raw_instruction, Mapping):
        return _abstain("inner instruction is malformed", as_of_slot)
    program_index = raw_instruction.get("programIdIndex")
    if type(program_index) is not int or not 0 <= program_index < len(account_pubkeys):
        return _abstain("inner program index is malformed", as_of_slot)
    if account_pubkeys[program_index] != SPL_2022_PROGRAM_ID:
        return None

    encoded_data = raw_instruction.get("data")
    if type(encoded_data) is not str:
        return _abstain("Token-2022 instruction data is malformed", as_of_slot)
    try:
        data = bytes(base58.b58decode(encoded_data))
    except ValueError:
        return _abstain("Token-2022 instruction data is not base58", as_of_slot)
    if not data:
        return _abstain("Token-2022 instruction data is empty", as_of_slot)
    if data[0] != TOKEN_2022_INITIALIZE_MINT2:
        return None

    raw_accounts = raw_instruction.get("accounts")
    if not _sequence(raw_accounts) or any(
        type(index) is not int for index in raw_accounts
    ):
        return _abstain("Token-2022 account indices are malformed", as_of_slot)
    if not raw_accounts:
        return _abstain("Token-2022 InitializeMint2 has no mint account", as_of_slot)
    mint_index = raw_accounts[0]
    if not 0 <= mint_index < len(account_pubkeys):
        return _abstain("Token-2022 mint account index is out of bounds", as_of_slot)
    if account_pubkeys[mint_index] != mint_pubkey:
        return None
    if len(data) == TOKEN_2022_INITIALIZE_MINT2_NONE_LEN:
        if data[-1] != 0:
            return _abstain(
                "Token-2022 InitializeMint2 freeze authority marker is invalid",
                as_of_slot,
            )
    elif len(data) == TOKEN_2022_INITIALIZE_MINT2_SOME_LEN:
        if data[34] != 1:
            return _abstain(
                "Token-2022 InitializeMint2 freeze authority marker is invalid",
                as_of_slot,
            )
    else:
        return _abstain(
            "Token-2022 InitializeMint2 data has an unsupported layout",
            as_of_slot,
            AbstainReason.DECODER_MISMATCH,
        )
    decimals = data[1]
    if decimals > TOKEN_2022_MAX_SUPPORTED_DECIMALS:
        return _abstain("Token-2022 mint decimals are unsupported", as_of_slot)
    return decimals


def _account_pubkeys(
    message: Mapping[str, object],
    meta: Mapping[str, object],
    as_of_slot: int,
) -> tuple[str, ...] | AbstainResult:
    static = message.get("accountKeys")
    if not _sequence(static) or any(type(item) is not str for item in static):
        return _abstain("transaction account keys are malformed", as_of_slot)

    loaded = meta.get("loadedAddresses")
    if loaded is None:
        return tuple(static)
    if not isinstance(loaded, Mapping):
        return _abstain("loaded transaction addresses are malformed", as_of_slot)
    writable = loaded.get("writable", ())
    readonly = loaded.get("readonly", loaded.get("readOnly", ()))
    if (
        not _sequence(writable)
        or not _sequence(readonly)
        or any(type(item) is not str for item in (*writable, *readonly))
    ):
        return _abstain("loaded transaction addresses are malformed", as_of_slot)
    return (*static, *writable, *readonly)


def _create_instruction(
    raw_instruction: object,
    *,
    observation: RawChainObservation,
    account_pubkeys: tuple[str, ...],
    fee_payer: str,
    signature_text: str,
    outer_index: int,
) -> CompiledPumpCreateV2Instruction | AbstainResult | None:
    if not isinstance(raw_instruction, Mapping):
        return _abstain(
            "compiled transaction instruction is malformed",
            observation.slot,
        )
    program_index = raw_instruction.get("programIdIndex")
    if type(program_index) is not int or not 0 <= program_index < len(account_pubkeys):
        return _abstain("compiled program index is malformed", observation.slot)
    if account_pubkeys[program_index] != PUMP_PROGRAM_ID:
        return None

    encoded_data = raw_instruction.get("data")
    if type(encoded_data) is not str:
        return _abstain("Pump instruction data is malformed", observation.slot)
    try:
        data = bytes(base58.b58decode(encoded_data))
    except ValueError:
        return _abstain("Pump instruction data is not base58", observation.slot)
    if data[: len(CREATE_V2_DISCRIMINATOR)] != CREATE_V2_DISCRIMINATOR:
        return None

    raw_accounts = raw_instruction.get("accounts")
    if not _sequence(raw_accounts) or any(
        type(index) is not int for index in raw_accounts
    ):
        return _abstain(
            "Pump create_v2 account indices are malformed",
            observation.slot,
        )
    account_indices = tuple(raw_accounts)
    if any(index < 0 or index >= len(account_pubkeys) for index in account_indices):
        return _abstain(
            "Pump create_v2 account index is out of bounds",
            observation.slot,
        )
    if len(account_indices) != len(CREATE_V2_ACCOUNT_NAMES):
        return _abstain(
            "Pump create_v2 account count is unsupported",
            observation.slot,
        )

    proofs = tuple(
        AccountRoleProof(
            name=name,
            pubkey=account_pubkeys[account_indices[position]],
        )
        for position, name in enumerate(CREATE_V2_ACCOUNT_NAMES)
    )
    fee_payer_proof = LaunchActorProof(
        as_of_slot=Slot(observation.slot),
        role=LaunchActorRole.FEE_PAYER,
        account_index=0,
        pubkey=fee_payer,
        evidence_ids=(f"transaction:{signature_text}:message.accountKeys[0]",),
        source_version="solana-message-fee-payer",
    )
    return CompiledPumpCreateV2Instruction(
        as_of_slot=Slot(observation.slot),
        program_id=PUMP_PROGRAM_ID,
        program_id_index=program_index,
        account_indices=account_indices,
        account_pubkeys=account_pubkeys,
        account_role_proofs=proofs,
        data=data,
        transaction_index=observation.transaction_index,
        outer_instruction_index=outer_index,
        signature=observation.signature,
        actor_role_proofs=(fee_payer_proof,),
        transaction_slot_account_state_available=False,
    )


def _sequence(value: object) -> bool:
    return isinstance(value, Sequence) and not isinstance(
        value,
        (str, bytes, bytearray),
    )


def _strict_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateJsonKeyError
        result[key] = value
    return result


def _abstain(
    message: str,
    as_of_slot: int,
    reason: AbstainReason = AbstainReason.UNKNOWN_PROTOCOL_STATE,
) -> AbstainResult:
    return AbstainResult(
        reason=reason,
        message=message,
        as_of_slot=as_of_slot,
    )
