"""Pure mapping between TUI settings widgets and the strict watcher YAML schema.

No Textual imports: every function operates on plain widget-id keyed value
mappings so the settings round-trip is unit-testable without a running app.
"""

# ruff: noqa: TRY003

from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING, Any

from rugbot.runtime.config import SniperConfigError
from rugbot.tracker.models import LAMPORTS_PER_SOL

if TYPE_CHECKING:
    from rugbot.runtime.config import CoreSniperConfig

WidgetValues = Mapping[str, "str | bool"]

VALID_EXECUTION_MODES = frozenset({"observe", "paper", "simulation", "live"})

DIP_LEVEL_COUNT = 3
EXIT_LEVEL_COUNT = 5
TRAILING_LEVEL_COUNT = 5
BIG_BUY_LEVEL_COUNT = 3


def format_sol_from_lamports(lamports: int) -> str:
    """Format lamports as a whole-or-fractional SOL string (no float money)."""
    return _scaled_decimal_str(Decimal(lamports) / Decimal(LAMPORTS_PER_SOL))


def format_percent_from_ppm(ppm: int | None) -> str:
    """Format parts-per-million as a human percent string."""
    if ppm is None:
        return ""
    return _scaled_decimal_str(Decimal(ppm) / Decimal(10_000))


def format_seconds_from_ms(value_ms: int | None) -> str:
    return "0" if value_ms is None else str(value_ms // 1_000)


def format_minutes_from_ms(value_ms: int | None) -> str:
    return "0" if value_ms is None else str(value_ms // 60_000)


def config_widget_values(config: CoreSniperConfig) -> dict[str, str | bool]:
    """Flatten a validated config into widget-id keyed display values."""
    execution = config.execution
    strategy = config.strategy
    rules = config.rules
    sell = rules.sell
    return {
        "target-wallet": config.target.id,
        "target-kind": config.target.kind.value,
        "tracking-mode": config.tracking_mode.value,
        "execution-mode": execution.mode.value,
        "snipe-size-sol": format_sol_from_lamports(execution.quote_size_lamports),
        "max-slippage": execution.max_slippage_bps,
        "priority-fee": execution.priority_fee_microlamports,
        "jito-tip": format_sol_from_lamports(execution.jito_tip_lamports),
        "routing-policy": execution.routing_policy,
        "compute-unit-limit": execution.compute_unit_limit,
        "loaded-accounts-limit": execution.loaded_accounts_data_size_limit,
        "signer-pubkey": execution.signer_pubkey or "",
        "jito-url": execution.jito_block_engine_url or "",
        "volume-bankroll": config.volume_sizing.max_bankroll_fraction_ppm,
        "volume-independent": (
            config.volume_sizing.max_independent_volume_fraction_ppm
        ),
        "volume-impact": config.volume_sizing.max_price_impact_ppm,
        "strategy-min-volume": strategy.min_volume_usd_micro,
        "strategy-max-creator-pairs": strategy.max_creator_pairs,
        "strategy-history-samples": strategy.history_sample_count,
        "strategy-min-winrate": format_percent_from_ppm(strategy.min_win_rate_ppm),
        "strategy-max-buys-hour": strategy.max_buys_per_hour,
        "strategy-max-entry-index": strategy.max_entry_transaction_index,
        "max-entry-mc": strategy.max_entry_market_cap_quote_base_units,
        "strategy-max-deviation": strategy.max_entry_deviation_ppm,
        "rule-min-mc": rules.min_market_cap_quote_base_units,
        "rule-max-mc": rules.max_market_cap_quote_base_units,
        "rule-max-age": format_minutes_from_ms(rules.max_token_age_ms),
        "rule-cooldown": format_seconds_from_ms(rules.copytrade_cooldown_ms),
        "rule-max-losses": rules.max_consecutive_losses,
        "snipe-delay": rules.snipe_delay_ms // 1_000,
        "take-profit-pct": (
            format_percent_from_ppm(sell.take_profit_levels[0].trigger_pnl_ppm)
            if sell.take_profit_levels
            else "100"
        ),
        "stop-loss-pct": (
            format_percent_from_ppm(sell.stop_loss_levels[0].trigger_pnl_ppm)
            if sell.stop_loss_levels
            else "-30"
        ),
        "min-winrate-pct": format_percent_from_ppm(strategy.min_win_rate_ppm),
        "max-gas-cap": "0.0050",
        "rule-no-activity": format_seconds_from_ms(sell.no_activity_timeout_ms),
        "strategy-bundle": strategy.require_bundle_match,
        "strategy-double-signature": strategy.require_double_signature,
        "strategy-prior-zero": strategy.require_prior_zero_balance,
        "strategy-historical": strategy.require_historical_qualification,
        "require-block-zero": strategy.max_entry_transaction_index == 0,
        "require-funding-match": strategy.require_bundle_match,
        "execution-mode-live": execution.mode.value == "live",
    }


def level_widget_values(config: CoreSniperConfig) -> dict[str, int | None]:
    """Flatten repeated dip, exit, trailing, and big-buy controls."""
    values: dict[str, int | None] = {}
    sell = config.rules.sell
    for index, level in enumerate(config.rules.buy_the_dip_levels):
        values[f"dip-{index}-drawdown"] = level.drawdown_ppm
        values[f"dip-{index}-size"] = level.quote_size_lamports
    for index, level in enumerate(sell.take_profit_levels):
        values[f"tp-{index}-trigger"] = level.trigger_pnl_ppm
        values[f"tp-{index}-fraction"] = level.sell_fraction_ppm
    for index, level in enumerate(sell.stop_loss_levels):
        values[f"sl-{index}-trigger"] = level.trigger_pnl_ppm
        values[f"sl-{index}-fraction"] = level.sell_fraction_ppm
    for index, level in enumerate(sell.trailing_levels):
        values[f"trail-{index}-mc"] = level.min_market_cap_quote_base_units
        values[f"trail-{index}-drawdown"] = level.drawdown_ppm
    for index, level in enumerate(sell.auto_sell_big_buy_levels):
        values[f"big-{index}-min"] = level.min_quote_base_units
        values[f"big-{index}-max"] = level.max_quote_base_units
        values[f"big-{index}-fraction"] = level.sell_fraction_ppm
    return values


def build_settings_document(values: WidgetValues) -> dict[str, Any]:
    """Build the strict YAML mapping from widget values (raises SniperConfigError)."""
    mode = text(values, "execution-mode").lower()
    if boolean(values, "execution-mode-live"):
        mode = "live"
    if mode not in VALID_EXECUTION_MODES:
        raise SniperConfigError(
            "execution-mode must be observe, paper, simulation, or live"
        )
    tp_levels = optional_level_pairs(values, "tp", EXIT_LEVEL_COUNT)
    sl_levels = optional_level_pairs(values, "sl", EXIT_LEVEL_COUNT)
    take_profit_ppm = ppm_from_percent(values, "take-profit-pct")
    stop_loss_ppm = -abs(ppm_from_percent(values, "stop-loss-pct"))
    if tp_levels:
        tp_levels[0]["trigger_pnl_ppm"] = take_profit_ppm
    else:
        tp_levels = [
            {"trigger_pnl_ppm": take_profit_ppm, "sell_fraction_ppm": 1_000_000}
        ]
    if sl_levels:
        sl_levels[0]["trigger_pnl_ppm"] = stop_loss_ppm
    else:
        sl_levels = [{"trigger_pnl_ppm": stop_loss_ppm, "sell_fraction_ppm": 1_000_000}]
    jito_tip = (
        lamports_from_sol(values, "jito-tip")
        if decimal_value(values, "jito-tip") > 0
        else 0
    )
    return {
        "target": {
            "kind": text(values, "target-kind").lower(),
            "id": text(values, "target-wallet"),
        },
        "execution": {
            "mode": mode,
            "quote_size_lamports": lamports_from_sol(values, "snipe-size-sol"),
            "max_slippage_bps": integer(values, "max-slippage", minimum=0),
            "routing_policy": text(values, "routing-policy").lower(),
            "priority_fee_microlamports": integer(values, "priority-fee", minimum=0),
            "jito_tip_lamports": jito_tip,
            "compute_unit_limit": integer(values, "compute-unit-limit", minimum=1),
            "loaded_accounts_data_size_limit": integer(
                values, "loaded-accounts-limit", minimum=1
            ),
            "signer_pubkey": text(values, "signer-pubkey") or None,
            "jito_block_engine_url": text(values, "jito-url") or None,
        },
        "tracking_mode": text(values, "tracking-mode").lower(),
        "volume_sizing": {
            "max_bankroll_fraction_ppm": integer(values, "volume-bankroll", minimum=0),
            "max_independent_volume_fraction_ppm": integer(
                values, "volume-independent", minimum=0
            ),
            "max_price_impact_ppm": integer(values, "volume-impact", minimum=0),
        },
        "strategy": {
            "min_volume_usd_micro": optional_integer(values, "strategy-min-volume"),
            "max_creator_pairs": optional_integer(values, "strategy-max-creator-pairs"),
            "history_sample_count": integer(
                values, "strategy-history-samples", minimum=1
            ),
            "min_win_rate_ppm": ppm_from_percent(values, "min-winrate-pct"),
            "max_buys_per_hour": integer(values, "strategy-max-buys-hour", minimum=1),
            "max_entry_transaction_index": integer(
                values, "strategy-max-entry-index", minimum=0
            ),
            "max_entry_market_cap_quote_base_units": optional_integer(
                values, "max-entry-mc"
            ),
            "max_entry_deviation_ppm": integer(
                values, "strategy-max-deviation", minimum=0
            ),
            "require_bundle_match": boolean(values, "strategy-bundle"),
            "require_double_signature": boolean(values, "strategy-double-signature"),
            "require_prior_zero_balance": boolean(values, "strategy-prior-zero"),
            "require_historical_qualification": boolean(values, "strategy-historical"),
        },
        "rules": {
            "snipe_delay_seconds": integer(values, "snipe-delay", minimum=0),
            "min_market_cap_quote_base_units": optional_integer(values, "rule-min-mc"),
            "max_market_cap_quote_base_units": optional_integer(values, "rule-max-mc"),
            "max_token_age_minutes": integer(values, "rule-max-age", minimum=0),
            "follow_cooldown_seconds": integer(values, "rule-cooldown", minimum=0),
            "buy_only_once": boolean(values, "rule-buy-once"),
            "max_consecutive_losses": optional_integer(values, "rule-max-losses"),
            "buy_the_dip": {"levels": dip_levels(values)},
            "sell": {
                "take_profit_levels": tp_levels,
                "stop_loss_levels": sl_levels,
                "trailing_levels": trailing_levels(values),
                "no_activity_seconds": integer(values, "rule-no-activity", minimum=0),
                "auto_sell_big_buy": {"levels": big_buy_levels(values)},
            },
        },
    }


def text(values: WidgetValues, widget_id: str) -> str:
    raw = values.get(widget_id, "")
    return "" if isinstance(raw, bool) else str(raw).strip()


def integer(values: WidgetValues, widget_id: str, *, minimum: int | None = None) -> int:
    raw = text(values, widget_id)
    if not raw:
        return minimum if minimum is not None else 0
    try:
        value = int(raw)
    except ValueError as error:
        raise SniperConfigError(f"{widget_id} must be an integer") from error
    if minimum is not None and value < minimum:
        raise SniperConfigError(f"{widget_id} must be at least {minimum}")
    return value


def optional_integer(values: WidgetValues, widget_id: str) -> int | None:
    raw = text(values, widget_id)
    if not raw or raw.lower() in {"none", "null"}:
        return None
    return integer(values, widget_id)


def decimal_value(values: WidgetValues, widget_id: str) -> Decimal:
    raw = text(values, widget_id).replace("$", "").replace("%", "")
    if not raw:
        return Decimal(0)
    try:
        return Decimal(raw)
    except (InvalidOperation, ValueError) as error:
        raise SniperConfigError(f"{widget_id} must be numeric") from error


def boolean(values: WidgetValues, widget_id: str) -> bool:
    return values.get(widget_id) is True


def ppm_from_percent(values: WidgetValues, widget_id: str) -> int:
    scaled = decimal_value(values, widget_id) * Decimal(10_000)
    if scaled != scaled.to_integral_value():
        raise SniperConfigError(f"{widget_id} has too many decimal places")
    return int(scaled)


def lamports_from_sol(values: WidgetValues, widget_id: str) -> int:
    scaled = decimal_value(values, widget_id) * Decimal(LAMPORTS_PER_SOL)
    if scaled != scaled.to_integral_value() or scaled <= 0:
        raise SniperConfigError(f"{widget_id} must represent positive whole lamports")
    return int(scaled)


def optional_level_pairs(
    values: WidgetValues, prefix: str, count: int
) -> list[dict[str, int]]:
    levels: list[dict[str, int]] = []
    for index in range(count):
        trigger = text(values, f"{prefix}-{index}-trigger")
        fraction = text(values, f"{prefix}-{index}-fraction")
        if not trigger and not fraction:
            continue
        if not trigger or not fraction:
            raise SniperConfigError(f"{prefix} level {index + 1} requires both fields")
        levels.append(
            {"trigger_pnl_ppm": int(trigger), "sell_fraction_ppm": int(fraction)}
        )
    return levels


def dip_levels(values: WidgetValues) -> list[dict[str, int]]:
    levels: list[dict[str, int]] = []
    for index in range(DIP_LEVEL_COUNT):
        drawdown = text(values, f"dip-{index}-drawdown")
        size = text(values, f"dip-{index}-size")
        if not drawdown and not size:
            continue
        if not drawdown or not size:
            raise SniperConfigError(f"dip level {index + 1} requires both fields")
        levels.append({"drawdown_ppm": int(drawdown), "quote_size_lamports": int(size)})
    return levels


def trailing_levels(values: WidgetValues) -> list[dict[str, int | None]]:
    levels: list[dict[str, int | None]] = []
    for index in range(TRAILING_LEVEL_COUNT):
        minimum = text(values, f"trail-{index}-mc")
        drawdown = text(values, f"trail-{index}-drawdown")
        if not minimum and not drawdown:
            continue
        if not drawdown:
            raise SniperConfigError(f"trail level {index + 1} requires drawdown")
        levels.append(
            {
                "min_market_cap_quote_base_units": int(minimum) if minimum else None,
                "drawdown_ppm": int(drawdown),
            }
        )
    return levels


def big_buy_levels(values: WidgetValues) -> list[dict[str, int]]:
    levels: list[dict[str, int]] = []
    for index in range(BIG_BUY_LEVEL_COUNT):
        entries = [
            text(values, f"big-{index}-{suffix}")
            for suffix in ("min", "max", "fraction")
        ]
        if not any(entries):
            continue
        if not all(entries):
            raise SniperConfigError(f"big-buy level {index + 1} requires all fields")
        levels.append(
            {
                "min_quote_base_units": int(entries[0]),
                "max_quote_base_units": int(entries[1]),
                "sell_fraction_ppm": int(entries[2]),
            }
        )
    return levels


def _scaled_decimal_str(value: Decimal) -> str:
    return format(value, "f").rstrip("0").rstrip(".") or "0"
