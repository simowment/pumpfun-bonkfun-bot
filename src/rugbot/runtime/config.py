"""Strict configuration for the Pump.fun wallet watcher."""

# Error messages are the public boundary for this small strict parser. The
# loader derives from SafeLoader and adds only duplicate-key rejection.
# ruff: noqa: S105, S506, TRY003

import os
import tempfile
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

import base58
import yaml

from rugbot.decision.playbook_rules import (
    MAX_BIG_BUY_LEVELS,
    MAX_DIP_LEVELS,
    MAX_SELL_LEVELS,
    PROBABILITY_PPM_DENOMINATOR,
    BigBuySellLevel,
    BuyTheDipLevel,
    PlaybookRules,
    SellLevel,
    SellRules,
    TrailingStopLevel,
)

PUBKEY_LENGTH = 32
MAX_SLIPPAGE_BPS = 10_000
MAX_PORTFOLIO_WALLETS = 100
MAX_STRATEGY_HISTORY_SAMPLES = 100
MAX_STRATEGY_BUYS_PER_HOUR = 10_000
MAX_STRATEGY_ENTRY_INDEX = 20


class SniperConfigError(ValueError):
    """Raised when the watcher configuration is malformed."""


class TargetKind(StrEnum):
    """Supported launch target identities."""

    WALLET = "wallet"
    TOKEN = "token"


class ExecutionMode(StrEnum):
    """Execution modes, with live submission explicitly gated by the CLI."""

    OBSERVE = "observe"
    PAPER = "paper"
    LIVE = "live"


@dataclass(frozen=True, slots=True)
class SniperTarget:
    """One Solana wallet or token identity."""

    kind: TargetKind
    id: str


@dataclass(frozen=True, slots=True)
class SniperExecution:
    """Execution mode and requested quote amount."""

    mode: ExecutionMode
    quote_size_lamports: int
    max_slippage_bps: int = 500


@dataclass(frozen=True, slots=True)
class VolumeSizingPolicy:
    """Configured caps applied to point-in-time volume sizing evidence."""

    max_bankroll_fraction_ppm: int = 100_000
    max_independent_volume_fraction_ppm: int = 25_000
    max_price_impact_ppm: int = 100_000


@dataclass(frozen=True, slots=True)
class StrategyFilterSettings:
    """Configurable operator and entry evidence thresholds."""

    min_volume_usd_micro: int | None = 30_000_000_000
    max_creator_pairs: int | None = 10
    history_sample_count: int = 10
    min_win_rate_ppm: int = 500_000
    max_buys_per_hour: int = 1
    max_entry_transaction_index: int = 1
    max_entry_market_cap_quote_base_units: int | None = None
    max_entry_deviation_ppm: int = 250_000
    require_bundle_match: bool = False
    require_double_signature: bool = False
    require_prior_zero_balance: bool = False


@dataclass(frozen=True, slots=True)
class CoreSniperConfig:
    """Complete Pump.fun watcher configuration."""

    target: SniperTarget
    execution: SniperExecution
    rules: PlaybookRules = field(default_factory=PlaybookRules)
    volume_sizing: VolumeSizingPolicy = field(default_factory=VolumeSizingPolicy)
    strategy: StrategyFilterSettings = field(default_factory=StrategyFilterSettings)


@dataclass(frozen=True, slots=True)
class WalletPortfolio:
    """Strict, ordered set of creator wallets watched by the runtime."""

    wallets: tuple[str, ...]


class _StrictLoader(yaml.SafeLoader):
    """YAML loader that rejects duplicate mapping keys."""


def _construct_mapping(
    loader: _StrictLoader,
    node: yaml.nodes.MappingNode,
) -> dict[Any, Any]:
    values: dict[Any, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=True)
        if not isinstance(key, str):
            raise SniperConfigError("YAML mapping keys must be strings")
        if key in values:
            raise SniperConfigError(f"duplicate YAML key: {key!r}")
        values[key] = loader.construct_object(value_node, deep=True)
    return values


_StrictLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    _construct_mapping,
)


def load_sniper_config(path: Path) -> CoreSniperConfig:
    """Load one watcher YAML document."""

    try:
        text = path.read_text(encoding="utf-8")
    except OSError as error:
        raise SniperConfigError(f"cannot read watcher config: {path}") from error
    return parse_sniper_config(text)


def load_sniper_document(path: Path) -> dict[str, Any]:
    """Load the raw watcher mapping through the strict YAML loader."""

    try:
        text = path.read_text(encoding="utf-8")
        document = yaml.load(text, Loader=_StrictLoader)
    except (OSError, yaml.YAMLError, TypeError, SniperConfigError) as error:
        raise SniperConfigError(f"cannot read watcher config: {path}") from error
    if type(document) is not dict:
        raise SniperConfigError("watcher config must be one mapping")
    return document


def save_sniper_document(path: Path, document: dict[str, Any]) -> CoreSniperConfig:
    """Validate and atomically replace one watcher YAML document."""

    if type(document) is not dict:
        raise SniperConfigError("watcher config must be one mapping")
    try:
        candidate = yaml.safe_dump(document, sort_keys=False)
        config = parse_sniper_config(candidate)
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            "w",
            delete=False,
            dir=path.parent,
            encoding="utf-8",
        ) as temporary:
            temporary.write(candidate)
            temporary.flush()
            os.fsync(temporary.fileno())
            temporary_path = Path(temporary.name)
        temporary_path.replace(path)
    except (OSError, TypeError, ValueError, yaml.YAMLError) as error:
        raise SniperConfigError("watcher config was not saved") from error
    return config


def load_wallet_portfolio(path: Path) -> WalletPortfolio:
    """Load a strict YAML wallet portfolio document."""

    try:
        text = path.read_text(encoding="utf-8")
    except OSError as error:
        raise SniperConfigError(f"cannot read wallet portfolio: {path}") from error
    return parse_wallet_portfolio(text)


def parse_wallet_portfolio(text: str) -> WalletPortfolio:
    """Parse an ordered portfolio of unique Solana wallet public keys."""

    try:
        document = yaml.load(text, Loader=_StrictLoader)
    except (yaml.YAMLError, TypeError) as error:
        raise SniperConfigError("wallet portfolio is not valid YAML") from error
    if type(document) is not dict:
        raise SniperConfigError("wallet portfolio must be one mapping")
    if "schema" in document or "version" in document:
        raise SniperConfigError("schema and version fields are forbidden")
    _require_exact_keys(document, {"wallets"}, "wallet portfolio")
    wallets = document["wallets"]
    if type(wallets) is not list or not wallets:
        raise SniperConfigError("wallet portfolio.wallets must be a non-empty list")
    if len(wallets) > MAX_PORTFOLIO_WALLETS:
        raise SniperConfigError(
            "wallet portfolio.wallets must contain at most "
            f"{MAX_PORTFOLIO_WALLETS} wallets"
        )
    parsed: list[str] = []
    seen: set[str] = set()
    for index, wallet in enumerate(wallets):
        field_name = f"wallet portfolio.wallets[{index}]"
        _validate_pubkey(wallet, field_name)
        if wallet in seen:
            raise SniperConfigError(f"duplicate wallet in portfolio: {wallet}")
        seen.add(wallet)
        parsed.append(wallet)
    return WalletPortfolio(wallets=tuple(parsed))


