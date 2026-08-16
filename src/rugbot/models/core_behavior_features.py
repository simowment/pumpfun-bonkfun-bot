"""Pure reducer for the minimum adverse-intelligence behavior features."""

from dataclasses import dataclass

from rugbot.domain.amounts import QuoteBaseUnits, Slot, TokenBaseUnits
from rugbot.domain.decisions import AbstainReason, AbstainResult
from rugbot.graph.point_in_time import PROBABILITY_PPM_DENOMINATOR


@dataclass(frozen=True, slots=True)
class CoreBehaviorMarketInput:
    """Trusted market inputs for one decision slot."""

    as_of_slot: Slot
    launch_slot: Slot
    curve_progress_ppm: int
    trusted: bool


@dataclass(frozen=True, slots=True)
class CoreBehaviorFlowInput:
    """Trusted flow and inventory inputs for one decision slot."""

    as_of_slot: Slot
    buy_volume_quote_base_units: QuoteBaseUnits
    sell_volume_quote_base_units: QuoteBaseUnits
    independent_buyer_count: int
    operator_inventory_base_units: TokenBaseUnits
    external_circulating_supply_base_units: TokenBaseUnits
    linked_wallet_sell_volume_quote_base_units: QuoteBaseUnits
    absorbable_external_volume_quote_base_units: QuoteBaseUnits
    trusted: bool


@dataclass(frozen=True, slots=True)
class CoreBehaviorExitInput:
    """Trusted reserve-backed exit capacity for one position and slot."""

    as_of_slot: Slot
    position_base_units: TokenBaseUnits
    executable_exit_capacity_base_units: TokenBaseUnits
    trusted: bool


@dataclass(frozen=True, slots=True)
class CoreBehaviorFeatureInputs:
    """Complete point-in-time inputs for the core behavior reducer."""

    as_of_slot: Slot
    market: CoreBehaviorMarketInput
    flow: CoreBehaviorFlowInput
    exit: CoreBehaviorExitInput


@dataclass(frozen=True, slots=True)
class CoreBehaviorFeatureSnapshot:
    """Immutable core behavior features at one slot boundary.

    ``buy_sell_imbalance_ppm`` is signed: positive values mean buy-dominant
    flow, negative values mean sell-dominant flow. External volume remains a
    separate context feature and never contributes to exit capacity.
    """

    as_of_slot: Slot
    elapsed_slots: int
    curve_progress_ppm: int
    buy_sell_imbalance_ppm: int
    independent_buyer_count: int
    operator_inventory_share_ppm: int
    linked_wallet_sell_pressure_ppm: int
    absorbable_external_volume_quote_base_units: QuoteBaseUnits
    executable_exit_capacity_base_units: TokenBaseUnits


CoreBehaviorFeatureResult = CoreBehaviorFeatureSnapshot | AbstainResult


def reduce_core_behavior_features(
    *, inputs: CoreBehaviorFeatureInputs
) -> CoreBehaviorFeatureResult:
    """Reduce explicit point-in-time inputs into core behavior features.

    The reducer is pure and fail-closed. All source components must be trusted,
    integer-valued, and aligned to the top-level ``as_of_slot``. No value is
    inferred when a required denominator or source component is unavailable.
    """

    if not isinstance(inputs, CoreBehaviorFeatureInputs):
        return _unsupported("core behavior inputs are malformed", Slot(-1))
    if not _non_negative_int(inputs.as_of_slot):
        return _unsupported("as_of_slot must be non-negative", inputs.as_of_slot)

    validation_error = _validate_inputs(inputs)
    if validation_error is not None:
        return validation_error

    market = inputs.market
    flow = inputs.flow
    exit_input = inputs.exit
    buy_volume = int(flow.buy_volume_quote_base_units)
    sell_volume = int(flow.sell_volume_quote_base_units)
    external_supply = int(flow.external_circulating_supply_base_units)
    operator_inventory = int(flow.operator_inventory_base_units)
    linked_sell_volume = int(flow.linked_wallet_sell_volume_quote_base_units)

    return CoreBehaviorFeatureSnapshot(
        as_of_slot=inputs.as_of_slot,
        elapsed_slots=int(inputs.as_of_slot) - int(market.launch_slot),
        curve_progress_ppm=market.curve_progress_ppm,
        buy_sell_imbalance_ppm=_signed_ratio_ppm(
            buy_volume - sell_volume,
            buy_volume + sell_volume,
        ),
        independent_buyer_count=flow.independent_buyer_count,
        operator_inventory_share_ppm=(
            operator_inventory * PROBABILITY_PPM_DENOMINATOR // external_supply
        ),
        linked_wallet_sell_pressure_ppm=(
            _ratio_ppm(linked_sell_volume, sell_volume) if sell_volume > 0 else 0
        ),
        absorbable_external_volume_quote_base_units=(
            flow.absorbable_external_volume_quote_base_units
        ),
        executable_exit_capacity_base_units=(
            exit_input.executable_exit_capacity_base_units
        ),
    )


