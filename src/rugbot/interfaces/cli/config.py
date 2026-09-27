"""rug_config CLI — show/set DB-backed configs."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from rugbot.runtime.config import SniperConfigError, resolve_state_dir
from rugbot.storage.config_store import (
    load_scalper_config_db,
    load_sniper_config_db,
    scalper_to_mapping,
    set_config_db,
    set_dotted,
    sniper_to_mapping,
)
from rugbot.utils.logger import get_logger

logger = get_logger(__name__)


def _parse_value(raw: str) -> object:
    # try JSON
    try:
        return json.loads(raw)
    except Exception:
        return raw


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="rug_config", description="DB-backed config show/set"
    )
    p.add_argument(
        "--state-dir", type=Path, default=None, help="state dir (default .state/watch)"
    )
    sub = p.add_subparsers(dest="cmd", required=True)
    show = sub.add_parser("show")
    show.add_argument("--type", choices=["sniper", "scalper"], default="sniper")
    show.add_argument("--json", action="store_true")
    s = sub.add_parser("set")
    s.add_argument("--type", choices=["sniper", "scalper"], required=True)
    g = s.add_mutually_exclusive_group(required=True)
    g.add_argument(
        "--key", help="dotted key e.g. execution.quote_size_lamports or wallets"
    )
    g.add_argument(
        "--file",
        type=Path,
        help="JSON/YAML file with full mapping (import-only; DB is the source of truth)",
    )
    s.add_argument("--value", help="value for --key (JSON literal or string)")
    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    state_dir = (
        resolve_state_dir(args.state_dir) if args.state_dir else resolve_state_dir(None)
    )
    try:
        if args.cmd == "show":
            if args.type == "sniper":
                cfg = load_sniper_config_db(state_dir)
                mapping = sniper_to_mapping(cfg)
            else:
                cfg = load_scalper_config_db(state_dir)
                mapping = scalper_to_mapping(cfg)
            if args.json:
                print(json.dumps(mapping, sort_keys=True, indent=2))
            else:
                print(json.dumps(mapping, sort_keys=True, indent=2))
            return 0
        elif args.cmd == "set":
            if args.file:
                text = args.file.read_text(encoding="utf-8")
                # try json then yaml
                try:
                    mapping = json.loads(text)
                except Exception:
                    import yaml

                    mapping = yaml.safe_load(text)
                if type(mapping) is not dict:
                    print("mapping must be a dict", file=sys.stderr)
                    return 1
                set_config_db(state_dir, args.type, mapping)
                print(f"{args.type} config written")
                return 0
            else:
                if args.value is None:
                    print("--value required with --key", file=sys.stderr)
                    return 1
                # load existing mapping
                cur = (
                    sniper_to_mapping(load_sniper_config_db(state_dir))
                    if args.type == "sniper"
                    else scalper_to_mapping(load_scalper_config_db(state_dir))
                )
                val = _parse_value(args.value)
                set_dotted(cur, args.key, val)
                set_config_db(state_dir, args.type, cur)
                print(f"{args.type}.{args.key} = {val!r}")
                return 0
    except SniperConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 1
