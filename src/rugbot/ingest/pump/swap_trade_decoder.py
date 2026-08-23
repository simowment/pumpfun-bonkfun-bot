"""Pure PumpSwap trade instruction decoder for pinned IDL evidence."""

from dataclasses import dataclass
from struct import unpack_from

from rugbot.domain.account_roles import AccountRoleProof
from rugbot.domain.amounts import QuoteBaseUnits, Slot, TokenBaseUnits
from rugbot.domain.decisions import AbstainReason, AbstainResult
from rugbot.domain.trades import (
    PumpSwapTradeInstructionEvidence,
    TradeSide,
)

PUMP_AMM_PROGRAM_ID = "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA"
SYSTEM_PROGRAM_ID = "11111111111111111111111111111111"
ASSOCIATED_SPL_PROGRAM_ID = "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL"
PUMP_FEE_PROGRAM_ID = "pfeeUxB6jkeY1Hxd7CsFCAjcbHA9rWtchMGdZ6VojVZ"
PINNED_PUMP_SWAP_IDL_SHA256 = (
    "da268f6f26a1e89fa83ec47f1db7dbff8ce16f96564a683fad00353e1bf19443"
)
PUMP_SWAP_TRADE_DECODER_VERSION = "pump-swap-trade-instruction-v1"

BUY_DISCRIMINATOR = bytes([102, 6, 61, 18, 1, 218, 235, 234])
BUY_EXACT_QUOTE_IN_DISCRIMINATOR = bytes([198, 46, 21, 82, 180, 217, 232, 112])
SELL_DISCRIMINATOR = bytes([51, 230, 133, 164, 1, 127, 131, 173])
DISCRIMINATOR_SIZE = 8
U64_SIZE = 8

BUY_ACCOUNT_NAMES = (
    "pool",
    "user",
    "global_config",
    "base_mint",
    "quote_mint",
    "user_base_token_account",
    "user_quote_token_account",
    "pool_base_token_account",
    "pool_quote_token_account",
    "protocol_fee_recipient",
    "protocol_fee_recipient_token_account",
    "base_token_program",
    "quote_token_program",
    "system_program",
    "associated_token_program",
    "event_authority",
    "program",
    "coin_creator_vault_ata",
    "coin_creator_vault_authority",
    "global_volume_accumulator",
    "user_volume_accumulator",
    "fee_config",
    "fee_program",
)
SELL_ACCOUNT_NAMES = tuple(
    name
    for name in BUY_ACCOUNT_NAMES
    if name not in {"global_volume_accumulator", "user_volume_accumulator"}
)

FIXED_ACCOUNT_PUBKEYS = {
    "system_program": SYSTEM_PROGRAM_ID,
    "associated_token_program": ASSOCIATED_SPL_PROGRAM_ID,
    "program": PUMP_AMM_PROGRAM_ID,
    "fee_program": PUMP_FEE_PROGRAM_ID,
}


@dataclass(frozen=True, slots=True)
class CompiledPumpSwapInstruction:
    """Protocol-neutral compiled PumpSwap instruction envelope."""

    as_of_slot: Slot
    program_id: str
    account_indices: tuple[int, ...]
    data: bytes
    transaction_index: int | None
    outer_instruction_index: int
    program_id_index: int | None = None
    account_pubkeys: tuple[str, ...] | None = None
    account_role_proofs: tuple[AccountRoleProof, ...] = ()
    signature: bytes | None = None
    transaction_slot_account_state_available: bool = False
    inner_instruction_group_index: int | None = None
    inner_instruction_index: int | None = None


@dataclass(frozen=True, slots=True)
class _TradeInstructionSchema:
    name: str
    side: TradeSide
    discriminator: bytes
    required_account_names: tuple[str, ...]
    allowed_data_lengths: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class _DecodedArgs:
    base_amount_base_units: TokenBaseUnits | None = None
    quote_amount_base_units: QuoteBaseUnits | None = None
    max_quote_cost_base_units: QuoteBaseUnits | None = None
    min_base_output_base_units: TokenBaseUnits | None = None
    min_quote_output_base_units: QuoteBaseUnits | None = None
    track_volume: bool | None = None


TradeInstructionDecodeResult = PumpSwapTradeInstructionEvidence | AbstainResult

