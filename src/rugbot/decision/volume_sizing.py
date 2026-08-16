"""Pure conservative sizing from bankroll, volume, and pool liquidity."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable

from rugbot.domain.amounts import QuoteBaseUnits, Slot, TokenBaseUnits
from rugbot.domain.decisions import AbstainReason, AbstainResult

PPM_DENOMINATOR = 1_000_000


@dataclass(frozen=True, slots=True)
class VolumeSizingRequest:
    """Point-in-time evidence and limits for one proposed entry.

    ``independent_volume_quote_base_units`` must exclude the target operator,
    linked wallets, and known wash volume before it reaches this pure module.
    The reserve fields describe a constant-product pool immediately before the
    proposed buy.
    """

    as_of_slot: Slot | None
    requested_quote_base_units: QuoteBaseUnits | None
    bankroll_quote_base_units: QuoteBaseUnits | None
    max_bankroll_fraction_ppm: int | None
    independent_volume_quote_base_units: QuoteBaseUnits | None
    max_independent_volume_fraction_ppm: int | None
    pool_quote_reserve_base_units: QuoteBaseUnits | None
    pool_token_reserve_base_units: TokenBaseUnits | None
    max_price_impact_ppm: int | None
    max_one_shot_exit_token_base_units: TokenBaseUnits | None


@dataclass(frozen=True, slots=True)
class ConservativeVolumeSize:
    """Executable integer size and the caps that prove its conservatism."""

    as_of_slot: Slot
    quote_size_base_units: QuoteBaseUnits
    expected_token_output_base_units: TokenBaseUnits
    projected_price_impact_ppm: int
    requested_cap_quote_base_units: QuoteBaseUnits
    bankroll_cap_quote_base_units: QuoteBaseUnits
    independent_volume_cap_quote_base_units: QuoteBaseUnits
    price_impact_cap_quote_base_units: QuoteBaseUnits
    full_exit_cap_quote_base_units: QuoteBaseUnits
    limiting_constraints: tuple[str, ...]


def size_volume_liquidity_aware(
    request: VolumeSizingRequest,
) -> ConservativeVolumeSize | AbstainResult:
    """Select the largest requested size satisfying every supplied limit.

    The constant-product price-impact bound uses the post-trade marginal price
    ratio ``((quote_reserve + input) / quote_reserve) ** 2``. Gross input is
    used without a fee discount, which conservatively overstates both price
    impact and token output for the one-shot exit-capacity check.
    """

    error = _validate_request(request)
    if error is not None:
        return error

    as_of_slot = Slot(request.as_of_slot)
    requested = int(request.requested_quote_base_units)
    bankroll = int(request.bankroll_quote_base_units)
    independent_volume = int(request.independent_volume_quote_base_units)
    quote_reserve = int(request.pool_quote_reserve_base_units)
    token_reserve = int(request.pool_token_reserve_base_units)
    exit_capacity = int(request.max_one_shot_exit_token_base_units)
    bankroll_fraction = int(request.max_bankroll_fraction_ppm)
    volume_fraction = int(request.max_independent_volume_fraction_ppm)
    max_price_impact = int(request.max_price_impact_ppm)

    bankroll_cap = bankroll * bankroll_fraction // PPM_DENOMINATOR
    volume_cap = independent_volume * volume_fraction // PPM_DENOMINATOR
    price_impact_cap = _largest_valid_input(
        upper_bound=requested,
        predicate=lambda quote_input: _price_impact_within_limit(
            quote_input=quote_input,
            quote_reserve=quote_reserve,
            max_price_impact_ppm=max_price_impact,
        ),
    )
    full_exit_cap = _largest_valid_input(
        upper_bound=requested,
        predicate=lambda quote_input: (
            _expected_token_output(
                quote_input=quote_input,
                quote_reserve=quote_reserve,
                token_reserve=token_reserve,
            )
            <= exit_capacity
        ),
    )

    caps = {
        "requested": requested,
        "bankroll": bankroll_cap,
        "independent_volume": volume_cap,
        "price_impact": price_impact_cap,
        "full_exit": full_exit_cap,
    }
    selected = min(caps.values())
    if selected <= 0:
        return _abstain(
            reason=AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
            message="no positive entry size satisfies every sizing constraint",
            as_of_slot=as_of_slot,
        )

    expected_tokens = _expected_token_output(
        quote_input=selected,
        quote_reserve=quote_reserve,
        token_reserve=token_reserve,
    )
    if expected_tokens <= 0:
        return _abstain(
            reason=AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
            message="selected entry size produces no token base units",
            as_of_slot=as_of_slot,
        )

    limiting_constraints = tuple(
        name
        for name, cap in caps.items()
        if cap == selected
        and (name not in {"price_impact", "full_exit"} or cap < requested)
    )
    return ConservativeVolumeSize(
        as_of_slot=as_of_slot,
        quote_size_base_units=QuoteBaseUnits(selected),
        expected_token_output_base_units=TokenBaseUnits(expected_tokens),
        projected_price_impact_ppm=_price_impact_ppm_ceil(
            quote_input=selected,
            quote_reserve=quote_reserve,
        ),
        requested_cap_quote_base_units=QuoteBaseUnits(requested),
        bankroll_cap_quote_base_units=QuoteBaseUnits(bankroll_cap),
        independent_volume_cap_quote_base_units=QuoteBaseUnits(volume_cap),
        price_impact_cap_quote_base_units=QuoteBaseUnits(price_impact_cap),
        full_exit_cap_quote_base_units=QuoteBaseUnits(full_exit_cap),
        limiting_constraints=limiting_constraints,
    )


def _validate_request(request: object) -> AbstainResult | None:
    if not isinstance(request, VolumeSizingRequest):
        return _abstain(
            reason=AbstainReason.MISSING_FEATURE,
            message="volume sizing request is missing or has the wrong type",
            as_of_slot=Slot(0),
        )

    raw_slot = request.as_of_slot
    safe_slot = Slot(raw_slot) if type(raw_slot) is int and raw_slot >= 0 else Slot(0)
    field_error = _validate_required_integer_fields(request, safe_slot)
    if field_error is not None:
        return field_error
    return _validate_numeric_ranges(request, safe_slot)


def _validate_required_integer_fields(
    request: VolumeSizingRequest, safe_slot: Slot
) -> AbstainResult | None:
    fields = (
        ("as_of_slot", request.as_of_slot),
        ("requested_quote_base_units", request.requested_quote_base_units),
        ("bankroll_quote_base_units", request.bankroll_quote_base_units),
        ("max_bankroll_fraction_ppm", request.max_bankroll_fraction_ppm),
        (
            "independent_volume_quote_base_units",
            request.independent_volume_quote_base_units,
        ),
        (
            "max_independent_volume_fraction_ppm",
            request.max_independent_volume_fraction_ppm,
        ),
        ("pool_quote_reserve_base_units", request.pool_quote_reserve_base_units),
        ("pool_token_reserve_base_units", request.pool_token_reserve_base_units),
        ("max_price_impact_ppm", request.max_price_impact_ppm),
        (
            "max_one_shot_exit_token_base_units",
            request.max_one_shot_exit_token_base_units,
        ),
    )
    for name, value in fields:
        if value is None:
            return _abstain(
                reason=AbstainReason.MISSING_FEATURE,
                message=f"{name} is required for conservative volume sizing",
                as_of_slot=safe_slot,
            )
        if type(value) is not int:
            return _abstain(
                reason=AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
                message=f"{name} must be an integer",
                as_of_slot=safe_slot,
            )
    return None


def _validate_numeric_ranges(
    request: VolumeSizingRequest, safe_slot: Slot
) -> AbstainResult | None:

    if request.as_of_slot < 0:
        return _invalid_value("as_of_slot must be non-negative", safe_slot)

    positive_fields = (
        ("requested_quote_base_units", request.requested_quote_base_units),
        ("bankroll_quote_base_units", request.bankroll_quote_base_units),
        ("pool_quote_reserve_base_units", request.pool_quote_reserve_base_units),
        ("pool_token_reserve_base_units", request.pool_token_reserve_base_units),
    )
    for name, value in positive_fields:
        if value <= 0:
            return _invalid_value(f"{name} must be positive", safe_slot)

    non_negative_fields = (
        (
            "independent_volume_quote_base_units",
            request.independent_volume_quote_base_units,
        ),
        (
            "max_one_shot_exit_token_base_units",
            request.max_one_shot_exit_token_base_units,
        ),
    )
    for name, value in non_negative_fields:
        if value < 0:
            return _invalid_value(f"{name} must be non-negative", safe_slot)

    ppm_fields = (
        ("max_bankroll_fraction_ppm", request.max_bankroll_fraction_ppm),
        (
            "max_independent_volume_fraction_ppm",
            request.max_independent_volume_fraction_ppm,
        ),
        ("max_price_impact_ppm", request.max_price_impact_ppm),
    )
    for name, value in ppm_fields:
        if not 0 <= value <= PPM_DENOMINATOR:
            return _invalid_value(
                f"{name} must be between 0 and {PPM_DENOMINATOR}", safe_slot
            )
    return None


def _largest_valid_input(*, upper_bound: int, predicate: Callable[[int], bool]) -> int:
    low = 0
    high = upper_bound
    while low < high:
        candidate = (low + high + 1) // 2
        if predicate(candidate):
            low = candidate
        else:
            high = candidate - 1
    return low


def _price_impact_within_limit(
    *, quote_input: int, quote_reserve: int, max_price_impact_ppm: int
) -> bool:
    impact_numerator = (2 * quote_reserve * quote_input) + quote_input**2
    return impact_numerator * PPM_DENOMINATOR <= max_price_impact_ppm * quote_reserve**2


def _price_impact_ppm_ceil(*, quote_input: int, quote_reserve: int) -> int:
    numerator = ((2 * quote_reserve * quote_input) + quote_input**2) * PPM_DENOMINATOR
    denominator = quote_reserve**2
    return (numerator + denominator - 1) // denominator


def _expected_token_output(
    *, quote_input: int, quote_reserve: int, token_reserve: int
) -> int:
    return token_reserve * quote_input // (quote_reserve + quote_input)


def _invalid_value(message: str, as_of_slot: Slot) -> AbstainResult:
    return _abstain(
        reason=AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
        message=message,
        as_of_slot=as_of_slot,
    )


def _abstain(*, reason: AbstainReason, message: str, as_of_slot: Slot) -> AbstainResult:
    return AbstainResult(reason=reason, message=message, as_of_slot=int(as_of_slot))
