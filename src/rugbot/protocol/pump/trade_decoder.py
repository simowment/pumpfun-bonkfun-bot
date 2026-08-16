"""Pure Pump trade instruction decoder for pinned IDL evidence."""

from dataclasses import dataclass
from struct import unpack_from

from rugbot.domain.account_roles import AccountRoleProof
from rugbot.domain.amounts import QuoteBaseUnits, Slot, TokenBaseUnits
from rugbot.domain.decisions import AbstainReason, AbstainResult
from rugbot.domain.trades import PumpTradeInstructionEvidence, TradeSide

PUMP_PROGRAM_ID = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
SYSTEM_PROGRAM_ID = "11111111111111111111111111111111"
PUMP_FEE_PROGRAM_ID = "pfeeUxB6jkeY1Hxd7CsFCAjcbHA9rWtchMGdZ6VojVZ"
PINNED_PUMP_IDL_SHA256 = (
    "662f9afea2feb1a4318852b65d4c1f642f1fdae8d3c9228478efd01d42dfa41d"
)
PUMP_TRADE_DECODER_VERSION = "pump-trade-instruction-v1"

BUY_DISCRIMINATOR = bytes([102, 6, 61, 18, 1, 218, 235, 234])
SELL_DISCRIMINATOR = bytes([51, 230, 133, 164, 1, 127, 131, 173])
DISCRIMINATOR_SIZE = 8
U64_SIZE = 8
BOOL_SIZE = 1

BUY_ACCOUNT_NAMES = (
    "global",
    "fee_recipient",
    "mint",
    "bonding_curve",
    "associated_bonding_curve",
    "associated_user",
    "user",
    "system_program",
    "token_program",
    "creator_vault",
    "event_authority",
    "program",
    "global_volume_accumulator",
    "user_volume_accumulator",
    "fee_config",
    "fee_program",
)
SELL_ACCOUNT_NAMES = (
    "global",
    "fee_recipient",
    "mint",
    "bonding_curve",
    "associated_bonding_curve",
    "associated_user",
    "user",
    "system_program",
    "creator_vault",
    "token_program",
    "event_authority",
    "program",
    "fee_config",
    "fee_program",
)
FIXED_ACCOUNT_PUBKEYS = {
    "system_program": SYSTEM_PROGRAM_ID,
    "program": PUMP_PROGRAM_ID,
    "fee_program": PUMP_FEE_PROGRAM_ID,
}


@dataclass(frozen=True, slots=True)
class CompiledPumpInstruction:
    """Protocol-neutral compiled instruction envelope."""

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
    data_length: int


@dataclass(frozen=True, slots=True)
class _DecodedArgs:
    base_amount_base_units: TokenBaseUnits | None = None
    quote_amount_base_units: QuoteBaseUnits | None = None
    max_quote_cost_base_units: QuoteBaseUnits | None = None
    min_base_output_base_units: TokenBaseUnits | None = None
    min_quote_output_base_units: QuoteBaseUnits | None = None
    track_volume: bool | None = None


TradeInstructionDecodeResult = PumpTradeInstructionEvidence | AbstainResult

_TRADE_SCHEMAS = {
    BUY_DISCRIMINATOR: _TradeInstructionSchema(
        name="buy",
        side=TradeSide.BUY,
        discriminator=BUY_DISCRIMINATOR,
        required_account_names=BUY_ACCOUNT_NAMES,
        data_length=DISCRIMINATOR_SIZE + U64_SIZE + U64_SIZE + BOOL_SIZE,
    ),
    SELL_DISCRIMINATOR: _TradeInstructionSchema(
        name="sell",
        side=TradeSide.SELL,
        discriminator=SELL_DISCRIMINATOR,
        required_account_names=SELL_ACCOUNT_NAMES,
        data_length=DISCRIMINATOR_SIZE + U64_SIZE + U64_SIZE,
    ),
}


def decode_pump_trade_instruction(
    instruction: CompiledPumpInstruction,
    *,
    idl_hash: str,
    decoder_version: str = PUMP_TRADE_DECODER_VERSION,
) -> TradeInstructionDecodeResult:
    """Decode Pump trade instruction evidence from a pinned IDL layout.

    Args:
        instruction: Compiled instruction envelope from a finalized transaction.
        idl_hash: SHA-256 of the Pump IDL used to authorize the decoder.
        decoder_version: Version of this decoder.

    Returns:
        PumpTradeInstructionEvidence on supported trade layouts, otherwise
        AbstainResult. This function is pure and performs no RPC or database
        access.
    """

    validation = _validate_decoder_context(instruction, idl_hash, decoder_version)
    if validation is not None:
        return validation

    discriminator = instruction.data[:DISCRIMINATOR_SIZE]
    schema = _TRADE_SCHEMAS.get(discriminator)
    if schema is None:
        return _abstain(
            reason=AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
            message="unsupported Pump trade instruction discriminator",
            as_of_slot=instruction.as_of_slot,
        )

    layout_error = _validate_instruction_layout(instruction, schema)
    if layout_error is not None:
        return layout_error

    decoded_args = _decode_args(instruction, schema)
    if isinstance(decoded_args, AbstainResult):
        return decoded_args

    return _build_trade_instruction(
        instruction=instruction,
        schema=schema,
        decoded_args=decoded_args,
        idl_hash=idl_hash,
        decoder_version=decoder_version,
    )


