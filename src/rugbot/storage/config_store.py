"""DB-backed config store (app_config table) for Rugbot."""

# ruff: noqa: TRY003

from __future__ import annotations

import dataclasses
import json
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from rugbot.domain.scalper_strategy import ScalperConfig
from rugbot.runtime.config import (
    CoreSniperConfig,
    SniperConfigError,
    TargetKind,
    default_sniper_config,
    parse_sniper_config_dict,
    resolve_tracker_db_path,
)
from rugbot.storage.database import DatabaseManager
from rugbot.utils.logger import get_logger

if TYPE_CHECKING:
    import sqlite3
    from pathlib import Path

logger = get_logger(__name__)

CONFIG_TYPES = {"sniper", "scalper"}
MAX_TRACKERS = 100


@dataclass(frozen=True, slots=True)
class Tracker:
    """One tracked wallet: its full sniper config plus operator bookkeeping."""

    wallet: str
    group: str | None
    enabled: bool
    config: CoreSniperConfig


def _ensure_tables(db: DatabaseManager) -> None:
    for statement in (
        """
        CREATE TABLE IF NOT EXISTS app_config (
            config_type TEXT PRIMARY KEY,
            payload_json TEXT NOT NULL,
            updated_at INTEGER NOT NULL
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS trackers (
            wallet TEXT PRIMARY KEY,
            group_name TEXT,
            enabled INTEGER NOT NULL,
            payload_json TEXT NOT NULL,
            updated_at INTEGER NOT NULL
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS tracker_presets (
            name TEXT PRIMARY KEY,
            payload_json TEXT NOT NULL,
            updated_at INTEGER NOT NULL
        )
        """,
    ):
        db.connection.execute(statement)
    db.connection.execute("PRAGMA journal_mode=WAL")


class ConfigStore:
    """Thin wrapper around DatabaseManager for app_config."""

    def __init__(
        self, state_dir: Path | str | None = None, db: DatabaseManager | None = None
    ) -> None:
        if db is not None:
            self._db = db
        else:
            self._db = DatabaseManager(resolve_tracker_db_path(state_dir))
        _ensure_tables(self._db)

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

    def list_trackers(self, group: str | None = None) -> tuple[Tracker, ...]:
        """Return every tracker, optionally only one group, ordered by wallet."""

        rows = self._db.connection.execute(
            "SELECT wallet, group_name, enabled, payload_json FROM trackers "
            "WHERE ? IS NULL OR group_name = ? ORDER BY wallet",
            (group, group),
        ).fetchall()
        return tuple(_tracker_from_row(row) for row in rows)

    def get_tracker(self, wallet: str) -> Tracker | None:
        """Return one tracker, or ``None`` when the wallet is not tracked."""

        row = self._db.connection.execute(
            "SELECT wallet, group_name, enabled, payload_json FROM trackers "
            "WHERE wallet = ?",
            (wallet,),
        ).fetchone()
        return None if row is None else _tracker_from_row(row)

    def save_tracker(
        self,
        mapping: dict[str, Any],
        *,
        group: str | None,
        enabled: bool,
    ) -> Tracker:
        """Validate one sniper config mapping and upsert it as a tracker.

        The tracked wallet is the config's ``target.id``.
        """

        config = parse_sniper_config_dict(mapping, source="tracker")
        if config.target.kind is not TargetKind.WALLET:
            raise SniperConfigError("a tracker must target a wallet")
        wallet = config.target.id
        if self.get_tracker(wallet) is None and len(self.list_trackers()) >= (
            MAX_TRACKERS
        ):
            raise SniperConfigError(f"at most {MAX_TRACKERS} trackers are allowed")
        self._db.connection.execute(
            "INSERT INTO trackers(wallet,group_name,enabled,payload_json,updated_at) "
            "VALUES(?,?,?,?,?) ON CONFLICT(wallet) DO UPDATE SET "
            "group_name=excluded.group_name, enabled=excluded.enabled, "
            "payload_json=excluded.payload_json, updated_at=excluded.updated_at",
            (
                wallet,
                group,
                int(enabled),
                json.dumps(sniper_to_mapping(config), sort_keys=True),
                int(time.time()),
            ),
        )
        self._db.connection.commit()
        return Tracker(wallet=wallet, group=group, enabled=enabled, config=config)

    def delete_tracker(self, wallet: str) -> bool:
        """Remove one tracker; return whether it existed."""

        cursor = self._db.connection.execute(
            "DELETE FROM trackers WHERE wallet = ?", (wallet,)
        )
        self._db.connection.commit()
        return cursor.rowcount > 0

    def list_presets(self) -> tuple[str, ...]:
        """Return saved preset names in order."""

        rows = self._db.connection.execute(
            "SELECT name FROM tracker_presets ORDER BY name"
        ).fetchall()
        return tuple(row["name"] for row in rows)

    def get_preset(self, name: str) -> dict[str, Any] | None:
        """Return one preset's sniper config mapping, or ``None``."""

        row = self._db.connection.execute(
            "SELECT payload_json FROM tracker_presets WHERE name = ?", (name,)
        ).fetchone()
        return None if row is None else json.loads(row["payload_json"])

    def save_preset(self, name: str, config: CoreSniperConfig) -> None:
        """Save one validated sniper config as a reusable preset."""

        self._db.connection.execute(
            "INSERT INTO tracker_presets(name,payload_json,updated_at) VALUES(?,?,?) "
            "ON CONFLICT(name) DO UPDATE SET payload_json=excluded.payload_json, "
            "updated_at=excluded.updated_at",
            (
                name,
                json.dumps(sniper_to_mapping(config), sort_keys=True),
                int(time.time()),
            ),
        )
        self._db.connection.commit()

    def delete_preset(self, name: str) -> bool:
        """Remove one preset; return whether it existed."""

        cursor = self._db.connection.execute(
            "DELETE FROM tracker_presets WHERE name = ?", (name,)
        )
        self._db.connection.commit()
        return cursor.rowcount > 0