_TRADE_SCHEMAS = {
    BUY_DISCRIMINATOR: _TradeInstructionSchema(
        name="buy",
        side=TradeSide.BUY,
        discriminator=BUY_DISCRIMINATOR,
        required_account_names=BUY_ACCOUNT_NAMES,
        allowed_data_lengths=(
            DISCRIMINATOR_SIZE + U64_SIZE * 2,
            DISCRIMINATOR_SIZE + U64_SIZE * 2 + 1,
        ),
    ),
    BUY_EXACT_QUOTE_IN_DISCRIMINATOR: _TradeInstructionSchema(
        name="buy_exact_quote_in",
        side=TradeSide.BUY,
        discriminator=BUY_EXACT_QUOTE_IN_DISCRIMINATOR,
        required_account_names=BUY_ACCOUNT_NAMES,
        allowed_data_lengths=(
            DISCRIMINATOR_SIZE + U64_SIZE * 2,
            DISCRIMINATOR_SIZE + U64_SIZE * 2 + 1,
        ),
    ),
    SELL_DISCRIMINATOR: _TradeInstructionSchema(
        name="sell",
        side=TradeSide.SELL,
        discriminator=SELL_DISCRIMINATOR,
        required_account_names=SELL_ACCOUNT_NAMES,
        allowed_data_lengths=(DISCRIMINATOR_SIZE + U64_SIZE * 2,),
    ),
}


def decode_pump_swap_trade_instruction(
    instruction: CompiledPumpSwapInstruction,
    *,
    idl_hash: str,
    decoder_version: str = PUMP_SWAP_TRADE_DECODER_VERSION,
) -> TradeInstructionDecodeResult:
    """Decode a PumpSwap trade instruction using the pinned account layout."""

    context_error = _validate_context(instruction, idl_hash, decoder_version)
    if context_error is not None:
        return context_error

    schema = _TRADE_SCHEMAS.get(instruction.data[:DISCRIMINATOR_SIZE])
    if schema is None:
        return _abstain(
            AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
            "unsupported PumpSwap trade instruction discriminator",
            instruction.as_of_slot,
        )
    layout_error = _validate_layout(instruction, schema)
    if layout_error is not None:
        return layout_error
    decoded_args = _decode_args(instruction, schema)
    if isinstance(decoded_args, AbstainResult):
        return decoded_args
    return _build_evidence(
        instruction=instruction,
        schema=schema,
        args=decoded_args,
        idl_hash=idl_hash,
        decoder_version=decoder_version,
    )


def _validate_context(
    instruction: CompiledPumpSwapInstruction,
    idl_hash: str,
    decoder_version: str,
) -> AbstainResult | None:
    checks = (
        (
            type(instruction.as_of_slot) is not int or instruction.as_of_slot < 0,
            AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
            "as_of_slot must be a non-negative integer",
        ),
        (
            instruction.program_id != PUMP_AMM_PROGRAM_ID,
            AbstainReason.UNKNOWN_PROTOCOL_STATE,
            "instruction program_id is not the pinned PumpSwap program",
        ),
        (
            idl_hash != PINNED_PUMP_SWAP_IDL_SHA256,
            AbstainReason.DECODER_MISMATCH,
            "PumpSwap IDL hash does not match the pinned decoder",
        ),
        (
            decoder_version != PUMP_SWAP_TRADE_DECODER_VERSION,
            AbstainReason.DECODER_MISMATCH,
            "decoder_version does not match the pinned PumpSwap decoder",
        ),
        (
            len(instruction.data) < DISCRIMINATOR_SIZE,
            AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
            "instruction data is shorter than discriminator",
        ),
        (
            instruction.outer_instruction_index < 0,
            AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
            "outer_instruction_index must be non-negative",
        ),
        (
            any(index < 0 for index in instruction.account_indices),
            AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
            "account indices must be non-negative",
        ),
        (
            instruction.account_pubkeys is None,
            AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
            "resolved account_pubkeys are required to prove account layout",
        ),
        (
            instruction.program_id_index is None,
            AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
            "program_id_index is required to prove account layout",
        ),
        (
            instruction.program_id_index is not None
            and instruction.program_id_index < 0,
            AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
            "program_id_index must be non-negative",
        ),
    )
    for failed, reason, message in checks:
        if failed:
            return _abstain(reason, message, instruction.as_of_slot)
    return _validate_account_key_bounds(instruction)


