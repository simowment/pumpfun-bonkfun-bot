"""Reconstruct executed Pump bonding-curve fills from finalized evidence."""

# The boundary is intentionally explicit: malformed finalized evidence must
# abstain at the exact field that cannot be proven.
# ruff: noqa: C901, PLR0911, PLR0912, PLR0913, PLR2004

from __future__ import annotations

import base64
import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from struct import unpack_from

import base58

from rugbot.backtest.dataset import FinalizedTrade
from rugbot.domain.amounts import QuoteBaseUnits, Slot, TokenBaseUnits
from rugbot.domain.decisions import AbstainReason, AbstainResult
from rugbot.domain.observations import RawChainObservation
from rugbot.domain.trades import (
    PumpSwapTradeInstructionEvidence,
    PumpTradeInstructionEvidence,
    TradeSide,
)
from rugbot.storage.jsonl_observation_store import observation_identity

TRADE_EVENT_DISCRIMINATOR = bytes([189, 219, 127, 211, 78, 230, 97, 238])


@dataclass(frozen=True, slots=True)
class PumpTradeEventProof:
    """Executed amounts and fees decoded from one finalized Pump event."""

    mint: str
    user: str
    sol_amount_base_units: int
    token_amount_base_units: int
    is_buy: bool
    instruction_name: str
    timestamp: int
    virtual_sol_reserves_base_units: int
    virtual_token_reserves_base_units: int
    real_sol_reserves_base_units: int
    real_token_reserves_base_units: int
    protocol_fee_base_units: int
    creator_fee_base_units: int
    protocol_fee_basis_points: int
    creator_fee_basis_points: int
    cashback_base_units: int
    encoded_event: bytes
    buyback_fee_basis_points: int = 0
    buyback_fee_base_units: int = 0
    shareholders: tuple[tuple[str, int], ...] = ()
    quote_mint: str = ""
    quote_amount_base_units: int = 0
    virtual_quote_reserves_base_units: int = 0
    real_quote_reserves_base_units: int = 0


@dataclass(frozen=True, slots=True)
class FinalizedTradeJoin:
    """Typed launch join for one decoded Pump instruction."""

    signature: bytes
    outer_instruction_index: int
    launch_id: str
    token_mint: str
    wallet: str


