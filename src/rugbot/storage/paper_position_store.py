"""Strict (de)serialization for durable paper position state."""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

from rugbot.decision.playbook_rules import ExitRuleState
from rugbot.domain.amounts import Slot, TokenBaseUnits
from rugbot.execution.position_runtime import PaperPositionState

if TYPE_CHECKING:
    from collections.abc import Mapping

_POSITION_FIELDS = frozenset(
    {
        "as_of_slot",
        "market_id",
        "target_id",
        "execution_mode",
        "original_position_base_units",
        "current_position_base_units",
        "entry_quote_lamports",
        "entry_cost_lamports",
        "take_profit_pnl_ppm",
        "stop_loss_pnl_ppm",
        "max_slippage_bps",
        "peak_pnl_ppm",
        "exit_rule_state",
        "emitted_sell_intent_count",
    }
)
_EXIT_RULE_FIELDS = frozenset(
    {
        "filled_take_profit_level_indices",
        "filled_stop_loss_level_indices",
        "filled_big_buy_level_indices",
        "exited_fraction_ppm",
    }
)
_PPM_DENOMINATOR = 1_000_000
_MAX_SLIPPAGE_BPS = 10_000


class PaperPositionStoreError(ValueError):
    """Raised when durable paper position state is invalid or inaccessible."""

    @classmethod
    def malformed_state(cls) -> PaperPositionStoreError:
        """Build an error for malformed durable state."""

        return cls("paper position state is malformed")

    @classmethod
    def invalid_field(cls, field_name: str) -> PaperPositionStoreError:
        """Build an error for an invalid position field."""

        return cls(f"invalid paper position field: {field_name}")

    @classmethod
    def duplicate_market(cls) -> PaperPositionStoreError:
        """Build an error for duplicate canonical market identity."""

        return cls("duplicate paper position market_id")

    @classmethod
    def read_failed(cls) -> PaperPositionStoreError:
        """Build an error for unreadable durable state."""

        return cls("paper position state could not be read")

    @classmethod
    def write_failed(cls) -> PaperPositionStoreError:
        """Build an error for a failed atomic rewrite."""

        return cls("paper position state could not be written")

    @classmethod
    def duplicate_key(cls) -> PaperPositionStoreError:
        """Build an error for a duplicate JSON key."""

        return cls("duplicate paper position JSON key")


def _position_from_json(payload: object) -> PaperPositionState:
    data = _exact_mapping(payload, _POSITION_FIELDS, "record")
    return _validate_state(
        PaperPositionState(
            as_of_slot=Slot(_nonnegative_int(data, "as_of_slot")),
            market_id=_market_id(data),
            target_id=_nonempty_text(data, "target_id"),
            execution_mode=_execution_mode(data),
            original_position_base_units=TokenBaseUnits(
                _positive_int(data, "original_position_base_units")
            ),
            current_position_base_units=TokenBaseUnits(
                _nonnegative_int(data, "current_position_base_units")
            ),
            entry_quote_lamports=_positive_int(data, "entry_quote_lamports"),
            entry_cost_lamports=_nonnegative_int(data, "entry_cost_lamports"),
            take_profit_pnl_ppm=_optional_integer(data, "take_profit_pnl_ppm"),
            stop_loss_pnl_ppm=_optional_integer(data, "stop_loss_pnl_ppm"),
            max_slippage_bps=_bounded_slippage(data, "max_slippage_bps"),
            peak_pnl_ppm=_integer(data, "peak_pnl_ppm"),
            exit_rule_state=_exit_rule_state_from_json(data["exit_rule_state"]),
            emitted_sell_intent_count=_nonnegative_int(
                data, "emitted_sell_intent_count"
            ),
        )
    )


def _exit_rule_state_from_json(payload: object) -> ExitRuleState:
    data = _exact_mapping(payload, _EXIT_RULE_FIELDS, "exit_rule_state")
    return ExitRuleState(
        filled_take_profit_level_indices=_index_tuple(
            data, "filled_take_profit_level_indices"
        ),
        filled_stop_loss_level_indices=_index_tuple(
            data, "filled_stop_loss_level_indices"
        ),
        filled_big_buy_level_indices=_index_tuple(data, "filled_big_buy_level_indices"),
        exited_fraction_ppm=_bounded_ppm(data, "exited_fraction_ppm"),
    )