def parse_sniper_config(text: str) -> CoreSniperConfig:
    """Parse one closed-shape watcher configuration."""

    try:
        document = yaml.load(text, Loader=_StrictLoader)
    except (yaml.YAMLError, TypeError) as error:
        raise SniperConfigError("watcher config is not valid YAML") from error
    if type(document) is not dict:
        raise SniperConfigError("watcher config must be one mapping")
    if "schema" in document or "version" in document:
        raise SniperConfigError("schema and version fields are forbidden")
    _require_known_keys(
        document, {"target", "execution", "rules", "volume_sizing", "strategy"}
    )
    _require_required_keys(document, {"target", "execution"})

    return CoreSniperConfig(
        target=_parse_target(document["target"]),
        execution=_parse_execution(document["execution"]),
        rules=_parse_rules(document.get("rules")),
        volume_sizing=_parse_volume_sizing(document.get("volume_sizing")),
        strategy=_parse_strategy(document.get("strategy")),
    )


def _parse_target(raw: object) -> SniperTarget:
    mapping = _mapping(raw, "target")
    _require_exact_keys(mapping, {"kind", "id"}, "target")
    kind = mapping["kind"]
    target_id = mapping["id"]
    if not isinstance(kind, str) or kind not in {item.value for item in TargetKind}:
        raise SniperConfigError("target.kind must be wallet or token")
    _validate_pubkey(target_id, "target.id")
    return SniperTarget(kind=TargetKind(kind), id=target_id)


def _parse_execution(raw: object) -> SniperExecution:
    mapping = _mapping(raw, "execution")
    _require_known_keys(
        mapping, {"mode", "quote_size_lamports", "max_slippage_bps"}, "execution"
    )
    mode = mapping["mode"]
    quote_size = mapping["quote_size_lamports"]
    if not isinstance(mode, str) or mode not in {item.value for item in ExecutionMode}:
        raise SniperConfigError("execution.mode must be observe, paper, or live")
    if mode == ExecutionMode.LIVE.value:
        raise SniperConfigError(
            "execution.mode live is disabled until paper and out-of-sample evidence"
        )
    if type(quote_size) is not int or quote_size <= 0:
        raise SniperConfigError(
            "execution.quote_size_lamports must be a positive integer"
        )
    max_slippage_bps = mapping.get("max_slippage_bps", 500)
    if (
        type(max_slippage_bps) is not int
        or not 0 <= max_slippage_bps <= MAX_SLIPPAGE_BPS
    ):
        raise SniperConfigError(
            "execution.max_slippage_bps must be between 0 and 10000"
        )
    return SniperExecution(
        mode=ExecutionMode(mode),
        quote_size_lamports=quote_size,
        max_slippage_bps=max_slippage_bps,
    )


def _parse_volume_sizing(raw: object) -> VolumeSizingPolicy:
    if raw is None:
        return VolumeSizingPolicy()
    mapping = _mapping(raw, "volume_sizing")
    _require_exact_keys(
        mapping,
        {
            "max_bankroll_fraction_ppm",
            "max_independent_volume_fraction_ppm",
            "max_price_impact_ppm",
        },
        "volume_sizing",
    )
    return VolumeSizingPolicy(
        max_bankroll_fraction_ppm=_bounded_ppm(
            mapping["max_bankroll_fraction_ppm"],
            "volume_sizing.max_bankroll_fraction_ppm",
        ),
        max_independent_volume_fraction_ppm=_bounded_ppm(
            mapping["max_independent_volume_fraction_ppm"],
            "volume_sizing.max_independent_volume_fraction_ppm",
        ),
        max_price_impact_ppm=_bounded_ppm(
            mapping["max_price_impact_ppm"],
            "volume_sizing.max_price_impact_ppm",
        ),
    )