def build_finalized_trades_from_observations(
    *,
    observations: tuple[RawChainObservation, ...],
    joins: tuple[FinalizedTradeJoin, ...],
    as_of_slot: Slot,
) -> tuple[FinalizedTrade, ...] | AbstainResult:
    """Derive executed fills from immutable observations and typed joins."""

    cutoff = as_of_slot if type(as_of_slot) is int else -1
    if type(as_of_slot) is not int or as_of_slot < 0:
        return _abstain(
            AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
            "trade cutoff slot must be a non-negative integer",
            cutoff,
        )
    if type(observations) is not tuple or type(joins) is not tuple:
        return _abstain(
            AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
            "trade observations and joins must be tuples",
            cutoff,
        )
    if any(type(item) is not RawChainObservation for item in observations):
        return _abstain(
            AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
            "trade observations are malformed",
            cutoff,
        )
    if any(type(item) is not FinalizedTradeJoin for item in joins):
        return _abstain(
            AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
            "finalized trade joins are malformed",
            cutoff,
        )
    join_by_key: dict[tuple[bytes, int], FinalizedTradeJoin] = {}
    for join in joins:
        if (
            type(join.signature) is not bytes
            or not join.signature
            or type(join.outer_instruction_index) is not int
            or join.outer_instruction_index < 0
            or not all(
                isinstance(value, str) and value
                for value in (
                    join.launch_id,
                    join.token_mint,
                    join.wallet,
                )
            )
        ):
            return _abstain(
                AbstainReason.MISSING_FEATURE,
                "finalized trade join identity is incomplete",
                cutoff,
            )
        key = (join.signature, join.outer_instruction_index)
        if key in join_by_key:
            return _abstain(
                AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
                "finalized trade joins are duplicated",
                cutoff,
            )
        join_by_key[key] = join

    from rugbot.ingest.pump_swap_trade_observation import (  # noqa: PLC0415
        decode_pump_swap_trade_observation,
    )
    from rugbot.ingest.pump_trade_observation import (  # noqa: PLC0415
        decode_pump_trade_observation,
    )

    built: list[FinalizedTrade] = []
    seen_join_keys: set[tuple[bytes, int]] = set()
    joined_signatures = {signature for signature, _ in join_by_key}
    for observation in observations:
        if observation.signature not in joined_signatures:
            continue
        decoded = decode_pump_trade_observation(observation)
        if isinstance(decoded, AbstainResult):
            return decoded
        for instruction in decoded:
            signature = observation.signature
            if signature is None:
                return _abstain(
                    AbstainReason.MISSING_FEATURE,
                    "decoded Pump trade lacks a signature",
                    cutoff,
                )
            key = (signature, instruction.outer_instruction_index)
            join = join_by_key.get(key)
            if join is None:
                continue
            fill = build_finalized_pump_trade(
                observation=observation,
                instruction=instruction,
                launch_id=join.launch_id,
                token_mint=join.token_mint,
                wallet=join.wallet,
                as_of_slot=Slot(cutoff),
            )
            if isinstance(fill, AbstainResult):
                return fill
            built.append(fill)
            seen_join_keys.add(key)

        decoded_swap = decode_pump_swap_trade_observation(observation)
        if isinstance(decoded_swap, AbstainResult):
            return decoded_swap
        for instruction in decoded_swap:
            signature = observation.signature
            if signature is None:
                return _abstain(
                    AbstainReason.MISSING_FEATURE,
                    "decoded Pump AMM trade lacks a signature",
                    cutoff,
                )
            key = (signature, instruction.outer_instruction_index)
            join = join_by_key.get(key)
            if join is None:
                continue
            fill = build_finalized_pump_swap_trade(
                observation=observation,
                instruction=instruction,
                launch_id=join.launch_id,
                token_mint=join.token_mint,
                wallet=join.wallet,
                as_of_slot=Slot(cutoff),
            )
            if isinstance(fill, AbstainResult):
                return fill
            built.append(fill)
            seen_join_keys.add(key)

    missing = set(join_by_key).difference(seen_join_keys)
    if missing:
        return _abstain(
            AbstainReason.MISSING_FEATURE,
            "a finalized trade join has no matching decoded Pump instruction",
            cutoff,
        )
    return tuple(built)