def _tracker_from_row(row: sqlite3.Row) -> Tracker:
    try:
        mapping = json.loads(row["payload_json"])
    except (json.JSONDecodeError, TypeError) as exc:
        raise SniperConfigError(f"tracker payload corrupt for {row['wallet']}") from exc
    return Tracker(
        wallet=row["wallet"],
        group=row["group_name"],
        enabled=bool(row["enabled"]),
        config=parse_sniper_config_dict(mapping, source="tracker"),
    )


def set_dotted(mapping: dict[str, Any], key: str, value: object) -> None:
    """Set one existing dotted key (``rules.sell.no_activity_seconds``) in place.

    Unknown paths raise instead of creating keys, so typos never pass silently.
    """

    *parents, leaf = key.split(".")
    target = mapping
    for part in parents:
        child = target.get(part)
        if not isinstance(child, dict):
            raise SniperConfigError(f"unknown key path: {key}")
        target = child
    if leaf not in target:
        raise SniperConfigError(f"unknown key: {key}")
    target[leaf] = value


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
        "funding": dataclasses.asdict(cfg.funding),
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
                "levels": [
                    dataclasses.asdict(level) for level in cfg.rules.buy_the_dip_levels
                ]
            },
            "sell": {
                "take_profit_levels": [
                    dataclasses.asdict(level)
                    for level in cfg.rules.sell.take_profit_levels
                ],
                "stop_loss_levels": [
                    dataclasses.asdict(level)
                    for level in cfg.rules.sell.stop_loss_levels
                ],
                "trailing_levels": [
                    dataclasses.asdict(level)
                    for level in cfg.rules.sell.trailing_levels
                ],
                "no_activity_seconds": (cfg.rules.sell.no_activity_timeout_ms // 1000)
                if cfg.rules.sell.no_activity_timeout_ms is not None
                else 0,
                "auto_sell_big_buy": {
                    "levels": [
                        dataclasses.asdict(level)
                        for level in cfg.rules.sell.auto_sell_big_buy_levels
                    ]
                },
                "copy_sells": cfg.rules.sell.copy_sells.value,
                "copy_sell_delay_ms": cfg.rules.sell.copy_sell_delay_ms,
            },
        },
    }


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