def _parse_strategy(raw: object) -> StrategyFilterSettings:
    if raw is None:
        return StrategyFilterSettings()
    mapping = _mapping(raw, "strategy")
    _require_known_keys(
        mapping,
        {
            "min_volume_usd_micro",
            "max_creator_pairs",
            "history_sample_count",
            "min_win_rate_ppm",
            "max_buys_per_hour",
            "max_entry_transaction_index",
            "max_entry_market_cap_quote_base_units",
            "max_entry_deviation_ppm",
            "require_bundle_match",
            "require_double_signature",
            "require_prior_zero_balance",
        },
        "strategy",
    )
    booleans = (
        "require_bundle_match",
        "require_double_signature",
        "require_prior_zero_balance",
    )
    for field_name in booleans:
        value = mapping.get(field_name, False)
        if type(value) is not bool:
            raise SniperConfigError(f"strategy.{field_name} must be boolean")
    history_sample_count = mapping.get("history_sample_count", 10)
    max_buys_per_hour = mapping.get("max_buys_per_hour", 1)
    max_entry_transaction_index = mapping.get("max_entry_transaction_index", 1)
    if (
        type(history_sample_count) is not int
        or not 1 <= history_sample_count <= MAX_STRATEGY_HISTORY_SAMPLES
    ):
        raise SniperConfigError(
            "strategy.history_sample_count must be between 1 and 100"
        )
    if (
        type(max_buys_per_hour) is not int
        or not 1 <= max_buys_per_hour <= MAX_STRATEGY_BUYS_PER_HOUR
    ):
        raise SniperConfigError(
            "strategy.max_buys_per_hour must be between 1 and 10000"
        )
    if (
        type(max_entry_transaction_index) is not int
        or not 0 <= max_entry_transaction_index <= MAX_STRATEGY_ENTRY_INDEX
    ):
        raise SniperConfigError(
            "strategy.max_entry_transaction_index must be between 0 and 20"
        )
    return StrategyFilterSettings(
        min_volume_usd_micro=_optional_positive_int(
            mapping.get("min_volume_usd_micro"),
            "strategy.min_volume_usd_micro",
        ),
        max_creator_pairs=_optional_positive_int(
            mapping.get("max_creator_pairs"),
            "strategy.max_creator_pairs",
            maximum=100_000,
        ),
        history_sample_count=history_sample_count,
        min_win_rate_ppm=_bounded_ppm(
            mapping.get("min_win_rate_ppm", 500_000),
            "strategy.min_win_rate_ppm",
        ),
        max_buys_per_hour=max_buys_per_hour,
        max_entry_transaction_index=max_entry_transaction_index,
        max_entry_market_cap_quote_base_units=_optional_non_negative_int(
            mapping.get("max_entry_market_cap_quote_base_units"),
            "strategy.max_entry_market_cap_quote_base_units",
        ),
        max_entry_deviation_ppm=_bounded_ppm(
            mapping.get("max_entry_deviation_ppm", 250_000),
            "strategy.max_entry_deviation_ppm",
        ),
        require_bundle_match=mapping.get("require_bundle_match", False),
        require_double_signature=mapping.get("require_double_signature", False),
        require_prior_zero_balance=mapping.get("require_prior_zero_balance", False),
    )