def build_finalized_pump_swap_trade(
    *,
    observation: RawChainObservation,
    instruction: PumpSwapTradeInstructionEvidence,
    launch_id: str,
    token_mint: str,
    wallet: str,
    as_of_slot: Slot,
) -> FinalizedTrade | AbstainResult:
    """Build one AMM fill from its exact event and instruction join."""

    cutoff = as_of_slot if type(as_of_slot) is int else -1
    if type(observation) is not RawChainObservation:
        return _abstain(
            AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
            "Pump AMM fill observation is malformed",
            cutoff,
        )
    if (
        observation.commitment != "finalized"
        or observation.canonical_status != "canonical"
        or observation.source_update_kind != "transaction"
        or observation.slot > cutoff
        or not isinstance(observation.raw_source_payload, bytes)
        or observation.signature is None
        or observation.transaction_index is None
    ):
        return _abstain(
            AbstainReason.STALE_STATE,
            "Pump AMM fill requires finalized transaction evidence",
            cutoff,
        )
    if type(instruction) is not PumpSwapTradeInstructionEvidence:
        return _abstain(
            AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
            "Pump AMM instruction proof is malformed",
            cutoff,
        )
    accounts = instruction.account_pubkeys
    if accounts is None:
        return _abstain(
            AbstainReason.MISSING_FEATURE,
            "Pump AMM instruction account proof is missing",
            cutoff,
        )
    mint = _account_at(accounts, instruction.base_mint_account_index)
    user = _account_at(accounts, instruction.user_account_index)
    if mint != token_mint or user != wallet:
        return _abstain(
            AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
            "Pump AMM instruction identity does not match its join",
            cutoff,
        )
    from rugbot.ingest.pump_swap_event_observation import (  # noqa: PLC0415
        decode_pump_swap_events_observation,
    )

    events = decode_pump_swap_events_observation(observation)
    if isinstance(events, AbstainResult):
        return events
    matching = tuple(
        event
        for event in events
        if event.user == wallet
        and event.side is instruction.side
        and event.instruction_name == instruction.instruction_name
    )
    if len(matching) != 1:
        return _abstain(
            AbstainReason.MISSING_FEATURE,
            "Pump AMM instruction has no unique finalized event",
            cutoff,
        )
    event = matching[0]
    amount_matches = instruction.base_amount_base_units is not None and int(
        instruction.base_amount_base_units
    ) == int(event.base_amount_base_units)
    if instruction.instruction_name == "buy_exact_quote_in":
        amount_matches = instruction.min_base_output_base_units is not None and int(
            event.base_amount_base_units
        ) >= int(instruction.min_base_output_base_units)
    if not amount_matches or int(event.user_quote_amount_base_units) <= 0:
        return _abstain(
            AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
            "Pump AMM executed amount does not match its instruction",
            cutoff,
        )
    transaction_fee = _transaction_fee(observation)
    if isinstance(transaction_fee, AbstainResult):
        return transaction_fee
    execution_cost = (
        transaction_fee
        + int(event.lp_fee_base_units)
        + int(event.protocol_fee_base_units)
        + int(event.creator_fee_base_units)
    )
    evidence_root = _evidence_root(observation)
    return FinalizedTrade(
        as_of_slot=Slot(cutoff),
        launch_id=launch_id,
        token_mint=token_mint,
        wallet=wallet,
        side=event.side,
        slot=Slot(observation.slot),
        transaction_index=observation.transaction_index,
        signature=observation.signature,
        base_amount_base_units=TokenBaseUnits(event.base_amount_base_units),
        quote_amount_base_units=QuoteBaseUnits(event.user_quote_amount_base_units),
        execution_cost_quote_base_units=QuoteBaseUnits(execution_cost),
        evidence_ids=(
            f"{evidence_root}:pump-amm-trade-event",
            f"{evidence_root}:transaction-fee",
        ),
    )


def build_finalized_pump_trade(
    *,
    observation: RawChainObservation,
    instruction: PumpTradeInstructionEvidence,
    launch_id: str,
    token_mint: str,
    wallet: str,
    as_of_slot: Slot,
) -> FinalizedTrade | AbstainResult:
    """Build one fill only when finalized Pump execution evidence is complete.

    Instruction arguments are used solely as an integrity check.  Executed
    token and SOL amounts come from the pinned ``TradeEvent`` emitted by the
    program, while transaction fees come from finalized transaction metadata.
    The function is pure and never contacts RPC or storage.
    """

    cutoff = as_of_slot if type(as_of_slot) is int else -1
    validation = _validate_inputs(
        observation=observation,
        instruction=instruction,
        launch_id=launch_id,
        token_mint=token_mint,
        wallet=wallet,
        as_of_slot=as_of_slot,
    )
    if validation is not None:
        return validation

    payload = _load_transaction_payload(observation)
    if isinstance(payload, AbstainResult):
        return payload
    meta, event_payloads = payload

    event = _select_event(
        event_payloads,
        instruction=instruction,
        token_mint=token_mint,
        wallet=wallet,
        as_of_slot=observation.slot,
    )
    if isinstance(event, AbstainResult):
        return event

    if event.token_amount_base_units != int(instruction.base_amount_base_units or 0):
        return _abstain(
            AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
            "executed token amount does not match Pump instruction amount",
            cutoff,
        )
    if event.sol_amount_base_units <= 0:
        return _abstain(
            AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
            "executed quote amount must be positive",
            cutoff,
        )

    transaction_fee = meta["fee"]
    execution_cost = (
        transaction_fee
        + event.protocol_fee_base_units
        + event.creator_fee_base_units
        + event.buyback_fee_base_units
        - event.cashback_base_units
    )
    if execution_cost < 0:
        return _abstain(
            AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
            "Pump cashback exceeds recorded execution fees",
            cutoff,
        )

    signature = observation.signature
    transaction_index = observation.transaction_index
    if signature is None or transaction_index is None:
        return _abstain(
            AbstainReason.MISSING_FEATURE,
            "finalized trade transaction identity is incomplete",
            cutoff,
        )

    evidence_root = _evidence_root(observation)
    return FinalizedTrade(
        as_of_slot=Slot(cutoff),
        launch_id=launch_id,
        token_mint=token_mint,
        wallet=wallet,
        side=TradeSide.BUY if event.is_buy else TradeSide.SELL,
        slot=Slot(observation.slot),
        transaction_index=transaction_index,
        signature=signature,
        base_amount_base_units=TokenBaseUnits(event.token_amount_base_units),
        quote_amount_base_units=QuoteBaseUnits(event.sol_amount_base_units),
        execution_cost_quote_base_units=QuoteBaseUnits(execution_cost),
        evidence_ids=(
            f"{evidence_root}:pump-trade-event",
            f"{evidence_root}:transaction-fee",
        ),
    )


