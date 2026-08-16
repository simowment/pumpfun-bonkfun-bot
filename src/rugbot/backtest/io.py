"""Strict JSON input loading for the leakage-safe backtest runner."""

# Dynamic field diagnostics are intentional for this fixed-shape input boundary.
# The loader still rejects the document closed-world and fails closed.
# ruff: noqa: TRY003

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, NoReturn

from rugbot.backtest.evaluation import (
    BacktestAction,
    BacktestConfig,
    BacktestFillStatus,
    BacktestLaunchResult,
    FrozenModelManifest,
    OrderingScenario,
)
from rugbot.domain.amounts import Slot
from rugbot.domain.decisions import AbstainReason, AbstainResult
from rugbot.models.outcome_labels import (
    HorizonOutcomeLabel,
    LaunchOutcomeLabels,
)

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path


@dataclass(frozen=True, slots=True)
class BacktestDocument:
    """One fixed-shape backtest input document."""

    config: BacktestConfig
    launches: tuple[BacktestLaunchResult, ...]
    raw_observation_path: str


def load_backtest_document(path: Path) -> BacktestDocument | AbstainResult:
    """Load and strictly hydrate one backtest document from JSON."""

    try:
        payload = json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_json_constant,
        )
        root = _mapping(payload, "document")
        _exact_keys(
            root,
            {"config", "launches", "raw_observation_path"},
            "document",
        )
        config = _config(_required(root, "config", "document"))
        launches_payload = _list(root, "launches", "document")
        launches = tuple(_launch(item) for item in launches_payload)
        raw_observation_path = _string(
            root,
            "raw_observation_path",
            "document",
        )
        return BacktestDocument(
            config=config,
            launches=launches,
            raw_observation_path=raw_observation_path,
        )
    except (BacktestDocumentError, OSError, json.JSONDecodeError) as error:
        return AbstainResult(
            reason=AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
            message=f"backtest document abstained: {type(error).__name__}",
            as_of_slot=-1,
        )


class BacktestDocumentError(ValueError):
    """Raised when a backtest document violates its fixed input shape."""

    @classmethod
    def field(cls, message: str) -> BacktestDocumentError:
        """Build a deterministic document-field error."""

        return cls(message)


def _config(value: object) -> BacktestConfig:
    mapping = _mapping(value, "config")
    _exact_keys(
        mapping,
        {
            "as_of_slot",
            "evaluation_version",
            "manifest",
            "train_end_slot",
            "test_start_slot",
            "test_end_slot",
            "train_entity_ids",
            "stress_entity_ids",
            "expected_shortfall_tail_ppm",
        },
        "config",
    )
    return BacktestConfig(
        as_of_slot=Slot(_int(mapping, "as_of_slot", "config")),
        evaluation_version=_string(mapping, "evaluation_version", "config"),
        manifest=_manifest(_required(mapping, "manifest", "config")),
        train_end_slot=Slot(_int(mapping, "train_end_slot", "config")),
        test_start_slot=Slot(_int(mapping, "test_start_slot", "config")),
        test_end_slot=Slot(_int(mapping, "test_end_slot", "config")),
        train_entity_ids=_strings(mapping, "train_entity_ids", "config"),
        stress_entity_ids=_strings(mapping, "stress_entity_ids", "config"),
        expected_shortfall_tail_ppm=_int(
            mapping,
            "expected_shortfall_tail_ppm",
            "config",
        ),
    )


def _launch(value: object) -> BacktestLaunchResult:
    mapping = _mapping(value, "launch")
    _exact_keys(
        mapping,
        {
            "as_of_slot",
            "launch_id",
            "decision_id",
            "token_mint",
            "entity_id",
            "regime_id",
            "decision_slot",
            "decision_index",
            "action",
            "fill_status",
            "ordering_scenario",
            "net_pnl_quote_base_units",
            "gross_profit_quote_base_units",
            "execution_cost_quote_base_units",
            "selected_size_quote_base_units",
            "outcome",
            "manifest",
            "reason_codes",
            "evidence_ids",
        },
        "launch",
    )
    return BacktestLaunchResult(
        as_of_slot=Slot(_int(mapping, "as_of_slot", "launch")),
        launch_id=_string(mapping, "launch_id", "launch"),
        decision_id=_string(mapping, "decision_id", "launch"),
        token_mint=_string(mapping, "token_mint", "launch"),
        entity_id=_string(mapping, "entity_id", "launch"),
        regime_id=_string(mapping, "regime_id", "launch"),
        decision_slot=Slot(_int(mapping, "decision_slot", "launch")),
        decision_index=_int(mapping, "decision_index", "launch"),
        action=_enum(mapping, "action", "launch", BacktestAction),
        fill_status=_enum(mapping, "fill_status", "launch", BacktestFillStatus),
        ordering_scenario=_optional_enum(
            mapping,
            "ordering_scenario",
            "launch",
            OrderingScenario,
        ),
        net_pnl_quote_base_units=_optional_int(
            mapping,
            "net_pnl_quote_base_units",
            "launch",
        ),
        gross_profit_quote_base_units=_optional_int(
            mapping,
            "gross_profit_quote_base_units",
            "launch",
        ),
        execution_cost_quote_base_units=_optional_int(
            mapping,
            "execution_cost_quote_base_units",
            "launch",
        ),
        selected_size_quote_base_units=_optional_int(
            mapping,
            "selected_size_quote_base_units",
            "launch",
        ),
        outcome=_outcome(_required(mapping, "outcome", "launch")),
        manifest=_manifest(_required(mapping, "manifest", "launch")),
        reason_codes=_strings(mapping, "reason_codes", "launch"),
        evidence_ids=_strings(mapping, "evidence_ids", "launch"),
    )