def _parse_rules(raw: object) -> PlaybookRules:
    if raw is None:
        return PlaybookRules()
    mapping = _mapping(raw, "rules")
    _require_known_keys(
        mapping,
        {
            "snipe_delay_seconds",
            "min_market_cap_quote_base_units",
            "max_market_cap_quote_base_units",
            "max_token_age_minutes",
            "follow_cooldown_seconds",
            "buy_only_once",
            "buy_the_dip",
            "sell",
            "max_consecutive_losses",
        },
        "rules",
    )
    snipe_delay_ms = _duration_ms(
        mapping.get("snipe_delay_seconds", 0), "rules.snipe_delay_seconds"
    )
    max_token_age_ms = _optional_duration_ms(
        mapping.get("max_token_age_minutes", 0),
        "rules.max_token_age_minutes",
        multiplier=60_000,
    )
    cooldown_ms = _duration_ms(
        mapping.get("follow_cooldown_seconds", 0),
        "rules.follow_cooldown_seconds",
    )
    min_market_cap = _optional_non_negative_int(
        mapping.get("min_market_cap_quote_base_units"),
        "rules.min_market_cap_quote_base_units",
    )
    max_market_cap = _optional_non_negative_int(
        mapping.get("max_market_cap_quote_base_units"),
        "rules.max_market_cap_quote_base_units",
    )
    if (
        min_market_cap is not None
        and max_market_cap is not None
        and min_market_cap > max_market_cap
    ):
        raise SniperConfigError("rules market-cap bounds are inverted")
    buy_only_once = mapping.get("buy_only_once", False)
    if type(buy_only_once) is not bool:
        raise SniperConfigError("rules.buy_only_once must be boolean")
    max_losses = _optional_positive_int(
        mapping.get("max_consecutive_losses"),
        "rules.max_consecutive_losses",
        maximum=20,
    )
    return PlaybookRules(
        snipe_delay_ms=snipe_delay_ms,
        min_market_cap_quote_base_units=min_market_cap,
        max_market_cap_quote_base_units=max_market_cap,
        max_token_age_ms=max_token_age_ms,
        copytrade_cooldown_ms=cooldown_ms,
        buy_only_once=buy_only_once,
        buy_the_dip_levels=_parse_dip_levels(mapping.get("buy_the_dip")),
        sell=_parse_sell_rules(mapping.get("sell")),
        max_consecutive_losses=max_losses,
    )


def _parse_dip_levels(raw: object) -> tuple[BuyTheDipLevel, ...]:
    if raw is None:
        return ()
    mapping = _mapping(raw, "rules.buy_the_dip")
    _require_known_keys(mapping, {"levels"}, "rules.buy_the_dip")
    levels = mapping.get("levels", [])
    if type(levels) is not list or len(levels) > MAX_DIP_LEVELS:
        raise SniperConfigError(
            "rules.buy_the_dip.levels must contain at most 3 levels"
        )
    parsed: list[BuyTheDipLevel] = []
    for index, raw_level in enumerate(levels):
        level = _mapping(raw_level, f"rules.buy_the_dip.levels[{index}]")
        _require_exact_keys(
            level,
            {"drawdown_ppm", "quote_size_lamports"},
            f"rules.buy_the_dip.levels[{index}]",
        )
        drawdown = _bounded_ppm(
            level["drawdown_ppm"],
            f"rules.buy_the_dip.levels[{index}].drawdown_ppm",
            strictly_positive=True,
        )
        quote_size = _positive_int(
            level["quote_size_lamports"],
            f"rules.buy_the_dip.levels[{index}].quote_size_lamports",
        )
        parsed.append(
            BuyTheDipLevel(
                drawdown_ppm=drawdown,
                quote_size_lamports=quote_size,
            )
        )
    return tuple(parsed)


def _parse_sell_rules(raw: object) -> SellRules:
    if raw is None:
        return SellRules()
    mapping = _mapping(raw, "rules.sell")
    _require_known_keys(
        mapping,
        {
            "take_profit_levels",
            "stop_loss_levels",
            "trailing_levels",
            "no_activity_seconds",
            "auto_sell_big_buy",
        },
        "rules.sell",
    )
    return SellRules(
        take_profit_levels=_parse_sell_levels(
            mapping.get("take_profit_levels", []),
            "rules.sell.take_profit_levels",
            positive=True,
        ),
        stop_loss_levels=_parse_sell_levels(
            mapping.get("stop_loss_levels", []),
            "rules.sell.stop_loss_levels",
            positive=False,
        ),
        trailing_levels=_parse_trailing_levels(mapping.get("trailing_levels", [])),
        no_activity_timeout_ms=_optional_duration_ms(
            mapping.get("no_activity_seconds", 0),
            "rules.sell.no_activity_seconds",
            multiplier=1_000,
        ),
        auto_sell_big_buy_levels=_parse_big_buy_levels(
            mapping.get("auto_sell_big_buy")
        ),
    )