def decode_pump_trade_event_proofs(
    observation: RawChainObservation,
) -> tuple[tuple[int, PumpTradeEventProof], ...] | AbstainResult:
    """Decode every pinned Pump ``TradeEvent`` in one finalized observation.

    The returned ordinal is the encounter order of matching ``Program data``
    log records. It is provenance for trajectory ordering only; it does not
    replace missing protocol, mint, or account-state proofs.
    """

    validation = _validate_trade_event_observation(observation)
    if validation is not None:
        return validation
    payload = _load_transaction_payload(observation)
    if isinstance(payload, AbstainResult):
        return payload
    _, event_payloads = payload
    decoded: list[tuple[int, PumpTradeEventProof]] = []
    for event_index, event_payload in enumerate(event_payloads):
        event = _decode_trade_event(event_payload, observation.slot)
        if isinstance(event, AbstainResult):
            return event
        decoded.append((event_index, event))
    return tuple(decoded)


def _validate_trade_event_observation(
    observation: object,
) -> AbstainResult | None:
    if type(observation) is not RawChainObservation:
        return _abstain(
            AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
            "Pump TradeEvent observation is malformed",
            -1,
        )
    if (
        observation.commitment != "finalized"
        or observation.canonical_status != "canonical"
        or observation.source_update_kind != "transaction"
        or not isinstance(observation.raw_source_payload, bytes)
        or observation.signature is None
        or observation.transaction_index is None
    ):
        return _abstain(
            AbstainReason.STALE_STATE,
            "Pump TradeEvent requires finalized transaction evidence",
            observation.slot,
        )
    return None


def _validate_inputs(
    *,
    observation: RawChainObservation,
    instruction: PumpTradeInstructionEvidence,
    launch_id: str,
    token_mint: str,
    wallet: str,
    as_of_slot: Slot,
) -> AbstainResult | None:
    cutoff = as_of_slot if type(as_of_slot) is int else -1
    if type(as_of_slot) is not int or as_of_slot < 0:
        return _abstain(
            AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
            "trade cutoff slot must be a non-negative integer",
            cutoff,
        )
    if type(observation) is not RawChainObservation:
        return _abstain(
            AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
            "raw finalized observation is malformed",
            cutoff,
        )
    if (
        observation.commitment != "finalized"
        or observation.canonical_status != "canonical"
        or observation.source_update_kind != "transaction"
        or observation.slot > as_of_slot
        or not isinstance(observation.raw_source_payload, bytes)
    ):
        return _abstain(
            AbstainReason.STALE_STATE,
            "trade fill requires finalized canonical transaction evidence",
            cutoff,
        )
    if (
        type(instruction) is not PumpTradeInstructionEvidence
        or instruction.as_of_slot != observation.slot
        or instruction.signature != observation.signature
        or instruction.transaction_index != observation.transaction_index
        or instruction.side not in (TradeSide.BUY, TradeSide.SELL)
    ):
        return _abstain(
            AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
            "trade instruction is not joined to the raw observation",
            cutoff,
        )
    if not all(
        isinstance(value, str) and value for value in (launch_id, token_mint, wallet)
    ):
        return _abstain(
            AbstainReason.MISSING_FEATURE,
            "trade launch, mint, and wallet identity are required",
            cutoff,
        )
    if (
        instruction.account_pubkeys is None
        or instruction.mint_account_index < 0
        or instruction.mint_account_index >= len(instruction.account_pubkeys)
        or instruction.user_account_index < 0
        or instruction.user_account_index >= len(instruction.account_pubkeys)
        or instruction.outer_instruction_index < 0
    ):
        return _abstain(
            AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
            "trade account layout proof is incomplete",
            cutoff,
        )
    if (
        instruction.account_pubkeys[instruction.mint_account_index] != token_mint
        or instruction.account_pubkeys[instruction.user_account_index] != wallet
    ):
        return _abstain(
            AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
            "trade mint or wallet does not match the pinned account layout",
            cutoff,
        )
    return None


