"""Integer-only quote interfaces for Pump curve and canonical PumpSwap paths."""

from dataclasses import dataclass

from rugbot.domain.amounts import QuoteBaseUnits, Slot, TokenBaseUnits
from rugbot.domain.decisions import AbstainReason, AbstainResult
from rugbot.domain.fees import BASIS_POINTS_DENOMINATOR, FeeConfig
from rugbot.domain.quotes import ExecutableQuote, QuotePath

MAX_SUPPORTED_DECIMALS = 18
CANONICAL_PUMP_PROGRAM_CONFIG_VERSION = "pump-global-v1"
CANONICAL_PUMPSWAP_PROGRAM_CONFIG_VERSION = "pump-amm-v1"
PUMP_SWAP_POOL_DECODER_VERSION = "pump-swap-pool-v1"


@dataclass(frozen=True, slots=True)
class PoolReserves:
    """Decoded integer reserve state for a base/quote market."""

    virtual_base_reserves: TokenBaseUnits
    virtual_quote_reserves: QuoteBaseUnits
    real_base_reserves: TokenBaseUnits | None
    real_quote_reserves: QuoteBaseUnits | None
    is_complete: bool | None
    as_of_slot: Slot
    base_decimals: int | None
    quote_decimals: int | None
    decoder_version: str
    idl_hash: str | None
    program_config_version: str | None


QuoteOutcome = ExecutableQuote | AbstainResult


@dataclass(frozen=True, slots=True)
class _ValidatedPoolSnapshot:
    real_base_reserves: TokenBaseUnits
    real_quote_reserves: QuoteBaseUnits
    base_decimals: int
    quote_decimals: int
    idl_hash: str
    program_config_version: str


@dataclass(frozen=True, slots=True)
class _ValidatedQuoteContext:
    path: QuotePath
    fee_config: FeeConfig
    pool_snapshot: _ValidatedPoolSnapshot


def executable_buy_quote(
    *,
    path: QuotePath,
    reserves: PoolReserves,
    quote_input_amount: QuoteBaseUnits,
    fee_config: FeeConfig | None,
) -> QuoteOutcome:
    """Quote a buy using exact integer arithmetic.

    Args:
        path: Market path used for the quote.
        reserves: Current integer reserves.
        quote_input_amount: Quote asset amount in base units.
        fee_config: Historical fee configuration.

    Returns:
        Executable quote, or AbstainResult when state is unsupported.
    """

    context = _prepare_quote_context(path, reserves, fee_config)
    if isinstance(context, AbstainResult):
        return context

    if type(quote_input_amount) is not int or quote_input_amount <= 0:
        return _abstain_unsupported(
            "quote_input_amount must be a positive integer", reserves
        )

    if context.path == QuotePath.PUMP_BONDING_CURVE:
        quote_after_fee, fee_amount = _pump_bonding_curve_net_quote_in(
            spendable_quote_in=int(quote_input_amount),
            fee_config=context.fee_config,
        )
    else:
        quote_after_fee, fee_amount = _pump_swap_net_quote_in(
            spendable_quote_in=int(quote_input_amount),
            fee_config=context.fee_config,
        )
    amount_in = (
        max(0, quote_after_fee - 1)
        if context.path == QuotePath.PUMP_BONDING_CURVE
        else quote_after_fee
    )
    output_amount = _constant_product_amount_out(
        amount_in=amount_in,
        input_reserves=int(reserves.virtual_quote_reserves),
        output_reserves=int(reserves.virtual_base_reserves),
    )
    if output_amount <= 0:
        return _abstain_unsupported("quote output would be zero", reserves)
    if output_amount > int(context.pool_snapshot.real_base_reserves):
        return _abstain_unsupported("insufficient real base reserves", reserves)

    return _build_executable_quote(
        reserves=reserves,
        context=context,
        input_amount_base_units=int(quote_input_amount),
        output_amount_base_units=output_amount,
        fee_amount_base_units=fee_amount,
    )