def _validate_inputs(
    inputs: CoreBehaviorFeatureInputs,
) -> AbstainResult | None:
    slot = inputs.as_of_slot
    if not _non_negative_int(slot):
        return _unsupported("as_of_slot must be non-negative", slot)

    component_error = _validate_components(inputs, slot)
    if component_error is not None:
        return component_error

    market_error = _validate_market(inputs.market, slot)
    if market_error is not None:
        return market_error
    flow_error = _validate_flow(inputs.flow, slot)
    if flow_error is not None:
        return flow_error
    return _validate_exit(inputs.exit, slot)


def _validate_components(
    inputs: CoreBehaviorFeatureInputs,
    as_of_slot: Slot,
) -> AbstainResult | None:
    for component, expected_type, name in (
        (inputs.market, CoreBehaviorMarketInput, "market"),
        (inputs.flow, CoreBehaviorFlowInput, "flow"),
        (inputs.exit, CoreBehaviorExitInput, "exit"),
    ):
        if component is None:
            return _missing(f"{name} input is required", as_of_slot)
        if not isinstance(component, expected_type):
            return _unsupported(f"{name} input is malformed", as_of_slot)
        component_error = _validate_component_slot_and_trust(
            component=component,
            name=name,
            as_of_slot=as_of_slot,
        )
        if component_error is not None:
            return component_error
    return None


def _validate_component_slot_and_trust(
    *,
    component: object,
    name: str,
    as_of_slot: Slot,
) -> AbstainResult | None:
    if component is None:
        return _missing(f"{name} input is required", as_of_slot)
    component_slot = getattr(component, "as_of_slot", None)
    if not _non_negative_int(component_slot):
        return _unsupported(f"{name} input as_of_slot is invalid", as_of_slot)
    if component_slot != as_of_slot:
        return _stale(f"{name} input uses a different as_of_slot", as_of_slot)
    trusted = getattr(component, "trusted", None)
    if type(trusted) is not bool:
        return _unknown(f"{name} input trust state is unknown", as_of_slot)
    if not trusted:
        return _unknown(f"{name} input is not trusted", as_of_slot)
    return None


def _validate_market(
    market: CoreBehaviorMarketInput,
    as_of_slot: Slot,
) -> AbstainResult | None:
    if not _non_negative_int(market.launch_slot):
        return _unsupported("launch_slot must be non-negative", as_of_slot)
    if market.launch_slot > as_of_slot:
        return _stale("launch_slot is newer than as_of_slot", as_of_slot)
    if not _valid_probability_ppm(market.curve_progress_ppm):
        return _unsupported("curve_progress_ppm is invalid", as_of_slot)
    return None


def _validate_flow(
    flow: CoreBehaviorFlowInput,
    as_of_slot: Slot,
) -> AbstainResult | None:
    amounts_error = _validate_flow_amounts(flow, as_of_slot)
    if amounts_error is not None:
        return amounts_error
    return _validate_flow_relations(flow, as_of_slot)