def _manifest(value: object) -> FrozenModelManifest:
    mapping = _mapping(value, "manifest")
    fields = {
        "as_of_slot",
        "model_freeze_slot",
        "decision_version",
        "model_version",
        "outcome_labeler_version",
        "profile_snapshot_version",
        "graph_snapshot_version",
        "feature_snapshot_version",
        "market_snapshot_version",
        "latency_model_version",
        "fee_config_version",
    }
    _exact_keys(mapping, fields, "manifest")
    return FrozenModelManifest(
        as_of_slot=Slot(_int(mapping, "as_of_slot", "manifest")),
        model_freeze_slot=Slot(_int(mapping, "model_freeze_slot", "manifest")),
        decision_version=_string(mapping, "decision_version", "manifest"),
        model_version=_string(mapping, "model_version", "manifest"),
        outcome_labeler_version=_string(
            mapping,
            "outcome_labeler_version",
            "manifest",
        ),
        profile_snapshot_version=_string(
            mapping,
            "profile_snapshot_version",
            "manifest",
        ),
        graph_snapshot_version=_string(
            mapping,
            "graph_snapshot_version",
            "manifest",
        ),
        feature_snapshot_version=_string(
            mapping,
            "feature_snapshot_version",
            "manifest",
        ),
        market_snapshot_version=_string(
            mapping,
            "market_snapshot_version",
            "manifest",
        ),
        latency_model_version=_string(
            mapping,
            "latency_model_version",
            "manifest",
        ),
        fee_config_version=_string(mapping, "fee_config_version", "manifest"),
    )


def _outcome(value: object) -> LaunchOutcomeLabels:
    mapping = _mapping(value, "outcome")
    _exact_keys(
        mapping,
        {
            "as_of_slot",
            "launch_id",
            "token_mint",
            "labeler_version",
            "first_material_adverse_event_slot",
            "first_material_adverse_event_elapsed_ms",
            "max_executable_full_position_net_profit_before_adverse_event",
            "horizon_labels",
            "source_point_count",
            "evidence_ids",
            "reason_codes",
        },
        "outcome",
    )
    return LaunchOutcomeLabels(
        as_of_slot=Slot(_int(mapping, "as_of_slot", "outcome")),
        launch_id=_string(mapping, "launch_id", "outcome"),
        token_mint=_string(mapping, "token_mint", "outcome"),
        labeler_version=_string(mapping, "labeler_version", "outcome"),
        first_material_adverse_event_slot=_optional_int(
            mapping,
            "first_material_adverse_event_slot",
            "outcome",
        ),
        first_material_adverse_event_elapsed_ms=_optional_int(
            mapping,
            "first_material_adverse_event_elapsed_ms",
            "outcome",
        ),
        max_executable_full_position_net_profit_before_adverse_event=_optional_int(
            mapping,
            "max_executable_full_position_net_profit_before_adverse_event",
            "outcome",
        ),
        horizon_labels=tuple(
            _horizon(item) for item in _list(mapping, "horizon_labels", "outcome")
        ),
        source_point_count=_int(mapping, "source_point_count", "outcome"),
        evidence_ids=_strings(mapping, "evidence_ids", "outcome"),
        reason_codes=_strings(mapping, "reason_codes", "outcome"),
    )