def _validate_account_key_bounds(
    instruction: CompiledPumpSwapInstruction,
) -> AbstainResult | None:
    account_pubkeys = instruction.account_pubkeys
    program_id_index = instruction.program_id_index
    if account_pubkeys is None or program_id_index is None:
        return None
    if any(index >= len(account_pubkeys) for index in instruction.account_indices):
        return _abstain(
            AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
            "account index is outside supplied account_pubkeys",
            instruction.as_of_slot,
        )
    if program_id_index >= len(account_pubkeys):
        return _abstain(
            AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
            "program_id_index is outside supplied account_pubkeys",
            instruction.as_of_slot,
        )
    if account_pubkeys[program_id_index] != instruction.program_id:
        return _abstain(
            AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
            "program_id_index does not resolve to instruction program_id",
            instruction.as_of_slot,
        )
    return None


def _validate_layout(
    instruction: CompiledPumpSwapInstruction,
    schema: _TradeInstructionSchema,
) -> AbstainResult | None:
    if len(instruction.data) not in schema.allowed_data_lengths:
        return _abstain(
            AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
            f"{schema.name} instruction data length is unsupported",
            instruction.as_of_slot,
        )
    if len(instruction.account_indices) < len(schema.required_account_names):
        return _abstain(
            AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
            f"{schema.name} required accounts are missing",
            instruction.as_of_slot,
        )
    fixed_error = _validate_fixed_accounts(instruction, schema)
    if fixed_error is not None:
        return fixed_error
    return _validate_role_proofs(instruction, schema)


def _validate_fixed_accounts(
    instruction: CompiledPumpSwapInstruction,
    schema: _TradeInstructionSchema,
) -> AbstainResult | None:
    account_pubkeys = instruction.account_pubkeys
    if account_pubkeys is None:
        return _abstain(
            AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
            "resolved account_pubkeys are required to prove account layout",
            instruction.as_of_slot,
        )
    for name, expected_pubkey in FIXED_ACCOUNT_PUBKEYS.items():
        if name not in schema.required_account_names:
            continue
        position = _account_position(schema, name)
        compiled_index = instruction.account_indices[position]
        if account_pubkeys[compiled_index] != expected_pubkey:
            return _abstain(
                AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
                f"{schema.name} {name} account does not match IDL",
                instruction.as_of_slot,
            )
    return None


def _validate_role_proofs(
    instruction: CompiledPumpSwapInstruction,
    schema: _TradeInstructionSchema,
) -> AbstainResult | None:
    account_pubkeys = instruction.account_pubkeys
    if account_pubkeys is None:
        return _abstain(
            AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
            "resolved account_pubkeys are required to prove account layout",
            instruction.as_of_slot,
        )
    proofs: dict[str, str] = {}
    for proof in instruction.account_role_proofs:
        if proof.name in proofs:
            return _abstain(
                AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
                "duplicate account role proof",
                instruction.as_of_slot,
            )
        proofs[proof.name] = proof.pubkey
    if set(proofs) != set(schema.required_account_names):
        return _abstain(
            AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
            f"{schema.name} account role proof set is incomplete",
            instruction.as_of_slot,
        )
    for name in schema.required_account_names:
        position = _account_position(schema, name)
        if account_pubkeys[instruction.account_indices[position]] != proofs[name]:
            return _abstain(
                AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
                f"{schema.name} account role proof mismatch",
                instruction.as_of_slot,
            )
    return None