def _load_transaction_payload(
    observation: RawChainObservation,
) -> tuple[dict[str, int], tuple[bytes, ...]] | AbstainResult:
    try:
        envelope = json.loads(observation.raw_source_payload or b"")
    except (UnicodeDecodeError, json.JSONDecodeError):
        return _abstain(
            AbstainReason.UNKNOWN_PROTOCOL_STATE,
            "finalized trade payload is invalid JSON",
            observation.slot,
        )
    if not isinstance(envelope, Mapping) or envelope.get("jsonrpc") != "2.0":
        return _abstain(
            AbstainReason.UNKNOWN_PROTOCOL_STATE,
            "finalized trade payload envelope is malformed",
            observation.slot,
        )
    result = envelope.get("result")
    if not isinstance(result, Mapping) or result.get("slot") != observation.slot:
        return _abstain(
            AbstainReason.STALE_STATE,
            "finalized trade payload slot does not match observation",
            observation.slot,
        )
    transaction = result.get("transaction")
    meta = result.get("meta")
    if not isinstance(transaction, Mapping) or not isinstance(meta, Mapping):
        return _abstain(
            AbstainReason.MISSING_FEATURE,
            "finalized trade transaction metadata is missing",
            observation.slot,
        )
    if meta.get("err") is not None:
        return _abstain(
            AbstainReason.UNKNOWN_PROTOCOL_STATE,
            "failed finalized transaction cannot produce a fill",
            observation.slot,
        )
    fee = meta.get("fee")
    if type(fee) is not int or fee < 0:
        return _abstain(
            AbstainReason.MISSING_FEATURE,
            "finalized transaction fee is missing or malformed",
            observation.slot,
        )
    signatures = transaction.get("signatures")
    if not isinstance(signatures, list) or not signatures:
        return _abstain(
            AbstainReason.MISSING_FEATURE,
            "finalized transaction signatures are missing",
            observation.slot,
        )
    if observation.signature is None:
        return _abstain(
            AbstainReason.MISSING_FEATURE,
            "raw observation signature is missing",
            observation.slot,
        )
    expected_signature = base58.b58encode(observation.signature).decode("ascii")
    if signatures[0] != expected_signature:
        return _abstain(
            AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
            "finalized transaction signature does not match observation",
            observation.slot,
        )
    logs = meta.get("logMessages")
    if not isinstance(logs, list) or any(not isinstance(item, str) for item in logs):
        return _abstain(
            AbstainReason.MISSING_FEATURE,
            "Pump trade event logs are missing",
            observation.slot,
        )
    event_payloads: list[bytes] = []
    for message in logs:
        if not message.startswith("Program data: "):
            continue
        encoded = message.removeprefix("Program data: ")
        try:
            payload = base64.b64decode(encoded, validate=True)
        except (ValueError, TypeError):
            continue
        if payload.startswith(TRADE_EVENT_DISCRIMINATOR):
            event_payloads.append(payload)
    return {"fee": fee}, tuple(event_payloads)