def _parse_big_buy_levels(raw: object) -> tuple[BigBuySellLevel, ...]:
    """Parse bounded quote-size ranges for the playbook big-buy exit."""

    if raw is None:
        return ()
    mapping = _mapping(raw, "rules.sell.auto_sell_big_buy")
    _require_exact_keys(mapping, {"levels"}, "rules.sell.auto_sell_big_buy")
    levels = mapping["levels"]
    if type(levels) is not list or len(levels) > MAX_BIG_BUY_LEVELS:
        raise SniperConfigError(
            "rules.sell.auto_sell_big_buy.levels must contain at most 3 levels"
        )
    parsed: list[BigBuySellLevel] = []
    previous_max = -1
    for index, raw_level in enumerate(levels):
        field_name = f"rules.sell.auto_sell_big_buy.levels[{index}]"
        level = _mapping(raw_level, field_name)
        _require_exact_keys(
            level,
            {"min_quote_base_units", "max_quote_base_units", "sell_fraction_ppm"},
            field_name,
        )
        minimum = _positive_int(
            level["min_quote_base_units"],
            f"{field_name}.min_quote_base_units",
        )
        maximum = _positive_int(
            level["max_quote_base_units"],
            f"{field_name}.max_quote_base_units",
        )
        if minimum > maximum or minimum <= previous_max:
            raise SniperConfigError(
                f"{field_name} ranges must be ordered and non-overlapping"
            )
        fraction = _bounded_ppm(
            level["sell_fraction_ppm"],
            f"{field_name}.sell_fraction_ppm",
            strictly_positive=True,
        )
        parsed.append(
            BigBuySellLevel(
                min_quote_base_units=minimum,
                max_quote_base_units=maximum,
                sell_fraction_ppm=fraction,
            )
        )
        previous_max = maximum
    return tuple(parsed)


def _parse_sell_levels(
    raw: object,
    field_name: str,
    *,
    positive: bool,
) -> tuple[SellLevel, ...]:
    if type(raw) is not list or len(raw) > MAX_SELL_LEVELS:
        raise SniperConfigError(f"{field_name} must contain at most 5 levels")
    parsed: list[SellLevel] = []
    for index, raw_level in enumerate(raw):
        level_name = f"{field_name}[{index}]"
        level = _mapping(raw_level, level_name)
        _require_exact_keys(level, {"trigger_pnl_ppm", "sell_fraction_ppm"}, level_name)
        trigger = _bounded_pnl(level["trigger_pnl_ppm"], level_name, positive=positive)
        fraction = _bounded_ppm(
            level["sell_fraction_ppm"],
            f"{level_name}.sell_fraction_ppm",
            strictly_positive=True,
        )
        parsed.append(SellLevel(trigger_pnl_ppm=trigger, sell_fraction_ppm=fraction))
    return tuple(parsed)


def _parse_trailing_levels(raw: object) -> tuple[TrailingStopLevel, ...]:
    if type(raw) is not list or len(raw) > MAX_SELL_LEVELS:
        raise SniperConfigError(
            "rules.sell.trailing_levels must contain at most 5 levels"
        )
    parsed: list[TrailingStopLevel] = []
    for index, raw_level in enumerate(raw):
        level_name = f"rules.sell.trailing_levels[{index}]"
        level = _mapping(raw_level, level_name)
        _require_exact_keys(
            level, {"min_market_cap_quote_base_units", "drawdown_ppm"}, level_name
        )
        min_market_cap = _optional_non_negative_int(
            level["min_market_cap_quote_base_units"],
            f"{level_name}.min_market_cap_quote_base_units",
        )
        drawdown = _bounded_ppm(
            level["drawdown_ppm"],
            f"{level_name}.drawdown_ppm",
            strictly_positive=True,
        )
        parsed.append(
            TrailingStopLevel(
                min_market_cap_quote_base_units=min_market_cap,
                drawdown_ppm=drawdown,
            )
        )
    return tuple(parsed)