def executable_sell_quote(
    *,
    path: QuotePath,
    reserves: PoolReserves,
    base_input_amount: TokenBaseUnits,
    fee_config: FeeConfig | None,
) -> QuoteOutcome:
    """Quote a sell using exact integer arithmetic.

    Args:
        path: Market path used for the quote.
        reserves: Current integer reserves.
        base_input_amount: Base token amount in base units.
        fee_config: Historical fee configuration.

    Returns:
        Executable quote, or AbstainResult when state is unsupported.
    """

    context = _prepare_quote_context(path, reserves, fee_config)
    if isinstance(context, AbstainResult):
        return context

    if type(base_input_amount) is not int or base_input_amount <= 0:
        return _abstain_unsupported(
            "base_input_amount must be a positive integer", reserves
        )

    quote_before_fee = _constant_product_amount_out(
        amount_in=int(base_input_amount),
        input_reserves=int(reserves.virtual_base_reserves),
        output_reserves=int(reserves.virtual_quote_reserves),
    )
    if context.path == QuotePath.PUMP_BONDING_CURVE:
        fee_amount = _ceil_fee_components(quote_before_fee, context.fee_config)
    else:
        fee_amount = _ceil_swap_fee_components(quote_before_fee, context.fee_config)
    output_amount = quote_before_fee - fee_amount
    if output_amount <= 0:
        return _abstain_unsupported("quote output would be zero after fees", reserves)
    if output_amount > int(context.pool_snapshot.real_quote_reserves):
        return _abstain_unsupported("insufficient real quote reserves", reserves)

    return _build_executable_quote(
        reserves=reserves,
        context=context,
        input_amount_base_units=int(base_input_amount),
        output_amount_base_units=output_amount,
        fee_amount_base_units=fee_amount,
    )


def _pump_bonding_curve_net_quote_in(
    *,
    spendable_quote_in: int,
    fee_config: FeeConfig,
) -> tuple[int, int]:
    """Return net quote input and fees per Pump buy_exact_sol_in IDL docs."""

    net_quote = (
        spendable_quote_in
        * BASIS_POINTS_DENOMINATOR
        // (BASIS_POINTS_DENOMINATOR + fee_config.total_fee_bps)
    )
    total_fee = _ceil_fee_components(net_quote, fee_config)
    total_spend = net_quote + total_fee
    if total_spend > spendable_quote_in:
        net_quote -= total_spend - spendable_quote_in
        total_fee = _ceil_fee_components(net_quote, fee_config)

    return max(0, net_quote), total_fee


def _pump_swap_net_quote_in(
    *,
    spendable_quote_in: int,
    fee_config: FeeConfig,
) -> tuple[int, int]:
    """Return effective PumpSwap input and ceil-rounded swap fees."""

    net_quote = (
        spendable_quote_in
        * BASIS_POINTS_DENOMINATOR
        // (BASIS_POINTS_DENOMINATOR + fee_config.swap_total_fee_bps)
    )
    total_fee = _ceil_swap_fee_components(net_quote, fee_config)
    total_spend = net_quote + total_fee
    if total_spend > spendable_quote_in:
        net_quote -= total_spend - spendable_quote_in
        total_fee = _ceil_swap_fee_components(net_quote, fee_config)
    return max(0, net_quote), total_fee


def _ceil_fee_components(amount_base_units: int, fee_config: FeeConfig) -> int:
    protocol_fee = _ceil_div(
        amount_base_units * fee_config.protocol_fee_bps,
        BASIS_POINTS_DENOMINATOR,
    )
    creator_fee = _ceil_div(
        amount_base_units * fee_config.creator_fee_bps,
        BASIS_POINTS_DENOMINATOR,
    )
    return protocol_fee + creator_fee


def _ceil_swap_fee_components(amount_base_units: int, fee_config: FeeConfig) -> int:
    """Return ceil-rounded LP, protocol, and creator fees."""

    lp_fee = _ceil_div(
        amount_base_units * fee_config.lp_fee_bps,
        BASIS_POINTS_DENOMINATOR,
    )
    return lp_fee + _ceil_fee_components(amount_base_units, fee_config)


def _ceil_div(numerator: int, denominator: int) -> int:
    return (numerator + denominator - 1) // denominator


def _constant_product_amount_out(
    *,
    amount_in: int,
    input_reserves: int,
    output_reserves: int,
) -> int:
    numerator = amount_in * output_reserves
    denominator = input_reserves + amount_in
    if denominator <= 0:
        return 0
    return numerator // denominator