def _position_to_json(state: PaperPositionState) -> dict[str, object]:
    exit_state = state.exit_rule_state
    return {
        "as_of_slot": state.as_of_slot,
        "market_id": state.market_id,
        "target_id": state.target_id,
        "execution_mode": state.execution_mode,
        "original_position_base_units": state.original_position_base_units,
        "current_position_base_units": state.current_position_base_units,
        "entry_quote_lamports": state.entry_quote_lamports,
        "entry_cost_lamports": state.entry_cost_lamports,
        "take_profit_pnl_ppm": state.take_profit_pnl_ppm,
        "stop_loss_pnl_ppm": state.stop_loss_pnl_ppm,
        "max_slippage_bps": state.max_slippage_bps,
        "peak_pnl_ppm": state.peak_pnl_ppm,
        "exit_rule_state": {
            "filled_take_profit_level_indices": (
                exit_state.filled_take_profit_level_indices
            ),
            "filled_stop_loss_level_indices": (
                exit_state.filled_stop_loss_level_indices
            ),
            "filled_big_buy_level_indices": exit_state.filled_big_buy_level_indices,
            "exited_fraction_ppm": exit_state.exited_fraction_ppm,
        },
        "emitted_sell_intent_count": state.emitted_sell_intent_count,
    }


def _validate_state(state: object) -> PaperPositionState:  # noqa: C901
    if type(state) is not PaperPositionState:
        raise PaperPositionStoreError.invalid_field("record")
    _validate_nonnegative_integer(state.as_of_slot, "as_of_slot")
    _validate_market_id(state.market_id)
    if type(state.target_id) is not str or not state.target_id:
        raise PaperPositionStoreError.invalid_field("target_id")
    if state.execution_mode not in {"paper", "simulation", "simulated", "live"}:
        raise PaperPositionStoreError.invalid_field("execution_mode")
    _validate_positive_integer(
        state.original_position_base_units, "original_position_base_units"
    )
    _validate_nonnegative_integer(
        state.current_position_base_units, "current_position_base_units"
    )
    if state.current_position_base_units > state.original_position_base_units:
        raise PaperPositionStoreError.invalid_field("current_position_base_units")
    _validate_positive_integer(state.entry_quote_lamports, "entry_quote_lamports")
    _validate_nonnegative_integer(state.entry_cost_lamports, "entry_cost_lamports")
    _validate_integer(state.peak_pnl_ppm, "peak_pnl_ppm")
    for field_name, threshold in (
        ("take_profit_pnl_ppm", state.take_profit_pnl_ppm),
        ("stop_loss_pnl_ppm", state.stop_loss_pnl_ppm),
    ):
        if threshold is not None:
            _validate_integer(threshold, field_name)
    if type(state.max_slippage_bps) is not int or not (
        0 <= state.max_slippage_bps <= _MAX_SLIPPAGE_BPS
    ):
        raise PaperPositionStoreError.invalid_field("max_slippage_bps")
    if type(state.exit_rule_state) is not ExitRuleState:
        raise PaperPositionStoreError.invalid_field("exit_rule_state")
    for field_name, indices in (
        (
            "filled_take_profit_level_indices",
            state.exit_rule_state.filled_take_profit_level_indices,
        ),
        (
            "filled_stop_loss_level_indices",
            state.exit_rule_state.filled_stop_loss_level_indices,
        ),
        (
            "filled_big_buy_level_indices",
            state.exit_rule_state.filled_big_buy_level_indices,
        ),
    ):
        _validate_index_tuple(indices, field_name)
    exited_fraction = state.exit_rule_state.exited_fraction_ppm
    _validate_nonnegative_integer(exited_fraction, "exited_fraction_ppm")
    if exited_fraction > _PPM_DENOMINATOR:
        raise PaperPositionStoreError.invalid_field("exited_fraction_ppm")
    _validate_nonnegative_integer(
        state.emitted_sell_intent_count, "emitted_sell_intent_count"
    )
    return state