def _select_event(
    payloads: tuple[bytes, ...],
    *,
    instruction: PumpTradeInstructionEvidence,
    token_mint: str,
    wallet: str,
    as_of_slot: int,
) -> PumpTradeEventProof | AbstainResult:
    events: list[PumpTradeEventProof] = []
    for payload in payloads:
        event = _decode_trade_event(payload, as_of_slot)
        if isinstance(event, AbstainResult):
            return event
        if (
            event.mint == token_mint
            and event.user == wallet
            and event.is_buy == (instruction.side is TradeSide.BUY)
            and event.instruction_name == instruction.instruction_name
        ):
            events.append(event)
    if not events:
        return _abstain(
            AbstainReason.MISSING_FEATURE,
            "no finalized Pump TradeEvent matches the instruction",
            as_of_slot,
        )
    if len(events) != 1:
        return _abstain(
            AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
            "multiple matching Pump TradeEvents cannot be assigned safely",
            as_of_slot,
        )
    return events[0]


def _decode_trade_event(
    payload: bytes,
    as_of_slot: int,
) -> PumpTradeEventProof | AbstainResult:
    reader = _EventReader(payload)
    if reader.read_bytes(8) != TRADE_EVENT_DISCRIMINATOR:
        return _abstain(
            AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
            "unexpected Pump trade event discriminator",
            as_of_slot,
        )
    mint = reader.read_pubkey()
    sol_amount = reader.read_u64()
    token_amount = reader.read_u64()
    is_buy = reader.read_bool()
    user = reader.read_pubkey()
    timestamp = reader.read_i64()
    virtual_sol_reserves = reader.read_u64()
    virtual_token_reserves = reader.read_u64()
    real_sol_reserves = reader.read_u64()
    real_token_reserves = reader.read_u64()
    reader.skip_pubkey()
    protocol_fee_basis_points = reader.read_u64()
    protocol_fee = reader.read_u64()
    reader.skip_pubkey()
    creator_fee_basis_points = reader.read_u64()
    creator_fee = reader.read_u64()
    reader.skip_bool()
    reader.skip_u64(3)
    reader.skip_i64()
    instruction_name = reader.read_string()
    reader.skip_bool()
    reader.skip_u64()
    cashback = reader.read_u64()
    buyback_fee_basis_points = reader.read_u64()
    buyback_fee = reader.read_u64()
    shareholders = reader.read_shareholders()
    quote_mint = reader.read_pubkey()
    quote_amount = reader.read_u64()
    virtual_quote_reserves = reader.read_u64()
    real_quote_reserves = reader.read_u64()
    if reader.error is not None or reader.remaining != 0:
        return _abstain(
            AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
            "Pump trade event layout is not exactly pinned",
            as_of_slot,
        )
    return PumpTradeEventProof(
        mint=mint,
        user=user,
        sol_amount_base_units=sol_amount,
        token_amount_base_units=token_amount,
        is_buy=is_buy,
        instruction_name=instruction_name,
        timestamp=timestamp,
        virtual_sol_reserves_base_units=virtual_sol_reserves,
        virtual_token_reserves_base_units=virtual_token_reserves,
        real_sol_reserves_base_units=real_sol_reserves,
        real_token_reserves_base_units=real_token_reserves,
        protocol_fee_base_units=protocol_fee,
        creator_fee_base_units=creator_fee,
        protocol_fee_basis_points=protocol_fee_basis_points,
        creator_fee_basis_points=creator_fee_basis_points,
        cashback_base_units=cashback,
        encoded_event=payload,
        buyback_fee_basis_points=buyback_fee_basis_points,
        buyback_fee_base_units=buyback_fee,
        shareholders=shareholders,
        quote_mint=quote_mint,
        quote_amount_base_units=quote_amount,
        virtual_quote_reserves_base_units=virtual_quote_reserves,
        real_quote_reserves_base_units=real_quote_reserves,
    )