def _prepare_quote_context(
    path: QuotePath,
    reserves: PoolReserves,
    fee_config: FeeConfig | None,
) -> _ValidatedQuoteContext | AbstainResult:
    if path not in (QuotePath.PUMP_BONDING_CURVE, QuotePath.CANONICAL_PUMPSWAP):
        return _abstain_unsupported(
            "quote path is unsupported",
            reserves,
        )

    pool_snapshot = _validate_pool_snapshot(reserves, path=path)
    if isinstance(pool_snapshot, AbstainResult):
        return pool_snapshot

    if fee_config is None:
        return _abstain_unknown_fee_config(reserves.as_of_slot)

    fee_abstention = _validate_fee_config(fee_config, reserves, path=path)
    if fee_abstention is not None:
        return fee_abstention

    return _ValidatedQuoteContext(
        path=path,
        fee_config=fee_config,
        pool_snapshot=pool_snapshot,
    )


def _build_executable_quote(
    *,
    reserves: PoolReserves,
    context: _ValidatedQuoteContext,
    input_amount_base_units: int,
    output_amount_base_units: int,
    fee_amount_base_units: int,
) -> ExecutableQuote:
    return ExecutableQuote(
        path=context.path,
        as_of_slot=reserves.as_of_slot,
        input_amount_base_units=input_amount_base_units,
        output_amount_base_units=output_amount_base_units,
        fee_amount_base_units=fee_amount_base_units,
        base_decimals=context.pool_snapshot.base_decimals,
        quote_decimals=context.pool_snapshot.quote_decimals,
        fee_config_version=context.fee_config.version,
        decoder_version=reserves.decoder_version,
        idl_hash=context.pool_snapshot.idl_hash,
        program_config_version=context.pool_snapshot.program_config_version,
    )


def _abstain_unknown_fee_config(as_of_slot: Slot) -> AbstainResult:
    return AbstainResult(
        reason=AbstainReason.UNKNOWN_FEE_CONFIG,
        message="fee_config is required for executable quotes",
        as_of_slot=int(as_of_slot),
    )


def _abstain_unsupported(
    message: str,
    reserves: PoolReserves,
) -> AbstainResult:
    return AbstainResult(
        reason=AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
        message=message,
        as_of_slot=int(reserves.as_of_slot),
    )


def _abstain_unknown_protocol(
    message: str,
    reserves: PoolReserves,
) -> AbstainResult:
    return AbstainResult(
        reason=AbstainReason.UNKNOWN_PROTOCOL_STATE,
        message=message,
        as_of_slot=int(reserves.as_of_slot),
    )


def _abstain_decoder_mismatch(
    message: str,
    reserves: PoolReserves,
) -> AbstainResult:
    return AbstainResult(
        reason=AbstainReason.DECODER_MISMATCH,
        message=message,
        as_of_slot=int(reserves.as_of_slot),
    )


def _validate_pool_snapshot(
    reserves: PoolReserves,
    *,
    path: QuotePath,
) -> _ValidatedPoolSnapshot | AbstainResult:
    provenance_error = _validate_pool_provenance(reserves, path=path)
    if provenance_error is not None:
        return provenance_error

    decimals = _validated_decimals(reserves)
    if isinstance(decimals, AbstainResult):
        return decimals

    real_reserves = _validated_real_curve_state(reserves, path=path)
    if isinstance(real_reserves, AbstainResult):
        return real_reserves

    reserve_error = _validate_reserve_amounts(reserves)
    if reserve_error is not None:
        return reserve_error

    return _ValidatedPoolSnapshot(
        real_base_reserves=real_reserves[0],
        real_quote_reserves=real_reserves[1],
        base_decimals=decimals[0],
        quote_decimals=decimals[1],
        idl_hash=str(reserves.idl_hash),
        program_config_version=str(reserves.program_config_version),
    )


