"""Durable storage for immutable paper position state."""

from __future__ import annotations

import json
import os
from typing import TYPE_CHECKING, cast

from rugbot.decision.playbook_rules import ExitRuleState
from rugbot.domain.amounts import Slot, TokenBaseUnits
from rugbot.execution.position_runtime import PaperPositionState

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
    from pathlib import Path

_POSITION_FIELDS = frozenset(
    {
        "as_of_slot",
        "market_id",
        "original_position_base_units",
        "current_position_base_units",
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


class PaperPositionStore:
    """Strict atomically rewritten store keyed by canonical ``market_id``."""

    def __init__(self, path: Path) -> None:
        """Initialize a store without loading signing or execution material."""

        self._path = path

    def read_all(self) -> tuple[PaperPositionState, ...]:
        """Read all immutable paper positions in canonical market order."""

        if not self._path.exists():
            return ()
        try:
            raw = self._path.read_bytes()
        except OSError as error:
            raise PaperPositionStoreError.read_failed() from error
        try:
            payload = json.loads(
                raw.decode("utf-8"),
                object_pairs_hook=_reject_duplicate_keys,
                parse_constant=_reject_json_constant,
            )
            positions = _positions_from_json(payload)
        except (PaperPositionStoreError, UnicodeError, ValueError) as error:
            raise PaperPositionStoreError.malformed_state() from error
        return tuple(sorted(positions, key=lambda state: state.market_id))

    def get(self, market_id: str) -> PaperPositionState | None:
        """Return the position for one canonical market identity, if present."""

        _validate_market_id(market_id)
        return next(
            (state for state in self.read_all() if state.market_id == market_id),
            None,
        )

    def save(self, state: PaperPositionState) -> None:
        """Durably insert or replace one immutable position snapshot."""

        validated = _validate_state(state)
        positions = {position.market_id: position for position in self.read_all()}
        positions[validated.market_id] = validated
        self._rewrite(tuple(positions.values()))

    def remove(self, market_id: str) -> bool:
        """Durably remove one position by canonical market identity."""

        _validate_market_id(market_id)
        positions = {position.market_id: position for position in self.read_all()}
        if positions.pop(market_id, None) is None:
            return False
        self._rewrite(tuple(positions.values()))
        return True

    def _rewrite(self, positions: Sequence[PaperPositionState]) -> None:
        validated = _validate_positions(positions)
        encoded = (
            json.dumps(
                [_position_to_json(state) for state in validated],
                separators=(",", ":"),
                sort_keys=True,
            )
            + "\n"
        ).encode("utf-8")
        self._path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path = self._path.with_name(f".{self._path.name}.tmp")
        descriptor: int | None = None
        try:
            descriptor = os.open(
                temporary_path,
                os.O_CREAT | os.O_TRUNC | os.O_WRONLY,
                0o600,
            )
            _write_all(descriptor, encoded)
            os.fsync(descriptor)
            os.close(descriptor)
            descriptor = None
            temporary_path.replace(self._path)
            _fsync_directory(self._path.parent)
        except OSError as error:
            raise PaperPositionStoreError.write_failed() from error
        finally:
            if descriptor is not None:
                os.close(descriptor)
            try:
                temporary_path.unlink(missing_ok=True)
            except OSError:
                pass


def _positions_from_json(payload: object) -> tuple[PaperPositionState, ...]:
    if type(payload) is not list:
        raise PaperPositionStoreError.malformed_state()
    positions = tuple(_position_from_json(item) for item in payload)
    return _validate_positions(positions)


def _position_from_json(payload: object) -> PaperPositionState:
    data = _exact_mapping(payload, _POSITION_FIELDS, "record")
    return _validate_state(
        PaperPositionState(
            as_of_slot=Slot(_nonnegative_int(data, "as_of_slot")),
            market_id=_market_id(data),
            original_position_base_units=TokenBaseUnits(
                _positive_int(data, "original_position_base_units")
            ),
            current_position_base_units=TokenBaseUnits(
                _nonnegative_int(data, "current_position_base_units")
            ),
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
        "original_position_base_units": state.original_position_base_units,
        "current_position_base_units": state.current_position_base_units,
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


def _validate_positions(
    positions: Sequence[PaperPositionState],
) -> tuple[PaperPositionState, ...]:
    validated = tuple(_validate_state(state) for state in positions)
    market_ids = [state.market_id for state in validated]
    if len(set(market_ids)) != len(market_ids):
        raise PaperPositionStoreError.duplicate_market()
    return tuple(sorted(validated, key=lambda state: state.market_id))


def _validate_state(state: object) -> PaperPositionState:
    if type(state) is not PaperPositionState:
        raise PaperPositionStoreError.invalid_field("record")
    _validate_nonnegative_integer(state.as_of_slot, "as_of_slot")
    _validate_market_id(state.market_id)
    _validate_positive_integer(
        state.original_position_base_units, "original_position_base_units"
    )
    _validate_nonnegative_integer(
        state.current_position_base_units, "current_position_base_units"
    )
    if state.current_position_base_units > state.original_position_base_units:
        raise PaperPositionStoreError.invalid_field("current_position_base_units")
    _validate_integer(state.peak_pnl_ppm, "peak_pnl_ppm")
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


def _write_all(descriptor: int, data: bytes) -> None:
    offset = 0
    while offset < len(data):
        written = os.write(descriptor, data[offset:])
        if written <= 0:
            raise PaperPositionStoreError.write_failed()
        offset += written


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _reject_duplicate_keys(
    pairs: list[tuple[str, object]],
) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise PaperPositionStoreError.duplicate_key()
        result[key] = value
    return result


def _reject_json_constant(value: str) -> None:
    raise PaperPositionStoreError.invalid_field(value)


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
    "PaperPositionStore",
    "PaperPositionStoreError",
    "paper_position_state_from_json",
    "paper_position_state_to_json",
    "validate_paper_position_state",
]