def _validate_flow_amounts(
    flow: CoreBehaviorFlowInput,
    as_of_slot: Slot,
) -> AbstainResult | None:
    non_negative_fields = (
        ("buy_volume_quote_base_units", flow.buy_volume_quote_base_units),
        ("sell_volume_quote_base_units", flow.sell_volume_quote_base_units),
        ("independent_buyer_count", flow.independent_buyer_count),
        ("operator_inventory_base_units", flow.operator_inventory_base_units),
        (
            "external_circulating_supply_base_units",
            flow.external_circulating_supply_base_units,
        ),
        (
            "linked_wallet_sell_volume_quote_base_units",
            flow.linked_wallet_sell_volume_quote_base_units,
        ),
        (
            "absorbable_external_volume_quote_base_units",
            flow.absorbable_external_volume_quote_base_units,
        ),
    )
    for field_name, value in non_negative_fields:
        if value is None:
            return _missing(f"{field_name} is required", as_of_slot)
        if not _non_negative_int(value):
            return _unsupported(
                f"{field_name} must be a non-negative integer", as_of_slot
            )
    return None


def _validate_flow_relations(
    flow: CoreBehaviorFlowInput,
    as_of_slot: Slot,
) -> AbstainResult | None:
    buy_volume = int(flow.buy_volume_quote_base_units)
    sell_volume = int(flow.sell_volume_quote_base_units)
    linked_sell_volume = int(flow.linked_wallet_sell_volume_quote_base_units)
    if buy_volume + sell_volume == 0:
        return _missing("buy and sell volume are required for imbalance", as_of_slot)
    if linked_sell_volume > sell_volume:
        return _unsupported(
            "linked wallet sell volume exceeds total sell volume", as_of_slot
        )

    external_supply = int(flow.external_circulating_supply_base_units)
    operator_inventory = int(flow.operator_inventory_base_units)
    if external_supply == 0:
        return _missing("external circulating supply is required", as_of_slot)
    if operator_inventory > external_supply:
        return _unsupported(
            "operator inventory exceeds external circulating supply", as_of_slot
        )
    return None


def _validate_exit(
    exit_input: CoreBehaviorExitInput,
    as_of_slot: Slot,
) -> AbstainResult | None:
    for field_name, value in (
        ("position_base_units", exit_input.position_base_units),
        (
            "executable_exit_capacity_base_units",
            exit_input.executable_exit_capacity_base_units,
        ),
    ):
        if value is None:
            return _missing(f"{field_name} is required", as_of_slot)
        if not _non_negative_int(value):
            return _unsupported(
                f"{field_name} must be a non-negative integer", as_of_slot
            )
    if int(exit_input.position_base_units) == 0:
        return _unsupported("position_base_units must be positive", as_of_slot)
    return None


def _ratio_ppm(numerator: int, denominator: int) -> int:
    return numerator * PROBABILITY_PPM_DENOMINATOR // denominator


def _signed_ratio_ppm(numerator: int, denominator: int) -> int:
    if numerator >= 0:
        return _ratio_ppm(numerator, denominator)
    return -_ratio_ppm(-numerator, denominator)


def _valid_probability_ppm(value: object) -> bool:
    return _non_negative_int(value) and value <= PROBABILITY_PPM_DENOMINATOR


def _non_negative_int(value: object) -> bool:
    return type(value) is int and value >= 0


def _missing(message: str, as_of_slot: Slot) -> AbstainResult:
    return AbstainResult(
        reason=AbstainReason.MISSING_FEATURE,
        message=message,
        as_of_slot=_abstain_slot(as_of_slot),
    )


def _stale(message: str, as_of_slot: Slot) -> AbstainResult:
    return AbstainResult(
        reason=AbstainReason.STALE_STATE,
        message=message,
        as_of_slot=_abstain_slot(as_of_slot),
    )


def _unknown(message: str, as_of_slot: Slot) -> AbstainResult:
    return AbstainResult(
        reason=AbstainReason.UNKNOWN_PROTOCOL_STATE,
        message=message,
        as_of_slot=_abstain_slot(as_of_slot),
    )


def _unsupported(message: str, as_of_slot: Slot) -> AbstainResult:
    return AbstainResult(
        reason=AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
        message=message,
        as_of_slot=_abstain_slot(as_of_slot),
    )


def _abstain_slot(as_of_slot: object) -> int:
    if type(as_of_slot) is int:
        return as_of_slot
    return -1