def _validate_pool_provenance(  # noqa: PLR0911
    reserves: PoolReserves,
    *,
    path: QuotePath,
) -> AbstainResult | None:
    # Import lazily because the pinned account decoder adapts its output to
    # PoolReserves. This keeps the module dependency acyclic while ensuring
    # executable quotes accept only the decoder's fixed provenance.
    from rugbot.protocol.pump.bonding_curve_account import (  # noqa: PLC0415
        PINNED_PUMP_IDL_SHA256,
        PUMP_BONDING_CURVE_ACCOUNT_DECODER_VERSION,
    )
    from rugbot.protocol.pump.swap_trade_decoder import (  # noqa: PLC0415
        PINNED_PUMP_SWAP_IDL_SHA256,
    )

    if type(reserves.as_of_slot) is not int or reserves.as_of_slot < 0:
        return _abstain_unsupported(
            "as_of_slot must be a non-negative integer", reserves
        )
    if path == QuotePath.CANONICAL_PUMPSWAP:
        if reserves.decoder_version != PUMP_SWAP_POOL_DECODER_VERSION:
            return _abstain_decoder_mismatch(
                "decoder_version does not match the pinned PumpSwap pool decoder",
                reserves,
            )
        if reserves.idl_hash != PINNED_PUMP_SWAP_IDL_SHA256:
            return _abstain_decoder_mismatch(
                "idl_hash does not match the pinned PumpSwap decoder",
                reserves,
            )
        if reserves.program_config_version != (
            CANONICAL_PUMPSWAP_PROGRAM_CONFIG_VERSION
        ):
            return _abstain_unknown_protocol(
                "program_config_version is not the canonical PumpSwap config",
                reserves,
            )
        return None

    if reserves.decoder_version != PUMP_BONDING_CURVE_ACCOUNT_DECODER_VERSION:
        return _abstain_decoder_mismatch(
            "decoder_version does not match the pinned bonding-curve decoder",
            reserves,
        )
    if reserves.idl_hash != PINNED_PUMP_IDL_SHA256:
        return _abstain_decoder_mismatch(
            "idl_hash does not match the pinned bonding-curve decoder",
            reserves,
        )
    if reserves.program_config_version != CANONICAL_PUMP_PROGRAM_CONFIG_VERSION:
        return _abstain_unknown_protocol(
            "program_config_version is not the canonical Pump config",
            reserves,
        )
    return None


def _validated_decimals(
    reserves: PoolReserves,
) -> tuple[int, int] | AbstainResult:
    if reserves.base_decimals is None or reserves.quote_decimals is None:
        return _abstain_unknown_protocol("token decimals are required", reserves)
    if not _valid_decimals(reserves.base_decimals):
        return _abstain_unsupported("base_decimals are unsupported", reserves)
    if not _valid_decimals(reserves.quote_decimals):
        return _abstain_unsupported("quote_decimals are unsupported", reserves)
    return reserves.base_decimals, reserves.quote_decimals


def _validated_real_curve_state(
    reserves: PoolReserves,
    *,
    path: QuotePath,
) -> tuple[TokenBaseUnits, QuoteBaseUnits] | AbstainResult:
    if reserves.real_base_reserves is None or reserves.real_quote_reserves is None:
        return _abstain_unknown_protocol("real reserves are required", reserves)
    if path == QuotePath.CANONICAL_PUMPSWAP:
        if reserves.is_complete is not None and type(reserves.is_complete) is not bool:
            return _abstain_unknown_protocol(
                "PumpSwap completion state is malformed", reserves
            )
        return reserves.real_base_reserves, reserves.real_quote_reserves
    if type(reserves.is_complete) is not bool:
        return _abstain_unknown_protocol(
            "bonding curve completion state is required", reserves
        )
    if reserves.is_complete:
        return _abstain_unsupported("bonding curve is complete", reserves)
    return reserves.real_base_reserves, reserves.real_quote_reserves


def _validate_reserve_amounts(reserves: PoolReserves) -> AbstainResult | None:
    amount_fields = (
        ("virtual_base_reserves", reserves.virtual_base_reserves),
        ("virtual_quote_reserves", reserves.virtual_quote_reserves),
        ("real_base_reserves", reserves.real_base_reserves),
        ("real_quote_reserves", reserves.real_quote_reserves),
    )
    for field_name, value in amount_fields:
        if type(value) is not int:
            return _abstain_unsupported(f"{field_name} must be an integer", reserves)
        if value < 0:
            return _abstain_unsupported(f"{field_name} must be non-negative", reserves)
    if reserves.virtual_base_reserves <= 0:
        return _abstain_unsupported("virtual_base_reserves must be positive", reserves)
    if reserves.virtual_quote_reserves <= 0:
        return _abstain_unsupported("virtual_quote_reserves must be positive", reserves)
    return None