def _horizon(value: object) -> HorizonOutcomeLabel:
    mapping = _mapping(value, "horizon")
    _exact_keys(
        mapping,
        {
            "as_of_slot",
            "launch_id",
            "token_mint",
            "horizon_ms",
            "censored",
            "last_observed_slot",
            "last_observed_elapsed_ms",
            "adverse_event_observed",
            "curve_completed",
            "migration_observed",
            "drawdown_ppm",
            "recovery_ppm",
            "full_exit_net_pnl_quote_base_units",
            "labeler_version",
            "evidence_ids",
        },
        "horizon",
    )
    return HorizonOutcomeLabel(
        as_of_slot=Slot(_int(mapping, "as_of_slot", "horizon")),
        launch_id=_string(mapping, "launch_id", "horizon"),
        token_mint=_string(mapping, "token_mint", "horizon"),
        horizon_ms=_int(mapping, "horizon_ms", "horizon"),
        censored=_bool(mapping, "censored", "horizon"),
        last_observed_slot=_optional_int(
            mapping,
            "last_observed_slot",
            "horizon",
        ),
        last_observed_elapsed_ms=_optional_int(
            mapping,
            "last_observed_elapsed_ms",
            "horizon",
        ),
        adverse_event_observed=_bool(mapping, "adverse_event_observed", "horizon"),
        curve_completed=_bool(mapping, "curve_completed", "horizon"),
        migration_observed=_bool(mapping, "migration_observed", "horizon"),
        drawdown_ppm=_optional_int(mapping, "drawdown_ppm", "horizon"),
        recovery_ppm=_optional_int(mapping, "recovery_ppm", "horizon"),
        full_exit_net_pnl_quote_base_units=_optional_int(
            mapping,
            "full_exit_net_pnl_quote_base_units",
            "horizon",
        ),
        labeler_version=_string(mapping, "labeler_version", "horizon"),
        evidence_ids=_strings(mapping, "evidence_ids", "horizon"),
    )


def _mapping(value: object, field_name: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise BacktestDocumentError.field(f"{field_name} must be an object")
    if any(not isinstance(key, str) for key in value):
        raise BacktestDocumentError.field(f"{field_name} keys must be strings")
    return value


def _list(mapping: Mapping[str, object], field_name: str, context: str) -> list[object]:
    value = _required(mapping, field_name, context)
    if not isinstance(value, list):
        raise BacktestDocumentError.field(f"{context}.{field_name} must be an array")
    return value


def _required(mapping: Mapping[str, object], field_name: str, context: str) -> object:
    if field_name not in mapping:
        raise BacktestDocumentError.field(f"{context}.{field_name} is required")
    return mapping[field_name]


def _exact_keys(
    mapping: Mapping[str, object],
    expected: set[str],
    context: str,
) -> None:
    if set(mapping) != expected:
        raise BacktestDocumentError.field(f"{context} has an unexpected field set")


def _string(mapping: Mapping[str, object], field_name: str, context: str) -> str:
    value = _required(mapping, field_name, context)
    if not isinstance(value, str) or not value:
        raise BacktestDocumentError.field(
            f"{context}.{field_name} must be a non-empty string"
        )
    return value


def _strings(
    mapping: Mapping[str, object], field_name: str, context: str
) -> tuple[str, ...]:
    values = _list(mapping, field_name, context)
    if any(not isinstance(value, str) or not value for value in values):
        raise BacktestDocumentError.field(
            f"{context}.{field_name} must contain strings"
        )
    return tuple(values)


def _int(mapping: Mapping[str, object], field_name: str, context: str) -> int:
    value = _required(mapping, field_name, context)
    if type(value) is not int:
        raise BacktestDocumentError.field(f"{context}.{field_name} must be an integer")
    return value


def _optional_int(
    mapping: Mapping[str, object],
    field_name: str,
    context: str,
) -> int | None:
    value = _required(mapping, field_name, context)
    if value is None:
        return None
    if type(value) is not int:
        raise BacktestDocumentError.field(
            f"{context}.{field_name} must be an integer or null"
        )
    return value


def _bool(mapping: Mapping[str, object], field_name: str, context: str) -> bool:
    value = _required(mapping, field_name, context)
    if type(value) is not bool:
        raise BacktestDocumentError.field(f"{context}.{field_name} must be a boolean")
    return value


def _enum(
    mapping: Mapping[str, object],
    field_name: str,
    context: str,
    enum_type: type[BacktestAction] | type[BacktestFillStatus],
) -> BacktestAction | BacktestFillStatus:
    value = _string(mapping, field_name, context)
    try:
        return enum_type(value)
    except ValueError as error:
        raise BacktestDocumentError.field(
            f"{context}.{field_name} is unsupported"
        ) from error


def _optional_enum(
    mapping: Mapping[str, object],
    field_name: str,
    context: str,
    enum_type: type[OrderingScenario],
) -> OrderingScenario | None:
    value = _required(mapping, field_name, context)
    if value is None:
        return None
    if not isinstance(value, str):
        raise BacktestDocumentError.field(
            f"{context}.{field_name} must be a string or null"
        )
    try:
        return enum_type(value)
    except ValueError as error:
        raise BacktestDocumentError.field(
            f"{context}.{field_name} is unsupported"
        ) from error


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise BacktestDocumentError.field("duplicate JSON field")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> NoReturn:
    raise BacktestDocumentError.field(f"unsupported JSON constant: {value}")


__all__ = ["BacktestDocument", "load_backtest_document"]