def _exact_mapping(
    payload: object,
    fields: frozenset[str],
    field_name: str,
) -> Mapping[str, object]:
    if type(payload) is not dict:
        raise PaperPositionStoreError.invalid_field(field_name)
    data = cast("Mapping[str, object]", payload)
    if frozenset(data) != fields:
        raise PaperPositionStoreError.invalid_field(field_name)
    return data


def _market_id(data: Mapping[str, object]) -> str:
    value = data["market_id"]
    _validate_market_id(value)
    return cast("str", value)


def _execution_mode(data: Mapping[str, object]) -> str:
    value = data["execution_mode"]
    if value not in {"paper", "simulation", "simulated", "live"}:
        raise PaperPositionStoreError.invalid_field("execution_mode")
    return cast("str", value)


def _integer(data: Mapping[str, object], field_name: str) -> int:
    value = data[field_name]
    _validate_integer(value, field_name)
    return cast("int", value)


def _nonnegative_int(data: Mapping[str, object], field_name: str) -> int:
    value = data[field_name]
    _validate_nonnegative_integer(value, field_name)
    return cast("int", value)


def _positive_int(data: Mapping[str, object], field_name: str) -> int:
    value = data[field_name]
    _validate_positive_integer(value, field_name)
    return cast("int", value)


def _nonempty_text(data: Mapping[str, object], field_name: str) -> str:
    value = data[field_name]
    if type(value) is not str or not value:
        raise PaperPositionStoreError.invalid_field(field_name)
    return value


def _optional_integer(
    data: Mapping[str, object],
    field_name: str,
) -> int | None:
    value = data[field_name]
    if value is None:
        return None
    _validate_integer(value, field_name)
    return cast("int", value)


def _bounded_slippage(data: Mapping[str, object], field_name: str) -> int:
    value = _nonnegative_int(data, field_name)
    if value > _MAX_SLIPPAGE_BPS:
        raise PaperPositionStoreError.invalid_field(field_name)
    return value


def _bounded_ppm(data: Mapping[str, object], field_name: str) -> int:
    value = _nonnegative_int(data, field_name)
    if value > _PPM_DENOMINATOR:
        raise PaperPositionStoreError.invalid_field(field_name)
    return value


def _index_tuple(data: Mapping[str, object], field_name: str) -> tuple[int, ...]:
    value = data[field_name]
    if type(value) is not list:
        raise PaperPositionStoreError.invalid_field(field_name)
    indices = tuple(value)
    _validate_index_tuple(indices, field_name)
    return cast("tuple[int, ...]", indices)


def _validate_market_id(value: object) -> None:
    if type(value) is not str or not value:
        raise PaperPositionStoreError.invalid_field("market_id")


def _validate_integer(value: object, field_name: str) -> None:
    if type(value) is not int:
        raise PaperPositionStoreError.invalid_field(field_name)


def _validate_nonnegative_integer(value: object, field_name: str) -> None:
    _validate_integer(value, field_name)
    if cast("int", value) < 0:
        raise PaperPositionStoreError.invalid_field(field_name)


def _validate_positive_integer(value: object, field_name: str) -> None:
    _validate_integer(value, field_name)
    if cast("int", value) <= 0:
        raise PaperPositionStoreError.invalid_field(field_name)


def _validate_index_tuple(value: object, field_name: str) -> None:
    if type(value) is not tuple:
        raise PaperPositionStoreError.invalid_field(field_name)
    indices = cast("tuple[object, ...]", value)
    if any(type(index) is not int or index < 0 for index in indices):
        raise PaperPositionStoreError.invalid_field(field_name)
    if len(set(indices)) != len(indices):
        raise PaperPositionStoreError.invalid_field(field_name)


def paper_position_state_from_json(payload: object) -> PaperPositionState:
    """Decode and strictly validate one persisted paper position snapshot."""

    return _position_from_json(payload)


def paper_position_state_to_json(state: PaperPositionState) -> dict[str, object]:
    """Encode one strictly validated paper position snapshot."""

    return _position_to_json(_validate_state(state))


def validate_paper_position_state(state: object) -> PaperPositionState:
    """Validate one paper position snapshot."""

    return _validate_state(state)


__all__ = [
    "PaperPositionStoreError",
    "paper_position_state_from_json",
    "paper_position_state_to_json",
    "validate_paper_position_state",
]