class _EventReader:
    """Small bounded reader for the pinned Anchor event layout."""

    def __init__(self, payload: bytes) -> None:
        self._payload = payload
        self._offset = 0
        self.error: str | None = None

    @property
    def remaining(self) -> int:
        return max(len(self._payload) - self._offset, 0)

    def read_bytes(self, size: int) -> bytes:
        if self.error is not None or size < 0 or self.remaining < size:
            self.error = "event payload is truncated"
            return b""
        value = self._payload[self._offset : self._offset + size]
        self._offset += size
        return value

    def read_u64(self) -> int:
        value = self.read_bytes(8)
        return unpack_from("<Q", value)[0] if len(value) == 8 else 0

    def read_i64(self) -> int:
        value = self.read_bytes(8)
        return unpack_from("<q", value)[0] if len(value) == 8 else 0

    def read_u16(self) -> int:
        value = self.read_bytes(2)
        return unpack_from("<H", value)[0] if len(value) == 2 else 0

    def read_u32(self) -> int:
        value = self.read_bytes(4)
        return unpack_from("<I", value)[0] if len(value) == 4 else 0

    def read_pubkey(self) -> str:
        value = self.read_bytes(32)
        return base58.b58encode(value).decode("ascii") if len(value) == 32 else ""

    def read_bool(self) -> bool:
        value = self.read_bytes(1)
        if len(value) != 1 or value[0] not in (0, 1):
            self.error = "event boolean is malformed"
            return False
        return bool(value[0])

    def read_string(self) -> str:
        length_bytes = self.read_bytes(4)
        if len(length_bytes) != 4:
            return ""
        length = unpack_from("<I", length_bytes)[0]
        raw = self.read_bytes(length)
        if len(raw) != length:
            return ""
        try:
            return raw.decode("utf-8")
        except UnicodeDecodeError:
            self.error = "event string is not UTF-8"
            return ""

    def read_shareholders(self) -> tuple[tuple[str, int], ...]:
        count = self.read_u32()
        if self.error is not None:
            return ()
        entry_size = 32 + 2
        if count > self.remaining // entry_size:
            self.error = "shareholder vector is truncated"
            return ()
        return tuple((self.read_pubkey(), self.read_u16()) for _ in range(count))

    def skip_bool(self) -> None:
        self.read_bool()

    def skip_i64(self) -> None:
        self.read_bytes(8)

    def skip_pubkey(self) -> None:
        self.read_bytes(32)

    def skip_u64(self, count: int = 1) -> None:
        self.read_bytes(8 * count)


def _evidence_root(observation: RawChainObservation) -> str:
    identity = repr(observation_identity(observation)).encode("utf-8")
    return f"observation:{hashlib.sha256(identity).hexdigest()}"


def _account_at(accounts: tuple[str, ...], index: int) -> str | None:
    if type(index) is not int or index < 0 or index >= len(accounts):
        return None
    value = accounts[index]
    return value if isinstance(value, str) and value else None


def _transaction_fee(observation: RawChainObservation) -> int | AbstainResult:
    try:
        envelope = json.loads(observation.raw_source_payload or b"")
    except (UnicodeDecodeError, json.JSONDecodeError):
        return _abstain(
            AbstainReason.UNKNOWN_PROTOCOL_STATE,
            "finalized AMM transaction payload is invalid JSON",
            observation.slot,
        )
    if not isinstance(envelope, Mapping):
        return _abstain(
            AbstainReason.UNKNOWN_PROTOCOL_STATE,
            "finalized AMM transaction envelope is malformed",
            observation.slot,
        )
    result = envelope.get("result")
    meta = result.get("meta") if isinstance(result, Mapping) else None
    fee = meta.get("fee") if isinstance(meta, Mapping) else None
    if type(fee) is not int or fee < 0:
        return _abstain(
            AbstainReason.MISSING_FEATURE,
            "finalized AMM transaction fee is missing",
            observation.slot,
        )
    return fee


def _abstain(
    reason: AbstainReason,
    message: str,
    as_of_slot: int,
) -> AbstainResult:
    return AbstainResult(reason=reason, message=message, as_of_slot=as_of_slot)


__all__ = [
    "TRADE_EVENT_DISCRIMINATOR",
    "FinalizedTradeJoin",
    "PumpTradeEventProof",
    "build_finalized_pump_swap_trade",
    "build_finalized_pump_trade",
    "build_finalized_trades_from_observations",
    "decode_pump_trade_event_proofs",
]