def _decode_args(  # noqa: PLR0911
    instruction: CompiledPumpSwapInstruction,
    schema: _TradeInstructionSchema,
) -> _DecodedArgs | AbstainResult:
    first = _u64_at(instruction.data, DISCRIMINATOR_SIZE)
    second = _u64_at(instruction.data, DISCRIMINATOR_SIZE + U64_SIZE)
    track_volume = None
    if len(instruction.data) == DISCRIMINATOR_SIZE + U64_SIZE * 2 + 1:
        value = instruction.data[-1]
        if value not in (0, 1):
            return _abstain(
                AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
                "PumpSwap track_volume bool is unsupported",
                instruction.as_of_slot,
            )
        track_volume = bool(value)
    if first <= 0:
        return _abstain(
            AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
            f"{schema.name} first amount must be positive",
            instruction.as_of_slot,
        )
    if schema.name == "buy":
        if second <= 0:
            return _abstain(
                AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
                "buy max quote cost must be positive",
                instruction.as_of_slot,
            )
        return _DecodedArgs(
            base_amount_base_units=TokenBaseUnits(first),
            max_quote_cost_base_units=QuoteBaseUnits(second),
            track_volume=track_volume,
        )
    if schema.name == "buy_exact_quote_in":
        if second <= 0:
            return _abstain(
                AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
                "buy_exact_quote_in minimum base output must be positive",
                instruction.as_of_slot,
            )
        return _DecodedArgs(
            quote_amount_base_units=QuoteBaseUnits(first),
            min_base_output_base_units=TokenBaseUnits(second),
            track_volume=track_volume,
        )
    return _DecodedArgs(
        base_amount_base_units=TokenBaseUnits(first),
        min_quote_output_base_units=QuoteBaseUnits(second),
    )


def _build_evidence(
    *,
    instruction: CompiledPumpSwapInstruction,
    schema: _TradeInstructionSchema,
    args: _DecodedArgs,
    idl_hash: str,
    decoder_version: str,
) -> PumpSwapTradeInstructionEvidence:
    missing_evidence = ()
    if not instruction.transaction_slot_account_state_available:
        missing_evidence = ("transaction_slot_account_state",)
    return PumpSwapTradeInstructionEvidence(
        as_of_slot=instruction.as_of_slot,
        program_id=instruction.program_id,
        program_id_index=instruction.program_id_index,
        signature=instruction.signature,
        instruction_name=schema.name,
        side=schema.side,
        account_indices=instruction.account_indices,
        account_pubkeys=instruction.account_pubkeys,
        account_role_proofs=tuple(
            (proof.name, proof.pubkey) for proof in instruction.account_role_proofs
        ),
        required_account_names=schema.required_account_names,
        remaining_account_indices=instruction.account_indices[
            len(schema.required_account_names) :
        ],
        transaction_index=instruction.transaction_index,
        outer_instruction_index=instruction.outer_instruction_index,
        inner_instruction_group_index=instruction.inner_instruction_group_index,
        inner_instruction_index=instruction.inner_instruction_index,
        pool_account_index=_account_index(instruction, schema, "pool"),
        user_account_index=_account_index(instruction, schema, "user"),
        global_config_account_index=_account_index(
            instruction, schema, "global_config"
        ),
        base_mint_account_index=_account_index(instruction, schema, "base_mint"),
        quote_mint_account_index=_account_index(instruction, schema, "quote_mint"),
        pool_base_token_account_index=_account_index(
            instruction, schema, "pool_base_token_account"
        ),
        pool_quote_token_account_index=_account_index(
            instruction, schema, "pool_quote_token_account"
        ),
        base_token_program_account_index=_account_index(
            instruction, schema, "base_token_program"
        ),
        quote_token_program_account_index=_account_index(
            instruction, schema, "quote_token_program"
        ),
        fee_config_account_index=_account_index(instruction, schema, "fee_config"),
        fee_program_account_index=_account_index(instruction, schema, "fee_program"),
        base_amount_base_units=args.base_amount_base_units,
        quote_amount_base_units=args.quote_amount_base_units,
        max_quote_cost_base_units=args.max_quote_cost_base_units,
        min_base_output_base_units=args.min_base_output_base_units,
        min_quote_output_base_units=args.min_quote_output_base_units,
        track_volume=args.track_volume,
        transaction_slot_account_state_available=(
            instruction.transaction_slot_account_state_available
        ),
        missing_evidence=missing_evidence,
        decoder_version=decoder_version,
        idl_hash=idl_hash,
    )


def _account_position(schema: _TradeInstructionSchema, name: str) -> int:
    return schema.required_account_names.index(name)


def _account_index(
    instruction: CompiledPumpSwapInstruction,
    schema: _TradeInstructionSchema,
    name: str,
) -> int:
    return instruction.account_indices[_account_position(schema, name)]


def _u64_at(data: bytes, offset: int) -> int:
    return int(unpack_from("<Q", data, offset)[0])


def _abstain(
    reason: AbstainReason,
    message: str,
    as_of_slot: Slot,
) -> AbstainResult:
    return AbstainResult(reason=reason, message=message, as_of_slot=int(as_of_slot))
