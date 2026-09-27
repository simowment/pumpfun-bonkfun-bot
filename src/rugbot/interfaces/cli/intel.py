"""rug_intel — every operator / cabal / rugger analysis tool behind one command.

``rug_intel <tool> ...`` forwards the rest of the arguments to that tool, so
each keeps its own options (``rug_intel <tool> --help``).
"""

from __future__ import annotations

import importlib
import sys

# tool -> (module under rugbot.interfaces.cli, what it answers)
TOOLS: dict[str, tuple[str, str]] = {
    "check": ("check_mint", "mint one-liner: creator, B0/B1 bundle, rugged, copy pick"),
    "triage": ("triage", "mint data sheet: entity activity, bundlers, backtest"),
    "profile": ("entity_profile", "funder or mint -> burner set and launch outcomes"),
    "history": ("entity_history", "creator/funder launch history + realistic backtest"),
    "chain": ("chain", "walk a wallet's funding chain to its hub and siblings"),
    "graph": ("graph", "classify an operator's wallet graph from one seed"),
    "wallet": ("wallet", "resolve a token/wallet, intelligence, backtest optimizer"),
    "cluster": ("cluster", "discover cluster tokens and run Bible backtests"),
}


def main(argv: list[str] | None = None) -> int:
    """Dispatch to one analysis tool."""

    args = sys.argv[1:] if argv is None else argv
    if not args or args[0] not in TOOLS:
        print("usage: rug_intel <tool> [args...]\n")
        for name, (_, summary) in TOOLS.items():
            print(f"  {name:8} {summary}")
        return 0 if args[:1] in ([], ["-h"], ["--help"]) else 2
    module = importlib.import_module(f"rugbot.interfaces.cli.{TOOLS[args[0]][0]}")
    return module.main(args[1:])