def _validate_decoder_context(
    instruction: CompiledPumpInstruction,
    idl_hash: str,
    decoder_version: str,
) -> AbstainResult | None:
    failed_check = _first_context_validation_failure(
        instruction, idl_hash, decoder_version
    )
    if failed_check is None:
        return None
    reason, message = failed_check
    return _abstain(
        reason=reason,
        message=message,
        as_of_slot=instruction.as_of_slot,
    )


def _first_context_validation_failure(
    instruction: CompiledPumpInstruction,
    idl_hash: str,
    decoder_version: str,
) -> tuple[AbstainReason, str] | None:
    checks = (
        (
            type(instruction.as_of_slot) is not int or instruction.as_of_slot < 0,
            AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
            "as_of_slot must be a non-negative integer",
        ),
        (
            instruction.program_id != PUMP_PROGRAM_ID,
            AbstainReason.UNKNOWN_PROTOCOL_STATE,
            "instruction program_id is not the pinned Pump program",
        ),
        (
            idl_hash != PINNED_PUMP_IDL_SHA256,
            AbstainReason.DECODER_MISMATCH,
            "Pump IDL hash does not match the pinned decoder",
        ),
        (
            decoder_version != PUMP_TRADE_DECODER_VERSION,
            AbstainReason.DECODER_MISMATCH,
            "decoder_version does not match the pinned trade decoder",
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
            instruction.program_id_index is not None
            and instruction.program_id_index < 0,
            AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
            "program_id_index must be non-negative when supplied",
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
    )
    for failed, reason, message in checks:
        if failed:
            return reason, message
    return _account_key_validation_failure(instruction)


def _account_key_validation_failure(
    instruction: CompiledPumpInstruction,
) -> tuple[AbstainReason, str] | None:
    account_pubkeys = instruction.account_pubkeys
    if account_pubkeys is None:
        return None
    if any(index >= len(account_pubkeys) for index in instruction.account_indices):
        return (
            AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
            "account index is outside supplied account_pubkeys",
        )
    program_id_index = instruction.program_id_index
    if program_id_index is None:
        return None
    if program_id_index >= len(account_pubkeys):
        return (
            AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
            "program_id_index is outside supplied account_pubkeys",
        )
    if account_pubkeys[program_id_index] != instruction.program_id:
        return (
            AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
            "program_id_index does not resolve to instruction program_id",
        )
    return None


def _validate_instruction_layout(
    instruction: CompiledPumpInstruction,
    schema: _TradeInstructionSchema,
) -> AbstainResult | None:
    if len(instruction.data) != schema.data_length:
        return _abstain(
            reason=AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
            message=f"{schema.name} instruction data length is unsupported",
            as_of_slot=instruction.as_of_slot,
        )
    if len(instruction.account_indices) < len(schema.required_account_names):
        return _abstain(
            reason=AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
            message=f"{schema.name} required accounts are missing",
            as_of_slot=instruction.as_of_slot,
        )
    fixed_account_error = _validate_fixed_account_pubkeys(instruction, schema)
    if fixed_account_error is not None:
        return fixed_account_error
    return _validate_account_role_proofs(instruction, schema)


def _validate_fixed_account_pubkeys(
    instruction: CompiledPumpInstruction,
    schema: _TradeInstructionSchema,
) -> AbstainResult | None:
    account_pubkeys = instruction.account_pubkeys
    if account_pubkeys is None:
        return _abstain(
            reason=AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
            message="resolved account_pubkeys are required to prove account layout",
            as_of_slot=instruction.as_of_slot,
        )

    for account_name, expected_pubkey in FIXED_ACCOUNT_PUBKEYS.items():
        if account_name not in schema.required_account_names:
            continue
        compiled_index = _account_index(instruction, schema, account_name)
        if account_pubkeys[compiled_index] != expected_pubkey:
            return _abstain(
                reason=AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
                message=f"{schema.name} {account_name} account does not match IDL",
                as_of_slot=instruction.as_of_slot,
            )
    return None


def _validate_account_role_proofs(
    instruction: CompiledPumpInstruction,
    schema: _TradeInstructionSchema,
) -> AbstainResult | None:
    account_pubkeys = instruction.account_pubkeys
    if account_pubkeys is None:
        return _abstain(
            reason=AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
            message="resolved account_pubkeys are required to prove account layout",
            as_of_slot=instruction.as_of_slot,
        )

    proof_by_name: dict[str, str] = {}
    for proof in instruction.account_role_proofs:
        if proof.name in proof_by_name:
            return _abstain(
                reason=AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
                message="duplicate account role proof",
                as_of_slot=instruction.as_of_slot,
            )
        proof_by_name[proof.name] = proof.pubkey

    required_names = set(schema.required_account_names)
    if set(proof_by_name) != required_names:
        return _abstain(
            reason=AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
            message=f"{schema.name} account role proof set is incomplete",
            as_of_slot=instruction.as_of_slot,
        )

    for account_name in schema.required_account_names:
        compiled_index = _account_index(instruction, schema, account_name)
        if account_pubkeys[compiled_index] != proof_by_name[account_name]:
            return _abstain(
                reason=AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
                message=f"{schema.name} account role proof mismatch",
                as_of_slot=instruction.as_of_slot,
            )
    return None


def _decode_args(
    instruction: CompiledPumpInstruction,
    schema: _TradeInstructionSchema,
) -> _DecodedArgs | AbstainResult:
    if schema.discriminator == BUY_DISCRIMINATOR:
        track_volume = _decode_bool(instruction.data[DISCRIMINATOR_SIZE + 16])
        if track_volume is None:
            return _abstain(
                reason=AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
                message="buy track_volume bool is unsupported",
                as_of_slot=instruction.as_of_slot,
            )
        base_amount = _u64_at(instruction.data, 8)
        max_quote_cost = _u64_at(instruction.data, 16)
        if base_amount <= 0 or max_quote_cost <= 0:
            return _abstain(
                reason=AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
                message="buy amounts must be positive",
                as_of_slot=instruction.as_of_slot,
            )
        return _DecodedArgs(
            base_amount_base_units=TokenBaseUnits(base_amount),
            max_quote_cost_base_units=QuoteBaseUnits(max_quote_cost),
            track_volume=track_volume,
        )
    base_amount = _u64_at(instruction.data, 8)
    if base_amount <= 0:
        return _abstain(
            reason=AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
            message="sell base amount must be positive",
            as_of_slot=instruction.as_of_slot,
        )
    return _DecodedArgs(
        base_amount_base_units=TokenBaseUnits(base_amount),
        min_quote_output_base_units=QuoteBaseUnits(_u64_at(instruction.data, 16)),
    )


def _decode_bool(value: int) -> bool | None:
    if value == 0:
        return False
    if value == 1:
        return True
    return None


def _u64_at(data: bytes, offset: int) -> int:
    return int(unpack_from("<Q", data, offset)[0])


def _build_trade_instruction(
    *,
    instruction: CompiledPumpInstruction,
    schema: _TradeInstructionSchema,
    decoded_args: _DecodedArgs,
    idl_hash: str,
    decoder_version: str,
) -> PumpTradeInstructionEvidence:
    missing_evidence = ()
    if not instruction.transaction_slot_account_state_available:
        missing_evidence = ("transaction_slot_account_state",)

    return PumpTradeInstructionEvidence(
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
        mint_account_index=_account_index(instruction, schema, "mint"),
        bonding_curve_account_index=_account_index(
            instruction, schema, "bonding_curve"
        ),
        associated_bonding_curve_account_index=_account_index(
            instruction, schema, "associated_bonding_curve"
        ),
        associated_user_account_index=_account_index(
            instruction, schema, "associated_user"
        ),
        user_account_index=_account_index(instruction, schema, "user"),
        token_program_account_index=_account_index(
            instruction, schema, "token_program"
        ),
        fee_config_account_index=_account_index(instruction, schema, "fee_config"),
        fee_program_account_index=_account_index(instruction, schema, "fee_program"),
        base_amount_base_units=decoded_args.base_amount_base_units,
        quote_amount_base_units=decoded_args.quote_amount_base_units,
        max_quote_cost_base_units=decoded_args.max_quote_cost_base_units,
        min_base_output_base_units=decoded_args.min_base_output_base_units,
        min_quote_output_base_units=decoded_args.min_quote_output_base_units,
        track_volume=decoded_args.track_volume,
        transaction_slot_account_state_available=(
            instruction.transaction_slot_account_state_available
        ),
        missing_evidence=missing_evidence,
        decoder_version=decoder_version,
        idl_hash=idl_hash,
    )


def _account_index(
    instruction: CompiledPumpInstruction,
    schema: _TradeInstructionSchema,
    account_name: str,
) -> int:
    position = schema.required_account_names.index(account_name)
    return instruction.account_indices[position]


def _abstain(
    *,
    reason: AbstainReason,
    message: str,
    as_of_slot: Slot,
) -> AbstainResult:
    return AbstainResult(
        reason=reason,
        message=message,
        as_of_slot=int(as_of_slot),
    )