def _validate_fee_config(
    fee_config: FeeConfig | None,
    reserves: PoolReserves,
    *,
    path: QuotePath,
) -> AbstainResult | None:
    if fee_config is None:
        return _abstain_unknown_fee_config(reserves.as_of_slot)

    provenance_error = _validate_fee_config_provenance(fee_config, reserves)
    if provenance_error is not None:
        return provenance_error

    slot_error = _validate_fee_config_slot(fee_config, reserves.as_of_slot)
    if slot_error is not None:
        return slot_error

    return _validate_fee_config_bps(fee_config, reserves.as_of_slot, path=path)


def _validate_fee_config_provenance(
    fee_config: FeeConfig,
    reserves: PoolReserves,
) -> AbstainResult | None:
    if (
        type(fee_config.is_known) is not bool
        or not fee_config.is_known
        or type(fee_config.version) is not str
        or not fee_config.version
        or type(fee_config.program_config_version) is not str
        or not fee_config.program_config_version
    ):
        return _abstain_unknown_fee_config(reserves.as_of_slot)
    if fee_config.program_config_version != reserves.program_config_version:
        return AbstainResult(
            reason=AbstainReason.UNKNOWN_FEE_CONFIG,
            message="fee_config does not match pool program_config_version",
            as_of_slot=int(reserves.as_of_slot),
        )
    if (
        fee_config.valid_from_slot is None
        or type(fee_config.valid_from_slot) is not int
    ):
        return _abstain_unknown_fee_config(reserves.as_of_slot)
    if (
        type(fee_config.source_artifact_version) is not str
        or not fee_config.source_artifact_version
    ):
        return _abstain_unknown_fee_config(reserves.as_of_slot)
    return None


def _validate_fee_config_slot(
    fee_config: FeeConfig,
    as_of_slot: Slot,
) -> AbstainResult | None:
    if (
        fee_config.valid_from_slot is None
        or type(fee_config.valid_from_slot) is not int
        or fee_config.valid_from_slot < 0
    ):
        return _abstain_unknown_fee_config(as_of_slot)
    if fee_config.valid_to_slot is not None:
        if type(fee_config.valid_to_slot) is not int or fee_config.valid_to_slot <= (
            fee_config.valid_from_slot
        ):
            return _abstain_unknown_fee_config(as_of_slot)
    if int(as_of_slot) < int(fee_config.valid_from_slot):
        return _abstain_unknown_fee_config(as_of_slot)
    if fee_config.valid_to_slot is not None and int(as_of_slot) >= int(
        fee_config.valid_to_slot
    ):
        return _abstain_unknown_fee_config(as_of_slot)
    return None


def _validate_fee_config_bps(
    fee_config: FeeConfig,
    as_of_slot: Slot,
    *,
    path: QuotePath,
) -> AbstainResult | None:
    bps_values = (
        fee_config.protocol_fee_bps,
        fee_config.creator_fee_bps,
    )
    if path == QuotePath.CANONICAL_PUMPSWAP:
        bps_values += (fee_config.lp_fee_bps,)
    if any(not _valid_fee_bps_type(value) for value in bps_values):
        return AbstainResult(
            reason=AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
            message="fee basis points must be integers",
            as_of_slot=int(as_of_slot),
        )
    if any(value < 0 for value in bps_values):
        return AbstainResult(
            reason=AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
            message="fee basis points must be non-negative",
            as_of_slot=int(as_of_slot),
        )
    total_fee_bps = (
        fee_config.swap_total_fee_bps
        if path == QuotePath.CANONICAL_PUMPSWAP
        else fee_config.total_fee_bps
    )
    if total_fee_bps > BASIS_POINTS_DENOMINATOR:
        return AbstainResult(
            reason=AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
            message="total fee basis points exceed denominator",
            as_of_slot=int(as_of_slot),
        )
    return None


def _valid_fee_bps_type(value: object) -> bool:
    return type(value) is int


def _valid_decimals(decimals: int) -> bool:
    return 0 <= decimals <= MAX_SUPPORTED_DECIMALS
