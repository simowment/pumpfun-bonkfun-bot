"""DB-backed config store (app_config table) for Rugbot."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from rugbot.domain.scalper_strategy import ScalperConfig
from rugbot.runtime.config import (
    CoreSniperConfig,
    SniperConfigError,
    WalletPortfolio,
    default_sniper_config,
    default_wallet_portfolio,
    parse_sniper_config_dict,
    parse_wallet_portfolio_dict,
    resolve_state_dir,
)
from rugbot.storage.database import DatabaseManager
from rugbot.utils.logger import get_logger

logger = get_logger(__name__)

CONFIG_TYPES = {"sniper", "portfolio", "scalper"}


def _ensure_app_config_table(db: DatabaseManager) -> None:
    db.connection.execute(
        """
        CREATE TABLE IF NOT EXISTS app_config (
            config_type TEXT PRIMARY KEY,
            payload_json TEXT NOT NULL,
            updated_at INTEGER NOT NULL
        )
        """
    )
    db.connection.execute("PRAGMA journal_mode=WAL")


def _db_path_for_state_dir(state_dir: Path | str | None) -> Path:
    sd = resolve_state_dir(Path(state_dir) if state_dir is not None else None)
    return sd / "rugbot.db"


class ConfigStore:
    """Thin wrapper around DatabaseManager for app_config."""

    def __init__(
        self, state_dir: Path | str | None = None, db: DatabaseManager | None = None
    ) -> None:
        if db is not None:
            self._db = db
        else:
            self._db = DatabaseManager(_db_path_for_state_dir(state_dir))
        _ensure_app_config_table(self._db)

    def get_config(self, config_type: str) -> dict[str, Any] | None:
        if config_type not in CONFIG_TYPES:
            raise SniperConfigError(f"unknown config_type: {config_type}")
        try:
            row = self._db.connection.execute(
                "SELECT payload_json FROM app_config WHERE config_type=?",
                (config_type,),
            ).fetchone()
        except Exception as exc:
            raise SniperConfigError(f"config DB unavailable: {exc}") from exc
        if row is None:
            return None
        try:
            payload = json.loads(row["payload_json"])
        except (json.JSONDecodeError, TypeError) as exc:
            raise SniperConfigError(
                f"config payload corrupt for {config_type}"
            ) from exc
        if type(payload) is not dict:
            raise SniperConfigError(f"config payload must be mapping for {config_type}")
        return payload

    def set_config(self, config_type: str, mapping: dict[str, Any]) -> None:
        if config_type not in CONFIG_TYPES:
            raise SniperConfigError(f"unknown config_type: {config_type}")
        if type(mapping) is not dict:
            raise SniperConfigError("config mapping must be a dict")
        # validate via dict parsers
        if config_type == "sniper":
            parse_sniper_config_dict(mapping, source="db")
        elif config_type == "portfolio":
            parse_wallet_portfolio_dict(mapping, source="db")
        elif config_type == "scalper":
            _validate_scalper_dict(mapping)
        payload_json = json.dumps(mapping, sort_keys=True)
        try:
            self._db.connection.execute(
                "INSERT INTO app_config(config_type,payload_json,updated_at) VALUES(?,?,?) "
                "ON CONFLICT(config_type) DO UPDATE SET payload_json=excluded.payload_json, updated_at=excluded.updated_at",
                (config_type, payload_json, int(time.time())),
            )
            self._db.connection.commit()
        except Exception as exc:
            raise SniperConfigError(f"config DB write failed: {exc}") from exc

    def delete_config(self, config_type: str) -> None:
        if config_type not in CONFIG_TYPES:
            raise SniperConfigError(f"unknown config_type: {config_type}")
        try:
            self._db.connection.execute(
                "DELETE FROM app_config WHERE config_type=?", (config_type,)
            )
            self._db.connection.commit()
        except Exception as exc:
            raise SniperConfigError(f"config DB delete failed: {exc}") from exc


def _validate_scalper_dict(mapping: dict[str, Any]) -> ScalperConfig:
    allowed = {
        "position_size_sol",
        "entry_mc_max_sol",
        "entry_max_quote_lamports",
        "tp_levels_pct",
        "sl_pct",
        "sell_fractions",
        "daily_loss_stop",
        "max_concurrent",
        "max_hold_slots",
        "max_entry_slot_offset",
        "min_trades_for_entry",
    }
    unknown = set(mapping) - allowed
    if unknown:
        raise SniperConfigError(f"scalper config has unknown fields: {sorted(unknown)}")
    filtered: dict[str, Any] = {}
    for k, v in mapping.items():
        if k in ("tp_levels_pct", "sell_fractions"):
            if not isinstance(v, list):
                raise SniperConfigError(f"scalper.{k} must be a list")
            filtered[k] = tuple(float(x) for x in v)
        else:
            filtered[k] = v
    try:
        return ScalperConfig(**filtered)
    except (ValueError, TypeError) as exc:
        raise SniperConfigError(f"scalper config invalid: {exc}") from exc


def get_config(state_dir: Path | str | None, config_type: str) -> dict[str, Any] | None:
    return ConfigStore(state_dir=state_dir).get_config(config_type)


def set_config(
    state_dir: Path | str | None, config_type: str, mapping: dict[str, Any]
) -> None:
    ConfigStore(state_dir=state_dir).set_config(config_type, mapping)


def delete_config(state_dir: Path | str | None, config_type: str) -> None:
    ConfigStore(state_dir=state_dir).delete_config(config_type)


def load_sniper_config_db(state_dir: Path | str | None = None) -> CoreSniperConfig:
    mapping = get_config(state_dir, "sniper")
    if mapping is None:
        return default_sniper_config()
    return parse_sniper_config_dict(mapping, source="db")


def load_wallet_portfolio_db(state_dir: Path | str | None = None) -> WalletPortfolio:
    mapping = get_config(state_dir, "portfolio")
    if mapping is None:
        return default_wallet_portfolio()
    return parse_wallet_portfolio_dict(mapping, source="db")


def load_scalper_config_db(state_dir: Path | str | None = None) -> ScalperConfig:
    mapping = get_config(state_dir, "scalper")
    if mapping is None:
        return ScalperConfig()
    return _validate_scalper_dict(mapping)


def set_config_db(
    state_dir: Path | str | None, config_type: str, mapping: dict[str, Any]
) -> None:
    set_config(state_dir, config_type, mapping)


# Helpers for dumping dataclass to mapping for rug_config show


def sniper_to_mapping(cfg: CoreSniperConfig) -> dict[str, Any]:
    import dataclasses

    # manual mapping mirrors yaml structure
    return {
        "target": {"kind": cfg.target.kind.value, "id": cfg.target.id},
        "execution": {
            "mode": cfg.execution.mode.value,
            "quote_size_lamports": cfg.execution.quote_size_lamports,
            "max_slippage_bps": cfg.execution.max_slippage_bps,
            "signer_pubkey": cfg.execution.signer_pubkey,
            "routing_policy": cfg.execution.routing_policy,
            "priority_fee_microlamports": cfg.execution.priority_fee_microlamports,
            "jito_tip_lamports": cfg.execution.jito_tip_lamports,
            "compute_unit_limit": cfg.execution.compute_unit_limit,
            "loaded_accounts_data_size_limit": cfg.execution.loaded_accounts_data_size_limit,
            "jito_block_engine_url": cfg.execution.jito_block_engine_url,
        },
        "risk": {
            "max_buy_lamports": cfg.risk.max_buy_lamports,
            "max_exposure_lamports": cfg.risk.max_exposure_lamports,
            "daily_loss_limit_lamports": cfg.risk.daily_loss_limit_lamports,
            "max_open_positions": cfg.risk.max_open_positions,
            "minimum_wallet_reserve_lamports": cfg.risk.minimum_wallet_reserve_lamports,
        },
        "tracking_mode": cfg.tracking_mode.value,
        "listener": cfg.listener.value,
        "volume_sizing": dataclasses.asdict(cfg.volume_sizing),
        "strategy": dataclasses.asdict(cfg.strategy),
        "rules": {
            "snipe_delay_seconds": cfg.rules.snipe_delay_ms // 1000,
            "min_market_cap_quote_base_units": cfg.rules.min_market_cap_quote_base_units,
            "max_market_cap_quote_base_units": cfg.rules.max_market_cap_quote_base_units,
            "max_token_age_minutes": (cfg.rules.max_token_age_ms // 60000)
            if cfg.rules.max_token_age_ms is not None
            else 0,
            "follow_cooldown_seconds": cfg.rules.copytrade_cooldown_ms // 1000,
            "buy_only_once": cfg.rules.buy_only_once,
            "max_consecutive_losses": cfg.rules.max_consecutive_losses,
            "buy_the_dip": {
                "levels": [dataclasses.asdict(l) for l in cfg.rules.buy_the_dip_levels]
            },
            "sell": {
                "take_profit_levels": [
                    dataclasses.asdict(l) for l in cfg.rules.sell.take_profit_levels
                ],
                "stop_loss_levels": [
                    dataclasses.asdict(l) for l in cfg.rules.sell.stop_loss_levels
                ],
                "trailing_levels": [
                    dataclasses.asdict(l) for l in cfg.rules.sell.trailing_levels
                ],
                "no_activity_seconds": (cfg.rules.sell.no_activity_timeout_ms // 1000)
                if cfg.rules.sell.no_activity_timeout_ms is not None
                else 0,
                "auto_sell_big_buy": {
                    "levels": [
                        dataclasses.asdict(l)
                        for l in cfg.rules.sell.auto_sell_big_buy_levels
                    ]
                },
            },
        },
    }


def portfolio_to_mapping(pf: WalletPortfolio) -> dict[str, Any]:
    return {"wallets": list(pf.wallets)}


def scalper_to_mapping(cfg: ScalperConfig) -> dict[str, Any]:
    return {
        "position_size_sol": cfg.position_size_sol,
        "entry_mc_max_sol": cfg.entry_mc_max_sol,
        "entry_max_quote_lamports": cfg.entry_max_quote_lamports,
        "tp_levels_pct": list(cfg.tp_levels_pct),
        "sl_pct": cfg.sl_pct,
        "sell_fractions": list(cfg.sell_fractions),
        "daily_loss_stop": cfg.daily_loss_stop,
        "max_concurrent": cfg.max_concurrent,
        "max_hold_slots": cfg.max_hold_slots,
        "max_entry_slot_offset": cfg.max_entry_slot_offset,
        "min_trades_for_entry": cfg.min_trades_for_entry,
    }