def _mapping(raw: object, field_name: str) -> dict[Any, Any]:
    if type(raw) is not dict:
        raise SniperConfigError(f"{field_name} must be a mapping")
    return raw


def _require_exact_keys(
    mapping: dict[Any, Any],
    expected: set[str],
    field_name: str = "config",
) -> None:
    if set(mapping) != expected:
        raise SniperConfigError(f"{field_name} has unknown or missing fields")


def _require_known_keys(
    mapping: dict[Any, Any],
    expected: set[str],
    field_name: str = "config",
) -> None:
    if not all(isinstance(key, str) for key in mapping) or not set(mapping) <= expected:
        raise SniperConfigError(f"{field_name} has unknown fields")


def _require_required_keys(
    mapping: dict[Any, Any], required: set[str], field_name: str = "config"
) -> None:
    if not required <= set(mapping):
        raise SniperConfigError(f"{field_name} has missing fields")


def _duration_ms(value: object, field_name: str) -> int:
    if type(value) is not int or value < 0:
        raise SniperConfigError(f"{field_name} must be a non-negative integer")
    return value * 1_000


def _optional_duration_ms(
    value: object,
    field_name: str,
    *,
    multiplier: int,
) -> int | None:
    if type(value) is not int or value < 0:
        raise SniperConfigError(f"{field_name} must be a non-negative integer")
    return None if value == 0 else value * multiplier


def _optional_non_negative_int(value: object, field_name: str) -> int | None:
    if value is None:
        return None
    if type(value) is not int or value < 0:
        raise SniperConfigError(f"{field_name} must be a non-negative integer or null")
    return None if value == 0 else value


def _optional_positive_int(
    value: object,
    field_name: str,
    *,
    maximum: int | None = None,
) -> int | None:
    if value is None or value == 0:
        if value is not None and type(value) is not int:
            raise SniperConfigError(f"{field_name} must be an integer or null")
        return None
    if type(value) is not int or value < 1 or (maximum is not None and value > maximum):
        raise SniperConfigError(f"{field_name} is outside its supported range")
    return value


def _positive_int(value: object, field_name: str) -> int:
    if type(value) is not int or value <= 0:
        raise SniperConfigError(f"{field_name} must be a positive integer")
    return value


def _bounded_ppm(
    value: object,
    field_name: str,
    *,
    strictly_positive: bool = False,
) -> int:
    lower = 1 if strictly_positive else 0
    if type(value) is not int or not lower <= value <= PROBABILITY_PPM_DENOMINATOR:
        raise SniperConfigError(
            f"{field_name} must be an integer between {lower} and "
            f"{PROBABILITY_PPM_DENOMINATOR}"
        )
    return value


def _bounded_pnl(value: object, field_name: str, *, positive: bool) -> int:
    if type(value) is not int:
        raise SniperConfigError(f"{field_name}.trigger_pnl_ppm must be an integer")
    if positive and not 0 < value <= PROBABILITY_PPM_DENOMINATOR:
        raise SniperConfigError(f"{field_name}.trigger_pnl_ppm must be positive")
    if not positive and not -PROBABILITY_PPM_DENOMINATOR <= value < 0:
        raise SniperConfigError(f"{field_name}.trigger_pnl_ppm must be negative")
    return value


def _validate_pubkey(value: object, field_name: str) -> None:
    if not isinstance(value, str) or not value:
        raise SniperConfigError(f"{field_name} must be a Solana base58 public key")
    try:
        decoded = base58.b58decode(value)
    except ValueError as error:
        raise SniperConfigError(
            f"{field_name} must be a Solana base58 public key"
        ) from error
    if (
        len(decoded) != PUBKEY_LENGTH
        or base58.b58encode(decoded).decode("ascii") != value
    ):
        raise SniperConfigError(f"{field_name} must be a Solana base58 public key")
