"""rug_tracker CLI — add, edit, list and remove wallet trackers.

A tracker is one wallet plus its full sniper config: tracking mode, execution
mode and size, entry filters and multi-level exits. Flags take human units
(SOL, %, seconds) and are converted to the config's integer units; anything
without a flag is reachable with ``--set dotted.key=JSON``.
"""

# ruff: noqa: TRY003

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

from rugbot.domain.amounts import LAMPORTS_PER_SOL, PPM_SCALE
from rugbot.runtime.config import (
    ExecutionMode,
    SniperConfigError,
    TrackingMode,
    resolve_state_dir,
)
from rugbot.storage.config_store import (
    ConfigStore,
    load_sniper_config_db,
    set_dotted,
    sniper_to_mapping,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from rugbot.storage.config_store import Tracker

PERCENT_TO_PPM = PPM_SCALE // 100
NONE_LITERAL = "none"
TRACKER_EXECUTION_MODES = (
    ExecutionMode.OBSERVE.value,
    ExecutionMode.PAPER.value,
    ExecutionMode.SIMULATION.value,
)


def _lamports(sol: float) -> int:
    return round(sol * LAMPORTS_PER_SOL)


def _ppm(percent: float) -> int:
    return round(percent * PERCENT_TO_PPM)


def _pairs(raw: str) -> list[tuple[float, float]]:
    """Parse ``"50:50,100:100"`` into float pairs; ``none`` clears the list."""

    if raw.lower() == NONE_LITERAL:
        return []
    pairs = []
    for item in raw.split(","):
        left, sep, right = item.partition(":")
        if not sep:
            raise SniperConfigError(f"expected A:B pairs, got {item!r}")
        pairs.append((float(left), float(right)))
    return pairs


def _trailing(raw: str) -> list[dict[str, int | None]]:
    """Parse ``"20,30@100"``: drawdown %, optionally from a market cap in SOL."""

    if raw.lower() == NONE_LITERAL:
        return []
    levels = []
    for item in raw.split(","):
        drawdown, sep, min_mc = item.partition("@")
        levels.append(
            {
                "drawdown_ppm": _ppm(float(drawdown)),
                "min_market_cap_quote_base_units": (
                    _lamports(float(min_mc)) if sep else None
                ),
            }
        )
    return levels


def _take_profit(raw: str) -> list[dict[str, int]]:
    return [
        {"trigger_pnl_ppm": _ppm(pnl), "sell_fraction_ppm": _ppm(sold)}
        for pnl, sold in _pairs(raw)
    ]


def _stop_loss(raw: str) -> list[dict[str, int]]:
    return [
        {"trigger_pnl_ppm": -abs(_ppm(loss)), "sell_fraction_ppm": _ppm(sold)}
        for loss, sold in _pairs(raw)
    ]


def _dip(raw: str) -> list[dict[str, int]]:
    return [
        {"drawdown_ppm": _ppm(drop), "quote_size_lamports": _lamports(sol)}
        for drop, sol in _pairs(raw)
    ]


# (flag attribute, dotted config key, conversion from the flag's human unit)
FLAG_KEYS: tuple[tuple[str, str, Callable[[Any], object]], ...] = (
    ("mode", "tracking_mode", str),
    ("exec", "execution.mode", str),
    ("size", "execution.quote_size_lamports", _lamports),
    ("slippage", "execution.max_slippage_bps", int),
    ("prio", "execution.priority_fee_microlamports", int),
    ("tip", "execution.jito_tip_lamports", _lamports),
    ("tp", "rules.sell.take_profit_levels", _take_profit),
    ("sl", "rules.sell.stop_loss_levels", _stop_loss),
    ("trail", "rules.sell.trailing_levels", _trailing),
    ("dip", "rules.buy_the_dip.levels", _dip),
    ("no_activity", "rules.sell.no_activity_seconds", int),
    ("delay", "rules.snipe_delay_seconds", int),
    ("min_mc", "rules.min_market_cap_quote_base_units", _lamports),
    ("max_mc", "rules.max_market_cap_quote_base_units", _lamports),
    ("max_age", "rules.max_token_age_minutes", int),
    ("cooldown", "rules.follow_cooldown_seconds", int),
    ("buy_once", "rules.buy_only_once", bool),
    ("max_losses", "rules.max_consecutive_losses", int),
)


def _flag_changes(args: argparse.Namespace) -> dict[str, object]:
    """Translate the edit flags that were given into dotted config changes."""

    changes = {
        key: convert(getattr(args, attribute))
        for attribute, key, convert in FLAG_KEYS
        if getattr(args, attribute) is not None
    }
    for item in args.set:
        key, sep, raw = item.partition("=")
        if not sep:
            raise SniperConfigError(f"--set expects KEY=JSON, got {item!r}")
        changes[key] = json.loads(raw)
    return changes


def _apply(mapping: dict[str, Any], changes: dict[str, object]) -> dict[str, Any]:
    for key, value in changes.items():
        set_dotted(mapping, key, value)
    # Risk caps are per tracker: a larger buy size raises its own cap with it.
    size = mapping["execution"]["quote_size_lamports"]
    risk = mapping["risk"]
    risk["max_buy_lamports"] = max(risk["max_buy_lamports"], size)
    risk["max_exposure_lamports"] = max(
        risk["max_exposure_lamports"], risk["max_buy_lamports"]
    )
    return mapping


def _levels(levels: list[dict[str, int]], sign: str = "+") -> str:
    return (
        ",".join(
            f"{sign}{abs(level['trigger_pnl_ppm']) // PERCENT_TO_PPM}%"
            f"→{level['sell_fraction_ppm'] // PERCENT_TO_PPM}%"
            for level in levels
        )
        or "-"
    )


def _row(tracker: Tracker) -> str:
    mapping = sniper_to_mapping(tracker.config)
    sell = mapping["rules"]["sell"]
    trail = (
        ",".join(
            f"{level['drawdown_ppm'] // PERCENT_TO_PPM}%"
            for level in sell["trailing_levels"]
        )
        or "-"
    )
    take_profit = _levels(sell["take_profit_levels"])
    if sell["trailing_levels"] and sell["take_profit_levels"]:
        take_profit += " (ignored: trailing on)"
    return (
        f"{tracker.wallet:44}  {'on ' if tracker.enabled else 'off'}  "
        f"{tracker.group or '-':10}  {mapping['tracking_mode']:19}  "
        f"{mapping['execution']['mode']:10}  "
        f"{mapping['execution']['quote_size_lamports'] / LAMPORTS_PER_SOL:6.3f}  "
        f"TP {take_profit}  "
        f"SL {_levels(sell['stop_loss_levels'], '-')}  trail {trail}"
    )


def _edit_flags() -> argparse.ArgumentParser:
    edit = argparse.ArgumentParser(add_help=False)
    edit.add_argument("--mode", choices=[mode.value for mode in TrackingMode])
    edit.add_argument("--exec", choices=TRACKER_EXECUTION_MODES)
    edit.add_argument("--size", type=float, help="buy size in SOL")
    edit.add_argument("--slippage", type=int, help="max slippage in bps")
    edit.add_argument("--prio", type=int, help="priority fee in microlamports/CU")
    edit.add_argument("--tip", type=float, help="Jito tip in SOL")
    edit.add_argument(
        "--tp", help="take-profit levels PNL%%:SOLD%% (SOLD%% cumulative), or none"
    )
    edit.add_argument("--sl", help="stop-loss levels LOSS%%:SOLD%%, or none")
    edit.add_argument(
        "--trail",
        help="trailing stop drawdown%%[@MIN_MC_SOL], e.g. 20,30@100; overrides TP",
    )
    edit.add_argument("--dip", help="buy-the-dip levels DROP%%:SOL (max 3), or none")
    edit.add_argument("--no-activity", type=int, help="sell after N quiet seconds")
    edit.add_argument("--delay", type=int, help="snipe delay in seconds")
    edit.add_argument("--min-mc", type=float, help="minimum market cap in SOL")
    edit.add_argument("--max-mc", type=float, help="maximum market cap in SOL")
    edit.add_argument("--max-age", type=int, help="max token age in minutes (copy)")
    edit.add_argument("--cooldown", type=int, help="follow cooldown in seconds")
    edit.add_argument("--buy-once", action=argparse.BooleanOptionalAction)
    edit.add_argument("--max-losses", type=int, help="pause after N losses in a row")
    edit.add_argument("--group")
    edit.add_argument(
        "--set",
        action="append",
        default=[],
        metavar="KEY=JSON",
        help="set any config key, e.g. rules.sell.no_activity_seconds=30",
    )
    return edit


def build_parser() -> argparse.ArgumentParser:
    """Build the rug_tracker argument parser."""

    parser = argparse.ArgumentParser(
        prog="rug_tracker", description="Manage wallet trackers."
    )
    parser.add_argument("--state-dir", type=Path, default=None)
    sub = parser.add_subparsers(dest="cmd", required=True)
    edit = _edit_flags()
    add = sub.add_parser("add", parents=[edit], help="track a wallet")
    add.add_argument("wallet")
    add.add_argument("--preset", help="start from a saved preset")
    change = sub.add_parser("set", parents=[edit], help="edit a tracker")
    change.add_argument("wallet")
    listing = sub.add_parser("list", help="list trackers")
    listing.add_argument("--group")
    for name in ("show", "rm", "enable", "disable"):
        sub.add_parser(name).add_argument("wallet")
    preset = sub.add_parser("preset", help="save, list or remove presets")
    preset_sub = preset.add_subparsers(dest="preset_cmd", required=True)
    save = preset_sub.add_parser("save")
    save.add_argument("name")
    save.add_argument("--from", dest="source", required=True, help="tracker wallet")
    preset_sub.add_parser("list")
    preset_sub.add_parser("rm").add_argument("name")
    return parser


def _require(store: ConfigStore, wallet: str) -> Tracker:
    tracker = store.get_tracker(wallet)
    if tracker is None:
        raise SniperConfigError(f"not tracked: {wallet}")
    return tracker


def _run(args: argparse.Namespace, store: ConfigStore) -> str:  # noqa: C901, PLR0911, PLR0912
    if args.cmd == "add":
        if store.get_tracker(args.wallet) is not None:
            raise SniperConfigError(f"already tracked: {args.wallet} (use set)")
        if args.preset is None:
            mapping = sniper_to_mapping(load_sniper_config_db(args.state_dir))
            mapping["execution"]["mode"] = ExecutionMode.PAPER.value
        else:
            preset = store.get_preset(args.preset)
            if preset is None:
                raise SniperConfigError(f"unknown preset: {args.preset}")
            mapping = preset
        mapping["target"] = {"kind": "wallet", "id": args.wallet}
        tracker = store.save_tracker(
            _apply(mapping, _flag_changes(args)), group=args.group, enabled=True
        )
        return _row(tracker)
    if args.cmd == "set":
        current = _require(store, args.wallet)
        mapping = _apply(sniper_to_mapping(current.config), _flag_changes(args))
        tracker = store.save_tracker(
            mapping,
            group=current.group if args.group is None else args.group,
            enabled=current.enabled,
        )
        return _row(tracker)
    if args.cmd in ("enable", "disable"):
        current = _require(store, args.wallet)
        tracker = store.save_tracker(
            sniper_to_mapping(current.config),
            group=current.group,
            enabled=args.cmd == "enable",
        )
        return _row(tracker)
    if args.cmd == "list":
        trackers = store.list_trackers(args.group)
        return "\n".join(_row(tracker) for tracker in trackers) or "no trackers"
    if args.cmd == "show":
        tracker = _require(store, args.wallet)
        return json.dumps(
            {
                "group": tracker.group,
                "enabled": tracker.enabled,
                "config": sniper_to_mapping(tracker.config),
            },
            indent=2,
            sort_keys=True,
        )
    if args.cmd == "rm":
        if not store.delete_tracker(args.wallet):
            raise SniperConfigError(f"not tracked: {args.wallet}")
        return f"removed {args.wallet}"
    if args.preset_cmd == "save":
        store.save_preset(args.name, _require(store, args.source).config)
        return f"preset {args.name} saved from {args.source}"
    if args.preset_cmd == "list":
        return "\n".join(store.list_presets()) or "no presets"
    if not store.delete_preset(args.name):
        raise SniperConfigError(f"unknown preset: {args.name}")
    return f"removed preset {args.name}"


def main(argv: list[str] | None = None) -> int:
    """Run one rug_tracker command."""

    args = build_parser().parse_args(argv)
    args.state_dir = resolve_state_dir(args.state_dir)
    try:
        print(_run(args, ConfigStore(state_dir=args.state_dir)))
    except (SniperConfigError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    return 0
